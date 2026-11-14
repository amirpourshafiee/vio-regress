# Nightly VIO job on a GitHub-hosted runner (sessions S5a, S5b, S5c)

`.github/workflows/nightly.yml` runs **daily at 11:17 UTC** and on manual dispatch
(`workflow_dispatch`; inputs `sequences`, default `MH_01_easy V1_01_easy MH_03_medium`, and
`runs`, default `2`; a scheduled run uses the same defaults). Since S5b it **pulls** the image pinned in `docker/IMAGE_REF` and checks
its registry digest (S5a built the image instead). Then, for each sequence, it fetches,
runs, scores and deletes the bag, printing `df -h /` at each phase. It judges all sequences
with the gate (fails closed), writes a job summary, and uploads `results/` plus
`junit-gate.xml` (never the dataset).

Sections 1–2 below record the S5a design and its first run. Section 3 is the S5b image
design. Section 4 (S5c) covers the schedule and fetch failures.

## Can a standard runner do it?

| | GitHub-hosted runner, public repo | Our job |
|---|---|---|
| CPU / RAM | **4 CPU, 16 GB** ([runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)) | build used 4 cores, about 6 GB RAM at peak |
| Disk | **14 GB SSD** (same page; measured 145 GB in run 43573219393, see S5b) | image build peaks near **10 GB**; bag 2.7 GB plus a 1.6 GB temp zip while fetching |
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

Recommended for S5b; S5a keeps the self-contained build. (Done in S5b: see below.)

## S5a: what the first run would say (NEEDS RE-BASELINE)

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

**Measured (run 43558839448, 2026-11-14, MH_01_easy, 2 runs):** the verdict was
`[NEEDS-REBASELINE]` as predicted. The result key was `sha256:4eaac9a1` and the baseline
key `sha256:f08eacb1`. The runner had the same CPU as the baseline host (AMD EPYC 7763,
4 vCPU, identical SIMD flags), and **both runs reproduced `baselines/MH_01_easy.tum`
exactly** (RPE and ATE ratio 1.000). Only the key differed.

## S5b: one published image, keyed by its registry digest

- `publish-image.yml` (manual, `packages: write`, the only workflow with it) builds
  `docker/Dockerfile.openvins` once and pushes it to GHCR. Run 43561755606 published tag
  `d492d65` (OV_REF `93adc241`).
- **`docker/IMAGE_REF`** holds that image as one line, `<repo>@sha256:<64 hex>`. nightly
  requires it to match exactly `^ghcr\.io/amirpourshafiee/vioreg-openvins@sha256:[0-9a-f]{64}$`
  (one line, no tag, no other repository), pulls it, and fails unless `docker image inspect` RepoDigests lists
  exactly that ref. It then exports the ref as `IMAGE`. nightly keeps `contents: read`:
  the package is public, so the pull is anonymous. If the package became private the pull
  would fail rather than widen the token.
- **`image_digest` is the registry digest** (`src/vioreg/image_ref.py`). The runner verifies
  it before any run and writes `run<k>.image_digest`. The scorer records it and fails the
  result if a run's sidecar differs. A local image ID is never used: it changes on every
  rebuild (`apt-get update`) and with Docker's storage mode (containerd reports the
  manifest digest, overlay2 the config digest). See the README.
- Before pinning, the published image was inspected (`docker history --no-trunc`, plus `/ws`
  and the home directories listed in a throwaway container). It contains only Ubuntu, ROS
  Noetic, our apt packages, OpenVINS at `93adc241`, and the upstream config. It has no
  dataset, credentials or personal paths.
- **Re-baseline (2026-11-14, Codespace):** 2 serial runs per sequence on the pinned image.
  Every run was pose-equivalent to `baselines/<seq>.tum` (2767 / 2776 / 2296 poses), and
  every metric was identical. Following the agreed rule, nothing was re-recorded. The S3
  baseline was re-keyed to the new digest in `docs/rebaseline_s5b/baseline.json`, which
  includes the evidence. Thresholds were regenerated with the margins unchanged. Their diff
  is only the three `image_digest` lines and the provenance comment. The gate on those runs:
  PASS 3, including the ATE geomean.

**First green nightly (run 43573219393, 2026-11-14, `main` at `dcef94c`, defaults: all 3
sequences, 2 runs each, 13 min):** gate **PASS 3**, 5 passed, 0 skipped. That's the three
per-sequence checks plus the ATE geomean. Every key matched (`image_digest sha256:29b31b7a`,
config `5adca0e7`). Both runs of every sequence reproduced `baselines/<seq>.tum` exactly,
with RPE and ATE ratio 1.000.
- **Different CPU, same poses.** The runner was an Intel Xeon Platinum 8573C (4 vCPU,
  AVX-512). The baseline host and the S5a runner were an AMD EPYC 7763 (no AVX-512). That
  is evidence for one CPU pair, not a guarantee for every host.
- **The storage-mode problem showed up in practice.** On the runner, `docker image inspect`
  reported `.Id` = `sha256:a19e5ab0…` (the config digest, as the publish build logged), not
  the registry digest `sha256:29b31b7a…`. The harness recorded the registry digest, so the
  key matched. Under the pre-S5b scheme this run would have been NEEDS-REBASELINE again.

Disk on the runner: the pull is about 1.55 GB compressed and 7.8 GB unpacked. Each
sequence then peaks at about 3 GB while fetching (bag 2.5–2.7 GB, after a 1.6 GB temp
zip that is deleted first). Deleting each bag before the next fetch keeps the peak at one
bag. The loop also refuses to fetch below 5 GB free.

