# Fault injection: does the gate have teeth? (session S6)

I replayed faulted **copies** of MH_01_easy through the pinned image and judged each one with
the real gate and the unchanged margins (RPE ratio <= 1.28 per sequence, ATE geomean <= 1.5
over all three sequences, coverage, pose count). Every result kept the baseline key
(`vio_commit 93adc241`, `image_digest sha256:29b31b7a`, `config_hash 5adca0e7`), so a failure
is a **quality** verdict, never "needs re-baseline". The fault is recorded only in
`labels/fault_manifest.csv`, because result JSONs stay frozen.

**Answer: partly.** The gate caught **2 of the 5 fault types** with the planned parameters:
drop-frames, and time-offset at 40 ms or more. It did **not** catch imu-noise, gamma 2.2 or
blur 9. The target of "at least 3 of 5" was **not met**. Margins were not changed.

**The follow-up on V1_01_easy and MH_03_medium (same 15 settings) gives the same answer, 2 of
5 on every sequence** (see "Other sequences" below). The only difference is where time-offset
starts to be caught: from 40 ms on MH_01_easy, but only from 100 ms on the other two (50 ms is
absorbed there). Outside the planned set, blur 25 is caught on MH_03_medium alone.

## How a fault is applied (inspected in the pinned image)

MH_01_easy.bag is uncompressed (2.5 GB), with these topics:
- `/cam0/image_raw` and `/cam1/image_raw`: `sensor_msgs/Image`, mono8 752×480, 3682 each, 20 Hz
- `/imu0`: `sensor_msgs/Imu`, 36820 messages, 200 Hz
- `/leica/position`

OpenVINS' serial node orders messages and pairs stereo frames by bag **record time**, and uses
`header.stamp` as the measurement time.

`python -m vioreg.fault_cli` mounts the original bag **read-only** into the pinned image and
runs `vioreg.bagfault` there (rosbag, numpy). That rewrites a copy at
`data/faulted/<seq>__<fault>_<params>/<seq>/<seq>.bag` with the same topics, counts (except
drop-frames) and record times:

| fault | what changes | what does not |
|---|---|---|
| imu-noise σ_w, σ_a, seed | `/imu0` gyro + accel values += N(0, σ), seeded | stamps, images |
| gamma γ | pixels of both cameras, `(x/255)^γ` | stamps, IMU |
| blur L | pixels of both cameras, horizontal box blur of L px | stamps, IMU |
| drop-frames k | keeps every k-th stereo pair (cam0 + cam1 with the same stamp) | IMU, kept images |
| time-offset ms | `header.stamp` of both cameras += exactly ms | record times, IMU, pixels |
| identity | nothing (control) | everything |

After each copy, `compare()` checks it against the original and the result is stored in
`bagfault_summary.json`. For example, the 20 ms copy had both camera stamps shifted by
exactly 20 000 000 ns (min = max), IMU stamps and values unchanged (0 of 36820), and record
times equal. The original bag's sha256 was checked before and after every copy and never
changed (`57f440cc…`). Disk: each copy is about 2.67 GB. The peak was the original plus one
copy, with at least 2.0 GB free. Each copy was deleted after its runs.

**Control:** the identity copy reproduced the baseline exactly (ATE 0.0909188275359106 m, RPE
ratio 1.000). The rewrite alone changes nothing, so every effect below comes from the fault.

## Results (MH_01_easy, 2 serial runs each, pinned image)

The **scoped** gate judges MH_01_easy alone, with the per-sequence signals: ok, coverage, pose
count and RPE ratio. The **full** gate judges the faulted MH_01_easy together with clean
pinned-image results for V1_01_easy and MH_03_medium (`results/s5b_pinned`, 2026-11-14). That
is what a three-sequence nightly would see, and the only way the ATE-geomean signal can fire.

