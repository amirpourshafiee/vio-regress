"""Fetch single EuRoC sequences from the ETH Research Collection.

Source: doi:10.3929/ethz-b-000690084 ("The EuRoC micro aerial vehicle datasets").
License: In Copyright - Non-Commercial Use Permitted (rightsstatements.org InC-NC/1.0);
download and storage for personal, non-commercial use. Data stays under data/ (gitignored).

The record only offers whole-environment archives (machine_hall.zip is 12.7 GB), but
the server honours HTTP Range requests. So we read the zip central directory (a few
KB from the tail), then stream exactly the members we need:

  <env>/<seq>/<seq>.bag                        deflated in the outer zip -> inflate
  <env>/<seq>/<seq>.zip :: mav0/state_groundtruth_estimate0/data.csv
                                               the inner ASL zip. In the real record it is
                                               DEFLATED (checked for MH_01_easy), so it is
                                               inflated to a temp file, CRC-checked, the CSV
                                               extracted, and the temp file deleted. If an
                                               archive ever stores it, we range-read into it.

For MH_01_easy this transfers ~3.6 GB (2.04 GB bag + 1.57 GB ASL zip), not 12.7 GB.

Integrity: each member's CRC32 and size from the zip directory are checked. The
whole-archive MD5 published on the record cannot be checked on a partial download.
Everything fails closed: on any error the partial file is deleted and we exit non-zero.
"""
from __future__ import annotations

import argparse
import http.client
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

DOI = "10.3929/ethz-b-000690084"
API = "https://www.research-collection.ethz.ch/server/api/core/bitstreams"
USER_AGENT = "vioreg-fetch/0.1"
GT_MEMBER = "mav0/state_groundtruth_estimate0/data.csv"
DEFAULT_MAX_BYTES = 5e9


@dataclass(frozen=True)
class Archive:
    name: str
    bitstream: str
    size: int
    md5: str

    @property
    def url(self) -> str:
        return f"{API}/{self.bitstream}/content"


# From the record's ORIGINAL bundle (REST API, checked 2026-11-13).
ARCHIVES = {
    "machine_hall": Archive("machine_hall", "7b2419c1-62b5-4714-b7f8-485e5fe3e5fe",
                            12683729426, "363f5c2502b469cdd97ef85997714806"),
    "vicon_room1": Archive("vicon_room1", "02ecda9a-298f-498b-970c-b7c44334d880",
                           6042263426, "5ce06b405827e453a82523d3ca9c2fd0"),
    "vicon_room2": Archive("vicon_room2", "ea12bc01-3677-4b4c-853d-87c7870b8c44",
                           6013384949, "c6347f4e0476aaa9a43a919c163c49c5"),
}
SEQUENCES = {
    "machine_hall": ["MH_01_easy", "MH_02_easy", "MH_03_medium", "MH_04_difficult",
                     "MH_05_difficult"],
    "vicon_room1": ["V1_01_easy", "V1_02_medium", "V1_03_difficult"],
    "vicon_room2": ["V2_01_easy", "V2_02_medium", "V2_03_difficult"],
}


class FetchError(RuntimeError):
    pass


class FetchUnavailable(FetchError):
    """Transient: HTTP 429/5xx, a timeout or a dropped connection that persisted after the
    retries. The data could not be fetched; it says nothing about VIO quality."""


EXIT_UNAVAILABLE = 75  # EX_TEMPFAIL: transient failure after every retry (1 = any other failure)
TRANSIENT_HTTP = (429, 500, 502, 503, 504)
TRANSIENT_NET = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


def archive_for(seq: str) -> Archive:
    for env, seqs in SEQUENCES.items():
        if seq in seqs:
            return ARCHIVES[env]
    raise ValueError(f"unknown EuRoC sequence {seq!r}")


def check_budget(nbytes: float, max_bytes: float, consent: bool) -> None:
    if nbytes > max_bytes and not consent:
        raise FetchError(f"plan downloads {nbytes / 1e9:.2f} GB > limit "
                         f"{max_bytes / 1e9:.2f} GB; re-run with --yes to allow")


