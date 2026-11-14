"""Fetcher tests against a local fake of the Research Collection layout.

The real archive is outer.zip -> <env>/<seq>/<seq>.bag (deflated) and
<env>/<seq>/<seq>.zip (stored, itself a zip holding mav0/...). These tests build
that layout in tmp_path and serve it through an in-memory range transport, so
no network is touched.
"""
import io
import json
import zipfile

import pytest

from vioreg import fetch

GT_CSV = b"#timestamp,p_x,p_y,p_z,q_w,q_x,q_y,q_z\n1,0,0,0,1,0,0,0\n"
BAG = b"#ROSBAG V2.0\n" + bytes(range(256)) * 400


class MemTransport:
    """Range transport over bytes. Counts requests so tests can check we stay cheap."""

    def __init__(self, blob: bytes):
        self.blob, self.requests = blob, 0

    def size(self, url):
        return len(self.blob)

    def get(self, url, a, b):
        self.requests += 1
        return self.blob[a:b + 1]

    def stream(self, url, a, b, chunk=1 << 16):
        self.requests += 1
        for i in range(a, b + 1, chunk):
            yield self.blob[i:min(i + chunk, b + 1)]


def make_outer(seq="MH_01_easy", env="machine_hall", bag=BAG, gt=GT_CSV,
               inner_type=zipfile.ZIP_STORED):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mav0/state_groundtruth_estimate0/data.csv", gt)
        z.writestr("mav0/cam0/data.csv", b"ignored")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr(f"{env}/{seq}/{seq}.bag", bag, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(f"{env}/{seq}/{seq}.zip", inner.getvalue(), compress_type=inner_type)
    return outer.getvalue()


def test_archive_for_sequence():
    assert fetch.archive_for("MH_01_easy").name == "machine_hall"
    assert fetch.archive_for("V1_01_easy").name == "vicon_room1"
    assert fetch.archive_for("V2_03_difficult").name == "vicon_room2"
    with pytest.raises(ValueError):
        fetch.archive_for("MH_09_bogus")


@pytest.mark.parametrize("inner_type", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_fetch_extracts_bag_and_groundtruth(tmp_path, inner_type):
    # The real MH_01_easy.zip is DEFLATED inside machine_hall.zip, so it must be
    # inflated in full; the STORED path range-reads straight into it.
    tr = MemTransport(make_outer(inner_type=inner_type))
    out = fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")
    assert (tmp_path / "MH_01_easy" / "MH_01_easy.bag").read_bytes() == BAG
    gt = tmp_path / "MH_01_easy" / "mav0" / "state_groundtruth_estimate0" / "data.csv"
    assert gt.read_bytes() == GT_CSV
    man = json.loads((tmp_path / "MH_01_easy" / "manifest.json").read_text())
    assert man["sequence"] == "MH_01_easy" and len(man["files"]) == 2
    assert all("crc32" in f for f in man["files"])
    assert out == tmp_path / "MH_01_easy"
    assert tr.requests < 15  # directory reads + one stream per member, never per-chunk
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("*.asl.zip"))


def test_plan_counts_inner_zip_only_when_it_must_be_inflated():
    stored = MemTransport(make_outer(inner_type=zipfile.ZIP_STORED))
    deflated = MemTransport(make_outer(inner_type=zipfile.ZIP_DEFLATED))
    assert fetch.plan_bytes("MH_01_easy", stored, "mem://x") < fetch.plan_bytes(
        "MH_01_easy", deflated, "mem://x")


def test_second_fetch_is_a_noop(tmp_path):
    tr = MemTransport(make_outer())
    fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")
    tr.requests = 0
    fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")
    assert tr.requests == 0


def test_corrupt_payload_fails_closed_and_leaves_nothing(tmp_path):
    blob = bytearray(make_outer())
    # Flip bytes inside the deflated bag payload (after the first local header).
    i = blob.index(b"MH_01_easy.bag") + 200
    blob[i:i + 8] = b"\x00" * 8
    with pytest.raises(fetch.FetchError):
        fetch.fetch_sequence("MH_01_easy", tmp_path, transport=MemTransport(bytes(blob)),
                             url="mem://x")
    assert not (tmp_path / "MH_01_easy" / "MH_01_easy.bag").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert not (tmp_path / "MH_01_easy" / "manifest.json").exists()


def test_missing_member_fails_closed(tmp_path):
    tr = MemTransport(make_outer(seq="MH_02_easy"))
    with pytest.raises(fetch.FetchError, match="MH_01_easy"):
        fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")


def test_size_guard_refuses_large_plans_without_consent():
    with pytest.raises(fetch.FetchError, match="--yes"):
        fetch.check_budget(6e9, max_bytes=5e9, consent=False)
    fetch.check_budget(6e9, max_bytes=5e9, consent=True)
    fetch.check_budget(2e9, max_bytes=5e9, consent=False)


def test_manifest_with_missing_file_triggers_refetch(tmp_path):
    """S3 deletes the raw bag after scoring. A later fetch must not trust the stale
    manifest and silently skip: any listed file missing means fetch again."""
    tr = MemTransport(make_outer())
    fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")
    (tmp_path / "MH_01_easy" / "MH_01_easy.bag").unlink()
    tr.requests = 0
    fetch.fetch_sequence("MH_01_easy", tmp_path, transport=tr, url="mem://x")
    assert tr.requests > 0
    assert (tmp_path / "MH_01_easy" / "MH_01_easy.bag").read_bytes() == BAG


def test_is_complete(tmp_path):
    assert not fetch.is_complete(tmp_path / "MH_01_easy")
    fetch.fetch_sequence("MH_01_easy", tmp_path, transport=MemTransport(make_outer()), url="mem://x")
    assert fetch.is_complete(tmp_path / "MH_01_easy")
    (tmp_path / "MH_01_easy" / "MH_01_easy.bag").write_bytes(b"truncated")
    assert not fetch.is_complete(tmp_path / "MH_01_easy")  # size mismatch


# ---------------------------------------------------------------- retry / transient failures (S5c)
import http.client  # noqa: E402
import urllib.error  # noqa: E402


class FakeResponse:
    def __init__(self, data=b"x", status=206, fail_read=None):
        self.status, self._data, self.fail_read = status, data, fail_read
        self.headers = {"Content-Range": f"bytes 0-0/{len(data)}"}

    def read(self, n=-1):
        if self.fail_read:
            raise self.fail_read
        d, self._data = self._data, b""
        return d


def opener_from(events):
    """Each call pops one event: an exception to raise, or a FakeResponse to return."""
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        ev = events.pop(0)
        if isinstance(ev, BaseException):
            raise ev
        return ev
    return opener, calls


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


@pytest.mark.parametrize("transient", [
    http_error(429), http_error(500), http_error(502), http_error(503), http_error(504),
    urllib.error.URLError("connection refused"), TimeoutError("timed out"),
    ConnectionResetError("reset"),
])
def test_transport_retries_transient_errors_with_exponential_backoff(transient):
    opener, calls = opener_from([transient, transient, FakeResponse(b"ab")])
    slept = []
    tr = fetch.HttpTransport(retries=5, backoff_s=10.0, opener=opener, sleep=slept.append)
    assert tr.get("https://x", 0, 1) == b"ab"
    assert len(calls) == 3 and slept == [10.0, 20.0]


def test_transport_gives_up_after_retries_as_unavailable():
    opener, calls = opener_from([http_error(429)] * 4)
    slept = []
    tr = fetch.HttpTransport(retries=3, backoff_s=1.0, opener=opener, sleep=slept.append)
    with pytest.raises(fetch.FetchUnavailable, match="429"):
        tr.get("https://x", 0, 0)
    assert len(calls) == 4 and slept == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("code", [403, 404, 416])
def test_transport_does_not_retry_permanent_http_errors(code):
    opener, calls = opener_from([http_error(code)])
    tr = fetch.HttpTransport(retries=5, opener=opener, sleep=lambda s: pytest.fail("slept"))
    with pytest.raises(fetch.FetchError) as ei:
        tr.get("https://x", 0, 0)
    assert not isinstance(ei.value, fetch.FetchUnavailable) and len(calls) == 1


@pytest.mark.parametrize("err", [ConnectionResetError("reset"), TimeoutError("t"),
                                 http.client.IncompleteRead(b"")])
def test_a_drop_mid_stream_is_unavailable_not_a_crash(err):
    opener, _ = opener_from([FakeResponse(b"abc", fail_read=err)])
    tr = fetch.HttpTransport(retries=0, opener=opener, sleep=lambda s: None)
    with pytest.raises(fetch.FetchUnavailable):
        list(tr.stream("https://x", 0, 2))


class FlakyTransport(MemTransport):
    """size() is called once by planning (call 1) and once per fetch attempt (calls 2, 3,
    ...). Raises FetchUnavailable on the call numbers in `fail_on`."""

    def __init__(self, blob, fail_on):
        super().__init__(blob)
        self.fail_on, self.sizes = set(fail_on), 0

    def size(self, url):
        self.sizes += 1
        if self.sizes in self.fail_on:
            raise fetch.FetchUnavailable("HTTP 429 after retries")
        return super().size(url)


def run_main(tmp_path, transport, attempts="3", extra=()):
    slept = []
    status = tmp_path / "MH_01_easy.fetch.json"
    rc = fetch.main([str(tmp_path / "data"), "MH_01_easy", "--attempts", attempts,
                     "--backoff-s", "60", "--status-dir", str(tmp_path), *extra],
                    transport_factory=lambda: transport, sleep=slept.append)
    return rc, slept, json.loads(status.read_text())


def test_main_retries_the_sequence_with_exponential_backoff_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    tr = FlakyTransport(make_outer(), fail_on={2, 3})  # fetch attempts 1 and 2 fail
    rc, slept, st = run_main(tmp_path, tr)
    assert rc == 0 and slept == [60.0, 120.0]
    assert st == {"sequence": "MH_01_easy", "ok": True, "kind": "ok", "attempts": 3, "reason": ""}
    assert (tmp_path / "data" / "MH_01_easy" / "MH_01_easy.bag").read_bytes() == BAG


def test_main_exhausted_transient_failure_exits_75_and_says_not_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    rc, slept, st = run_main(tmp_path, FlakyTransport(make_outer(), fail_on={2, 3, 4}))
    assert rc == fetch.EXIT_UNAVAILABLE == 75
    assert slept == [60.0, 120.0]
    assert st["ok"] is False and st["kind"] == "unavailable" and st["attempts"] == 3
    assert "429" in st["reason"]
    assert not (tmp_path / "data" / "MH_01_easy" / "MH_01_easy.bag").exists()


def test_main_permanent_failure_exits_1_without_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    blob = bytearray(make_outer())
    i = blob.find(b"#ROSBAG")
    blob[i + 100] ^= 0xFF  # corrupt the stored payload -> integrity failure, not transient
    rc, slept, st = run_main(tmp_path, MemTransport(bytes(blob)))
    assert rc == 1 and slept == []
    assert st["ok"] is False and st["kind"] == "error" and st["attempts"] == 1


def test_status_file_overwrites_a_stale_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    (tmp_path / "MH_01_easy.fetch.json").write_text('{"ok": false, "kind": "unavailable"}')
    rc, _, st = run_main(tmp_path, MemTransport(make_outer()))
    assert rc == 0 and st["ok"] is True


def test_main_retries_planning_too(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    rc, slept, st = run_main(tmp_path, FlakyTransport(make_outer(), fail_on={1}))
    assert rc == 0 and slept == [60.0] and st["ok"] is True and st["attempts"] == 1


def test_main_planning_unavailable_after_retries_exits_75(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "archive_for", lambda s: fetch.Archive("machine_hall", "mem://x", 1, ""))
    rc, slept, st = run_main(tmp_path, FlakyTransport(make_outer(), fail_on={1, 2, 3}))
    assert rc == 75 and slept == [60.0, 120.0]
    assert st["ok"] is False and st["kind"] == "unavailable" and st["attempts"] == 3


def test_wrapper_script_passes_exit_code_75_through(tmp_path):
    """scripts/fetch_euroc.sh execs python, so the workflow sees 75, not a generic 1."""
    import subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    shim = tmp_path / "python3"
    shim.write_text("#!/bin/sh\nexit 75\n")
    shim.chmod(0o755)
    import os
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    rc = subprocess.run(["bash", str(root / "scripts" / "fetch_euroc.sh"), "x", "MH_01_easy"],
                        env=env).returncode
    assert rc == 75
