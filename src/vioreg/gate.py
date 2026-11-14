"""Gate logic (v2, session S4): results + configs/thresholds.yaml -> failures and warnings.

Design (docs/gate.md):
 1. Hard fail per sequence: missing/unparsable result, missing provenance, ate/rpe ok=false,
    any failed run, coverage below baseline - coverage_drop_max, associated pose count
    (ate.n) below baseline - pose_count_drop_max. The pose-count allowance (S4) exists only
    because the recorder can lose up to 2 poses at each end of an otherwise identical run
    (startup and shutdown races of ov_eval pose_to_file; poseimu publisher queue = 2).
 2. RPE RMSE per sequence as a ratio to baseline: fail above margins.rpe_ratio_max.
 3. ATE RMSE only as the GEOMETRIC MEAN of per-sequence ratios across every sequence in
    the thresholds file: fail above margins.ate_geomean_ratio_max. No single-sequence ATE
    gate (S3b: innocent changes move one sequence's ATE by up to +73%).
 4. When the result's (vio_commit, image_digest, config_hash) match the baseline, its
    trajectories are compared pose by pose with the baseline trajectory: a difference is a
    WARNING (behaviour changed under an identical setup), never a failure.
 5. A baseline is keyed by (sequence, vio_commit, image_digest, config_hash). No entry for
    the result's key is a FAILURE: "needs re-baseline".
Every check fails closed: anything missing or malformed is a failure, never a pass.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

KEY_FIELDS = ("vio_commit", "image_digest", "config_hash")
PROVENANCE = ("vio_commit", "image_digest", "harness_commit", "config_hash")


def threshold_from_baseline(samples, k: float = 3.0, rel_margin: float = 0.15,
                            floor: float = 0.01) -> float:
    """Robust limit from repeated baseline runs of the SAME code on the SAME data.

    Not used by the v2 gate: serial OpenVINS is bit-deterministic (S3), so MAD = 0 and the
    spread term contributes nothing; the limit collapses to median * (1 + rel_margin).
    """
    x = np.asarray(samples, dtype=float)
    if len(x) < 5:
        raise ValueError("need at least 5 baseline runs to set a threshold")
    med = float(np.median(x))
    mad = 1.4826 * float(np.median(np.abs(x - med)))
    return max(med + k * mad, med * (1.0 + rel_margin), floor)


# ---------------------------------------------------------------- helpers
def _short(v) -> str:
    v = str(v)
    return v[:15] if v.startswith("sha256:") else v[:8]


def _key_str(seq: str, key: dict) -> str:
    return f"({seq}, vio {_short(key.get('vio_commit'))}, image {_short(key.get('image_digest'))}, " \
           f"config {_short(key.get('config_hash'))})"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def margins(cfg: dict) -> dict:
    m = cfg.get("margins") or {}
    for k in ("rpe_ratio_max", "ate_geomean_ratio_max", "coverage_drop_max", "pose_count_drop_max"):
        if not _num(m.get(k)):
            raise ValueError(f"thresholds: margins.{k} missing or not a number")
    return m


def find_baseline(cfg: dict, seq: str, result: dict):
    """(entry, None) for the baseline matching the result's key, else (None, reason)."""
    sq = cfg.get("sequences", {}).get(seq)
    if sq is None:
        return None, f"{seq} not in thresholds"
    key = {k: result.get(k) for k in KEY_FIELDS}
    for e in sq.get("baselines") or []:
        if all(e.get(k) == key[k] for k in KEY_FIELDS):
            return e, None
    return None, f"needs re-baseline: no baseline for {_key_str(seq, key)}"


