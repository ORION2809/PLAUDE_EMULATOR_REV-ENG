"""R3 transfer tests over the real Bumble virtual GATT path.

The emulator speaks the evidence-backed q$a transfer family, file-list (p2/q2),
resume (b5/c5) and transfer start (y6). Synthetic file bytes and tables
throughout; nothing here is presented as device behaviour.

The link is brought up the way the Android SDK does -- MTU exchange first, then
discovery -- because `discoverServices()` has exactly one call site in the
shipped SDK and it is inside `onMtuChanged`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from struct import pack

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk

from plaudsim.filesync import parse_file_data_frame, parse_file_list_frame, parse_sync_head
from plaudsim.profile import PlaudPeripheral
from plaudsim.transfer import FileListAccumulator, pack_file_list_frame, pack_sync_tail, pack_empty_package_frame  # R7-S13

SESSION = 0x12345678
STAMP = 0x655A1B00
FILE_BYTES = bytes(range(96))
TABLE = [
    {"session_id": 0x12345678, "file_size": 96, "scene": 2, "attribute": 1},
    {"session_id": 0x11111111, "file_size": 0, "scene": 3, "attribute": 0},
]
CRC = 0xBEEF


def y6(session: int, start: int, end: int) -> bytes:
    return b"\x01\x1c\x00" + pack("<III", session, start, end)


def b5(session: int, scene: int) -> bytes:
    return b"\x01\x16\x00" + pack("<I", session) + pack("<B", scene)


def p2(stamp: int, start_session: int, flag: int) -> bytes:
    return b"\x01\x1a\x00" + pack("<II", stamp, start_session) + pack("<B", flag)


async def linked(**kwargs):
    return await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device,
            file_bytes=FILE_BYTES,
            file_table=[dict(e) for e in TABLE],
            tail_crc=CRC,
            **kwargs,
        )
    )


@pytest.mark.asyncio
async def test_transfer_full_sequence() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)

    assert len(responses) == 6          # HEAD + 3 DATA (96 / 32) + EMPTY_PACKAGE + TAIL (R7-S13)
    assert parse_sync_head(responses[0]) == {"session_id": SESSION, "status": 0}
    for i, off in enumerate((0, 32, 64)):
        parsed = parse_file_data_frame(responses[1 + i], port_version=7)
        assert parsed["session_id"] == SESSION
        assert parsed["offset"] == off
        assert parsed["length"] == 32
        assert parsed["payload"] == FILE_BYTES[off : off + 32]
    assert responses[4] == pack_empty_package_frame(0, SESSION, 7)  # R7-S13: the client completes on this
    assert responses[5] == pack_sync_tail(SESSION, CRC)
    log = peripheral.packet_log
    assert log[0] == {"direction": "request", "bytes": y6(SESSION, 0, 0).hex()}
    assert [e["direction"] for e in log[1:]] == ["response"] * 6  # + EMPTY_PACKAGE (R7-S13)


@pytest.mark.asyncio
async def test_transfer_offsets_are_contiguous_and_cover_the_file() -> None:
    """The SDK's gap detector is `if (d2 - this.a != 0)`, so a conforming
    device must emit offsets that exactly continue the host's cursor."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)

    cursor = 0
    body = b""
    for frame in responses[1:-2]:  # [-2] is the EMPTY_PACKAGE sentinel (R7-S13)
        parsed = parse_file_data_frame(frame, port_version=7)
        assert parsed["offset"] == cursor, "a gap here would trigger SDK resend"
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    assert body == FILE_BYTES
    assert cursor == len(FILE_BYTES)


@pytest.mark.asyncio
async def test_transfer_resume_from_offset() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 64, 0), with_response=True)

    assert len(responses) == 4  # HEAD + DATA@64 + EMPTY_PACKAGE + TAIL (R7-S13)
    parsed = parse_file_data_frame(responses[1], port_version=7)
    assert parsed["offset"] == 64
    assert parsed["payload"] == FILE_BYTES[64:]
    assert responses[2] == pack_empty_package_frame(0, SESSION, 7)  # R7-S13
    assert responses[3] == pack_sync_tail(SESSION, CRC)


