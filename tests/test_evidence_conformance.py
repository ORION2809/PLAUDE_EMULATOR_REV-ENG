"""Assert the emulator against the SDK's BYTECODE, not against our own fixtures.

Every other test in this repo is, to some degree, circular: the emulator encodes
a frame and the test decodes it with expectations that were written from the
same reading of the protocol. If that reading is wrong, the test still passes.
That is exactly how an earlier reconstruction shipped a big-endian `version`
field, a fabricated 0x01 prefix on marker frames, and a file-list entry offset
of 9 instead of 11 -- all of them green.

This module breaks the loop. `docs/evidence-digest.json` is produced
MECHANICALLY by `scripts/extract_evidence_digest.py`, which walks the `javap`
disassembly of the shipped `plaud-sdk.aar` and records, per class:

  * the constant returned by getBleProtocolType / getBleRequestType / a()
  * every TntBleCommUtils reader call in the parse method, as (width, offset)
  * the `bArr.length` guard chain
  * the toString() format literal, which carries the real field names

The assertions below are written against that digest. A layout error in the
emulator fails here even when every fixture agrees with it.

The digest is pinned to the AAR's sha256. When the decompiled evidence tree is
present the digest is REGENERATED and compared, so a stale digest also fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from struct import unpack_from

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))

from plaudsim import filesync, handshake, profile, transfer  # noqa: E402

DIGEST_PATH = ROOT / "docs/evidence-digest.json"
EVIDENCE_JAVAP = ROOT / "build/evidence/javap/com/plaud/sdk/proto"
AAR = ROOT / "reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar"

DIGEST = json.loads(DIGEST_PATH.read_text())
CLASSES = DIGEST["classes"]

WIDTH_FMT = {8: "<B", 16: "<H", 32: "<I", 64: "<Q"}


def readers(cls: str, method: str | None = None) -> list[dict]:
    entry = CLASSES[cls]
    if method is None:
        return entry.get("init_readers", [])
    for sig, body in entry.get("other_methods", {}).items():
        if method in sig:
            return body["readers"]
    raise AssertionError(f"{cls} has no method matching {method!r}")


def guards(cls: str) -> list[int]:
    return [g["value"] for g in CLASSES[cls].get("init_guards", []) if g["value"] is not None]


def field_at(frame: bytes, width_bits: int, offset: int) -> int:
    return unpack_from(WIDTH_FMT[width_bits], frame, offset)[0]


# --------------------------------------------------------------------------
# The digest itself must be current.
# --------------------------------------------------------------------------


def test_digest_is_pinned_to_the_shipped_aar() -> None:
    """A digest whose AAR hash does not match the checked-out SDK proves nothing."""
    if not AAR.exists():
        pytest.skip("SDK not fetched; run ./scripts/fetch-references.sh")
    import hashlib

    assert DIGEST["aar_sha256"] == hashlib.sha256(AAR.read_bytes()).hexdigest(), (
        "docs/evidence-digest.json was extracted from a different plaud-sdk.aar. "
        "Re-run ./scripts/build-evidence.sh && python3 scripts/extract_evidence_digest.py "
        "and re-audit every protocol claim."
    )


def test_digest_regenerates_identically() -> None:
    """Catches a hand-edited or stale digest whenever the evidence tree is present."""
    if not EVIDENCE_JAVAP.is_dir():
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    before = DIGEST_PATH.read_text()
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/extract_evidence_digest.py")],
        check=True,
        capture_output=True,
    )
    after = DIGEST_PATH.read_text()
    assert before == after, (
        "docs/evidence-digest.json is stale or was hand-edited: re-extracting it from "
        "the bytecode produced a different file."
    )


# --------------------------------------------------------------------------
# GATT + opcodes
# --------------------------------------------------------------------------


def test_gatt_uuids_match_the_sdk_constants() -> None:
    g = DIGEST["gatt"]
    assert profile.PLAUD_SERVICE_UUID.lower() == g["service"].lower()
    assert profile.DATA_NOTIFY_UUID.lower() == g["notify"].lower()
    assert profile.COMMAND_WRITE_UUID.lower() == g["write"].lower()
    # A swap would be silent on the wire but fatal against the real SDK.
    assert profile.DATA_NOTIFY_UUID.lower() != profile.COMMAND_WRITE_UUID.lower()


def test_opcode_constants_match_the_sdk_classes() -> None:
    assert profile.OPCODE_GET_STATE == CLASSES["z2"]["response_opcode"] == 3
    assert profile.OPCODE_GET_STORAGE == CLASSES["n6"]["response_opcode"] == 6
    assert profile.OPCODE_GET_STORAGE == CLASSES["a3"]["request_opcode"]
    assert profile.OPCODE_SYNC_TIME == CLASSES["n7"]["response_opcode"] == 4
    assert profile.OPCODE_SYNC_TIME == CLASSES["m7"]["request_opcode"]
    assert filesync.OPCODE_SYNC_START == CLASSES["y6"]["request_opcode"] == 28
    assert filesync.OPCODE_SYNC_HEAD == CLASSES["s6"]["response_opcode"] == 28
    assert filesync.OPCODE_SYNC_TAIL == CLASSES["t6"]["response_opcode"] == 29
    assert filesync.OPCODE_FILE_LIST == CLASSES["p2"]["request_opcode"] == 26
    assert filesync.OPCODE_FILE_LIST == CLASSES["q2"]["response_opcode"]
    assert filesync.OPCODE_RESUME == CLASSES["b5"]["request_opcode"] == 22
    assert filesync.OPCODE_RESUME == CLASSES["c5"]["response_opcode"]
    assert handshake.HANDSHAKE_OPCODE == CLASSES["l3"]["response_opcode"] == 1
    assert handshake.HANDSHAKE_OPCODE == CLASSES["k3"]["request_opcode"]
    assert handshake.SSN_OPCODE == CLASSES["x2"]["response_opcode"] == 2


def test_opcode_22_is_resume_not_file_list() -> None:
    """The correction that started the R3 re-audit, asserted from the artifact."""
    assert CLASSES["c5"]["tostring"].startswith("RecordResumeRsp")
    assert CLASSES["q2"]["tostring"].startswith("GetRecSessionsRsp")
    assert CLASSES["c5"]["response_opcode"] == 22
    assert CLASSES["q2"]["response_opcode"] == 26
    assert filesync.OPCODE_RESUME != filesync.OPCODE_FILE_LIST


# --------------------------------------------------------------------------
# Response encoders vs. the SDK's own parse offsets
# --------------------------------------------------------------------------


def test_get_state_response_lands_on_z2_parse_offsets() -> None:
    state = profile.PlaudDeviceState(
        state=0x1001,
        privacy_enabled=True,
        key_state=1,
        usb_state=False,
        scene=4,
        session_id=0x12345678,
        find_my_state=1,
        unnamed_16=7,
        unnamed_17=8,
    )
    frame = state.encode()
    # z2.<init> reads, in order: u32@3 u8@7 u8@8 u8@9 u8@10 u32@11 u8@15 u8@16 u8@17
    expected = [(32, 3), (8, 7), (8, 8), (8, 9), (8, 10), (32, 11), (8, 15), (8, 16), (8, 17)]
    assert [(r["width_bits"], r["offset"]) for r in readers("z2")] == expected
    values = [
        state.state,
        int(state.privacy_enabled),
        state.key_state,
        int(state.usb_state),
        state.scene,
        state.session_id,
        state.find_my_state,
        state.unnamed_16,
        state.unnamed_17,
    ]
    for (width, offset), value in zip(expected, values):
        assert field_at(frame, width, offset) == value, f"field at @{offset} w{width}"
    assert len(frame) == 18 == max(guards("z2"))


def test_get_storage_response_lands_on_n6_parse_offsets() -> None:
    storage = profile.PlaudStorageState(free=8589934592, total=17179869184, duration=36000)
    frame = storage.encode()
    assert [(r["width_bits"], r["offset"]) for r in readers("n6")] == [(64, 3), (64, 11), (64, 19)]
    assert field_at(frame, 64, 3) == storage.free
    assert field_at(frame, 64, 11) == storage.total
    assert field_at(frame, 64, 19) == storage.duration
    # n6 guards `length >= 27` before reading duration; the full frame is 27 bytes.
    assert len(frame) == 27 == max(guards("n6"))


def test_storage_field_names_come_from_the_sdk_tostring() -> None:
    """free is at 3 and total at 11, in that order -- the shuffled n6 getters
    make this the single easiest field pair to invert."""
    assert CLASSES["n6"]["tostring"].startswith("StorageRsp{free=")
    lit = CLASSES["n6"]["tostring"]
    assert lit.index("free") < len(lit)
    offsets = [r["offset"] for r in readers("n6")]
    assert offsets == [3, 11, 19]


def test_sync_time_response_lands_on_n7_parse_offsets() -> None:
    st = profile.PlaudSyncTimeState(stamp=1700000000, timezone=8, has_statistics=True)
    frame = st.encode_response()
    assert [(r["width_bits"], r["offset"]) for r in readers("n7")] == [(32, 3), (8, 7), (8, 8)]
    assert field_at(frame, 32, 3) == st.stamp
    assert field_at(frame, 8, 7) == st.timezone
    assert field_at(frame, 8, 8) == int(st.has_statistics)
    assert len(frame) == 9 == max(guards("n7"))


def test_sync_time_field_at_offset_7_is_named_timezone() -> None:
    """An earlier reconstruction called this field `raw7` and recorded it as
    unnamed. The SDK's own toString names it."""
    assert CLASSES["n7"]["tostring"] == "TimeSyncRsp{stamp="
    javap = EVIDENCE_JAVAP / "n7.txt"
    if not javap.is_file():
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    text = javap.read_text()
    assert "String , timezone=" in text, "n7 must name its offset-7 field `timezone`"
    assert hasattr(profile.PlaudSyncTimeState(), "timezone")
    assert not hasattr(profile.PlaudSyncTimeState(), "raw7")