# ---------------------------------------------------------------- 1 + 2 + 5: per sequence
def evaluate_sequence(result: dict, cfg: dict, seq: str) -> list[str]:
    """Hard checks, baseline key, and the per-sequence RPE ratio. Empty list = pass."""
    if not isinstance(result, dict) or not result:
        return ["result is empty or not an object"]
    m = margins(cfg)
    missing = [k for k in PROVENANCE if not result.get(k)]
    if missing:
        return [f"invalid result: provenance missing ({', '.join(missing)})"]
    base, why = find_baseline(cfg, seq, result)
    if base is None:
        return [why]
    ate, rpe = result.get("ate") or {}, result.get("rpe") or {}
    fails = []
    if ate.get("ok") is not True or rpe.get("ok") is not True:
        fails.append(f"ok=false (ate.ok={ate.get('ok')}, rpe.ok={rpe.get('ok')}): "
                     "a run failed or the result is unusable")
    per = result.get("per_run_ate_rmse")
    if not isinstance(per, list) or not per or any(not _num(x) for x in per):
        n_bad = sum(not _num(x) for x in per) if isinstance(per, list) else "?"
        fails.append(f"{n_bad} failed run(s) of {result.get('runs')}")
    if fails:
        return fails
    for k in ("rmse", "coverage", "n"):
        if not _num(ate.get(k)):
            return [f"ate.{k} missing"]
    if not _num(rpe.get("rmse")):
        return ["rpe.rmse missing"]
    if ate["coverage"] < base["coverage"] - m["coverage_drop_max"]:
        fails.append(f"coverage {ate['coverage']:.3f} < baseline {base['coverage']:.3f} - "
                     f"{m['coverage_drop_max']:.3f} (tracking lost or estimator died)")
    if ate["n"] < base["n"] - m["pose_count_drop_max"]:
        fails.append(f"associated pose count {ate['n']} < baseline {base['n']} - "
                     f"{m['pose_count_drop_max']:g}")
    ratio = rpe["rmse"] / base["rpe_rmse"]
    if ratio > m["rpe_ratio_max"]:
        fails.append(f"RPE ratio {ratio:.3f} > {m['rpe_ratio_max']:.3f} "
                     f"(RPE {rpe['rmse'] * 1e3:.2f} mm vs baseline {base['rpe_rmse'] * 1e3:.2f} mm)")
    return fails


# ---------------------------------------------------------------- 3: aggregate ATE
def aggregate_ate_ratio(results: dict, cfg: dict) -> float:
    """Geometric mean over EVERY thresholds sequence of ate.rmse / baseline ate_rmse.
    Raises ValueError if any sequence cannot contribute."""
    logs = []
    for seq in sorted(cfg.get("sequences", {})):
        r = results.get(seq)
        if not isinstance(r, dict):
            raise ValueError(f"{seq}: missing result")
        base, why = find_baseline(cfg, seq, r)
        if base is None:
            raise ValueError(f"{seq}: {why}")
        a = (r.get("ate") or {})
        if a.get("ok") is not True or not _num(a.get("rmse")) or a["rmse"] <= 0:
            raise ValueError(f"{seq}: no usable ATE")
        logs.append(math.log(a["rmse"] / base["ate_rmse"]))
    if not logs:
        raise ValueError("no sequences in thresholds")
    return math.exp(sum(logs) / len(logs))


def evaluate_aggregate(results: dict, cfg: dict) -> list[str]:
    m = margins(cfg)
    try:
        g = aggregate_ate_ratio(results, cfg)
    except ValueError as e:
        return [f"aggregate ATE cannot be computed: {e}"]
    if g > m["ate_geomean_ratio_max"]:
        return [f"ATE geometric mean ratio {g:.3f} > {m['ate_geomean_ratio_max']:.3f} "
                f"across {len(cfg['sequences'])} sequences"]
    return []


# ---------------------------------------------------------------- 4: pose warning
def pose_warnings(results_dir: Path, seq: str, result: dict, cfg: dict,
                  root: Path) -> tuple[list[str], list[str]]:
    """(warnings, notes): a pose-level difference is a warning; a comparison that could not
    be made (no baseline trajectory, no run*.tum next to the result) is only a note."""
    from .score import find_runs
    from .variance import pose_equivalent

    base, _ = find_baseline(cfg, seq, result)
    if base is None:
        return [], []
    ref = base.get("trajectory")
    if not ref or not (Path(root) / ref).is_file():
        return [], [f"{seq}: pose comparison skipped (no baseline trajectory)"]
    runs = find_runs(Path(results_dir) / seq)
    if not runs:
        return [], [f"{seq}: pose comparison skipped (no run*.tum next to the result)"]
    out = []
    for k, path in runs:
        try:
            same = pose_equivalent(path, Path(root) / ref)
        except Exception:  # noqa: BLE001 - an unreadable run is already a hard failure
            same = False
        if not same:
            out.append(f"{seq}: run{k} trajectory differs from the baseline although "
                       f"vio_commit, image_digest and config_hash match "
                       "(behaviour changed: different host, harness or data?)")
    return out, []


