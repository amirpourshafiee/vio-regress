"""S10 judge runner: dev split only, frames only, cached, hard call cap. No API key needed."""
import builtins
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from vioreg import judge, judge_run as jr


class FakeClient:
    """Stands in for anthropic.Anthropic(). reply: str or callable(call_no) -> str."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or (lambda n: json.dumps(
            {"label": "motion_blur", "confidence": 0.8, "evidence": "smeared edges"}))
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.reply(len(self.calls)) if callable(self.reply) else self.reply
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


def make_dir(tmp_path, n_dev=3, n_test=2, with_poison=False):
    d = tmp_path / "labels"
    (d / "sheets").mkdir(parents=True)
    ids = [f"{i:010x}" for i in range(n_dev + n_test)]
    for k, wid in enumerate(ids):
        Image.new("L", (64 + k, 32), 40 + k).save(d / "sheets" / f"{wid}.png")
    with open(d / "windows_ui.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["window_id", "sheet"])
        for wid in ids:
            w.writerow([wid, f"sheets/{wid}.png"])
    with open(d / "split.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["window_id", "split"])
        for k, wid in enumerate(ids):
            w.writerow([wid, "dev" if k < n_dev else "test"])
    if with_poison:
        (d / "human_v1.csv").write_text("window_id,label\n" + "".join(
            f"{i},illumination\n" for i in ids))
        (d / "windows.csv").write_text("window_id,sequence,source,peak_err_m\n" + "".join(
            f"{i},SECRETSEQ,control,0.001\n" for i in ids))
    return d, ids


def run(d, client, **kw):
    args = dict(labels_dir=d, split="dev", client=client, model="m-test", repeats=3,
                max_calls=1000, cache_dir=d / "cache", out_path=d / "out.csv", date="2026-11-18")
    args.update(kw)
    return jr.run_judge(**args)


def read_out(p):
    return list(csv.DictReader(open(p)))


def test_runs_dev_windows_only_three_repeats_each(tmp_path):
    d, ids = make_dir(tmp_path)
    c = FakeClient()
    s = run(d, c)
    rows = read_out(d / "out.csv")
    assert len(c.calls) == 3 * 3 == s["calls"]
    assert {r["window_id"] for r in rows} == set(ids[:3])
    assert sorted({r["run"] for r in rows}) == ["0", "1", "2"]
    assert all(r["model"] == "m-test" and r["prompt_version"] == judge.PROMPT_VERSION for r in rows)
    assert all(r["split"] == "dev" for r in rows)


def test_prompt_is_frames_only_and_one_image(tmp_path):
    d, _ = make_dir(tmp_path, with_poison=True)
    c = FakeClient()
    run(d, c)
    for kw in c.calls:
        content = kw["messages"][0]["content"]
        assert [b["type"] for b in content] == ["image", "text"]
        text = content[1]["text"].lower()
        for banned in ("secretseq", "peak", "error", "sequence", "injected", "control", "fault"):
            assert banned not in text, banned


def test_never_opens_human_labels_or_windows_csv(tmp_path, monkeypatch):
    d, _ = make_dir(tmp_path, with_poison=True)
    opened = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    real_read = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])
    run(d, FakeClient())
    assert not [p for p in opened if p.endswith("human_v1.csv") or p.endswith("windows.csv")]


def test_cache_makes_rerun_free_and_identical(tmp_path):
    d, _ = make_dir(tmp_path)
    c1 = FakeClient()
    run(d, c1)
    first = read_out(d / "out.csv")
    c2 = FakeClient(reply="not json")
    s = run(d, c2, out_path=d / "out2.csv")
    assert c2.calls == [] and s["calls"] == 0 and s["cached"] == 9
    second = read_out(d / "out2.csv")
    assert [(r["window_id"], r["run"], r["label"]) for r in first] == \
           [(r["window_id"], r["run"], r["label"]) for r in second]
    assert all(r["cached"] == "True" for r in second)


def test_cache_key_depends_on_image_prompt_model_and_repeat():
    k = jr.cache_key(b"img", "p", "m", 0)
    assert k == jr.cache_key(b"img", "p", "m", 0)
    assert len({k, jr.cache_key(b"IMG", "p", "m", 0), jr.cache_key(b"img", "q", "m", 0),
                jr.cache_key(b"img", "p", "n", 0), jr.cache_key(b"img", "p", "m", 1)}) == 5


def test_hard_cap_stops_before_exceeding_and_keeps_partial(tmp_path):
    d, _ = make_dir(tmp_path)
    c = FakeClient()
    s = run(d, c, max_calls=4)
    assert s["capped"] is True and len(c.calls) == 4
    rows = read_out(d / "out.csv")
    assert len(rows) == 4
    assert {r["run"] for r in rows} <= {"0", "1"}


def test_retries_count_against_the_cap(tmp_path):
    d, _ = make_dir(tmp_path, n_dev=1)
    c = FakeClient(reply="garbage")
    s = run(d, c, max_calls=5, repeats=3)
    assert len(c.calls) == 5 and s["capped"] is True


def test_parse_failure_is_unclear_and_not_cached(tmp_path):
    d, _ = make_dir(tmp_path, n_dev=1)
    s = run(d, FakeClient(reply="garbage"), repeats=1, max_calls=50)
    rows = read_out(d / "out.csv")
    assert rows[0]["label"] == "unclear" and rows[0]["ok"] == "False"
    assert not list((d / "cache").glob("*.json"))
    assert s["calls"] == 3  # first try plus 2 retries


def test_test_split_needs_explicit_flag_and_runs_once(tmp_path):
    d, ids = make_dir(tmp_path)
    with pytest.raises(PermissionError, match="test split"):
        run(d, FakeClient(), split="test")
    c = FakeClient()
    run(d, c, split="test", test_once=True, out_path=d / "t.csv")
    assert {r["window_id"] for r in read_out(d / "t.csv")} == set(ids[3:])
    with pytest.raises(FileExistsError):
        run(d, FakeClient(), split="test", test_once=True, out_path=d / "t.csv")


def test_dev_output_not_overwritten_unless_asked(tmp_path):
    d, _ = make_dir(tmp_path)
    run(d, FakeClient())
    with pytest.raises(FileExistsError):
        run(d, FakeClient())
    run(d, FakeClient(), overwrite=True)


def test_missing_sheet_is_an_error(tmp_path):
    d, ids = make_dir(tmp_path)
    (d / "sheets" / f"{ids[0]}.png").unlink()
    with pytest.raises(FileNotFoundError):
        run(d, FakeClient())


def test_split_must_cover_the_window_list(tmp_path):
    d, ids = make_dir(tmp_path)
    with open(d / "split.csv", "a") as f:
        f.write("ffffffffff,dev\n")
    with pytest.raises(ValueError, match="split"):
        run(d, FakeClient())


def test_summary_reports_tokens_and_dry_run_costs_nothing(tmp_path):
    d, _ = make_dir(tmp_path)
    c = FakeClient()
    plan = jr.plan_run(d, "dev", model="m-test", repeats=3, cache_dir=d / "cache")
    assert plan["windows"] == 3 and plan["calls_needed"] == 9 and c.calls == []
    s = run(d, c)
    assert s["input_tokens"] == 9000 and s["output_tokens"] == 900


def test_build_client_without_key_gives_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        jr.build_client()
    assert "ANTHROPIC_API_KEY" in str(e.value)


def test_cli_requires_explicit_cap(tmp_path, capsys):
    with pytest.raises(SystemExit):
        jr.main(["--labels-dir", str(tmp_path)])
    assert "--max-calls" in capsys.readouterr().err


class ThinkingFirstClient(FakeClient):
    """Sonnet 5.5 can put a thinking block (no .text) before the answer text."""

    def create(self, **kw):
        r = super().create(**kw)
        text = r.content[0].text
        r.content = [SimpleNamespace(type="thinking", thinking="hmm"),
                     SimpleNamespace(type="text", text=text)]
        return r


def test_reply_text_skips_thinking_blocks_and_joins_text():
    blocks = [SimpleNamespace(type="thinking", thinking="x"),
              SimpleNamespace(type="text", text="a"), SimpleNamespace(type="text", text="b")]
    assert judge.reply_text(SimpleNamespace(content=blocks)) == "ab"
    assert judge.reply_text(SimpleNamespace(content=[SimpleNamespace(text="plain")])) == "plain"
    assert judge.reply_text(SimpleNamespace(content=[SimpleNamespace(type="thinking")])) == ""


def test_thinking_block_before_text_is_handled(tmp_path):
    d, _ = make_dir(tmp_path, n_dev=1)
    s = run(d, ThinkingFirstClient(), repeats=1)
    rows = read_out(d / "out.csv")
    assert rows[0]["label"] == "motion_blur" and rows[0]["ok"] == "True" and s["calls"] == 1


def test_request_leaves_room_for_thinking_and_sets_low_effort(tmp_path):
    d, _ = make_dir(tmp_path, n_dev=1)
    c = FakeClient()
    run(d, c, repeats=1)
    kw = c.calls[0]
    assert kw["max_tokens"] >= 1000
    assert kw["extra_body"] == {"output_config": {"effort": "low"}}
    assert [r["settings"] for r in read_out(d / "out.csv")] == [judge.REQUEST_SETTINGS_ID]


def test_cache_prompt_includes_request_settings():
    assert judge.REQUEST_SETTINGS_ID in judge.CACHE_PROMPT
    assert judge.PROMPT_V1 in judge.CACHE_PROMPT
