"""V6 compose track -- the pieces that can run here without Docker.

* emulator/serve.py starts, prints its READY line and opens the HCI TCP server and the
  health port within 20 s; a Bumble central attached through the bridged controller
  receives the PlaudPeripheral advertisement (portVersion 7).
* The bridge survives centrals that leave badly (review finding C1): one that connects
  and closes TCP without an LE disconnect, one that leaves while scanning, and one that
  dies half-way through an HCI packet. A later probe must still see the advertisement.
* `docker/probe.py scan` is bounded by --timeout even against a TCP listener that never
  speaks HCI (C2), and rejects an advertiser at the right address that is not a
  portVersion-7 PlaudPeripheral (C5).
* `python -m mockcloud` starts and answers GET /_mock/health within 20 s, and
  docker/cloud_roundtrip.py pushes a generated device recording through it
  (identity -> bind -> multipart upload -> transcription) into a hyp.json that evals
  scores with the oracle suite.
* scripts/local-up.sh (the whole topology on the venv) completes within 60 s, refuses a
  port that is already taken (C6), reports a failing job as FAIL / exit 1 (C7) and tears
  its whole process tree down promptly on SIGTERM (C8).

Every subprocess uses ephemeral ports so the suite never collides with a running
local-up.sh or compose stack. Tests skip with a reason when the environment cannot
spawn processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PROBE = ROOT / "docker" / "probe.py"
CLOUD = ROOT / "docker" / "cloud_roundtrip.py"
SERVE = ROOT / "emulator" / "serve.py"
LOCAL_UP = ROOT / "scripts" / "local-up.sh"
READY_MARKER = "EMULATOR_READY"
PERIPHERAL = "F0:1A:2B:3C:4D:5E"

BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONPATH": f"{ROOT}:{ROOT / 'emulator'}",
}

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "emulator"))


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


def _group_alive(pgid: int) -> bool:
    """Any live process left in the group? macOS answers EPERM (not ESRCH) when the
    only members left are zombies, so EPERM counts as "nothing left to signal"."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_group(pgid: int, leader: subprocess.Popen | None = None) -> None:
    """SIGTERM the whole process group, then SIGKILL whatever is left (review finding C8).
    `leader` (the group leader, our child) is reaped as it dies so it is not a zombie."""
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if leader is not None:
                leader.poll()
            if not _group_alive(pgid):
                return
            time.sleep(0.1)


def run_group(argv: list[str], env: dict[str, str], timeout: float) -> tuple[subprocess.CompletedProcess, bool, int]:
    """Like subprocess.run, but the child gets its own session and, on timeout, the
    WHOLE group is killed. subprocess.run(timeout=...) SIGKILLs only the direct child,
    so a script's background services and in-flight job would survive as orphans."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT,
                                start_new_session=True)
    except (OSError, PermissionError) as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot spawn {argv[0]} here: {exc}")
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, proc)
        out, err = proc.communicate(timeout=10)
        timed_out = True
    _kill_group(proc.pid, proc)  # anything the script left behind even on a normal exit
    return subprocess.CompletedProcess(argv, proc.returncode, out, err), timed_out, proc.pid


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


def _probe(kind: str, target: str, timeout: float, extra: list[str] | None = None,
           hard_limit: float | None = None) -> subprocess.CompletedProcess:
    argv = [PY, str(PROBE), kind, target, "--timeout", str(timeout)] + (extra or [])
    return subprocess.run(argv, capture_output=True, text=True, env=BASE_ENV, cwd=ROOT,
                          timeout=hard_limit if hard_limit is not None else timeout + 30)


def _read_health(port: int) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.settimeout(3)
        return json.loads(s.makefile("rb").readline())


class Emulator:
    """emulator/serve.py on ephemeral loopback ports."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        self.hci_port, self.health_port = free_port(), free_port()
        self.log = tmp_path / f"emulator-{self.hci_port}.log"
        full = dict(BASE_ENV, PLAUD_HCI_TRANSPORT=f"tcp-server:127.0.0.1:{self.hci_port}",
                    PLAUD_HEALTH_HOST="127.0.0.1", PLAUD_HEALTH_PORT=str(self.health_port), **env)
        self.proc = _spawn([PY, str(SERVE)], full, self.log)
        self.status = _wait_for(lambda: _read_health(self.health_port), 20, "emulator health", self.proc, self.log)

    @property
    def hci(self) -> str:
        return f"tcp-client:127.0.0.1:{self.hci_port}"

    def health(self) -> dict:
        return _read_health(self.health_port)

    def wait_idle(self, timeout: float = 10.0) -> dict:
        """Health says: advertising again, no peripheral connection, no HCI client."""
        def idle() -> dict:
            h = self.health()
            assert h["advertising"] is True and h["peripheral_connections"] == 0 and h["hci_clients"] == 0, h
            assert h["ready"] is True, h
            return h
        return _wait_for(idle, timeout, "emulator back to idle", self.proc, self.log)

    def stop(self) -> None:
        _stop(self.proc)


