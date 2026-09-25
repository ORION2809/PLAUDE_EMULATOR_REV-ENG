"""R1e: the manufacturer-specific advertising data the SDK parses on scan.

Why this milestone exists: `com.plaud.sdk.proto.u4.a(ScanResult)` is the only
gate between an emulator and the real client. Scanning is unfiltered at the BLE
stack level (`startScan` gets an empty ScanFilter list), all filtering is done
in software on the fields this parser recovers, and -- decisively --
`portVersion`, the switch that decides whether the entire link is
ChaCha20-Poly1305 encrypted, is read from here and never renegotiated.

`plaudsim.advertising.parse_manufacturer_data` is a port of the SDK's parser
written from the bytecode; `build_manufacturer_data` is written from the parse
RULES. A round trip between them is therefore a check of two separately
written artifacts, not of one function against itself. The assertions that
matter most, though, are the FIXED OFFSETS the bytecode pins (15 and 18) and
the branch that silently does not assign portVersion at all -- those cannot be
satisfied by any self-consistent pair of functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.advertising import (
    FIXED_15_OFFSET,
    FIXED_18_OFFSET,
    PRODUCT_NAMES,
    SHORT_SERIAL_LEN,
    ScanFields,
    build_manufacturer_data,
    parse_manufacturer_data,
)
from plaudsim.profile import ENCRYPTED_PORT_VERSION, PlaudPeripheral


def test_round_trip_recovers_every_field() -> None:
    blob = build_manufacturer_data(
        project_code=881,
        version_type="V",
        version_code=123,
        serial=b"\x88\x10\x00\x00\x01",
        port_version=7,
        bind_info=0,
    )
    f = parse_manufacturer_data(blob)
    assert f.project_code == 881
    assert f.version_type == "V"
    assert f.version_code == 123
    assert f.serial_number == "8810000001"
    assert f.port_version == 7
    assert f.bind_info == 0
    assert f.version_name == "V0123"
    assert f.product_name == "Plaud Note Pro"


def test_port_version_sits_at_the_fixed_offset_the_bytecode_pins() -> None:
    """u4.a reads portVersion with `b(bArr, 18)` for project codes 888/881 and
    `b(bArr, 15)` for everything else with a short serial. Those are literal
    operands (`bipush 18` / `bipush 15`), not cursor arithmetic, so the whole
    layout is constrained by them."""
    for project, offset in ((881, FIXED_18_OFFSET), (888, FIXED_18_OFFSET), (880, FIXED_15_OFFSET)):
        blob = build_manufacturer_data(
            project_code=project, serial=b"\x88\x10\x00", port_version=0x1234
        )
        assert int.from_bytes(blob[offset : offset + 2], "little") == 0x1234
        assert parse_manufacturer_data(blob).port_version == 0x1234


def test_long_serial_branch_never_assigns_port_version() -> None:
    """A serial of 10+ bytes takes the third branch of u4.a, which assigns
    serial/bindInfo but NOT portVersion -- so such a device is always seen as
    portVersion 0, i.e. cleartext. The builder refuses to produce it rather
    than emit an advertisement whose portVersion is silently ignored."""
    assert SHORT_SERIAL_LEN == 10
    with pytest.raises(ValueError, match="portVersion"):
        build_manufacturer_data(serial=b"8810000001", port_version=7)

    # Hand-build one to prove the parser really drops portVersion there.
    body = bytearray([2]) + (881).to_bytes(2, "little") + bytes([4, ord("V")])
    body += (1).to_bytes(3, "little")
    body += bytes([12]) + b"881000000123"
    body += bytes([0, 1, 0])
    f = parse_manufacturer_data(bytes(body))
    assert f.serial_number.startswith("881")
    assert f.port_version == 0, "the long-serial branch leaves portVersion at its default"


def test_short_serial_is_rendered_as_hex_not_ascii() -> None:
    """`if (len2 < 10)` selects `s7.b(byte[])`, a hex renderer. A reimplementer
    who decodes it as ASCII gets a different serial AND a different project
    code, because the SDK then overwrites projectCode with int(serial[:3])."""
    blob = build_manufacturer_data(project_code=881, serial=b"\xAB\xCD", port_version=7)
    f = parse_manufacturer_data(blob)
    assert f.serial_number == "ABCD"
    # "ABC" is not a decimal number, so the override is swallowed by the SDK's
    # own catch-all and the u16 project code survives.
    assert f.project_code == 881


def test_project_code_is_overwritten_from_the_serial_text() -> None:
    """u4.a ends with `if (sn.length() >= 3) projectCode = parseInt(sn[0:3])`,
    so the u16 at offset 1 is NOT the last word on the project code."""
    blob = build_manufacturer_data(project_code=881, serial=b"\x88\x20\x00", port_version=7)
    assert parse_manufacturer_data(blob).serial_number == "882000"
    assert parse_manufacturer_data(blob).project_code == 882, "overwritten from the serial"


def test_product_name_table() -> None:
    assert PRODUCT_NAMES == {
        "880": "Plaud NotePin",
        "888": "Plaud Note",
        "881": "Plaud Note Pro",
        "882": "Plaud NotePin S",
    }
    for code, name in PRODUCT_NAMES.items():
        f = ScanFields(serial_number=code + "0000")
        assert f.product_name == name
    assert ScanFields(serial_number="99").product_name is None


def test_version_name_matches_the_x2_construction() -> None:
    """BleDevice.getVersionName() and x2's versionName must agree, because the
    SDK's checkSn compares the advertised name against the on-wire SSN."""
    from plaudsim.handshake import parse_x2

    blob = build_manufacturer_data(version_type="V", version_code=42, serial=b"\x88\x10\x00")
    advertised = parse_manufacturer_data(blob).version_name

    frame = bytearray(63)
    frame[0:3] = bytes([0x01, 0x02, 0x00])
    frame[3:9] = b"881000"
    frame[9] = 0
    frame[59] = ord("V")
    frame[60:63] = (42).to_bytes(3, "little")
    on_wire = parse_x2(bytes(frame))["version_name"]

    assert advertised == on_wire == "V0042"


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x02",                       # a length byte and nothing else
        b"\x02\x71\x03",               # projectCode but no len1
        b"\x02\x71\x03\x04\x56",       # versionType but no versionCode
        b"\x02\x71\x03\x04\x56\x7b\x00\x00",   # versionCode but no serial length
    ],
)
def test_truncated_advertisements_are_rejected_loudly(blob: bytes) -> None:
    with pytest.raises(ValueError):
        parse_manufacturer_data(blob)


def test_unsupported_field_widths_are_rejected() -> None:
    """readInt only supports 8/16/24/32/64, so a len1 of 6 (a 5-byte
    versionCode) is not something the SDK can read."""
    body = bytearray([2]) + (881).to_bytes(2, "little") + bytes([6, ord("V")])
    body += bytes(20)
    with pytest.raises(ValueError, match="versionCode width"):
        parse_manufacturer_data(bytes(body))


def test_peripheral_advertisement_declares_its_own_port_version() -> None:
    """The emulator must not advertise a portVersion that contradicts what it
    actually speaks: the SDK would then expect an encrypted channel."""
    peripheral = PlaudPeripheral(None.__class__ and _NullDevice(), port_version=7)
    blob = peripheral.manufacturer_data()
    f = parse_manufacturer_data(blob)
    assert f.port_version == peripheral.port_version == 7
    assert f.port_version < ENCRYPTED_PORT_VERSION
    assert f.product_name == "Plaud Note Pro"


class _NullDevice:
    """Enough of a Bumble Device for construction; no link is needed here."""

    connections: dict = {}

    def on(self, *_args, **_kwargs) -> None:
        return None
