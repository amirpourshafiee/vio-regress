#!/usr/bin/env bash
# Three-way ATE cross-check for one sequence: vioreg ate() vs evo_ape vs ov_eval.
# See docs/crosscheck.md for what each number means and the tolerance rule.
#   usage: scripts/crosscheck.sh <SEQ> [run_index] [image]
#   needs: data/euroc/<SEQ> (fetch_euroc.sh), results/<SEQ>/run<k>.tum + run<k>.ov.txt,
#          .venv with evo installed, the pinned Docker image.
set -euo pipefail
SEQ=${1:?usage: crosscheck.sh SEQ [run] [image]}; RUN=${2:-1}; IMAGE=${3:-vioreg-openvins:v2.7}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
GT_CSV=data/euroc/$SEQ/mav0/state_groundtruth_estimate0/data.csv
EST_TUM=results/$SEQ/run$RUN.tum; EST_OV=results/$SEQ/run$RUN.ov.txt
SKIP_S=$(python3 -c "import yaml;print(float(yaml.safe_load(open('configs/thresholds.yaml'))['sequences']['$SEQ'].get('skip_s',0)))")
XC=$(mktemp -d); trap 'rm -rf "$XC"' EXIT
cp "$GT_CSV" "$XC/gt.csv"; cp tools/ov_eval_ate_full_precision.cpp "$XC/"
T_START=$(python3 -c "import numpy as n;t=n.loadtxt('$GT_CSV',delimiter=',',skiprows=1,usecols=0,max_rows=1);print(f'{t*1e-9+$SKIP_S:.6f}')")
. .venv/bin/activate

echo "== (1) vioreg ate(), skip_s=$SKIP_S"
python - "$EST_TUM" "$GT_CSV" "$SKIP_S" <<'PY'
import sys
from vioreg.euroc_io import load_tum, load_euroc_gt
from vioreg.metrics import ate
est, gt, skip = load_tum(sys.argv[1]), load_euroc_gt(sys.argv[2]), float(sys.argv[3])
for align, dt in (("posyaw", 0.005), ("se3", 0.005), ("posyaw", 0.02), ("se3", 0.02)):
    r = ate(est, gt, align=align, skip_s=skip, max_dt=dt)
    print(f"vioreg  align={align:6s} max_dt={dt:.3f} rmse={r['rmse']:.9f} n={r['n']}")
PY

echo "== (2) evo_ape, closest option to posyaw is -a (SE3 Umeyama, no scale)"
evo_ape euroc "$GT_CSV" "$EST_TUM" -a -r trans_part --t_max_diff 0.005 --t_start "$T_START" \
  --save_results "$XC/evo.zip" --no_warnings > "$XC/evo.log"
python3 -c "import zipfile,json;s=json.loads(zipfile.ZipFile('$XC/evo.zip').read('stats.json'));print(f\"evo     align=SE3    max_dt=0.005 rmse={s['rmse']:.9f} n={round(s['sse']/s['rmse']**2)}\")"

echo "== (3) ov_eval (pinned image): format_converter, error_singlerun, full-precision driver"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e ROS_HOME=/tmp/.ros \
  -v "$XC:/x" -v "$ROOT/results/$SEQ:/res:ro" "$IMAGE" bash -c "
  set -e
  rosrun ov_eval format_converter /x/gt.csv >/dev/null
  rosrun ov_eval error_singlerun posyaw /x/gt.txt /res/run$RUN.ov.txt 2>&1 | grep -m1 'rmse_ori' | sed 's/^/ov_eval error_singlerun: /'
  g++ -O2 -std=c++14 /x/ov_eval_ate_full_precision.cpp -o /tmp/ate_fp \
    -I/ws/src/open_vins/ov_eval/src -I/ws/src/open_vins/ov_core/src -I/usr/include/eigen3 \
    -I/opt/ros/noetic/include -I/usr/include/python3.8 \
    -L/ws/devel/lib -Wl,-rpath,/ws/devel/lib -lov_eval_lib -lov_core_lib -lboost_filesystem -lboost_system \
    -L/opt/ros/noetic/lib -Wl,-rpath,/opt/ros/noetic/lib -lroscpp -lrosconsole -lrostime -lcpp_common
  for m in posyaw se3; do /tmp/ate_fp \$m /x/gt.txt /res/run$RUN.ov.txt 2>/dev/null | grep '^ov_eval'; done" \
  | sed 's/\x1b\[[0-9;]*m//g'