def emit_warning(msg: str) -> None:
    """GitHub annotation in Actions, plain stderr elsewhere. Never fails anything."""
    import sys
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=vioreg gate::{msg}")
    else:
        print(f"WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- verdicts + scope (S5a)
VERDICTS = ("pass", "needs-rebaseline", "quality-fail", "missing", "invalid")


def classify(fails: list[str]) -> str:
    """One verdict per sequence, so a key mismatch is never reported as a quality failure.
    Precedence: missing > invalid > needs-rebaseline > quality-fail > pass."""
    if not fails:
        return "pass"
    if any(f.startswith(("missing result", "unparsable result", "result is empty")) for f in fails):
        return "missing"
    if any(f.startswith("invalid result") or "not in thresholds" in f for f in fails):
        return "invalid"
    if any(f.startswith("needs re-baseline") for f in fails):
        return "needs-rebaseline"
    return "quality-fail"


def parse_scope(text: str | None) -> list[str] | None:
    """HARNESS_SEQUENCES='MH_01_easy V1_01_easy' (spaces or commas). None/blank = all."""
    if not text or not text.strip():
        return None
    return [s for s in text.replace(",", " ").split() if s]


# ---------------------------------------------------------------- whole results directory
def load_result(path: Path):
    try:
        return json.loads(Path(path).read_text()), None
    except (OSError, ValueError) as e:
        return None, f"unparsable result {path}: {e}"


def evaluate_results_dir(results_dir: Path, cfg: dict, require: bool,
                         root: Path | None = None, scope: list[str] | None = None) -> dict:
    """Evaluate the thresholds sequences (or only `scope`, e.g. a one-sequence nightly).

    Returns {"sequences": {seq: [fails]}, "verdicts": {seq: verdict}, "aggregate": [fails],
    "aggregate_skipped": reason or "", "warnings": [...], "notes": [...], "skipped": [...],
    "passed": bool}. A scoped run judges only its sequences (missing ones still fail when
    `require`); the ATE geometric mean is defined over ALL baseline sequences (rule 3), so a
    partial scope SKIPS it with an explicit reason instead of computing a single-sequence ATE
    gate."""
    from .score import ROOT
    root = Path(root) if root is not None else ROOT
    rep = {"sequences": {}, "verdicts": {}, "aggregate": [], "aggregate_skipped": "",
           "warnings": [], "notes": [], "skipped": [], "passed": False}
    results = {}
    all_seqs = sorted(cfg.get("sequences", {}))
    seqs = all_seqs if scope is None else list(dict.fromkeys(scope))
    for seq in seqs:
        if seq not in cfg.get("sequences", {}):
            rep["sequences"][seq] = [f"{seq} not in thresholds"]
            continue
        f = Path(results_dir) / f"{seq}.json"
        if not f.exists():
            if require:
                rep["sequences"][seq] = [f"missing result file {f}; the runner did not produce it"]
            else:
                rep["skipped"].append(seq)
            results[seq] = None
            continue
        r, err = load_result(f)
        if err:
            rep["sequences"][seq] = [err]
            results[seq] = None
            continue
        results[seq] = r
        rep["sequences"][seq] = evaluate_sequence(r, cfg, seq)
        if not rep["sequences"][seq]:
            w, n = pose_warnings(results_dir, seq, r, cfg, root)
            rep["warnings"] += w
            rep["notes"] += n
    if scope is not None and set(seqs) != set(all_seqs):
        rep["aggregate_skipped"] = (
            f"ATE geometric mean needs all {len(all_seqs)} baseline sequences; this run covers "
            f"{len(set(seqs) & set(all_seqs))} of {len(all_seqs)} ({' '.join(seqs)})")
    elif not (rep["skipped"] and not require):
        rep["aggregate"] = evaluate_aggregate(results, cfg)
    rep["verdicts"] = {s: classify(f) for s, f in rep["sequences"].items()}
    rep["passed"] = (not any(rep["sequences"].values()) and not rep["aggregate"]
                     and (require or not rep["skipped"]))
    return rep
