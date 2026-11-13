"""The real gate. One test per sequence, reading results/<seq>.json written by the runner.

In CI tiers that are supposed to have results, set HARNESS_REQUIRE_RESULTS=1 so a
missing file FAILS the test. A gate that skips silently is a gate that is off.
"""
import json
import os
from pathlib import Path

import pytest
import yaml

from vioreg.gate import evaluate_gate

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(os.environ.get("HARNESS_RESULTS", ROOT / "results"))
CFG = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())
REQUIRE = os.environ.get("HARNESS_REQUIRE_RESULTS") == "1"


@pytest.mark.sequence
@pytest.mark.parametrize("seq", sorted(CFG["sequences"]))
def test_sequence_within_gate(seq):
    f = RESULTS / f"{seq}.json"
    if not f.exists():
        if REQUIRE:
            pytest.fail(f"missing result file {f}; the runner did not produce it")
        pytest.skip(f"no result for {seq}")
    limits = {**CFG["defaults"], **CFG["sequences"][seq]}
    fails = evaluate_gate(json.loads(f.read_text()), limits)
    assert not fails, f"{seq}: " + "; ".join(fails)
