"""V6 compose track -- the pieces that can run here without Docker.

* emulator/serve.py starts, prints its READY line and opens the HCI TCP server and the
  health port within 20 s; a Bumble central attached through the bridged controller
  receives the PlaudPeripheral advertisement (portVersion 7).
* `python -m mockcloud` starts and answers GET /_mock/health within 20 s.
* scripts/local-up.sh (the whole topology on the venv) completes within 60 s.

Every subprocess uses ephemeral ports so the suite never collides with a running
local-up.sh or compose stack. Tests skip with a reason when the environment cannot
spawn processes.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PROBE = ROOT / "docker" / "probe.py"
SERVE = ROOT / "emulator" / "serve.py"
READY_MARKER = "EMULATOR_READY"

BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONPATH": f"{ROOT}:{ROOT / 'emulator'}",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(argv: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    try:
        return subprocess.Popen(argv, stdout=log.open("wb"), stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    except (OSError, PermissionError) as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot spawn subprocesses here: {exc}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)


def _wait_for(pred, timeout: float, what: str, proc: subprocess.Popen | None = None, log: Path | None = None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            pytest.fail(f"{what}: process exited early rc={proc.returncode}\n{log.read_text() if log else ''}")
        try:
            return pred()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.2)
    pytest.fail(f"{what}: not ready within {timeout}s (last error: {last!r})\n{log.read_text() if log else ''}")


def _probe(kind: str, target: str, timeout: float, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    argv = [PY, str(PROBE), kind, target, "--timeout", str(timeout)] + (extra or [])
    return subprocess.run(argv, capture_output=True, text=True, env=BASE_ENV, cwd=ROOT, timeout=timeout + 30)


def _read_health(port: int) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.settimeout(3)
        return json.loads(s.makefile("rb").readline())


# --- emulator ------------------------------------------------------------------
def test_emulator_serve_opens_its_ports_and_advertises_within_20s(tmp_path):
    hci_port, health_port = free_port(), free_port()
    env = dict(BASE_ENV, PLAUD_HCI_TRANSPORT=f"tcp-server:127.0.0.1:{hci_port}", PLAUD_HEALTH_HOST="127.0.0.1",
               PLAUD_HEALTH_PORT=str(health_port))
    log = tmp_path / "emulator.log"
    t0 = time.monotonic()
    proc = _spawn([PY, str(SERVE)], env, log)
    try:
        status = _wait_for(lambda: _read_health(health_port), 20, "emulator health", proc, log)
        ready_after = time.monotonic() - t0
        assert status["ready"] is True
        assert status["port_version"] == 7
        assert status["mode"] == "bridge"
        assert status["stream_in_task"] is True and status["response_pacing_s"] == 0.004
        assert status["file"].endswith("tests/fixtures/r6s2_16k_mono.ogg") and status["file_source"] == "fixture"
        assert status["file_bytes"] == (ROOT / "tests/fixtures/r6s2_16k_mono.ogg").stat().st_size
        assert status["hci_client_attached"] is False

        # the HCI TCP server is open too
        with socket.create_connection(("127.0.0.1", hci_port), timeout=2):
            pass

        # the health listener also speaks enough HTTP for curl / a browser
        with urllib.request.urlopen(f"http://127.0.0.1:{health_port}/", timeout=3) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["ready"] is True

        # a central attached through the bridged controller sees the peripheral advertise
        scan = _probe("scan", f"tcp-client:127.0.0.1:{hci_port}", 15)
        assert scan.returncode == 0, scan.stdout + scan.stderr
        assert "PROBE_OK kind=scan" in scan.stdout
        detail = json.loads(scan.stdout.split(" ", 4)[4])
        assert detail["address"].upper() == "F0:1A:2B:3C:4D:5E"
        assert detail["port_version"] == 7, detail
        assert detail["company_id"] == "0xFFFF"
        assert detail["connectable"] is True

        # the same predicate the compose healthcheck uses, via the shared probe
        health = _probe("emulator", f"127.0.0.1:{health_port}", 5)
        assert health.returncode == 0, health.stdout + health.stderr
    finally:
        _stop(proc)
    text = log.read_text()
    assert READY_MARKER in text, text
    ready_line = next(line for line in text.splitlines() if READY_MARKER in line)
    assert "pv=7" in ready_line and "stream_in_task=True" in ready_line and "pacing_s=0.004" in ready_line
    assert ready_after < 20


def test_emulator_serve_exits_2_on_a_misconfigured_meeting_dir(tmp_path):
    env = dict(BASE_ENV, PLAUD_MEETING_DIR=str(tmp_path / "nope"), PLAUD_HEALTH_PORT="0",
               PLAUD_HCI_TRANSPORT=f"tcp-server:127.0.0.1:{free_port()}")
    proc = subprocess.run([PY, str(SERVE)], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert proc.returncode == 2
    assert "EMULATOR_CONFIG_ERROR" in proc.stderr and "recording.ogg" in proc.stderr


def test_emulator_serve_serves_a_generated_meeting_directory(tmp_path):
    meeting = next((ROOT / "build" / "synthetic").glob("*/*/device/recording.ogg"), None)
    if meeting is None:
        pytest.skip("no generated meeting under build/synthetic (run scripts/make-synthetic-set.sh)")
    mdir = meeting.parents[1]
    hci_port, health_port = free_port(), free_port()
    env = dict(BASE_ENV, PLAUD_MEETING_DIR=str(mdir), PLAUD_HCI_TRANSPORT=f"tcp-server:127.0.0.1:{hci_port}",
               PLAUD_HEALTH_HOST="127.0.0.1", PLAUD_HEALTH_PORT=str(health_port))
    log = tmp_path / "emulator.log"
    proc = _spawn([PY, str(SERVE)], env, log)
    try:
        status = _wait_for(lambda: _read_health(health_port), 20, "emulator health", proc, log)
        assert status["file"] == str(meeting) and status["file_source"] == "PLAUD_MEETING_DIR"
        assert status["file_bytes"] == meeting.stat().st_size
    finally:
        _stop(proc)


# --- mockcloud -----------------------------------------------------------------
def test_mockcloud_answers_its_health_url_within_20s(tmp_path):
    port = free_port()
    log = tmp_path / "mockcloud.log"
    t0 = time.monotonic()
    proc = _spawn([PY, "-m", "mockcloud", "--host", "127.0.0.1", "--port", str(port)], BASE_ENV, log)
    try:
        def fetch():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/_mock/health", timeout=2) as resp:
                assert resp.status == 200
                return json.loads(resp.read())

        body = _wait_for(fetch, 20, "mockcloud health", proc, log)
        assert body["mock"] is True
        assert time.monotonic() - t0 < 20
        probe = _probe("mockcloud", f"http://127.0.0.1:{port}", 5)
        assert probe.returncode == 0, probe.stdout + probe.stderr
    finally:
        _stop(proc)


# --- the whole topology on the venv --------------------------------------------------
def test_local_up_completes_within_60s(tmp_path):
    script = ROOT / "scripts" / "local-up.sh"
    env = dict(BASE_ENV, EMULATOR_HCI_PORT=str(free_port()), EMULATOR_HEALTH_PORT=str(free_port()),
               MOCKCLOUD_PORT=str(free_port()), LOCAL_UP_OUT=str(tmp_path / "local-up"))
    try:
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=ROOT, timeout=110)
    except (OSError, PermissionError) as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot spawn local-up.sh here: {exc}")
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"local-up.sh did not finish within 110 s\n{exc.stdout}\n{exc.stderr}")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    live = float(re.search(r"^LOCAL_UP_LIVE_S=([0-9.]+)$", proc.stdout, re.M).group(1))
    total = float(re.search(r"^LOCAL_UP_TOTAL_S=([0-9.]+)$", proc.stdout, re.M).group(1))
    assert live <= 60, out
    assert total <= 60, out
    assert "JOB_OK" in proc.stdout and "PROBE_OK kind=scan" in proc.stdout, out
    report = json.loads((tmp_path / "local-up" / "job" / "report.json").read_text())
    assert report.get("schema", "").startswith("plaud-harness/eval-report/"), report.keys()