# ---------------------------------------------------------------- transport
class HttpTransport:
    """Range GETs with bounded retry and exponential backoff (backoff_s * 2**attempt) on
    HTTP 429/5xx, timeouts and refused/reset connections (the server rate-limits scrapers).
    Exhausted transient errors raise FetchUnavailable; any other HTTP error raises
    FetchError at once. A drop while reading a body also raises FetchUnavailable, so the
    caller can restart the whole sequence (main(): --attempts)."""

    def __init__(self, retries: int = 5, backoff_s: float = 10.0,
                 opener=urllib.request.urlopen, sleep=time.sleep):
        self.retries, self.backoff_s = retries, backoff_s
        self.opener, self.sleep = opener, sleep

    def _open(self, url, a, b):
        req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}",
                                                   "User-Agent": USER_AGENT})
        for attempt in range(self.retries + 1):
            try:
                r = self.opener(req, timeout=120)
            except urllib.error.HTTPError as e:  # a URLError subclass: must come first
                if e.code not in TRANSIENT_HTTP:
                    raise FetchError(f"HTTP {e.code} for {url} bytes={a}-{b}") from e
                err = f"HTTP {e.code}"
            except TRANSIENT_NET as e:
                err = f"{type(e).__name__}: {e}"
            else:
                if r.status != 206:  # a 200 would silently send the whole 12 GB archive
                    raise FetchError(f"server ignored Range (HTTP {r.status}) for {url}")
                return r
            if attempt < self.retries:
                self.sleep(self.backoff_s * 2 ** attempt)
        raise FetchUnavailable(f"{err} for {url} bytes={a}-{b} after {self.retries} retries")

    def _read(self, r, n=-1):
        try:
            return r.read(n)
        except (OSError, http.client.HTTPException) as e:  # reset, timeout, IncompleteRead
            raise FetchUnavailable(f"connection dropped while reading: "
                                   f"{type(e).__name__}: {e}") from e

    def size(self, url):
        r = self._open(url, 0, 0)
        return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def get(self, url, a, b):
        data = self._read(self._open(url, a, b))
        if len(data) != b - a + 1:
            raise FetchError(f"short read {len(data)} != {b - a + 1}")
        return data

    def stream(self, url, a, b, chunk=1 << 20):
        r = self._open(url, a, b)
        while True:
            d = self._read(r, chunk)
            if not d:
                return
            yield d


class RangeFile:
    """Read-only seekable view of bytes [base, base+length) of a remote object.

    The tail is prefetched once so zipfile's central-directory parsing costs one
    request instead of hundreds.
    """

    def __init__(self, transport, url, base=0, length=None, tail=1 << 20):
        self.t, self.url, self.base = transport, url, base
        self.length = transport.size(url) - base if length is None else length
        n = min(tail, self.length)
        self.tail_start = self.length - n
        self.tail = transport.get(url, base + self.tail_start, base + self.length - 1)
        self.pos = 0

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = [off, self.pos + off, self.length + off][whence]
        return self.pos

    def read(self, n=-1):
        n = self.length - self.pos if n is None or n < 0 else min(n, self.length - self.pos)
        if n <= 0:
            return b""
        if self.pos >= self.tail_start:
            o = self.pos - self.tail_start
            d = self.tail[o:o + n]
        else:
            d = self.t.get(self.url, self.base + self.pos, self.base + self.pos + n - 1)
        self.pos += len(d)
        return d


def _data_offset(rf: RangeFile, zi: zipfile.ZipInfo) -> int:
    """Offset (within rf) of a member's payload, after its local file header."""
    rf.seek(zi.header_offset)
    hdr = rf.read(30)
    if hdr[:4] != b"PK\x03\x04":
        raise FetchError(f"bad local header for {zi.filename}")
    n, m = struct.unpack("<HH", hdr[26:30])
    return zi.header_offset + 30 + n + m


def _member(z: zipfile.ZipFile, name: str, seq: str) -> zipfile.ZipInfo:
    try:
        return z.getinfo(name)
    except KeyError:
        raise FetchError(f"{seq}: member {name!r} not in archive") from None


