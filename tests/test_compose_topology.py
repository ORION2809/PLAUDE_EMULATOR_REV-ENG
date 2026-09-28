"""V6 compose track -- static checks on docker-compose.yml, docker/**, scripts/** and docs.

No Docker daemon is needed: these tests prove the topology is correct by
construction -- the YAML parses, every path it names exists, the healthchecks
point at endpoints/ports the code really serves, the one-shot job depends on both
services being healthy, the build volume and the network are project-scoped and
private, the served-file knobs reach the emulator, the images can compile the
sdist-only pins, and the Bumble tree the images copy is at the pinned commit.
Where the `docker compose` CLI is on PATH (it resolves config without a daemon)
the resolved config is checked as well.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
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
    for svc in SERVICES:
        mounts = compose["services"][svc]["volumes"]
        assert "harness-build:/app/build" in mounts, svc


def test_network_and_volume_names_are_project_scoped(compose):
    """Review finding C9: an explicit `name:` is global, so `-p other` would share the
    network (duplicate emulator/mockcloud DNS names) and the build volume."""
    assert "name" not in (compose["volumes"]["harness-build"] or {}), compose["volumes"]
    assert "name" not in compose["networks"]["harness"], compose["networks"]


def _compose_cli() -> list[str] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    probe = subprocess.run([docker, "compose", "version"], capture_output=True, text=True, timeout=30)
    return [docker, "compose"] if probe.returncode == 0 else None


def _resolved_config(project: str, **env: str) -> dict:
    cli = _compose_cli()
    if cli is None:
        pytest.skip("docker compose CLI is not on PATH; the static checks above cover the YAML")
    full = {k: v for k, v in os.environ.items() if not k.startswith(("PLAUD_", "COMPOSE_"))}
    full.update(env)
    proc = subprocess.run(cli + ["-f", str(COMPOSE), "-p", project, "--profile", "check", "config", "--format", "json"],
                          capture_output=True, text=True, env=full, cwd=ROOT, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_resolved_config_scopes_names_and_forwards_the_served_file_knobs():
    """`docker compose config` needs no daemon: the names follow the project, and a
    shell PLAUD_MEETING_DIR / PLAUD_FILE reaches the emulator (review findings C9, C4)."""
    cfg = _resolved_config("other-project", PLAUD_MEETING_DIR="/app/build/compose/job/meetings/synth-smoke-s0001",
                           PLAUD_FILE="")
    assert cfg["networks"]["harness"]["name"] == "other-project_harness"
    assert cfg["volumes"]["harness-build"]["name"] == "other-project_harness-build"
    env = cfg["services"]["emulator"]["environment"]
    assert env["PLAUD_MEETING_DIR"] == "/app/build/compose/job/meetings/synth-smoke-s0001", env
    assert env.get("PLAUD_FILE", "") == "", env
    plain = _resolved_config("plaud-harness")
    penv = plain["services"]["emulator"]["environment"]
    assert penv.get("PLAUD_MEETING_DIR", "") == "" and penv.get("PLAUD_FILE", "") == "", penv
    assert plain["networks"]["harness"]["name"] == "plaud-harness_harness"


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


def _prologue(svc: str) -> str:
    text = _dockerfile(svc)
    start = text.index("FROM ")
    end = text.index('VOLUME ["/app/build"]')
    return text[start:end]


def test_dockerfile_prologues_are_identical_and_compile_sdists():
    """Review finding C3: meeteval==0.4.3 is an sdist with mandatory C++ extensions
    (.github/workflows/tests.yml header: "meeteval ships only an sdist"), and
    python:3.11-slim has no compiler. The shared pip layer must bring one, and drop
    it again in the same layer so the images stay slim."""
    prologues = {svc: _prologue(svc) for svc in SERVICES}
    assert len(set(prologues.values())) == 1, "the three prologues must stay byte-identical (shared layers)"
    text = prologues["job"]
    runs = [r for r in re.split(r"\n(?=[A-Z]+ )", text) if r.startswith("RUN ")]
    pip_run = next(r for r in runs if "requirements/all.txt" in r)
    assert "build-essential" in pip_run, pip_run
    assert pip_run.index("build-essential") < pip_run.index("pip install -r /app/requirements/all.txt")
    assert "apt-get purge" in pip_run and pip_run.index("apt-get purge") > pip_run.index("requirements/all.txt")
    assert "rm -rf /var/lib/apt/lists/*" in pip_run
    assert re.search(r"^meeteval==", (ROOT / "requirements" / "all.txt").read_text(), re.M)


def test_every_script_the_job_runs_is_copied_into_the_images():
    """job.sh calls helpers next to itself ($HERE/...); each must exist and be COPY'd."""
    job = (DOCKER / "job.sh").read_text()
    helpers = sorted(set(re.findall(r'"\$HERE/([\w-]+\.(?:py|sh))"', job)))
    assert {"probe.py", "cloud_roundtrip.py"} <= set(helpers), helpers
    for svc in SERVICES:
        copy = next(line for line in _dockerfile(svc).splitlines() if line.startswith("COPY docker/"))
        for name in helpers:
            assert (DOCKER / name).is_file(), name
            assert f"docker/{name}" in copy.split(), (svc, name, copy)