**Measured (run 43573219393): the runner's disk was not 14 GB.** `/` was a 145 GB
`/dev/root` with 86 GB free at the start, and 98 GB free after the "Free disk" step and the
pull. Each fetch used about +3 GB, and deleting the bag gave it back (47 → 50 → 47 GB used).
So the 14 GB budget in the table above (from the documented runner spec) was very
conservative for this run. Hosted runner sizes can change, so the "Free disk" step and the
5 GB guard stay as cheap insurance.

**Changing the image** means a new publish, a new `docker/IMAGE_REF`, and a deliberate
re-baseline. If the new image reproduces the baseline poses, re-key as above. If it does
not, re-record the baseline and review the margins.

## S5c: daily schedule, main only, fetch failures labelled

**Schedule.** `cron: "17 11 * * *"` runs every day at 11:17 UTC. That's 3:17 AM Pacific
standard time, or 4:17 AM in summer time, because cron uses UTC. The minute is off the top
of the hour on purpose: GitHub's scheduler is busiest at :00, and scheduled runs can then
start late or be dropped. A scheduled run uses the workflow file and code at the **latest
commit on the default branch**, and has no inputs. The job env falls back to the input
defaults (`inputs.sequences || '…'`), and a test keeps the two identical. The schedule only
takes effect once this file is on `main`.

**Main only.** The job has `if: github.ref == 'refs/heads/main'`. A dispatch from any other
branch shows the job as *skipped* (not failed, and judging nothing), so start manual runs
from `main`. `publish-image.yml` has the same job-level guard, so the image can only be
built and pushed from `main`.

**No overlap.** The `concurrency` group `nightly-vio` with `cancel-in-progress: false` makes
a second run wait while one is running. A running job is never cancelled. If more runs
arrive, GitHub keeps only the newest one pending.

**Scheduled runs can be switched off by GitHub.** In a public repository, GitHub
automatically disables scheduled workflows after **60 days without repository activity**
(no commits and similar). A disabled workflow simply stops running: no red run appears, so
check the Actions tab if the daily runs stop. To re-enable it:
- in the browser: **Actions → nightly-vio → "Enable workflow"** (the banner at the top), or
- `gh workflow enable nightly-vio` with a token that has `actions: write`. The Codespace
  token cannot do this; the owner does it.
Any commit to the repository also resets the 60-day clock.

**Fetch retries.** The EuRoC server rate-limits. `src/vioreg/fetch.py` retries at two levels:
1. each HTTP range request retries HTTP 429/500/502/503/504, timeouts and refused or reset
   connections up to 5 times, waiting 10, 20, 40, 80 and 160 s;
2. `--attempts 3 --backoff-s 60` restarts the whole sequence after a transient failure,
   including a connection dropped mid-download, waiting 60 s and then 120 s. Partial files
   are always deleted.
A transient failure that survives every retry exits **75**. Any other fetch failure
(integrity or CRC mismatch, missing member, size guard, unknown sequence, permanent HTTP
error) exits **1**. Either way, `--status-dir results` writes `results/<seq>.fetch.json`
(`ok`, `kind` = ok | unavailable | error, `attempts`, `reason`). It is written on success too,
so a stale failure can never mislabel a later run.

**Fetch failure vs gate failure.** When `results/<seq>.fetch.json` records a failure, the
gate gives that sequence the verdict **DATA FETCH FAILED**, with the message *"data fetch
failed, not a quality result"* and the reason. This takes precedence over every other
verdict, because without data every other failure is a consequence. The ATE aggregate
reports *"not judged: data fetch failed for <seq>, not a quality result"*. Both still
**fail** the gate (fail closed: no data is never a pass), but the job summary and the gate
message say plainly that VIO quality was not measured. An unreadable status file counts as
a fetch failure. With no status file at all (local runs that fetch separately), the gate
makes no fetch claim.

## Pieces

| Step | Command |
|---|---|
| Host fingerprint (first step) | `grep 'model name' /proc/cpuinfo`, SIMD flags → `results/host_ci_cpuinfo.txt`, then `python -m vioreg.hostinfo --out results/host_ci.json` |
| Pull | `docker pull "$(cat docker/IMAGE_REF)"`, then check RepoDigests contains it; `IMAGE` = that ref |
| Per sequence | fetch `bash scripts/fetch_euroc.sh data/euroc <seq> --attempts 3 --backoff-s 60 --status-dir results` (5 GB free guard; retries above; exit 75 = unavailable) → run `RUNS=<n> bash scripts/run_sequence.sh <seq> results` (serial; verifies the digest) → score `python -m vioreg.score --seqs <seq> --out results --image "$IMAGE"` → `rm` the bag; `df -h /` at each phase |
| Gate | `HARNESS_RESULTS=results HARNESS_REQUIRE_RESULTS=1 HARNESS_SEQUENCES=<seqs> pytest -m sequence --junitxml=junit-gate.xml` |
| Summary | `python -m vioreg.report results --seqs <seqs> --host results/host_ci.json >> $GITHUB_STEP_SUMMARY` |

The default covers all three baseline sequences, so the ATE geometric mean is judged. A
one-sequence run is judged with `HARNESS_SEQUENCES`. The per-sequence checks run; the ATE
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
- S5b: pull of `docker/IMAGE_REF` with a digest-form check and a RepoDigests check, no `docker build`,
  no `packages:`, login or secrets; a per-sequence loop with fetch → run → score → delete the bag in order,
  `df -h /` at each phase and a `trap` that removes the bag; the default covers every baseline sequence
- S5c: exactly one schedule (`17 11 * * *`) plus dispatch; scheduled-run env equals the input
  defaults; job-level `if` on `refs/heads/main` in nightly and publish-image; concurrency
  without cancel; the fetch call uses `--attempts 3 --backoff-s 60 --status-dir results` and
  labels exit 75 and other failures "data fetch failed, not a quality result"
