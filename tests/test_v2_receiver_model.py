"""V2 (pure): the reconstructed SDK receiver and the fault injector, without
Bumble. Each test pins one bytecode rule of q$a (R1-R13 in
tests/fault_support.py) to an analytically known answer, so the GATT-level
matrix in test_v2_fault_matrix.py rests on a model that is itself tested.

R7-S13 (r7/r7-s13-recording-pull.md) settled which branch completes a
transfer: `v3.finish(code)` in the EMPTY_PACKAGE branch (ALL.txt:69100-69108),
not the TAIL callback. The sequence tests below pin the model to that
runtime fact through the same dispatch path the SDK uses (R12).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from fault_support import (
    STICKY_OPCODES,
    Action,
    ReceiverUndecodable,
    SdkHost,
    SdkReceiver,
    SdkTransferDriver,
    _Run,
)

from plaudsim.faults import (
    Fault,
    FaultInjector,
    FaultKind,
    Kind,
    LinkAction,
    Stream,
    classify_frame,
    data_header_len,
)
from plaudsim.filesync import EMPTY_PACKAGE_OFFSET, pack_stop_sync_request, pack_sync_start
from plaudsim.transfer import (
    TransferSession,
    pack_empty_package_frame,
    pack_file_data_frame,
    pack_stop_sync_response,
    pack_sync_head,
    pack_sync_tail,
)

SID = 0x0BADF00D
FILE = bytes((i * 31 + 5) & 0xFF for i in range(96))


def data(offset: int, n: int = 32, sid: int = SID) -> bytes:
    return pack_file_data_frame(offset, FILE[offset : offset + n], sid, 7)


def empty(code: int = 0, sid: int = SID) -> bytes:
    return pack_empty_package_frame(code, sid, 7)


def receiver(start: int = 0, H: bool = False) -> tuple[SdkHost, SdkReceiver]:
    host = SdkHost(7)
    host.recovery_in_flight = H
    r = SdkReceiver(host, SID, start, 0, False)
    return host, r


def kinds(actions: list[Action]) -> list[str]:
    return [a.kind for a in actions]


def device_frames(**kw) -> list[bytes]:
    """What the emulator's TransferSession puts on the wire (default: with the sentinel)."""
    t = TransferSession(file_bytes=FILE, crc=0xBEEF, port_version=7, **kw)
    t.start(SID, 0, 0)
    return t.frames()


def dispatch_all(frames: list[bytes]) -> tuple[SdkHost, SdkReceiver, list[str]]:
    """Feed frames through z's dispatcher (R12), not straight into q$a."""
    host = SdkHost(7)
    r = SdkReceiver(host, SID, 0, 0, False)
    host.register({28, 29}, r.on_frame, "syncFile#0")
    out: list[str] = []
    for f in frames:
        out.extend(kinds(host.dispatch(f)))
    return host, r, out


# --- R1 session gate --------------------------------------------------------


def test_r1_session_mismatch_drops_the_frame_with_no_other_effect() -> None:
    _, r = receiver()
    assert kinds(r.on_frame(data(0, sid=SID + 1))) == ["session_mismatch"]
    assert r.cursor == 0 and not r.resend_pending
    # the very next correct frame is accepted: the gate has no memory
    assert kinds(r.on_frame(data(0))) == ["data"]
    assert r.cursor == 32


def test_r1_legacy_layout_has_no_session_gate() -> None:
    host = SdkHost(5)
    r = SdkReceiver(host, SID, 0, 0, False)
    legacy = pack_file_data_frame(0, FILE[:32], None, 5)
    acts = r.on_frame(legacy)
    assert kinds(acts) == ["data"] and acts[0].payload == FILE[:32]
    assert r.cursor == 32


def test_r1_the_sentinel_is_session_gated_too() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    assert kinds(r.on_frame(empty(0, sid=SID ^ 1))) == ["session_mismatch"]
    assert kinds(r.on_frame(empty(0))) == ["finish"]


# --- R3 / R5 / R7 cursor discipline ---------------------------------------


