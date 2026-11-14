# vioreg part 1: a regression gate for visual-inertial odometry

This is part 1 of vioreg. It covers a working regression gate and the evidence behind its
design.

**Not built:** the later stages planned for the project, which are failure windows, a
labeler, human labels, a vision-language model (VLM) judge, and agreement measured with
Cohen's kappa. Unit-tested starter code for the windows, the judge and kappa exists in
`src/vioreg/`, but it has **never been run on real data**. There are **no labels and no
results**. Section 9 lists these stages.

## 1. What it is and why

vioreg is an open-source regression gate for visual-inertial odometry (VIO), the estimation
of a camera's pose (position and orientation) from images plus an inertial measurement unit.
It replays sequences from the public EuRoC MAV (European Robotics Challenge, micro aerial
vehicle) dataset through OpenVINS, an open-source VIO estimator. It scores the output
trajectory against ground truth and gives a pass or fail verdict for a commit. The gate
judges **estimator output only**, using two error measures. Absolute trajectory error (ATE)
is the position error of the whole trajectory after alignment. Relative pose error (RPE) is
the drift over short windows. The question is narrow: are the trajectories worse than a
recorded baseline on the same inputs?

It runs at two levels in continuous integration (CI). The **pull request tier** (also run on
every push to `main`) runs the unit tests and the gate on stored golden result files. It
**does not run OpenVINS** and needs no dataset. **Only the nightly** (scheduled daily) **and
manual dispatches** of it run the estimator, replaying three sequences: MH_01_easy, V1_01_easy
and MH_03_medium.

## 2. Design

**A pinned image, identified by its registry digest.** OpenVINS is built once into a Docker
image, pushed to the GitHub Container Registry (GHCR), and pinned as one line in
`docker/IMAGE_REF` in the form `<repo>@sha256:<digest>`. A local image ID would not work as
an identity: it changes on every rebuild, because `apt-get update` makes builds
non-reproducible. It also depends on Docker's storage mode, because the containerd store
reports a manifest digest and the classic overlay2 store reports a config digest. The runner
refuses to start if the local image does not carry exactly the pinned digest.

**A keyed baseline.** Each baseline entry is keyed by (sequence, `vio_commit`,
`image_digest`, `config_hash`). The config hash covers the estimator config and the two
calibration files it references. If no entry matches a result's key, the gate fails with
"needs re-baseline", which is not a quality verdict. So any OpenVINS bump or config edit,
even an innocent one, needs a deliberate re-baseline, and the accuracy margins only ever
compare results that share a key. Every result JSON also records `harness_commit`. A result
without all four provenance fields is invalid.

**Serial replay.** OpenVINS reads the bag itself in a serial node, so image updates are
synchronous with the inertial feed. Realtime replay through `rosbag play` gave a different
answer on MH_01_easy (ATE 60.086 mm against 90.919 mm serial) and diverged under CPU load or
a faster rate. The harness therefore pins serial replay.

**The gate checks**, in order:

| Check | Rule | Margin |
|---|---|---|
| Hard checks, per sequence | result present and parsable, provenance present, `ok` true, no failed run | n/a |
| Coverage | coverage not below baseline minus margin | 0.01 |
| Pose count | associated poses not below baseline minus margin | 4 |
| RPE ratio, per sequence | RPE RMSE / baseline RPE RMSE | at most 1.28 |
| ATE geometric mean, all sequences | geometric mean of per-sequence ATE ratios | at most 1.5 |
| Exact poses, same key | every run compared pose by pose with the stored baseline trajectory | warning only |

<!-- source: configs/thresholds.yaml (margins), docs/gate.md ("The design") -->

The pose-count allowance of 4 covers a recorder race that can lose up to 2 poses at each
end of an otherwise identical run. There is deliberately no single-sequence ATE limit
(section 4 explains why).

The ATE implementation was cross-checked against two independent tools on MH_01_easy. It
matches ov_eval, the evaluation package that ships with OpenVINS, to within 1e-9 m with the
same alignment and association window. evo has no alignment restricted to yaw plus
translation (4 degrees of freedom, DoF), so it was compared using full 6-DoF alignment,
where vioreg and evo agree exactly.

<!-- source: docs/crosscheck.md -->

