"""Gate v2 against the committed configs/thresholds.yaml and real golden results.

tests/golden_results/<seq>.json   real S3 baseline score JSONs (A_serial, 10 identical runs)
tests/golden_results/variants/    two bundles of REAL sweep results re-keyed to the baseline
                                  key (scripts/experiments/make_golden_bundle.py):
  legit_fast_threshold_18   the worst legitimate variant with a fine outcome
                            (RPE max 1.056, ATE geomean 1.286): must PASS
  known_bad_num_pts_50      the hardest known-bad variant to catch
                            (RPE max 1.431 on MH_01, ATE geomean 1.306): must FAIL
"""
import json
import shutil
from pathlib import Path

import yaml

from vioreg import gate

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests" / "golden_results"
CFG = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())


def bundle_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    for seq, r in json.loads((GOLD / "variants" / f"{name}.json").read_text())["results"].items():
        (d / f"{seq}.json").write_text(json.dumps(r))
    return d


def test_typical_baseline_run_passes():
    rep = gate.evaluate_results_dir(GOLD, CFG, require=True, root=ROOT)
    assert rep["passed"], rep
    assert gate.aggregate_ate_ratio(
        {s: json.loads((GOLD / f"{s}.json").read_text()) for s in CFG["sequences"]}, CFG) == 1.0


def test_legitimate_variant_passes(tmp_path):
    rep = gate.evaluate_results_dir(bundle_dir(tmp_path, "legit_fast_threshold_18"), CFG,
                                    require=True, root=ROOT)
    assert rep["passed"], rep


def test_known_bad_variant_fails_on_rpe(tmp_path):
    rep = gate.evaluate_results_dir(bundle_dir(tmp_path, "known_bad_num_pts_50"), CFG,
                                    require=True, root=ROOT)
    assert not rep["passed"]
    assert any("RPE ratio" in f for f in rep["sequences"]["MH_01_easy"]), rep


def test_missing_results_fail_closed(tmp_path):
    d = tmp_path / "res"
    shutil.copytree(GOLD, d, ignore=shutil.ignore_patterns("variants"))
    (d / "V1_01_easy.json").unlink()
    rep = gate.evaluate_results_dir(d, CFG, require=True, root=ROOT)
    assert not rep["passed"]
    assert any("missing result" in f for f in rep["sequences"]["V1_01_easy"])
    assert rep["aggregate"]


def test_missing_baseline_key_fails_needs_rebaseline(tmp_path):
    d = tmp_path / "res"
    shutil.copytree(GOLD, d, ignore=shutil.ignore_patterns("variants"))
    r = json.loads((d / "MH_03_medium.json").read_text())
    r["config_hash"] = "0" * 40  # e.g. a config edit that was not re-baselined
    (d / "MH_03_medium.json").write_text(json.dumps(r))
    rep = gate.evaluate_results_dir(d, CFG, require=True, root=ROOT)
    assert not rep["passed"]
    assert any("needs re-baseline" in f for f in rep["sequences"]["MH_03_medium"])


def test_goldens_follow_the_frozen_contract():
    keys = {"sequence", "backend", "vio_commit", "image_digest", "harness_commit", "config_hash",
            "runs", "align", "skip_s", "ate", "rpe", "per_run_ate_rmse", "wall_s"}
    for f in GOLD.glob("*.json"):
        assert set(json.loads(f.read_text())) == keys, f
    for f in (GOLD / "variants").glob("*.json"):
        for r in json.loads(f.read_text())["results"].values():
            assert set(r) == keys, f


def test_committed_margins_match_the_measured_separation():
    """Guard against silently loosening: the RPE margin must sit strictly between the
    measured legitimate per-sequence max (1.145, MH_03 start -2 s) and the known-bad min
    (1.431, num_pts 50 on MH_01); see docs/gate.md."""
    m = CFG["margins"]
    assert 1.145 < m["rpe_ratio_max"] < 1.431
    assert 1.286 < m["ate_geomean_ratio_max"] < 2.038  # backstop: above legit, below 2 of 3 bad
