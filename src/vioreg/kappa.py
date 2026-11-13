"""Agreement statistics between human labels and VLM labels."""
from __future__ import annotations

from collections import Counter

import numpy as np
from sklearn.metrics import cohen_kappa_score, confusion_matrix, precision_recall_fscore_support


def kappa(a, b, labels) -> float:
    return float(cohen_kappa_score(a, b, labels=labels))


def bootstrap_ci(a, b, labels, n: int = 2000, seed: int = 0, alpha: float = 0.05,
                 groups=None):
    """Percentile bootstrap. With groups (e.g. the sequence name of each window),
    whole groups are resampled, which respects that windows from one sequence are
    correlated. Without groups, items are resampled independently."""
    rng = np.random.default_rng(seed)
    a, b = np.asarray(a), np.asarray(b)
    if groups is None:
        draw = lambda: rng.integers(0, len(a), len(a))  # noqa: E731
    else:
        g = np.asarray(groups)
        uniq = np.unique(g)
        members = {u: np.nonzero(g == u)[0] for u in uniq}
        draw = lambda: np.concatenate([members[u] for u in rng.choice(uniq, len(uniq))])  # noqa: E731
    vals = []
    for _ in range(n):
        idx = draw()
        if len(set(a[idx])) < 2 and len(set(b[idx])) < 2:
            continue  # kappa undefined when both raters are constant
        v = cohen_kappa_score(a[idx], b[idx], labels=labels)
        if not np.isnan(v):
            vals.append(v)
    lo, hi = np.quantile(vals, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def agreement_report(human, vlm, labels, groups=None) -> dict:
    human, vlm = list(human), list(vlm)
    if len(human) != len(vlm) or not human:
        raise ValueError("need equal-length, non-empty label lists")
    n, k = len(human), len(labels)
    po = float(np.mean([h == v for h, v in zip(human, vlm)]))
    majority_label, majority_n = Counter(human).most_common(1)[0]
    lo, hi = bootstrap_ci(human, vlm, labels, groups=groups)
    p, r, f1, support = precision_recall_fscore_support(
        human, vlm, labels=labels, zero_division=0)
    return {
        "n": n,
        "kappa": kappa(human, vlm, labels),
        "kappa_ci95": [lo, hi],
        "ci_resamples": "groups" if groups is not None else "items",
        "accuracy": po,
        "prevalence_adjusted_kappa": (k * po - 1) / (k - 1),
        "majority_baseline_accuracy": majority_n / n,
        "majority_label": majority_label,
        "macro_f1": float(np.mean(f1)),
        "per_class": {lab: {"precision": float(p[i]), "recall": float(r[i]),
                            "support": int(support[i])} for i, lab in enumerate(labels)},
        "labels": list(labels),
        "confusion": confusion_matrix(human, vlm, labels=labels).tolist(),
    }
