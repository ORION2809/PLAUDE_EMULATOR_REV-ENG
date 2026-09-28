"""BLE advertising: the manufacturer-specific data the SDK parses on scan.

This matters more than it looks. `com.plaud.sdk.proto.u4.a(ScanResult)` is the
only thing standing between an emulator and the real client, for two reasons:

1. **Scanning is unfiltered at the BLE stack level.** `startScan` is handed a
   freshly allocated empty `ScanFilter` list, so the public
   `t3.a(ScanFilter)` / `t3.b(ScanFilter)` API has no effect. ALL filtering is
   software, performed on the fields this parser recovers, and a result whose
   serial number or MAC comes out empty is dropped.
2. **portVersion -- the switch that decides whether the whole link is
   ChaCha20-Poly1305 encrypted -- is read from here**, not negotiated. `z`
   consults `l().getPortVersion()` on every send and every notification, and
   the field is `final` on `BleDevice`, never refreshed from the handshake.

So a device declares, before a central ever connects, whether it speaks
cleartext. That is what lets this project ship a cleartext emulator honestly.

EVIDENCE. `u4.a(ScanResult)` is the single worst method in the artifact for
jadx: it renders `bArr[0]` as `bArr[0] ? 1 : 0` and mangles the control flow
into something that does not compile back. Everything below was walked in
`javap/com/plaud/sdk/proto/u4.txt`, bytecode offsets 254-719, and the offsets
are quoted per rule.

CONFIDENCE. The PARSE RULES are SOURCE-DERIVED: they are what the shipped
client does, and `build_manufacturer_data` is therefore a correct way to be
parsed as intended. What a REAL device actually advertises is UNKNOWN -- no
capture exists. Do not read this module as a record of observed hardware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from struct import pack, unpack_from

#: `readInt(width, buf, off)` supports 8/16/24/32/64 only.
_READ_WIDTHS = {1: 8, 2: 16, 3: 24, 4: 32, 8: 64}

#: Model-code -> product name, used only when the device advertises no GAP
#: name. From the string table at the end of u4.a.
PRODUCT_NAMES = {
    "880": "Plaud NotePin",
    "888": "Plaud Note",
    "881": "Plaud Note Pro",
    "882": "Plaud NotePin S",
}

#: Project codes whose branch reads portVersion at a CURSOR-RELATIVE offset.
CURSOR_PORT_VERSION_PROJECTS = (901, 705)
#: Project codes whose branch reads portVersion at the FIXED offset 18.
FIXED_18_PROJECTS = (888, 881)
#: Every other project code with a short serial reads it at the FIXED offset 15.
FIXED_15_OFFSET = 15
FIXED_18_OFFSET = 18

#: The serial-number length below which the serial is rendered as HEX rather
#: than as ASCII text (`if (len2 < 10)`, bytecode offset 448).
SHORT_SERIAL_LEN = 10


def scrub_serial(text: str) -> str:
    """`u4.a` 795-805: ``serial.replaceAll("[^a-zA-Z0-9_-\\u2E80-\\u9FFF]", "")``.

    Java parses ``_-\\u2E80`` as a RANGE (U+005F..U+2E80) and the trailing
    ``-\\u9FFF`` as a literal hyphen and U+9FFF, so the kept set is: ``-``,
    ``0-9``, ``A-Z``, U+005F..U+2E80 (which includes ``a-z``, backtick,
    ``{|}~``, DEL and all of Latin-1) and U+9FFF.  Most CJK characters are
    therefore REMOVED, whatever the pattern's author meant.  Established by
    running the pattern in a JDK 17 over the whole BMP (2026-09-28).
    Characters above the BMP are two surrogates in Java, neither kept."""
    return "".join(
        c for c in text
        if c == "-" or "0" <= c <= "9" or "A" <= c <= "Z" or 0x5F <= ord(c) <= 0x2E80 or ord(c) == 0x9FFF
    )


def _java_parse_int(text: str) -> int | None:
    """``Integer.parseInt``: an optional sign then decimal digits (Unicode
    decimal digits count, as in ``Character.digit``); anything else -- spaces,
    underscores, an empty string -- throws, which `u4.a` catches (None here)."""
    if not re.fullmatch(r"[+-]?\d+", text):
        return None
    return int(text)


def _l2i(value: int) -> int:
    """Java ``l2i``: the low 32 bits, as a signed int."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def _hexify(raw: bytes) -> str:
    """`s7.b(byte[])` -- the hex rendering used for short serial numbers."""
    return raw.hex().upper()


