# Nightly VIO job on a GitHub-hosted runner (session S5a)

`.github/workflows/nightly.yml`, **manual trigger only** (`workflow_dispatch`; inputs
`sequences`, default `MH_01_easy`, and `runs`, default `2`). There is no schedule yet. It
builds the pinned image, fetches the sequence, runs it, scores it, judges it with the gate
(fails closed), writes a job summary, and uploads `results/` plus `junit-gate.xml` (never
the dataset).

## Can a standard runner do it?

| | GitHub-hosted runner, public repo | Our job |
|---|---|---|
| CPU / RAM | **4 CPU, 16 GB** ([runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)) | build used 4 cores, about 6 GB RAM at peak |
| Disk | **14 GB SSD** (same page) | image build peaks near **10 GB**; bag 2.7 GB plus a 1.6 GB temp zip while fetching |
| Time | **6 h per job** ([limits](https://docs.github.com/en/actions/reference/limits)) | build 15–20 min, fetch ~5 min, 2 runs × ~45 s |
| Cost | standard runners are **free and unlimited for public repos** ([billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions); this repo is public) | n/a |
| Cache | 10 GB per repo (billing page) | not used: the dataset is not cached |

Verdict: CPU, memory, time and cost fit easily. **Disk does not fit as documented**: about
10 GB of image build plus about 4.3 GB of fetch peak is more than 14 GB. The workflow
therefore:
1. builds first, then `docker builder prune -af` before any data lands;
2. deletes preinstalled toolchains the job never uses (`/usr/share/dotnet`, Android SDK,
   GHC, CodeQL, boost);
3. prints `df -h /` after each stage and **refuses to fetch below 5 GB free** with a clear
   error, instead of failing obscurely mid-run.

How much (2) frees is not documented, so the first run measures it.

Alternative: **push the image to GHCR once and pull it by digest.**
- Gains: no 15–20 min build, no build-cache peak, and above all the **same image digest**
  as the baseline. That is the only way the baseline key can ever match on CI (next
  section).
- Costs: a one-time push with `packages: write` (outside this job; the job stays
  `contents: read` if the package is public); registry storage; and publishing a
  ~1.6 GB image (OpenVINS is GPL-3, so publishing source and image is fine).

Recommended for S5b; S5a keeps the self-contained build.

## What the first run will most likely say: NEEDS RE-BASELINE

The baseline key is (vio_commit, image_digest, config_hash). On the runner:
- `vio_commit` and `config_hash` will match: the same `OV_REF` and the same config files
  baked into the image.
- `image_digest` will **not** match:
  1. A rebuild is not reproducible: `osrf/ros:noetic-desktop-full` is a moving tag, and
     `apt-get update` pulls current packages.
  2. Docker reports image IDs differently per store. Locally (Docker 29, **containerd
     image store**) `docker image inspect .Id` is the **manifest-list digest**
     (`f08eacb1…`). The classic overlay2 store reports the **config digest** (the build
     log showed `a9a6f2fe…` for the same image). So even a byte-identical image can carry
     a different `image_digest`.

The gate therefore fails with `[NEEDS-REBASELINE]`, which is not a quality failure. The job
summary still answers the cross-machine question, labelled *informational*:
- (a) whether the runner reproduced the baseline poses exactly (`pose_equivalent`), and
  whether its 2 runs agree with each other;
- (b) the RPE ratio vs the baseline, with the margin.

**To fix for S5b:** record a store-independent digest (the registry `RepoDigest` after a
GHCR push, or the config digest from both stores), and pin the base image by digest in
`docker/Dockerfile.openvins`.

## Pieces

| Step | Command |
|---|---|
| Host fingerprint (first step) | `grep 'model name' /proc/cpuinfo`, SIMD flags → `results/host_ci_cpuinfo.txt`, then `python -m vioreg.hostinfo --out results/host_ci.json` |
| Fetch | `bash scripts/fetch_euroc.sh data/euroc <seqs>` (5 GB size guard; 429 retry/backoff) |
| Run | `RUNS=<n> bash scripts/run_sequence.sh <seq> results` (serial replay) |
| Score | `python -m vioreg.score --seqs <seqs> --out results --image vioreg-openvins:v2.7` |
| Gate | `HARNESS_RESULTS=results HARNESS_REQUIRE_RESULTS=1 HARNESS_SEQUENCES=<seqs> pytest -m sequence --junitxml=junit-gate.xml` |
| Summary | `python -m vioreg.report results --seqs <seqs> --host results/host_ci.json >> $GITHUB_STEP_SUMMARY` |

A one-sequence run is judged with `HARNESS_SEQUENCES`. The per-sequence checks run; the ATE
geometric mean is defined over all baseline sequences, so it is **skipped with its reason**
rather than turned into a single-sequence ATE gate. Inputs reach the shell only through
`env` and are validated (sequence-name regex, `runs` 1–9) before use.

## Local checks

`bash scripts/check_workflows.sh` runs actionlint (pinned v1.7.7, checksum-verified; with
shellcheck from `shellcheck-py==0.10.0.1` it also lints every `run:` block) and
`tests/test_workflows.py`. The tests cover:
- dispatch-only trigger, no schedule, inputs and defaults
- `contents: read`
- the fingerprint is the first step
- every referenced script and module exists
- fail-closed gate env
- judging steps run after a VIO failure
- the upload is unconditional and never includes `data/`
- no `${{ inputs }}` in `run:` blocks
