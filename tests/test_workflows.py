"""Invariants of .github/workflows/nightly.yml (S5a). Cheap, no network; actionlint (if
installed, scripts/check_workflows.sh) covers syntax and expressions."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "nightly.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WF.read_text())


def triggers(wf):
    return wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as boolean True


def steps(wf):
    (job,) = wf["jobs"].values()
    return job["steps"]


def test_manual_trigger_only_no_schedule(wf):
    t = triggers(wf)
    assert set(t) == {"workflow_dispatch"}
    # no schedule: key, live or commented out (a prose mention of the word is fine)
    assert not re.search(r"^\s*#?\s*schedule\s*:", WF.read_text(), re.M)
    assert not re.search(r"^\s*#?\s*-?\s*cron\s*:", WF.read_text(), re.M)


def test_inputs_and_defaults(wf):
    inp = triggers(wf)["workflow_dispatch"]["inputs"]
    assert inp["sequences"]["default"] == "MH_01_easy"
    assert str(inp["runs"]["default"]) == "2"


def test_least_privilege(wf):
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        assert "permissions" not in job


def test_first_step_fingerprints_the_cpu(wf):
    first = steps(wf)[0]
    run = first.get("run", "")
    assert "model name" in run and "/proc/cpuinfo" in run
    for flag in ("avx", "fma"):
        assert flag in run
    names = [s.get("uses", "") for s in steps(wf)]
    assert names.index(next(n for n in names if n.startswith("actions/checkout"))) > 0
    text = WF.read_text()
    assert "vioreg.hostinfo --out results/host_ci.json" in text


def test_every_referenced_script_and_module_exists(wf):
    text = WF.read_text()
    scripts = set(re.findall(r"bash (scripts/[\w./-]+)", text))
    modules = set(re.findall(r"-m (vioreg\.\w+)", text))
    assert {"scripts/fetch_euroc.sh", "scripts/run_sequence.sh"} <= scripts
    assert {"vioreg.score", "vioreg.report", "vioreg.hostinfo"} <= modules
    for s in scripts:
        assert (ROOT / s).is_file(), s
    for m in modules:
        assert (ROOT / "src" / Path(*m.split("."))).with_suffix(".py").is_file(), m


def test_gate_fails_closed_and_is_scoped(wf):
    gate = next(s for s in steps(wf) if "pytest -m sequence" in s.get("run", ""))
    env = gate.get("env", {})
    assert env.get("HARNESS_REQUIRE_RESULTS") == "1"
    assert env.get("HARNESS_RESULTS") == "results"
    assert "HARNESS_SEQUENCES" in env or "HARNESS_SEQUENCES" in gate["run"]
    assert "--junitxml=junit-gate.xml" in gate["run"]


def test_judging_steps_run_even_after_a_vio_failure(wf):
    st = steps(wf)
    vio = next(i for i, s in enumerate(st) if "scripts/run_sequence.sh" in s.get("run", ""))
    for s in st[vio + 1:]:
        assert s.get("if") in ("${{ !cancelled() }}", "always()"), s.get("name")


def test_upload_always_results_and_junit_never_dataset(wf):
    up = next(s for s in steps(wf) if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert up.get("if") == "always()"
    paths = up["with"]["path"].split()
    assert "results/" in paths and "junit-gate.xml" in paths
    assert not any(p.startswith(("data", "/", "~")) or ".bag" in p for p in paths)


def test_inputs_reach_shell_only_through_env(wf):
    """No ${{ inputs.* }} inside run: blocks (script injection); inputs go via env and are
    validated before use."""
    for s in steps(wf):
        assert "inputs." not in s.get("run", ""), s.get("name")
    assert any("^(MH|V1|V2)_0" in s.get("run", "") for s in steps(wf)), "inputs are validated"


def test_actions_are_pinned_to_major_versions(wf):
    for s in steps(wf):
        if "uses" in s:
            assert re.search(r"@v\d+$", s["uses"]), s["uses"]
