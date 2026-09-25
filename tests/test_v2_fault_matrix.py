"""V2: systematic fault-injection matrix for the BLE emulator.

Every cell is a real Bumble TwoDevices session through the GATT path: the
emulator (`plaudsim.faults.FaultyPeripheral`) serves a file while the
injector damages its outbound stream, and the reconstructed SDK receiver
(`tests/fault_support.py`, rules R1-R13) drives the transfer the way the
Android SDK would. The invariant asserted for EVERY cell is `outcome_of`:
the reassembled bytes equal the served file byte-for-byte, or the client
model reports a detected failure -- never a silently corrupted file. Where
the answer is analytically known (number of restarts, stop-syncs, frames)
it is asserted too.

What "complete" means here (R7-S13, r7/r7-s13-recording-pull.md): the
genuine SDK completes a transfer ONLY on the EMPTY_PACKAGE sentinel sent
after the last DATA frame and before the TAIL (`v3.finish(code)`,
ALL.txt:69100-69108). The emulator's default sequence is therefore
HEAD . DATA... . EMPTY_PACKAGE(0) . TAIL, and a TAIL is a callback, not a
completion gate.

Emission modes (HARNESS_POLICY, see FaultyPeripheral):
  atomic  the whole stream is emitted inside the y6 write handler, so the
          client sees every frame before it can react. Fine for cells
          without recovery; for a GAP it reproduces run 6 (the device kept
          serving the abandoned stream -> a second restart) and is run as
          exactly one cell, `old_stream_not_abandoned`.
  paced   `stream_in_task=True, response_pacing_s=PACE`: the base class
          streams from a cancellable task that a z6 or a new y6 aborts --
          the device under which the genuine SDK's gap recovery converged
          with one restart (runs 6b/6c/6d). All gap cells run this way.

Outcomes are recorded into build/v2-fault-matrix.json (build/ is
gitignored); docs/v2-fault-matrix.md reproduces the table.

Timers: the SDK's stall timer is 5 000 ms (R10). Cells scale it down
(HARNESS_POLICY, `stall_timeout`) -- the mechanism is unchanged and the one
cell that exercises the expiry path asserts it fired.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from fault_support import (
    CORRUPTION_RISK,
    DETECTED,
    RECOVERED,
    RECOVERED_BY_APP_RESUME,
    Link,
    SdkTransferDriver,
    TransferResult,
    bring_up,
    bring_up_two_centrals,
    driver_for,
    outcome_of,
    reconnect,
    run6_signature,
    sync_with_app_resume,
)

from plaudsim.faults import Fault, FaultKind, FaultyPeripheral, Kind
from plaudsim.filesync import (
    OPCODE_STOP_SYNC_RSP,
    pack_delete_file_request,
    parse_file_data_frame,
)
from plaudsim.profile import PlaudBatteryState
from plaudsim.transfer import DEFAULT_DATA_PAYLOAD_SIZE, DEFAULT_EMPTY_PACKAGE_CODE

# --- fixtures ----------------------------------------------------------------

RNG = random.Random(0x5EED)
FILE = bytes(RNG.getrandbits(8) for _ in range(1024))          # 32 frames of 32 bytes
FILE_B = bytes(RNG.getrandbits(8) for _ in range(777))         # 25 frames, last one short
SID = 0x12345678
SID_B = 0x2222AAAA
CRC = 0xBEEF
STAMP = 0x655A1B00
PAYLOAD = DEFAULT_DATA_PAYLOAD_SIZE
LAST_OFFSET = (len(FILE) // PAYLOAD - 1) * PAYLOAD             # 992
MID = 512
#: HARNESS_POLICY: inter-frame pacing of the task-streamed device. 4 ms is the
#: value under which the genuine SDK converged on the AVD (R7-S13 S4.4, runs 6b-10).
PACE = 0.004
#: HARNESS_POLICY (P1-P3): the SDK's stall timer is 5 s; scaled down for speed.
TIMING = dict(stall_timeout=0.4, idle_timeout=0.6, max_restarts=8)

TABLE = [{"session_id": SID, "file_size": len(FILE), "scene": 2, "attribute": 1}]
MATRIX_PATH = Path(__file__).parents[1] / "build" / "v2-fault-matrix.json"
RECORDS: list[dict[str, Any]] = []


@pytest.fixture(scope="module", autouse=True)
def write_matrix():
    RECORDS.clear()
    yield
    MATRIX_PATH.parent.mkdir(exist_ok=True)
    MATRIX_PATH.write_text(json.dumps({"cells": RECORDS}, indent=2, sort_keys=True))


def record(cell: str, outcome: str, expected: str | tuple[str, ...], evidence: str, notes: str, result: TransferResult | None = None, served: bytes | None = None, **extra: Any) -> None:
    accepted = (expected,) if isinstance(expected, str) else tuple(expected)
    row: dict[str, Any] = {
        "cell": cell,
        "outcome": outcome,
        "expected": accepted[0] if len(accepted) == 1 else list(accepted),
        "evidence": evidence,
        "notes": notes,
    }
    if result is not None and served is not None:
        row.update(
            bytes="exact" if result.data == served else ("empty" if not result.data else "prefix"),
            complete=result.complete,
            tail_seen=result.tail_seen,
            failure=result.failure,
            restarts=result.restarts,
            stop_syncs=result.stop_syncs,
            timer_restarts=result.timer_restarts,
            data_frames=result.data_frames,
            finish_codes=result.finish_codes,
            post_finish=result.post_finish,
        )
    row.update(extra)
    RECORDS.append(row)
    assert outcome in accepted, f"{cell}: outcome {outcome!r}, expected {accepted!r}: {row}"


def peripheral_factory(faults: list[Fault] = (), pace: float | None = None, file_bytes: bytes = FILE, **kw: Any) -> Callable[[Any], FaultyPeripheral]:
    return lambda device: FaultyPeripheral(
        device,
        faults=list(faults),
        pace=pace,
        file_bytes=file_bytes,
        tail_crc=CRC,
        file_table=[dict(e) for e in TABLE],
        **kw,
    )


async def run_cell(faults: list[Fault], pace: float | None, *, file_bytes: bytes = FILE, mtu: int | None = 255, prefer_notify: bool = True, timing: dict[str, Any] = TIMING, **pkw: Any) -> tuple[Link, SdkTransferDriver, TransferResult]:
    link = await bring_up(peripheral_factory(faults, pace, file_bytes, **pkw), mtu=mtu)
    driver = await driver_for(link, prefer_notify=prefer_notify, **timing)
    result = await driver.sync_file(SID, 0, 0, expected_size=len(file_bytes))
    return link, driver, result


def aborted(link: Link, reason: str) -> bool:
    return any(e.get("event") == "aborted" and e.get("reason") == reason for e in link.peripheral.stream_log)


MODES = [pytest.param(None, id="atomic"), pytest.param(PACE, id="paced")]
RUNTIME_S13 = "r7/r7-s13-recording-pull.md"

# --- the sequence family: no recovery involved, both emission modes -------------


@dataclass
class Cell:
    id: str
    faults: Callable[[], list[Fault]]
    expected: str
    evidence: str
    notes: str
    pkw: dict[str, Any] | None = None
    restarts: int | None = 0
    stop_syncs: int | None = 0


SEQUENCE_CELLS = [
    Cell(
        "clean_default_sequence", lambda: [],
        RECOVERED, f"RUNTIME_PROVEN ({RUNTIME_S13} runs 3/4b/10) + BYTECODE_PROVEN (R2 finish, R9 callback)",
        "HEAD . DATA x32 . EMPTY_PACKAGE(0) . TAIL: finish(0) completes, then bleSyncFileTail fires.",
    ),
    Cell(
        "no_empty_package", lambda: [],
        DETECTED, f"RUNTIME_PROVEN ({RUNTIME_S13} runs 1/1b/2/9: never completes) + BYTECODE_PROVEN (R9 has no finish; R10 not armed) + HARNESS_POLICY (P2)",
        "the pre-R7-S13 sequence HEAD . DATA . TAIL: all bytes arrive, the tail callback fires, bleDataComplete never does; the request stays pending at SDK level.",
        pkw=dict(empty_package_code=None),
    ),
    Cell(
        "empty_after_tail", lambda: [Fault(FaultKind.EMPTY_PACKAGE_AFTER, Kind.TAIL, param=DEFAULT_EMPTY_PACKAGE_CODE)],
        DETECTED, f"RUNTIME_PROVEN ({RUNTIME_S13} run 7: never completes) + BYTECODE_PROVEN (R12: the TAIL unregisters the receiver, 'File Sync Callback is Null') + HARNESS_POLICY (P2)",
        "HEAD . DATA . TAIL . EMPTY_PACKAGE(0): the sentinel arrives after its receiver is gone and is dropped.",
        pkw=dict(empty_package_code=None),
    ),
    Cell(
        "empty_code_1", lambda: [],
        RECOVERED, f"RUNTIME_PROVEN ({RUNTIME_S13} run 8) + BYTECODE_PROVEN (R2: code is only inspected on the while-recovering branch)",
        "EMPTY_PACKAGE(1) before the TAIL completes exactly like code 0.",
        pkw=dict(empty_package_code=1),
    ),
    Cell(
        "empty_code_7", lambda: [],
        RECOVERED, "BYTECODE_PROVEN (R2: any code finishes when H is clear); runtime UNKNOWN (U15: no run used a code other than 0/1)",
        "EMPTY_PACKAGE(7) before the TAIL completes; what real firmware puts in this byte is UNKNOWN.",
        pkw=dict(empty_package_code=7),
    ),
    Cell(
        "empty_package_duplicated", lambda: [Fault(FaultKind.DUPLICATE, Kind.EMPTY)],
        RECOVERED, "BYTECODE_PROVEN (R2 latches nothing; R12 keeps the registration until the TAIL)",
        "two sentinels -> finish(0) twice (the second inside the post-finish window, P7); bytes exact; app effect of a double bleDataComplete UNKNOWN.",
    ),
    Cell(
        "empty_package_wrong_session", lambda: [Fault(FaultKind.WRONG_SESSION, Kind.EMPTY, param=0x0000ABCD)],
        DETECTED, "BYTECODE_PROVEN (R1 gates the sentinel like any type-2 frame) + HARNESS_POLICY (P2)",
        "a mislabelled sentinel is dropped by the session gate; the TAIL then fires without a finish -> pending.",
    ),
    Cell(
        "empty_package_truncated", lambda: [Fault(FaultKind.TRUNCATE, Kind.EMPTY, param=10)],
        DETECTED, "UNKNOWN (R13: the code byte at i+5 is read by native TntBleCommUtils past the frame end) + HARNESS_POLICY (report, do not guess)",
        "a 10-byte sentinel has no code byte; the model refuses to guess what the SDK's native reader returns.",
    ),
    Cell(
        "tail_missing", lambda: [Fault(FaultKind.DROP, Kind.TAIL)],
        RECOVERED, f"BYTECODE_PROVEN (R2 finish has no TAIL dependency) + RUNTIME ({RUNTIME_S13}: completion callbacks fired before the TAIL in runs 3/4b/8; no run omitted the TAIL, so the app-level effect is UNKNOWN)",
        "finish(0) completes with all 1024 bytes; bleSyncFileTail never fires and the [28,29] registration stays until the app's stopSyncFile (run 5).",
    ),
    Cell(
        "tail_crc_corrupted", lambda: [Fault(FaultKind.CORRUPT, Kind.TAIL, param=(7, 0xFF))],
        RECOVERED, f"BYTECODE_PROVEN (R13: crc never verified) + RUNTIME ({RUNTIME_S13} runs 1b/2: 0xBEEF and 0 behave identically)",
        "byte-exact reassembly with a crc the device never sent; tolerated, as the ledger says.",
    ),
    Cell(
        "head_missing", lambda: [Fault(FaultKind.DROP, Kind.HEAD)],
        RECOVERED, "BYTECODE_PROVEN (R3, R2: HEAD not load-bearing)",
        "q$a keeps no HEAD state; the head callback simply never fires (app reaction UNKNOWN).",
    ),
    Cell(
        "head_status_nonzero", lambda: [Fault(FaultKind.HEAD_STATUS, Kind.HEAD, param=1)],
        RECOVERED, "BYTECODE_PROVEN (R8)",
        "z.c(29) marks the state and the head callback fires with status 1; DATA is still accepted. App reaction UNKNOWN.",
    ),
    Cell(
        "battery_push_interleaved", lambda: [Fault(FaultKind.INTERLEAVE_AFTER, Kind.DATA, offsets=(MID,), param=PlaudBatteryState(True, 42).encode())],
        RECOVERED, "BYTECODE_PROVEN (R12 dispatch by opcode)",
        "the opcode-9 push is routed by opcode and never reaches the receiver.",
    ),
    Cell(
        "data_duplicate", lambda: [Fault(FaultKind.DUPLICATE, Kind.DATA, offsets=(MID,))],
        RECOVERED, "BYTECODE_PROVEN (R5)",
        "the second copy is behind the cursor and is dropped silently; no recovery.",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SEQUENCE_CELLS, ids=[c.id for c in SEQUENCE_CELLS])
@pytest.mark.parametrize("pace", MODES)
async def test_sequence_cells(cell: Cell, pace: float | None) -> None:
    link, driver, result = await run_cell(cell.faults(), pace, **(cell.pkw or {}))
    outcome = outcome_of(result, FILE)
    mode = "atomic" if pace is None else "paced"
    record(f"{cell.id}[{mode}]", outcome, cell.expected, cell.evidence, cell.notes, result, FILE, mode=mode)
    assert result.restarts == cell.restarts and result.stop_syncs == cell.stop_syncs and result.timer_restarts == 0
    assert result.data == FILE, "every sequence cell delivers all the bytes; only completion differs"
    if cell.id == "clean_default_sequence":
        assert result.complete and result.tail_seen and result.finish_codes == [DEFAULT_EMPTY_PACKAGE_CODE]
    if cell.id == "no_empty_package":
        assert not result.complete and result.tail_seen and result.failure == "stalled_after_tail"
        assert result.finish_codes == [] and driver.host.registrations == []
    if cell.id == "empty_after_tail":
        assert not result.complete and result.tail_seen and result.failure == "stalled_after_tail"
        assert result.dropped_no_receiver == 1 and "File Sync Callback is Null" in result.events
    if cell.id == "empty_code_1":
        assert result.complete and result.finish_codes == [1]
    if cell.id == "empty_code_7":
        assert result.complete and result.finish_codes == [7]
    if cell.id == "empty_package_duplicated":
        assert result.finish_codes == [0, 0] and result.tail_seen
    if cell.id == "empty_package_wrong_session":
        assert not result.complete and result.session_mismatches == 1 and result.failure == "stalled_after_tail"
    if cell.id == "empty_package_truncated":
        assert not result.complete and result.failure is not None and result.failure.startswith("undecodable")
    if cell.id == "tail_missing":
        assert result.complete and not result.tail_seen and result.finish_codes == [0]
        assert len(driver.host.registrations) == 1, "nothing unregistered the receiver"
    if cell.id == "tail_crc_corrupted":
        assert result.tail_crc == (CRC ^ 0xFF) and result.tail_crc != CRC
    if cell.id == "head_status_nonzero":
        assert result.head_statuses == [1]
    if cell.id == "head_missing":
        assert result.head_statuses == []
    if cell.id == "battery_push_interleaved":
        assert result.other_frames == 1
    if cell.id == "data_duplicate":
        assert result.dropped_behind == 1


# --- the gap family: the SDK's own recovery, paced device that abandons its stream --


GAP_CELLS = [
    Cell(
        "data_drop_first", lambda: [Fault(FaultKind.DROP, Kind.DATA, offsets=(0,))],
        RECOVERED, f"BYTECODE_PROVEN (R6, R11) + RUNTIME ({RUNTIME_S13} runs 6b/6d: stopSync then y6 from the cursor, one restart)",
        "DATA@32 arrives at cursor 0: gap -> z6 -> device aborts its stream -> a7 -> y6(start=0); clean re-stream.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_drop_middle", lambda: [Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,))],
        RECOVERED, f"BYTECODE_PROVEN (R6, R11) + RUNTIME ({RUNTIME_S13} run 6b: drop@3200 -> one restart, byte-exact)",
        "gap at 512 -> restart from 512.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_drop_run_of_3", lambda: [Fault(FaultKind.DROP, Kind.DATA, offsets=(MID, MID + 32, MID + 64))],
        RECOVERED, "BYTECODE_PROVEN (R6, R11)",
        "one gap however many frames are missing: a single restart from 512.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_reordered", lambda: [Fault(FaultKind.REORDER, Kind.DATA, offsets=(320,))],
        RECOVERED, "BYTECODE_PROVEN (R6, R3, R11)",
        "DATA@352 first: gap (frame consumed, not buffered); DATA@320 then accepted; restart from 352.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_truncated_partial", lambda: [Fault(FaultKind.TRUNCATE, Kind.DATA, offsets=(MID,), param=15)],
        RECOVERED, "BYTECODE_PROVEN (R7 clamp, R6)",
        "5 of 32 declared bytes survive: cursor 517; next frame is a gap; restart from 517.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_truncated_header_only", lambda: [Fault(FaultKind.TRUNCATE, Kind.DATA, offsets=(MID,), param=10)],
        RECOVERED, "BYTECODE_PROVEN (R7 clamp, R6)",
        "zero payload bytes survive; cursor stays 512; next frame is a gap; restart from 512.", restarts=1, stop_syncs=1,
    ),
    Cell(
        "data_wrong_session_once", lambda: [Fault(FaultKind.WRONG_SESSION, Kind.DATA, offsets=(MID,), param=0x0000ABCD)],
        RECOVERED, "BYTECODE_PROVEN (R1, R6)",
        "mislabelled frame dropped by the session gate; the following frame is a gap.", restarts=1, stop_syncs=1,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", GAP_CELLS, ids=[c.id for c in GAP_CELLS])
async def test_gap_cells_recover_with_one_restart(cell: Cell) -> None:
    link, driver, result = await run_cell(cell.faults(), PACE)
    outcome = outcome_of(result, FILE)
    record(f"{cell.id}[paced]", outcome, cell.expected, cell.evidence, cell.notes, result, FILE, mode="paced+stream_in_task")
    assert result.restarts == cell.restarts and result.stop_syncs == cell.stop_syncs, result.events
    assert result.timer_restarts == 0 and result.complete and result.tail_seen
    assert aborted(link, "stop_sync"), link.peripheral.stream_log
    assert not run6_signature(result) and not any(e == "restart:empty_package_while_recovering" for e in result.events)
    assert result.finish_codes == [0], "exactly one sentinel reached the receiver: the restarted stream's"
    if cell.id == "data_wrong_session_once":
        assert result.session_mismatches == 1


# --- the run-6 device: a stream that is not abandoned ------------------------------


@pytest.mark.asyncio
async def test_old_stream_not_abandoned_is_the_run6_double_restart() -> None:
    """Atomic emission: the whole faulty stream is queued before the client
    reacts, so nothing can be abandoned -- the in-process form of R7-S13 run
    6. The old stream's EMPTY arrives while H is set -> isPacketLossStopSync
    restart; its TAIL then lands on the fresh receiver with no progress ->
    another restart. The receiver model's bytes are exact (P6), but the
    genuine SDK's export layer corrupted its output under this trace (P8),
    so the cell is recorded as `corruption_risk`, not `recovered`."""
    link, driver, result = await run_cell([Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,))], None)
    model = outcome_of(result, FILE)
    outcome = CORRUPTION_RISK if run6_signature(result) else model
    record(
        "old_stream_not_abandoned[atomic]", outcome, CORRUPTION_RISK,
        f"RUNTIME ({RUNTIME_S13} run 6: second restart, output 12 871 B corrupted) + BYTECODE_PROVEN (R2 while-H branch, R9, R12 newest-first) + HARNESS_POLICY (P8: export writer not modelled)",
        "the abandoned stream's EMPTY(0) triggers ---isPacketLossStopSync---, its TAIL a tail-without-progress restart; receiver-level bytes exact, real export corrupted.",
        result, FILE, mode="atomic", model_outcome=model,
    )
    assert result.restarts == 2 and result.stop_syncs == 1 and result.timer_restarts == 0
    assert "restart:empty_package_while_recovering" in result.events
    assert "restart:tail_without_progress" in result.events
    assert result.complete and result.data == FILE


@pytest.mark.asyncio
async def test_old_stream_not_abandoned_paced_never_converges_cleanly() -> None:
    """The same device in paced form (`abandon_stream_on_restart=False`,
    `cancel_stream_on_stop=False`): two streams drain concurrently and every
    frame of the stale one beyond the cursor is a fresh gap. The exact cascade
    is timing-dependent; what is not is that the client restarts more than
    once and the transfer is detected or carries the run-6 signature."""
    link, driver, result = await run_cell(
        [Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,))], PACE,
        abandon_stream_on_restart=False, cancel_stream_on_stop=False,
    )
    model = outcome_of(result, FILE)
    outcome = CORRUPTION_RISK if run6_signature(result) else model
    record(
        "old_stream_not_abandoned[paced]", outcome, (CORRUPTION_RISK, DETECTED),
        f"RUNTIME ({RUNTIME_S13} run 6 vs 6b) + HARNESS_POLICY (FaultyPeripheral.abandon_stream_on_restart=False, P3/P2/P8)",
        "the device keeps serving the stale stream: a cascade of gap -> z6 -> restart; never one clean restart.",
        result, FILE, mode="paced, no abort", model_outcome=model,
        not_abandoned=len(link.peripheral.stream_events("not_abandoned")),
    )
    assert result.restarts >= 2 and result.stop_syncs >= 2
    assert link.peripheral.stream_events("not_abandoned"), link.peripheral.stream_log


# --- the last-frame case: the SDK completes SHORT -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pace", MODES)
async def test_data_drop_last_completes_short_then_app_resumes(pace: float | None) -> None:
    """R2: the sentinel arrives at cursor 992 (progress made, no resend
    pending) and finish(0) fires; q$a never compares the cursor with the file
    size. Only P4 (size check) + P5 (app resume from the cursor) turn that
    into a byte-exact file."""
    link = await bring_up(peripheral_factory([Fault(FaultKind.DROP, Kind.DATA, offsets=(LAST_OFFSET,))], pace))
    driver = await driver_for(link, **TIMING)

    async def same_driver() -> SdkTransferDriver:
        return driver

    trace = await sync_with_app_resume(same_driver, SID, len(FILE))
    first, result = trace.results[0], trace.final
    assert first.complete and first.failure == "finish_short" and first.cursor == LAST_OFFSET and first.tail_seen
    assert first.data == FILE[:LAST_OFFSET] and outcome_of(first, FILE) == DETECTED
    outcome = outcome_of(result, FILE, trace.resumes)
    mode = "atomic" if pace is None else "paced"
    record(
        f"data_drop_last[{mode}]", outcome, RECOVERED_BY_APP_RESUME,
        "BYTECODE_PROVEN (R2 finishes short) + HARNESS_POLICY (P4 size check, P5 resume)",
        "the SDK receiver reports completion at 992/1024 bytes; without the app-level size check this is a short file.",
        result, FILE, mode=mode, app_resumes=trace.resumes, sdk_level_cursor_at_finish=first.cursor,
    )
    assert trace.resumes == 1 and result.restarts == 0 and result.finish_codes == [0]


# --- under-length frames -------------------------------------------------------


@pytest.mark.asyncio
async def test_data_truncated_below_header_is_reported_not_guessed() -> None:
    link, driver, result = await run_cell([Fault(FaultKind.TRUNCATE, Kind.DATA, offsets=(MID,), param=7)], None)
    outcome = outcome_of(result, FILE)
    record(
        "data_truncated_below_header[atomic]", outcome, DETECTED,
        "UNKNOWN (R13: TntBleCommUtils.readInt is native) + HARNESS_POLICY (report, do not guess)",
        "a 7-byte type-2 frame has no offset field; the SDK's native reader behaviour is not in the bytecode.",
        result, FILE, mode="atomic",
    )
    assert result.failure is not None and result.failure.startswith("undecodable")
    assert result.data == FILE[:MID]


# --- the stall timer (R10) end to end ------------------------------------------


@pytest.mark.asyncio
async def test_a7_lost_restart_comes_from_the_stall_timer() -> None:
    """Drop the a7 ack: the z6 goes out, the device aborts its stream,
    nothing else arrives, and after the 5 s budget (scaled) q$a.b(JJ..)
    restarts from the cursor."""
    faults = [
        Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,)),
        Fault(FaultKind.DROP, Kind.OTHER, opcodes=(OPCODE_STOP_SYNC_RSP,)),
    ]
    link, driver, result = await run_cell(faults, PACE, timing=dict(stall_timeout=0.3, idle_timeout=1.0, max_restarts=8))
    outcome = outcome_of(result, FILE)
    record(
        "stop_ack_lost_timer_restart[paced]", outcome, RECOVERED,
        "BYTECODE_PROVEN (R6, R10, R11) + HARNESS_POLICY (P1 scaled to 0.3 s)",
        "z6 sent, a7 never arrives: the countdown expires with H set and restarts from the cursor.",
        result, FILE, mode="paced+stream_in_task",
    )
    assert result.stop_syncs == 1 and result.timer_restarts == 1 and result.restarts == 1
    assert "timer_armed" in result.events and result.complete


# --- stop / restart driven from the app side -----------------------------------


@pytest.mark.asyncio
async def test_stop_sync_mid_transfer_then_restart() -> None:
    link = await bring_up(peripheral_factory([], PACE))
    driver = await driver_for(link, **TIMING)

    async def stop_at_256(cursor: int) -> bool:
        return cursor >= 256

    first = await driver.sync_file(SID, 0, 0, expected_size=len(FILE), on_progress=stop_at_256)
    assert first.failure == "app_stopped" and 256 <= first.cursor < len(FILE)
    emitted = link.peripheral.emitted
    ack = next(i for i, e in enumerate(emitted) if e.get("opcode") == OPCODE_STOP_SYNC_RSP)
    assert all(e.get("kind") not in ("data", "empty_package", "tail") for e in emitted[ack + 1 :]), "the device kept streaming after acking the stop"
    assert aborted(link, "stop_sync")

    result = await driver.sync_file(SID, first.cursor, 0, expected_size=len(FILE), sink=bytearray(first.data))
    outcome = outcome_of(result, FILE, app_resumes=1)
    record(
        "stop_sync_mid_transfer_then_restart[paced]", outcome, RECOVERED_BY_APP_RESUME,
        f"BYTECODE_PROVEN (z6/a7 opcodes, y6 from cursor) + RUNTIME ({RUNTIME_S13} run 5: stopSyncFile -> 29 -> a7) + HARNESS_POLICY (app-initiated stop, emulator aborts its stream)",
        "the emulator answers a7 and emits nothing of the old stream after it; the resume from the cursor is byte-exact.",
        result, FILE, mode="paced+stream_in_task", app_resumes=1, stopped_at=first.cursor,
    )
    assert result.restarts == 0 and result.data_frames == (len(FILE) - first.cursor) // PAYLOAD


@pytest.mark.asyncio
async def test_link_disconnect_mid_transfer_reconnect_and_resume() -> None:
    link = await bring_up(peripheral_factory([Fault(FaultKind.DISCONNECT_AFTER, Kind.DATA, offsets=(MID,))], PACE))
    state: dict[str, Any] = {"link": link, "driver": None, "reconnects": 0}

    async def make() -> SdkTransferDriver:
        if state["driver"] is not None and state["driver"].link_lost:
            state["link"] = await reconnect(state["link"])
            state["reconnects"] += 1
        state["driver"] = await driver_for(state["link"], **TIMING)
        return state["driver"]

    trace = await sync_with_app_resume(make, SID, len(FILE))
    result = trace.final
    assert trace.results[0].failure == "link_lost" and trace.results[0].cursor == MID + PAYLOAD
    outcome = outcome_of(result, FILE, trace.resumes)
    record(
        "link_disconnect_then_resume[paced]", outcome, RECOVERED_BY_APP_RESUME,
        "BYTECODE_PROVEN (y6 start=cursor is the SDK's resume primitive; q.a aborts while disconnected, ALL.txt:39892) + HARNESS_POLICY (P5 reconnect)",
        "the device drops the link after DATA@512; after reconnect + MTU + discovery the app resumes from 544.",
        result, FILE, mode="paced+stream_in_task", app_resumes=trace.resumes, reconnects=state["reconnects"], lost_at=trace.results[0].cursor,
    )
    assert state["reconnects"] == 1 and trace.resumes == 1
    assert link.peripheral.stream_events("link_dropped_by_device")


# --- file list -------------------------------------------------------------------

SIX = [{"session_id": 0x1000 + i, "file_size": 100 * (i + 1), "scene": 2, "attribute": 0} for i in range(6)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault, cell, notes",
    [
        (
            Fault(FaultKind.CUT_BEFORE, Kind.FILE_LIST, indices=(1,)), "file_list_interrupted_mid_page",
            "the device goes silent after page 0 of 3; opcode 26 is sticky and nothing times it out.",
        ),
        (
            Fault(FaultKind.DROP, Kind.FILE_LIST, indices=(1,)), "file_list_page_dropped",
            "page 2 carries frameStartIndex 4 while 2 entries are held: ignored; the list never completes.",
        ),
    ],
    ids=["interrupted", "dropped"],
)
async def test_file_list_faults_stall_forever(fault: Fault, cell: str, notes: str) -> None:
    def factory(device: Any) -> FaultyPeripheral:
        return FaultyPeripheral(device, faults=[fault], file_bytes=FILE, tail_crc=CRC, file_table=[dict(e) for e in SIX], file_list_per_frame=2)

    link = await bring_up(factory)
    driver = await driver_for(link, **TIMING)
    acc, verdicts = await driver.get_file_list(STAMP)
    outcome = DETECTED if not acc.complete else RECOVERED
    record(
        cell, outcome, DETECTED,
        "BYTECODE_PROVEN (s5/q2 frameStartIndex gate, ledger S5.6) + HARNESS_POLICY (P2 idle timeout)",
        notes, entries=len(acc.entries), totals=acc.totals, verdicts=verdicts,
    )
    assert acc.totals == 6 and len(acc.entries) == 2 and verdicts[-1] == "stalled"
    if cell == "file_list_page_dropped":
        assert "ignored_index_mismatch" in verdicts
    # a fresh request with a new stamp is unaffected: the fault was one-shot
    acc2, verdicts2 = await driver.get_file_list(STAMP + 1)
    assert acc2.complete and acc2.entries == SIX


# --- MTU ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mtu", [23, 185, 247, 517])
async def test_mtu_variants_with_frames_fitted_to_the_mtu(mtu: int) -> None:
    link, driver, result = await run_cell([], None, mtu=None if mtu == 23 else mtu, fit_payload_to_mtu=True)
    assert link.connection.att_mtu == mtu
    payload = min(255, mtu - 3 - 10)
    expected_frames = math.ceil(len(FILE) / payload)
    outcome = outcome_of(result, FILE)
    record(
        f"mtu_{mtu}_fitted[atomic]", outcome, RECOVERED,
        "HARNESS_POLICY (frame size is not pinned by the SDK, ledger S12) + BYTECODE_PROVEN (R3/R7 reassembly, R2 finish)",
        f"payload {payload} B/frame -> {expected_frames} DATA frames + sentinel, none truncated.",
        result, FILE, mode="atomic", att_mtu=mtu, payload=payload,
    )
    assert result.restarts == 0 and result.data_frames == expected_frames and result.complete
    sizes = [e["len"] for e in link.peripheral.emitted if e.get("kind") == "data"]
    assert len(sizes) == expected_frames and max(sizes) <= mtu - 3
    assert sum(1 for e in link.peripheral.emitted if e.get("kind") == "empty_package") == 1


@pytest.mark.asyncio
async def test_mtu_23_with_unsized_frames_converges_one_frame_per_restart() -> None:
    """Base-peripheral frame size (32 B payload) over a 23-byte MTU: Bumble
    truncates every notification to 20 bytes (gatt_server.py:425-426), the
    receiver clamps to 10 payload bytes (R7), and the frame after a truncated
    one is a gap (R6). Each restart therefore advances exactly one truncated
    frame -- 10 bytes -- for as long as the restarted stream has a SECOND
    DATA frame to expose the gap: starts 0..60 do (96 - start > 32), so 7
    restarts (from 10, 20, ..., 70). The stream from 70 is a single 26-byte
    chunk, truncated to 10, followed by the 11-byte sentinel (which fits):
    R2 finishes SHORT at 80. Two app-level resumes (P4/P5) finish it:
    80 -> 90 (short again), 90 -> 96 (a 16-byte frame fits and is intact).
    Paced, so each stopSync aborts the stale stream (as in the gap cells)."""
    small = FILE[:96]
    link = await bring_up(peripheral_factory([], PACE, small), mtu=None)
    assert link.connection.att_mtu == 23
    driver = await driver_for(link, stall_timeout=0.4, idle_timeout=0.6, max_restarts=16)

    async def same_driver() -> SdkTransferDriver:
        return driver

    trace = await sync_with_app_resume(same_driver, SID, len(small))
    result = trace.final
    outcome = outcome_of(result, small, trace.resumes)
    record(
        "mtu_23_unsized_frames[paced]", outcome, RECOVERED_BY_APP_RESUME,
        "BYTECODE_PROVEN (R7 clamp, R6, R2 finishes short, R11) + HARNESS_POLICY (32-byte frames, a truncating link, P4/P5)",
        "7 SDK restarts advance one truncated frame each; the SDK then completes short at 80/96; two app resumes reach 96.",
        result, small, mode="paced+stream_in_task", app_resumes=trace.resumes,
        sdk_restarts=[r.restarts for r in trace.results], cursors=[r.cursor for r in trace.results],
    )
    assert [r.restarts for r in trace.results] == [7, 0, 0]
    assert [r.failure for r in trace.results] == ["finish_short", "finish_short", None]
    assert [r.cursor for r in trace.results] == [80, 90, 96]
    assert sum(r.data_frames for r in trace.results) == 10


# --- CCCD mode --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("prefer_notify", [True, False], ids=["notify", "indicate"])
async def test_cccd_mode_clean_and_with_a_gap(prefer_notify: bool) -> None:
    link, driver, result = await run_cell([Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,))], PACE, prefer_notify=prefer_notify)
    # The server keys its CCCD table by ITS connection object, not the central's.
    cccd = link.peripheral.device.gatt_server.subscribers[link.peripheral.connection][link.peripheral.data_characteristic.handle]
    assert cccd[0] == (0x01 if prefer_notify else 0x02)
    # Bumble's client delivers a frame to this callback only through the
    # subscriber set the CCCD write selected, so received frames prove the mode.
    assert (link.peripheral.data_characteristic.handle in link.peer.gatt_client.indication_subscribers) is (not prefer_notify)
    outcome = outcome_of(result, FILE)
    name = "notify" if prefer_notify else "indicate"
    record(
        f"cccd_{name}_with_gap[paced]", outcome, RECOVERED,
        "BYTECODE_PROVEN (z.a(boolean) copes with either CCCD mode, ledger S12) + HARNESS_POLICY (2BB0 advertises both)",
        f"one gap recovered under {name}; the recovery path is transport-mode agnostic.",
        result, FILE, mode="paced+stream_in_task",
    )
    assert result.restarts == 1 and result.complete


# --- delete of the file being synced --------------------------------------------


@pytest.mark.asyncio
async def test_delete_of_the_file_being_synced() -> None:
    link = await bring_up(peripheral_factory([], PACE))
    driver = await driver_for(link, **TIMING)
    deleted = {"done": False}

    async def delete_at_256(cursor: int) -> bool:
        if cursor >= 256 and not deleted["done"]:
            deleted["done"] = True
            await driver.write(pack_delete_file_request(SID))
        return False

    result = await driver.sync_file(SID, 0, 0, expected_size=len(FILE), on_progress=delete_at_256)
    outcome = outcome_of(result, FILE)
    record(
        "delete_file_being_synced[paced]", outcome, DETECTED,
        "HARNESS_POLICY (the emulator aborts the stream on a delete of the active session; real device: UNKNOWN) + HARNESS_POLICY (P2)",
        "the x6 ack is routed as 'other'; the stream stops before its sentinel; the receiver stalls with a prefix; the list no longer holds the entry.",
        result, FILE, mode="paced+stream_in_task",
    )
    assert result.failure == "stalled" and 256 <= len(result.data) < len(FILE) and not result.complete
    assert result.other_frames == 1, "exactly the x6 ack reached the dispatcher outside the receiver"
    assert aborted(link, "delete_of_streaming_session")
    acc, _ = await driver.get_file_list(STAMP)
    assert acc.complete and acc.entries == []


# --- file-size extremes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_length_file_is_a_restart_livelock() -> None:
    """R2 ignores the sentinel without progress and R9 makes a TAIL with
    cursor == start a restart, unconditionally. Nothing in q$a can complete a
    zero-length stream; the driver's cap (P3) is what terminates it."""
    link, driver, result = await run_cell([], None, file_bytes=b"")
    outcome = outcome_of(result, b"")
    record(
        "zero_length_file[atomic]", outcome, DETECTED,
        "BYTECODE_PROVEN (R2 needs progress; R9 tail-without-progress restart; no terminal state) + HARNESS_POLICY (P3 cap, emulator answers HEAD+EMPTY+TAIL); runtime UNKNOWN",
        "the sink is (trivially) exact but completion never fires; the real SDK would restart forever.",
        result, b"", mode="atomic",
    )
    assert result.failure == "restart_cap_exceeded" and result.restarts == TIMING["max_restarts"] + 1
    assert all(e.startswith("restart:tail_without_progress") for e in result.events if e.startswith("restart"))
    assert result.finish_codes == []


