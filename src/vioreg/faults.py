"""Fault injection. Always operate on a COPY of a sequence, never on the original data.

Each fault has a known cause, which makes failed runs useful as labeled test cases for
the gate. Report injected windows separately from natural ones: they are easier and
they are biased by construction.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d


def add_imu_noise(imu: np.ndarray, sigma_w: float, sigma_a: float, seed: int = 0):
    """imu columns: t_ns, wx, wy, wz, ax, ay, az (EuRoC imu0/data.csv order)."""
    rng = np.random.default_rng(seed)
    out = imu.copy()
    out[:, 1:4] += rng.normal(0.0, sigma_w, out[:, 1:4].shape)
    out[:, 4:7] += rng.normal(0.0, sigma_a, out[:, 4:7].shape)
    return out


def apply_gamma(img: np.ndarray, gamma: float) -> np.ndarray:
    """gamma > 1 darkens, gamma < 1 brightens. img is uint8."""
    x = (img.astype(np.float32) / 255.0) ** gamma
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


def motion_blur(img: np.ndarray, length: int) -> np.ndarray:
    """Horizontal box blur of `length` pixels."""
    return uniform_filter1d(img.astype(np.float32), size=length, axis=1,
                            mode="nearest").astype(np.uint8)


def shift_cam_time(cam_t_ns: np.ndarray, offset_ms: float) -> np.ndarray:
    """Simulate a camera-IMU time offset by shifting camera timestamps only."""
    return cam_t_ns + int(round(offset_ms * 1e6))


def drop_frames(items: list, keep_every: int) -> list:
    """Keep every k-th item. keep_every=2 halves the frame rate."""
    return items[::keep_every]
