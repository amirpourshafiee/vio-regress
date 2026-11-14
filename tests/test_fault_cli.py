"""fault_cli (S6): output only under data/faulted/<seq>__<fault>_<params>/, never inside the
original data, original checksum unchanged, one manifest row per faulted copy.

The container step is replaced by a fake `apply` that writes a small file, so these tests
are fast and need no Docker; the real container run is exercised by hand (docs/faults.md).
"""
import csv
import hashlib
import json

import pytest

from vioreg import fault_cli as fc


@pytest.fixture
def repo(tmp_path):
    data = tmp_path / "data"
    seq = data / "euroc" / "MH_01_easy"
    seq.mkdir(parents=True)
    (seq / "MH_01_easy.bag").write_bytes(b"ORIGINAL-BAG" * 1000)
    (data / "faulted").mkdir()
    return tmp_path


def fake_apply(log=None, mutate_original=False):
    def apply(src_bag, out_bag, fault, params, seed):
        if log is not None:
            log.append((src_bag, out_bag, fault, params, seed))
        if mutate_original:
            src_bag.write_bytes(b"CHANGED")
        out_bag.write_bytes(b"FAULTED")
        return {"counts_equal": True, "topics": {}}
    return apply


def call(repo, argv, **kw):
    return fc.main(argv, repo_root=repo, apply=kw.pop("apply", fake_apply()),
                   now=lambda: "2026-11-14T12:00:00Z", **kw)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest_rows(repo):
    with open(repo / "labels" / "fault_manifest.csv", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- happy path
def test_time_offset_writes_copy_under_data_faulted_and_a_manifest_row(repo):
    orig = repo / "data" / "euroc" / "MH_01_easy" / "MH_01_easy.bag"
    before = sha(orig)
    log = []
    rc = call(repo, ["time-offset", "--offset-ms", "5", "--seq", "MH_01_easy"],
              apply=fake_apply(log))
    assert rc == 0
    run_id = "MH_01_easy__time-offset_offset_ms5"
    out = repo / "data" / "faulted" / run_id / "MH_01_easy" / "MH_01_easy.bag"
    assert out.read_bytes() == b"FAULTED"
    assert sha(orig) == before, "original bag unchanged"
    assert log[0][2:] == ("time-offset", {"offset_ms": 5.0}, 0)
    rows = manifest_rows(repo)
    assert rows == [{"run_id": run_id, "sequence": "MH_01_easy", "fault": "time-offset",
                     "params": json.dumps({"offset_ms": 5.0}, sort_keys=True), "seed": "0",
                     "created_utc": "2026-11-14T12:00:00Z"}]
    meta = json.loads((repo / "data" / "faulted" / run_id / "fault.json").read_text())
    assert meta["run_id"] == run_id and meta["original_sha256"] == before


@pytest.mark.parametrize("argv, run_id", [
    (["imu-noise", "--sigma-w", "0.01", "--sigma-a", "0.2", "--seed", "7"],
     "MH_01_easy__imu-noise_sigma_w0.01-sigma_a0.2-seed7"),
    (["gamma", "--gamma", "2.2"], "MH_01_easy__gamma_gamma2.2"),
    (["blur", "--length", "9"], "MH_01_easy__blur_length9"),
    (["drop-frames", "--keep-every", "2"], "MH_01_easy__drop-frames_keep_every2"),
    (["time-offset", "--offset-ms", "-1.5"], "MH_01_easy__time-offset_offset_ms-1.5"),
    (["identity"], "MH_01_easy__identity_none"),
])
def test_run_ids_are_deterministic_and_named_by_params(repo, argv, run_id):
    assert call(repo, [*argv, "--seq", "MH_01_easy"]) == 0
    assert (repo / "data" / "faulted" / run_id / "MH_01_easy" / "MH_01_easy.bag").exists()
    assert manifest_rows(repo)[-1]["run_id"] == run_id


def test_manifest_appends_and_keeps_one_header(repo):
    call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"])
    call(repo, ["blur", "--length", "9", "--seq", "MH_01_easy"])
    text = (repo / "labels" / "fault_manifest.csv").read_text().splitlines()
    assert text[0] == "run_id,sequence,fault,params,seed,created_utc"
    assert len(text) == 3 and sum(l.startswith("run_id,") for l in text) == 1


# ---------------------------------------------------------------- refusals
def test_refuses_an_output_root_other_than_data_faulted(repo):
    other = repo / "elsewhere"
    other.mkdir()
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy",
                       "--out-root", str(other)]) == 2
    assert not any(other.iterdir())
    assert not (repo / "labels" / "fault_manifest.csv").exists()


def test_refuses_data_faulted_that_resolves_into_the_original_data(repo):
    (repo / "data" / "faulted").rmdir()
    (repo / "data" / "faulted").symlink_to(repo / "data" / "euroc")  # a trap
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"]) == 2
    assert sorted(p.name for p in (repo / "data" / "euroc").iterdir()) == ["MH_01_easy"]