def test_r5_duplicate_and_behind_cursor_frames_are_dropped_silently() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    r.on_frame(data(32))
    assert kinds(r.on_frame(data(32))) == ["dropped_behind_cursor"]   # exact duplicate
    assert kinds(r.on_frame(data(0))) == ["dropped_behind_cursor"]    # far behind
    assert r.cursor == 64 and not r.resend_pending, "no recovery is started for old frames"


def test_r7_declared_length_is_clamped_to_the_frame_end() -> None:
    """A 32-byte payload cut to 5 bytes on the wire advances the cursor by 5,
    not by the declared 32 -- the SDK copies `len - (i+5)` bytes."""
    _, r = receiver()
    frame = data(0)
    acts = r.on_frame(frame[:15])
    assert acts[0].kind == "data" and acts[0].payload == FILE[:5] and acts[0].value == 32
    assert r.cursor == 5
    # what follows on the device's cursor is now a gap for the host
    assert kinds(r.on_frame(data(32))) == ["loss_recovery"]


def test_r7_zero_length_payload_advances_nothing() -> None:
    _, r = receiver()
    frame = data(0)[:10]                 # header only, declared 32, no payload bytes
    acts = r.on_frame(frame)
    assert acts[0].kind == "data" and acts[0].payload == b""
    assert r.cursor == 0
    assert kinds(r.on_frame(data(0))) == ["data"], "the same offset is accepted again"


# --- R6 / R4 gap handling ---------------------------------------------------


def test_r6_first_gap_starts_recovery_once_and_later_gaps_are_ignored() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    assert kinds(r.on_frame(data(64))) == ["loss_recovery"]
    assert r.resend_pending
    assert kinds(r.on_frame(data(96))) == ["gap_ignored_pending"]
    assert r.cursor == 32, "the gap frame is consumed, never buffered"


@pytest.mark.asyncio
async def test_r6_gap_writes_stop_sync_synchronously_then_restarts_on_a7() -> None:
    """The order R7-S13 S4.2 observed on the wire (capture-run6b: 28@0, 29,
    28@3200 35 ms later), driven through the model with a recording write:
    gap -> z6 immediately -> H set, timer armed -> a7 -> y6(cursor) with a
    fresh receiver. No 5 s timer is involved on this path."""
    drv = SdkTransferDriver(peer=None, data_char=None, command_char=None, stall_timeout=0.2)
    wire: list[bytes] = []

    async def fake_write(payload: bytes) -> None:
        wire.append(bytes(payload))

    drv.write = fake_write  # type: ignore[method-assign]
    st = _Run(SID, 0, 0, bytearray())
    assert await drv._sync_file_start(st, 0, is_resend=False) is None
    r0 = st.receiver
    for act in drv.host.dispatch(data(0)):
        await drv._execute(st, act)
    gap = drv.host.dispatch(data(64))
    assert kinds(gap) == ["loss_recovery"]
    assert await drv._execute(st, gap[0]) is None
    assert wire == [pack_sync_start(SID, 0, 0), pack_stop_sync_request()], "z6 goes out on the gap itself"
    assert drv.host.recovery_in_flight and r0.timer_task is not None and st.stop_syncs == 1
    assert st.restarts == 0, "no syncFileStart before the a7"
    # the device's a7 (opcode 30, non-sticky) reaches the stopSync registration
    a7 = drv.host.dispatch(pack_stop_sync_response())
    assert kinds(a7) == ["a7"]
    assert await drv._execute(st, a7[0]) is None
    assert wire[-1] == pack_sync_start(SID, 32, 0), "y6 re-issued from the cursor"
    assert st.restarts == 1 and r0.stopped and r0.timer_task is None
    assert st.receiver is not r0 and st.receiver.cursor == st.receiver.start == 32
    assert not drv.host.recovery_in_flight, "q.a clears H (R11)"
    # the restarted stream's first frame is accepted by the NEW receiver only
    assert kinds(drv.host.dispatch(data(32))) == ["data"] and st.receiver.cursor == 64 and r0.cursor == 32
    for r in st.receivers:
        r.cancel_timer()


