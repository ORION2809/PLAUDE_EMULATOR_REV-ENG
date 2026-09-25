"""R3 codec tests: recording/file-sync structures, and the ways they can be wrong.

Pure codec/parser assertions. Nothing here drives the BLE peripheral; the
end-to-end behaviour lives in tests/test_r3_transfer.py and the bytecode-derived
offsets in tests/test_evidence_conformance.py.

Three corrections from the 2026-09-22 audit are pinned here so they cannot
regress: entries start at offset 11 (not 9), scene/attribute are at +8/+9 (not
+9/+8), and the bulk stream is protocol type 2 (not 4).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.filesync import (
    EMPTY_PACKAGE_OFFSET,
    FILE_DATA_TYPE,
    FILE_LIST_ENTRY_OFFSET,
    OPCODE_FILE_LIST,
    OPCODE_RATE_TEST,
    OPCODE_RESUME,
    OPCODE_SYNC_HEAD,
    OPCODE_SYNC_START,
    OPCODE_SYNC_TAIL,
    RATE_TEST_TYPE,
    file_entry_stride,
    pack_file_list_request,
    pack_resume_record_request,
    pack_sync_start,
    parse_file_data_frame,
    parse_file_entry,
    parse_file_list_frame,
    parse_rate_test_frame,
    parse_resume_record_response,
    parse_sync_head,
    parse_sync_tail,
)
from plaudsim.transfer import (
    pack_empty_package_frame,
    pack_file_data_frame,
    pack_file_list_frame,
)


def test_opcodes_and_frame_types() -> None:
    assert (OPCODE_SYNC_START, OPCODE_SYNC_HEAD, OPCODE_SYNC_TAIL) == (28, 28, 29)
    assert OPCODE_FILE_LIST == 26
    assert OPCODE_RESUME == 22
    assert OPCODE_RATE_TEST == 101
    assert FILE_DATA_TYPE == 2
    assert RATE_TEST_TYPE == 4


# --- requests ------------------------------------------------------------


def test_sync_start_request_layout() -> None:
    frame = pack_sync_start(0x12345678, 0, 0)
    assert frame == bytes([0x01, 0x1C, 0x00, 0x78, 0x56, 0x34, 0x12]) + bytes(8)
    assert len(frame) == 15
    with pytest.raises(ValueError):
        pack_sync_start(0x1_0000_0000, 0, 0)


def test_file_list_request_layout_and_naming() -> None:
    """p2's first field is the REQUEST STAMP (unix seconds), not a session id:
    q builds `new p2(s5.e(), startSessionId, flag)` and s5.e() is
    `System.currentTimeMillis()/1000` captured when the request was issued."""
    frame = pack_file_list_request(0x655A1B00, 0x12345678, True)
    assert frame[:3] == b"\x01\x1a\x00"
    assert frame[3:7] == (0x655A1B00).to_bytes(4, "little")
    assert frame[7:11] == (0x12345678).to_bytes(4, "little")
    assert frame[11] == 1
    assert len(frame) == 12
    assert pack_file_list_request(1, 2, False)[11] == 0


def test_resume_record_request_layout() -> None:
    assert pack_resume_record_request(0x12345678, 0) == bytes(
        [0x01, 0x16, 0x00, 0x78, 0x56, 0x34, 0x12, 0x00]
    )
    with pytest.raises(ValueError):
        pack_resume_record_request(0, 256)


# --- control responses ---------------------------------------------------


def test_sync_head_tail_and_resume_parse() -> None:
    head = bytes([0x01, 0x1C, 0x00, 0x78, 0x56, 0x34, 0x12, 0x00])
    assert parse_sync_head(head) == {"session_id": 0x12345678, "status": 0}
    tail = bytes([0x01, 0x1D, 0x00, 0x78, 0x56, 0x34, 0x12, 0xAB, 0xCD])
    assert parse_sync_tail(tail) == {"session_id": 0x12345678, "crc": 0xCDAB}
    with pytest.raises(ValueError):
        parse_sync_head(bytes(7))
    # A HEAD frame must not parse as a TAIL: opcode 28 != 29.
    with pytest.raises(ValueError):
        parse_sync_tail(head + b"\x00")


def test_resume_response_short_frame_uses_field_defaults() -> None:
    """c5 guards `length >= 17` before scene and startTime; a 12-byte frame
    leaves both at 0 and is indistinguishable from a device reporting zeros."""
    short = bytes([0x01, 0x16, 0x00]) + (7).to_bytes(4, "little") + (64).to_bytes(4, "little") + b"\x00"
    parsed = parse_resume_record_response(short)
    assert parsed == {"session_id": 7, "start": 64, "status": 0, "scene": 0, "start_time": 0}
    with pytest.raises(ValueError):
        parse_resume_record_response(short[:11])


# --- file list -----------------------------------------------------------


def test_entry_stride_is_selected_by_port_version() -> None:
    """q2.a switches on the format selector q passes, which is l3.portVersion."""
    assert file_entry_stride(1) == 8
    for pv in range(2, 7):
        assert file_entry_stride(pv) == 9
    for pv in (7, 9, 20, 255):
        assert file_entry_stride(pv) == 10


def test_file_entry_scene_and_attribute_are_at_plus_8_and_plus_9() -> None:
    entry = bytes([0x78, 0x56, 0x34, 0x12, 0x00, 0x10, 0x00, 0x00, 0x04, 0x03])
    assert parse_file_entry(entry, port_version=7) == {
        "session_id": 0x12345678,
        "file_size": 0x1000,
        "scene": 4,        # BleFile.isMusic() is scene == 4
        "attribute": 3,
    }
    # The pre-audit reading had these swapped; assert the difference explicitly.
    assert parse_file_entry(entry, 7)["scene"] != parse_file_entry(entry, 7)["attribute"]


def test_file_entry_narrow_strides_leave_the_missing_fields_zero() -> None:
    body = bytes([0x01, 0, 0, 0, 0x02, 0, 0, 0])
    assert parse_file_entry(body, port_version=1) == {
        "session_id": 1, "file_size": 2, "scene": 0, "attribute": 0
    }
    assert parse_file_entry(body + bytes([9]), port_version=3) == {
        "session_id": 1, "file_size": 2, "scene": 0, "attribute": 9
    }
    with pytest.raises(ValueError):
        parse_file_entry(body, port_version=7)   # 8 bytes cannot satisfy stride 10


def test_file_list_entries_start_at_offset_11() -> None:
    """The offset-9 reading dropped two bytes off every entry, which silently
    rotated sessionId into fileSize. Assert the byte positions directly."""
    assert FILE_LIST_ENTRY_OFFSET == 11
    entries = [{"session_id": 0xAABBCCDD, "file_size": 0x11223344, "scene": 1, "attribute": 2}]
    frame = pack_file_list_frame(0xDEADBEEF, 1, entries, frame_start_index=0)
    assert frame[11:15] == (0xAABBCCDD).to_bytes(4, "little")
    parsed = parse_file_list_frame(frame)
    assert parsed["request_stamp"] == 0xDEADBEEF
    assert parsed["totals"] == 1
    assert parsed["frame_start_index"] == 0
    assert parsed["entries"] == entries
    # Parsing the same frame as if entries began at 9 yields a different,
    # wrong session id -- proof the two readings are distinguishable.
    assert int.from_bytes(frame[9:13], "little") != 0xAABBCCDD


def test_file_list_header_only_frame_is_not_an_error() -> None:
    """q2.a's own guard is `if (bArr.length < 11) return;` -- header, no entries."""
    frame = bytes([0x01, 0x1A, 0x00]) + (1).to_bytes(4, "little") + (5).to_bytes(2, "little")
    parsed = parse_file_list_frame(frame)
    assert parsed["totals"] == 5
    assert parsed["frame_start_index"] is None
    assert parsed["entries"] == []