def test_sync_head_tail_and_resume_land_on_parse_offsets() -> None:
    head = transfer.pack_sync_head(0x12345678, 0)
    assert [(r["width_bits"], r["offset"]) for r in readers("s6")] == [(32, 3), (8, 7)]
    assert field_at(head, 32, 3) == 0x12345678
    assert len(head) == 8

    tail = transfer.pack_sync_tail(0x12345678, 0xBEEF)
    assert [(r["width_bits"], r["offset"]) for r in readers("t6")] == [(32, 3), (16, 7)]
    assert field_at(tail, 32, 3) == 0x12345678
    assert field_at(tail, 16, 7) == 0xBEEF
    assert len(tail) == 9

    resume = transfer.pack_resume_record_response(0x12345678, 64, 0, 5, 1700000000)
    assert [(r["width_bits"], r["offset"]) for r in readers("c5")] == [
        (32, 3), (32, 7), (8, 11), (8, 12), (32, 13)
    ]
    for (width, offset), value in zip(
        [(32, 3), (32, 7), (8, 11), (8, 12), (32, 13)],
        [0x12345678, 64, 0, 5, 1700000000],
    ):
        assert field_at(resume, width, offset) == value
    assert len(resume) == 17 == max(guards("c5"))


def test_file_list_frame_lands_on_q2_parse_offsets() -> None:
    """The offset-11 correction, asserted mechanically.

    q2.<init> reads totals at 7. q2.a([B,I) guards `length >= 11`, reads the
    resumption index at 9, and starts the entry loop at a cursor initialised to
    11 -- so a builder that starts entries at 9 is wrong by two bytes.
    """
    assert [(r["width_bits"], r["offset"]) for r in readers("q2")] == [(16, 7)]
    entry_readers = readers("q2", "a(byte[], int)")
    assert entry_readers[0]["offset"] == 9 and entry_readers[0]["width_bits"] == 16
    assert CLASSES["q2"]["other_methods"]["public void a(byte[], int)"]["guards"][0]["value"] == 11
    assert filesync.FILE_LIST_ENTRY_OFFSET == 11

    entries = [{"session_id": 0x12345678, "file_size": 0x1000, "scene": 4, "attribute": 3}]
    frame = transfer.pack_file_list_frame(0xAABBCCDD, 1, entries, frame_start_index=0)
    assert field_at(frame, 32, 3) == 0xAABBCCDD
    assert field_at(frame, 16, 7) == 1
    assert field_at(frame, 16, 9) == 0
    assert field_at(frame, 32, 11) == 0x12345678
    assert field_at(frame, 32, 15) == 0x1000
    assert len(frame) == 11 + 10