@pytest.mark.asyncio
async def test_r4_gap_while_recovery_in_flight_only_arms_the_timer() -> None:
    host, r = receiver(H=True)
    r.stall_timeout = 0.05
    r.timer_expiries = asyncio.Queue()
    r.on_frame(data(0))
    assert kinds(r.on_frame(data(64))) == ["gap_while_recovering"]
    assert not r.resend_pending and r.timer_task is not None
    assert r.events[-1] == "timer_armed"
    r.on_frame(data(96))
    assert r.events[-1] == "timer_extended"
    kind, who = await asyncio.wait_for(r.timer_expiries.get(), 1.0)      # R10 expiry
    assert kind == "timer" and who is r
    r.cancel_timer()


@pytest.mark.asyncio
async def test_r10_timer_does_not_fire_once_cancelled_or_once_H_is_clear() -> None:
    host, r = receiver(H=True)
    r.stall_timeout = 0.03
    r.timer_expiries = asyncio.Queue()
    r.arm_timer()
    r.cancel_timer()                       # q$a.b(): executor shut down
    await asyncio.sleep(0.08)
    assert r.timer_expiries.empty()
    r.arm_timer()
    host.recovery_in_flight = False        # a restart cleared H (q.a)
    await asyncio.sleep(0.08)
    assert r.timer_expiries.empty()


def test_r10_a_tail_without_a_sentinel_arms_nothing() -> None:
    """The stall timer is armed by R4/R6 only; HEAD.DATA.TAIL leaves q$a idle
    with the request pending (R7-S13 runs 1/2/9)."""
    _, r = receiver()
    r.on_frame(data(0))
    r.on_frame(pack_sync_tail(SID, 1))
    assert r.timer_task is None and "timer_armed" not in r.events


# --- R9 TAIL: a callback, not completion ------------------------------------


def test_r9_tail_with_progress_fires_the_tail_callback_and_never_checks_the_size() -> None:
    host, r = receiver()
    r.on_frame(data(0))
    acts = r.on_frame(pack_sync_tail(SID, 0xABCD))
    assert kinds(acts) == ["tail"], "bleSyncFileTail -- and NOT finish"
    assert acts[0].offset == 32 and acts[0].value == {"session_id": SID, "crc": 0xABCD}
    assert host.state == 29
    assert "finish" not in kinds(acts) and not any(e.startswith("finish") for e in r.events)


def test_r9_tail_without_progress_is_always_a_restart() -> None:
    _, r = receiver(start=64)
    acts = r.on_frame(pack_sync_tail(SID, 1))
    assert kinds(acts) == ["restart"] and acts[0].offset == 64 and acts[0].value == "tail_without_progress"
    assert r.stopped
    assert kinds(r.on_frame(pack_sync_tail(SID, 1))) == ["tail_ignored_stopped"]


def test_r9_tail_while_recovery_in_flight_restarts_from_the_cursor() -> None:
    _, r = receiver(H=True)
    r.on_frame(data(0))
    acts = r.on_frame(pack_sync_tail(SID, 1))
    assert kinds(acts) == ["restart"] and acts[0].offset == 32


def test_r13_tail_crc_is_forwarded_not_verified() -> None:
    """Two TAILs that differ only in crc behave identically (runtime: 0xBEEF and 0, runs 1b/2)."""
    for crc in (0x0000, 0xFFFF):
        _, r = receiver()
        r.on_frame(data(0))
        acts = r.on_frame(pack_sync_tail(SID, crc))
        assert acts[0].kind == "tail" and acts[0].value["crc"] == crc


# --- R2 EMPTY_PACKAGE: the finish branch --------------------------------------


@pytest.mark.parametrize("code", [0, 1, 7])
def test_r2_empty_package_with_progress_and_no_recovery_finishes(code: int) -> None:
    _, r = receiver()
    r.on_frame(data(0))
    acts = r.on_frame(empty(code))
    assert kinds(acts) == ["finish"] and acts[0].value == code and acts[0].offset == 32
    assert r.events[-1] == f"finish:{code}"


def test_r2_empty_package_without_progress_is_ignored() -> None:
    _, r = receiver()
    assert kinds(r.on_frame(empty(0))) == ["empty_package_ignored"]


def test_r2_empty_package_with_a_resend_pending_is_ignored() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    r.on_frame(data(64))                    # gap -> b = true
    assert kinds(r.on_frame(empty(0))) == ["empty_package_ignored"], "b set: no finish (:69100-69104)"


