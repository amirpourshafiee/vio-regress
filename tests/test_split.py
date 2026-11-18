"""Dev/test split of the labeled windows (guide 8.3, rule 6): written once, never recomputed."""
import csv
from collections import Counter
from pathlib import Path

import pytest

from vioreg import split as sp

ROOT = Path(__file__).resolve().parents[1]


def make_rows(counts):
    """counts: {(label, source): n} -> (windows rows, label rows) with opaque ids."""
    windows, labels = [], []
    i = 0
    for (label, source), n in sorted(counts.items()):
        for _ in range(n):
            wid = f"{i:010x}"
            i += 1
            windows.append({"window_id": wid, "source": source})
            labels.append({"window_id": wid, "label": label})
    return windows, labels


COUNTS = {
    ("motion_blur", "injected"): 20, ("motion_blur", "natural"): 3, ("motion_blur", "control"): 1,
    ("illumination", "injected"): 15, ("illumination", "natural"): 1, ("illumination", "control"): 4,
    ("no_visual_cause", "natural"): 80, ("no_visual_cause", "control"): 50,
    ("no_visual_cause", "injected"): 32, ("handling_event", "injected"): 1,
    ("handling_event", "natural"): 1, ("unclear", "natural"): 2, ("unclear", "injected"): 1,
    ("low_texture", "injected"): 1, ("low_texture", "natural"): 1,
}


def test_partition_covers_every_window_once():
    w, l = make_rows(COUNTS)
    s = sp.make_split(w, l, seed=1)
    assert sorted(s) == sorted(r["window_id"] for r in w)
    assert set(s.values()) == {"dev", "test"}


def test_dev_share_is_about_forty_percent_overall_and_per_stratum():
    w, l = make_rows(COUNTS)
    s = sp.make_split(w, l, seed=1)
    n = len(w)
    assert abs(sum(v == "dev" for v in s.values()) - 0.4 * n) <= 1.0
    lab = {r["window_id"]: r["label"] for r in l}
    src = {r["window_id"]: r["source"] for r in w}
    size, dev = Counter(), Counter()
    for wid, part in s.items():
        key = (lab[wid], src[wid])
        size[key] += 1
        dev[key] += part == "dev"
    for key, m in size.items():
        assert abs(dev[key] - 0.4 * m) < 1.0, key


def test_same_seed_same_split_and_other_seed_differs():
    w, l = make_rows(COUNTS)
    assert sp.make_split(w, l, seed=7) == sp.make_split(w, l, seed=7)
    assert sp.make_split(w, l, seed=7) != sp.make_split(w, l, seed=8)


def test_input_order_does_not_change_the_split():
    w, l = make_rows(COUNTS)
    a = sp.make_split(w, l, seed=3)
    b = sp.make_split(list(reversed(w)), list(reversed(l)), seed=3)
    assert a == b


def test_label_set_must_match_window_set():
    w, l = make_rows({("motion_blur", "natural"): 5})
    with pytest.raises(ValueError, match="missing"):
        sp.make_split(w, l[:-1], seed=1)
    extra = l + [{"window_id": "ffffffffff", "label": "unclear"}]
    with pytest.raises(ValueError, match="not in the window set"):
        sp.make_split(w, extra, seed=1)


def test_duplicate_ids_rejected():
    w, l = make_rows({("motion_blur", "natural"): 3})
    with pytest.raises(ValueError, match="duplicate"):
        sp.make_split(w + [w[0]], l, seed=1)
    with pytest.raises(ValueError, match="duplicate"):
        sp.make_split(w, l + [l[0]], seed=1)


def test_write_is_sorted_has_no_labels_and_refuses_overwrite(tmp_path):
    w, l = make_rows(COUNTS)
    s = sp.make_split(w, l, seed=1)
    out = tmp_path / "split.csv"
    sp.write_split(s, out)
    lines = out.read_text().splitlines()
    assert lines[0] == "window_id,split"
    ids = [x.split(",")[0] for x in lines[1:]]
    assert ids == sorted(ids)
    assert all(len(x.split(",")) == 2 for x in lines)
    with pytest.raises(FileExistsError):
        sp.write_split(s, out)


def test_check_split_detects_drift(tmp_path):
    w, l = make_rows(COUNTS)
    s = sp.make_split(w, l, seed=1)
    out = tmp_path / "split.csv"
    sp.write_split(s, out)
    assert sp.check_split(out, [r["window_id"] for r in w]) == []
    ids = [r["window_id"] for r in w]
    problems = sp.check_split(out, ids[:-1])
    assert any("not in the window set" in p for p in problems)
    problems = sp.check_split(out, ids + ["ffffffffff"])
    assert any("missing" in p for p in problems)


def test_cli_end_to_end(tmp_path):
    w, l = make_rows(COUNTS)
    wp, hp, out = tmp_path / "w.csv", tmp_path / "h.csv", tmp_path / "split.csv"
    with open(wp, "w", newline="") as f:
        d = csv.DictWriter(f, ["window_id", "source"])
        d.writeheader()
        d.writerows(w)
    with open(hp, "w", newline="") as f:
        d = csv.DictWriter(f, ["window_id", "label", "labeled_at_utc", "notes"])
        d.writeheader()
        d.writerows({**r, "labeled_at_utc": "", "notes": ""} for r in l)
    assert sp.main(["--windows", str(wp), "--labels", str(hp), "--out", str(out)]) == 0
    assert out.exists()
    assert sp.main(["--windows", str(wp), "--labels", str(hp), "--out", str(out)]) != 0


def test_committed_split_matches_committed_window_set():
    split = ROOT / "labels" / "split.csv"
    ui = ROOT / "labels" / "windows_ui.csv"
    if not split.exists():
        pytest.skip("labels/split.csv not committed yet")
    ids = [r["window_id"] for r in csv.DictReader(open(ui))]
    assert sp.check_split(split, ids) == []
