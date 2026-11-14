import numpy as np
import pytest

from vioreg import gate
from vioreg.gate import threshold_from_baseline

KEY = {"vio_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64, "config_hash": "c" * 40}
SEQS = ["MH_01_easy", "MH_03_medium", "V1_01_easy"]
BASE = {"MH_01_easy": (0.0909, 0.0264, 0.973, 2746),
        "MH_03_medium": (0.0915, 0.0463, 0.979, 2502),
        "V1_01_easy": (0.0634, 0.0444, 0.968, 2768)}


def cfg(rpe_max=1.10, ate_max=1.30, cov_drop=0.01, n_drop=4, extra_key=None):
    seqs = {}
    for s, (a, r, c, n) in BASE.items():
        seqs[s] = {"skip_s": 0.0, "baselines": [{**KEY, "ate_rmse": a, "rpe_rmse": r,
                                                  "coverage": c, "n": n}]}
        if extra_key:
            seqs[s]["baselines"].append({**extra_key, "ate_rmse": 1.0, "rpe_rmse": 1.0,
                                         "coverage": 0.5, "n": 1})
    return {"version": 2, "defaults": {"align": "posyaw"},
            "margins": {"rpe_ratio_max": rpe_max, "ate_geomean_ratio_max": ate_max,
                        "coverage_drop_max": cov_drop, "pose_count_drop_max": n_drop},
            "sequences": seqs}


def result(seq, ate_x=1.0, rpe_x=1.0, cov_d=0.0, n_d=0, ok=True, runs=3, failed_runs=0, **key):
    a, r, c, n = BASE[seq]
    per = [a * ate_x] * (runs - failed_runs) + [None] * failed_runs
    return {"sequence": seq, "backend": "openvins", **KEY, "harness_commit": "d" * 40, **key,
            "runs": runs, "align": "posyaw", "skip_s": 0.0,
            "ate": {"ok": ok, "rmse": a * ate_x if ok else None, "median": None, "max": None,
                    "coverage": c + cov_d if ok else None, "n": n + n_d if ok else None},
            "rpe": {"ok": ok, "rmse": r * rpe_x if ok else None, "delta_s": 1.0},
            "per_run_ate_rmse": per, "wall_s": 40.0}


def all_results(**over):
    return {s: over.get(s, result(s)) for s in SEQS}


# ---------------------------------------------------------------- per sequence (hard + RPE)
def test_typical_baseline_run_passes():
    c = cfg()
    for s in SEQS:
        assert gate.evaluate_sequence(result(s), c, s) == []
    assert gate.evaluate_aggregate(all_results(), c) == []


@pytest.mark.parametrize("bad,needle", [
    (dict(ok=False), "ok=false"),
    (dict(failed_runs=1), "failed run"),
    (dict(cov_d=-0.011), "coverage"),
    (dict(n_d=-5), "pose count"),
])
def test_hard_failures(bad, needle):
    fails = gate.evaluate_sequence(result("MH_01_easy", **bad), cfg(), "MH_01_easy")
    assert fails and any(needle in f for f in fails), fails


def test_coverage_within_one_point_and_more_poses_pass():
    assert gate.evaluate_sequence(result("MH_01_easy", cov_d=-0.009, n_d=+5), cfg(),
                                  "MH_01_easy") == []


def test_recorder_race_pose_loss_within_allowance_passes():
    """Up to 2 poses lost at each end by the recorder (identical estimator output)."""
    assert gate.evaluate_sequence(result("MH_01_easy", n_d=-4), cfg(n_drop=4), "MH_01_easy") == []
    assert gate.evaluate_sequence(result("MH_01_easy", n_d=-1), cfg(n_drop=0), "MH_01_easy")


def test_rpe_ratio_over_margin_fails_with_numbers():
    fails = gate.evaluate_sequence(result("V1_01_easy", rpe_x=1.20), cfg(rpe_max=1.10),
                                   "V1_01_easy")
    assert len(fails) == 1 and "RPE" in fails[0] and "1.200" in fails[0] and "1.100" in fails[0]


def test_rpe_ratio_at_margin_passes():
    assert gate.evaluate_sequence(result("V1_01_easy", rpe_x=1.10), cfg(rpe_max=1.10),
                                  "V1_01_easy") == []


def test_single_sequence_ate_is_not_gated_per_sequence():
    """Rule 3: no single-sequence ATE gate; a +60% ATE on one sequence alone is judged
    only through the cross-sequence geometric mean."""
    assert gate.evaluate_sequence(result("MH_01_easy", ate_x=1.60), cfg(), "MH_01_easy") == []


# ---------------------------------------------------------------- baseline key
def test_missing_baseline_key_fails_with_needs_rebaseline():
    r = result("MH_01_easy", config_hash="e" * 40)
    fails = gate.evaluate_sequence(r, cfg(), "MH_01_easy")
    assert len(fails) == 1 and "needs re-baseline" in fails[0] and "eeeeeeee" in fails[0]


@pytest.mark.parametrize("field", ["vio_commit", "image_digest", "config_hash"])
def test_each_key_field_must_match(field):
    r = result("MH_01_easy", **{field: "sha256:" + "f" * 64 if field == "image_digest" else "f" * 40})
    assert any("needs re-baseline" in f for f in gate.evaluate_sequence(r, cfg(), "MH_01_easy"))


def test_correct_baseline_chosen_among_several_keys():
    other = {"vio_commit": "9" * 40, "image_digest": "sha256:" + "9" * 64, "config_hash": "9" * 40}
    assert gate.evaluate_sequence(result("MH_01_easy"), cfg(extra_key=other), "MH_01_easy") == []


@pytest.mark.parametrize("field", ["vio_commit", "image_digest", "config_hash", "harness_commit"])
def test_missing_provenance_fails_closed(field):
    r = result("MH_01_easy")
    r[field] = None
    assert gate.evaluate_sequence(r, cfg(), "MH_01_easy")


def test_unknown_sequence_fails_closed():
    assert any("not in thresholds" in f
               for f in gate.evaluate_sequence(result("MH_01_easy"), cfg(), "MH_09_bogus"))


def test_garbage_result_fails_closed():
    assert gate.evaluate_sequence({}, cfg(), "MH_01_easy")
    assert gate.evaluate_sequence({"ate": {"ok": True}}, cfg(), "MH_01_easy")


# ---------------------------------------------------------------- aggregate ATE
def test_aggregate_is_geometric_mean_of_ratios():
    # 1.5 * 1.0 * 1.0 -> geomean 1.5 ** (1/3) = 1.1447
    rs = all_results(MH_01_easy=result("MH_01_easy", ate_x=1.5))
    assert gate.aggregate_ate_ratio(rs, cfg()) == pytest.approx(1.5 ** (1 / 3))
    assert gate.evaluate_aggregate(rs, cfg(ate_max=1.15)) == []
    fails = gate.evaluate_aggregate(rs, cfg(ate_max=1.14))
    assert fails and "geometric mean" in fails[0] and "1.145" in fails[0]


def test_aggregate_fails_closed_on_missing_sequence():
    rs = all_results()
    rs["V1_01_easy"] = None
    fails = gate.evaluate_aggregate(rs, cfg())
    assert fails and "V1_01_easy" in fails[0]


def test_aggregate_fails_closed_on_unusable_or_rekeyed_result():
    assert gate.evaluate_aggregate(all_results(V1_01_easy=result("V1_01_easy", ok=False)), cfg())
    rk = all_results(V1_01_easy=result("V1_01_easy", vio_commit="f" * 40))
    assert any("needs re-baseline" in f for f in gate.evaluate_aggregate(rk, cfg()))


# ---------------------------------------------------------------- results directory + pose warning
def write_results(d, rs):
    import json
    d.mkdir(parents=True, exist_ok=True)
    for s, r in rs.items():
        if r is not None:
            (d / f"{s}.json").write_text(json.dumps(r))


def test_missing_results_fail_closed_when_required(tmp_path):
    rs = all_results()
    rs["MH_03_medium"] = None
    write_results(tmp_path / "res", rs)
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=True)
    assert any("missing result" in f for f in rep["sequences"]["MH_03_medium"])
    assert rep["aggregate"]
    assert not rep["passed"]