def test_file_list_trailing_partial_entry_is_discarded() -> None:
    """The entry count is integer division `(len - 11) / stride`, so a frame
    carrying one and a half entries yields exactly one."""
    entries = [{"session_id": 1, "file_size": 2, "scene": 0, "attribute": 0}]
    frame = pack_file_list_frame(1, 2, entries) + b"\x99\x99\x99"
    parsed = parse_file_list_frame(frame)
    assert len(parsed["entries"]) == 1


def test_file_list_rejects_a_resume_frame() -> None:
    """Dispatch-confusion guard for the opcode 22 vs 26 correction."""
    resume_like = bytes([0x01, 0x16, 0x00, 0, 0, 0, 0, 0x02, 0x00]) + bytes(12)
    with pytest.raises(ValueError):
        parse_file_list_frame(resume_like)


# --- bulk data -----------------------------------------------------------


def test_file_data_frame_modern_layout() -> None:
    frame = pack_file_data_frame(0x100, b"DATA", session_id=0x12345678, port_version=7)
    assert frame == bytes([0x02, 0x78, 0x56, 0x34, 0x12, 0x00, 0x01, 0x00, 0x00, 0x04]) + b"DATA"
    parsed = parse_file_data_frame(frame, port_version=7)
    assert parsed["session_id"] == 0x12345678
    assert parsed["offset"] == 0x100
    assert parsed["length"] == 4
    assert parsed["payload"] == b"DATA"
    assert parsed["empty_package"] is False