def test_file_entry_scene_and_attribute_are_not_swapped() -> None:
    """q2.a's 10-byte loop calls `new BleFile(d5, d6, a3, a2)` where a2 is the
    byte at +8 and a3 the byte at +9, and BleFile's 4-arg ctor is
    (sessionId, fileSize, attribute, scene). So +8 is SCENE, +9 is ATTRIBUTE.
    """
    entry_readers = readers("q2", "a(byte[], int)")
    # After the index read at 9, the cursor-relative reads are the literals the
    # bytecode adds to i2: 11 (i2 itself), 4, 8, 9.
    addends = [r["offset"] for r in entry_readers[1:]]
    assert 8 in addends and 9 in addends

    frame = transfer.pack_file_list_frame(
        1, 1, [{"session_id": 1, "file_size": 2, "scene": 0xAA, "attribute": 0xBB}]
    )
    assert frame[11 + 8] == 0xAA, "byte at +8 must carry SCENE"
    assert frame[11 + 9] == 0xBB, "byte at +9 must carry ATTRIBUTE"
    back = filesync.parse_file_entry(frame[11:21], port_version=7)
    assert back == {"session_id": 1, "file_size": 2, "scene": 0xAA, "attribute": 0xBB}


# --------------------------------------------------------------------------
# Request encoders vs. the SDK's own enPkg construction
# --------------------------------------------------------------------------