def _extract(rf: RangeFile, zi: zipfile.ZipInfo, dest: Path) -> dict:
    """Stream one member to dest, inflating if needed, verifying size and CRC32."""
    if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise FetchError(f"{zi.filename}: unsupported compression {zi.compress_type}")
    start = rf.base + _data_offset(rf, zi)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    crc, size = 0, 0
    inflate = zlib.decompressobj(-15) if zi.compress_type == zipfile.ZIP_DEFLATED else None
    try:
        with open(part, "wb") as f:
            if zi.compress_size:
                for chunk in rf.t.stream(rf.url, start, start + zi.compress_size - 1):
                    out = inflate.decompress(chunk) if inflate else chunk
                    crc, size = zlib.crc32(out, crc), size + len(out)
                    f.write(out)
            if inflate:
                out = inflate.flush()
                crc, size = zlib.crc32(out, crc), size + len(out)
                f.write(out)
        if size != zi.file_size or crc != zi.CRC:
            raise FetchError(f"{zi.filename}: integrity check failed "
                             f"(size {size}/{zi.file_size}, crc {crc:08x}/{zi.CRC:08x})")
        part.replace(dest)
    except BaseException as e:
        part.unlink(missing_ok=True)
        if isinstance(e, zlib.error):
            raise FetchError(f"{zi.filename}: corrupt deflate stream: {e}") from e
        raise
    return {"member": zi.filename, "bytes": size, "crc32": f"{crc:08x}"}


