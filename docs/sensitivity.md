# How far does ATE move under legitimate changes? (session S3b)

**Result: much further than run-to-run noise.** S3 showed serial replay is bit-identical run
to run (zero spread). But a single, innocent one-at-a-time change moves ATE RMSE by
**−21.6% to +73.4%** on MH_01_easy and **−13.5% to +1.0%** on V1_01_easy. Every variant
was itself perfectly repeatable, and **0 of 77 runs failed**, with coverage unchanged in
every variant. A percentage-of-baseline ATE tolerance cannot tell such changes apart from a
regression on MH_01 below about +75%. The gate needs a different structure, not just a
wider number (see Conclusion). No thresholds are chosen here.

- Image `vioreg-openvins:v2.7` (`sha256:f08eacb1…`), OpenVINS `93adc241` (v2.7);
  harness `de09c8d` (clean)
- S3 serial baseline (median per-run ATE, `docs/variance/baseline.json`):
  MH_01_easy 90.919 mm, V1_01_easy 63.408 mm. The S3b `base` runs reproduce both
  byte-for-byte (same sha1).
- ATE: posyaw, `skip_s` from `configs/thresholds.yaml` (MH_01 40 s, V1_01 0 s). 3 runs per value.
- Host: AMD EPYC 7763, 4 logical CPUs, avx/avx2/fma/sse4.x, no AVX-512
  (`docs/sensitivity/host_codespace.json`)
- Machine-readable: `docs/sensitivity/sensitivity.json` (every run, every variant)

## Part 1a: were the S3 runs independent? Yes

In each of S3's 10 cells (variant × sequence), all 10 runs have a distinct roslaunch
`run_id` UUID (a fresh ROS master per run) and a distinct `wall_s`. Their output files
have distinct modification times, spaced by the run duration (about 45 s serial, 150 s
realtime). ov_eval's `Recorder` deletes any existing output file when it opens, so a
running recorder cannot append to an old file.

**One gap was found and closed.** The runner numbered runs from `run<k>.tum` alone. If a
`run<k>.tum` had been deleted while its `run<k>.ov.txt` remained, and the next container
exited 0 without the recorder starting, that stale file would have been scored as a new
run. A new test reproduced this before the fix. S3 was not affected, because every
results folder started empty. Fix (commit `c6daa3f`), defence in depth:
- run numbers skip past any `run<k>.*` file
- a run refuses to start if its `run<k>.ov.txt` exists
- the trajectory must be modified after the run started
- every run records its exact docker command in `run<k>.cmd`

## Part 1b: why did 10 realtime runs give 1 file?