@dataclass
class ScanFields:
    """What `u4.a` recovers, in `BleDevice` constructor order."""

    manufacturer_code: int = 0
    project_code: int = 0
    version_type: str = "V"
    version_code: int = 0
    serial_number: str = ""
    bind_info: int = -1
    port_version: int = 0

    @property
    def version_name(self) -> str:
        """`BleDevice.getVersionName()` -- and the same string `x2` builds."""
        return f"{self.version_type}{self.version_code:04d}"

    @property
    def product_name(self) -> str | None:
        """The synthesised name, used only when the GAP name is absent
        (`u4.a` 812-904: the first three characters of the SCRUBBED serial,
        which is what `serial_number` holds)."""
        return PRODUCT_NAMES.get(self.serial_number[:3]) if len(self.serial_number) >= 3 else None


def parse_manufacturer_data(data: bytes, manufacturer_code: int = 0) -> ScanFields:
    """Port of `u4.a(ScanResult)`'s manufacturer-data walk.

    Written from the bytecode, not from `build_manufacturer_data`, so that a
    round-trip test is a check of two independently written artifacts rather
    than of one function against itself.

    The shape is a short chain of length-prefixed fields, not a general TLV:

        [0]                 u8   len0
        [1 .. len0]         projectCode, read as readInt(len0*8) -- ONLY when
                            len0 == 2 (bytecode 277-298). Otherwise the cursor
                            stays at 1 and this field is skipped entirely.
        [1+len0]            u8   len1            (read at 300-305)
        [4]                 char versionType     (331-344; cursor is len0+2)
        [5 .. 5+len1-2]     uint versionCode, readInt((len1-1)*8) (365-381)
        [4+len1]            u8   len2            (383-388)
        [5+len1 .. ]        serial number, len2 bytes (415-424)

    then one of three branches (427-589), and finally a bindInfo read (655-693)
    and the projectCode override (699-719).

    Raises ValueError where the SDK would silently give up, so a malformed
    advertisement is loud here instead of yielding a half-populated device.
    """
    raw = bytes(data)
    out = ScanFields(manufacturer_code=manufacturer_code)
    if not raw:
        raise ValueError("empty manufacturer data")

    length = raw[0]
    cursor = 1
    end = cursor + length
    if len(raw) <= end:                                      # 263-274
        raise ValueError(f"manufacturer data too short for field 0: {len(raw)}")

    if length == 2:                                          # 277-280
        width = _READ_WIDTHS.get(length)
        if width is None:
            raise ValueError(f"unsupported projectCode width: {length}")
        out.project_code = int.from_bytes(raw[1 : 1 + length], "little")
        length = raw[end]                                    # 300-305: len1
        cursor = 2 + 2                                       # 307-316: len0 + 2

    version_at = cursor + 1                                  # 318-326
    if len(raw) <= version_at:
        raise ValueError(f"manufacturer data too short for versionType: {len(raw)}")
    out.version_type = chr(raw[cursor])                      # 331-344

    code_width = length - 1                                  # 345-349
    code_end = version_at + code_width                       # 351-360
    if len(raw) <= code_end:
        raise ValueError(f"manufacturer data too short for versionCode: {len(raw)}")
    if _READ_WIDTHS.get(code_width) is None:
        raise ValueError(f"unsupported versionCode width: {code_width} bytes")
    out.version_code = _l2i(int.from_bytes(raw[version_at:code_end], "little"))   # 365-381: readInt, l2i

    serial_len = raw[code_end]                               # 383-388
    serial_at = code_end + 1                                 # 390
    serial_end = serial_at + serial_len                      # 393-402
    if len(raw) <= serial_end:
        raise ValueError(f"manufacturer data too short for the serial number: {len(raw)}")
    serial = raw[serial_at:serial_end]

    def first_nonzero_text(buf: bytes) -> str:
        # 537-573 / 595-631: scan for the first non-zero byte; the whole buffer
        # is then decoded, not the tail from that index.
        return next((buf.decode("ascii", "replace") for b in buf if b), "")

    if out.project_code in CURSOR_PORT_VERSION_PROJECTS:      # 427-443 -> 592
        out.serial_number = first_nonzero_text(serial)
        out.port_version = _u16(raw, serial_end)              # 634-644
        gate_at, gate_len = len(raw) - 1, 1                   # 646-652 -> 527
    elif serial_len < SHORT_SERIAL_LEN:                       # 446-450
        out.serial_number = _hexify(serial)                   # 453-460: HEX, not ASCII
        offset = (
            FIXED_18_OFFSET if out.project_code in FIXED_18_PROJECTS else FIXED_15_OFFSET
        )                                                     # 462-518
        out.port_version = _u16(raw, offset)
        gate_at, gate_len = len(raw) - 1, 1                   # 521-529
    else:                                                     # 534
        out.serial_number = first_nonzero_text(serial)
        # NOTE: this branch never assigns portVersion, so it stays 0 -- which
        # means a device with a >= 10-byte serial is ALWAYS treated as
        # cleartext (portVersion 0 < 20) by this client.
        gate_len = raw[serial_end]                            # 576-581
        gate_at = serial_end + 1                              # 583-587

    if len(raw) >= gate_at + gate_len:                        # 655-663
        if gate_at >= 1 and raw[gate_at - 1] == 1:            # 666-680
            out.bind_info = raw[gate_at]                      # 683-693

    if len(out.serial_number) >= 3:                           # 699-719: the UNSCRUBBED serial
        parsed = _java_parse_int(out.serial_number[:3])
        if parsed is not None:     # a throw goes to handler 250 -> 795: the u16 code stays
            out.project_code = parsed
    out.serial_number = scrub_serial(out.serial_number)       # 795-805; BleDevice gets this
    return out