@pytest.mark.asyncio
async def test_injected_gap_is_detectable_by_the_sdk_rule() -> None:
    """Adversarial: drop the middle DATA frame and prove a cursor-tracking host
    sees a discontinuity. This is the fault the SDK recovers from by re-issuing
    syncFileStart(sessionId, lastPosition); the emulator does not itself
    retransmit, and nothing here claims it does."""
    devices, peripheral, peer, data, command = await linked(drop_data_offsets=(32,))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)

    assert len(responses) == 5          # HEAD + 2 DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    cursor = 0
    gap_at = None
    for frame in responses[1:-2]:
        parsed = parse_file_data_frame(frame, port_version=7)
        if parsed["offset"] != cursor:
            gap_at = (cursor, parsed["offset"])
            break
        cursor += len(parsed["payload"])
    assert gap_at == (32, 64), "host cursor 32, device sent 64"


@pytest.mark.asyncio
async def test_wrong_session_replaces_the_active_transfer() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)
    assert len(responses) == 6  # HEAD + 3 DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    await peer.write_value(command, y6(0x11111111, 0, 0), with_response=True)
    assert len(responses) == 12
    assert parse_sync_head(responses[6])["session_id"] == 0x11111111
    # Every frame of the second run carries the new session id: a host gating on
    # its requested session would otherwise drop them all.
    for frame in responses[7:10]:
        assert parse_file_data_frame(frame, port_version=7)["session_id"] == 0x11111111


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        bytes([0x01, 0x1C, 0x00, 0x01]),                       # truncated y6
        b"\x01\x1c\x00" + pack("<III", 1, 2, 3) + b"\x00",     # one byte too many
        b"\x01\x1c\x01" + pack("<III", 1, 2, 3),               # opcode 284, not 28
        b"\x02\x1c\x00" + pack("<III", 1, 2, 3),               # protocol type 2
        b"\x01\x1a\x00" + pack("<III", 1, 2, 3),               # right length, wrong opcode
    ],
)
async def test_malformed_requests_are_rejected_without_a_response(bad: bytes) -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, bad, with_response=True)
    assert responses == [], f"emulator answered a malformed request: {bad.hex()}"
    assert peripheral.packet_log[-1]["direction"] == "error"


@pytest.mark.asyncio
async def test_write_to_the_notify_characteristic_is_ignored() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    n_before = len(peripheral.packet_log)
    await peer.write_value(data, y6(SESSION, 0, 0), with_response=True)
    assert len(peripheral.packet_log) == n_before
    assert responses == []


