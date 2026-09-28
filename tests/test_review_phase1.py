"""Findings of the 2026-09-28 independent review of the phase-1 protocol code
(emulator/plaudsim advertising, handshake, sealed, transfer, filesync, audio,
profile; emulator/serve.py).  Each test pins one confirmed defect's fix; the
bytecode citations are in the fixed code's comments and in
docs/progress-report.md.  Test names say what the SDK does."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from plaudsim.advertising import (  # noqa: E402
    ScanFields,
    build_manufacturer_data,
    parse_manufacturer_data,
    scrub_serial,
)
from plaudsim.handshake import (  # noqa: E402
    MARKER_SECRET,
    ReplayWindow,
    pack_marker_frame,
    parse_handshake_request,
    reassemble_secret_chunks,
)
from plaudsim.profile import WIFI_OPEN_STATUS_FAILED, PlaudBatteryState, PlaudPeripheral  # noqa: E402
from plaudsim.transfer import FILE_LIST_HEADER_LEN, FileListAccumulator, FileTable  # noqa: E402


class _StubDevice:
    def on(self, *_a, **_k):
        return None


# --- advertising (u4.a) ---------------------------------------------------------


def test_builder_refuses_the_cursor_branch_projects() -> None:
    for code in (901, 705):
        with pytest.raises(ValueError, match="cursor branch"):
            build_manufacturer_data(project_code=code, serial=b"\x90\x10\x00\x00\x01")


def test_version_code_is_narrowed_to_a_signed_int_like_l2i() -> None:
    blob = bytearray(build_manufacturer_data())
    # rebuild with a 4-byte versionCode of FF FF FF FF: len1 = 5
    raw = bytes([2]) + blob[1:3] + bytes([5]) + b"V" + b"\xff\xff\xff\xff" + bytes([5]) + b"\x88\x10\x00\x00\x01"
    raw += bytes(18 - len(raw) + 1) + b"\x07\x00" + b"\x01\x00"
    fields = parse_manufacturer_data(raw)
    assert fields.version_code == -1 and fields.version_name == "V-001"


def test_serial_is_scrubbed_with_the_sdks_character_class() -> None:
    # Java keeps '-', 0-9, A-Z, U+005F..U+2E80 and U+9FFF (JDK 17, whole BMP)
    assert scrub_serial("8810000001\x00\x00") == "8810000001"
    assert scrub_serial("88_1ab`{|}~ é中鿿-.") == "88_1ab`{|}~é鿿-"
    # product_name reads the scrubbed serial: "88_" is no product
    assert ScanFields(serial_number="88_1XXXXXXX").product_name is None
    assert ScanFields(serial_number="8810000001").product_name == "Plaud Note Pro"


def test_project_code_override_parses_like_integer_parse_int() -> None:
    # a serial whose first three characters are not an integer keeps the u16 code
    base = build_manufacturer_data(project_code=881, serial=b"\x88\x10\x00\x00\x01")
    assert parse_manufacturer_data(base).project_code == 881
    from plaudsim.advertising import _java_parse_int

    assert _java_parse_int("1_2") is None and _java_parse_int(" 12") is None
    assert _java_parse_int("+12") == 12 and _java_parse_int("-12") == -12 and _java_parse_int("881") == 881


# --- handshake (k3/j3, FE12, receive counter) --------------------------------------


def test_a_five_byte_handshake_is_rejected_not_an_index_error() -> None:
    with pytest.raises(ValueError, match="too short"):
        parse_handshake_request(b"\x01\x01\x00\x02\x00", 7)


def test_is_second_below_port_version_3_comes_from_the_j3_block() -> None:
    from plaudsim.handshake import build_k3

    k3 = build_k3("TOKEN", 2)
    assert parse_handshake_request(k3, 2)["is_second"] is False
    j3 = k3 + b"DEVTOKEN" + bytes([4]) + b"user"
    parsed = parse_handshake_request(j3, 2)
    assert parsed["is_second"] is True and parsed["dev_token"] == "DEVTOKEN"


def test_receive_counter_compares_signed_like_the_sdk() -> None:
    w = ReplayWindow()
    assert w.accept(0) and w.accept(5)
    assert w.accept(0x80000000) is False  # -2147483648 after l2i


def test_secret_reassembly_completes_at_the_first_matching_count() -> None:
    frames = [pack_marker_frame(MARKER_SECRET, 2, i, bytes([65 + i]) * 2) for i in range(3)]
    assert reassemble_secret_chunks(frames) == b"AABB"  # complete after the second frame


def test_secret_count_of_128_or_more_is_negative_and_never_completes() -> None:
    frames = [pack_marker_frame(MARKER_SECRET, 200, i, bytes([i])) for i in range(200)]
    with pytest.raises(ValueError, match="count says -56"):
        reassemble_secret_chunks(frames)


# --- sealed round trips ---------------------------------------------------------


def test_sealed_sync_reports_the_sequences_actually_sealed() -> None:
    from plaudsim.sealed import SealedLink, sealed_sync_roundtrip

    link = SealedLink()  # synthetic key material
    data = bytes(range(96))
    first = sealed_sync_roundtrip(link, data, 7)
    second = sealed_sync_roundtrip(link, data, 7)
    assert first["request_seq"] == 2 and second["request_seq"] == 3
    n = len(first["response_wires"])
    assert first["response_seqs"] == list(range(2, 2 + n))
    assert second["response_seqs"] == list(range(2 + n, 2 + 2 * n))
    assert second["payload"] == data


def test_sealed_sync_payload_check_notices_a_wrong_offset(monkeypatch) -> None:
    import plaudsim.filesync as fs
    from plaudsim.sealed import SealedLink, sealed_sync_roundtrip

    real = fs.parse_file_data_frame

    def shifted(frame, pv):
        d = dict(real(frame, pv))
        if d["offset"] == 32:
            d["offset"] = 40
        return d

    monkeypatch.setattr(fs, "parse_file_data_frame", shifted)
    with pytest.raises(AssertionError, match="does not continue"):
        sealed_sync_roundtrip(SealedLink(), bytes(range(96)), 7)


# --- file list and frame sizes ------------------------------------------------------


@pytest.mark.parametrize("pv,stride", [(7, 10), (4, 9), (1, 8)])
def test_file_list_pages_fit_one_notification(pv: int, stride: int) -> None:
    table = FileTable([{"session_id": 1000 + i, "file_size": 10 * i, "scene": 0, "attribute": 0} for i in range(40)], port_version=pv)
    frames = table.frames(0x1234, None, max_frame_len=252)
    assert all(len(f) <= 252 for f in frames) and len(frames) > 1
    assert len(frames[0]) == FILE_LIST_HEADER_LEN + stride * ((252 - FILE_LIST_HEADER_LEN) // stride)
    acc = FileListAccumulator(port_version=pv)
    for f in frames:
        assert acc.ingest_frame(f) == "accepted"
    assert acc.complete and len(acc.entries) == 40


# --- peripheral ------------------------------------------------------------------


def test_advertised_and_served_port_versions_must_agree() -> None:
    with pytest.raises(ValueError, match="must declare the protocol"):
        PlaudPeripheral(_StubDevice(), scan_fields=ScanFields(project_code=881, serial_number="8810000001", port_version=20))


@pytest.mark.asyncio
async def test_a_packing_error_is_a_rejected_request_not_an_escaping_exception() -> None:
    p = PlaudPeripheral(_StubDevice(), file_table=[{"session_id": 2**32, "file_size": 1}])
    sent: list[bytes] = []

    async def respond(_c, frame):
        sent.append(frame)

    p._respond = respond  # type: ignore[assignment]
    from plaudsim.filesync import pack_file_list_request

    await p._on_command_write(None, pack_file_list_request(1, 0, False))
    assert sent == []
    assert p.packet_log[-1]["direction"] == "error" and p.packet_log[-1]["reason"].startswith("malformed_request")


def test_a_failing_wifi_factory_answers_a_failure_and_leaves_the_hotspot_down() -> None:
    from plaudsim.profile import OPCODE_OPEN_WIFI

    def broken(_p):
        raise ValueError("chunk_size must be positive")

    p = PlaudPeripheral(_StubDevice(), wifi_device_factory=broken)
    frames = p._open_wifi(b"\x01" + OPCODE_OPEN_WIFI.to_bytes(2, "little") + b"\x00")
    assert frames[0][3] == WIFI_OPEN_STATUS_FAILED and p.wifi_hotspot_on is False
    assert "failed" in p.wifi_log[-1]


def test_push_battery_keeps_the_battery_characteristic_in_step() -> None:
    p = PlaudPeripheral(_StubDevice(), battery=PlaudBatteryState(level=90))
    p._live_connection = lambda connection=None: None  # type: ignore[assignment]  # no link here
    assert p.battery_characteristic.value == bytes([90])
    coro = p.push_battery(PlaudBatteryState(level=15))
    coro.close()  # not awaited: no link; the value is updated synchronously
    assert p.battery_characteristic.value == bytes([15])


# --- serve.py ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["4294967296", "-1", "x"])
def test_serve_refuses_a_session_id_that_is_not_a_u32(value: str) -> None:
    from serve import ConfigError, ServeConfig

    with pytest.raises(ConfigError, match="PLAUD_SESSION_ID"):
        ServeConfig.from_env({"PLAUD_SESSION_ID": value})