**From the code** (the image's source at `93adc241`):
- After initialization, image updates are data-driven. Images sit in a timestamp-sorted
  `camera_queue` and are processed in time order, once an IMU sample newer than the image
  has arrived. The frame-drop rule (`track_frequency`) compares message timestamps, not
  wall-clock time. The IMU subscriber queue holds 1000 messages.
- **Initialization is not data-driven in realtime mode.** `VioManager::try_to_initialize`
  (`VioManagerHelper.cpp`) runs the initializer in a `std::thread`. The serial node forces
  `use_multi_threading_subs=false`, so that thread is joined. The subscribe node sets it
  true, so the thread is detached. Frames arriving while it runs go to
  `camera_queue_init`, and on success the state is propagated over them with a
  `clone_rate` that depends on how many arrived. The result therefore depends on
  wall-clock timing.
- This explains why realtime differs from serial from the very first pose: 9 cm apart, with
  the first timestamps 0.9 ms apart.

**From the data:** the code does not guarantee repeatability; timing headroom does.

| Realtime condition (MH_01_easy) | Runs | Distinct files | ATE RMSE per run (mm) | Meaning |
|---|---|---|---|---|
| S3, rate 1.0, idle | 10 | 1 | 60.086 × 10 | repeatable |
| S3b, rate 1.0, idle | 5 | 2 | 60.086 × 5 | estimator identical; files differ only in the last pose (see below) |
| rate 1.5, idle | 3 | 1 | 60.086 × 3 | repeatable |
| **rate 2.0, idle** | 3 | 3 | **58.595, 69.900, 60.149** | estimator diverges |
| **rate 1.0, all 4 CPUs busy** | 3 | 3 | **60.086, 60.086, 75.935** | 1 of 3 diverges |

V1_01_easy behaves the same way: rate 1.0 and 1.5 give 57.031 mm × 6, while rate 2.0 gives
68.303, 57.031 and 59.185 mm.

The 2 files at idle rate 1.0 differ in exactly one line: the final pose at t = …763.8135 s,
70 ms after ground truth ends. When `rosbag play` (a required node) finishes, roslaunch
kills every node, and whether the recorder has written that last pose is a race. It does
not affect ATE. **Consequence: a file checksum is a fragile determinism check.** Compare
poses over the common time span instead. Every serial run so far had an identical length,
but the same race exists in principle.

**Answer:** 10 realtime runs gave one file because, on an idle 4-core machine at 1×, the
detached initializer and update threads always finished within the same data intervals.
Squeeze the timing (2× rate, or CPU contention) and the result scatters by up to 16 mm.
Serial replay has no such dependency. This is another reason to pin `MODE=serial` in CI.

## Part 2: sensitivity sweep

Every name below was read from the image's `serial.launch`, `subscribe.launch` and
`config/euroc_mav/estimator_config.yaml` (lines 81, 82 and 22). Each variant changes one
thing from the S3 baseline. Config variants come from `make_config_variant.sh`, which
copies the image config and changes exactly one line. Each run's config hash is recorded
and checked by the scorer.

| Knob | Exists as | Values tried (S3 value first) | Why it is "legitimate" |
|---|---|---|---|
| Replay start offset | `serial.launch` arg `bag_start` | MH_01: 40, 38, 42 · V1_01: 0, 1, 2 | a harness or launch tweak |
| Replay rate | `subscribe.launch` arg `bag_rate` (**realtime only**; serial reads the bag directly and has no rate) | 1.0, 1.5, 2.0 | a faster CI replay |
| Feature count | `num_pts: 200` | 180, 220 (±10%) | typical tuning |
| FAST threshold | `fast_threshold: 20` | 18, 22 (±10%) | typical tuning |
| Gravity constant | `gravity_mag: 9.81` | 9.80665 (standard g), 9.80 | an "innocent" cleanup edit |

### MH_01_easy (S3 baseline 90.919 mm)

| Variant | config_hash | Runs OK | Distinct | ATE RMSE (mm) | vs baseline |
|---|---|---|---|---|---|
| base | `5adca0e7` | 3/3 | 1 | 90.919 | 0.0% |
| bag_start 38 | `5adca0e7` | 3/3 | 1 | 108.216 | +19.0% |
| bag_start 42 | `5adca0e7` | 3/3 | 1 | 77.113 | −15.2% |
| num_pts 180 | `fb341684` | 3/3 | 1 | 71.262 | −21.6% |
| num_pts 220 | `8a143d98` | 3/3 | 1 | 74.898 | −17.6% |
| fast_threshold 18 | `56bb52f2` | 3/3 | 1 | **157.623** | **+73.4%** |
| fast_threshold 22 | `4e4bea14` | 3/3 | 1 | 71.989 | −20.8% |
| gravity_mag 9.80665 | `830b68f2` | 3/3 | 1 | 104.512 | +15.0% |
| gravity_mag 9.80 | `e7d725ac` | 3/3 | 1 | 74.054 | −18.5% |
| *realtime, rate 1.0 / 1.5* | `5adca0e7` | 8/8 | see 1b | 60.086 | −33.9% |
| *realtime, rate 2.0* | `5adca0e7` | 3/3 | 3 | 58.6 to 69.9 | −35.6% to −23.1% |

### V1_01_easy (S3 baseline 63.408 mm)

| Variant | config_hash | Runs OK | Distinct | ATE RMSE (mm) | vs baseline |
|---|---|---|---|---|---|
| base | `5adca0e7` | 3/3 | 1 | 63.408 | 0.0% |
| bag_start 1 | `5adca0e7` | 3/3 | 1 (same file as base) | 63.408 | 0.0% |
| bag_start 2 | `5adca0e7` | 3/3 | 1 | 54.870 | −13.5% |
| num_pts 180 | `fb341684` | 3/3 | 1 | 60.095 | −5.2% |
| num_pts 220 | `8a143d98` | 3/3 | 1 | 57.265 | −9.7% |
| fast_threshold 18 | `56bb52f2` | 3/3 | 1 | 57.176 | −9.8% |
| fast_threshold 22 | `4e4bea14` | 3/3 | 1 | 64.057 | +1.0% |
| gravity_mag 9.80665 | `830b68f2` | 3/3 | 1 | 56.880 | −10.3% |
| gravity_mag 9.80 | `e7d725ac` | 3/3 | 1 | 62.528 | −1.4% |
| *realtime, rate 1.0 / 1.5* | `5adca0e7` | 6/6 | 1 | 57.031 | −10.1% |
| *realtime, rate 2.0* | `5adca0e7` | 3/3 | 3 | 57.0 to 68.3 | −10.1% to +7.7% |

### Across all legitimate (serial) variants

| Sequence | Min ATE | Max ATE | Range | As % of S3 baseline | Failed runs | Coverage |
|---|---|---|---|---|---|---|
| MH_01_easy | 71.262 mm | 157.623 mm | 86.361 mm | **−21.6% … +73.4%** (range 95.0%) | 0 / 27 | 0.973 in every variant |
| V1_01_easy | 54.870 mm | 64.057 mm | 9.187 mm | **−13.5% … +1.0%** (range 14.5%) | 0 / 27 | 0.968 in every variant |

Realtime is excluded above because the harness pins serial. Including it would widen
MH_01 down to −35.6% and V1_01 up to +7.7%.

**Why so large, when runs are bit-identical?** On both sequences every variant
initializes on the same camera frame (MH_01: first pose at …624.662 s ± 1.3 ms, 2767 poses
each): the initializer waits for the pick-up motion whatever the start offset. What
changes is the content of the stationary IMU window before that motion (start offset), or
which features are extracted (`num_pts`, `fast_threshold`). The initial bias and gravity
estimate, or the feature set, then differs slightly, and the filter follows a different,
equally valid trajectory. The estimator is deterministic but sensitive. MH_01, with its
hand-held pick-up phase, is far more sensitive than V1_01.

## Part 3: host fingerprint

`docs/sensitivity/host_codespace.json` (written by `python -m vioreg.hostinfo`; a sidecar,
not part of the result JSON):

| Field | This Codespace |
|---|---|
| CPU | AMD EPYC 7763 64-Core Processor (Zen 3), Microsoft hypervisor |
| Logical CPUs | 4 (2 cores × 2 threads); the container sees the same 4 |
| SIMD flags | avx, avx2, fma, sse4_1, sse4_2, sse4a; **no AVX-512** |
| Kernel | 6.8.0-1070-azure, x86_64 |

OpenVINS is compiled with `-march=x86-64` (seen in the S1 compiler command line). So a
cross-host difference is more likely to come from OpenCV's runtime CPU dispatch than from
Eigen code compiled into OpenVINS.

## Conclusion: legitimate noise vs regression

**ATE.** On one sequence, a legitimate change can move ATE by −22% to +73% (MH_01) or
−14% to +1% (V1_01). The upward side is what a gate cares about. An ATE-only tolerance tight
enough to catch a +20% regression on MH_01 would also fail an innocent `fast_threshold`
tweak. A tolerance loose enough to pass every innocent change (above +73%, about 158 mm)
only catches gross breakage. Treat as "legitimate noise", per sequence:
- MH_01_easy: anything inside about 71–158 mm (up to +75%)
- V1_01_easy: anything inside about 55–64 mm (up to about +2% above baseline;
  the measured envelope is mostly below baseline)

Treat as "regression": ATE above the legitimate envelope's maximum, plus any of the
signals below.

**Sharp signals that never moved under any legitimate change (77 runs):**
- `ok=false`: 0 occurrences
- coverage: identical per sequence across every variant (0.973 / 0.968)
- pose count: 2767 on MH_01 and 2776 on V1_01, in all 27 serial runs of each
- serial determinism: every variant was bit-identical across its runs

Any change in these is not "noise" and should fail.

**Implications for S4 (not decided here):** a credible gate is probably two-tier.
1. Hard fail: `ok=false`, a coverage drop, lost determinism (two runs differ), or ATE
   above the legitimate envelope's maximum (or an absolute per-sequence ceiling).
2. A "changed" flag for review: because serial replay is deterministic, any change to the
   trajectory, compared pose by pose, is a real behaviour change to review, even inside
   the envelope.

Sizing the envelope with more sequences and more perturbations would make the ceiling less
arbitrary. Note that the existing placeholder `max_ate_m: 0.15` for MH_01 would have
**failed** the innocent `fast_threshold: 18` variant (157.6 mm).

## What is not known yet

- **Cross-machine behaviour on the CI runner.** Everything here ran on one AMD EPYC 7763
  Codespace. A GitHub-hosted runner may differ in CPU (Intel vs AMD, AVX-512), core count
  and load. It may not reproduce the bit-identical trajectories, and how far ATE moves
  there is unmeasured. Given the sensitivity above, even a tiny floating-point difference
  could shift MH_01's ATE by tens of percent. S5 should re-run the baseline there, record
  `python -m vioreg.hostinfo`, and compare against this file.
- Only 2 sequences, 5 knobs and 2 values each. The envelope is a lower bound on legitimate
  movement, not its full extent.
- MH_03_medium and the difficult sequences were not swept.

## Exact commands

```bash
# Part 1a (evidence for S3 independence): run_id / wall_s / mtime per run
for d in results/variance/*/*/; do
  grep -ho "setting /run_id to [0-9a-f-]*" $d/run*.log | sort -u | wc -l   # = 10 per cell
  cat $d/run*.wall_s | sort -u | wc -l                                      # = 10 per cell
done

# Part 1b + 2 (one sequence at a time; checks disk, fetches, runs, scores, deletes the bag)
bash scripts/experiments/sensitivity.sh MH_01_easy 3   # includes 5 realtime + 3 under CPU load
bash scripts/experiments/sensitivity.sh V1_01_easy 3

# a single variant by hand, e.g. fast_threshold 18:
bash scripts/experiments/make_config_variant.sh results/sensitivity/configs/fast_threshold_18 fast_threshold 18
CONFIG_DIR=results/sensitivity/configs/fast_threshold_18 RUNS=3 \
  bash scripts/run_sequence.sh MH_01_easy results/sensitivity/fast_threshold_18
python -m vioreg.score --seqs MH_01_easy --out results/sensitivity/fast_threshold_18 \
  --config-dir results/sensitivity/configs/fast_threshold_18
# start offset / realtime rate:
BAG_START=38 RUNS=3 bash scripts/run_sequence.sh MH_01_easy results/sensitivity/bag_start_38
MODE=realtime BAG_RATE=2.0 RUNS=3 bash scripts/run_sequence.sh MH_01_easy results/sensitivity/rt_rate_2.0

# analysis (tables above)
python -m vioreg.sensitivity --seqs MH_01_easy V1_01_easy --out docs/sensitivity/sensitivity.json

# Part 3
python -m vioreg.hostinfo --out docs/sensitivity/host_codespace.json
```
