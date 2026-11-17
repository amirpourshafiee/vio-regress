"""Pick failure windows and control windows from a per-pose error series (S7).

Pure, host-side logic. Frames come from vioreg.bagframes (inside the pinned image); this
module only chooses windows, picks frame times, lays out contact sheets and writes the CSVs.

Leak rules (guide 8.3: label blind): the labeling UI must not learn the sequence, the error
or the fault. So window ids are salted hashes, sheet file names are the window id, and the
UI reads ONLY labels/windows_ui.csv (window_id, sheet). labels/windows.csv keeps the
metadata for analysis and never has a fault column; the fault stays in fault_manifest.csv.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import numpy as np

from .euroc_io import Trajectory
from .metrics import ate

N_FRAMES = 12
SOURCES = ("natural", "injected", "control")
META_FIELDS = ["window_id", "sequence", "run_id", "source", "t0", "t1", "peak_err_m", "sheet"]
UI_FIELDS = ["window_id", "sheet"]
ID_SALT = "vioreg-windows-v1"
# Seconds dropped after each run's first pose before windows are cut: the estimator's
# start-up transient (S7 Stage 1, MH_01_easy: most "worst" windows were the first 40 s).
# NOT the gate's skip_s, which counts from ground-truth start and removes no poses on the
# gated sequences (docs/crosscheck.md); the two are different quantities.
TRIM_START_S = 15.0
# Minimum time between an injected window and any natural or control window of the same
# sequence (one window length). Without it, adjacent windows showed the same scene clean and
# faulted (S7 Stage 1, MH_01_easy: gamma 89.7 s right after control 84.7-89.7 s).
INJ_GAP_S = 5.0


def rank_windows(t: np.ndarray, err: np.ndarray, win_s: float = 5.0) -> list[dict]:
    """Consecutive win_s windows (5+ poses each), worst peak error first."""
    edges = np.arange(t[0], t[-1], win_s)
    rows = []
    for a in edges:
        m = (t >= a) & (t < a + win_s)
        if m.sum() < 5:
            continue
        rows.append({"t0": float(a), "t1": float(a + win_s), "peak_err_m": float(err[m].max())})
    rows.sort(key=lambda r: r["peak_err_m"], reverse=True)
    return rows


def overlaps(t0: float, t1: float, avoid, gap_s: float = 0.0) -> bool:
    """[t0, t1) overlaps any (a0, a1) in avoid, or comes closer to one than gap_s. With
    gap_s=0, windows that only touch do not overlap; a distance of exactly gap_s is allowed."""
    return any(t0 < a1 + gap_s and a0 - gap_s < t1 for a0, a1 in avoid)


def pick_windows(t: np.ndarray, err: np.ndarray, win_s: float = 5.0,
                 top_k: int = 10, control_k: int = 5) -> list[dict]:
    """Split the run into consecutive win_s windows, rank by peak error.

    Returns the top_k worst windows (source='natural') plus control_k of the best
    windows (source='control'). Controls matter: a judge that always says
    'there is a visual problem' must be caught by windows where nothing went wrong.
    """
    rows = rank_windows(t, err, win_s)
    worst = [dict(r, source="natural") for r in rows[:top_k]]
    best = [dict(r, source="control") for r in rows[::-1][:control_k]]
    return worst + best


def trim_start(t: np.ndarray, err: np.ndarray, trim_s: float = TRIM_START_S):
    """Drop the first trim_s seconds counted from the run's first pose (t[0] of its own
    error series), not from ground-truth start."""
    if trim_s <= 0 or len(t) == 0:
        return t, err
    keep = t >= t[0] + trim_s
    return t[keep], err[keep]


def error_series(est: Trajectory, gt: Trajectory, align: str = "posyaw",
                 skip_s: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """(t, err) per associated pose: the same ATE error the gate scores, with its timestamps.

    score.py's err.npy has no time axis, so windows recompute it from the run itself."""
    a = ate(est, gt, align=align, skip_s=skip_s)
    if not a.get("ok"):
        raise ValueError(f"ATE not ok: {a.get('reason')}")
    return np.asarray(a["t"], dtype=float), np.asarray(a["per_pose"], dtype=float)


def window_id(run_id: str, t0: float) -> str:
    """Opaque 10-hex id; carries no sequence, fault or error information."""
    return hashlib.sha1(f"{ID_SALT}|{run_id}|{t0:.3f}".encode()).hexdigest()[:10]


