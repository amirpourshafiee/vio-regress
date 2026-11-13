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