@pytest.mark.parametrize("svc", SERVICES)
def test_dockerfile_entrypoint_paths_exist(svc):
    text = _dockerfile(svc)
    cmd_line = [line for line in text.splitlines() if line.startswith("CMD ")][-1]
    argv = yaml.safe_load(cmd_line[len("CMD "):])
    assert isinstance(argv, list) and argv, cmd_line
    # paths the image makes itself (`RUN mkdir -p /app/build`) need not exist in the
    # checkout: build/ is gitignored, so a fresh clone has none (review follow-up R6)
    created = {tok for line in text.splitlines() if line.startswith("RUN mkdir -p ") for tok in shlex.split(line)[3:]}
    for token in argv:
        if token.startswith("/app/") and token not in created:
            assert (ROOT / token[len("/app/"):]).exists(), f"{svc}: entrypoint path missing: {token}"
    if svc == "mockcloud":
        assert argv[:3] == ["python", "-m", "mockcloud"] and (ROOT / "mockcloud" / "__main__.py").is_file()
        assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0", "must not bind 127.0.0.1 in a container"


def _bumble_pin() -> str:
    pins = (ROOT / "docs" / "reference-pins.txt").read_text()
    m = re.search(r"^([0-9a-f]{40})\s+https://github.com/google/bumble\.git", pins, re.M)
    assert m, "bumble pin missing from docs/reference-pins.txt"
    return m.group(1)


def test_dockerfiles_pin_the_reference_bumble_commit():
    pin = _bumble_pin()
    for svc in SERVICES:
        assert f"ARG BUMBLE_PIN={pin}" in _dockerfile(svc), svc
    # the pretend version handed to setuptools_scm carries the same short sha
    for svc in SERVICES:
        assert f"BUMBLE_VERSION=0.1.dev1+g{pin[:9]}" in _dockerfile(svc), svc


def test_reference_bumble_tree_is_at_the_pinned_commit():
    """Review finding C14: the images COPY reference/upstream/bumble as it is on disk.
    Read-only: `git rev-parse` writes nothing under reference/**. Without a .git (a
    tarball checkout) the editable install's setuptools_scm version carries the sha."""
    pin = _bumble_pin()
    tree = ROOT / "reference" / "upstream" / "bumble"
    if (tree / ".git").exists() and shutil.which("git"):
        head = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        assert head.returncode == 0, head.stderr
        assert head.stdout.strip() == pin, f"reference/upstream/bumble is at {head.stdout.strip()}, pin is {pin}"
    else:
        from importlib.metadata import version

        assert f"+g{pin[:9]}" in version("bumble"), version("bumble")


#: what compose-smoke.sh runs before its Docker checks (the rest are bash builtins)
SMOKE_TOOLS = ("dirname", "awk", "grep", "git", "sh", "cat")