def test_r2_empty_package_while_recovering_restarts_unless_code_is_1() -> None:
    _, r = receiver(H=True)
    r.on_frame(data(0))
    assert kinds(r.on_frame(empty(1))) == ["empty_package_ignored"]
    acts = r.on_frame(empty(0))
    assert kinds(acts) == ["restart"] and acts[0].value == "empty_package_while_recovering"
    assert r.stopped
    assert kinds(r.on_frame(empty(0))) == ["empty_package_ignored"], "c set: at most one restart"


def test_r2_finish_latches_nothing_so_a_second_sentinel_finishes_again() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    assert kinds(r.on_frame(empty(0))) == ["finish"]
    assert kinds(r.on_frame(empty(0))) == ["finish"], "app-level effect of a double bleDataComplete: UNKNOWN"


# --- the sequences R7-S13 ran, through the dispatcher (R2 + R9 + R12) ----------


def test_sequence_default_finishes_on_the_sentinel_then_fires_the_tail_callback() -> None:
    host, r, out = dispatch_all(device_frames())
    assert out.count("finish") == 1 and out.count("tail") == 1
    assert out.index("finish") < out.index("tail"), "runs 3/4b/8: completion precedes bleSyncFileTail"
    assert out[-2:] == ["finish", "tail"] and r.cursor == len(FILE)
    assert host.registrations == [], "the TAIL (non-sticky 29) unregisters the receiver (R12)"


def test_sequence_legacy_head_data_tail_never_finishes() -> None:
    host, r, out = dispatch_all(device_frames(empty_package_code=None))
    assert "finish" not in out and out[-1] == "tail" and r.cursor == len(FILE)
    assert host.registrations == [] and r.timer_task is None, "runs 1/2/9: request pending, nothing armed"


def test_sequence_sentinel_after_tail_has_no_receiver() -> None:
    frames = device_frames(empty_package_code=None) + [empty(0)]
    host, r, out = dispatch_all(frames)
    assert out[-2:] == ["tail", "dropped_no_receiver"], "run 7: never completes"
    assert "finish" not in out and host.log[-1] == "File Sync Callback is Null"


@pytest.mark.parametrize("code", [0, 1])
def test_sequence_codes_0_and_1_both_finish_on_the_normal_path(code: int) -> None:
    _, _, out = dispatch_all(device_frames(empty_package_code=code))
    assert out[-2:] == ["finish", "tail"]


def test_sequence_zero_length_file_can_never_finish() -> None:
    t = TransferSession(file_bytes=b"", crc=1, port_version=7)
    t.start(SID, 0, 0)
    host, r, out = dispatch_all(t.frames())
    assert out == ["head", "empty_package_ignored", "restart"] and r.stopped


# --- R8 HEAD --------------------------------------------------------------


def test_r8_head_status_nonzero_marks_state_29_and_still_fires() -> None:
    host, r = receiver()
    acts = r.on_frame(pack_sync_head(SID, 3))
    assert kinds(acts) == ["head"] and acts[0].value["status"] == 3 and host.state == 29
    assert kinds(r.on_frame(data(0))) == ["data"], "a failed HEAD does not gate DATA"


def test_r8_head_callback_is_suppressed_while_a_resend_is_pending() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    r.on_frame(data(64))                    # gap -> b = true
    assert kinds(r.on_frame(pack_sync_head(SID, 0))) == ["head_suppressed"]
    assert kinds(r.on_frame(pack_sync_head(SID, 1))) == ["head"], "a FAILED head always fires"


# --- R12 registry and dispatch ---------------------------------------------


def test_r12_newest_registration_wins_and_older_ones_are_evicted() -> None:
    host = SdkHost(7)
    old = SdkReceiver(host, SID, 0, 0, False)
    new = SdkReceiver(host, SID, 32, 0, True)
    host.register({28, 29}, old.on_frame, "old")
    host.register({28, 29}, new.on_frame, "new")
    acts = host.dispatch(data(32))
    assert kinds(acts) == ["data"] and new.cursor == 64 and old.cursor == 0
    assert [r.label for r in host.registrations] == ["new"]