def alloc_widths(cls: str) -> list[int]:
    return [c["width_bits"] for c in CLASSES[cls].get("enpkg_calls", []) if "allocator" in c]


def test_request_payload_widths_match_enpkg() -> None:
    # l.packHead() for protocolType 1 emits [u8 type][u16le opcode] = 3 bytes,
    # so a request's length is 3 + sum(payload widths)/8.
    cases = [
        ("m7", profile.PlaudSyncTimeState(stamp=1, tz_hours=2, tz_mins=3).encode_request()),
        ("y6", filesync.pack_sync_start(1, 2, 3)),
        ("p2", filesync.pack_file_list_request(1, 2, True)),
        ("b5", filesync.pack_resume_record_request(1, 2)),
    ]
    for cls, frame in cases:
        widths = alloc_widths(cls)
        assert widths, f"{cls} has no enPkg allocator calls in the digest"
        assert len(frame) == 3 + sum(widths) // 8, f"{cls} payload width"
        assert frame[0] == CLASSES[cls]["protocol_type"] == 1
        assert unpack_from("<H", frame, 1)[0] == CLASSES[cls]["request_opcode"]


def test_marker_frames_have_no_protocol_type_prefix() -> None:
    """v4/w4 override enPkg() and never call packHead().

    Their enPkg emits exactly three allocator calls -- u16 marker, u8, u8 --
    followed by the raw chunk. A leading 0x01 byte, which an earlier
    reconstruction emitted, is not in the bytecode.
    """
    for cls in ("v4", "w4"):
        assert alloc_widths(cls) == [16, 8, 8], f"{cls} enPkg allocator widths"

    chunk = bytes(range(10))
    frame = handshake.pack_marker_frame(handshake.MARKER_PRE_HANDSHAKE, 3, 1, chunk)
    assert len(frame) == 4 + len(chunk)
    assert frame[0] != 1 or handshake.MARKER_PRE_HANDSHAKE & 0xFF == 1
    assert unpack_from("<H", frame, 0)[0] == handshake.MARKER_PRE_HANDSHAKE
    assert frame[0:2] == bytes([0x10, 0xFE]), "marker is little-endian at offset ZERO"
    assert frame[2] == 3 and frame[3] == 1
    assert frame[4:] == chunk


