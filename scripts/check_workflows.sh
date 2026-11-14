#!/usr/bin/env bash
# Cheap local check of .github/workflows: actionlint (syntax, expressions, shellcheck of run:
# blocks if shellcheck is installed) when available, plus the invariant tests.
#   usage: scripts/check_workflows.sh
# actionlint is optional; install a pinned, checksum-verified release, e.g. v1.7.7:
#   https://github.com/rhysd/actionlint/releases/tag/v1.7.7
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
AL=$(command -v actionlint || true); [ -z "$AL" ] && [ -x .venv/bin/actionlint ] && AL=.venv/bin/actionlint
if [ -n "$AL" ]; then
  echo "actionlint $($AL -version | head -1)"; "$AL" .github/workflows/*.yml && echo "actionlint: OK"
else
  echo "actionlint not installed; skipping it (invariant tests still run)"
fi
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
$PY -m pytest -q tests/test_workflows.py
