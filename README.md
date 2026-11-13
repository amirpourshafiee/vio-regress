# vioreg starter

Tested starter modules from the build guide: metrics, gate, faults, windows, judge, kappa.
Not included on purpose: runner scripts, score.py, report.py, make_thresholds.py (sessions S1 to S4).
Dockerfile and workflows are UNTESTED skeletons.

    python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
    pytest -m "not sequence and not slow"
    HARNESS_RESULTS=tests/golden_results HARNESS_REQUIRE_RESULTS=1 pytest -m sequence

tests/golden_results holds hand-made JSONs that exercise gate logic only. They are not VIO results.
Every sequence listed in configs/thresholds.yaml needs a golden file, or the PR tier fails by design.
Thresholds in configs/thresholds.yaml are placeholders until you run the baseline experiment.