def test_missing_results_are_skipped_only_when_not_required(tmp_path):
    rs = all_results()
    rs["MH_03_medium"] = None
    write_results(tmp_path / "res", rs)
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=False)
    assert rep["skipped"] == ["MH_03_medium"]


def test_unparsable_result_fails_closed(tmp_path):
    d = tmp_path / "res"
    write_results(d, all_results())
    (d / "V1_01_easy.json").write_text("{not json")
    rep = gate.evaluate_results_dir(d, cfg(), require=True)
    assert any("unparsable" in f for f in rep["sequences"]["V1_01_easy"])


def _tum(path, n=5, shift=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{100 + 0.05 * i:.9f} {i * 0.1 + shift:.9f} 0 0 0 0 0 1\n"
                            for i in range(n)))


def test_pose_change_under_same_key_is_a_warning_not_a_failure(tmp_path):
    c = cfg()
    _tum(tmp_path / "baselines" / "MH_01_easy.tum")
    c["sequences"]["MH_01_easy"]["baselines"][0]["trajectory"] = "baselines/MH_01_easy.tum"
    d = tmp_path / "res"
    write_results(d, all_results())
    _tum(d / "MH_01_easy" / "run1.tum", shift=1e-6)  # same key, slightly different poses
    rep = gate.evaluate_results_dir(d, c, require=True, root=tmp_path)
    assert rep["passed"]
    assert any("MH_01_easy" in w and "differs from the baseline" in w for w in rep["warnings"])