def test_refuses_to_overwrite_an_existing_faulted_copy(repo):
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"]) == 0
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"]) == 2
    assert len(manifest_rows(repo)) == 1


def test_refuses_a_missing_original(repo):
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "V1_01_easy"]) == 2


@pytest.mark.parametrize("bad", [["gamma", "--gamma", "0"], ["blur", "--length", "4"],
                                 ["time-offset", "--offset-ms", "0"],
                                 ["drop-frames", "--keep-every", "1"]])
def test_bad_parameters_create_nothing(repo, bad):
    assert call(repo, [*bad, "--seq", "MH_01_easy"]) == 2
    assert not any((repo / "data" / "faulted").iterdir())


def test_unknown_sequence_name_is_refused(repo):
    with pytest.raises(SystemExit):
        call(repo, ["gamma", "--gamma", "2.2", "--seq", "../../etc"])


def test_changed_original_checksum_fails_and_removes_the_copy(repo):
    rc = call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"],
              apply=fake_apply(mutate_original=True))
    assert rc == 1
    assert not (repo / "data" / "faulted" / "MH_01_easy__gamma_gamma2.2").exists()
    assert not (repo / "labels" / "fault_manifest.csv").exists()


def test_failed_apply_leaves_no_copy_and_no_manifest_row(repo):
    def boom(*a):
        raise RuntimeError("container failed")
    assert call(repo, ["gamma", "--gamma", "2.2", "--seq", "MH_01_easy"], apply=boom) == 1
    assert not any((repo / "data" / "faulted").iterdir())
    assert not (repo / "labels" / "fault_manifest.csv").exists()


# ---------------------------------------------------------------- the container command
def test_docker_command_mounts_original_read_only_and_runs_the_pinned_ref(repo, monkeypatch):
    ref = "ghcr.io/amirpourshafiee/vioreg-openvins@sha256:" + "c" * 64
    src = repo / "data" / "euroc" / "MH_01_easy" / "MH_01_easy.bag"
    out = repo / "data" / "faulted" / "x" / "MH_01_easy" / "MH_01_easy.bag"
    cmd = fc.docker_cmd(ref, src, out, "gamma", {"gamma": 2.2}, 0, repo_root=repo, uid=1, gid=2)
    assert cmd[:3] == ["docker", "run", "--rm"] and "--network" in cmd
    assert f"{src.parent}:/in:ro" in cmd
    assert f"{out.parent}:/out" in cmd and f"{repo / 'src'}:/harness:ro" in cmd
    i = cmd.index(ref)
    assert cmd[i + 1:i + 4] == ["python3", "-m", "vioreg.bagfault"]
    assert "--params" in cmd and json.loads(cmd[cmd.index("--params") + 1]) == {"gamma": 2.2}


# ---------------------------------------------------------------- spec form (nightly input)
@pytest.mark.parametrize("spec, fault, params", [
    ("gamma=2.2", "gamma", {"gamma": 2.2}),
    ("blur=9", "blur", {"length": 9}),
    ("drop-frames=2", "drop-frames", {"keep_every": 2}),
    ("time-offset=200", "time-offset", {"offset_ms": 200.0}),
    ("time-offset=-5.5", "time-offset", {"offset_ms": -5.5}),
    ("imu-noise=0.01,0.2", "imu-noise", {"sigma_w": 0.01, "sigma_a": 0.2}),
])
def test_parse_spec(spec, fault, params):
    assert fc.parse_spec(spec) == (fault, params)


@pytest.mark.parametrize("bad", [
    "", "gamma", "gamma=", "gamma=2.2;id", "gamma=$(id)", "gamma=2.2 ", " gamma=2.2",
    "blur=9.5", "blur=-9", "drop-frames=x", "identity", "identity=1", "imu-noise=0.01",
    "imu-noise=0.01,0.2,3", "time-offset=1e3", "GAMMA=2.2", "gamma=2.2\nblur=9",
    "gamma=12345.6", "gamma=0", "blur=4",
])
def test_parse_spec_refuses_anything_off_the_allow_list(bad):
    with pytest.raises((fc.Refused, fc.bf.BagFaultError)):
        fc.parse_spec(bad)


def test_spec_pattern_is_the_one_the_workflow_uses():
    import re
    from pathlib import Path
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "nightly.yml").read_text()
    assert f"fre='{fc.SPEC_PATTERN}'" in wf


def test_spec_subcommand_creates_the_same_copy_and_prints_its_data_root(repo, capsys):
    assert call(repo, ["spec", "blur=9", "--seq", "MH_01_easy", "--print-data-root"]) == 0
    root = repo / "data" / "faulted" / "MH_01_easy__blur_length9"
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(root.resolve())
    assert (root / "MH_01_easy" / "MH_01_easy.bag").exists()
    assert manifest_rows(repo)[-1]["fault"] == "blur"


def test_spec_subcommand_refuses_a_bad_spec(repo):
    assert call(repo, ["spec", "blur=9;rm -rf /", "--seq", "MH_01_easy"]) == 2
    assert not any((repo / "data" / "faulted").iterdir())
