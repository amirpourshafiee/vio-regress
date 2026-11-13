import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from vioreg.euroc_io import Trajectory, associate
from vioreg.metrics import ate, rpe, umeyama, yaw_align


def make_traj(n=400, hz=20.0, seed=0) -> Trajectory:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / hz
    p = np.column_stack([2 * np.sin(0.5 * t), 1.5 * np.cos(0.4 * t), 0.3 * np.sin(t)])
    q = Rotation.from_euler("xyz", rng.normal(0, 0.05, (n, 3)).cumsum(0) * 0.1).as_quat()
    return Trajectory(t=t, p=p, q=q)


def transform(tr: Trajectory, rot: np.ndarray, t: np.ndarray) -> Trajectory:
    q = (Rotation.from_matrix(rot) * Rotation.from_quat(tr.q)).as_quat()
    return Trajectory(t=tr.t.copy(), p=tr.p @ rot.T + t, q=q)


def test_umeyama_recovers_rigid_transform():
    rng = np.random.default_rng(1)
    src = rng.normal(size=(100, 3))
    rot = Rotation.from_euler("xyz", [0.3, -0.2, 1.1]).as_matrix()
    t = np.array([1.0, -2.0, 0.5])
    r, tt, s = umeyama(src, src @ rot.T + t)
    assert np.allclose(r, rot, atol=1e-9) and np.allclose(tt, t, atol=1e-9) and s == 1.0


def test_umeyama_recovers_scale():
    rng = np.random.default_rng(2)
    src = rng.normal(size=(100, 3))
    _, _, s = umeyama(src, 2.5 * src, with_scale=True)
    assert s == pytest.approx(2.5)


def test_yaw_align_recovers_yaw_only():
    gt = make_traj()
    rot = Rotation.from_euler("z", 0.7).as_matrix()
    est = transform(gt, rot.T, np.array([3.0, 1.0, -1.0]))  # est = gt in another frame
    r, _, _ = yaw_align(est.p, gt.p)
    assert np.allclose(r, rot, atol=1e-9)


@pytest.mark.parametrize("align", ["se3", "posyaw"])
def test_ate_zero_for_pure_frame_change(align):
    gt = make_traj()
    rot = Rotation.from_euler("z", -1.2).as_matrix()
    est = transform(gt, rot, np.array([5.0, -3.0, 0.0]))
    out = ate(est, gt, align=align)
    assert out["rmse"] < 1e-9 and out["coverage"] == pytest.approx(1.0)


def test_ate_matches_injected_noise():
    gt = make_traj(n=4000)
    sigma = 0.02
    noise = np.random.default_rng(3).normal(0, sigma, gt.p.shape)
    est = Trajectory(gt.t, gt.p + noise, gt.q)
    out = ate(est, gt, align="se3")
    assert out["rmse"] == pytest.approx(sigma * np.sqrt(3), rel=0.05)


def test_ate_reports_lost_tracking_as_low_coverage():
    gt = make_traj()
    cut = len(gt) // 2
    est = Trajectory(gt.t[:cut], gt.p[:cut], gt.q[:cut])
    assert ate(est, gt)["coverage"] == pytest.approx(0.5, abs=0.02)


def test_ate_skip_s_drops_leading_seconds():
    gt = make_traj()
    est = Trajectory(gt.t, gt.p.copy(), gt.q)
    est.p[:40] += 5.0  # garbage during the first 2 s (pick-up phase)
    assert ate(est, gt, align="none")["rmse"] > 1.0
    assert ate(est, gt, align="none", skip_s=2.5)["rmse"] < 1e-9


def test_rpe_is_alignment_free():
    gt = make_traj()
    est = transform(gt, Rotation.from_euler("xyz", [0.1, 0.2, 0.3]).as_matrix(),
                    np.array([9.0, 9.0, 9.0]))
    assert rpe(est, gt, delta_s=1.0)["rmse"] < 1e-9


def test_associate_drops_far_timestamps():
    gt = make_traj()
    est = Trajectory(gt.t + 0.5, gt.p, gt.q)  # shifted by 0.5 s (10 frames)
    ie, ig = associate(est, gt, max_dt=0.005)
    assert np.all(np.abs(est.t[ie] - gt.t[ig]) <= 0.005)


