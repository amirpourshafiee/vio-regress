#!/usr/bin/env python3
"""Write configs/thresholds.yaml (v2) from baseline.json plus margins.

Thin wrapper; the logic and its tests live in src/vioreg/thresholds.py and
tests/test_thresholds.py.

    python scripts/make_thresholds.py --baseline docs/variance/baseline.json \\
        --rpe-ratio-max R --ate-geomean-ratio-max A --trajectories results/variance/A_serial
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vioreg.thresholds import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
