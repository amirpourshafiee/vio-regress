# Shared helpers for scripts/experiments/*.sh (sourced, not executed).
PY=${PY:-.venv/bin/python}
# Use THIS checkout's src/ (not whatever the venv's editable install points at), so
# harness_commit in every score JSON names the code that actually ran.
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:${PYTHONPATH}}"
MIN_FREE_GB=${MIN_FREE_GB:-2}

free_gb() { df --output=avail -B1 / | tail -1 | awk '{printf "%.2f", $1/1e9}'; }

# fetch_checked SEQ: fetch one sequence unless complete, refusing (exit 2) if the fetch's
# peak disk use (inflated ASL temp + raw bag, read from the zip directory) would leave
# under MIN_FREE_GB free.
fetch_checked() {
  local SEQ=$1
  echo "df before $SEQ: $(df -h / | tail -1)"
  if $PY -c "import sys; from vioreg.fetch import is_complete; sys.exit(0 if is_complete('data/euroc/$SEQ') else 1)"; then
    echo "$SEQ already on disk"; return 0
  fi
  local XFER PEAK AFTER
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
    echo "STOP: fetching $SEQ would leave under ${MIN_FREE_GB} GB free; ask the owner first." >&2; return 2
  fi
  bash scripts/fetch_euroc.sh data/euroc "$SEQ" || { echo "fetch failed" >&2; return 1; }
  echo "df after fetch: $(df -h / | tail -1)"
}

delete_bag() {
  rm -f "data/euroc/$1/$1.bag"
  echo "deleted raw bag; kept: $(cd data/euroc/$1 && find . -type f | tr '\n' ' ')"
  echo "df after delete: $(df -h / | tail -1)"
}