@pytest.mark.asyncio
async def test_file_list_echoes_the_request_stamp() -> None:
    """s5.a(byte[]) drops any frame whose u32@3 differs from the stamp the host
    sent, so echoing it is mandatory, not cosmetic."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, p2(STAMP, 0, 0), with_response=True)

    assert len(responses) == 1
    parsed = parse_file_list_frame(responses[0])
    assert parsed["request_stamp"] == STAMP
    assert parsed["totals"] == 2
    assert parsed["frame_start_index"] == 0
    assert parsed["entries"] == TABLE

    await peer.write_value(command, p2(0xAABBCCDD, 0, 0), with_response=True)
    assert parse_file_list_frame(responses[1])["request_stamp"] == 0xAABBCCDD


@pytest.mark.asyncio
async def test_file_list_pages_with_correct_start_indices() -> None:
    devices, peripheral, peer, data, command = await linked(file_list_per_frame=1)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, p2(STAMP, 0, 0), with_response=True)

    assert len(responses) == 2
    acc = FileListAccumulator(request_stamp=STAMP)
    for frame in responses:
        assert acc.ingest_frame(frame) == "accepted"
    assert acc.complete
    assert acc.entries == TABLE


@pytest.mark.asyncio
async def test_resume_record_round_trip() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, b5(SESSION, 5), with_response=True)

    from plaudsim.filesync import parse_resume_record_response

    assert len(responses) == 1
    parsed = parse_resume_record_response(responses[0])
    assert parsed["session_id"] == SESSION
    assert parsed["scene"] == 5, "the request's scene must be echoed"
    assert len(responses[0]) == 17


@pytest.mark.asyncio
async def test_transfer_and_state_frames_are_not_confusable() -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)
    assert responses[0][:3] == b"\x01\x1c\x00"
    assert responses[0][:3] != b"\x01\x03\x00"
    with pytest.raises(ValueError):
        parse_file_list_frame(responses[0])


# --- host-side accumulator (pure) ----------------------------------------


def frame(stamp: int, totals: int, start: int, entries: list[dict[str, int]]) -> bytes:
    return pack_file_list_frame(stamp, totals, entries, frame_start_index=start)


def test_accumulator_accepts_ordered_frames() -> None:
    acc = FileListAccumulator(request_stamp=STAMP)
    assert acc.ingest_frame(frame(STAMP, 2, 0, [TABLE[0]])) == "accepted"
    assert not acc.complete
    assert acc.ingest_frame(frame(STAMP, 2, 1, [TABLE[1]])) == "accepted"
    assert acc.complete
    assert acc.entries == TABLE


def test_accumulator_drops_frames_for_a_different_request_stamp() -> None:
    acc = FileListAccumulator(request_stamp=STAMP)
    assert acc.ingest_frame(frame(0xDEADBEEF, 2, 0, [TABLE[0]])) == "dropped_stamp_mismatch"
    assert acc.entries == []


def test_accumulator_ignores_duplicate_and_reordered_frames() -> None:
    """The frameStartIndex gate is what makes retransmission harmless: a frame
    whose start index is not the current count contributes nothing."""
    acc = FileListAccumulator(request_stamp=STAMP)
    first = frame(STAMP, 2, 0, [TABLE[0]])
    assert acc.ingest_frame(first) == "accepted"
    assert acc.ingest_frame(first) == "ignored_index_mismatch"      # exact duplicate
    assert acc.ingest_frame(frame(STAMP, 2, 0, [TABLE[1]])) == "ignored_index_mismatch"
    assert acc.ingest_frame(frame(STAMP, 2, 5, [TABLE[1]])) == "ignored_index_mismatch"
    assert len(acc.entries) == 1
    assert acc.ingest_frame(frame(STAMP, 2, 1, [TABLE[1]])) == "accepted"
    assert acc.complete


def test_accumulator_takes_totals_from_the_first_frame_only() -> None:
    """q2.<init> runs once; later frames' totals fields are never read."""
    acc = FileListAccumulator(request_stamp=STAMP)
    acc.ingest_frame(frame(STAMP, 2, 0, [TABLE[0]]))
    assert acc.ingest_frame(frame(STAMP, 99, 1, [TABLE[1]])) == "accepted"
    assert acc.totals == 2
    assert acc.complete


def test_accumulator_overshoot_never_completes() -> None:
    """Nothing caps accumulation at totals, and completion is equality, so a
    device that sends more entries than it promised wedges the transfer
    forever. Real SDK behaviour, asserted so it is not silently 'fixed'."""
    acc = FileListAccumulator(request_stamp=STAMP)
    assert acc.ingest_frame(frame(STAMP, 1, 0, TABLE)) == "accepted"
    assert len(acc.entries) == 2
    assert acc.totals == 1
    assert not acc.complete
    assert acc.ingest_frame(frame(STAMP, 1, 2, [TABLE[0]])) == "accepted"
    assert not acc.complete


def test_accumulator_header_only_frame_sets_totals_without_entries() -> None:
    header = bytes([0x01, 0x1A, 0x00]) + pack("<I", STAMP) + pack("<H", 3)
    acc = FileListAccumulator(request_stamp=STAMP)
    assert acc.ingest_frame(header) == "header_only"
    assert acc.totals == 3
    assert acc.entries == []
    assert not acc.complete


def test_empty_table_completes_immediately() -> None:
    acc = FileListAccumulator(request_stamp=STAMP)
    assert acc.ingest_frame(frame(STAMP, 0, 0, [])) == "accepted"
    assert acc.complete
    assert acc.entries == []


