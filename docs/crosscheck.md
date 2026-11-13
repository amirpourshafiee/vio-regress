# ATE cross-check: vioreg vs evo vs ov_eval

**Result: the three implementations agree.** On MH_01_easy run 1, once each tool is given
the same pose pairs and the same alignment, all three ATE RMSE values match to 9 decimals.
The only differences under each tool's default settings come from known policy choices
(association window, alignment DoF), each explained and measured below.

- Data: EuRoC MH_01_easy, OpenVINS v2.7 (`93adc241`), image `sha256:f08eacb146ff…`,
  `results/MH_01_easy/run1.{tum,ov.txt}` from session S1
- Tools: evo 1.38.1 (pip, in `.venv`); ov_eval from the same pinned image
- Date: 2026-11-13

## The three numbers

| # | Tool | Alignment | Assoc. window | ATE RMSE [m] | Pairs |
|---|------|-----------|---------------|--------------|-------|
| 1 | vioreg `ate()` | posyaw (4-DoF) | 5 ms | **0.090918828** | 2746 |
| 2 | `evo_ape -a` | SE3 (6-DoF), closest available | 5 ms | **0.090792405** | 2746 |
| 3 | ov_eval `error_singlerun posyaw` | posyaw (4-DoF) | 20 ms (hard-coded) | **0.090936498** (prints `0.091`) | 2747 |

The same tools at matched settings:

| Comparison | Settings | A | B | \|A − B\| |
|---|---|---|---|---|
| vioreg vs evo | SE3, 5 ms | 0.090792405 | 0.090792405 | < 1e-9 m |
| vioreg vs ov_eval | posyaw, 20 ms | 0.090936498 | 0.090936498 | < 1e-9 m |
| vioreg vs ov_eval | SE3, 20 ms | 0.090810299 | 0.090810299 | < 1e-9 m |

## Tolerance, and why

The tolerance has two tiers. Neither is a single loose band.

1. **Matched settings (same pose pairs, same alignment DoF): |Δ| ≤ 1 µm (1e-6 m).**
   These are the same closed-form least-squares problem on the same inputs. All three
   tools read the same 6-decimal text files: the ground-truth CSV, and the OpenVINS output
   (raw for ov_eval, 8-column TUM for vioreg and evo). The only legitimate source of
   difference is floating-point summation order, which is orders of magnitude below 1 µm.
   Observed: < 1e-9 m. A real implementation bug (wrong pairing, wrong yaw solver sign,
   wrong centring) moves the result by far more than 1 µm.

2. **Default settings: no numeric tolerance.** Each difference must be explained by a named
   cause and reproduced by switching that one setting.
   - vioreg posyaw vs ov_eval posyaw: Δ = 0.0177 mm, fully explained by one extra pair
     (see below).
   - vioreg posyaw vs evo SE3: Δ = 0.126 mm, explained by alignment DoF (see below). This
     comparison also has a hard invariant: SE3 RMSE ≤ posyaw RMSE on the same pairs.
     Observed: 0.090792 ≤ 0.090919, so it holds.

**Why not a looser, mm-level tolerance?** On this sequence the effects such a tolerance is
meant to catch are tiny. Confusing posyaw with SE3 changes ATE by 0.13 mm. A −20 ms
timestamp shift changes it by 1.5 mm (S1 shift probe). A 1 mm or 0.5% band would silently
pass an alignment-mode bug, while matched settings let us demand essentially exact
agreement for free.

**What this cross-check does not cover:** timestamp correctness. All three tools read the
same timestamps, so a shared offset would pass. That is covered separately by the S1
time-shift probe: ATE is minimal at 0 shift and rises on both sides.

## Differences, investigated

### ov_eval: one extra pose pair (association window 20 ms vs 5 ms)
`ResultTrajectory::ResultTrajectory` calls
`AlignUtils::perform_association(0, 0.02, ...)`, i.e. a fixed 20 ms window
(`ov_eval/src/calc/ResultTrajectory.cpp` at `93adc241`). vioreg's `associate()` uses 5 ms.
Against 200 Hz ground truth, every interior pose has a match within 2.5 ms either way. The
one difference is the estimate pose at t = 1403636762.76353, which is **19.97 ms after the
last ground-truth sample** (1403636762.74356). ov_eval pairs it; vioreg does not.

Proof: rerunning vioreg with `max_dt=0.02` gives n = 2747 and RMSE 0.090936498, identical
to ov_eval to 9 decimals, for both posyaw and SE3.

We keep 5 ms. Pairing a pose with ground truth 20 ms away (MH_01 speed after
initialisation: median 0.50 m/s, p95 0.91 m/s) injects 1-2 cm of pure timing error into the metric, and a stricter window costs at most a pose
or two at the ends.

### evo: no 4-DoF (yaw + translation) alignment exists
evo 1.38.1 offers `-a/--align` (SE3 Umeyama), `-s/--correct_scale` (with `-a`: Sim3),
`--align_origin`, and `--n_to_align`. None constrains the rotation to yaw. A search of
`evo/core/trajectory.py`, `evo/core/geometry.py` and `evo/main_ape.py` finds no yaw-only
solver. **There is no flag that maps to posyaw.** The closest option is `-a` (6-DoF SE3).

