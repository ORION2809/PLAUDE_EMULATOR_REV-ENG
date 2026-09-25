"""V6 compose track -- static checks on docker-compose.yml, docker/**, scripts/** and docs.

No Docker here (none is installed): these tests prove the topology is correct by
construction -- the YAML parses, every path it names exists, the healthchecks
point at endpoints/ports the code really serves, the one-shot job depends on both
services being healthy, the build volume is named, the network is private, and
the reference pin the Dockerfiles carry is the pinned bumble commit.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKER = ROOT / "docker"
SERVICES = ("emulator", "mockcloud", "job")
PY = sys.executable

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "emulator"))

import serve  # noqa: E402  (emulator/serve.py)


@pytest.fixture(scope="module")
def compose() -> dict:
    data = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(data, dict)
    return data


# --- YAML shape ----------------------------------------------------------------
def test_compose_parses_and_declares_the_three_services(compose):
    assert set(compose["services"]) == set(SERVICES)
    assert compose["name"] == "plaud-harness"


def test_job_depends_on_both_services_being_healthy(compose):
    deps = compose["services"]["job"]["depends_on"]
    assert set(deps) == {"emulator", "mockcloud"}
    for name, cond in deps.items():
        assert cond == {"condition": "service_healthy"}, (name, cond)
    # one-shot: behind a profile so `up --wait` stays well defined, never restarted
    assert compose["services"]["job"]["profiles"] == ["check"]
    assert str(compose["services"]["job"]["restart"]) == "no"


def test_named_build_volume_is_mounted_by_every_service(compose):
    assert "harness-build" in compose["volumes"]
    assert compose["volumes"]["harness-build"]["name"] == "plaud-harness-build"
    for svc in SERVICES:
        mounts = compose["services"][svc]["volumes"]
        assert "harness-build:/app/build" in mounts, svc


def test_network_is_private_internal_and_nothing_is_published(compose):
    nets = compose["networks"]
    assert list(nets) == ["harness"]
    assert nets["harness"].get("internal") is True
    assert nets["harness"].get("external") in (None, False)
    for svc in SERVICES:
        s = compose["services"][svc]
        assert s["networks"] == ["harness"], svc
        assert "network_mode" not in s, svc
        assert "ports" not in s, f"{svc}: ports belong in docker/compose.host-ports.yml"
        assert "env_file" not in s and "secrets" not in s, svc


def test_every_service_is_built_locally_from_the_repo_root(compose):
    for svc in SERVICES:
        build = compose["services"][svc]["build"]
        context = (COMPOSE.parent / build["context"]).resolve()
        assert context == ROOT, (svc, context)
        dockerfile = context / build["dockerfile"]
        assert dockerfile.is_file(), (svc, dockerfile)
        assert dockerfile.name == f"Dockerfile.{svc}"
        assert compose["services"][svc]["image"].startswith("plaud-harness/")


# --- Dockerfiles ---------------------------------------------------------------
def _dockerfile(svc: str) -> str:
    return (DOCKER / f"Dockerfile.{svc}").read_text()


@pytest.mark.parametrize("svc", SERVICES)
def test_dockerfile_copy_sources_exist_in_the_build_context(svc):
    text = _dockerfile(svc)
    copies = [line for line in text.splitlines() if line.startswith("COPY ")]
    assert copies, svc
    for line in copies:
        parts = shlex.split(line)[1:]
        sources, dest = parts[:-1], parts[-1]
        assert dest.startswith("/app"), line
        for src in sources:
            assert (ROOT / src).exists(), f"{svc}: COPY source missing: {src}"
    assert "FROM python:3.11-slim" in text
    assert "pip install -e /app/reference/upstream/bumble" in text
    assert "requirements/all.txt" in text


@pytest.mark.parametrize("svc", SERVICES)
def test_dockerfile_entrypoint_paths_exist(svc):
    text = _dockerfile(svc)
    cmd_line = [line for line in text.splitlines() if line.startswith("CMD ")][-1]
    argv = yaml.safe_load(cmd_line[len("CMD "):])
    assert isinstance(argv, list) and argv, cmd_line
    for token in argv:
        if token.startswith("/app/"):
            assert (ROOT / token[len("/app/"):]).exists(), f"{svc}: entrypoint path missing: {token}"
    if svc == "mockcloud":
        assert argv[:3] == ["python", "-m", "mockcloud"] and (ROOT / "mockcloud" / "__main__.py").is_file()
        assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0", "must not bind 127.0.0.1 in a container"


def test_dockerfiles_pin_the_reference_bumble_commit():
    pins = (ROOT / "docs" / "reference-pins.txt").read_text()
    m = re.search(r"^([0-9a-f]{40})\s+https://github.com/google/bumble\.git", pins, re.M)
    assert m, "bumble pin missing from docs/reference-pins.txt"
    pin = m.group(1)
    for svc in SERVICES:
        assert f"ARG BUMBLE_PIN={pin}" in _dockerfile(svc), svc
    # the pretend version handed to setuptools_scm carries the same short sha
    for svc in SERVICES:
        assert f"BUMBLE_VERSION=0.1.dev1+g{pin[:9]}" in _dockerfile(svc), svc


def test_per_dockerfile_ignore_files_exist_and_match():
    texts = {svc: (DOCKER / f"Dockerfile.{svc}.dockerignore").read_text() for svc in SERVICES}
    assert len(set(texts.values())) == 1, "the three ignore files must be identical"
    body = texts["emulator"]
    for must in (".venv", "build", "data", "reference/*", "!reference/upstream/bumble", "*.aar", "*.apk", "r7"):
        assert must in body.splitlines(), must


def test_host_ports_override_parses_and_only_binds_loopback():
    data = yaml.safe_load((DOCKER / "compose.host-ports.yml").read_text())
    for svc, spec in data["services"].items():
        for port in spec["ports"]:
            assert port.startswith("127.0.0.1:"), (svc, port)
    assert data["networks"]["harness"]["internal"] is False


# --- healthchecks reference real endpoints ------------------------------------------
def test_emulator_healthcheck_targets_the_health_port_serve_py_opens(compose):
    svc = compose["services"]["emulator"]
    test = svc["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"]
    code = test[3]
    assert f"('127.0.0.1', {serve.DEFAULT_HEALTH_PORT})" in code
    assert "['ready'] is True" in code
    env = svc["environment"]
    assert int(env["PLAUD_HEALTH_PORT"]) == serve.DEFAULT_HEALTH_PORT
    assert env["PLAUD_HCI_TRANSPORT"] == serve.DEFAULT_HCI_TRANSPORT
    assert env["PLAUD_HCI_MODE"] == serve.DEFAULT_HCI_MODE
    assert set(svc["expose"]) == {"9000", "9001"}
    hci_port = int(serve.DEFAULT_HCI_TRANSPORT.rsplit(":", 1)[1])
    assert hci_port == 9000 and serve.DEFAULT_HEALTH_PORT == 9001
    # the emulator's own status line says ready only after advertising started
    assert serve.READY_MARKER == "EMULATOR_READY"


def test_mockcloud_healthcheck_targets_a_route_the_app_serves(compose):
    svc = compose["services"]["mockcloud"]
    code = svc["healthcheck"]["test"][3]
    m = re.search(r"http://127\.0\.0\.1:(\d+)(/_mock/health)", code)
    assert m, code
    port, path = int(m.group(1)), m.group(2)
    cmd = svc["command"]
    assert int(cmd[cmd.index("--port") + 1]) == port
    assert svc["expose"] == [str(port)]

    import asyncio

    import httpx
    from mockcloud.app import create_app
    from mockcloud.settings import MockSettings
    from mockcloud.state import MockState

    app = create_app(MockSettings(), MockState())
    assert path in app.openapi()["paths"], sorted(p for p in app.openapi()["paths"] if p.startswith("/_mock"))

    async def call() -> httpx.Response:  # in-process, no port bound (same mechanism as tests/test_mockcloud_*.py)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as client:
            return await client.get(path)

    resp = asyncio.run(call())
    assert resp.status_code == 200 and resp.json()["mock"] is True, resp.text


def test_job_environment_points_at_the_service_names_and_ports(compose):
    env = compose["services"]["job"]["environment"]
    assert env["MOCKCLOUD_URL"] == "http://mockcloud:8787"
    assert env["EMULATOR_HEALTH"] == f"emulator:{serve.DEFAULT_HEALTH_PORT}"
    assert env["EMULATOR_HCI"] == "tcp-client:emulator:9000"
    assert env["JOB_OUT"].startswith("/app/build/")
    gates = yaml.safe_load((ROOT / "evals" / "gates.yaml").read_text())
    assert env["JOB_SUITE"] in gates["suites"]
    presets = subprocess.run([PY, "-m", "generator", "presets"], cwd=ROOT, capture_output=True, text=True,
                             env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}, timeout=60)
    assert presets.returncode == 0, presets.stderr
    assert env["JOB_SCENARIO"] in presets.stdout


def test_healthcheck_one_liners_are_valid_python(compose):
    import ast

    for svc in ("emulator", "mockcloud"):
        code = compose["services"][svc]["healthcheck"]["test"][3]
        ast.parse(code)


# --- scripts -------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["scripts/compose-smoke.sh", "scripts/local-up.sh", "docker/job.sh"])
def test_shell_scripts_are_executable_and_parse(rel):
    path = ROOT / rel
    assert path.is_file() and path.stat().st_mode & 0o111, rel
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_compose_smoke_refuses_clearly_when_docker_is_absent():
    import shutil

    if shutil.which("docker"):
        pytest.skip("docker is present; the smoke script would try to build and run the real thing")
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "compose-smoke.sh")], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "docker is not installed" in proc.stderr
    assert "local-up.sh" in proc.stderr


def test_probe_module_help_lists_the_four_probes():
    proc = subprocess.run([PY, str(DOCKER / "probe.py"), "--help"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    for kind in ("tcp", "emulator", "mockcloud", "scan"):
        assert kind in proc.stdout


# --- emulator entrypoint config ------------------------------------------------
def test_serve_resolves_the_recording_in_the_documented_order(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "r6s2_16k_mono.ogg"
    assert serve.resolve_recording({}) == (fixture, "fixture")
    mdir = tmp_path / "meeting"
    (mdir / "device").mkdir(parents=True)
    with pytest.raises(serve.ConfigError):
        serve.resolve_recording({"PLAUD_MEETING_DIR": str(mdir)})  # set but empty is an error, not a fallback
    (mdir / "device" / "recording.ogg").write_bytes(b"OggS" + b"\0" * 60)
    assert serve.resolve_recording({"PLAUD_MEETING_DIR": str(mdir)}) == (mdir / "device" / "recording.ogg", "PLAUD_MEETING_DIR")
    explicit = tmp_path / "x.ogg"
    explicit.write_bytes(b"OggS")
    assert serve.resolve_recording({"PLAUD_MEETING_DIR": str(mdir), "PLAUD_FILE": str(explicit)}) == (explicit, "PLAUD_FILE")
    with pytest.raises(serve.ConfigError):
        serve.resolve_recording({"PLAUD_FILE": str(tmp_path / "missing.ogg")})


def test_serve_config_defaults_and_r7_s13_policy():
    cfg = serve.ServeConfig.from_env({})
    assert cfg.transport == "tcp-server:0.0.0.0:9000"
    assert cfg.mode == "bridge"
    assert (cfg.health_host, cfg.health_port) == ("0.0.0.0", 9001)
    assert cfg.session_id == 1700000000
    assert serve.PORT_VERSION == 7
    assert serve.STREAM_IN_TASK is True and serve.RESPONSE_PACING_S == 0.004
    assert serve.parse_health_port("off") == -1 and serve.parse_health_port("0") == 0
    with pytest.raises(serve.ConfigError):
        serve.ServeConfig.from_env({"PLAUD_HCI_MODE": "radio"})


def test_docs_state_that_v6_was_not_executed_with_docker():
    doc = (ROOT / "docs" / "compose.md").read_text()
    assert "NOT executed" in doc
    assert "LOCAL_UP_LIVE_S" in doc or "measured" in doc.lower()
    assert "HARNESS_POLICY" in doc