# --- the SDK's own recovery path ----------------------------------------


@pytest.mark.asyncio
async def test_stop_sync_is_answered_with_opcode_30() -> None:
    """The SDK sends z6 (opcode 29 INBOUND, 3 bytes) the moment it sees a DATA
    offset gap, and waits for response opcode **30** -- not 29. A peripheral
    that ignores it, or that echoes 29, strands the client's recovery."""
    from plaudsim.filesync import parse_stop_sync_response

    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)
    assert peripheral.transfer is not None
    n = len(responses)

    await peer.write_value(command, b"\x01\x1d\x00", with_response=True)
    assert len(responses) == n + 1
    assert responses[-1] == b"\x01\x1e\x00"
    assert parse_stop_sync_response(responses[-1]) == {}
    assert peripheral.transfer is None, "stop must abandon the active transfer"


@pytest.mark.asyncio
async def test_full_gap_recovery_round_trip() -> None:
    """End-to-end rehearsal of the SDK's recovery sequence against the
    emulator: y6 with a gap -> detect -> z6 stop -> y6 from the cursor ->
    a clean, contiguous stream that reassembles the whole file."""
    devices, peripheral, peer, data, command = await linked(drop_data_offsets=(32,))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, y6(SESSION, 0, 0), with_response=True)
    cursor, body = 0, b""
    for frame in responses[1:-1]:
        parsed = parse_file_data_frame(frame, port_version=7)
        if parsed["offset"] != cursor:
            break
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    assert cursor == 32, "the gap must be detected at the cursor"

    await peer.write_value(command, b"\x01\x1d\x00", with_response=True)
    assert responses[-1] == b"\x01\x1e\x00"

    peripheral.drop_data_offsets = ()
    n = len(responses)
    await peer.write_value(command, y6(SESSION, cursor, 0), with_response=True)
    for frame in responses[n + 1 : -2]:  # [-2] is the EMPTY_PACKAGE sentinel (R7-S13)
        parsed = parse_file_data_frame(frame, port_version=7)
        assert parsed["offset"] == cursor
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    assert body == FILE_BYTES
    assert responses[-2] == pack_empty_package_frame(0, SESSION, 7)
    assert responses[-1] == pack_sync_tail(SESSION, CRC)


@pytest.mark.asyncio
async def test_delete_file_removes_the_entry_and_acks_with_opcode_31() -> None:
    from plaudsim.filesync import parse_delete_file_response, parse_file_list_frame

    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, b"\x01\x1e\x00" + pack("<I", SESSION), with_response=True)
    assert parse_delete_file_response(responses[-1], port_version=7) == {
        "session_id": SESSION,
        "status": 0,
    }
    await peer.write_value(command, p2(STAMP, 0, 0), with_response=True)
    remaining = parse_file_list_frame(responses[-1])
    assert remaining["totals"] == 1
    assert [e["session_id"] for e in remaining["entries"]] == [0x11111111]


def test_opcode_29_and_30_mean_different_things_in_each_direction() -> None:
    """Request and response opcode spaces are separate tables, and these two
    numbers are where that bites: 29 inbound is stopSync but outbound is the
    transfer TAIL; 30 inbound is deleteFile but outbound is the stop ack."""
    from plaudsim.filesync import (
        OPCODE_DELETE_FILE,
        OPCODE_DELETE_FILE_RSP,
        OPCODE_STOP_SYNC,
        OPCODE_STOP_SYNC_RSP,
        OPCODE_SYNC_TAIL,
        parse_sync_tail,
    )
    from plaudsim.transfer import pack_stop_sync_response

    assert OPCODE_STOP_SYNC == OPCODE_SYNC_TAIL == 29
    assert OPCODE_STOP_SYNC_RSP == OPCODE_DELETE_FILE == 30
    assert OPCODE_DELETE_FILE_RSP == 31
    # The stop ACK must not be mistakable for a tail, and vice versa.
    with pytest.raises(ValueError):
        parse_sync_tail(pack_stop_sync_response())
    assert pack_sync_tail(1, 2)[:3] != pack_stop_sync_response()[:3]
