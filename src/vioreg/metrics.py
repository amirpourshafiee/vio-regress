"""Trajectory error metrics: ATE (aligned) and RPE (alignment free)."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .euroc_io import Trajectory, associate


def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool = False):
    """Least squares R, t, s such that dst ~= s * R @ src + t. Inputs are (N, 3)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    xs, xd = src - mu_s, dst - mu_d
    cov = xd.T @ xs / len(src)
    u, d, vt = np.linalg.svd(cov)
    s_mat = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        s_mat[2, 2] = -1.0
    rot = u @ s_mat @ vt
    scale = 1.0
    if with_scale:
        scale = float(np.trace(np.diag(d) @ s_mat) / ((xs**2).sum() / len(src)))
    return rot, mu_d - scale * rot @ mu_s, scale


def yaw_align(src: np.ndarray, dst: np.ndarray):
    """4-DoF alignment (yaw about +z plus translation). Right choice for VIO,
    because gravity makes roll and pitch observable."""
    xs, xd = src - src.mean(0), dst - dst.mean(0)
    a = (xs[:, 0] * xd[:, 1] - xs[:, 1] * xd[:, 0]).sum()
    b = (xs[:, 0] * xd[:, 0] + xs[:, 1] * xd[:, 1]).sum()
    th = np.arctan2(a, b)
    c, s = np.cos(th), np.sin(th)
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return rot, dst.mean(0) - rot @ src.mean(0), 1.0


def align_points(est_p, gt_p, mode: str):
    if mode == "none":
        return est_p
    fn = {
        "se3": lambda a, b: umeyama(a, b, False),
        "sim3": lambda a, b: umeyama(a, b, True),
        "posyaw": yaw_align,
    }[mode]
    rot, t, s = fn(est_p, gt_p)
    return s * est_p @ rot.T + t


def ate(est: Trajectory, gt: Trajectory, align: str = "posyaw",
        max_dt: float = 0.005, skip_s: float = 0.0) -> dict:
    """Absolute trajectory error. skip_s drops the first seconds of ground truth,
    which is how you skip the Machine Hall pick-up phase."""
    ie, ig = associate(est, gt, max_dt)
    keep = gt.t[ig] >= gt.t[0] + skip_s
    ie, ig = ie[keep], ig[keep]
    if len(ie) < 10:
        return {"ok": False, "reason": "fewer than 10 associated poses", "n": len(ie)}
    aligned = align_points(est.p[ie], gt.p[ig], align)
    err = np.linalg.norm(aligned - gt.p[ig], axis=1)
    span = gt.t[ig][-1] - gt.t[ig][0]
    return {
        "ok": True,
        "n": int(len(ie)),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "mean": float(err.mean()),
        "median": float(np.median(err)),
        "max": float(err.max()),
        "coverage": float(span / (gt.t[-1] - gt.t[0] - skip_s)),
        "per_pose": err,  # strip before writing JSON
        "t": gt.t[ig],
    }


def rpe(est: Trajectory, gt: Trajectory, delta_s: float = 1.0,
        max_dt: float = 0.005) -> dict:
    """Translational relative pose error over windows of about delta_s seconds."""
    ie, ig = associate(est, gt, max_dt)
    t = gt.t[ig]
    j = np.searchsorted(t, t + delta_s)
    valid = j < len(t)
    i = np.nonzero(valid)[0]
    j = j[valid]
    if len(i) < 10:
        return {"ok": False, "reason": "too few windows", "n": int(len(i))}
    re = Rotation.from_quat(est.q[ie]).as_matrix()
    rg = Rotation.from_quat(gt.q[ig]).as_matrix()
    pe, pg = est.p[ie], gt.p[ig]
    rel_e = np.einsum("nji,nj->ni", re[i], pe[j] - pe[i])  # R_i^T (p_j - p_i)
    rel_g = np.einsum("nji,nj->ni", rg[i], pg[j] - pg[i])
    rot_g = np.einsum("nji,njk->nik", rg[i], rg[j])  # R_i^T R_j
    err = np.linalg.norm(np.einsum("nji,nj->ni", rot_g, rel_e - rel_g), axis=1)
    return {"ok": True, "n": int(len(i)), "rmse": float(np.sqrt(np.mean(err**2))),
            "mean": float(err.mean()), "max": float(err.max())}