| fault | parameter | RPE ratio | ATE ratio (MH_01) | ATE geomean (3 seq) | coverage | n | ok | gate (scoped / full) | caught by |
|---|---|---|---|---|---|---|---|---|---|
| identity (control) | — | 1.000 | 1.000 | 1.000 | 0.9728 | 2746 | yes | pass / pass | — |
| time-offset | 1 ms | 1.070 | 0.816 | 0.935 | 0.9728 | 2746 | yes | pass / pass | **not caught** |
| time-offset | 5 ms | 0.997 | 1.007 | 1.002 | 0.9727 | 2746 | yes | pass / pass | **not caught** |
| time-offset | 20 ms | 0.992 | 0.742 | 0.906 | 0.9727 | 2746 | yes | pass / pass | **not caught** |
| time-offset | 30 ms | 1.187 | 1.247 | 1.076 | 0.9726 | 2746 | yes | pass / pass | **not caught** |
| time-offset | **40 ms** | 11 533 | 101 737 | 46.7 | 0.9727 | 2746 | yes | **QUALITY FAIL / FAIL** | RPE ratio; ATE geomean |
| time-offset | 50 ms | 22 339 | 197 278 | 58.2 | 0.9727 | 2746 | yes | **QUALITY FAIL / FAIL** | RPE ratio; ATE geomean |
| time-offset | 100 ms | 21 845 | 205 454 | 59.0 | 0.9721 | 2744 | yes | **QUALITY FAIL / FAIL** | RPE ratio; ATE geomean |
| time-offset | 200 ms | 21 920 | 194 877 | 58.0 | 0.9714 | 2742 | yes | **QUALITY FAIL / FAIL** | RPE ratio; ATE geomean |
| imu-noise | σ_w 0.01 rad/s, σ_a 0.2 m/s², seed 0 | 1.070 | 1.197 | 1.062 | 0.9728 | 2746 | yes | pass / pass | **not caught** |
| gamma | 2.2 | 0.983 | 0.904 | 0.967 | 0.9728 | 2746 | yes | pass / pass | **not caught** |
| blur | 9 px | 1.145 | 0.901 | 0.966 | 0.9728 | 2746 | yes | pass / pass | **not caught** |
| drop-frames | keep every 2 | 1.139 | 0.995 | 0.998 | 0.9704 | 1371 | yes | **QUALITY FAIL / FAIL** | pose count (1371 < 2746 − 4) |

The gate message names the sequence, metric, value and limit, for example:
`MH_01_easy [QUALITY-FAIL]: RPE ratio 22338.560 > 1.280 (RPE 589455.65 mm vs baseline 26.39 mm)`
and `MH_01_easy [QUALITY-FAIL]: associated pose count 1371 < baseline 2746 - 4`.

**One stronger setting each, chosen once up front (not iterated toward a failure) and reported
apart from the planned parameters:**

| fault | parameter | RPE ratio | ATE ratio (MH_01) | ATE geomean | ok | gate | note |
|---|---|---|---|---|---|---|---|
| blur | 25 px | 1.164 | 1.357 | 1.107 | yes | pass / pass | **still not caught** |
| imu-noise | σ ×5 (0.05, 1.0) | — | — | — | **no** | QUALITY FAIL (ok=false) | **breaks the pipeline**: no poses written, the estimator stopped after 16 s (about 50 s normally). Not counted as a catch |

## Other sequences: V1_01_easy and MH_03_medium (follow-up, same day)

The same 15 settings, fixed in advance to match MH_01_easy and not adjusted afterwards, were
run on each of the other baseline sequences. Same method: a faulted copy, 2 serial runs on
the pinned image, scored against the original ground truth. "Full" means the faulted sequence
judged together with the clean `results/s5b_pinned` results for the other two sequences.
Original bag sha256 values before and after every copy: V1_01_easy `6dc6192f…`, MH_03_medium
`c51b0064…`, unchanged. Both **identity controls reproduced the baseline exactly**: RPE and ATE
ratios 1.000, and poses byte-identical to `baselines/<seq>.tum`.

