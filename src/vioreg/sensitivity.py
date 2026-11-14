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


def analyse(root: Path, seqs, data_root: Path, thresholds: dict, s3_baseline: dict,
            s3_rpe_baseline: dict | None = None) -> dict:
    out = {"s3_baseline_ate_m": s3_baseline, "s3_baseline_rpe_m": s3_rpe_baseline,
           "sequences": {}}
    variants = sorted(p.name for p in Path(root).iterdir() if p.is_dir() and p.name not in
                      ("configs", "logs"))
    for seq in seqs:
        align, skip_s = variance._limits(thresholds, seq)
        base = float(s3_baseline[seq])
        rbase = None if not s3_rpe_baseline else float(s3_rpe_baseline[seq])
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
                      "distinct_files": s["distinct_files"],
                      "per_run_ate_m": [r["ate_rmse"] for r in runs],
                      "ate_median_m": med, "ate_range_m": s["ate_rmse"]["range"],
                      "rpe_median_m": s["rpe_rmse"]["median"],
                      "per_run_rpe_m": [r["rpe_rmse"] for r in runs],
                      "coverage_min": s["coverage_min"],
                      "delta_m": None if med is None else med - base,
                      "delta_pct": None if med is None else 100.0 * (med - base) / base,
                      "wall_median_s": s["wall_s"]["median"]}

        def span(groups):
            inc = [v for v, d in per.items() if d["group"] in groups]
            vals = [x for v in inc for x in per[v]["per_run_ate_m"] if x is not None]
            return _span(vals, base, inc, sum(per[v]["failed"] for v in inc))

        out["sequences"][seq] = {"s3_baseline_ate_m": base, "s3_baseline_rpe_m": rbase,
                                 "variants": per,
                                 "legitimate": span(LEGITIMATE),
                                 "realtime": span({"realtime_rate", "realtime_load"})}
    return out


# ---------------------------------------------------------------- per-knob + cross-sequence
def _knob(variant: str) -> tuple[str, str]:
    if variant == "base":
        return "baseline", "-"
    m = re.match(r"^(.*)_([\d.]+)$", variant)
    return (m.group(1), m.group(2)) if m else (variant, "")


def _ratio(v, base):
    return None if v is None or not base else float(v) / float(base)


def knob_rows(analysis: dict) -> list[dict]:
    """One row per variant: ATE and RPE median as percent of each sequence's S3 baseline."""
    seqs = list(analysis["sequences"])
    names = sorted({v for s in seqs for v in analysis["sequences"][s]["variants"]},
                   key=lambda v: ([g for _, g in GROUPS].index(group_of(v)), v))
    rows = []
    for v in names:
        knob, value = _knob(v)
        row = {"variant": v, "group": group_of(v), "knob": knob, "value": value,
               "ate_pct": {}, "rpe_pct": {}}
        for s in seqs:
            sd = analysis["sequences"][s]
            d = sd["variants"].get(v)
            a = _ratio(d and d["ate_median_m"], sd["s3_baseline_ate_m"])
            r = _ratio(d and d["rpe_median_m"], sd.get("s3_baseline_rpe_m"))
            row["ate_pct"][s] = None if a is None else 100.0 * a
            row["rpe_pct"][s] = None if r is None else 100.0 * r
        rows.append(row)
    return rows


# Variants with the same MEANING on both sequences. bag_start values differ per sequence
# (MH_01 38/42 around 40; V1_01 1/2 above 0); only "+2 s" exists on both.
SPECIAL_PAIRS = {"bag_start_+2s": {"MH_01_easy": "bag_start_42", "V1_01_easy": "bag_start_2"}}


def cross_sequence(analysis: dict, seqs: list[str]) -> dict:
    """Geometric mean, across sequences, of each variant's ATE and RPE ratio to baseline."""
    sq = analysis["sequences"]
    common = set.intersection(*(set(sq[s]["variants"]) for s in seqs))
    common = {v for v in common if group_of(v) != "replay_offset"}
    pairs_def = {v: {s: v for s in seqs} for v in common}
    for name, members in SPECIAL_PAIRS.items():
        if set(members) == set(seqs) and all(members[s] in sq[s]["variants"] for s in seqs):
            pairs_def[name] = dict(members)
    pairs = []
    for name, members in sorted(pairs_def.items()):
        ds = [sq[s]["variants"][members[s]] for s in seqs]
        ar = [_ratio(d["ate_median_m"], sq[s]["s3_baseline_ate_m"]) for s, d in zip(seqs, ds)]
        rr = [_ratio(d["rpe_median_m"], sq[s].get("s3_baseline_rpe_m")) for s, d in zip(seqs, ds)]
        gm = lambda xs: None if any(x is None for x in xs) else float(np.exp(np.mean(np.log(xs))))  # noqa: E731
        pairs.append({"name": name, "group": group_of(members[seqs[0]]), "members": members,
                      "ate_ratios": dict(zip(seqs, ar)), "rpe_ratios": dict(zip(seqs, rr)),
                      "ate_geomean": gm(ar), "rpe_geomean": gm(rr),
                      "runs": sum(d["runs"] for d in ds), "failed": sum(d["failed"] for d in ds)})

    def span(groups):
        inc = [p for p in pairs if p["group"] in groups and p["ate_geomean"] is not None]
        a = [p["ate_geomean"] for p in inc]
        r = [p["rpe_geomean"] for p in inc if p["rpe_geomean"] is not None]
        return {"included": [p["name"] for p in inc], "variants": len(inc),
                "runs": sum(p["runs"] for p in inc),
                "ate_min": min(a, default=None), "ate_max": max(a, default=None),
                "rpe_min": min(r, default=None), "rpe_max": max(r, default=None)}

    return {"sequences": seqs, "pairs": pairs, "legitimate": span(LEGITIMATE),
            "realtime": span({"realtime_rate", "realtime_load"})}