**Fail-closed behavior.** Missing results, unparsable JSON, missing metrics, low coverage,
an unknown sequence or a missing baseline key all **fail**. None of them is a skip or a
pass. A download failure is labelled separately: if the EuRoC server stays unavailable
after every retry, that sequence gets the verdict **DATA FETCH FAILED**, "data fetch
failed, not a quality result". It still fails the gate, because no data is never a pass, but
the message says plainly that VIO quality was not measured. When one sequence is missing,
the ATE aggregate reports "not judged" and also fails.

<!-- source: docs/ci.md ("Fetch failure vs gate failure"), CLAUDE.md ("Harness invariants") -->

## 3. Determinism evidence

| Where | CPU | Runs | Observed |
|---|---|---|---|
| Codespace, session S3 | AMD EPYC 7763 | 90 serial runs (3 sequences, 3 variants, 10 runs each) | trajectory files byte-identical within each sequence; ATE and RPE spread exactly 0; 0 of 100 runs failed |
| GitHub runner, run 43558839448 | AMD EPYC 7763 | MH_01_easy, 2 runs | both runs reproduced the baseline trajectory exactly; verdict NEEDS-REBASELINE only because the image was rebuilt |
| Codespace, re-baseline on the pinned image | not recorded | 3 sequences, 2 runs each | every run pose-equivalent to the stored baseline; every metric identical |
| GitHub runner, run 43573219393 | Intel Xeon Platinum 8573C (AVX-512) | 3 sequences, 2 runs each | both runs of every sequence reproduced the baseline exactly; RPE and ATE ratio 1.000; gate PASS |

<!-- source: docs/variance/variance.md (S3 runs), docs/ci.md ("S5a", "S5b", "First green nightly"; baseline host CPU on the "Different CPU, same poses" line) -->

Serial OpenVINS was **observed** to reproduce the baseline poses exactly on two CPU families,
AMD (without AVX-512) and Intel (with AVX-512). That is evidence for one pair of CPUs and one
image. It is not a proof that every host will match. That is why an exact-pose mismatch
under the same key is a warning, not a failure: if a future runner produces slightly
different floats, the ratio margins decide.

On a fixed host, 10 identical runs are 1 sample. The nightly uses 2 runs per sequence,
enough to notice if determinism ever breaks.

## 4. Sensitivity findings

Determinism means a tolerance cannot come from run-to-run noise, because there is none. So
the question became: how far do metrics move under changes that are innocent? Single-setting
changes (start offset, feature count, FAST corner threshold, gravity constant) were tried one
at a time.

| Sequence | ATE, % of baseline | RPE, % of baseline |
|---|---|---|
| MH_01_easy | 78.4 to 173.4 | 99.7 to 106.2 |
| V1_01_easy | 86.5 to 101.0 | 99.4 to 100.7 |

<!-- source: docs/sensitivity.md ("Is RPE less sensitive than ATE?") -->

Lowering the FAST threshold from 20 to 18 moved MH_01_easy's ATE by +73.4% and its RPE by
+5.6%. RPE measures local drift over 1 s windows, so it does not accumulate a different
initial bias or gravity estimate. It is the steadier signal. It also sees less: a slow drift
or a wrong global alignment is invisible in 1 s windows.

On MH_03_medium, one innocent change made the estimator genuinely fail. Raising the FAST
threshold from 20 to 22 diverged to 4,437 km of error with coverage unchanged at 0.979. The
hard checks did not see it; only the ratios did. Unchanged coverage does not mean a healthy
filter.

Against three known-bad variants (gravity constant 9.0, feature budget cut from 200 to 50,
and a units bug in the inertial noise), the signals separated like this:

| Signal | Legitimate, fine outcome (max) | Known-bad (min) | Separates? |
|---|---|---|---|
| RPE ratio, per sequence | 1.145 | 1.431 | yes; margin 1.28 is the geometric midpoint |
| ATE geometric mean, 3 sequences | 1.286 | 1.306 | no; margin 1.5 is a backstop only |
| Coverage, pose count, `ok` | never moved | never moved | no |

<!-- source: docs/gate.md ("Separation and the chosen margins", "What this means") -->

This is why the gate uses the per-sequence RPE ratio as its main signal and treats ATE only
as an aggregate backstop. The room is thin: it rests on one known-bad variant and one
legitimate one, from a small one-at-a-time sample on one host. It is an observed gap, not a
bound.

## 5. Fault injection: does the gate catch real input problems?

