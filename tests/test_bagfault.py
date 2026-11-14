"""Bag-level fault injection (S6), tested on fake rosbag-like messages (no ROS on the host).

vioreg.bagfault runs inside the pinned image (Python 3.8, numpy, rosbag); here its pure core
is exercised with duck-typed messages: Image (header.stamp, height, width, step, encoding,
data) and Imu (header.stamp, angular_velocity, linear_acceleration).
"""
import copy
from types import SimpleNamespace as NS

import numpy as np
import pytest

from vioreg import bagfault as bf

CAM0, CAM1, IMU, LEICA = "/cam0/image_raw", "/cam1/image_raw", "/imu0", "/leica/position"
T0 = 1_403_636_579_000_000_000


class FakeTime:
    def __init__(self, ns):
        self.ns = int(ns)

    def to_nsec(self):
        return self.ns

    def __eq__(self, other):
        return isinstance(other, FakeTime) and other.ns == self.ns

    def __repr__(self):
        return f"FakeTime({self.ns})"


def image(stamp_ns, value=100, h=6, w=8, pad=0, seed=None):
    rng = np.random.default_rng(seed) if seed is not None else None
    px = rng.integers(0, 256, (h, w), dtype=np.uint8) if rng is not None else \
        np.full((h, w), value, np.uint8)
    rows = np.zeros((h, w + pad), np.uint8)
    rows[:, :w] = px
    return NS(header=NS(stamp=FakeTime(stamp_ns), frame_id="cam0"), height=h, width=w,
              step=w + pad, encoding="mono8", is_bigendian=0, data=rows.tobytes())


def imu(stamp_ns, w=(0.1, 0.2, 0.3), a=(0.0, 0.0, 9.81)):
    return NS(header=NS(stamp=FakeTime(stamp_ns), frame_id="imu4"),
              angular_velocity=NS(x=w[0], y=w[1], z=w[2]),
              linear_acceleration=NS(x=a[0], y=a[1], z=a[2]))


def fake_bag(n_frames=6, imu_per_frame=10, seed=0):
    """(topic, msg, record_time) in record order, like rosbag.read_messages. Record times
    differ from header stamps, as in the real EuRoC bag."""
    recs = []
    for i in range(n_frames):
        stamp = T0 + i * 50_000_000
        for j in range(imu_per_frame):
            s = stamp + j * 5_000_000
            recs.append((IMU, imu(s, w=(0.01 * j, 0.0, 0.0)), FakeTime(s + 51_000)))
        recs.append((CAM0, image(stamp, seed=seed + i), FakeTime(stamp + 47_000_000)))
        recs.append((CAM1, image(stamp, seed=seed + 100 + i), FakeTime(stamp + 48_000_000)))
        recs.append((LEICA, NS(header=NS(stamp=FakeTime(stamp)), point=NS(x=1, y=2, z=3)),
                     FakeTime(stamp + 1)))
    return recs


def run(fault, params, seed=0, recs=None):
    recs = copy.deepcopy(recs) if recs is not None else fake_bag()  # rosbag yields fresh msgs
    orig = copy.deepcopy(recs)
    out = []
    bf.rewrite(lambda topics=None: iter([r for r in recs if topics is None or r[0] in topics]),
               lambda t, m, rt: out.append((t, m, rt)), fault, params, seed, make_time=FakeTime)
    return orig, out


def px(msg):
    return np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.step)[:, :msg.width]


# ---------------------------------------------------------------- time-offset
@pytest.mark.parametrize("offset_ms", [1.0, 5.0, 20.0, -5.0, 0.25])
def test_time_offset_shifts_only_camera_stamps_by_exactly_the_offset(offset_ms):
    orig, out = run("time-offset", {"offset_ms": offset_ms})
    want = int(round(offset_ms * 1e6))
    assert len(out) == len(orig)
    for (t0, m0, r0), (t1, m1, r1) in zip(orig, out):
        assert t1 == t0 and r1 == r0, "topic and record time never change"
        d = m1.header.stamp.to_nsec() - m0.header.stamp.to_nsec()
        if t0 in (CAM0, CAM1):
            assert d == want, (t0, d)
            assert m1.data == m0.data
        else:
            assert d == 0, (t0, d)
            if t0 == IMU:
                assert vars(m1.angular_velocity) == vars(m0.angular_velocity)
                assert vars(m1.linear_acceleration) == vars(m0.linear_acceleration)


# ---------------------------------------------------------------- imu-noise
def test_imu_noise_changes_only_imu_values_and_is_deterministic():
    recs = fake_bag(n_frames=40, imu_per_frame=50)
    orig, a = run("imu-noise", {"sigma_w": 0.01, "sigma_a": 0.2}, seed=3, recs=recs)
    _, b = run("imu-noise", {"sigma_w": 0.01, "sigma_a": 0.2}, seed=3, recs=recs)
    _, c = run("imu-noise", {"sigma_w": 0.01, "sigma_a": 0.2}, seed=4, recs=recs)
    dw, da = [], []
    for (t0, m0, r0), (t1, m1, r1), (_, mb, _), (_, mc, _) in zip(orig, a, b, c):
        assert t1 == t0 and r1 == r0
        assert m1.header.stamp == m0.header.stamp
        if t0 == IMU:
            assert vars(m1.angular_velocity) == vars(mb.angular_velocity), "same seed, same noise"
            assert vars(m1.angular_velocity) != vars(mc.angular_velocity), "other seed differs"
            dw += [getattr(m1.angular_velocity, k) - getattr(m0.angular_velocity, k) for k in "xyz"]
            da += [getattr(m1.linear_acceleration, k) - getattr(m0.linear_acceleration, k)
                   for k in "xyz"]
        elif t0 in (CAM0, CAM1):
            assert m1.data == m0.data
    assert np.std(dw) == pytest.approx(0.01, rel=0.1)
    assert np.std(da) == pytest.approx(0.2, rel=0.1)