def _extract_from_inflated_zip(outer_rf, asl_zi, member, out: Path, seq: str) -> dict:
    """Inflate a compressed inner zip to a temp file, pull one member, delete the temp."""
    tmp = out / f"{seq}.asl.zip"
    try:
        _extract(outer_rf, asl_zi, tmp)  # CRC-checked
        with zipfile.ZipFile(tmp) as inner:
            zi = _member(inner, member, seq)
            dest = out / member
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = inner.read(zi)  # zipfile verifies the member CRC on read
        dest.write_bytes(data)
        return {"member": f"{asl_zi.filename}::{member}", "bytes": len(data),
                "crc32": f"{zlib.crc32(data):08x}"}
    except zipfile.BadZipFile as e:
        raise FetchError(f"{asl_zi.filename}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)


def is_complete(seq_dir: Path) -> bool:
    """True only if manifest.json exists and every file it lists is present with the
    recorded size. S3 deletes raw bags after scoring; a stale manifest must not make a
    later fetch skip them."""
    m = Path(seq_dir) / "manifest.json"
    try:
        files = json.loads(m.read_text())["files"]
    except (OSError, ValueError, KeyError):
        return False
    return all((Path(seq_dir) / f["path"]).is_file()
               and (Path(seq_dir) / f["path"]).stat().st_size == f["bytes"] for f in files)


def plan_bytes(seq: str, transport=None, url=None) -> int:
    """Compressed bytes that fetch_sequence would transfer for seq (directory reads only)."""
    tr, arch = transport or HttpTransport(), archive_for(seq)
    outer = zipfile.ZipFile(RangeFile(tr, url or arch.url))
    n = _member(outer, f"{arch.name}/{seq}/{seq}.bag", seq).compress_size
    asl = _member(outer, f"{arch.name}/{seq}/{seq}.zip", seq)
    return n + (asl.compress_size if asl.compress_type != zipfile.ZIP_STORED else 0)


def fetch_sequence(seq: str, out_root: str | Path, transport=None, url=None) -> Path:
    tr, arch = transport or HttpTransport(), archive_for(seq)
    out = Path(out_root) / seq
    manifest = out / "manifest.json"
    if is_complete(out):
        return out
    manifest.unlink(missing_ok=True)  # stale: a listed file was deleted or changed
    url = url or arch.url
    outer_rf = RangeFile(tr, url)
    outer = zipfile.ZipFile(outer_rf)
    bag_zi = _member(outer, f"{arch.name}/{seq}/{seq}.bag", seq)
    asl_zi = _member(outer, f"{arch.name}/{seq}/{seq}.zip", seq)
    if asl_zi.compress_type == zipfile.ZIP_STORED:
        inner_rf = RangeFile(tr, url, base=_data_offset(outer_rf, asl_zi),
                             length=asl_zi.compress_size)
        gt_zi = _member(zipfile.ZipFile(inner_rf), GT_MEMBER, seq)
        gt = _extract(inner_rf, gt_zi, out / GT_MEMBER)
    else:
        gt = _extract_from_inflated_zip(outer_rf, asl_zi, GT_MEMBER, out, seq)

    files = [{"path": GT_MEMBER, **gt},
             {"path": f"{seq}.bag", **_extract(outer_rf, bag_zi, out / f"{seq}.bag")}]
    manifest.write_text(json.dumps({
        "sequence": seq, "doi": DOI, "archive": arch.name, "url": url,
        "license": "In Copyright - Non-Commercial Use Permitted (InC-NC/1.0)",
        "files": files}, indent=2) + "\n")
    return out


def _with_retry(fn, attempts: int, backoff_s: float, sleep, what: str):
    """Call fn(); on FetchUnavailable wait backoff_s * 2**i and try again, up to `attempts`
    calls in all. Returns (value, calls). Re-raises the last FetchUnavailable with .calls."""
    for i in range(attempts):
        try:
            return fn(), i + 1
        except FetchUnavailable as e:
            if i + 1 == attempts:
                e.calls = attempts
                raise
            wait = backoff_s * 2 ** i
            print(f"  {what}: {e}; attempt {i + 1}/{attempts} failed, retrying in {wait:.0f} s",
                  file=sys.stderr, flush=True)
            sleep(wait)


def _write_status(status_dir, seq: str, ok: bool, kind: str, attempts: int, reason: str = ""):
    """<status_dir>/<seq>.fetch.json, always written so a stale failure cannot linger.
    kind: ok | unavailable (transient, after retries) | error (anything else)."""
    if status_dir is None:
        return
    Path(status_dir).mkdir(parents=True, exist_ok=True)
    (Path(status_dir) / f"{seq}.fetch.json").write_text(json.dumps(
        {"sequence": seq, "ok": ok, "kind": kind, "attempts": attempts, "reason": reason}) + "\n")


def main(argv=None, transport_factory=HttpTransport, sleep=time.sleep) -> int:
    """Exit 0 = fetched; EXIT_UNAVAILABLE (75) = transient failure (429/5xx, timeout, dropped
    connection) after every retry; 1 = any other failure (integrity, missing member, size
    guard, unknown sequence). Exits 75 and 1 both mean "no data", never a quality result."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_root")
    ap.add_argument("sequences", nargs="+")
    ap.add_argument("--yes", action="store_true", help="allow plans above --max-gb")
    ap.add_argument("--max-gb", type=float, default=DEFAULT_MAX_BYTES / 1e9)
    ap.add_argument("--attempts", type=int, default=3,
                    help="whole-sequence attempts on a transient failure (each request "
                         "also retries 5 times with its own backoff)")
    ap.add_argument("--backoff-s", type=float, default=60.0,
                    help="wait before the 2nd attempt; doubles each time")
    ap.add_argument("--status-dir", default=None,
                    help="write <dir>/<seq>.fetch.json with the outcome of each sequence")
    a = ap.parse_args(argv)
    if a.attempts < 1:
        ap.error("--attempts must be >= 1")
    tr = transport_factory()
    current, calls = list(a.sequences), 0
    try:
        todo = [s for s in a.sequences if not is_complete(Path(a.out_root) / s)]
        total, calls = _with_retry(lambda: sum(plan_bytes(s, transport=tr) for s in todo),
                                   a.attempts, a.backoff_s, sleep, "plan")
        print(f"plan: {len(todo)} sequence(s), {total / 1e9:.2f} GB to transfer", flush=True)
        check_budget(total, a.max_gb * 1e9, a.yes)
        for s in a.sequences:
            current = [s]
            print(f"fetch {s} ...", flush=True)
            out, calls = _with_retry(lambda: fetch_sequence(s, a.out_root, transport=tr),
                                     a.attempts, a.backoff_s, sleep, s)
            _write_status(a.status_dir, s, True, "ok", calls)
            print(f"  ok -> {out}", flush=True)
    except FetchUnavailable as e:
        for s in current:
            _write_status(a.status_dir, s, False, "unavailable", getattr(e, "calls", calls), str(e))
        print(f"fetch_euroc: FAIL (data unavailable after {a.attempts} attempts, "
              f"not a quality result): {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (FetchError, ValueError) as e:
        for s in current:
            _write_status(a.status_dir, s, False, "error", max(calls, 1), str(e))
        print(f"fetch_euroc: FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