| fault | parameter | V1_01_easy RPE / ATE ratio | V1 gate (scoped / full) | MH_03_medium RPE / ATE ratio | MH_03 gate (scoped / full) |
|---|---|---|---|---|---|
| identity (control) | — | 1.000 / 1.000 | pass / pass | 1.000 / 1.000 | pass / pass |
| time-offset | 1 ms | 0.997 / 0.940 | pass / pass | 1.021 / 1.138 | pass / pass |
| time-offset | 5 ms | 1.002 / 0.909 | pass / pass | 1.052 / 1.404 | pass / pass |
| time-offset | 20 ms | 1.000 / 0.918 | pass / pass | 1.029 / 1.354 | pass / pass |
| time-offset | 30 ms | 1.002 / 0.847 | pass / pass | 1.073 / 1.390 | pass / pass |
| time-offset | 40 ms | 1.008 / 0.987 | pass / pass | 1.011 / 1.575 | pass / pass |
| time-offset | 50 ms | 1.010 / 0.931 | pass / pass | 0.985 / 1.352 | pass / pass |
| time-offset | **100 ms** | 10 979 / 216 848 | **QUALITY FAIL / FAIL** (RPE; geomean 60.1) | 10 068 / 118 118 | **QUALITY FAIL / FAIL** (RPE; geomean 49.1) |
| time-offset | 200 ms | 11 364 / 235 032 | **QUALITY FAIL / FAIL** (RPE; geomean 61.7) | 11 657 / 161 524 | **QUALITY FAIL / FAIL** (RPE; geomean 54.5) |
| imu-noise | σ_w 0.01, σ_a 0.2, seed 0 | 1.003 / 1.122 | pass / pass | 1.176 / 1.480 | pass / pass |
| gamma | 2.2 | 0.991 / 0.939 | pass / pass | 1.048 / 1.178 | pass / pass |
| blur | 9 px | 1.010 / 1.239 | pass / pass | 1.091 / 1.288 | pass / pass |
| drop-frames | keep every 2 | 1.002 / 1.020 | **QUALITY FAIL / FAIL** (pose count 1377 < 2757 − 4) | 0.998 / 1.076 | **QUALITY FAIL / FAIL** (pose count 1133 < 2275 − 4) |
| *stronger:* blur | 25 px | 1.036 / 1.043 | pass / pass | **1.410** / 1.803 | **QUALITY FAIL / FAIL** (RPE 1.410 > 1.280; geomean 1.217) |
| *stronger:* imu-noise | σ ×5 (0.05, 1.0) | no poses, ok=false | FAIL (ok=false) | no poses, ok=false | FAIL (ok=false) |

Coverage stayed at baseline (V1 0.968, MH_03 0.979) in every run that produced poses, and the
pose count stayed within 4 of baseline except for drop-frames.

What this adds to the MH_01_easy picture:
- **Planned set: 2 of 5 on all three sequences**, the same two types (drop-frames by pose
  count, time-offset by divergence). The answer does not depend on which sequence is faulted.
- **The time-offset divergence point depends on the sequence.** MH_01_easy diverges between 30
  and 40 ms; V1_01_easy and MH_03_medium absorb 50 ms and diverge between 50 and 100 ms.
  Below that point the online time-offset calibration absorbs the offset (RPE ratio at most
  1.073). Above it the estimator diverges to hundreds of km, never degrading gradually. So the
  smallest offset the gate catches on *every* sequence is 100 ms.
- **imu-noise ×5 breaks the pipeline on all three sequences** in the same way (no poses, the
  estimator stops after about 15 s), so it is not specific to MH_01_easy. Why it stops is still
  unverified: logs are at WARNING and show no error.
- **Blur 25 is caught only on MH_03_medium** (RPE 1.410, faster motion). It is still not
  caught on MH_01_easy (1.164) or V1_01_easy (1.036). This is the stronger setting, not part
  of the planned set, so it does not change the 2-of-5 count.
- **MH_03_medium's ATE moves more than its RPE** under mild faults: ATE ratio 1.14–1.58 at
  1–50 ms offsets with RPE ≤ 1.073, and 1.48 under the planned imu-noise. Even the largest
  single-sequence ATE ratio here (1.803, blur 25) is diluted to a geomean of 1.217 < 1.5.
  This matches the 3.4× dilution noted below.

## What the gate does not catch, and why