@pytest.fixture
def emulator(tmp_path):
    emu = Emulator(tmp_path)
    try:
        yield emu
    finally:
        emu.stop()


def _scan_ok(emu: Emulator, timeout: float = 10.0) -> dict:
    scan = _probe("scan", emu.hci, timeout)
    assert scan.returncode == 0, scan.stdout + scan.stderr + emu.log.read_text()
    assert "PROBE_OK kind=scan" in scan.stdout
    return json.loads(scan.stdout.split(" ", 4)[4])


# --- emulator ------------------------------------------------------------------
def test_emulator_serve_opens_its_ports_and_advertises_within_20s(tmp_path):
    t0 = time.monotonic()
    emu = Emulator(tmp_path)
    try:
        status = emu.status
        ready_after = time.monotonic() - t0
        assert status["ready"] is True
        assert status["port_version"] == 7
        assert status["mode"] == "bridge"
        assert status["stream_in_task"] is True and status["response_pacing_s"] == 0.004
        assert status["file"].endswith("tests/fixtures/r6s2_16k_mono.ogg") and status["file_source"] == "fixture"
        assert status["file_bytes"] == (ROOT / "tests/fixtures/r6s2_16k_mono.ogg").stat().st_size
        assert status["hci_client_attached"] is False and status["hci_clients"] == 0
        assert status["peripheral_connections"] == 0 and status["advertising"] is True
        assert status["pid"] == emu.proc.pid

        # the HCI TCP server is open too
        with socket.create_connection(("127.0.0.1", emu.hci_port), timeout=2):
            pass

        # the health listener also speaks enough HTTP for curl / a browser
        with urllib.request.urlopen(f"http://127.0.0.1:{emu.health_port}/", timeout=3) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["ready"] is True

        # a central attached through the bridged controller sees the peripheral advertise
        detail = _scan_ok(emu, 15)
        assert detail["address"].upper() == PERIPHERAL
        assert detail["port_version"] == 7, detail
        assert detail["company_id"] == "0xFFFF"
        assert detail["connectable"] is True

        # the same predicate the compose healthcheck uses, via the shared probe
        health = _probe("emulator", f"127.0.0.1:{emu.health_port}", 5)
        assert health.returncode == 0, health.stdout + health.stderr
    finally:
        emu.stop()
    text = emu.log.read_text()
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


def test_emulator_serve_serves_a_generated_meeting_directory(tmp_path, cloud_meeting):
    """Review follow-up R4: PLAUD_MEETING_DIR serves the meeting's primary device
    recording (audio.device_primary, generator.contract.device_primary_path). A
    generator 0.2.0 meeting.json lists audio.device sorted, e2ee_ogg (ciphertext)
    first, so "the first entry" would be the wrong file."""
    import hashlib

    from generator.contract import device_primary_path

    mdir, meeting = cloud_meeting
    audio = meeting["audio"]
    assert audio["device_primary"] == "ogg_opus" and next(iter(audio["device"])) == "e2ee_ogg", audio["device"]
    served = mdir / device_primary_path(meeting)
    assert served == mdir / "device" / "recording.ogg"
    emu = Emulator(tmp_path, PLAUD_MEETING_DIR=str(mdir))
    try:
        assert emu.status["file"] == str(served) and emu.status["file_source"] == "PLAUD_MEETING_DIR"
        assert emu.status["file_bytes"] == served.stat().st_size
        assert emu.status["file_sha256"] == hashlib.sha256(served.read_bytes()).hexdigest()
        assert emu.status["file_sha256"] == audio["device_details"]["ogg_opus"]["sha256"]
    finally:
        emu.stop()


