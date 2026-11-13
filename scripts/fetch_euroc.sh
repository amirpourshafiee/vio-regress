#!/usr/bin/env bash
# Fetch individual EuRoC sequences (ROS bag + ground-truth CSV) from the ETH Research
# Collection, doi:10.3929/ethz-b-000690084. License: In Copyright - Non-Commercial Use
# Permitted. Uses HTTP Range requests so only the requested sequences are transferred,
# not the 6-13 GB environment archives. Fails closed (non-zero exit, no partial files).
#
#   usage: scripts/fetch_euroc.sh <out_dir> <SEQ> [SEQ...] [--yes] [--max-gb N]
#   e.g.   scripts/fetch_euroc.sh data/euroc MH_01_easy
#
# Plans above 5 GB are refused unless --yes is given.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="${here}/src${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m vioreg.fetch "$@"