def test_r12_tail_unregisters_the_receiver_and_late_data_is_dropped() -> None:
    host = SdkHost(7)
    r = SdkReceiver(host, SID, 0, 0, False)
    host.register({28, 29}, r.on_frame, "sync")
    host.dispatch(data(0))
    assert kinds(host.dispatch(pack_sync_tail(SID, 1))) == ["tail"]
    assert host.registrations == []
    assert kinds(host.dispatch(data(32))) == ["dropped_no_receiver"]
    assert r.cursor == 32


def test_r12_sticky_head_keeps_the_registration() -> None:
    host = SdkHost(7)
    r = SdkReceiver(host, SID, 0, 0, False)
    host.register({28, 29}, r.on_frame, "sync")
    assert 28 in STICKY_OPCODES and 29 not in STICKY_OPCODES and 30 not in STICKY_OPCODES
    host.dispatch(pack_sync_head(SID, 0))
    assert len(host.registrations) == 1


def test_r12_a_finish_leaves_the_registration_in_place() -> None:
    host = SdkHost(7)
    r = SdkReceiver(host, SID, 0, 0, False)
    host.register({28, 29}, r.on_frame, "sync")
    host.dispatch(data(0))
    assert kinds(host.dispatch(empty(0))) == ["finish"]
    assert len(host.registrations) == 1, "only the TAIL (or the app's stopSyncFile, run 5) closes the request"


def test_r12_unregistered_opcodes_are_routed_elsewhere() -> None:
    host = SdkHost(7)
    r = SdkReceiver(host, SID, 0, 0, False)
    host.register({28, 29}, r.on_frame, "sync")
    battery = b"\x01\x09\x00\x01\x50"
    acts = host.dispatch(battery)
    assert kinds(acts) == ["other"] and acts[0].value == 9
    assert kinds(host.dispatch(pack_stop_sync_response())) == ["other"]  # no stop registered


# --- R13 under-length frames ----------------------------------------------


@pytest.mark.parametrize("n", [1, 4, 7, 9])
def test_r13_frames_shorter_than_the_header_are_reported_not_guessed(n: int) -> None:
    _, r = receiver()
    with pytest.raises(ReceiverUndecodable):
        r.on_frame(data(0)[:n])
    assert r.cursor == 0


def test_r13_sentinel_without_its_code_byte_is_reported_not_guessed() -> None:
    _, r = receiver()
    r.on_frame(data(0))
    with pytest.raises(ReceiverUndecodable):
        r.on_frame(empty(0)[:10])


# --- the injector -----------------------------------------------------------


def transfer_frames() -> list[bytes]:
    return [pack_sync_head(SID, 0)] + [data(o) for o in range(0, 96, 32)] + [empty(0), pack_sync_tail(SID, 1)]


def run(inj: FaultInjector, frames: list[bytes]) -> list[bytes | LinkAction]:
    inj.begin_stream(Stream.TRANSFER)
    out: list[bytes | LinkAction] = []
    for f in frames:
        out.extend(inj.process(f))
    return out


def offsets(items: list[bytes | LinkAction]) -> list[int | str]:
    out: list[int | str] = []
    for x in items:
        if isinstance(x, LinkAction):
            out.append(x.value)
        else:
            k, _, _, off = classify_frame(x, 7)
            out.append(off if k is Kind.DATA else k.value)
    return out


def test_classify_tells_the_sentinel_apart_from_data() -> None:
    assert classify_frame(empty(0), 7) == (Kind.EMPTY, None, SID, EMPTY_PACKAGE_OFFSET)
    assert classify_frame(data(32), 7) == (Kind.DATA, None, SID, 32)
    assert classify_frame(pack_sync_tail(SID, 1), 7)[0] is Kind.TAIL


def test_injector_reorder_swaps_with_the_following_frame() -> None:
    inj = FaultInjector([Fault(FaultKind.REORDER, Kind.DATA, offsets=(32,))])
    assert offsets(run(inj, transfer_frames())) == ["head", 0, 64, 32, "empty_package", "tail"]


