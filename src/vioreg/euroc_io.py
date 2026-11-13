"""Trajectory loading and timestamp association. All times are float seconds."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Trajectory:
    t: np.ndarray  # (N,) seconds, strictly increasing
    p: np.ndarray  # (N, 3) metres
    q: np.ndarray  # (N, 4) unit quaternion, order x y z w

    def __post_init__(self) -> None:
        n = len(self.t)
        if self.p.shape != (n, 3) or self.q.shape != (n, 4):
            raise ValueError("shape mismatch between t, p, q")
        if n > 1 and not np.all(np.diff(self.t) > 0):
            raise ValueError("timestamps must be strictly increasing")

    def __len__(self) -> int:
        return len(self.t)


def load_euroc_gt(path: str | Path) -> Trajectory:
    """EuRoC state_groundtruth_estimate0/data.csv.

    Columns: timestamp[ns], p_x, p_y, p_z, q_w, q_x, q_y, q_z, then velocities
    and biases, which we ignore.
    """
    a = np.loadtxt(path, delimiter=",", skiprows=1, usecols=range(8), ndmin=2)
    return Trajectory(t=a[:, 0] * 1e-9, p=a[:, 1:4], q=a[:, [5, 6, 7, 4]])


def load_tum(path: str | Path) -> Trajectory:
    """TUM format: t[s] px py pz qx qy qz qw, whitespace separated, # comments."""
    a = np.loadtxt(path, comments="#")
    if a.shape[1] < 8:
        raise ValueError(f"{path}: expected 8 columns, got {a.shape[1]}")
    return Trajectory(t=a[:, 0], p=a[:, 1:4], q=a[:, 4:8])


def save_tum(traj: Trajectory, path: str | Path) -> None:
    rows = np.column_stack([traj.t, traj.p, traj.q])
    np.savetxt(path, rows, fmt="%.9f")


def associate(est: Trajectory, gt: Trajectory, max_dt: float = 0.005):
    """Match each estimate to the nearest ground-truth sample in time.

    Returns (est_idx, gt_idx) arrays. Pairs further apart than max_dt are dropped.
    Nearest-neighbour is fine at 200 Hz ground truth; interpolate if yours is sparser.
    """
    j = np.searchsorted(gt.t, est.t)
    j0 = np.clip(j - 1, 0, len(gt) - 1)
    j1 = np.clip(j, 0, len(gt) - 1)
    pick_left = np.abs(est.t - gt.t[j0]) <= np.abs(est.t - gt.t[j1])
    jj = np.where(pick_left, j0, j1)
    ok = np.abs(est.t - gt.t[jj]) <= max_dt
    return np.nonzero(ok)[0], jj[ok]