def test_file_data_frame_legacy_layout_has_no_session_id() -> None:
    frame = pack_file_data_frame(0x100, b"DATA", port_version=6)
    assert frame == bytes([0x02, 0x00, 0x01, 0x00, 0x00, 0x04]) + b"DATA"
    parsed = parse_file_data_frame(frame, port_version=6)
    assert parsed["session_id"] is None
    assert parsed["offset"] == 0x100
    assert parsed["payload"] == b"DATA"


def test_file_data_frame_decoded_with_the_wrong_branch_is_garbage() -> None:
    """Falsifiability: the two portVersion branches are NOT compatible, so an
    emulator that guesses wrong produces plausible-looking nonsense."""
    modern = pack_file_data_frame(0x100, b"DATA", session_id=0x12345678, port_version=7)
    as_legacy = parse_file_data_frame(modern, port_version=6)
    assert as_legacy["offset"] == 0x12345678       # the session id read as the offset
    assert as_legacy["payload"] != b"DATA"


def test_file_data_length_is_clamped_to_the_frame_end() -> None:
    """q$a.a: `if (a3 + i4 > bArr.length) i3 = bArr.length - i4;` -- an
    over-declared length truncates instead of throwing."""
    frame = bytes([0x02]) + (1).to_bytes(4, "little") + (0).to_bytes(4, "little") + bytes([0xFF]) + b"AB"
    parsed = parse_file_data_frame(frame, port_version=7)
    assert parsed["length"] == 0xFF
    assert parsed["payload"] == b"AB"


def test_empty_package_reads_its_code_at_i_plus_5_skipping_the_length_byte() -> None:
    """The asymmetry is in the bytecode: a normal frame's length lives at i+4
    and its payload at i+5, but an EMPTY_PACKAGE frame reads its code at i+5.
    The byte at i+4 is never read. Reproduced, not smoothed over."""
    frame = pack_empty_package_frame(3, session_id=7, port_version=7)
    assert frame[5:9] == (EMPTY_PACKAGE_OFFSET).to_bytes(4, "little")
    assert frame[9] == 0, "the byte the SDK skips"
    assert frame[10] == 3, "the code the SDK actually reads"
    parsed = parse_file_data_frame(frame, port_version=7)
    assert parsed["empty_package"] is True
    assert parsed["code"] == 3
    assert parsed["payload"] == b""
    legacy = pack_empty_package_frame(1, port_version=6)
    assert parse_file_data_frame(legacy, port_version=6) == {
        "frame_type": 2, "port_version_branch": "legacy", "session_id": None,
        "offset": EMPTY_PACKAGE_OFFSET, "length": None, "payload": b"",
        "empty_package": True, "code": 1,
    }


def test_file_data_frame_rejects_other_protocol_types() -> None:
    with pytest.raises(ValueError):
        parse_file_data_frame(bytes([0x01, 0x1C, 0x00, 0, 0, 0, 0, 0]), port_version=7)
    with pytest.raises(ValueError):
        parse_file_data_frame(bytes([0x04, 0x0A, 0, 0, 0]), port_version=7)
    with pytest.raises(ValueError):
        parse_file_data_frame(bytes([0x02, 0x01]), port_version=7)   # no room for a session id


def test_rate_test_frame_keeps_its_overlapping_reads_unnamed() -> None:
    """Type 4 is the BLE throughput probe (opcode 101), not file sync. Its
    parser genuinely reads a u32 and a u8 at the SAME offset 1 and then copies
    from offset 2, so only the u8 reading is self-consistent. Both values are
    surfaced and neither is given a protocol meaning."""
    frame = bytes([0x04, 0x0A, 0x00, 0x00, 0x00]) + bytes(range(10))
    parsed = parse_rate_test_frame(frame)
    assert parsed["u8_at_1"] == 0x0A
    assert parsed["u32_at_1"] == 0x0A
    assert parsed["payload"] == frame[2:12]
    assert "offset" not in parsed and "length" not in parsed
    # A vector where the two readings diverge.
    diverging = bytes([0x04, 0x2C, 0x01, 0x00, 0x00, 0xAA])
    p2_ = parse_rate_test_frame(diverging)
    assert p2_["u8_at_1"] == 0x2C and p2_["u32_at_1"] == 0x12C
    assert p2_["payload"] == bytes([0x01, 0x00, 0x00, 0xAA])
    with pytest.raises(ValueError):
        parse_rate_test_frame(bytes([0x02, 0x00, 0x00]))