@pytest.mark.asyncio
async def test_file_of_exactly_one_frame() -> None:
    one = FILE[:PAYLOAD]
    link, driver, result = await run_cell([], None, file_bytes=one)
    outcome = outcome_of(result, one)
    record("file_one_frame[atomic]", outcome, RECOVERED, "BYTECODE_PROVEN (R3, R2, R9)", "HEAD, one DATA, EMPTY_PACKAGE, TAIL.", result, one, mode="atomic")
    assert result.data_frames == 1 and result.restarts == 0 and result.complete


@pytest.mark.asyncio
async def test_file_of_several_thousand_frames() -> None:
    big = bytes(RNG.getrandbits(8) for _ in range(4096 * PAYLOAD))
    link, driver, result = await run_cell([], None, file_bytes=big, timing=dict(stall_timeout=0.4, idle_timeout=5.0, max_restarts=8))
    outcome = outcome_of(result, big)
    record(
        "file_4096_frames[atomic]", outcome, RECOVERED, "BYTECODE_PROVEN (R3, R2, R9)",
        f"{len(big)} bytes in 4096 frames, no restarts, {result.elapsed_s:.2f} s over the virtual link.",
        result, big, mode="atomic",
    )
    assert result.data_frames == 4096 and result.restarts == 0 and result.complete