- **Time offsets up to 30 ms.** The euroc config estimates the camera-IMU time offset online
  (`calib_cam_timeoffset: true`), so small offsets are absorbed. ATE even improved at 1 and
  20 ms. Between 30 and 40 ms the estimator does not degrade gradually: it **diverges** to
  kilometres of error. **The smallest offset caught is 40 ms; nothing at or below 30 ms is
  caught** (30 ms: RPE 1.187 < 1.28). At 20 Hz a frame is 50 ms. On V1_01_easy and
  MH_03_medium the divergence point is higher (50 ms absorbed, 100 ms caught); see
  "Other sequences".
- **Gamma 2.2: structurally invisible.** The front end applies global histogram equalization
  (`histogram_method: HISTOGRAM`), which undoes any monotonic brightness change except for
  quantization. The tracker sees almost the same images. That is pipeline robustness, not a gate gap.
- **Blur 9 and even 25.** The KLT tracker on the slow MH_01 motion tolerates it. The RPE
  ratio (1.145, 1.164) stays below 1.28. At 25 px the single-sequence ATE ratio is 1.357, but
  ATE is gated only as the geometric mean over three sequences, so one faulted sequence is
  diluted to the cube root (1.357 → 1.107 < 1.5). This is by design (docs/gate.md: no
  single-sequence ATE gate), but it means **a fault confined to one sequence must move RPE by
  28% or ATE by about 3.4× to fail**.
- **IMU noise at about 4×/7× the sensor spec.** The config's discrete-time noise at 200 Hz is
  about 0.0024 rad/s and 0.028 m/s², so σ_w 0.01 and σ_a 0.2 are roughly 4× and 7× that. ATE
  rose 20% and RPE 7%, both under the margins. At 20×/35× the pipeline produced no estimate at all.
- **`ok` stays true even for a diverged run** (time-offset 40–200 ms on MH_01_easy, 100–200 ms
  on V1_01_easy and MH_03_medium; ATE in km). The hard
  checks only see "a trajectory exists and covers the sequence". Divergence is caught only by
  the ratio signals.

drop-frames is caught by the **pose-count** check, not by accuracy: half the frames means half
the output poses. Its accuracy barely moved (RPE 1.139, ATE 0.995).

Parameter source: the build guide's table is not in this repository. The planned values are the
defaults in `tests/test_faults_windows.py` (σ_w 0.01, σ_a 0.2, gamma 2.2, blur 9,
keep_every 2), which match the session brief.

## Exact commands

```bash
bash scripts/fetch_euroc.sh data/euroc MH_01_easy --yes
sha256sum data/euroc/MH_01_easy/MH_01_easy.bag            # record; compare after each copy

# one faulted copy (pick one), then run, score, gate, delete
python -m vioreg.fault_cli identity --seq MH_01_easy                        # control
python -m vioreg.fault_cli time-offset --offset-ms 40 --seq MH_01_easy
python -m vioreg.fault_cli imu-noise --sigma-w 0.01 --sigma-a 0.2 --seed 0 --seq MH_01_easy
python -m vioreg.fault_cli gamma --gamma 2.2 --seq MH_01_easy
python -m vioreg.fault_cli blur --length 9 --seq MH_01_easy
python -m vioreg.fault_cli drop-frames --keep-every 2 --seq MH_01_easy

RID=MH_01_easy__time-offset_offset_ms40          # printed by fault_cli
R=results/faults/$RID
RUNS=2 MODE=serial DATA=data/faulted/$RID bash scripts/run_sequence.sh MH_01_easy $R
python -m vioreg.score --seqs MH_01_easy --out $R --data data/euroc          # GT from the original
HARNESS_RESULTS=$R HARNESS_REQUIRE_RESULTS=1 HARNESS_SEQUENCES=MH_01_easy pytest -m sequence -rs
# full view: copy the clean V1_01_easy / MH_03_medium results next to it and drop HARNESS_SEQUENCES
rm data/faulted/$RID/MH_01_easy/MH_01_easy.bag   # keep fault.json + bagfault_summary.json
```

Red-build demo in CI: the nightly `fault` input (docs/ci.md).