def test_marker_constants_match_the_sdk_tables() -> None:
    """w$c.a/b/c and w$a.a, and the two request classes' own opcode constants."""
    assert CLASSES["w4"]["request_opcode"] == handshake.MARKER_SECRET == 0xFE12
    # v4.getBleRequestType returns w.c.b (0xFE20) when forceClear else w.c.a (0xFE10);
    # the digest records the last ireturn, which is the false branch.
    assert CLASSES["v4"]["request_opcode"] in (
        handshake.MARKER_FORCE_CLEAR,
        handshake.MARKER_PRE_HANDSHAKE,
    )
    javap = ROOT / "build/evidence/javap/com/plaud/sdk/proto/v4.txt"
    if javap.is_file():
        text = javap.read_text()
        assert "int 65056" in text and "int 65040" in text


# --------------------------------------------------------------------------
# Parsers vs. the SDK's guard chains
# --------------------------------------------------------------------------


def test_l3_guard_chain_matches_the_sdk() -> None:
    """l3's nested `length >= N` chain is 8,9,10,11,12 (plus a vestigial >= 4)."""
    assert sorted(set(guards("l3"))) == [4, 8, 9, 10, 11, 12]
    base = bytes([0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08])
    parsed = handshake.parse_l3(base)
    # Unreached fields must carry the Java defaults the SDK assigns up front,
    # notably audioChannel = 1 (not 0) and versionType = "V".
    assert parsed["audio_channel"] == 1
    assert parsed["version_type"] == "V"
    assert parsed["timezone_min"] == 0


def test_l3_version_is_little_endian_u24() -> None:
    """l3.<init>: k = b[len-3] | (b[len-2]<<8) | (b[len-1]<<16).

    An earlier implementation shifted the other way; every fixture agreed with
    it because the fixture was written from the same mistake.
    """
    frame = bytes([0x01, 0x01, 0x00, 0x00, 0x09, 0x00, 0x08, 0x00, 0x02, 0x01, 0x00, 0x01]) + bytes(
        [ord("A"), 0x34, 0x12, 0x00]
    )
    parsed = handshake.parse_l3(frame)
    assert parsed["version_type"] == "A"
    assert parsed["version"] == 0x001234, "little-endian u24: 34 12 00 -> 0x1234"
    assert parsed["version"] != 0x341200, "big-endian reading is the old bug"
    # versionName is rendered "%04d", so version is a 4-digit decimal in practice.
    assert 0 <= parsed["version"] <= 0xFFFFFF


def test_x2_version_name_is_built_from_fixed_offsets_59_and_60() -> None:
    """x2 reads a u24 at literal offset 60 -- the digest proves the offset --
    and the char at 59, then formats "%04d". It is NOT the tail after the NUL.
    """
    assert [(r["width_bits"], r["offset"]) for r in readers("x2")] == [(24, 60)]
    assert handshake.VERSION_NAME_TYPE_OFFSET == 59
    assert handshake.VERSION_NAME_CODE_OFFSET == 60
    frame = bytearray(63)
    frame[0:3] = bytes([0x01, 0x02, 0x00])
    ssn = b"PLD0123456789012"
    frame[3 : 3 + len(ssn)] = ssn
    frame[3 + len(ssn)] = 0
    frame[59] = ord("V")
    frame[60:63] = (123).to_bytes(3, "little")
    parsed = handshake.parse_x2(bytes(frame))
    assert parsed["ssn"] == "PLD0123456789012"
    assert parsed["version_name"] == "V0123"


