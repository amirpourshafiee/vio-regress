import numpy as np
import pytest

from vioreg.faults import add_imu_noise, apply_gamma, drop_frames, motion_blur, shift_cam_time
from vioreg.windows import pick_windows


def test_imu_noise_has_requested_sigma_and_leaves_time_alone():
    imu = np.zeros((20000, 7))
    imu[:, 0] = np.arange(20000) * 5_000_000
    out = add_imu_noise(imu, sigma_w=0.01, sigma_a=0.2, seed=1)
    assert np.array_equal(out[:, 0], imu[:, 0])
    assert out[:, 1:4].std() == pytest.approx(0.01, rel=0.05)
    assert out[:, 4:7].std() == pytest.approx(0.2, rel=0.05)
    assert np.all(imu[:, 1:] == 0)  # input untouched


def test_gamma_darkens_and_is_monotonic():
    img = np.tile(np.arange(256, dtype=np.uint8), (8, 1))
    dark = apply_gamma(img, 2.2)
    assert dark.mean() < img.mean() and np.all(np.diff(dark[0].astype(int)) >= 0)


def test_blur_reduces_gradient_energy():
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (48, 64), dtype=np.uint8)
    g = lambda a: np.abs(np.diff(a.astype(float), axis=1)).mean()
    assert g(motion_blur(img, 9)) < 0.5 * g(img)


def test_time_shift_and_drop():
    assert shift_cam_time(np.array([1_000_000_000]), 5.0)[0] == 1_005_000_000
    assert drop_frames(list(range(10)), 2) == [0, 2, 4, 6, 8]


def test_pick_windows_finds_the_spike_and_a_quiet_control():
    t = np.arange(0, 60, 0.05)
    err = np.full_like(t, 0.05)
    err[(t >= 31) & (t < 34)] = 0.9  # failure burst inside window [30, 35)
    rows = pick_windows(t, err, win_s=5.0, top_k=1, control_k=1)
    worst = [r for r in rows if r["source"] == "natural"][0]
    ctrl = [r for r in rows if r["source"] == "control"][0]
    assert worst["t0"] == 30.0 and worst["peak_err_m"] == pytest.approx(0.9)
    assert ctrl["peak_err_m"] == pytest.approx(0.05)
