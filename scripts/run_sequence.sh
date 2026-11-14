#!/usr/bin/env bash
# Run OpenVINS on one EuRoC sequence inside the pinned image; see src/vioreg/runner.py.
#   usage: scripts/run_sequence.sh <SEQ> <OUT_ROOT>
#   env:   RUNS=1  MODE=serial|realtime  CPUS=<docker --cpus>  CONFIG_DIR=<override dir>
#          IMAGE=<repo@sha256:...>  DATA=data/euroc  BAG_START=<s>  BAG_RATE=<x, realtime only>
# IMAGE defaults to the single line of docker/IMAGE_REF (the published image, pinned by registry
# digest). The run refuses to start unless the local image carries exactly that digest.
# Writes <OUT_ROOT>/<SEQ>/run<k>.{tum,ov.txt,wall_s,config_hash,image_digest,log,cmd}.
# Exit 1 if any run failed.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "${here}/.venv/bin/python" ]; then py="${here}/.venv/bin/python"; else py=python3; fi
PYTHONPATH="${here}/src${PYTHONPATH:+:${PYTHONPATH}}" exec "$py" -m vioreg.runner "$@"
