"""R7-S12: opcode 8, the CommonSettings read/set channel (q0 -> r0).

Discovered at runtime: after getState the genuine SDK writes two opcode-8
frames, `01 08 00 01 0f 00` and `01 08 00 01 11 00`, which the emulator used
to reject as `unsupported_opcode`. Bytecode resolves them completely:

  * q0.enPkg  = packHead + c(action) [1 B] + a(type) [2 B LE]
                (+ a(J)(value) [4 B LE] on the SETTING path)     ALL.txt:59528+
  * r0.<init> = type u16le @3, value u32le @5 ("CommonSettingsRsp") ALL.txt:30994+
  * CommonAction wire values READ=1 / SETTING=2 are the enum's int field, not
    its ordinal                                                    ALL.txt:49154+
  * CommonType: ProGuard kept the 21 NAME strings; the wire value is the third
    ctor arg and is NOT the ordinal (ENABLE_VAD=15, REC_MODE=17, ... ,
    IBEACON_WAKEUP=49)                                             ALL.txt:22853+
  * q.f consumes the reply: ENABLE_VAD -> setVadOpen(v == 1);
    REC_MODE -> setNcClose(v != 2); VAD_SENSITIVITY/VPU_GAIN/MIC_GAIN -> int
    setters                                                        ALL.txt:41224+

The values the emulator reports are HARNESS_POLICY and labelled as such. The
javap-pinned tests below read the bytecode dump so a future edit cannot quietly
change a wire value; they skip when build/evidence is absent.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from plaudsim.profile import (  # noqa: E402
    COMMON_ACTION_READ,
    COMMON_ACTION_SET,
    COMMON_TYPE_ENABLE_VAD,
    COMMON_TYPE_NAMES,
    COMMON_TYPE_REC_MODE,
    DEFAULT_COMMON_SETTINGS,
    OPCODE_COMMON_SETTINGS,
    PlaudPeripheral,
    encode_common_settings_response,
    parse_common_settings_request,
)

JAVAP = ROOT / "build/evidence/javap/ALL.txt"

RUNTIME_READ_VAD = bytes.fromhex("010800010f00")
RUNTIME_READ_RECMODE = bytes.fromhex("010800011100")


# --- request parsing ----------------------------------------------------------

def test_the_two_runtime_frames_are_reads_of_enable_vad_and_rec_mode():
    a = parse_common_settings_request(RUNTIME_READ_VAD)
    b = parse_common_settings_request(RUNTIME_READ_RECMODE)
    assert a == {"action": COMMON_ACTION_READ, "type": 15, "name": "ENABLE_VAD", "value": None}
    assert b == {"action": COMMON_ACTION_READ, "type": 17, "name": "REC_MODE", "value": None}


def test_setting_frame_carries_a_u32le_value():
    frame = b"\x01\x08\x00" + bytes([COMMON_ACTION_SET]) + b"\x0f\x00" + b"\x01\x00\x00\x00"
    assert parse_common_settings_request(frame) == {
        "action": COMMON_ACTION_SET, "type": 15, "name": "ENABLE_VAD", "value": 1,
    }


@pytest.mark.parametrize("bad", [
    b"\x01\x08\x00",                       # no payload
    b"\x01\x08\x00\x01\x0f",               # type truncated
    b"\x01\x08\x00\x02\x0f\x00\x01\x00",   # SET without a full u32
    b"\x01\x08\x00\x07\x0f\x00",           # unknown action
    b"\x01\x03\x00\x01\x0f\x00",           # wrong opcode
    b"\x02\x08\x00\x01\x0f\x00",           # wrong protocol type
])
def test_malformed_requests_are_rejected_not_guessed(bad):
    with pytest.raises(ValueError):
        parse_common_settings_request(bad)


def test_unknown_type_parses_with_no_name():
    p = parse_common_settings_request(b"\x01\x08\x00\x01\xff\x00")
    assert p["type"] == 255 and p["name"] is None


# --- response encoding --------------------------------------------------------

def test_response_layout_is_9_bytes_type_u16_value_u32():
    assert encode_common_settings_response(15, 1).hex() == "0108000f0001000000"
    assert encode_common_settings_response(0x1234, 0xDEADBEEF).hex() == "0108003412efbeadde"
    with pytest.raises(ValueError):
        encode_common_settings_response(0x10000, 0)
    with pytest.raises(ValueError):
        encode_common_settings_response(1, 1 << 32)


# --- the name table -----------------------------------------------------------

def test_common_type_table_has_21_entries_and_is_not_the_ordinal():
    assert len(COMMON_TYPE_NAMES) == 21
    assert COMMON_TYPE_NAMES[COMMON_TYPE_ENABLE_VAD] == "ENABLE_VAD"
    assert COMMON_TYPE_NAMES[COMMON_TYPE_REC_MODE] == "REC_MODE"
    assert COMMON_TYPE_NAMES[49] == "IBEACON_WAKEUP"
    # the Swift-ordinal guess would have called 15/17 AUTO_RECORD/FIND_MY; it is wrong
    assert COMMON_TYPE_NAMES[25] == "AUTO_RECORD" and COMMON_TYPE_NAMES[27] == "FIND_MY"


@pytest.mark.skipif(not JAVAP.exists(), reason="build/evidence not built")
def test_common_type_wire_values_are_pinned_to_the_bytecode():
    text = JAVAP.read_text(errors="replace")
    start = text.index("com.plaud.sdk.proto.s0$b$b extends java.lang.Enum")
    block = text[start:start + 20000]
    # each constant: ldc "NAME" ; <ordinal> ; <wire> ; putstatic
    pat = re.compile(r'ldc\s+#\d+\s+// String ([A-Z_]+)\n\s+\d+: (?:iconst_(\d)|bipush\s+(\d+))\n\s+\d+: (?:iconst_(\d)|bipush\s+(\d+))')
    found = {}
    for m in pat.finditer(block):
        name = m.group(1)
        wire = int(m.group(4) or m.group(5))
        found[wire] = name
        if len(found) == 21:
            break
    assert found == COMMON_TYPE_NAMES


@pytest.mark.skipif(not JAVAP.exists(), reason="build/evidence not built")
def test_common_action_wire_values_are_pinned_to_the_bytecode():
    text = JAVAP.read_text(errors="replace")
    start = text.index("com.plaud.sdk.proto.s0$b$a extends java.lang.Enum")
    block = text[start:start + 4000]
    assert re.search(r'String READ\n\s+\d+: iconst_0\n\s+\d+: iconst_1', block)
    assert re.search(r'String SETTING\n\s+\d+: iconst_1\n\s+\d+: iconst_2', block)


@pytest.mark.skipif(not JAVAP.exists(), reason="build/evidence not built")
def test_r0_reads_type_at_3_as_u16_and_value_at_5_as_u32():
    text = JAVAP.read_text(errors="replace")
    start = text.index("public class com.plaud.sdk.proto.r0 extends com.plaud.sdk.proto.n")
    block = text[start:start + 3000]
    assert re.search(r'iconst_3\n\s+\d+: invokevirtual #\d+\s+// Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.b:\(\[BI\)I', block)
    assert re.search(r'iconst_5\n\s+\d+: invokevirtual #\d+\s+// Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.d:\(\[BI\)J', block)
    assert "CommonSettingsRsp{type=" in block


# --- device behaviour ---------------------------------------------------------

class _StubDevice:
    """Enough of a bumble Device for handler-level tests: the peripheral only
    registers a connection listener at construction."""

    def on(self, *_a, **_k):
        return None


def make_peripheral(**kw) -> PlaudPeripheral:
    return PlaudPeripheral(_StubDevice(), **kw)


def test_read_answers_the_policy_value_and_logs_the_request():
    p = make_peripheral()
    assert p._common_settings(RUNTIME_READ_VAD) == [
        encode_common_settings_response(15, DEFAULT_COMMON_SETTINGS[15])
    ]
    assert p._common_settings(RUNTIME_READ_RECMODE) == [
        encode_common_settings_response(17, DEFAULT_COMMON_SETTINGS[17])
    ]
    assert [e["name"] for e in p.settings_log] == ["ENABLE_VAD", "REC_MODE"]


def test_setting_stores_then_echoes_and_unknown_type_reads_zero():
    p = make_peripheral()
    set_frame = b"\x01\x08\x00" + bytes([COMMON_ACTION_SET]) + b"\x0f\x00" + b"\x00\x00\x00\x00"
    assert p._common_settings(set_frame) == [encode_common_settings_response(15, 0)]
    assert p.common_settings[15] == 0
    assert p._common_settings(RUNTIME_READ_VAD) == [encode_common_settings_response(15, 0)]
    # IBEACON_WAKEUP (49) has no policy value: reads as 0, never invented
    assert p._common_settings(b"\x01\x08\x00\x01\x31\x00") == [
        encode_common_settings_response(49, 0)
    ]


def test_opcode_8_is_now_dispatched_not_rejected():
    p = make_peripheral()
    emitted: list[bytes] = []

    async def record(_conn, frame: bytes) -> None:
        emitted.append(frame)

    p._emit = record  # type: ignore[assignment]
    asyncio.run(p._on_command_write(None, RUNTIME_READ_VAD))
    assert emitted == [encode_common_settings_response(15, 1)]
    assert not [e for e in p.packet_log if e.get("direction") == "error"]
    assert p.packet_log[0] == {"direction": "request", "bytes": RUNTIME_READ_VAD.hex()}


def test_l3_timezone_is_plumbed_into_the_handshake_response():
    from plaudsim.handshake import encode_l3

    p = make_peripheral(l3_timezone=5)
    k3 = bytes.fromhex("01010002000053594e54484849535430303031303030")
    frames = p._handshake(k3)
    assert frames[0][6] == 5
    assert frames[0] == encode_l3(
        status=0, port_version=p.port_version, timezone=5,
        version_type=p.scan_fields.version_type, version=p.scan_fields.version_code,
    )


@pytest.mark.asyncio
async def test_common_settings_round_trip_over_bumble_gatt() -> None:
    """The exact two READ frames the real SDK wrote, over a real GATT link."""
    from support import connect_like_the_sdk

    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, RUNTIME_READ_VAD, with_response=True)
    await peer.write_value(command, RUNTIME_READ_RECMODE, with_response=True)
    await asyncio.sleep(0.05)

    assert responses[-2:] == [
        encode_common_settings_response(15, DEFAULT_COMMON_SETTINGS[15]),
        encode_common_settings_response(17, DEFAULT_COMMON_SETTINGS[17]),
    ]
    assert [e["type"] for e in peripheral.settings_log] == [15, 17]
    assert not [e for e in peripheral.packet_log if e.get("direction") == "error"]
