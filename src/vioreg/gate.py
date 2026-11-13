"""Gate logic: turn metrics plus limits into a list of human-readable failures."""
from __future__ import annotations

import numpy as np


def threshold_from_baseline(samples, k: float = 3.0, rel_margin: float = 0.15,
                            floor: float = 0.01) -> float:
    """Robust limit from repeated baseline runs of the SAME code on the SAME data.

    median + k * scaled MAD handles run-to-run noise; rel_margin keeps the limit
    from collapsing when the baseline happens to be very tight; floor guards
    against a limit of ~0 on an unusually clean sequence.
    """
    x = np.asarray(samples, dtype=float)
    if len(x) < 5:
        raise ValueError("need at least 5 baseline runs to set a threshold")
    med = float(np.median(x))
    mad = 1.4826 * float(np.median(np.abs(x - med)))
    return max(med + k * mad, med * (1.0 + rel_margin), floor)


def evaluate_gate(result: dict, limits: dict) -> list[str]:
    """Return failure messages. Empty list means the sequence passes."""
    fails: list[str] = []
    if not result.get("ate", {}).get("ok", False):
        return [f"no usable ATE: {result.get('ate', {}).get('reason', 'missing')}"]
    ate = result["ate"]
    if ate["coverage"] < limits.get("min_coverage", 0.95):
        fails.append(f"coverage {ate['coverage']:.2f} < {limits['min_coverage']:.2f}"
                     " (tracking lost or estimator died)")
    if ate["rmse"] > limits["max_ate_m"]:
        fails.append(f"ATE RMSE {ate['rmse']:.3f} m > limit {limits['max_ate_m']:.3f} m")
    rpe = result.get("rpe", {})
    if "max_rpe_m" in limits and rpe.get("ok") and rpe["rmse"] > limits["max_rpe_m"]:
        fails.append(f"RPE RMSE {rpe['rmse']:.3f} m > limit {limits['max_rpe_m']:.3f} m")
    return fails
