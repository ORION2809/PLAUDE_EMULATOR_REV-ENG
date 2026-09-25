"""BLE side of the Wi-Fi handoff: opcodes 10 OpenWiFi, 13 CloseWiFi, 16/17 WebSocket profile.

Codec tests use hand-computed bytes from the recovered layouts (citations in
emulator/plaudsim/profile.py). The round-trip tests drive PlaudPeripheral over
a real Bumble virtual link, brought up the way the Android SDK does it
(tests/support.connect_like_the_sdk), and read the responses off 2BB0.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from plaudsim.profile import (  # noqa: E402
    DEFAULT_WEBSOCKET_PROFILE,
    OPCODE_OPEN_WIFI,
    WEBSOCKET_TYPE_DEV_TOKEN,
    WEBSOCKET_TYPE_SER_TOKEN,
    WEBSOCKET_TYPE_URL,
    WIFI_OPEN_STATUS_BUSY,
    PlaudPeripheral,
    encode_close_wifi_response,
    encode_open_wifi_response,
    encode_websocket_get_request,
    encode_websocket_response,
    encode_websocket_set_request,
    parse_close_wifi_request,
    parse_close_wifi_response,
    parse_open_wifi_request,
    parse_open_wifi_response,
    parse_websocket_request,
    parse_websocket_response,
)
from plaudsim.transfer import TransferSession  # noqa: E402

OPEN_ON = bytes.fromhex("010a00" "01")
OPEN_OFF = bytes.fromhex("010a00" "00")
OPEN_HEADER_ONLY = bytes.fromhex("010a00")
OPEN_WITH_PASS = bytes.fromhex("010a00" "01") + b"12345678"
CLOSE = bytes.fromhex("010d00")


# --- opcode 10 -------------------------------------------------------------------------


def test_open_wifi_request_layouts():
    assert parse_open_wifi_request(OPEN_ON) == {"on_off": 1, "wifi_pass": None}
    assert parse_open_wifi_request(OPEN_OFF) == {"on_off": 0, "wifi_pass": None}
    assert parse_open_wifi_request(OPEN_HEADER_ONLY) == {"on_off": None, "wifi_pass": None}
    assert parse_open_wifi_request(OPEN_WITH_PASS) == {"on_off": 1, "wifi_pass": "12345678"}
    for bad in (b"\x01\x0a", b"\x01\x0b\x00\x01", b"\x02\x0a\x00\x01", OPEN_ON + b"1234567", OPEN_WITH_PASS + b"x"):
        with pytest.raises(ValueError):
            parse_open_wifi_request(bad)


def test_open_wifi_response_bytes_and_host_mirror():
    assert encode_open_wifi_response(0) == bytes.fromhex("010a00" "00")
    assert encode_open_wifi_response(0, "10000001") == bytes.fromhex("010a00" "00") + b"10000001"
    assert encode_open_wifi_response(4) == bytes.fromhex("010a00" "04")
    with pytest.raises(ValueError):
        encode_open_wifi_response(0, "short")
    with pytest.raises(ValueError):
        encode_open_wifi_response(256)
    # j4: wifiPass only when len >= 12; an 11-byte frame has status but no pass
    assert parse_open_wifi_response(bytes.fromhex("010a0000") + b"1234567") == {"status": 0, "wifi_pass": None}
    assert parse_open_wifi_response(bytes.fromhex("010a0000") + b"12345678") == {"status": 0, "wifi_pass": "12345678"}
    assert parse_open_wifi_response(bytes.fromhex("010a0004")) == {"status": 4, "wifi_pass": None}


# --- opcode 13 -------------------------------------------------------------------------


def test_close_wifi_layouts():
    assert parse_close_wifi_request(CLOSE) == {}
    with pytest.raises(ValueError):
        parse_close_wifi_request(CLOSE + b"\x00")
    assert encode_close_wifi_response(0) == bytes.fromhex("010d00" "00")
    assert encode_close_wifi_response(7) == bytes.fromhex("010d00" "07")
    assert parse_close_wifi_response(bytes.fromhex("010d0007")) == {"status": 7}


# --- opcode 16 / 17 --------------------------------------------------------------------


def test_websocket_get_request_is_5_bytes():
    assert encode_websocket_get_request(WEBSOCKET_TYPE_URL) == bytes.fromhex("011000" "01" "01")
    assert encode_websocket_get_request(WEBSOCKET_TYPE_DEV_TOKEN) == bytes.fromhex("011000" "01" "03")
    with pytest.raises(ValueError, match="url/serToken/devToken"):
        encode_websocket_get_request(0)
    assert parse_websocket_request(bytes.fromhex("0110000102")) == {"op": 1, "type": 2, "name": "serToken", "value": None}
    with pytest.raises(ValueError):
        parse_websocket_request(bytes.fromhex("0110000102") + b"\x00")


def test_websocket_set_request_pads_to_64_or_16():
    url = encode_websocket_set_request(WEBSOCKET_TYPE_URL, "wss://a.invalid/x")
    assert url[:5] == bytes.fromhex("011000" "02" "01")
    assert len(url) == 5 + 64
    assert url[5:] == b"wss://a.invalid/x" + b"\x00" * (64 - 17)
    tok = encode_websocket_set_request(WEBSOCKET_TYPE_SER_TOKEN, "SYN-SER-TOKEN-01")
    assert len(tok) == 5 + 16 and tok[5:] == b"SYN-SER-TOKEN-01"   # 16 bytes: no NUL left
    with pytest.raises(ValueError, match="url length must be < 64"):
        encode_websocket_set_request(WEBSOCKET_TYPE_URL, "u" * 64)
    assert len(encode_websocket_set_request(WEBSOCKET_TYPE_URL, "u" * 63)) == 69
    with pytest.raises(ValueError, match="token length must be <= 16"):
        encode_websocket_set_request(WEBSOCKET_TYPE_DEV_TOKEN, "t" * 17)
    parsed = parse_websocket_request(url)
    assert parsed == {"op": 2, "type": 1, "name": "url", "value": "wss://a.invalid/x"}
    assert parse_websocket_request(tok)["value"] == "SYN-SER-TOKEN-01"
    with pytest.raises(ValueError):
        parse_websocket_request(url[:-1])       # wrong width for type 1
    with pytest.raises(ValueError):
        parse_websocket_request(bytes.fromhex("0110000301") + b"\x00" * 16)   # unknown op 3


def test_websocket_response_layout_and_v2_quirks():
    rsp = encode_websocket_response(WEBSOCKET_TYPE_URL, "wss://a.invalid/x")
    assert rsp[:4] == bytes.fromhex("011100" "01") and len(rsp) == 4 + 64
    assert parse_websocket_response(rsp) == {"type": 1, "name": "url", "content": "wss://a.invalid/x"}
    tok = encode_websocket_response(WEBSOCKET_TYPE_DEV_TOKEN, "SYN-DEV-TOKEN-01")
    assert len(tok) == 4 + 16
    # a full 16-byte token has no NUL: the scan resets to 4 and takes the whole width
    assert parse_websocket_response(tok)["content"] == "SYN-DEV-TOKEN-01"
    # the v2 quirk: an EMPTY value (NUL at offset 4) reads as the FULL width of NULs
    empty = encode_websocket_response(WEBSOCKET_TYPE_SER_TOKEN, "")
    assert parse_websocket_response(empty)["content"] == "\x00" * 16
    # a short frame without a NUL throws in Java; we raise
    with pytest.raises(ValueError):
        parse_websocket_response(bytes.fromhex("01110002") + b"abc")
    # a short frame WITH a NUL is fine
    assert parse_websocket_response(bytes.fromhex("01110002") + b"abc\x00")["content"] == "abc"
    # unknown type: enum stays null, width 16
    assert parse_websocket_response(bytes.fromhex("01110009") + b"q\x00" + b"\x00" * 14)["name"] is None
    with pytest.raises(ValueError):
        encode_websocket_response(WEBSOCKET_TYPE_SER_TOKEN, "t" * 17)


# --- handler-level behaviour ----------------------------------------------------------------


class _StubDevice:
    def on(self, *_a, **_k):
        return None


def make_peripheral(**kw) -> PlaudPeripheral:
    return PlaudPeripheral(_StubDevice(), **kw)


def test_open_wifi_reports_status_and_serial_derived_passphrase():
    p = make_peripheral()
    assert p.scan_fields.serial_number == "8810000001"
    assert p._open_wifi(OPEN_ON) == [encode_open_wifi_response(0, "10000001")]
    assert p.wifi_hotspot_on is True
    assert p._open_wifi(OPEN_OFF) == [encode_open_wifi_response(0)]
    assert p.wifi_hotspot_on is False
    assert p._open_wifi(OPEN_HEADER_ONLY) == [encode_open_wifi_response(0, "10000001")]
    assert [e["status"] for e in p.wifi_log] == [0, 0, 0]


def test_open_wifi_is_refused_with_busy_status_while_a_transfer_streams():
    p = make_peripheral()
    p.transfer = TransferSession(file_bytes=b"x" * 10)
    p.transfer.start(1, 0, 0)          # active, not done
    assert p._open_wifi(OPEN_ON) == [encode_open_wifi_response(WIFI_OPEN_STATUS_BUSY)]
    assert p.wifi_hotspot_on is False
    p.transfer.frames()                # done
    assert p._open_wifi(OPEN_ON) == [encode_open_wifi_response(0, "10000001")]


def test_open_status_policy_is_configurable_and_pass_is_absent_on_failure():
    p = make_peripheral(wifi_open_status=9)
    assert p._open_wifi(OPEN_ON) == [bytes.fromhex("010a0009")]
    assert p.wifi_hotspot_on is False


def test_close_wifi_drops_the_hotspot_flag():
    p = make_peripheral(wifi_close_status=0)
    p._open_wifi(OPEN_ON)
    assert p._close_wifi(CLOSE) == [encode_close_wifi_response(0)]
    assert p.wifi_hotspot_on is False


def test_websocket_get_then_set_then_get():
    p = make_peripheral()
    assert p._websocket_profile(encode_websocket_get_request(WEBSOCKET_TYPE_URL)) == [
        encode_websocket_response(WEBSOCKET_TYPE_URL, DEFAULT_WEBSOCKET_PROFILE[WEBSOCKET_TYPE_URL])
    ]
    new_url = "wss://synthetic.invalid/other"
    assert p._websocket_profile(encode_websocket_set_request(WEBSOCKET_TYPE_URL, new_url)) == [
        encode_websocket_response(WEBSOCKET_TYPE_URL, new_url)
    ]
    assert p.websocket_profile[WEBSOCKET_TYPE_URL] == new_url
    assert p._websocket_profile(encode_websocket_get_request(WEBSOCKET_TYPE_URL)) == [
        encode_websocket_response(WEBSOCKET_TYPE_URL, new_url)
    ]
    # tokens are independent slots
    assert p._websocket_profile(encode_websocket_set_request(WEBSOCKET_TYPE_DEV_TOKEN, "T")) == [
        encode_websocket_response(WEBSOCKET_TYPE_DEV_TOKEN, "T")
    ]
    assert p.websocket_profile[WEBSOCKET_TYPE_SER_TOKEN] == DEFAULT_WEBSOCKET_PROFILE[WEBSOCKET_TYPE_SER_TOKEN]
    assert [e["op"] for e in p.wifi_log] == [1, 2, 1, 2]


def test_websocket_type_zero_is_rejected_as_malformed_on_the_wire():
    p = make_peripheral()
    emitted: list[bytes] = []

    async def record(_conn, frame: bytes) -> None:
        emitted.append(frame)

    p._emit = record  # type: ignore[assignment]
    asyncio.run(p._on_command_write(None, bytes.fromhex("0110000100")))
    assert emitted == []
    assert p.packet_log[-1]["direction"] == "error"
    assert "malformed_request" in p.packet_log[-1]["reason"]


def test_all_three_opcodes_are_dispatched():
    p = make_peripheral()
    emitted: list[bytes] = []

    async def record(_conn, frame: bytes) -> None:
        emitted.append(frame)

    p._emit = record  # type: ignore[assignment]
    asyncio.run(p._on_command_write(None, OPEN_ON))
    asyncio.run(p._on_command_write(None, encode_websocket_get_request(WEBSOCKET_TYPE_SER_TOKEN)))
    asyncio.run(p._on_command_write(None, CLOSE))
    assert emitted == [
        encode_open_wifi_response(0, "10000001"),
        encode_websocket_response(WEBSOCKET_TYPE_SER_TOKEN, "SYN-SER-TOKEN-01"),
        encode_close_wifi_response(0),
    ]
    assert not [e for e in p.packet_log if e.get("direction") == "error"]
    assert OPCODE_OPEN_WIFI == 10


# --- over a real Bumble link ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wifi_handoff_round_trips_over_bumble_gatt() -> None:
    from support import connect_like_the_sdk

    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, OPEN_ON, with_response=True)
    await peer.write_value(command, encode_websocket_get_request(WEBSOCKET_TYPE_URL), with_response=True)
    await peer.write_value(
        command, encode_websocket_set_request(WEBSOCKET_TYPE_SER_TOKEN, "SYN-NEW-SER-0001"), with_response=True
    )
    await peer.write_value(command, encode_websocket_get_request(WEBSOCKET_TYPE_SER_TOKEN), with_response=True)
    await peer.write_value(command, CLOSE, with_response=True)
    await asyncio.sleep(0.05)

    assert responses == [
        encode_open_wifi_response(0, "10000001"),
        encode_websocket_response(WEBSOCKET_TYPE_URL, DEFAULT_WEBSOCKET_PROFILE[WEBSOCKET_TYPE_URL]),
        encode_websocket_response(WEBSOCKET_TYPE_SER_TOKEN, "SYN-NEW-SER-0001"),
        encode_websocket_response(WEBSOCKET_TYPE_SER_TOKEN, "SYN-NEW-SER-0001"),
        encode_close_wifi_response(0),
    ]
    # the host-side mirrors read them back
    assert parse_open_wifi_response(responses[0]) == {"status": 0, "wifi_pass": "10000001"}
    assert parse_websocket_response(responses[2])["content"] == "SYN-NEW-SER-0001"
    assert parse_close_wifi_response(responses[4]) == {"status": 0}
    assert [e["opcode"] for e in peripheral.wifi_log] == [10, 16, 16, 16, 13]
    assert peripheral.wifi_hotspot_on is False
    assert not [e for e in peripheral.packet_log if e.get("direction") == "error"]
    assert len(responses[1]) == 4 + 64 and len(responses[2]) == 4 + 16
