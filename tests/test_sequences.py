"""The real gate (v2, S4). Reads results/<seq>.json written by score.py; see src/vioreg/gate.py.

One test per sequence (hard checks, baseline key, RPE ratio), one test for the ATE
geometric mean across all sequences, and one that only WARNS when poses differ from the
baseline trajectory under an identical (vio_commit, image_digest, config_hash).

In CI tiers that are supposed to have results, set HARNESS_REQUIRE_RESULTS=1 so a missing
file FAILS. A gate that skips silently is a gate that is off.
"""
import os
import warnings
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from vioreg import gate

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(os.environ.get("HARNESS_RESULTS", ROOT / "results"))
CFG = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())
REQUIRE = os.environ.get("HARNESS_REQUIRE_RESULTS") == "1"


@lru_cache(maxsize=1)
def report():
    return gate.evaluate_results_dir(RESULTS, CFG, require=REQUIRE, root=ROOT)


@pytest.mark.sequence
@pytest.mark.parametrize("seq", sorted(CFG["sequences"]))
def test_sequence_within_gate(seq):
    rep = report()
    if seq in rep["skipped"]:
        pytest.skip(f"no result for {seq} (HARNESS_REQUIRE_RESULTS is not 1)")
    fails = rep["sequences"][seq]
    assert not fails, f"{seq}: " + "; ".join(fails)


@pytest.mark.sequence
def test_ate_geometric_mean_within_gate():
    rep = report()
    if rep["skipped"]:
        pytest.skip("not every sequence has a result (HARNESS_REQUIRE_RESULTS is not 1)")
    assert not rep["aggregate"], "; ".join(rep["aggregate"])


@pytest.mark.sequence
def test_trajectories_match_baseline_warning_only():
    """Never fails: a pose-level change under an identical setup is reported for review."""
    rep = report()
    for w in rep["warnings"]:
        gate.emit_warning(w)
        warnings.warn(w, UserWarning, stacklevel=1)
    for n in rep["notes"]:
        print(f"note: {n}")