def public_run_id(run_id: str, sequence: str, injected: bool) -> str:
    """run_id as written to windows.csv. A faulted run's id embeds the fault and its parameters
    (e.g. MH_01_easy__blur_length9), so it is replaced by an opaque key; the mapping back is
    fault_manifest.csv, which the labeling UI never reads."""
    if not injected:
        return run_id
    return f"{sequence}__inj-{hashlib.sha1(f'{ID_SALT}|{run_id}'.encode()).hexdigest()[:6]}"


def frame_times(t0: float, t1: float, n: int = N_FRAMES) -> np.ndarray:
    """n evenly spaced timestamps from t0 up to (not including) t1."""
    return np.linspace(t0, t1, n, endpoint=False)


def run_windows(run_id: str, sequence: str, t: np.ndarray, err: np.ndarray, *,
                injected: bool, win_s: float = 5.0, top_k: int = 8, control_k: int = 5,
                avoid=(), gap_s: float = 0.0) -> list[dict]:
    """Windows of one run as metadata rows (no sheet yet).

    Natural runs give top_k worst + control_k best. Injected runs give top_k worst only,
    labelled 'injected': a faulted run's best windows are not a clean control. With `avoid`
    ((t0, t1) intervals: the natural and control windows of the same sequence), an injected
    run skips every window that overlaps one of them or lies closer than gap_s, and takes the
    next-worst, so a labeler never sees the same moment clean and faulted. Fewer than top_k may remain; the caller reports that."""
    if injected and avoid:
        rows = [dict(r, source="natural") for r in rank_windows(t, err, win_s)
                if not overlaps(r["t0"], r["t1"], avoid, gap_s)][:top_k]
    else:
        rows = pick_windows(t, err, win_s, top_k, 0 if injected else control_k)
    out = []
    for r in rows:
        src = "injected" if injected and r["source"] == "natural" else r["source"]
        out.append({"window_id": window_id(run_id, r["t0"]), "sequence": sequence,
                    "run_id": public_run_id(run_id, sequence, injected), "source": src, "t0": r["t0"], "t1": r["t1"],
                    "peak_err_m": r["peak_err_m"], "sheet": ""})
    return out


def contact_sheet(frames: list[np.ndarray], cols: int = 4, tile_w: int = 188):
    """Grid of grayscale frames (row-major, time order), each resized to tile_w wide.

    Returns a PIL image. Deterministic: same frames in, same pixels out."""
    from PIL import Image  # local import: pillow is only needed where sheets are built
    if not frames:
        raise ValueError("no frames")
    h0, w0 = frames[0].shape
    tile_h = int(round(tile_w * h0 / w0))
    rows = -(-len(frames) // cols)
    sheet = Image.new("L", (cols * tile_w, rows * tile_h), 0)
    for i, f in enumerate(frames):
        if f.shape != (h0, w0):
            raise ValueError("frames differ in size")
        tile = Image.fromarray(np.ascontiguousarray(f, dtype=np.uint8)).resize(
            (tile_w, tile_h), Image.BILINEAR)
        sheet.paste(tile, ((i % cols) * tile_w, (i // cols) * tile_h))
    return sheet


def check_set(rows: list[dict], min_rows: int = 150, ctrl_lo: float = 0.15,
              ctrl_hi: float = 0.30) -> list[str]:
    """Warnings about the finished set (guide 8.2: 150+ windows, about 20-25 percent controls)."""
    out = []
    if len(rows) < min_rows:
        out.append(f"only {len(rows)} windows, the guide targets {min_rows}+")
    if rows:
        f = sum(r["source"] == "control" for r in rows) / len(rows)
        if not ctrl_lo <= f <= ctrl_hi:
            out.append(f"controls are {f:.0%} of windows, the guide targets about 20-25%")
    ids = [r["window_id"] for r in rows]
    if len(set(ids)) != len(ids):
        out.append("duplicate window ids")
    return out


def write_csvs(rows: list[dict], meta_path: Path, ui_path: Path) -> None:
    """windows.csv (metadata, no fault column) and windows_ui.csv (what the labeler reads)."""
    for p in (meta_path, ui_path):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_FIELDS, extrasaction="raise")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["window_id"]))
    with open(ui_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=UI_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r[k] for k in UI_FIELDS} for r in sorted(rows, key=lambda r: r["window_id"]))