def _u16(raw: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(raw):
        raise ValueError(f"u16 read at {offset} out of range for {len(raw)} bytes")
    return unpack_from("<H", raw, offset)[0]


def build_manufacturer_data(
    project_code: int = 881,
    version_type: str = "V",
    version_code: int = 1,
    serial: bytes = b"\x88\x10\x00\x00\x01",
    port_version: int = 7,
    bind_info: int = 0,
) -> bytes:
    """Build manufacturer data that `u4.a` parses into the requested fields.

    Targets the short-serial branch, because it is the ONLY branch that both
    sets `portVersion` and works for the Plaud project codes: the long-serial
    branch (serial >= 10 bytes) never assigns portVersion at all.

    The layout is forced almost entirely by the parser's fixed offsets. With a
    3-byte versionCode (which is what `versionName`'s "%04d" rendering and
    `l3`'s u24 version field both imply), the serial must land so that
    portVersion sits at exactly offset 18 for project codes 888/881, or 15 for
    anything else:

        [0]      02                 len0
        [1..2]   u16le projectCode
        [3]      04                 len1 -> versionCode is (4-1) = 3 bytes
        [4]      versionType char
        [5..7]   u24le versionCode
        [8]      len2               serial length, < 10
        [9..]    serial bytes
        [pad]    zero padding up to the fixed portVersion offset
        [15|18]  u16le portVersion
        [n-2]    01                 the gate byte bindInfo is read behind
        [n-1]    bindInfo

    Note the consequence of the short-serial branch: the serial is rendered as
    HEX, and the SDK then OVERWRITES projectCode with `int(serial_text[:3])`.
    The default serial `88 10 00 00 01` hexes to "8810000001", whose first
    three characters are "881" -- so the recovered project code and the
    synthesised product name ("Plaud Note Pro") stay coherent with the u16 at
    offset 1. Choosing a serial at random breaks that coherence, which is a
    property of the SDK, not of this builder.

    WHICH BRANCH REAL HARDWARE TAKES IS UNKNOWN. Of the parser's three
    branches only two assign portVersion, and no capture exists to say which
    one a real Plaud device lands in. This builder targets the one that is
    both reachable for the Plaud project codes and capable of declaring a
    portVersion.
    """
    if not 0 <= project_code <= 0xFFFF:
        raise ValueError(f"project_code out of u16 range: {project_code}")
    if project_code in CURSOR_PORT_VERSION_PROJECTS:
        raise ValueError(
            f"project_code {project_code} takes u4.a's cursor branch (427-443 -> 592): the serial is read as "
            "text and portVersion from the u16 right after it, not from the fixed offset this builder writes"
        )
    if len(version_type) != 1:
        raise ValueError("version_type is a single character")
    if not 0 <= version_code <= 0xFFFFFF:
        raise ValueError(f"version_code out of u24 range: {version_code}")
    if len(serial) >= SHORT_SERIAL_LEN:
        raise ValueError(
            f"serial must be shorter than {SHORT_SERIAL_LEN} bytes: the long-serial "
            "branch of u4.a never assigns portVersion, so the device would be read "
            "as portVersion 0"
        )
    if not 0 <= port_version <= 0xFFFF:
        raise ValueError(f"port_version out of u16 range: {port_version}")

    pv_offset = FIXED_18_OFFSET if project_code in FIXED_18_PROJECTS else FIXED_15_OFFSET
    body = bytearray()
    body += bytes([2])
    body += pack("<H", project_code)
    body += bytes([4])
    body += version_type.encode("ascii")
    body += version_code.to_bytes(3, "little")
    body += bytes([len(serial)])
    body += serial
    if len(body) > pv_offset:
        raise ValueError(
            f"serial of {len(serial)} bytes pushes the layout past the fixed "
            f"portVersion offset {pv_offset}"
        )
    body += bytes(pv_offset - len(body))          # zero padding
    body += pack("<H", port_version)
    body += bytes([1])                            # the gate byte
    body += bytes([bind_info & 0xFF])
    return bytes(body)
