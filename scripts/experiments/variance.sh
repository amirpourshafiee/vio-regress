#!/usr/bin/env bash
# S3 run-to-run variance experiment for ONE sequence (disk allows one bag at a time).
#   usage: scripts/experiments/variance.sh <SEQ> [RUNS=10]
# Variants (identical data, image, bag_start; RUNS runs each), under results/variance/<V>/:
#   A_serial            serial replay, image config (the baseline)
#   B_serial_cpus2      A + docker --cpus=2
#   C_serial_det        A + CONFIG_DIR with num_opencv_threads=1, multi_threading_pubs=false
#   D_realtime          rosbag play -r 1 + subscribe node (only for MH_01_easy: 1x speed)
# Then scores every variant and deletes the raw bag (ground truth + manifest kept).
set -uo pipefail
SEQ=${1:?usage: variance.sh SEQ [RUNS]}; RUNS=${2:-10}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
source scripts/experiments/lib.sh
R=results/variance; CFG_C=$R/configs/determinism
fetch_checked "$SEQ"; rc=$?; [ $rc -eq 0 ] || exit $rc

[ -d "$CFG_C" ] || bash scripts/experiments/make_determinism_config.sh "$CFG_C"

run_variant() {  # name, extra env...
  local V=$1; shift
  echo "== $SEQ $V ($(date -u +%H:%M:%S))"
  env RUNS="$RUNS" "$@" bash scripts/run_sequence.sh "$SEQ" "$R/$V"
}
run_variant A_serial
run_variant B_serial_cpus2 CPUS=2
run_variant C_serial_det CONFIG_DIR="$CFG_C"
[ "$SEQ" = MH_01_easy ] && run_variant D_realtime MODE=realtime

for V in A_serial B_serial_cpus2 C_serial_det D_realtime; do
  [ -d "$R/$V/$SEQ" ] || continue
  extra=(); [ "$V" = C_serial_det ] && extra=(--config-dir "$CFG_C")
  $PY -m vioreg.score --seqs "$SEQ" --out "$R/$V" "${extra[@]}"
done

delete_bag "$SEQ"
echo "free at end: $(free_gb) GB ($(date -u +%H:%M:%S))"
