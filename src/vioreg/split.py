"""Dev/test split of the labeled windows. Written once to labels/split.csv, never recomputed.

Guide 8.3, rule 6: stratified by label and source, 40 percent dev (the judge prompt may be
tuned on it), 60 percent test (touched once). The split file holds only window_id and split, so
the judge code can read it without seeing human labels or sources.

    python -m vioreg.split --windows labels/windows.csv --labels labels/human_v1.csv \
        --out labels/split.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path

DEV_FRACTION = 0.4
DEFAULT_SEED = 20261117


def _unique(rows, what):
    ids = [r["window_id"] for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"duplicate window_id in {what}: {dup[:3]}")
    return ids


def make_split(windows, labels, seed=DEFAULT_SEED, dev_fraction=DEV_FRACTION):
    """Return {window_id: "dev" | "test"}, stratified by (label, source).

    Each stratum gets floor(f * n) dev windows; the remaining dev slots go to the strata with
    the largest fractional parts (ties by stratum key), so the dev share is within one window
    of f * n overall and in every stratum. Which windows are dev is a seeded shuffle of sorted
    ids, so the result does not depend on input order.
    """
    w_ids = _unique(windows, "windows")
    l_ids = _unique(labels, "labels")
    extra = sorted(set(l_ids) - set(w_ids))
    if extra:
        raise ValueError(f"labeled window_id not in the window set: {extra[:3]}")
    missing = sorted(set(w_ids) - set(l_ids))
    if missing:
        raise ValueError(f"window_id missing a human label: {missing[:3]}")

    label = {r["window_id"]: r["label"] for r in labels}
    strata: dict[tuple, list] = {}
    for r in windows:
        strata.setdefault((label[r["window_id"]], r["source"]), []).append(r["window_id"])

    keys = sorted(strata)
    n_total = len(w_ids)
    base = {k: int(dev_fraction * len(strata[k])) for k in keys}
    frac = {k: dev_fraction * len(strata[k]) - base[k] for k in keys}
    target = round(dev_fraction * n_total)
    extra_slots = target - sum(base.values())
    for k in sorted(keys, key=lambda k: (-frac[k], k))[:max(extra_slots, 0)]:
        base[k] += 1

    rng = random.Random(seed)
    out = {}
    for k in keys:
        ids = sorted(strata[k])
        rng.shuffle(ids)
        for i, wid in enumerate(ids):
            out[wid] = "dev" if i < base[k] else "test"
    return out


def write_split(split, path):
    """Write window_id,split sorted by id. Refuses to overwrite: the split is frozen."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} exists; the split is frozen, never recomputed")
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["window_id", "split"])
        for wid in sorted(split):
            w.writerow([wid, split[wid]])


def read_split(path):
    with open(path, newline="") as f:
        return {r["window_id"]: r["split"] for r in csv.DictReader(f)}


def check_split(path, window_ids):
    """Problems (empty list when fine): a split must cover the window set exactly once."""
    split = read_split(path)
    problems = []
    ids = set(window_ids)
    for wid in sorted(set(split) - ids):
        problems.append(f"{wid}: in the split but not in the window set")
    for wid in sorted(ids - set(split)):
        problems.append(f"{wid}: missing from the split")
    bad = sorted({v for v in split.values()} - {"dev", "test"})
    if bad:
        problems.append(f"unknown split values: {bad}")
    return problems


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", required=True, help="labels/windows.csv (needs window_id, source)")
    ap.add_argument("--labels", required=True, help="labels/human_v1.csv (frozen)")
    ap.add_argument("--out", required=True, help="labels/split.csv (must not exist)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args(argv)
    try:
        split = make_split(_rows(a.windows), _rows(a.labels), seed=a.seed)
        write_split(split, a.out)
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}")
        return 1
    n_dev = sum(v == "dev" for v in split.values())
    print(f"wrote {a.out}: {n_dev} dev, {len(split) - n_dev} test (seed {a.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