Faulted **copies** of each bag were made inside the pinned image (the original is mounted
read-only and its checksum never changed) and judged by the real gate with **unchanged**
margins. Sample size: 15 settings per sequence, 45 in all, each replayed with 2 serial runs.
imu-noise used one seed (seed 0); the other faults are deterministic rewrites with no seed.
In all 45 settings the two runs gave bit-identical ATE, so each setting is one sample, not
two, and no setting was repeated with other seeds. Results kept the baseline key, so every failure is a
quality verdict. An identity copy (no fault) reproduced the baseline exactly on every
sequence. The target set before the runs was that the gate catch **at least 3 of the 5**
planned fault types. **It was not met.** The gate caught 2 of 5, on every sequence.

| Fault | Planned parameter | MH_01_easy | V1_01_easy | MH_03_medium | Signal |
|---|---|---|---|---|---|
| drop-frames | keep every 2nd stereo pair | caught | caught | caught | pose count (1371 < 2746 − 4; 1377 < 2757 − 4; 1133 < 2275 − 4) |
| time-offset | camera stamps shifted by 1, 5, 20, 30, 40, 50, 100, 200 ms | caught at 40 ms and above (threshold between 30 and 40 ms) | caught at 100 ms and above (threshold between 50 and 100 ms) | caught at 100 ms and above (threshold between 50 and 100 ms) | RPE ratio and ATE geometric mean, after divergence |
| imu-noise | σ_w 0.01 rad/s, σ_a 0.2 m/s² | not caught (RPE 1.070) | not caught (RPE 1.003) | not caught (RPE 1.176) | none |
| gamma | 2.2 | not caught (RPE 0.983) | not caught (RPE 0.991) | not caught (RPE 1.048) | none |
| blur | 9 px horizontal | not caught (RPE 1.145, see note) | not caught (RPE 1.010) | not caught (RPE 1.091) | none |

<!-- source: docs/faults.md ("Results (MH_01_easy ...)", "Other sequences"), labels/fault_manifest.csv -->

Offsets tested on every sequence: 1, 5, 20, 30, 40, 50, 100 and 200 ms. The grid has gaps,
so each threshold is only known to lie between two tested offsets:

| Sequence | Largest tested offset not caught (RPE ratio) | Smallest tested offset caught | Threshold lies |
|---|---|---|---|
| MH_01_easy | 30 ms (1.187) | 40 ms | between 30 and 40 ms |
| V1_01_easy | 50 ms (1.010) | 100 ms | between 50 and 100 ms |
| MH_03_medium | 50 ms (0.985) | 100 ms | between 50 and 100 ms |

<!-- source: docs/faults.md ("Sample size", MH_01_easy results table, "Other sequences" table) -->

**Note on blur 9 px on MH_01_easy.** Its RPE ratio, 1.145, equals at three decimals the
largest RPE ratio seen for a legitimate change (1.145, MH_03_medium started 2 s early; section
4). These are two different measurements that happen to round to the same value. So blur 9
sits at the edge of normal variation as measured so far, and well below the 1.28 margin.
Calling it "not caught" is correct for this gate, but it is not evidence that blur 9 does
nothing to the estimator.

<!-- source: docs/faults.md (blur 9 row), docs/gate.md ("Separation and the chosen margins") -->

Time offsets are not caught gradually. The configuration estimates the camera-to-inertial
time offset online, which absorbs small offsets: on V1_01_easy and MH_03_medium every tested
offset up to 50 ms was absorbed, and on MH_01_easy every tested offset up to 30 ms. Past a
sequence-specific point, somewhere in the untested gap, the estimator diverges to kilometres
of error. Below that point the gate sees almost nothing.

One stronger setting per fault was chosen up front and reported separately. Blur of 25 px
was caught only on MH_03_medium (RPE 1.410), the sequence with faster motion. Inertial
noise at 5 times the planned level produced no poses at all on any sequence (see section 6).

**Why so much is invisible.** The gate reads trajectory output only. It never inspects
images or inertial data. A degraded input that the estimator tolerates leaves no trace in
the output, so the gate cannot see it, by design. Gamma is the clearest case: the front end
equalizes image histograms, which undoes any monotonic brightness change, so the tracker sees
almost the same images. That is estimator robustness, not a gate defect. Two properties of
the gate add to it. First, because ATE is gated only as a three-sequence geometric mean, a
fault confined to one sequence is diluted to roughly its cube root. Second, `ok` stays true
on a diverged run, so divergence is caught only by the ratios. The project owner decided to
keep the gate as it is and document these gaps, rather than tune margins to make faults fail.