def test_matches_evo():
    """Cross-check against the reference tool. evo is a dev-only dependency (GPL)."""
    evo_traj = pytest.importorskip("evo.core.trajectory")
    evo_metrics = pytest.importorskip("evo.core.metrics")
    gt = make_traj(n=600)
    rng = np.random.default_rng(4)
    est = Trajectory(gt.t, gt.p + rng.normal(0, 0.03, gt.p.shape), gt.q)

    def to_evo(tr):
        wxyz = tr.q[:, [3, 0, 1, 2]]
        return evo_traj.PoseTrajectory3D(tr.p, wxyz, tr.t)

    ape = evo_metrics.APE(evo_metrics.PoseRelation.translation_part)
    e = to_evo(est)
    e.align(to_evo(gt), correct_scale=False)  # SE(3) Umeyama, same as align="se3"
    ape.process_data((to_evo(gt), e))
    evo_rmse = ape.get_statistic(evo_metrics.StatisticsType.rmse)
    assert ate(est, gt, align="se3")["rmse"] == pytest.approx(evo_rmse, rel=1e-6)


def _best_posyaw_rmse(est_p: np.ndarray, gt_p: np.ndarray) -> float:
    """Independent oracle: brute-force the yaw angle on a fine grid, with the
    translation fixed by matching centroids (optimal for any fixed rotation).
    Shares no code with yaw_align, so it cannot repeat its mistakes."""
    a, b = est_p - est_p.mean(0), gt_p - gt_p.mean(0)
    best = np.inf
    for th in np.linspace(-np.pi, np.pi, 7201):  # 0.05 degree steps
        c, s = np.cos(th), np.sin(th)
        rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        best = min(best, float(np.sqrt(np.mean(np.sum((a @ rot.T - b) ** 2, axis=1)))))
    return best


def test_ate_posyaw_does_not_absorb_gravity_tilt():
    """A 5 degree roll is a real VIO error because gravity is observable, so
    posyaw must report it. A looser alignment (se3) would hide it."""
    gt = make_traj()
    tilt = Rotation.from_euler("x", 5, degrees=True).as_matrix()
    est = transform(gt, tilt, np.array([1.0, 2.0, 0.0]))
    assert ate(est, gt, align="se3")["rmse"] < 1e-9  # a pure rigid change: fully absorbable
    posyaw = ate(est, gt, align="posyaw")["rmse"]
    assert posyaw > 0.01  # tilt survives alignment (guards against a silent switch to se3)
    assert posyaw == pytest.approx(_best_posyaw_rmse(est.p, gt.p), rel=1e-3)


def _line(n: int, speed: float) -> Trajectory:
    """Straight line along x, identity orientation. Time step 0.25 s is exact in
    binary floating point, so a 1.0 s window is exactly 4 samples."""
    t = np.arange(n) * 0.25
    p = np.column_stack([speed * t, np.zeros(n), np.zeros(n)])
    return Trajectory(t, p, np.tile([0.0, 0.0, 0.0, 1.0], (n, 1)))


def test_rpe_equals_known_constant_drift():
    """Estimate moves at 1.03 m/s, truth at 1.00 m/s. Over every 1 s window the
    relative translation differs by exactly 0.03 m, so rmse = mean = max = 0.03.
    Hand derivation, no shared code: error = (v_est - v_gt) * delta_s."""
    n = 200
    out = rpe(_line(n, 1.03), _line(n, 1.00), delta_s=1.0)
    assert out["ok"]
    assert out["n"] == n - 4  # the last 4 samples have no partner 1 s later
    for key in ("rmse", "mean", "max"):
        assert out[key] == pytest.approx(0.03, abs=1e-12)


def test_rpe_ignores_constant_position_offset_but_not_drift():
    """RPE is blind to a fixed offset (relative motion is unchanged) and must
    not be blind to drift. Together with the test above this stops an
    always-zero rpe() from passing the alignment-free test."""
    gt = _line(200, 1.0)
    offset = Trajectory(gt.t, gt.p + np.array([0.5, -0.2, 0.1]), gt.q)
    assert rpe(offset, gt)["rmse"] < 1e-12
    assert rpe(_line(200, 1.01), gt)["rmse"] > 1e-3