def test_injector_once_semantics_leave_the_second_stream_clean() -> None:
    inj = FaultInjector([Fault(FaultKind.DROP, Kind.DATA, offsets=(32,))])
    assert offsets(run(inj, transfer_frames())) == ["head", 0, 64, "empty_package", "tail"]
    assert offsets(run(inj, transfer_frames())) == ["head", 0, 32, 64, "empty_package", "tail"]
    persistent = FaultInjector([Fault(FaultKind.DROP, Kind.DATA, offsets=(32,), once=False)])
    assert offsets(run(persistent, transfer_frames())) == ["head", 0, 64, "empty_package", "tail"]
    assert offsets(run(persistent, transfer_frames())) == ["head", 0, 64, "empty_package", "tail"]


def test_injector_data_faults_never_touch_the_sentinel() -> None:
    every_data = FaultInjector([Fault(FaultKind.DROP, Kind.DATA, once=False)])
    assert offsets(run(every_data, transfer_frames())) == ["head", "empty_package", "tail"]
    only_sentinel = FaultInjector([Fault(FaultKind.DROP, Kind.EMPTY)])
    assert offsets(run(only_sentinel, transfer_frames())) == ["head", 0, 32, 64, "tail"]


def test_injector_can_move_the_sentinel_after_the_tail() -> None:
    inj = FaultInjector([Fault(FaultKind.DROP, Kind.EMPTY), Fault(FaultKind.EMPTY_PACKAGE_AFTER, Kind.TAIL, param=0)])
    out = run(inj, transfer_frames())
    assert offsets(out) == ["head", 0, 32, 64, "tail", "empty_package"]
    assert out[-1] == empty(0)


def test_injector_link_actions_and_rewrites() -> None:
    inj = FaultInjector(
        [
            Fault(FaultKind.WRONG_SESSION, Kind.DATA, offsets=(0,), param=1),
            Fault(FaultKind.HEAD_STATUS, Kind.HEAD, param=2),
            Fault(FaultKind.CORRUPT, Kind.TAIL, param=(7, 0xFF)),
            Fault(FaultKind.DISCONNECT_AFTER, Kind.DATA, offsets=(64,)),
        ]
    )
    out = run(inj, transfer_frames())
    assert out[0] == pack_sync_head(SID, 2)
    assert classify_frame(out[1], 7)[2] == 1
    assert out[-3] is LinkAction.DISCONNECT
    tail = out[-1]
    assert isinstance(tail, bytes) and tail[7] == (pack_sync_tail(SID, 1)[7] ^ 0xFF)


def test_injector_targets_other_frames_by_opcode() -> None:
    inj = FaultInjector([Fault(FaultKind.DROP, Kind.OTHER, opcodes=(30,))])
    inj.begin_stream(Stream.CONTROL)
    assert inj.process(pack_stop_sync_response()) == []
    assert inj.process(b"\x01\x09\x00\x00\x64") == [b"\x01\x09\x00\x00\x64"]


def test_data_header_len_follows_the_portversion_branch() -> None:
    assert data_header_len(7) == 10 and data_header_len(6) == 6
    assert len(pack_file_data_frame(0, b"", SID, 7)) == 10
    assert len(pack_file_data_frame(0, b"", None, 6)) == 6


# --- FaultyPeripheral stream scoping (review T7) and the invariant (T10) ---------------


class _StubDevice:
    def on(self, *_a, **_k):
        return None


def _faulty(**kw):
    from plaudsim.faults import FaultyPeripheral

    p = FaultyPeripheral(_StubDevice(), **kw)
    wire: list[bytes] = []

    async def respond(_conn, frame: bytes) -> None:
        wire.append(frame)

    p._respond = respond  # type: ignore[assignment]
    return p, wire


def _kinds(wire: list[bytes]) -> list[tuple[str, int | None]]:
    out = []
    for f in wire:
        k, _, _, off = classify_frame(f, 7)
        out.append((k.value, off if k is Kind.DATA else None))
    return out


async def _drain(p) -> None:
    for _ in range(200):
        tasks = [t for t in [p._stream_task, *p._orphans] if t is not None and not t.done()]
        if not tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("streams did not finish")