def single_sequence_ratio_span(analysis: dict, seq: str, groups=LEGITIMATE) -> dict:
    """Min/max of the per-variant MEDIAN ratio on one sequence (comparable with the
    cross-sequence geometric mean, which also uses per-variant medians)."""
    sd = analysis["sequences"][seq]
    vs = {v: d for v, d in sd["variants"].items() if d["group"] in groups}
    a = [_ratio(d["ate_median_m"], sd["s3_baseline_ate_m"]) for d in vs.values()]
    r = [_ratio(d["rpe_median_m"], sd.get("s3_baseline_rpe_m")) for d in vs.values()]
    a, r = [x for x in a if x is not None], [x for x in r if x is not None]
    return {"variants": len(vs), "runs": sum(d["runs"] for d in vs.values()),
            "ate_min": min(a), "ate_max": max(a), "rpe_min": min(r, default=None),
            "rpe_max": max(r, default=None)}


def markdown_tables(analysis: dict, seqs: list[str]) -> str:
    pct = lambda x: "-" if x is None else f"{x:.1f}%"  # noqa: E731
    rows = knob_rows(analysis)
    head = " | ".join(f"{s} ATE" for s in seqs) + " | " + " | ".join(f"{s} RPE" for s in seqs)
    out = [f"| Knob | Value | {head} |", "|---|---|" + "---|" * (2 * len(seqs))]
    for r in rows:
        cells = [pct(r["ate_pct"][s]) for s in seqs] + [pct(r["rpe_pct"][s]) for s in seqs]
        label = r["knob"] if r["group"] not in ("realtime_rate", "realtime_load") else f"*{r['knob']}*"
        out.append(f"| {label} | {r['value']} | " + " | ".join(cells) + " |")
    x = cross_sequence(analysis, seqs)
    out += ["", "| Variant (both sequences) | Group | ATE ratios | ATE geomean | RPE ratios | RPE geomean |",
            "|---|---|---|---|---|---|"]
    for p in sorted(x["pairs"], key=lambda p: ([g for _, g in GROUPS].index(p["group"]), p["name"])):
        name = p["name"] + (" (" + ", ".join(f"{s.split('_')[0]}_{s.split('_')[1]}: {m}"
                                             for s, m in p["members"].items()) + ")"
                            if p["name"] in SPECIAL_PAIRS else "")
        ar = " / ".join(f"{p['ate_ratios'][s]:.3f}" for s in seqs)
        rr = " / ".join(f"{p['rpe_ratios'][s]:.3f}" for s in seqs)
        out.append(f"| {name} | {p['group']} | {ar} | {p['ate_geomean']:.3f} | {rr} | {p['rpe_geomean']:.3f} |")
    return "\n".join(out)


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
    ap.add_argument("--markdown", type=Path, default=None,
                    help="also write the per-knob and cross-sequence tables as markdown")
    a = ap.parse_args(argv)

    b = json.loads(a.s3_baseline.read_text())["sequences"]
    s3 = {s: float(np.median([x for x in b[s]["per_run_ate_rmse"] if x is not None]))
          for s in a.seqs}
    s3r = {s: float(np.median([x for x in b[s]["per_run_rpe_rmse"] if x is not None]))
           for s in a.seqs}
    out = analyse(a.root, a.seqs, a.data, yaml.safe_load(a.thresholds.read_text()), s3, s3r)
    out["cross_sequence"] = cross_sequence(out, a.seqs)
    out["single_sequence_median_ratio_span"] = {s: single_sequence_ratio_span(out, s)
                                                for s in a.seqs}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    if a.markdown:
        a.markdown.write_text(markdown_tables(out, a.seqs) + "\n")
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