# --- C1: centrals that leave badly must not break the bridge ---------------------------
async def _central(emu: Emulator, address: str):
    from bumble.device import Device
    from bumble.hci import Address
    from bumble.transport import open_transport

    transport = await asyncio.wait_for(open_transport(emu.hci), 10)
    device = Device.with_hci("walk-away", Address(address), transport.source, transport.sink)
    await asyncio.wait_for(device.power_on(), 10)
    return transport, device


def test_bridge_recovers_after_a_central_connects_and_walks_away(emulator):
    """docs/compose.md section 4's snippet: connect, then close TCP without an LE disconnect."""
    _scan_ok(emulator)

    async def connect_and_walk_away() -> dict:
        transport, device = await _central(emulator, "F0:F1:F2:F3:F4:F7")
        conn = await asyncio.wait_for(device.connect(PERIPHERAL), 10)
        assert conn.handle is not None
        during = emulator.health()
        await transport.close()  # no HCI_Disconnect, no stop: the process just goes away
        return during

    during = asyncio.run(connect_and_walk_away())
    detail = _scan_ok(emulator)  # the symptom: the peripheral never advertised again
    assert detail["port_version"] == 7
    _scan_ok(emulator)  # and again: nothing accumulates
    assert during.get("peripheral_connections") == 1 and during.get("hci_clients") == 1, during
    assert during["ready"] is True, during  # a live session is not a fault
    emulator.wait_idle()


def test_bridge_recovers_after_a_central_leaves_while_scanning(emulator):
    async def scan_and_walk_away() -> None:
        transport, device = await _central(emulator, "F0:F1:F2:F3:F4:F8")
        await device.start_scanning()
        await asyncio.sleep(0.5)
        await transport.close()  # le_scan_enable stays set on a shared controller

    asyncio.run(scan_and_walk_away())
    _scan_ok(emulator)  # the symptom: LE_Set_Scan_Parameters -> COMMAND_DISALLOWED
    _scan_ok(emulator)
    emulator.wait_idle()


def test_bridge_recovers_after_a_client_dies_mid_packet(emulator):
    """Two bytes of an HCI command, then gone: a shared stream parser would stay misaligned."""
    with socket.create_connection(("127.0.0.1", emulator.hci_port), timeout=2) as s:
        s.sendall(b"\x01\x03")
    _scan_ok(emulator)  # the symptom: the next central's HCI_Reset is swallowed
    emulator.wait_idle()


def test_bridge_serves_two_centrals_at_once(emulator):
    """Each client gets its own controller: a second central attaching does not detach the first."""
    async def two() -> None:
        t1, d1 = await _central(emulator, "F0:F1:F2:F3:F4:F9")
        t2, d2 = await _central(emulator, "F0:F1:F2:F3:F4:FA")
        seen: dict[str, asyncio.Future] = {}
        for name, dev in (("one", d1), ("two", d2)):
            fut = asyncio.get_running_loop().create_future()
            seen[name] = fut
            dev.on(dev.EVENT_ADVERTISEMENT,
                   lambda adv, f=fut: f.done() or adv.address.to_string(False) != PERIPHERAL or f.set_result(True))
            await dev.start_scanning()
        await asyncio.wait_for(asyncio.gather(*seen.values()), 10)
        assert emulator.health().get("hci_clients") == 2
        await t1.close()
        await t2.close()

    asyncio.run(two())
    _scan_ok(emulator)
    emulator.wait_idle()