def test_identical_poses_and_shutdown_race_raise_no_warning(tmp_path):
    c = cfg()
    _tum(tmp_path / "baselines" / "MH_01_easy.tum", n=6)
    c["sequences"]["MH_01_easy"]["baselines"][0]["trajectory"] = "baselines/MH_01_easy.tum"
    d = tmp_path / "res"
    write_results(d, all_results())
    _tum(d / "MH_01_easy" / "run1.tum", n=5)  # baseline minus its final pose
    rep = gate.evaluate_results_dir(d, c, require=True, root=tmp_path)
    assert rep["passed"] and not [w for w in rep["warnings"] if "differs" in w]


# ---------------------------------------------------------------- legacy helper (kept, unused by gate)
def test_threshold_collapses_to_relative_margin_on_deterministic_baseline():
    """Why S4 does not use it: identical runs give MAD = 0, so the spread term adds nothing."""
    assert threshold_from_baseline([0.100] * 5) == pytest.approx(0.115)


def test_threshold_widens_with_noisy_baseline():
    tight = threshold_from_baseline([0.10, 0.101, 0.099, 0.10, 0.10])
    noisy = threshold_from_baseline([0.08, 0.14, 0.10, 0.12, 0.09])
    assert noisy > tight


def test_threshold_needs_enough_samples():
    with pytest.raises(ValueError):
        threshold_from_baseline([0.1, 0.1])


# ---------------------------------------------------------------- S5a: scoped runs + verdicts
def test_scoped_run_judges_only_listed_sequences(tmp_path):
    """Nightly runs ONE sequence: the others are out of scope, not missing."""
    write_results(tmp_path / "res", {"MH_01_easy": result("MH_01_easy")})
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=True,
                                    scope=["MH_01_easy"])
    assert set(rep["sequences"]) == {"MH_01_easy"} and rep["sequences"]["MH_01_easy"] == []
    assert rep["aggregate"] == [] and rep["aggregate_skipped"]
    assert "1 of 3" in rep["aggregate_skipped"]
    assert rep["passed"]


def test_scoped_run_still_fails_closed_on_its_own_missing_sequence(tmp_path):
    (tmp_path / "res").mkdir()
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=True, scope=["MH_01_easy"])
    assert not rep["passed"] and any("missing result" in f for f in rep["sequences"]["MH_01_easy"])


def test_scope_with_unknown_sequence_fails_closed(tmp_path):
    write_results(tmp_path / "res", all_results())
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=True,
                                    scope=["MH_01_easy", "MH_09_bogus"])
    assert not rep["passed"] and any("not in thresholds" in f for f in rep["sequences"]["MH_09_bogus"])


def test_full_scope_still_computes_the_aggregate(tmp_path):
    write_results(tmp_path / "res", all_results(MH_01_easy=result("MH_01_easy", ate_x=3.0)))
    rep = gate.evaluate_results_dir(tmp_path / "res", cfg(), require=True, scope=SEQS)
    assert rep["aggregate"] and not rep["aggregate_skipped"] and not rep["passed"]


def test_parse_scope():
    assert gate.parse_scope(None) is None and gate.parse_scope("  ") is None
    assert gate.parse_scope("MH_01_easy  V1_01_easy,MH_03_medium") == [
        "MH_01_easy", "V1_01_easy", "MH_03_medium"]


@pytest.mark.parametrize("fails,verdict", [
    ([], "pass"),
    (["needs re-baseline: no baseline for (MH_01_easy, ...)"], "needs-rebaseline"),
    (["RPE ratio 1.500 > 1.280"], "quality-fail"),
    (["missing result file results/MH_01_easy.json"], "missing"),
    (["unparsable result x"], "missing"),
    (["invalid result: provenance missing (vio_commit)"], "invalid"),
    (["ok=false (ate.ok=False, rpe.ok=False)"], "quality-fail"),
])
def test_classify_keeps_rebaseline_apart_from_quality(fails, verdict):
    assert gate.classify(fails) == verdict
