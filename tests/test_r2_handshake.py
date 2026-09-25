"""R2 handshake reconstruction tests (structure only, synthetic vectors).

No account token, RSA key or device SN exists in this repository, so nothing
here exercises real authentication. What IS verified: framing and parse logic
recovered from the shipped AAR, plus the failure modes that would falsify our
reading of it.

Cross-check: `tests/test_evidence_conformance.py` asserts the same layouts
against offsets extracted mechanically from bytecode, so a shared
misunderstanding between this file and the implementation cannot hide.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.handshake import (
    CHUNK_SIZE,
    MARKER_FORCE_CLEAR,
    MARKER_PRE_HANDSHAKE,
    MARKER_RSA_MARKER,
    MARKER_SECRET,
    SECRET_MAGIC,
    ReplayWindow,
    chunk_bytes,
    pack_marker_frame,
    parse_l3,
    parse_marker_frame,
    parse_x2,
    reassemble_secret_chunks,
    select_handshake_response,
    split_secret_package,
    unwrap_envelope_plaintext,
    wrap_envelope_plaintext,
)


# --- marker framing ------------------------------------------------------


def test_marker_frame_has_no_protocol_type_byte() -> None:
    """The correction that invalidated the previous R2 framing.

    v4/w4 override enPkg() and never call packHead(), and the receive side
    reads the marker with a u16 at offset ZERO. A leading 0x01 would shift
    every field by one and the real SDK would reject the frame.
    """
    frame = pack_marker_frame(MARKER_PRE_HANDSHAKE, 3, 1, b"XY")
    assert frame == bytes([0x10, 0xFE, 3, 1]) + b"XY"
    assert len(frame) == 4 + 2
    # An emulator that prepended 0x01 would produce this instead:
    assert frame != bytes([0x01, 0x10, 0xFE, 3, 1]) + b"XY"


def test_markers_are_little_endian_on_the_wire() -> None:
    for marker, low in (
        (MARKER_PRE_HANDSHAKE, 0x10),
        (MARKER_RSA_MARKER, 0x11),
        (MARKER_SECRET, 0x12),
        (MARKER_FORCE_CLEAR, 0x20),
    ):
        frame = pack_marker_frame(marker, 1, 0, b"")
        assert frame[0] == low and frame[1] == 0xFE, f"{marker:#x} must hit the wire low byte first"


def test_count_precedes_index() -> None:
    """enPkg emits c(this.b) then c(this.a), and the drivers construct
    v4(index, count, ...) / w4(index, count, ...), so COUNT is byte 2."""
    frame = pack_marker_frame(MARKER_SECRET, 5, 2, b"")
    assert frame[2] == 5, "byte 2 is the chunk COUNT"
    assert frame[3] == 2, "byte 3 is the chunk INDEX"
    parsed = parse_marker_frame(frame)
    assert parsed["count"] == 5 and parsed["index"] == 2


def test_marker_frame_round_trip_and_rejection() -> None:
    chunk = bytes(range(100))
    parsed = parse_marker_frame(pack_marker_frame(MARKER_FORCE_CLEAR, 3, 1, chunk))
    assert parsed == {"marker": MARKER_FORCE_CLEAR, "count": 3, "index": 1, "chunk": chunk}
    # The SDK's own guard is `value2.length < 4`.
    with pytest.raises(ValueError):
        parse_marker_frame(bytes([0x12, 0xFE, 0x01]))
    with pytest.raises(ValueError):
        pack_marker_frame(MARKER_PRE_HANDSHAKE, 256, 0, b"")
    with pytest.raises(ValueError):
        pack_marker_frame(0x1FFFF, 1, 0, b"")


def test_chunk_rule_matches_the_drivers() -> None:
    """q.n / q.a0: `int length = (bytes.length + 99) / 100;`"""
    assert CHUNK_SIZE == 100
    assert chunk_bytes(b"") == []            # degenerate: nothing is transmitted at all
    assert len(chunk_bytes(bytes(100))) == 1
    assert len(chunk_bytes(bytes(101))) == 2
    assert len(chunk_bytes(bytes(256))) == 3
    assert b"".join(chunk_bytes(bytes(range(256)) * 2)) == bytes(range(256)) * 2


def test_secret_reassembly_sorts_by_index_and_drops_duplicates() -> None:
    """z$c sorts held frames by `bArr[3] & 255` and skips byte-identical dupes."""
    payloads = [b"AAA", b"BBB", b"CCC"]
    frames = [pack_marker_frame(MARKER_SECRET, 3, i, p) for i, p in enumerate(payloads)]
    assert reassemble_secret_chunks(frames) == b"AAABBBCCC"
    # Out of order, with an exact duplicate thrown in.
    assert reassemble_secret_chunks([frames[2], frames[0], frames[2], frames[1]]) == b"AAABBBCCC"


def test_secret_reassembly_refuses_incomplete_and_wrong_count() -> None:
    frames = [pack_marker_frame(MARKER_SECRET, 3, i, bytes([i])) for i in range(3)]
    with pytest.raises(ValueError):
        reassemble_secret_chunks(frames[:2])          # missing the final chunk
    # A frame claiming a different count wins (z overwrites I on every frame),
    # so a wrong count byte wedges reassembly rather than silently completing.
    bogus = pack_marker_frame(MARKER_SECRET, 9, 2, b"\x02")
    with pytest.raises(ValueError):
        reassemble_secret_chunks([frames[0], frames[1], bogus])


def test_duplicate_index_with_different_payload_is_not_deduplicated() -> None:
    """z compares whole frames with Arrays.equals, not just the index byte, so
    two frames sharing an index but differing in payload are BOTH kept -- and
    the sort is then unstable with respect to them. Reproduced, not fixed."""
    a = pack_marker_frame(MARKER_SECRET, 2, 0, b"AAA")
    b = pack_marker_frame(MARKER_SECRET, 2, 0, b"BBB")
    out = reassemble_secret_chunks([a, b])
    assert out in (b"AAABBB", b"BBBAAA")
    assert len(out) == 6


# --- dispatch ------------------------------------------------------------


def test_select_mirrors_the_two_receive_paths() -> None:
    """Marker frames are recognised by u16le@0; control frames by byte0==1
    with the opcode at offset 1 (q.a's `if (a(bArr2,0)==1) { b(bArr2,1) ... }`)."""
    assert select_handshake_response(bytes([1, 1, 0]) + bytes(10)) == "l3"
    assert select_handshake_response(bytes([1, 2, 0]) + bytes(10)) == "x2"
    for marker in (MARKER_PRE_HANDSHAKE, MARKER_RSA_MARKER, MARKER_SECRET, MARKER_FORCE_CLEAR):
        assert select_handshake_response(pack_marker_frame(marker, 1, 0, b"AB")) == "marker"
    # Opcode 9 is a real opcode but not a handshake one: q.a routes it to
    # HANDSHAKE_FAIL, so classification must refuse rather than guess.
    with pytest.raises(ValueError):
        select_handshake_response(bytes([1, 9, 0]))
    with pytest.raises(ValueError):
        select_handshake_response(bytes([2, 1, 0]) + bytes(10))


# --- l3 ------------------------------------------------------------------


def test_l3_full_frame() -> None:
    frame = bytes([0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08, 0x00, 0x02, 0x01, 0x00, 0x01]) + bytes(
        [ord("A"), 0x34, 0x12, 0x00]
    )
    parsed = parse_l3(frame)
    assert parsed["status"] == 0
    assert parsed["port_version"] == 9
    assert parsed["timezone"] == 8
    assert parsed["timezone_min"] == 0
    assert parsed["audio_channel"] == 2
    assert parsed["support_wifi"] is True
    assert parsed["no_ns_agc"] is False
    assert parsed["is_ogg_audio"] is True
    assert parsed["version_type"] == "A"
    assert parsed["version"] == 0x1234


def test_l3_version_endianness_is_falsifiable() -> None:
    """Little-endian u24 over the LAST THREE bytes. The old implementation read
    it big-endian; this asserts a vector where the two differ."""
    frame = bytes([0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08, 0x00, 0x02, 0x01, 0x00, 0x01]) + bytes(
        [ord("V"), 0x01, 0x02, 0x03]
    )
    assert parse_l3(frame)["version"] == 0x030201
    assert parse_l3(frame)["version"] != 0x010203


def test_l3_truncation_yields_sdk_defaults_not_none() -> None:
    """l3.<init> assigns Java defaults before parsing and returns early, so a
    short frame leaves audioChannel at 1 and versionType at "V" -- which is
    what the real client observes, and differs from `None`."""
    base = [0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08]
    parsed = parse_l3(bytes(base))
    assert (parsed["status"], parsed["port_version"], parsed["timezone"]) == (0, 9, 8)
    assert parsed["timezone_min"] == 0
    assert parsed["audio_channel"] == 1
    assert parsed["support_wifi"] is False
    assert parsed["version_type"] == "V"
    assert parsed["version"] == 0

    parsed = parse_l3(bytes(base + [0x05]))
    assert parsed["timezone_min"] == 5
    assert parsed["audio_channel"] == 1     # still the default: needs len >= 9

    parsed = parse_l3(bytes(base + [0x05, 0x03, 0x01, 0x00, 0x01]))
    assert parsed["audio_channel"] == 3
    assert parsed["is_ogg_audio"] is True
    assert parsed["version_type"] == chr(0x03)
    assert parsed["version"] == 0x010001


def test_l3_boolean_fields_are_equality_with_one_not_truthiness() -> None:
    """`a(bArr,9) == 1`, so a byte of 2 reads as False."""
    base = [0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08, 0x00, 0x02]
    assert parse_l3(bytes(base + [0x02]))["support_wifi"] is False
    assert parse_l3(bytes(base + [0x01]))["support_wifi"] is True


def test_l3_rejects_wrong_opcode_and_short_prefix() -> None:
    with pytest.raises(ValueError):
        parse_l3(bytes([0x01, 0x02, 0x00]) + bytes(10))   # that is an x2 frame
    with pytest.raises(ValueError):
        parse_l3(bytes([0x01, 0x01, 0x00, 0x00, 0x09, 0x00]))   # b/c/d need len >= 7
    with pytest.raises(ValueError):
        parse_l3(bytes([0x02, 0x01, 0x00]) + bytes(10))   # wrong protocol type


# --- x2 ------------------------------------------------------------------


def x2_frame(ssn: bytes, version_type: bytes = b"V", version_code: int = 123) -> bytes:
    frame = bytearray(63)
    frame[0:3] = bytes([0x01, 0x02, 0x00])
    frame[3 : 3 + len(ssn)] = ssn
    frame[59] = version_type[0]
    frame[60:63] = version_code.to_bytes(3, "little")
    return bytes(frame)


def test_x2_version_name_is_offset_59_plus_u24_at_60() -> None:
    parsed = parse_x2(x2_frame(b"PLD0000000000001", b"V", 42))
    assert parsed["ssn"] == "PLD0000000000001"
    assert parsed["version_name"] == "V0042"
    # The old reading -- "everything after the NUL" -- would produce a long run
    # of NULs here, not a 5-character version name.
    assert len(parsed["version_name"]) == 5


def test_x2_version_name_matches_bledevice_version_name_construction() -> None:
    """BleDevice builds versionName as versionType + "%04d" % versionCode from
    the ADVERTISING data. x2 must produce the identical string from the wire,
    which is what makes the SDK's serial-number check comparable."""
    for code, expected in ((0, "V0000"), (7, "V0007"), (1234, "V1234"), (56789, "V56789")):
        assert parse_x2(x2_frame(b"SN1", b"V", code))["version_name"] == expected


def test_x2_ssn_scan_is_bounded_at_59() -> None:
    """A frame with no NUL before offset 59 falls back to 28 ASCII zeros."""
    frame = bytearray(x2_frame(b""))
    frame[3:59] = b"A" * 56
    parsed = parse_x2(bytes(frame))
    assert parsed["ssn"] == "0" * 28


def test_x2_empty_ssn_at_offset_3_is_the_zero_default() -> None:
    frame = bytearray(x2_frame(b""))
    frame[3] = 0
    assert parse_x2(bytes(frame))["ssn"] == "0" * 28


def test_x2_without_version_block_has_no_version_name() -> None:
    frame = bytearray(60)
    frame[0:3] = bytes([0x01, 0x02, 0x00])
    frame[3:7] = b"SN01"
    frame[7] = 0
    assert parse_x2(bytes(frame[:59]))["version_name"] is None
    # len == 60 satisfies `length > 59` but leaves no room for the u24 at 60.
    with pytest.raises(ValueError):
        parse_x2(bytes(frame))


def test_x2_rejects_wrong_opcode() -> None:
    with pytest.raises(ValueError):
        parse_x2(bytes([0x01, 0x04, 0x00]) + bytes(60))


# --- secret package + envelope -------------------------------------------


def test_secret_package_slices_and_minimum() -> None:
    plain = bytes(range(56)) + b"sealed"
    parts = split_secret_package(plain)
    assert len(parts["chacha_key"]) == 32
    assert len(parts["chacha_nonce"]) == 12
    assert len(parts["chacha_aad"]) == 12
    assert parts["sealed_magic"] == b"sealed"
    with pytest.raises(ValueError):
        split_secret_package(bytes(55))
    assert SECRET_MAGIC == b"PLAUD.AI"


def test_envelope_sequence_is_u32le_at_offset_zero() -> None:
    plain = wrap_envelope_plaintext(0x01020304, b"\x01\x03\x00")
    assert plain[:4] == bytes([0x04, 0x03, 0x02, 0x01])
    back = unwrap_envelope_plaintext(plain)
    assert back == {"seq": 0x01020304, "inner": b"\x01\x03\x00"}
    with pytest.raises(ValueError):
        unwrap_envelope_plaintext(b"\x00\x00\x00")


def test_replay_window_drops_non_increasing_sequences() -> None:
    """z$c: `if (z.N >= d) return; z.N = d;` with N starting at -1."""
    w = ReplayWindow()
    assert w.accept(0) is True
    assert w.accept(1) is True
    assert w.accept(1) is False          # replay
    assert w.accept(0) is False          # rollback
    assert w.accept(100) is True
    assert w.accept(99) is False


def test_replay_window_never_resets_across_a_reconnect() -> None:
    """z.N is a static field with no reset on disconnect, so a device that
    restarts its own counter is silently ignored forever. That is a real
    interoperability hazard, asserted so it cannot be 'fixed' by accident."""
    w = ReplayWindow()
    for seq in range(5):
        assert w.accept(seq) is True
    fresh_device_after_reconnect = 0
    assert w.accept(fresh_device_after_reconnect) is False