# --- C2: the scan probe is bounded -----------------------------------------------------
def test_probe_scan_is_bounded_by_its_timeout_against_a_silent_listener():
    """A TCP port that accepts but never answers HCI (a mistyped EMULATOR_HCI port)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_forever() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                held.append(conn)  # never read, never answered
            except OSError:
                continue

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    t0 = time.monotonic()
    try:
        scan = _probe("scan", f"tcp-client:127.0.0.1:{port}", 3, hard_limit=25)
    finally:
        stop.set()
        thread.join(2)
        for c in held:
            c.close()
        listener.close()
    elapsed = time.monotonic() - t0
    assert scan.returncode == 1, scan.stdout + scan.stderr
    assert "PROBE_FAIL kind=scan" in scan.stderr
    assert elapsed < 3 + 5, f"--timeout 3 took {elapsed:.1f}s"
    assert held, "the probe never even dialled the listener"


# --- C5: the scan probe checks what it claims to check --------------------------------------
def test_probe_scan_rejects_a_wrong_port_version_or_company_id(emulator):
    for extra, needle in ((["--port-version", "9"], "port_version"), (["--company-id", "0x1234"], "company")):
        t0 = time.monotonic()
        scan = _probe("scan", emulator.hci, 4, extra)
        assert scan.returncode == 1, (extra, scan.stdout, scan.stderr)
        assert "PROBE_FAIL kind=scan" in scan.stderr and needle in scan.stderr, scan.stderr
        assert time.monotonic() - t0 < 4 + 8
    _scan_ok(emulator, 10)  # the defaults (0xFFFF, 7) still pass


def test_probe_scan_rejects_an_imposter_at_the_expected_address():
    """A Bumble device at F0:1A:2B:3C:4D:5E advertising only a name: not a PlaudPeripheral."""
    async def main() -> tuple[int, str]:
        from bumble import data_types
        from bumble.controller import Controller
        from bumble.core import AdvertisingData
        from bumble.device import Device, DeviceConfiguration
        from bumble.hci import Address
        from bumble.host import Host
        from bumble.link import LocalLink
        from bumble.transport import open_transport
        from bumble.transport.common import AsyncPipeSink

        port = free_port()
        link = LocalLink()
        own = Controller("imposter", link=link, public_address=PERIPHERAL)
        dev = Device(config=DeviceConfiguration(name="NotAPlaud", address=Address(PERIPHERAL)),
                     host=Host(own, AsyncPipeSink(own)))
        hci = await open_transport(f"tcp-server:127.0.0.1:{port}")
        Controller("central", host_source=hci.source, host_sink=hci.sink, link=link,
                   public_address="F0:F1:F2:F3:F4:F5")
        await dev.power_on()
        dev.advertising_data = bytes(AdvertisingData([data_types.CompleteLocalName("NotAPlaud")]))
        await dev.start_advertising(auto_restart=True)
        try:
            p = await asyncio.create_subprocess_exec(
                PY, str(PROBE), "scan", f"tcp-client:127.0.0.1:{port}", "--timeout", "4",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=BASE_ENV, cwd=str(ROOT))
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            return p.returncode, out.decode()
        finally:
            await hci.close()

    rc, out = asyncio.run(main())
    assert rc == 1, out
    assert "PROBE_FAIL kind=scan" in out and "manufacturer" in out, out


# --- mockcloud -----------------------------------------------------------------
class MockCloud:
    def __init__(self, tmp_path: Path, *args: str) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = tmp_path / f"mockcloud-{self.port}.log"
        self.proc = _spawn([PY, "-m", "mockcloud", "--host", "127.0.0.1", "--port", str(self.port), *args],
                           BASE_ENV, self.log)

        def fetch():
            with urllib.request.urlopen(f"{self.url}/_mock/health", timeout=2) as resp:
                assert resp.status == 200
                return json.loads(resp.read())

        self.health = _wait_for(fetch, 20, "mockcloud health", self.proc, self.log)

    def stop(self) -> None:
        _stop(self.proc)


def test_mockcloud_answers_its_health_url_within_20s(tmp_path):
    t0 = time.monotonic()
    mock = MockCloud(tmp_path)
    try:
        assert mock.health["mock"] is True
        assert time.monotonic() - t0 < 20
        probe = _probe("mockcloud", mock.url, 5)
        assert probe.returncode == 0, probe.stdout + probe.stderr
    finally:
        mock.stop()


@pytest.fixture(scope="module")
def cloud_meeting(tmp_path_factory) -> tuple[Path, dict]:
    from generator.testing import fixture_meeting

    return fixture_meeting(tmp_path_factory.mktemp("compose-cloud"), "smoke", 3)


def _cloud(url: str, meeting_dir: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(CLOUD), "--url", url, "--meeting-dir", str(meeting_dir), "--out", str(out),
                           "--timeout", "30", *extra],
                          capture_output=True, text=True, env=BASE_ENV, cwd=ROOT, timeout=90)


def test_cloud_roundtrip_pushes_a_device_recording_through_the_mock_and_scores_it(tmp_path, cloud_meeting):
    mdir, meeting = cloud_meeting
    mock = MockCloud(tmp_path, "--chunk-size", "20000", "--task-step-seconds", "0.05", "--local-file-root",
                     str(mdir.parent))
    try:
        out = tmp_path / "hyp"
        rt = _cloud(mock.url, mdir, out)
        assert rt.returncode == 0, rt.stdout + rt.stderr + mock.log.read_text()
        line = next(ln for ln in rt.stdout.splitlines() if ln.startswith("CLOUD_OK"))
        fields = dict(kv.split("=", 1) for kv in line.split()[1:])
        assert int(fields["parts"]) >= 2
        assert int(fields["bytes"]) == (mdir / "device" / "recording.ogg").stat().st_size
        # The client polls every 0.05 s and the mock steps every 0.05 s, so a slow
        # runner can poll past STARTED (GitHub macos-latest, 28 Sep 2026: PENDING,SUCCESS).
        # Polled states must be an ordered part of the sequence; the mock's own
        # history must hold every step.
        polled = fields["states"].split(",")
        assert polled[0] == "PENDING" and polled[-1] == "SUCCESS", polled
        assert polled == [st for st in ("PENDING", "STARTED", "SUCCESS") if st in polled], polled
        step = next(ln for ln in rt.stdout.splitlines() if ln.startswith("CLOUD_STEP transcribe"))
        tid = dict(kv.split("=", 1) for kv in step.split()[2:] if "=" in kv)["id"]
        import urllib.request

        with urllib.request.urlopen(f"{mock.url}/_mock/state", timeout=10) as r:
            history = [h[0] for h in json.loads(r.read())["tasks"][tid]["history"]]
        assert history[-2:] == ["STARTED", "SUCCESS"], history
        assert fields["source"].startswith("objectstore+meeting+"), fields
        assert fields["meeting_id"] == meeting["meeting_id"]
        for step in ("identity", "bind", "presign", "upload", "complete", "download", "transcribe", "unbind"):
            assert f"CLOUD_STEP {step}" in rt.stdout, step
        hyp = out / meeting["meeting_id"] / "hyp.json"
        doc = json.loads(hyp.read_text())
        assert doc["schema"] == "plaud-harness/hypothesis/1" and doc["system"] == "mockcloud"
        assert len(doc["segments"]) == len(meeting["segments"])
        # nothing secret on stdout: the synthetic tokens are never printed
        assert "eyJ" not in rt.stdout + rt.stderr

        score = subprocess.run([PY, "-m", "evals", "score", "--ref", str(mdir), "--hyp", str(hyp),
                                "--suite", "oracle", "--report", str(tmp_path / "report.json")],
                               capture_output=True, text=True, env=BASE_ENV, cwd=ROOT, timeout=120)
        assert score.returncode == 0, score.stdout + score.stderr
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["gates"]["passed"] is True, report["gates"]
    finally:
        mock.stop()


def test_cloud_roundtrip_refuses_a_single_part_upload(tmp_path, cloud_meeting):
    """With the documented 5 MiB ChunkSize the ~49 KB file is one part: not the multipart path."""
    mdir, _ = cloud_meeting
    mock = MockCloud(tmp_path, "--task-step-seconds", "0.05")
    try:
        rt = _cloud(mock.url, mdir, tmp_path / "hyp")
        assert rt.returncode == 1, rt.stdout + rt.stderr
        assert "CLOUD_FAIL step=presign" in rt.stderr and "--chunk-size" in rt.stderr, rt.stderr
    finally:
        mock.stop()


# --- the whole topology on the venv --------------------------------------------------
def _local_up_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(BASE_ENV, EMULATOR_HCI_PORT=str(free_port()), EMULATOR_HEALTH_PORT=str(free_port()),
               MOCKCLOUD_PORT=str(free_port()), LOCAL_UP_OUT=str(tmp_path / "local-up"))
    env.update(extra)
    return env


def test_run_group_kills_the_whole_tree_on_timeout():
    """The helper the local-up tests use: a timed-out script leaves no orphan behind.
    (subprocess.run(timeout=...) would SIGKILL bash and leave the background sleep running.)"""
    t0 = time.monotonic()
    _, timed_out, pgid = run_group(["bash", "-c", "sleep 300 & sleep 300"], BASE_ENV, timeout=1)
    assert timed_out
    assert not _group_alive(pgid), "the background sleep survived"
    assert time.monotonic() - t0 < 15


def test_local_up_completes_within_60s(tmp_path):
    proc, timed_out, _ = run_group(["bash", str(LOCAL_UP)], _local_up_env(tmp_path), timeout=110)
    out = proc.stdout + proc.stderr
    assert not timed_out, f"local-up.sh did not finish within 110 s (process group killed)\n{out}"
    assert proc.returncode == 0, out
    live = float(re.search(r"^LOCAL_UP_LIVE_S=([0-9.]+)$", proc.stdout, re.M).group(1))
    total = float(re.search(r"^LOCAL_UP_TOTAL_S=([0-9.]+)$", proc.stdout, re.M).group(1))
    assert live <= 60, out
    assert total <= 60, out
    assert "JOB_OK" in proc.stdout and "PROBE_OK kind=scan" in proc.stdout, out
    assert "CLOUD_OK" in proc.stdout, out
    job = tmp_path / "local-up" / "job"
    report = json.loads((job / "report.json").read_text())
    assert report.get("schema", "").startswith("plaud-harness/eval-report/"), report.keys()
    cloud = json.loads((job / "cloud-report.json").read_text())
    assert cloud["gates"]["passed"] is True, cloud["gates"]


def test_local_up_refuses_a_port_that_is_already_taken(tmp_path):
    """A leftover mockcloud on the port would otherwise answer the probes (review finding C6)."""
    squatter = MockCloud(tmp_path)
    try:
        env = _local_up_env(tmp_path, MOCKCLOUD_PORT=str(squatter.port))
        t0 = time.monotonic()
        proc, timed_out, _ = run_group(["bash", str(LOCAL_UP)], env, timeout=60)
        out = proc.stdout + proc.stderr
        assert not timed_out, out
        assert proc.returncode == 1, out
        assert f"port {squatter.port} is already in use" in proc.stderr, out
        assert "JOB_OK" not in proc.stdout and "local-up: PASS" not in proc.stdout, out
        assert time.monotonic() - t0 < 15, out
        assert squatter.proc.poll() is None, "local-up must not touch a process it did not start"
    finally:
        squatter.stop()


def test_local_up_reports_a_failing_job_as_fail_with_exit_1(tmp_path):
    env = _local_up_env(tmp_path, JOB_SUITE="no-such-suite", JOB_MEETINGS="1")
    proc, timed_out, _ = run_group(["bash", str(LOCAL_UP)], env, timeout=110)
    out = proc.stdout + proc.stderr
    assert not timed_out, out
    assert proc.returncode == 1, out  # 1 = a service or the job failed; 2 is reserved for "venv missing"
    assert re.search(r"^LOCAL_UP_TOTAL_S=[0-9.]+$", proc.stdout, re.M), out
    assert re.search(r"local-up: FAIL job exited [1-9]", proc.stderr), out
    assert "JOB_FAIL step=score" in out, out


def test_local_up_tears_its_whole_tree_down_promptly_on_sigterm(tmp_path):
    """SIGTERM while the job runs: the trap fires at once and the job's children die too (C8)."""
    env = _local_up_env(tmp_path, JOB_MEETINGS="6")  # generation alone takes several seconds
    try:
        proc = subprocess.Popen(["bash", str(LOCAL_UP)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=env, cwd=ROOT, start_new_session=True)
    except (OSError, PermissionError) as exc:  # pragma: no cover
        pytest.skip(f"cannot spawn local-up.sh here: {exc}")
    lines: list[str] = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("JOB_STEP generate"):
                break
        assert any(ln.startswith("JOB_STEP generate") for ln in lines), "".join(lines)
        time.sleep(0.5)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)  # to bash only, as `kill <pid>` or a CI cancel would
        try:
            rest, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            rest = ""
        took = time.monotonic() - t0
        lines.append(rest or "")
        out = "".join(lines)
        assert took < 5, f"local-up took {took:.1f}s to honour SIGTERM\n{out}"
        assert "local-up: torn down" in out, out
        assert proc.returncode != 0, out
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _group_alive(proc.pid):
            time.sleep(0.1)
        assert not _group_alive(proc.pid), "a child of local-up survived its SIGTERM"
    finally:
        _kill_group(proc.pid, proc)