def test_file_data_frame_is_protocol_type_2_not_4() -> None:
    """The bulk recording stream is protocol type 2 (q$a.a's `if (a != 2) return`).

    Type 4 belongs to the BLE rate test on opcode 101, whose parser really does
    read a u32 and a u8 at the same offset. Attributing that quirk to file sync
    was a misreading.
    """
    assert filesync.FILE_DATA_TYPE == 2
    assert filesync.RATE_TEST_TYPE == 4
    assert filesync.OPCODE_RATE_TEST == 101
    frame = transfer.pack_file_data_frame(0x40, b"abcd", session_id=0x11223344, port_version=7)
    assert frame[0] == 2
    assert field_at(frame, 32, 1) == 0x11223344
    assert field_at(frame, 32, 5) == 0x40
    assert frame[9] == 4
    assert frame[10:] == b"abcd"
    with pytest.raises(ValueError):
        filesync.parse_rate_test_frame(frame)   # type 2 is not a rate-test frame


def test_secret_package_split_matches_the_z_slices() -> None:
    """z$c slices the RSA plaintext [0:32] / [32:44] / [44:56] and requires 56."""
    assert handshake.SECRET_MIN_LEN == 56
    assert handshake.SECRET_MAGIC == b"PLAUD.AI"
    javap = ROOT / "build/evidence/javap/sdk/ble/util/PlaudEncryptHeader.txt"
    if javap.is_file():
        assert "String PLAUD.AI" in javap.read_text()
    plain = bytes(range(80))
    parts = handshake.split_secret_package(plain)
    assert parts["chacha_key"] == plain[0:32]
    assert parts["chacha_nonce"] == plain[32:44]
    assert parts["chacha_aad"] == plain[44:56]
    assert parts["sealed_magic"] == plain[56:]
    with pytest.raises(ValueError):
        handshake.split_secret_package(bytes(55))


# --------------------------------------------------------------------------
# Which opcode table is which direction
# --------------------------------------------------------------------------


def test_w_c_is_the_request_table_and_w_a_the_response_table() -> None:
    """Settled by counting, not by assertion.

    `w$a` and `w$c` are two ~80-entry integer tables that overlap heavily, so
    the overlap says nothing. The opcodes UNIQUE to one table do: take each
    such opcode and ask whether a request class (getBleRequestType) or a
    response class (a()) claims it.

    The counts are lopsided and unambiguous, and they are the OPPOSITE of the
    intuitive reading of the alphabetical order. The one exception in each
    direction is reported rather than hidden.
    """
    tables = DIGEST.get("opcode_tables")
    if not tables:
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    a, c = set(tables["a"]), set(tables["c"])
    assert len(a) >= 60 and len(c) >= 60, "both tables should be ~70 distinct opcodes"
    assert len(a & c) > 50, "the tables overlap heavily, so only the unique opcodes decide"

    requests = {v["request_opcode"] for v in CLASSES.values() if v.get("request_opcode") is not None}
    responses = {v["response_opcode"] for v in CLASSES.values() if v.get("response_opcode") is not None}

    a_only, c_only = a - c, c - a
    a_rsp = len(a_only & responses)
    a_req = len(a_only & requests)
    c_req = len(c_only & requests)
    c_rsp = len(c_only & responses)

    assert a_rsp > a_req, (
        f"w$a-only opcodes map to {a_rsp} response classes vs {a_req} request classes"
    )
    assert c_req > c_rsp, (
        f"w$c-only opcodes map to {c_req} request classes vs {c_rsp} response classes"
    )
    # The marker constants agree: the two host->device pre-handshake markers
    # live in w$c and the device->host one lives in w$a.
    assert handshake.MARKER_PRE_HANDSHAKE in c_only and handshake.MARKER_FORCE_CLEAR in c_only
    assert handshake.MARKER_RSA_MARKER in a_only