def _smoke_env(tmp_path: Path, path_dirs: list[Path], **extra: str) -> dict[str, str]:
    """A PATH of symlinks to only the tools the script needs, so a real `docker` (on
    GitHub's runners it is /usr/bin/docker) can never leak into these tests."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in SMOKE_TOOLS:
        real = shutil.which(name)
        if real and not (tools / name).exists():
            (tools / name).symlink_to(real)
    no_git = tmp_path / "bumble-without-git"
    no_git.mkdir(exist_ok=True)
    env = {"PATH": ":".join([*(str(d) for d in path_dirs), str(tools)]), "HOME": str(tmp_path),
           "BUMBLE_TREE": str(no_git)}
    env.update(extra)
    return env


def _shim(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    f = d / "docker"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return d


def _smoke(env: dict[str, str]) -> subprocess.CompletedProcess:
    bash = shutil.which("bash") or "/bin/bash"  # resolved on the real PATH; the script's PATH is the shim's
    return subprocess.run([bash, str(ROOT / "scripts" / "compose-smoke.sh")], capture_output=True, text=True,
                          env=env, cwd=ROOT, timeout=60)


def test_compose_smoke_exits_2_when_the_docker_cli_is_missing(tmp_path):
    proc = _smoke(_smoke_env(tmp_path, []))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "docker is not installed" in proc.stderr and "local-up.sh" in proc.stderr


def test_compose_smoke_exits_2_when_the_compose_plugin_is_missing(tmp_path):
    shim = _shim(tmp_path, 'case "$1" in compose) exit 1;; *) exit 0;; esac\n')
    proc = _smoke(_smoke_env(tmp_path, [shim]))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "v2 plugin" in proc.stderr and "local-up.sh" in proc.stderr


def test_compose_smoke_exits_2_when_the_daemon_is_unreachable(tmp_path):
    """Review finding C13: the CLI is installed but no daemon answers (this machine's state)."""
    shim = _shim(tmp_path, 'case "$1" in compose) exit 0;; info) echo "Cannot connect" >&2; exit 1;; *) exit 0;; esac\n')
    proc = _smoke(_smoke_env(tmp_path, [shim]))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "daemon is not reachable" in proc.stderr and "local-up.sh" in proc.stderr


def test_compose_smoke_refuses_a_bumble_tree_off_the_pin(tmp_path):
    """Review finding C14: a tree at another commit must not be baked into images that
    still carry the pin's pretend version. Checked before anything Docker-related."""
    if not shutil.which("git"):
        pytest.fail("git is required to build a stand-in tree")
    tree = tmp_path / "bumble"
    tree.mkdir()
    genv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "no-global-gitconfig")}
    for argv in (["git", "init", "-q"],
                 ["git", "-c", "user.name=harness", "-c", "user.email=harness@example.invalid",
                  "commit", "-q", "--allow-empty", "-m", "not the pin"]):
        r = subprocess.run(argv, cwd=tree, capture_output=True, text=True, env=genv, timeout=30)
        assert r.returncode == 0, r.stderr
    shim = _shim(tmp_path, "exit 0\n")  # would pretend Docker works: the pin check must come first
    proc = _smoke(_smoke_env(tmp_path, [shim], BUMBLE_TREE=str(tree)))
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "not at the pin" in proc.stderr and _bumble_pin() in proc.stderr


def test_compose_smoke_on_this_machine_refuses_or_is_skipped():
    """With the real PATH: exit 2 unless a daemon really answers (then it would build)."""
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "info"], capture_output=True, timeout=60).returncode == 0:
        pytest.skip("docker is present and its daemon answers; the smoke script would build and run the real thing")
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "compose-smoke.sh")], capture_output=True, text=True,
                          timeout=60, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "local-up.sh" in proc.stderr


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
    # in a container the services must bind every interface, explicitly (serve.py's own
    # defaults are loopback, review finding C12); the port is serve.py's default port
    assert env["PLAUD_HCI_TRANSPORT"] == "tcp-server:0.0.0.0:9000"
    assert env["PLAUD_HEALTH_HOST"] == "0.0.0.0"
    assert env["PLAUD_HCI_TRANSPORT"].rsplit(":", 1)[1] == serve.DEFAULT_HCI_TRANSPORT.rsplit(":", 1)[1]
    assert env["PLAUD_HCI_MODE"] == serve.DEFAULT_HCI_MODE
    assert set(svc["expose"]) == {"9000", "9001"}
    hci_port = int(serve.DEFAULT_HCI_TRANSPORT.rsplit(":", 1)[1])
    assert hci_port == 9000 and serve.DEFAULT_HEALTH_PORT == 9001
    # the emulator's own status line says ready only after advertising started
    assert serve.READY_MARKER == "EMULATOR_READY"


def test_emulator_environment_forwards_the_served_file_knobs(compose):
    """Review finding C4: compose never forwards shell variables by itself; without the
    interpolation `PLAUD_MEETING_DIR=... docker compose up -d emulator` served the fixture."""
    env = compose["services"]["emulator"]["environment"]
    assert env["PLAUD_MEETING_DIR"] == "${PLAUD_MEETING_DIR:-}", env
    assert env["PLAUD_FILE"] == "${PLAUD_FILE:-}", env
    # serve.py treats the empty value compose substitutes for an unset variable as unset
    fixture = ROOT / "tests" / "fixtures" / "r6s2_16k_mono.ogg"
    assert serve.resolve_recording({"PLAUD_MEETING_DIR": "", "PLAUD_FILE": ""}) == (fixture, "fixture")