@pytest.mark.asyncio
async def test_persistent_wrong_session_id_never_completes() -> None:
    faults = [
        Fault(FaultKind.WRONG_SESSION, Kind.DATA, param=0xABCD, once=False),
        Fault(FaultKind.WRONG_SESSION, Kind.EMPTY, param=0xABCD, once=False),
    ]
    link, driver, result = await run_cell(faults, None)
    outcome = outcome_of(result, FILE)
    record(
        "data_wrong_session_persistent[atomic]", outcome, DETECTED,
        "BYTECODE_PROVEN (R1 gate, R9 restart) + HARNESS_POLICY (P3 cap)",
        "every type-2 frame fails the session gate; each TAIL arrives without progress and restarts; nothing is ever written.",
        result, FILE, mode="atomic",
    )
    assert result.failure == "restart_cap_exceeded" and result.data == b"" and result.session_mismatches > 0


# --- two files, back to back --------------------------------------------------------


@pytest.mark.asyncio
async def test_back_to_back_syncs_of_two_files() -> None:
    def factory(device: Any) -> FaultyPeripheral:
        return FaultyPeripheral(
            device,
            faults=[Fault(FaultKind.DROP, Kind.DATA, offsets=(MID,))],   # fires once, in the first file
            pace=PACE,
            files={SID: FILE, SID_B: FILE_B},
            file_bytes=FILE,
            tail_crc=CRC,
            file_table=[dict(e) for e in TABLE] + [{"session_id": SID_B, "file_size": len(FILE_B), "scene": 2, "attribute": 0}],
        )

    link = await bring_up(factory)
    driver = await driver_for(link, **TIMING)
    a = await driver.sync_file(SID, 0, 0, expected_size=len(FILE))
    b = await driver.sync_file(SID_B, 0, 0, expected_size=len(FILE_B))
    oa, ob = outcome_of(a, FILE), outcome_of(b, FILE_B)
    record(
        "back_to_back_two_files[paced]", RECOVERED if oa == ob == RECOVERED else DETECTED, RECOVERED,
        "BYTECODE_PROVEN (R1 gates frames by session; R12 the TAIL unregisters the first receiver) + HARNESS_POLICY (per-session content)",
        "file A recovers from a gap, file B (777 B, short last frame) is exact; no cross-talk between sessions.",
        b, FILE_B, mode="paced+stream_in_task", first_restarts=a.restarts,
    )
    assert a.restarts == 1 and b.restarts == 0 and b.data_frames == math.ceil(len(FILE_B) / PAYLOAD)
    assert a.complete and b.complete and a.tail_seen and b.tail_seen
    for frame in driver.frames:
        if frame[0] == 2:
            assert parse_file_data_frame(frame, 7)["session_id"] in (SID, SID_B)