def test_a_cut_stays_cut_when_the_client_writes_something_else() -> None:
    """T7: `_suppressed` was cleared by EVERY write, so an unrelated getState
    un-silenced a paced stream the device had cut, and its EMPTY_PACKAGE and
    TAIL went out after all."""
    p, wire = _faulty(faults=[Fault(FaultKind.CUT_AFTER, Kind.DATA, offsets=(64,))], pace=0.005, file_bytes=bytes(256))

    async def main() -> None:
        await p._on_command_write(None, pack_sync_start(7, 0, 0))
        await asyncio.sleep(0.05)
        await p._on_command_write(None, b"\x01\x03\x00")          # unrelated getState
        await _drain(p)

    asyncio.run(main())
    kinds = _kinds(wire)
    assert kinds[:4] == [("head", None), ("data", 0), ("data", 32), ("data", 64)]
    assert kinds[4:] == [("other", None)], kinds            # only the getState answer
    assert p.stream_events("device_went_silent")


def test_orphan_stream_keeps_its_own_fault_ordinals() -> None:
    """T7: begin_stream() reset the per-kind ordinals and the stream id while
    an orphan stream (abandon_stream_on_restart=False) was still emitting, so
    a fault scoped to stream 0 stopped applying to stream 0's later frames."""
    p, wire = _faulty(
        faults=[Fault(FaultKind.DROP, Kind.DATA, streams=(0,), once=False)],
        pace=0.004, file_bytes=bytes(512), abandon_stream_on_restart=False,
    )

    async def main() -> None:
        await p._on_command_write(None, pack_sync_start(7, 0, 0))
        await asyncio.sleep(0.02)
        await p._on_command_write(None, pack_sync_start(7, 256, 0))
        await _drain(p)

    asyncio.run(main())
    data_offsets = [off for k, off in _kinds(wire) if k == "data"]
    assert data_offsets == list(range(256, 512, 32)), data_offsets
    assert p.stream_events("not_abandoned")
    dropped_streams = {e["info"].stream for e in p.injector.log if e["event"] == "fault"}
    assert dropped_streams == {0}


def test_unknown_session_is_refused_instead_of_serving_the_last_file() -> None:
    """T7: with `files` set, a y6 for a session not in it silently served
    whatever file had been served last (`_start_transfer` mutated file_bytes)."""
    a, b = bytes(range(64)), bytes(range(100, 196))
    p, wire = _faulty(files={1: a, 2: b})

    async def main() -> None:
        await p._on_command_write(None, pack_sync_start(2, 0, 0))
        wire.clear()
        await p._on_command_write(None, pack_sync_start(3, 0, 0))

    asyncio.run(main())
    assert wire == [pack_sync_head(3, p.unknown_session_head_status)]
    assert p.unknown_session_head_status != 0
    assert p.file_bytes == b"" and p.transfer is None
    assert p.stream_events("unknown_session")


def test_file_revisions_serve_the_next_revision_on_each_sync_start() -> None:
    """T10 device hook: the n-th y6 for a session serves revision n (the last
    repeats) -- the file changing under an active sync, HARNESS_POLICY."""
    r0, r1 = bytes(64), bytes([0xAA]) * 64
    p, wire = _faulty(file_revisions={9: [r0, r1]})

    async def main() -> None:
        for start in (0, 32, 32):
            await p._on_command_write(None, pack_sync_start(9, start, 0))

    asyncio.run(main())
    payloads = [f[10:] for f in wire if classify_frame(f, 7)[0] is Kind.DATA]
    assert payloads == [r0[:32], r0[32:], r1[32:], r1[32:]]


def test_outcome_of_raises_on_wrong_bytes_at_the_right_offset() -> None:
    """T10: the invariant's corruption branch is reachable in principle."""
    from fault_support import SilentCorruption, TransferResult, outcome_of

    r = TransferResult(
        session_id=SID, data=b"\x00\x01\xff", complete=True, failure=None, cursor=3, restarts=0, stop_syncs=0,
        timer_restarts=0, tail_seen=True, tail_crc=None, head_statuses=[], finish_codes=[0], post_finish=[],
        session_mismatches=0, dropped_behind=0, dropped_no_receiver=0, other_frames=0, data_frames=1,
        events=[], elapsed_s=0.0,
    )
    with pytest.raises(SilentCorruption, match="SILENT CORRUPTION"):
        outcome_of(r, b"\x00\x01\x02")
    assert issubclass(SilentCorruption, AssertionError)