def test_emulator_dockerfile_binds_every_interface_explicitly():
    text = _dockerfile("emulator")
    assert "PLAUD_HCI_TRANSPORT=tcp-server:0.0.0.0:9000" in text
    assert "PLAUD_HEALTH_HOST=0.0.0.0" in text


def test_mockcloud_multipart_chunk_size_makes_the_job_upload_in_parts(compose):
    """The job's cloud round trip must be a genuine multipart upload (>= 2 parts) of a
    smoke meeting's ~49 KB device Ogg; the documented 5 MiB ChunkSize would be one part.
    local-up.sh uses the same value."""
    cmd = compose["services"]["mockcloud"]["command"]
    chunk = int(cmd[cmd.index("--chunk-size") + 1])
    assert 1024 <= chunk <= 24_000, chunk
    local_up = (ROOT / "scripts" / "local-up.sh").read_text()
    assert f'MOCKCLOUD_CHUNK_SIZE:-{chunk}' in local_up
    assert f'"--chunk-size", "{chunk}"' in (DOCKER / "Dockerfile.mockcloud").read_text()


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
    assert float(env["JOB_CLOUD_TIMEOUT"]) > 0
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


def test_probe_module_help_lists_the_four_probes():
    proc = subprocess.run([PY, str(DOCKER / "probe.py"), "--help"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    for kind in ("tcp", "emulator", "mockcloud", "scan"):
        assert kind in proc.stdout
    for flag in ("--port-version", "--company-id", "--address"):
        assert flag in proc.stdout, flag


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


def test_serve_meeting_dir_serves_the_primary_device_recording(tmp_path):
    """Review follow-up R4: with a meeting.json, PLAUD_MEETING_DIR serves the entry
    named by audio.device_primary (as generator.contract.device_primary_path), else
    audio.device.ogg_opus, else device/recording.ogg -- never the first audio.device
    entry, which on a generator 0.2.0 meeting (keys sorted) is the e2ee ciphertext."""
    from generator.contract import device_primary_path

    mdir = tmp_path / "meeting"
    (mdir / "device").mkdir(parents=True)
    for name in ("recording_e2ee.bin", "recording.ogg", "recording_stereo.ogg"):
        (mdir / "device" / name).write_bytes(b"OggS" + name.encode())
    device = {"e2ee_ogg": "device/recording_e2ee.bin", "ogg_opus": "device/recording.ogg",
              "ogg_opus_stereo": "device/recording_stereo.ogg"}

    def write(audio: dict) -> dict:
        meeting = {"schema": "plaud-harness/meeting/1", "meeting_id": "m", "audio": audio}
        (mdir / "meeting.json").write_text(json.dumps(meeting, indent=2, sort_keys=True) + "\n")
        return json.loads((mdir / "meeting.json").read_text())

    env = {"PLAUD_MEETING_DIR": str(mdir)}
    m = write({"device": device, "device_primary": "ogg_opus"})
    assert next(iter(m["audio"]["device"])) == "e2ee_ogg"  # what "the first entry" would have served
    assert serve.resolve_recording(env) == (mdir / device_primary_path(m), "PLAUD_MEETING_DIR")
    assert serve.resolve_recording(env)[0] == mdir / "device" / "recording.ogg"
    # a stereo meeting names the stereo Ogg as its primary recording
    m = write({"device": device, "device_primary": "ogg_opus_stereo"})
    assert serve.resolve_recording(env) == (mdir / "device" / "recording_stereo.ogg", "PLAUD_MEETING_DIR")
    assert serve.resolve_recording(env)[0] == mdir / device_primary_path(m)
    # no device_primary (pre-0.2.0): audio.device.ogg_opus; neither: device/recording.ogg
    write({"device": {"e2ee_ogg": "device/recording_e2ee.bin", "ogg_opus": "device/recording_stereo.ogg"}})
    assert serve.resolve_recording(env)[0] == mdir / "device" / "recording_stereo.ogg"
    write({"device": {"e2ee_ogg": "device/recording_e2ee.bin"}})
    assert serve.resolve_recording(env)[0] == mdir / "device" / "recording.ogg"
    # contract violations are configuration errors, never a silent fallback
    for bad in ({"device": device, "device_primary": "missing"},
                {"device": {**device, "ogg_opus": "../outside.ogg"}, "device_primary": "ogg_opus"},
                {"device": {**device, "ogg_opus": str(mdir / "device" / "recording.ogg")}}):
        write(bad)
        with pytest.raises(serve.ConfigError):
            serve.resolve_recording(env)
    (mdir / "meeting.json").write_text("{not json")
    with pytest.raises(serve.ConfigError, match="meeting.json"):
        serve.resolve_recording(env)
    # the primary file named but not on disk is an error too
    write({"device": device, "device_primary": "ogg_opus"})
    (mdir / "device" / "recording.ogg").unlink()
    with pytest.raises(serve.ConfigError, match="device/recording.ogg"):
        serve.resolve_recording(env)


def test_serve_config_defaults_and_r7_s13_policy():
    cfg = serve.ServeConfig.from_env({})
    # loopback by default: a bare run on a laptop must not expose the HCI server and the
    # served file to the LAN (review finding C12); compose and the image set 0.0.0.0
    assert cfg.transport == "tcp-server:127.0.0.1:9000"
    assert cfg.mode == "bridge"
    assert (cfg.health_host, cfg.health_port) == ("127.0.0.1", 9001)
    assert cfg.session_id == 1700000000
    assert serve.PORT_VERSION == 7
    assert serve.STREAM_IN_TASK is True and serve.RESPONSE_PACING_S == 0.004
    assert serve.parse_health_port("off") == -1 and serve.parse_health_port("0") == 0
    with pytest.raises(serve.ConfigError):
        serve.ServeConfig.from_env({"PLAUD_HCI_MODE": "radio"})


def test_health_turns_not_ready_when_the_peripheral_is_stuck():
    """Review finding C1: health used to say ready:true while the peripheral was held by a
    departed central. The bridge now prevents that state; if it happens anyway (or the
    advertiser dies) `ready` turns false after serve.STALE_FAULT_S, naming the fault."""
    from types import SimpleNamespace as NS

    adv = NS(enabled=True)
    ctrl = NS(le_legacy_advertiser=adv, advertising_sets={})
    device = NS(connections={})
    bridge = NS(clients={}, accepted=0)
    live = serve.Liveness(device, ctrl, bridge)

    live.tick(now=100.0)
    h = live.live(now=100.0)
    assert h["ready"] is True and h["advertising"] is True and h["hci_clients"] == 0
    assert h["hci_client_attached"] is False and h["peripheral_connections"] == 0

    # a live session: connected, a client attached -> not a fault
    adv.enabled, device.connections, bridge.clients = False, {1: object()}, {1: object()}
    live.tick(now=101.0)
    assert live.live(now=200.0)["ready"] is True

    # the client is gone but the peripheral is still connected (the old stale link)
    bridge.clients = {}
    live.tick(now=300.0)
    assert live.live(now=300.0 + serve.STALE_FAULT_S - 0.1)["ready"] is True  # grace window
    h = live.live(now=300.0 + serve.STALE_FAULT_S + 0.5)
    assert h["ready"] is False and "no HCI client attached" in h["fault"], h

    # recovered: advertising again, nothing connected
    adv.enabled, device.connections = True, {}
    live.tick(now=310.0)
    assert live.live(now=310.0)["ready"] is True and "fault" not in live.live(now=310.0)

    # neither connected nor advertising (the advertiser died)
    adv.enabled = False
    live.tick(now=400.0)
    h = live.live(now=400.0 + serve.STALE_FAULT_S + 1)
    assert h["ready"] is False and "neither connected nor advertising" in h["fault"], h


def test_bridge_transport_spec_parsing():
    assert serve.parse_tcp_server_spec("tcp-server:0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert serve.parse_tcp_server_spec("tcp-server:_:9000") == (None, 9000)
    assert serve.parse_tcp_server_spec("tcp-server:127.0.0.1:0") == ("127.0.0.1", 0)
    assert serve.parse_tcp_server_spec("pty:/tmp/x") is None
    with pytest.raises(serve.ConfigError):
        serve.parse_tcp_server_spec("tcp-server:nohostport")


def test_docs_state_whether_v6_was_executed_with_docker():
    """Either the doc says V6 was NOT executed with Docker, or it records a measured
    `up_wait_s=` from compose-smoke.sh; plus the measured venv timings either way."""
    doc = (ROOT / "docs" / "compose.md").read_text()
    not_run = "NOT executed" in doc
    measured = re.search(r"up_wait_s=[0-9.]+", doc) is not None
    assert not_run or measured, "docs/compose.md must say whether V6 ran under Docker"
    assert re.search(r"LOCAL_UP_LIVE_S=[0-9.]+", doc)
    assert "HARNESS_POLICY" in doc