# --- a second central during a transfer ----------------------------------------------


@pytest.mark.asyncio
async def test_second_central_control_traffic_during_a_transfer() -> None:
    first, connect_second = await bring_up_two_centrals(peripheral_factory([], PACE))
    driver_a = await driver_for(first, **TIMING)
    second: dict[str, Any] = {}

    async def at_128(cursor: int) -> bool:
        if cursor >= 128 and not second:
            link_b = await connect_second()
            driver_b = await driver_for(link_b, **TIMING)
            second["reply"] = await driver_b.get_state()
        return False

    result = await driver_a.sync_file(SID, 0, 0, expected_size=len(FILE), on_progress=at_128)
    outcome = outcome_of(result, FILE)
    record(
        "second_connection_control_traffic[paced]", outcome, RECOVERED,
        "HARNESS_POLICY (Bumble accepts a second central; per-connection CCCD routing in _respond)",
        "central B connects mid-stream and reads getState; central A's transfer is unaffected.",
        result, FILE, mode="paced+stream_in_task", second_reply_len=len(second.get("reply") or b""),
    )
    assert result.restarts == 0 and second.get("reply") is not None and len(second["reply"]) == 18
    assert len(first.devices[1].connections) == 2


@pytest.mark.asyncio
async def test_second_central_competing_sync_takes_the_single_transfer_slot() -> None:
    first, connect_second = await bring_up_two_centrals(peripheral_factory([], PACE))
    driver_a = await driver_for(first, **TIMING)
    second: dict[str, Any] = {}

    async def at_128(cursor: int) -> bool:
        if cursor >= 128 and not second:
            link_b = await connect_second()
            driver_b = await driver_for(link_b, **TIMING)
            second["result"] = await driver_b.sync_file(SID, 0, 0, expected_size=len(FILE))
        return False

    a = await driver_a.sync_file(SID, 0, 0, expected_size=len(FILE), on_progress=at_128)
    b = second["result"]
    oa, ob = outcome_of(a, FILE), outcome_of(b, FILE)
    assert ob == RECOVERED and b.restarts == 0
    assert a.failure == "stalled" and 128 <= len(a.data) < len(FILE)
    resumed = await driver_a.sync_file(SID, a.cursor, 0, expected_size=len(FILE), sink=bytearray(a.data))
    oa2 = outcome_of(resumed, FILE, app_resumes=1)
    record(
        "second_connection_competing_sync[paced]", oa2 if oa == DETECTED else DETECTED, RECOVERED_BY_APP_RESUME,
        "HARNESS_POLICY (single transfer slot: a second y6 aborts the active stream, ledger S12) + HARNESS_POLICY (P2, P5)",
        "B's y6 takes the slot: A stalls with a prefix, B is exact, A resumes from its cursor and is exact.",
        resumed, FILE, mode="paced+stream_in_task", a_stalled_at=a.cursor, b_outcome=ob,
    )
    assert aborted(first, "new_sync_start")


