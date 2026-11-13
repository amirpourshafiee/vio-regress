"""Pick failure windows and control windows from a per-pose error series."""
from __future__ import annotations

import numpy as np


def pick_windows(t: np.ndarray, err: np.ndarray, win_s: float = 5.0,
                 top_k: int = 10, control_k: int = 5) -> list[dict]:
    """Split the run into consecutive win_s windows, rank by peak error.

    Returns the top_k worst windows (source='natural') plus control_k of the best
    windows (source='control'). Controls matter: a judge that always says
    'there is a visual problem' must be caught by windows where nothing went wrong.
    """
    edges = np.arange(t[0], t[-1], win_s)
    rows = []
    for a in edges:
        m = (t >= a) & (t < a + win_s)
        if m.sum() < 5:
            continue
        rows.append({"t0": float(a), "t1": float(a + win_s), "peak_err_m": float(err[m].max())})
    rows.sort(key=lambda r: r["peak_err_m"], reverse=True)
    worst = [dict(r, source="natural") for r in rows[:top_k]]
    best = [dict(r, source="control") for r in rows[::-1][:control_k]]
    return worst + best
