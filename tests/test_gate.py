import numpy as np
import pytest

from vioreg.gate import evaluate_gate, threshold_from_baseline

LIMITS = {"max_ate_m": 0.20, "max_rpe_m": 0.05, "min_coverage": 0.95}


def result(rmse=0.10, cov=1.0, rpe=0.02, ok=True):
    return {"ate": {"ok": ok, "rmse": rmse, "coverage": cov},
            "rpe": {"ok": True, "rmse": rpe}}


def test_good_run_passes():
    assert evaluate_gate(result(), LIMITS) == []


def test_ate_regression_fails_with_numbers_in_message():
    fails = evaluate_gate(result(rmse=0.35), LIMITS)
    assert len(fails) == 1 and "0.350" in fails[0] and "0.200" in fails[0]


def test_lost_tracking_fails_even_if_ate_is_small():
    # A filter that dies early can look accurate on the part it did track.
    assert any("coverage" in f for f in evaluate_gate(result(rmse=0.05, cov=0.5), LIMITS))


def test_unusable_result_fails_closed():
    assert evaluate_gate({"ate": {"ok": False, "reason": "no poses"}}, LIMITS)
    assert evaluate_gate({}, LIMITS)


def test_threshold_never_below_relative_margin():
    samples = [0.100, 0.100, 0.100, 0.100, 0.100]  # zero MAD
    assert threshold_from_baseline(samples) == pytest.approx(0.115)


def test_threshold_widens_with_noisy_baseline():
    tight = threshold_from_baseline([0.10, 0.101, 0.099, 0.10, 0.10])
    noisy = threshold_from_baseline([0.08, 0.14, 0.10, 0.12, 0.09])
    assert noisy > tight


def test_threshold_needs_enough_samples():
    with pytest.raises(ValueError):
        threshold_from_baseline([0.1, 0.1])


def test_gate_catches_injected_degradation():
    """Meta-test: a 30 percent worse estimator must fail a gate built from baseline."""
    base = np.array([0.100, 0.104, 0.097, 0.102, 0.099, 0.101, 0.103])
    limit = threshold_from_baseline(base)
    limits = dict(LIMITS, max_ate_m=limit)
    assert evaluate_gate(result(rmse=float(np.median(base))), limits) == []
    assert evaluate_gate(result(rmse=float(np.median(base)) * 1.30), limits)