# ---------------------------------------------------------------- gamma / blur
def test_gamma_changes_pixels_of_both_cameras_only():
    orig, out = run("gamma", {"gamma": 2.2})
    for (t0, m0, r0), (t1, m1, r1) in zip(orig, out):
        assert r1 == r0 and m1.header.stamp == m0.header.stamp
        if t0 in (CAM0, CAM1):
            assert np.array_equal(px(m1), bf.faults.apply_gamma(px(m0), 2.2))
            assert px(m1).mean() < px(m0).mean()
        elif t0 == IMU:
            assert vars(m1.angular_velocity) == vars(m0.angular_velocity)


def test_blur_is_applied_per_row_and_keeps_row_padding():
    recs = [(CAM0, image(T0, seed=1, pad=4), FakeTime(T0)),
            (CAM1, image(T0, seed=2, pad=4), FakeTime(T0 + 1))]
    orig, out = run("blur", {"length": 9}, recs=recs)
    for (_, m0, _), (_, m1, _) in zip(orig, out):
        assert (m1.height, m1.width, m1.step, len(m1.data)) == (m0.height, m0.width, m0.step,
                                                                len(m0.data))
        assert np.array_equal(px(m1), bf.faults.motion_blur(px(m0), 9))


def test_non_mono8_image_is_refused():
    recs = fake_bag(n_frames=1)
    recs[recs.index(next(r for r in recs if r[0] == CAM0))][1].encoding = "bgr8"
    with pytest.raises(bf.BagFaultError, match="mono8"):
        run("gamma", {"gamma": 2.2}, recs=recs)


# ---------------------------------------------------------------- drop-frames
def test_drop_frames_keeps_every_kth_stereo_pair_and_nothing_else_changes():
    orig, out = run("drop-frames", {"keep_every": 2})
    cams = lambda rs, t: [m.header.stamp.to_nsec() for tt, m, _ in rs if tt == t]
    all0 = cams(orig, CAM0)
    assert cams(out, CAM0) == all0[::2]
    assert cams(out, CAM1) == all0[::2], "stereo pairs stay together"
    assert [r for r in out if r[0] not in (CAM0, CAM1)] == \
        [r for r in orig if r[0] not in (CAM0, CAM1)], "IMU and leica records exactly unchanged"


def test_drop_frames_without_stereo_partner_is_refused():
    recs = [r for r in fake_bag(n_frames=3) if not (r[0] == CAM1 and r[1].header.stamp.ns == T0)]
    with pytest.raises(bf.BagFaultError, match="stereo"):
        run("drop-frames", {"keep_every": 2}, recs=recs)


# ---------------------------------------------------------------- identity + compare
def test_identity_changes_nothing():
    orig, out = run("identity", {})
    assert len(out) == len(orig)
    for (t0, m0, r0), (t1, m1, r1) in zip(orig, out):
        assert (t1, r1) == (t0, r0) and m1.header.stamp == m0.header.stamp
        if t0 in (CAM0, CAM1):
            assert m1.data == m0.data


def test_compare_reports_counts_and_exact_camera_shift():
    orig, out = run("time-offset", {"offset_ms": 5.0})
    s = bf.compare(iter(orig), iter(out))
    assert s["counts_equal"] and s["record_times_equal"]
    assert s["topics"][CAM0]["stamp_shift_ns"] == {"min": 5_000_000, "max": 5_000_000}
    assert s["topics"][CAM1]["stamp_shift_ns"] == {"min": 5_000_000, "max": 5_000_000}
    assert s["topics"][IMU]["stamp_shift_ns"] == {"min": 0, "max": 0}
    assert s["topics"][IMU]["values_changed"] == 0


def test_compare_counts_dropped_frames():
    orig, out = run("drop-frames", {"keep_every": 2})
    s = bf.compare(iter(orig), iter(out))
    assert not s["counts_equal"]
    assert s["topics"][CAM0]["count"] == [6, 3] and s["topics"][IMU]["count"] == [60, 60]


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("fault, params", [
    ("gamma", {"gamma": 0}), ("gamma", {"gamma": -1}), ("gamma", {}),
    ("blur", {"length": 4}), ("blur", {"length": 1}), ("blur", {"length": 101}),
    ("drop-frames", {"keep_every": 1}), ("drop-frames", {"keep_every": 2.5}),
    ("time-offset", {"offset_ms": 0}), ("time-offset", {"offset_ms": 5000}),
    ("imu-noise", {"sigma_w": -0.1, "sigma_a": 0.2}), ("imu-noise", {"sigma_w": 0.1}),
    ("gamma", {"gamma": 2.2, "extra": 1}), ("nonsense", {}),
])
def test_bad_parameters_are_refused(fault, params):
    with pytest.raises(bf.BagFaultError):
        bf.validate(fault, params)


def test_module_is_python38_and_numpy_only():
    """It runs inside the pinned image: Python 3.8, numpy 1.17, no scipy."""
    import ast
    from pathlib import Path
    for mod in ("bagfault", "faults"):
        src = (Path(bf.__file__).parent / f"{mod}.py").read_text()
        tree = ast.parse(src, feature_version=(3, 8))
        imported = {a.name.split(".")[0] for n in ast.walk(tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
        assert "scipy" not in imported, mod
        assert imported <= {"__future__", "numpy", "json", "argparse", "sys", "copy",
                            "math", "", "faults", "rosbag", "rospy", "genpy"}, (mod, imported)