## 6. Known limitations and open items

- **Inertial noise at 5 times the planned level gives no poses, and the cause is
  unexplained.** The gate fails it closed (`ok` false), but that is "no output", not "worse
  output", so it is not counted as a catch. The estimator stops early with no error at the
  log level used. A re-run with more verbose logging is the next step.
- **The two variant golden results are only re-keyed.** The legitimate and known-bad variant
  results used by the pull request tests were re-keyed to the pinned image's digest, not
  re-run on it.
- **The exact-pose check is still a warning.** A same-key run with different poses does not
  fail the gate. This is deliberate until cross-host behavior is known on more hardware.
- **Licensing.** The published image contains OpenVINS, which upstream distributes under
  GPL-3. I have not verified the license of everything in the image myself.

<!-- source: docs/faults.md, docs/gate.md ("Not known yet"), docs/ci.md (GPL-3 note), NEXT.md -->

## 7. CI evidence

- First green nightly, all 3 sequences, gate PASS:
  <https://github.com/amirpourshafiee/vio-regress/actions/runs/43573219393>
- First CI run before the image was pinned, which failed as predicted with NEEDS-REBASELINE
  while reproducing the baseline poses exactly:
  <https://github.com/amirpourshafiee/vio-regress/actions/runs/43558839448>
- Image publish run, which built and pushed the pinned image:
  <https://github.com/amirpourshafiee/vio-regress/actions/runs/43561755606>

No red-build demo run (the nightly with a `fault` input) is recorded in the repository yet.
`docs/ci.md` describes how to start one and what it should print.

<!-- source: docs/ci.md (run IDs); URLs are github.com/<repo>/actions/runs/<run ID> -->

## 8. Reproduce it

Install and run the fast tests, then the pull request gate tier on the stored results:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest -m "not sequence and not slow"
HARNESS_RESULTS=tests/golden_results HARNESS_REQUIRE_RESULTS=1 pytest -m sequence
```

A full run of one sequence on the pinned image, as the nightly does it:

```bash
docker pull "$(cat docker/IMAGE_REF)"
bash scripts/fetch_euroc.sh data/euroc MH_01_easy --attempts 3 --backoff-s 60 --status-dir results
RUNS=2 bash scripts/run_sequence.sh MH_01_easy results
python -m vioreg.score --seqs MH_01_easy --out results --image "$(cat docker/IMAGE_REF)"
HARNESS_RESULTS=results HARNESS_REQUIRE_RESULTS=1 HARNESS_SEQUENCES=MH_01_easy pytest -m sequence
```

A single-sequence run skips the ATE geometric mean with its reason. Running all three
sequences judges it. One fault, for example a 40 ms time offset:

```bash
python -m vioreg.fault_cli time-offset --offset-ms 40 --seq MH_01_easy
RID=MH_01_easy__time-offset_offset_ms40
R=results/faults/$RID
RUNS=2 MODE=serial DATA=data/faulted/$RID bash scripts/run_sequence.sh MH_01_easy $R
python -m vioreg.score --seqs MH_01_easy --out $R --data data/euroc
HARNESS_RESULTS=$R HARNESS_REQUIRE_RESULTS=1 HARNESS_SEQUENCES=MH_01_easy pytest -m sequence -rs
```

<!-- source: README.md, docs/ci.md ("Pieces"), docs/faults.md ("Exact commands") -->

## 9. Future work (not built)

The gate says *that* a sequence got worse. The next stages were meant to say *why*, and to
measure how far an automated explanation can be trusted. None of these stages is built.
Starter modules for windows, the judge and kappa exist in `src/vioreg/` with unit tests, but
they have never been run on real data, no pipeline calls them, and there are no labels and
no results.

- **Failure windows (not built):** cut a failed run into short windows ranked by error, plus
  control windows where nothing went wrong.
- **Labeler (not built):** a tool for a person to tag each window with a cause, such as
  motion blur, low texture or bad lighting.
- **Human labels (not built):** a labelled set from that tool.
- **VLM judge (not built):** a vision-language model shown the same windows, asked for the
  same labels.
- **Cohen's kappa (not built):** agreement between the human and VLM labels, with confidence
  intervals, to decide whether the judge is useful.

Part 2 starts there.