# --- documented, deliberately outside the matrix invariant ------------------------


@pytest.mark.asyncio
async def test_payload_bitflip_is_invisible_to_the_sdk_receiver() -> None:
    """NOT a matrix cell: a flipped payload byte is accepted because the SDK has
    no integrity check (R13: the TAIL crc is never verified, and DATA carries
    none). Over real BLE the link-layer CRC would reject the PDU; over Bumble's
    virtual link nothing does. Recorded so the limitation is explicit, not
    hidden behind the matrix invariant."""
    link, driver, result = await run_cell([Fault(FaultKind.CORRUPT, Kind.DATA, offsets=(MID,), param=(10, 0x80))], None)
    assert result.complete and result.failure is None and result.restarts == 0
    assert result.data != FILE and len(result.data) == len(FILE)
    assert bytes([result.data[MID] ^ 0x80]) == FILE[MID : MID + 1] and result.data[MID + 1 :] == FILE[MID + 1 :]
    RECORDS.append(
        {
            "cell": "data_payload_bitflip[atomic]",
            "outcome": "undetectable",
            "expected": "undetectable",
            "evidence": "BYTECODE_PROVEN (no payload integrity check in q$a; TAIL crc unverified)",
            "notes": "excluded from the invariant by design; the only protection is the BLE link-layer CRC, which the virtual link does not model.",
            "bytes": "corrupted",
            "mode": "atomic",
        }
    )
