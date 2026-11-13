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
PY=.venv/bin/python; R=results/variance; CFG_C=$R/configs/determinism
MIN_FREE_GB=2

free_gb() { df --output=avail -B1 / | tail -1 | awk '{printf "%.2f", $1/1e9}'; }

if ! $PY -c "import sys; from vioreg.fetch import is_complete; sys.exit(0 if is_complete('data/euroc/$SEQ') else 1)"; then
  # Size check from the zip directory BEFORE fetching: peak disk = inflated ASL temp + raw bag.
  read -r XFER PEAK < <(PYTHONPATH=src $PY - "$SEQ" <<'PY'
import sys, zipfile
from vioreg import fetch
seq = sys.argv[1]; arch = fetch.archive_for(seq)
z = zipfile.ZipFile(fetch.RangeFile(fetch.HttpTransport(), arch.url))
bag = z.getinfo(f"{arch.name}/{seq}/{seq}.bag"); asl = z.getinfo(f"{arch.name}/{seq}/{seq}.zip")
print(bag.compress_size + asl.compress_size, bag.file_size + asl.file_size)
PY
)
  AFTER=$(python3 -c "print(f'{$(free_gb) - $PEAK/1e9:.2f}')")
  echo "fetch $SEQ: transfer $(python3 -c "print(f'{$XFER/1e9:.2f}')") GB, peak disk $(python3 -c "print(f'{$PEAK/1e9:.2f}')") GB, free now $(free_gb) GB -> ~$AFTER GB at peak"
  if python3 -c "import sys; sys.exit(0 if $AFTER < $MIN_FREE_GB else 1)"; then
    echo "STOP: fetching $SEQ would leave under ${MIN_FREE_GB} GB free; ask the owner first." >&2; exit 2
  fi
  bash scripts/fetch_euroc.sh data/euroc "$SEQ" || { echo "fetch failed" >&2; exit 1; }
fi
echo "free after fetch: $(free_gb) GB"

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

rm -f "data/euroc/$SEQ/$SEQ.bag"
echo "deleted raw bag; kept: $(cd data/euroc/$SEQ && find . -type f | tr '\n' ' ')"
echo "free at end: $(free_gb) GB ($(date -u +%H:%M:%S))"
