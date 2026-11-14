"""S3b sensitivity analysis: how far ATE moves under legitimate one-at-a-time changes.

    python -m vioreg.sensitivity --root results/sensitivity --seqs MH_01_easy V1_01_easy \
        --out docs/sensitivity/sensitivity.json

Per variant x sequence: per-run ATE/RPE (via vioreg.variance, i.e. score._score_run), distinct
trajectories, failures, the variant's config_hash (from its score JSON), and the change of
the median ATE against the S3 serial baseline (docs/variance/baseline.json).

"Legitimate" = serial replay with one innocent change (start offset, a config value about
10% off, an innocent constant). Realtime replay is reported separately: S3 showed the replay
MODE alone moves MH_01 by about 31 mm, and the harness pins serial, so rate changes are not
something the gate will see. This module chooses no thresholds.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

from . import variance

GROUPS = [
    (r"^base$", "baseline"),
    (r"^bag_start_[\d.]+$", "replay_offset"),
    (r"^(num_pts|fast_threshold)_[\d.]+$", "config"),
    (r"^gravity_mag_[\d.]+$", "innocent"),
    (r"^rt_rate_[\d.]+$", "realtime_rate"),
    (r"^rt_load_rate_[\d.]+$", "realtime_load"),
]
LEGITIMATE = {"baseline", "replay_offset", "config", "innocent"}


class SensitivityError(RuntimeError):
    pass


def group_of(variant: str) -> str:
    for pat, g in GROUPS:
        if re.match(pat, variant):
            return g
    raise ValueError(f"unknown variant {variant!r}")


def _span(values: list[float], base: float, included: list[str], failed: int) -> dict:
    if not values:
        return {"min_m": None, "max_m": None, "range_m": None, "min_pct": None,
                "max_pct": None, "range_pct": None, "included": included, "failed_runs": failed,
                "variants": len(included)}
    lo, hi = float(min(values)), float(max(values))
    return {"min_m": lo, "max_m": hi, "range_m": hi - lo,
            "min_pct": 100.0 * (lo - base) / base, "max_pct": 100.0 * (hi - base) / base,
            "range_pct": 100.0 * (hi - lo) / base, "included": included,
            "failed_runs": failed, "variants": len(included)}


def analyse(root: Path, seqs, data_root: Path, thresholds: dict, s3_baseline: dict) -> dict:
    out = {"s3_baseline_ate_m": s3_baseline, "sequences": {}}
    variants = sorted(p.name for p in Path(root).iterdir() if p.is_dir() and p.name not in
                      ("configs", "logs"))
    for seq in seqs:
        align, skip_s = variance._limits(thresholds, seq)
        base = float(s3_baseline[seq])
        per = {}
        for v in variants:
            if not (Path(root) / v / seq).is_dir():
                continue
            res_f = Path(root) / v / f"{seq}.json"
            if not res_f.exists():
                raise SensitivityError(f"{res_f} missing: run score.py on this variant first")
            res = json.loads(res_f.read_text())
            runs = variance.collect_runs(Path(root) / v, seq, data_root, align, skip_s)
            s = variance.summarise(runs)
            med = s["ate_rmse"]["median"]
            per[v] = {"group": group_of(v), "config_hash": res.get("config_hash"),
                      "runs": s["runs"], "failed": s["failed"],
                      "failed_reasons": s["failed_reasons"],
                      "distinct_trajectories": s["distinct_trajectories"],
                      "per_run_ate_m": [r["ate_rmse"] for r in runs],
                      "ate_median_m": med, "ate_range_m": s["ate_rmse"]["range"],
                      "rpe_median_m": s["rpe_rmse"]["median"],
                      "coverage_min": s["coverage_min"],
                      "delta_m": None if med is None else med - base,
                      "delta_pct": None if med is None else 100.0 * (med - base) / base,
                      "wall_median_s": s["wall_s"]["median"]}

        def span(groups):
            inc = [v for v, d in per.items() if d["group"] in groups]
            vals = [x for v in inc for x in per[v]["per_run_ate_m"] if x is not None]
            return _span(vals, base, inc, sum(per[v]["failed"] for v in inc))

        out["sequences"][seq] = {"s3_baseline_ate_m": base, "variants": per,
                                 "legitimate": span(LEGITIMATE),
                                 "realtime": span({"realtime_rate", "realtime_load"})}
    return out


def main(argv=None) -> int:
    import yaml

    from . import score
    ap = argparse.ArgumentParser(description="Summarise the S3b sensitivity sweep.")
    ap.add_argument("--root", type=Path, default=Path("results/sensitivity"))
    ap.add_argument("--seqs", nargs="+", required=True)
    ap.add_argument("--data", type=Path, default=Path("data/euroc"))
    ap.add_argument("--thresholds", type=Path, default=score.ROOT / "configs" / "thresholds.yaml")
    ap.add_argument("--s3-baseline", type=Path, default=Path("docs/variance/baseline.json"))
    ap.add_argument("--out", type=Path, default=Path("docs/sensitivity/sensitivity.json"))
    a = ap.parse_args(argv)

    b = json.loads(a.s3_baseline.read_text())["sequences"]
    s3 = {s: float(np.median([x for x in b[s]["per_run_ate_rmse"] if x is not None]))
          for s in a.seqs}
    out = analyse(a.root, a.seqs, a.data, yaml.safe_load(a.thresholds.read_text()), s3)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    for seq, s in out["sequences"].items():
        print(f"\n{seq}  (S3 serial baseline {s['s3_baseline_ate_m'] * 1e3:.3f} mm)")
        for v, d in sorted(s["variants"].items(), key=lambda kv: (kv[1]["group"], kv[0])):
            med = d["ate_median_m"]
            print(f"  {d['group']:14s} {v:22s} runs={d['runs']} failed={d['failed']} "
                  f"distinct={d['distinct_trajectories']} ATE={'n/a' if med is None else f'{med*1e3:8.3f}'} mm "
                  f"delta={'n/a' if med is None else f'{d['delta_pct']:+6.2f}'}% cfg={str(d['config_hash'])[:8]} "
                  f"cov_min={d['coverage_min']}")
        for k in ("legitimate", "realtime"):
            g = s[k]
            if g["min_m"] is not None:
                print(f"  {k}: min {g['min_m']*1e3:.3f} mm ({g['min_pct']:+.2f}%), max {g['max_m']*1e3:.3f} mm "
                      f"({g['max_pct']:+.2f}%), range {g['range_m']*1e3:.3f} mm ({g['range_pct']:.2f}% of baseline), "
                      f"failed runs {g['failed_runs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
