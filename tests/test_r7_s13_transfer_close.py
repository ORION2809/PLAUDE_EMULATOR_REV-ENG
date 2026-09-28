"""R7-S13: what closes a BLE file transfer for the genuine client.

Runtime evidence (r7/r7-s13-recording-pull.md, r7/r7-s13-evidence/): the
unmodified Android SDK, driven on an AVD over android-netsim against this
emulator, completed a pull (bleDataComplete / exportAudio onComplete) ONLY
when the device sent an EMPTY_PACKAGE frame after the last DATA frame and
before the TAIL. HEAD . DATA . TAIL alone never completed (runs 1, 2, 9);
EMPTY after TAIL never completed (run 7); codes 0 and 1 both completed
(runs 4b, 8). The delivered bytes hashed identically to the served fixture.

These tests pin (a) the emulator's new default sequence, (b) the legacy
opt-out, (c) the bytecode receiver model's agreement with the runtime, (d)
the task-streaming abort policy, and (e) the archived runtime evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from support import connect_like_the_sdk  # noqa: E402

from plaudsim.filesync import (  # noqa: E402
    EMPTY_PACKAGE_OFFSET,
    OPCODE_SYNC_TAIL,
    pack_sync_start,
    parse_file_data_frame,
)
from plaudsim.profile import PlaudPeripheral  # noqa: E402
from plaudsim.transfer import (  # noqa: E402
    DEFAULT_EMPTY_PACKAGE_CODE,
    TransferSession,
    pack_empty_package_frame,
    pack_sync_tail,
)

SID = 1700000000
FILE = bytes(range(256)) * 3  # 768 B -> 24 DATA frames at the 32-byte policy
CRC = 0xBEEF
FIXTURE = ROOT / "tests/fixtures/r6s2_16k_mono.ogg"
EVIDENCE = ROOT / "r7/r7-s13-evidence"


def _frames(**kw) -> list[bytes]:
    t = TransferSession(file_bytes=FILE, crc=CRC, port_version=7, **kw)
    t.start(SID, 0, 0)
    return t.frames()


# --- (a) the default sequence ------------------------------------------------


def test_default_sequence_is_head_data_empty_package_tail() -> None:
    frames = _frames()
    assert frames[0][:3] == b"\x01\x1c\x00"
    assert frames[-1] == pack_sync_tail(SID, CRC)
    assert frames[-2] == pack_empty_package_frame(DEFAULT_EMPTY_PACKAGE_CODE, SID, 7)
    # exact bytes the SDK's q$a.a parses: type 2, sessionId, 0xFFFFFFFF, filler, code
    assert frames[-2] == b"\x02" + SID.to_bytes(4, "little") + b"\xff\xff\xff\xff" + b"\x00" + bytes([DEFAULT_EMPTY_PACKAGE_CODE])
    datas = frames[1:-2]
    assert len(datas) == -(-len(FILE) // 32)
    for i, f in enumerate(datas):
        p = parse_file_data_frame(f, port_version=7)
        assert p["offset"] == i * 32 and p["offset"] != EMPTY_PACKAGE_OFFSET


def test_default_code_is_zero_and_documented_as_policy() -> None:
    assert DEFAULT_EMPTY_PACKAGE_CODE == 0
    src = (ROOT / "emulator/plaudsim/transfer.py").read_text()
    assert "HARNESS_POLICY" in src.split("DEFAULT_EMPTY_PACKAGE_CODE = 0")[0][-900:]


@pytest.mark.parametrize("code", [0, 1, 7])
def test_code_is_configurable(code: int) -> None:
    frames = _frames(empty_package_code=code)
    assert frames[-2][-1] == code


def test_resume_from_cursor_still_ends_with_empty_package_then_tail() -> None:
    t = TransferSession(file_bytes=FILE, crc=CRC, port_version=7)
    t.start(SID, 320, 0)
    frames = t.frames()
    assert parse_file_data_frame(frames[1], port_version=7)["offset"] == 320
    assert frames[-2][5:9] == b"\xff\xff\xff\xff" and frames[-1][:3] == b"\x01\x1d\x00"


# --- (b) the legacy opt-out ---------------------------------------------------


def test_none_restores_the_pre_r7_s13_sequence() -> None:
    frames = _frames(empty_package_code=None)
    assert frames[-1][:3] == b"\x01\x1d\x00"
    assert all(parse_file_data_frame(f, port_version=7)["offset"] != EMPTY_PACKAGE_OFFSET for f in frames[1:-1])
    assert len(frames) == len(_frames()) - 1


# --- (c) the bytecode receiver model agrees with the runtime ------------------


def _drive_receiver(frames: list[bytes]):
    """Feed frames through the faults track's SDK model the way the SDK does:
    z$c dispatch (R12) -> the registered q$a receiver (bytecode-derived)."""
    from fault_support import SdkHost, SdkReceiver
    from plaudsim.filesync import OPCODE_SYNC_HEAD

    host = SdkHost(port_version=7)
    r = SdkReceiver(host, SID, 0, 0, is_resend=False)
    host.register({OPCODE_SYNC_HEAD, OPCODE_SYNC_TAIL}, r.on_frame, "syncFile#0")
    acts: list = []
    for f in frames:
        acts.extend(host.dispatch(f))
    return acts


def test_receiver_model_finishes_on_default_sequence_and_not_on_legacy() -> None:
    kinds_default = [a.kind for a in _drive_receiver(_frames())]
    kinds_legacy = [a.kind for a in _drive_receiver(_frames(empty_package_code=None))]
    assert "finish" in kinds_default, kinds_default
    assert "finish" not in kinds_legacy, kinds_legacy


def test_receiver_model_drops_empty_package_after_tail() -> None:
    """Runtime run 7: EMPTY after TAIL never completed. In the model the TAIL
    consumes the syncFile registration (z.a(int), non-sticky opcode 29), so a
    later type-2 frame finds no receiver ("File Sync Callback is Null")."""
    frames = _frames(empty_package_code=None) + [pack_empty_package_frame(0, SID, 7)]
    kinds = [a.kind for a in _drive_receiver(frames)]
    assert "finish" not in kinds, kinds
    assert kinds[-1] == "dropped_no_receiver", kinds


# --- (d) the peripheral over Bumble --------------------------------------------


@pytest.mark.asyncio
async def test_peripheral_emits_empty_package_before_tail_inline() -> None:
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda d: PlaudPeripheral(d, file_bytes=FILE, tail_crc=CRC,
                                  file_table=[{"session_id": SID, "file_size": len(FILE), "scene": 2, "attribute": 1}])
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, pack_sync_start(SID, 0, 0), with_response=True)
    assert len(responses) == 1 + 24 + 1 + 1
    assert responses[-2] == pack_empty_package_frame(0, SID, 7)
    assert responses[-1][:3] == b"\x01\x1d\x00"


@pytest.mark.asyncio
async def test_task_streaming_is_paced_and_aborted_by_a_new_sync_start() -> None:
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda d: PlaudPeripheral(d, file_bytes=FILE, tail_crc=CRC, stream_in_task=True, response_pacing_s=0.01,
                                  file_table=[{"session_id": SID, "file_size": len(FILE), "scene": 2, "attribute": 1}])
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, pack_sync_start(SID, 0, 0), with_response=True)
    # inline the write returns before the stream ends: task mode is asynchronous
    assert len(responses) < 27
    await asyncio.sleep(0.06)
    n_before = len(responses)
    assert 2 <= n_before < 27
    # the client re-issues syncFileStart from its cursor (gap recovery): old stream must stop
    await peer.write_value(command, pack_sync_start(SID, 320, 0), with_response=True)
    await asyncio.sleep(0.5)
    assert any(e["event"] == "aborted" and e["reason"] == "new_sync_start" for e in peripheral.stream_log)
    assert any(e["event"] == "completed" for e in peripheral.stream_log)
    # everything after the abort belongs to the restarted stream: HEAD, DATA from 320, EMPTY, TAIL
    restarted = responses[n_before:]
    head_idx = next(i for i, f in enumerate(restarted) if f[:3] == b"\x01\x1c\x00")
    tail = restarted[head_idx + 1:]
    assert tail[-1][:3] == b"\x01\x1d\x00" and tail[-2][5:9] == b"\xff\xff\xff\xff"
    offsets = [parse_file_data_frame(f, port_version=7)["offset"] for f in tail[:-2]]
    assert offsets == list(range(320, len(FILE), 32))


@pytest.mark.asyncio
async def test_stop_sync_aborts_a_streaming_transfer() -> None:
    from plaudsim.filesync import pack_stop_sync_request

    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda d: PlaudPeripheral(d, file_bytes=FILE, tail_crc=CRC, stream_in_task=True, response_pacing_s=0.01,
                                  file_table=[{"session_id": SID, "file_size": len(FILE), "scene": 2, "attribute": 1}])
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, pack_sync_start(SID, 0, 0), with_response=True)
    await asyncio.sleep(0.05)
    await peer.write_value(command, pack_stop_sync_request(), with_response=True)
    await asyncio.sleep(0.3)
    assert peripheral.stream_log[-1]["event"] in ("aborted", "cancelled") or any(
        e["event"] == "aborted" and e["reason"] == "stop_sync" for e in peripheral.stream_log)
    assert responses[-1] == b"\x01\x1e\x00"  # a7: the stop ack is the last thing sent
    assert not any(f[:3] == b"\x01\x1d\x00" for f in responses)  # no TAIL of the aborted stream


# --- (e) the archived runtime evidence ----------------------------------------


def _sums(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, name = line.split(None, 1)
        rows.append((digest, name.split("   #")[0].strip()))
    return rows


def test_runtime_evidence_pins_byte_exact_delivery() -> None:
    import re

    fixture_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    # What the SDK delivered, hashed on the Mac after each run. Not archived because each equals
    # the fixture; DELIVERED-OUTPUTS.sha256.txt explains this (28 Sep correction).
    delivered = _sums(EVIDENCE / "DELIVERED-OUTPUTS.sha256.txt")
    byte_exact = [n for s, n in delivered if s == fixture_sha]
    # raw collector path, OPUS export path, and the gap-recovery runs all delivered the served bytes
    assert any("raw-run1b" in n for n in byte_exact)
    assert any("export-run4b" in n for n in byte_exact)
    assert any("run6b" in n for n in byte_exact) and any("run6d" in n for n in byte_exact)
    assert len(byte_exact) == len(delivered)
    # independent record: the driver printed the same hash on the device, in that run's own log
    for _, name in delivered:
        run = re.sub(r"^pullcap-(raw|export)-", "", name).rsplit(".", 1)[0]
        log = (EVIDENCE / f"logcat-{run}.filtered.log").read_text(errors="replace")
        device = re.findall(r"PULLCAP_(?:RAW|EXPORT)_DONE[^\n]*sha256=([0-9a-f]{64})", log)
        assert device and device[-1] == fixture_sha, name
    # SHA256SUMS covers exactly the files archived in the folder, and every entry verifies
    archived = _sums(EVIDENCE / "SHA256SUMS")
    for digest, name in archived:
        assert hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest() == digest, name
    # the two corrupted outputs are archived and are NOT byte-exact
    corrupted = [(s, n) for s, n in archived if "corrupted" in n or "concatenated" in n]
    assert len(corrupted) == 2 and all(s != fixture_sha for s, _ in corrupted)


def test_runtime_evidence_shows_completion_only_with_empty_package() -> None:
    def completed(run: str) -> bool:
        log = (EVIDENCE / f"logcat-{run}.filtered.log").read_text()
        return "PULLCAP_DATA_COMPLETE" in log or "PULLCAP_EXPORT_DONE" in log

    assert not completed("run1b")               # HEAD . DATA . TAIL(0xBEEF): never completes
    assert not completed("run2")                # TAIL field 0: still never completes
    assert not completed("run9-no-empty-export")
    assert not completed("run7-empty-after-tail")
    assert completed("run3") and completed("run4b-export-clean") and completed("run8-code1")
    assert completed("run6b-gap-export-abort") and completed("run6d-gap8000-export-abort")


def test_runtime_evidence_shows_stop_sync_then_restart_on_gap() -> None:
    cap = json.loads((EVIDENCE / "capture-run6b-gap-export-abort.json").read_text())
    ops = [(w.get("opcode"), w.get("sync_start", {}).get("start")) for w in cap["writes"] if w.get("opcode") in (28, 29)]
    assert ops == [(28, 0), (29, None), (28, 3200)]
    assert cap["stats"]["stop_syncs"] == 1 and cap["stats"]["streams_aborted"] == 1


# --- (f) the real-SDK preset and stream robustness (review T2, T6) --------------


class _StubDevice:
    def on(self, *_a, **_k):
        return None


def test_real_sdk_preset_is_the_r7_s13_device_and_the_constructor_default_is_not() -> None:
    """T2: runs 6b-13 converged against a task-streamed, 4 ms-paced device.
    Those values came from r7/pull_capture_peripheral.py's environment
    defaults and emulator/serve.py, not from the constructor, whose defaults
    stay inline and unpaced for in-process tests. `for_real_sdk` names the
    R7-S13 configuration so a real-SDK rig cannot silently get the other one."""
    from plaudsim.profile import REAL_SDK_RESPONSE_PACING_S, REAL_SDK_STREAM_IN_TASK

    plain = PlaudPeripheral(_StubDevice())
    assert plain.stream_in_task is False and plain.response_pacing_s == 0.0
    preset = PlaudPeripheral.for_real_sdk(_StubDevice(), file_bytes=FILE)
    assert REAL_SDK_STREAM_IN_TASK is True and REAL_SDK_RESPONSE_PACING_S == 0.004
    assert preset.stream_in_task is True and preset.response_pacing_s == 0.004
    assert preset.file_bytes == FILE
    # an explicit override still wins
    assert PlaudPeripheral.for_real_sdk(_StubDevice(), response_pacing_s=0.01).response_pacing_s == 0.01


@pytest.mark.asyncio
async def test_task_stream_is_aborted_when_the_central_disconnects() -> None:
    """T6: after a disconnection Bumble drops notifications silently, so a
    stream that keeps running logs frames nobody received and ends 'completed'."""
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda d: PlaudPeripheral(d, file_bytes=bytes(1024), stream_in_task=True, response_pacing_s=0.005)
    )
    got: list[bytes] = []
    await peer.subscribe(data, got.append, prefer_notify=True)
    await peer.write_value(command, pack_sync_start(SID, 0, 0), with_response=True)
    await asyncio.sleep(0.03)
    await devices.connections[0].disconnect()
    await asyncio.sleep(0.3)
    events = [e["event"] for e in peripheral.stream_log]
    assert "completed" not in events, peripheral.stream_log
    assert {"event": "aborted", "reason": "disconnected"} in peripheral.stream_log
    logged = [e for e in peripheral.packet_log if e["direction"] == "response"]
    assert len(logged) < 35
    assert len(logged) - len(got) <= 1, (len(logged), len(got))
    assert not peripheral.transfer_streaming


@pytest.mark.asyncio
async def test_task_stream_emit_error_is_logged_and_retrieved() -> None:
    """T6: an exception from the transport ends the stream with an 'error'
    entry; the frame that failed is not logged as a response, and the task's
    exception never reaches the loop's 'never retrieved' handler."""
    import gc

    loop = asyncio.get_running_loop()
    seen: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: seen.append(str(ctx.get("message"))))
    try:
        p = PlaudPeripheral(_StubDevice(), file_bytes=bytes(256), stream_in_task=True, response_pacing_s=0.001)
        calls = {"n": 0}

        async def respond(_conn, _frame):
            calls["n"] += 1
            if calls["n"] == 4:
                raise TimeoutError("indication not confirmed")

        p._respond = respond  # type: ignore[assignment]
        await p._on_command_write(None, pack_sync_start(7, 0, 0))
        await asyncio.sleep(0.1)
        assert p.stream_log == [{"event": "error", "error": "TimeoutError", "frames_sent": 3, "frames_total": 11}]
        assert len([e for e in p.packet_log if e["direction"] == "response"]) == 3
        assert not p.transfer_streaming
        p = None
        gc.collect()
        await asyncio.sleep(0)
        assert not [m for m in seen if "never retrieved" in m], seen
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.parametrize(
    "malformed",
    [bytes.fromhex("011c00") + b"\x00\x00\x00", bytes.fromhex("011d00") + b"\x00"],
    ids=["short_y6", "z6_with_payload"],
)
@pytest.mark.asyncio
async def test_malformed_sync_start_or_stop_does_not_abort_the_running_stream(malformed: bytes) -> None:
    """T6: the stream was aborted before the request was validated, so a
    malformed y6/z6 silenced the device and left `transfer` pointing at a
    stream that never finished."""
    p = PlaudPeripheral(_StubDevice(), file_bytes=bytes(512), stream_in_task=True, response_pacing_s=0.002)
    wire: list[bytes] = []

    async def respond(_conn, frame):
        wire.append(frame)

    p._respond = respond  # type: ignore[assignment]
    await p._on_command_write(None, pack_sync_start(7, 0, 0))
    running = p.transfer
    await asyncio.sleep(0.01)
    await p._on_command_write(None, malformed)
    assert any(e["direction"] == "error" and "malformed" in e["reason"] for e in p.packet_log)
    assert not [e for e in p.stream_log if e["event"] == "aborted"], p.stream_log
    assert p._stream_task is not None, "the running stream was dropped by a malformed request"
    await asyncio.wait_for(p._stream_task, 2)
    assert [e["event"] for e in p.stream_log] == ["completed"], p.stream_log
    assert p.transfer is running
    assert wire[-1] == pack_sync_tail(7, 0x1234)
