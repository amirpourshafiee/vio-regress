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
    assert inp["sequences"]["default"].split() == ["MH_01_easy", "V1_01_easy", "MH_03_medium"]
    thr = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())
    assert set(inp["sequences"]["default"].split()) == set(thr["sequences"]), \
        "default must cover every baseline sequence (the ATE geomean needs all of them)"
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


# ---------------------------------------------------------------- S5b: publish-image.yml
PUB = ROOT / ".github" / "workflows" / "publish-image.yml"
ALL_WF = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
REPO_URL = "https://github.com/amirpourshafiee/vio-regress"


@pytest.fixture(scope="module")
def pub():
    return yaml.safe_load(PUB.read_text())


def test_publish_trigger_is_manual_only(pub):
    assert set(triggers(pub)) == {"workflow_dispatch"}
    text = PUB.read_text()
    for word in ("schedule", "cron", "pull_request", "pull_request_target", "push:"):
        assert not re.search(rf"^\s*#?\s*-?\s*{word}\s*:?", text, re.M), word


def test_publish_permissions_exactly_contents_read_packages_write(pub):
    assert pub["permissions"] == {"contents": "read", "packages": "write"}
    for job in pub["jobs"].values():
        assert "permissions" not in job


@pytest.mark.parametrize("path", ALL_WF, ids=lambda p: p.name)
def test_no_pull_request_target_anywhere(path):
    assert "pull_request_target" not in path.read_text()


@pytest.mark.parametrize("path", ALL_WF, ids=lambda p: p.name)
def test_no_expressions_inside_run_blocks(path):
    wf = yaml.safe_load(path.read_text())
    for job in wf["jobs"].values():
        for st in job.get("steps", []):
            assert "${{" not in st.get("run", ""), f"{path.name}: {st.get('name')}"


def test_publish_secrets_only_github_token_via_env(pub):
    text = PUB.read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}
    for st in steps(pub):
        for k, v in (st.get("with") or {}).items():
            assert "secrets." not in str(v), f"secret passed via with: in {st.get('name')}"
        if "secrets." in str(st.get("env", {})):
            assert list(st["env"].values()).count("${{ secrets.GITHUB_TOKEN }}") == 1


def test_publish_actions_pinned_to_full_sha_with_version_comment():
    lines = [l for l in PUB.read_text().splitlines() if re.match(r"^\s*-?\s*uses:", l)]
    assert lines
    for l in lines:
        assert re.search(r"@[0-9a-f]{40}\s+#\s*v\d+\.\d+\.\d+\s*$", l), l


def test_publish_starts_with_fingerprint_and_same_free_disk_step(pub, wf):
    st = steps(pub)
    assert "/proc/cpuinfo" in st[0].get("run", "") and "model name" in st[0]["run"]
    def free(s):
        return next(x for x in s if x.get("name", "").startswith("Free disk"))
    assert free(st)["run"] == free(steps(wf))["run"]


def test_publish_builds_pinned_dockerfile_tags_and_logs_in_safely(pub):
    run = "\n".join(s.get("run", "") for s in steps(pub))
    assert "docker/Dockerfile.openvins" in run
    assert "--build-arg OV_REF" not in run  # OV_REF stays the value pinned in the Dockerfile
    assert "ghcr.io/${image_owner}/vioreg-openvins:${GITHUB_SHA::7}" in run
    assert 'image_owner="${OWNER,,}"' in run  # GHCR repository names must be lowercase
    assert "--password-stdin" in run and "-p " not in run.replace("--password-stdin", "")
    assert "docker push" in run


def test_publish_reports_registry_digest_as_output_and_summary(pub):
    job = next(iter(pub["jobs"].values()))
    push = next(s for s in steps(pub) if "docker push" in s.get("run", ""))
    assert push.get("id")
    assert "digest=" in push["run"] and "$GITHUB_OUTPUT" in push["run"]
    assert "$GITHUB_STEP_SUMMARY" in push["run"]
    assert "sha256:[0-9a-f]{64}" in push["run"]  # the digest is validated before it is reported
    assert job["outputs"]["digest"] == f"${{{{ steps.{push['id']}.outputs.digest }}}}"


def test_publish_never_touches_package_visibility():
    # code only: a comment may say that visibility is left alone
    text = "\n".join(l for l in PUB.read_text().lower().splitlines()
                     if not l.lstrip().startswith("#"))
    assert "visibility" not in text and "/packages/" not in text and "gh api" not in text


def test_dockerfile_source_label_points_at_this_repo():
    text = (ROOT / "docker" / "Dockerfile.openvins").read_text()
    m = re.search(r'org\.opencontainers\.image\.source="([^"]+)"', text)
    assert m and m.group(1) == REPO_URL


# ---------------------------------------------------------------- S5b phase 2: pinned image
def _step_with(wf, needle):
    found = [s for s in steps(wf) if needle in s.get("run", "")]
    assert len(found) == 1, f"expected one step running {needle!r}, found {len(found)}"
    return found[0]


def test_nightly_pulls_the_pinned_ref_and_never_builds(wf):
    text = WF.read_text()
    assert "docker build" not in text and "Dockerfile" not in text
    run = _step_with(wf, "docker pull")["run"]
    assert "docker/IMAGE_REF" in run
    assert "@sha256:[0-9a-f]{64}" in run, "the ref must be validated as digest-pinned"
    assert "RepoDigests" in run, "the pulled digest must be checked against the pinned one"
    assert "GITHUB_ENV" in run and "IMAGE=" in run
    (job,) = wf["jobs"].values()
    assert "IMAGE" not in job.get("env", {}), "IMAGE comes only from docker/IMAGE_REF"


def test_nightly_needs_no_package_permission(wf):
    text = WF.read_text()
    assert "packages:" not in text and "docker login" not in text
    assert "secrets." not in text


def test_nightly_loops_per_sequence_fetch_run_score_delete(wf):
    run = _step_with(wf, "scripts/run_sequence.sh")["run"]
    order = ["scripts/fetch_euroc.sh", "scripts/run_sequence.sh", "-m vioreg.score", "rm -f"]
    pos = [run.find(x) for x in order[:-1]] + [run.rfind(order[-1])]  # last rm, not the trap
    assert all(p >= 0 for p in pos) and pos == sorted(pos), dict(zip(order, pos))
    assert 'for s in "${seq_list[@]}"' in run
    assert run.count("df -h /") >= 4, "df -h at each phase"
    assert "trap" in run, "the bag is deleted even if a phase fails"
    assert '.bag' in run
    assert "rc=1" in run and "exit $rc" in run, "a failed phase fails the step after the loop"


def test_nightly_score_uses_the_pulled_image(wf):
    run = _step_with(wf, "scripts/run_sequence.sh")["run"]
    assert '--image "$IMAGE"' in run