def test_request_and_response_opcodes_are_not_always_equal() -> None:
    """The symmetric pairs (3/3, 6/6, 26/26, 22/22) invite a false rule. Find a
    request class whose opcode has no response class, proving the namespaces
    are independent and a reimplementer must not assume echo-the-opcode."""
    requests = {v["request_opcode"]: k for k, v in CLASSES.items() if v.get("request_opcode") is not None}
    responses = {v["response_opcode"]: k for k, v in CLASSES.items() if v.get("response_opcode") is not None}
    asymmetric = sorted(set(requests) - set(responses))
    assert asymmetric, "expected at least one request opcode with no matching response class"
    # And the reverse.
    assert sorted(set(responses) - set(requests))


# --------------------------------------------------------------------------
# k3 request structure, mechanically recovered
# --------------------------------------------------------------------------


def test_k3_request_structure_matches_sdk_bytecode() -> None:
    """The BOUND path's request side, broken out of the circular chain.

    Previously the k3 layout (header, stage rule, token widths, pad char)
    lived only in ledger prose plus an emulator and test written from the
    same reading. The digest now records k3.enPkg's structural facts by dumb
    pattern matching over javap text (see enpkg_structure), and this test
    pins the emulator to exactly those numbers:

    * packHead() is called -> the [01][01 00] header is real, not assumed;
    * compared constants include 3 and 9 -> the portVersion branch points;
    * pushed literals include 16, 32 (the token widths) and 48 ('0', the pad);
    * isEmpty/append/substring are all present (empty check, pad loop, trim).
    """
    s = CLASSES["k3"].get("enpkg_structure")
    assert s, "k3 has no mechanical enPkg structure; re-run the extractor"
    assert s["calls_packHead"] is True
    assert s["scratch_buffers"] == [255]
    compared = {c["value"] for c in s["compared_constants"]}
    assert {3, 9} <= compared, f"k3 branch thresholds: {compared}"
    assert {16, 32, 48} <= set(s["int_literals"]), s["int_literals"]
    for op in ("isEmpty", "append", "substring"):
        assert op in s["string_ops"], s["string_ops"]
    # The emulator must use exactly these numbers, no others. The MEANING
    # linkage (threshold 9 selects width 32; 48 pads; 3 gates the stage byte)
    # is ledger prose hand-verified against the same bytecode, triangulated
    # with the template-side fact that handshake tokens are 32 hex chars.
    assert handshake.token_width(7) == 16 and handshake.token_width(9) == 32
    assert {handshake.token_width(7), handshake.token_width(9)} <= set(s["int_literals"])
    short = (
        b"\x01\x01\x00" + bytes([0x02, 0x00, 0x00]) + b"0" * 15
    )
    with pytest.raises(ValueError):
        handshake.parse_handshake_request(short, 7)


def test_gatt_uuid_set_comes_from_w_d_bytecode() -> None:
    """The six UUIDs are extracted from w$d's javap, not hand-copied twice.

    Role assignment (which UUID notifies vs writes) remains ledger prose;
    what this test removes is the coincidence risk on the VALUES: a typo'd
    UUID in either place now fails.
    """
    uuids = DIGEST.get("gatt_uuids", [])
    assert len(uuids) == 6, uuids
    have = {
        profile.PLAUD_SERVICE_UUID.lower(),
        profile.DATA_NOTIFY_UUID.lower(),
        profile.COMMAND_WRITE_UUID.lower(),
    }
    assert have <= {u.lower() for u in uuids}, uuids