Expected difference: SE3 minimises the same squared position error over a superset of
posyaw's transforms (it also frees roll and pitch), so its RMSE can only be **lower or
equal**. The gap is small for VIO because gravity makes roll and pitch observable, so the
optimal SE3 rotation is nearly yaw-only. Observed: evo SE3 = 0.090792 < vioreg posyaw =
0.090919 (−0.126 mm), and evo SE3 equals vioreg SE3 exactly.

Other evo flag mappings used:
- `--t_max_diff 0.005`: same 5 ms window as vioreg (evo's default is 10 ms)
- `--t_start <gt_t0 + skip_s>`: our `skip_s=40` (drops pairs whose time is before
  ground-truth start + 40 s)
- `-r trans_part`: position error, as in vioreg
- Quaternions: evo stores wxyz internally. Its TUM reader rolls xyzw to wxyz
  (`file_interface.py:141`) and its EuRoC reader reads the CSV's wxyz natively, so both
  inputs are converted correctly. Position-only ATE does not depend on quaternions anyway.

### ov_eval notes
- **Argument order** (checked in the image, `ov_eval/src/error_singlerun.cpp:91-109`):
  `error_singlerun <align_mode> <file_gt.txt> <file_est.txt>`, ground truth **before** the
  estimate. Internally it calls `ResultTrajectory(est, gt, mode)`.
- **It cannot read EuRoC CSV directly.** `Loader::load_data` splits on spaces only and never
  dispatches to `load_data_csv`, so we first run ov_eval's own `format_converter`. That
  keeps ov_eval's ground truth independent of vioreg's `load_euroc_gt` (the wxyz→xyzw
  reorder happens in ov_eval's code, `Loader.cpp:148-156`).
- **It prints `%.3f`** (1 mm resolution), too coarse to test a 1 µm tolerance. So
  `tools/ov_eval_ate_full_precision.cpp`, a 20-line driver, links the **unmodified**
  `libov_eval_lib` from the image and calls the same `ResultTrajectory` + `calculate_ate`,
  printing full precision and the pair count. Its rounded output matches
  `error_singlerun`'s printout (0.091 / 0.081 / 0.209).
- ov_eval's estimate input is the **raw** 20-column OpenVINS output (`run1.ov.txt`), not
  our TUM conversion, so our conversion is also cross-checked.
- **No skip parameter**, but none is needed here. The estimate starts at ground-truth start
  + 43.8 s (`bag_start:=40` plus initialisation), so `skip_s=40` removes no pairs. On a
  sequence where it would, compare with `skip_s` applied on both sides.
- Also reported by ov_eval, but not a vioreg metric: orientation ATE RMSE 1.470 deg.

## Exact commands

All of the above is reproduced by one script (about 9 s; it compiles the driver inside the
image):

```bash
bash scripts/crosscheck.sh MH_01_easy        # [run_index=1] [image=vioreg-openvins:v2.7]
```

It runs exactly these commands (paths for MH_01_easy, run 1, `$XC` a temp dir):

```bash
GT=data/euroc/MH_01_easy/mav0/state_groundtruth_estimate0/data.csv
T_START=1403636620.838556          # gt_t0 (1403636580.838556) + skip_s (40)

# (1) vioreg
python -c 'from vioreg.euroc_io import *; from vioreg.metrics import ate
print(ate(load_tum("results/MH_01_easy/run1.tum"), load_euroc_gt("'$GT'"), align="posyaw", skip_s=40.0)["rmse"])'

# (2) evo
evo_ape euroc $GT results/MH_01_easy/run1.tum -a -r trans_part \
        --t_max_diff 0.005 --t_start $T_START --save_results $XC/evo.zip

# (3) ov_eval, inside the pinned image ($XC holds a writable copy of the CSV as gt.csv)
docker run --rm -v $XC:/x -v $PWD/results/MH_01_easy:/res:ro vioreg-openvins:v2.7 bash -c '
  rosrun ov_eval format_converter /x/gt.csv                               # -> /x/gt.txt
  rosrun ov_eval error_singlerun posyaw /x/gt.txt /res/run1.ov.txt        # mode, GT, EST
  g++ -O2 -std=c++14 /x/ov_eval_ate_full_precision.cpp -o /tmp/ate_fp ... # see script
  /tmp/ate_fp posyaw /x/gt.txt /res/run1.ov.txt'
```

Verified output (2026-11-13):

```
vioreg  align=posyaw max_dt=0.005 rmse=0.090918828 n=2746
vioreg  align=se3    max_dt=0.005 rmse=0.090792405 n=2746
vioreg  align=posyaw max_dt=0.020 rmse=0.090936498 n=2747
vioreg  align=se3    max_dt=0.020 rmse=0.090810299 n=2747
evo     align=SE3    max_dt=0.005 rmse=0.090792405 n=2746
ov_eval error_singlerun: rmse_ori = 1.470 | rmse_pos = 0.091
ov_eval posyaw rmse_pos=0.090936498 mean_pos=0.080859844 max_pos=0.208666893 min_pos=0.002076071 n=2747
ov_eval se3 rmse_pos=0.090810299 mean_pos=0.081053787 max_pos=0.207588508 min_pos=0.003759051 n=2747
```

(evo's pair count is derived as round(sse / rmse²) from its saved `stats.json`, since evo
does not print it.)
