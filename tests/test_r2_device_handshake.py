"""R2 (device side): the emulator answers the LEGACY handshake, and a client binds.

The audit's most consequential finding about R2 is an asymmetry that earlier
work had collapsed into a single "blocked".

`q.b0()` branches on the ADVERTISED portVersion: `>= 20` runs the RSA
pre-handshake, below it goes straight to `q.a()` (first_handshake). `q.a()`'s
only precondition is that the token string is non-empty — no network call, no
key, no signature. On that path the handshake is: **the host sends a token, the
DEVICE returns a status.**

So:

* binding a client *we* wrote to *real Plaud hardware* is blocked — we cannot
  mint a token the device accepts, and the RSA/ChaCha path needs cloud-issued
  keys; but
* binding *Plaud's real client* to a *device we wrote* is not blocked, because
  accepting the token is the device's decision and we are the device.

Accepting any token is HARNESS POLICY and is labelled as such throughout. These
tests exercise both the accept and the refuse path so the boundary stays
explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk

from plaudsim.advertising import ScanFields
from plaudsim.handshake import (
    encode_l3,
    encode_x2,
    parse_handshake_request,
    parse_l3,
    parse_x2,
    token_width,
)
from plaudsim.profile import PlaudLifecycle, PlaudPeripheral, PlaudSyncTimeState

TOKEN16 = "0123456789abcdef"
TOKEN32 = "0123456789abcdef0123456789abcdef"


def k3(token: str, port_version: int = 7, stage: int = 0, agent: int = 0) -> bytes:
    """Build a k3 request exactly as the SDK's enPkg does."""
    width = token_width(port_version)
    padded = (token + "0" * width)[:width]
    body = b"\x01\x01\x00" + bytes([0x02, agent])
    if port_version >= 3:
        body += bytes([stage])
    return body + padded.encode("ascii")


def j3(token: str, dev_token: str = "", user_name: str = "", port_version: int = 7) -> bytes:
    """k3 body plus j3's always-present tail."""
    tail = dev_token.encode("ascii")[:8].ljust(8, b"\x00")
    name = user_name.encode("ascii")
    return k3(token, port_version, stage=1) + tail + bytes([len(name)]) + name


# --- pure codecs ---------------------------------------------------------


def test_k3_round_trip_at_both_token_widths() -> None:
    assert token_width(7) == 16 and token_width(9) == 32
    for pv, token in ((7, TOKEN16), (9, TOKEN32)):
        parsed = parse_handshake_request(k3(token, pv), pv)
        assert parsed["token"] == token
        assert parsed["is_second"] is False
        assert parsed["dev_token"] is None
    # A short token is right-padded with ASCII '0', not NUL.
    frame = k3("ab", 7)
    assert frame[6:] == b"ab" + b"0" * 14


def test_k3_below_port_version_3_has_no_stage_byte() -> None:
    """`if (this.d >= 3)` guards the stage byte, so on pv 1..2 the first and
    second handshake requests are byte-identical in their k3 portion."""
    assert len(k3(TOKEN16, port_version=2)) == len(k3(TOKEN16, port_version=7)) - 1
    parsed = parse_handshake_request(k3(TOKEN16, 2), 2)
    assert parsed["stage"] == 0
    assert parsed["token"] == TOKEN16


def test_j3_tail_is_always_present_in_practice() -> None:
    """j3's null short-circuit needs BOTH extra strings null; the shipped
    wrappers pass empty strings, so the 8-byte field and the length byte ship."""
    parsed = parse_handshake_request(j3(TOKEN16, "dev12345", "alice"), 7)
    assert parsed["is_second"] is True
    assert parsed["dev_token"] == "dev12345"
    assert parsed["user_name"] == "alice"
    empty = parse_handshake_request(j3(TOKEN16), 7)
    assert empty["dev_token"] == "" and empty["user_name"] == ""
    assert len(j3(TOKEN16)) == len(k3(TOKEN16)) + 9


@pytest.mark.parametrize(
    "bad",
    [
        b"\x01\x01\x00",                                  # header only
        b"\x01\x02\x00" + bytes(20),                      # opcode 2, not 1
        b"\x02\x01\x00" + bytes(20),                      # protocol type 2
        b"\x01\x01\x00\x03\x00\x00" + b"0" * 16,          # constant at [3] is not 0x02
        b"\x01\x01\x00\x02\x00\x00" + b"0" * 15,          # token one char short
    ],
)
def test_malformed_handshake_requests_are_rejected(bad: bytes) -> None:
    with pytest.raises(ValueError):
        parse_handshake_request(bad, 7)


def test_l3_round_trips_through_our_own_parser() -> None:
    frame = encode_l3(status=0, port_version=7, audio_channel=2, is_ogg_audio=True, version=1234)
    parsed = parse_l3(frame)
    assert parsed["status"] == 0
    assert parsed["port_version"] == 7
    assert parsed["audio_channel"] == 2
    assert parsed["is_ogg_audio"] is True
    assert parsed["version_type"] == "V"
    assert parsed["version"] == 1234, "little-endian u24 read from the frame END"
    assert frame[-3:] == (1234).to_bytes(3, "little")


def test_x2_round_trip_and_ssn_scan_bound() -> None:
    frame = encode_x2("8810000001", "V", 7)
    assert len(frame) == 63
    parsed = parse_x2(frame)
    assert parsed == {"ssn": "8810000001", "version_name": "V0007"}
    with pytest.raises(ValueError):
        encode_x2("x" * 56)      # would leave no room for a NUL before offset 59


# --- over the wire -------------------------------------------------------


async def linked(**kwargs):
    return await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device, synctime=PlaudSyncTimeState(stamp=1700000000), **kwargs
        )
    )


@pytest.mark.asyncio
async def test_handshake_then_synctime_reaches_bound() -> None:
    """Mirrors the SDK's own gate: `bleBind` fires off the syncTime success
    callback, not off the handshake, so BOUND advances there too."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED

    await peer.write_value(command, k3(TOKEN16), with_response=True)
    assert parse_l3(responses[0])["status"] == 0
    assert peripheral.lifecycle == PlaudLifecycle.HANDSHAKED
    assert peripheral.handshake_log[0]["token"] == TOKEN16

    await peer.write_value(command, b"\x01\x02\x00", with_response=True)   # getSsn
    assert parse_x2(responses[1])["ssn"] == peripheral.scan_fields.serial_number

    await peer.write_value(
        command, PlaudSyncTimeState(stamp=1893456000).encode_request(), with_response=True
    )
    assert peripheral.lifecycle == PlaudLifecycle.BOUND
    assert peripheral.lifecycle_log[-3:] == ["connected", "handshaked", "bound"]


@pytest.mark.asyncio
async def test_a_refusing_peripheral_never_leaves_connected() -> None:
    """The handshake boundary, asserted the strict way: with the policy off,
    no sequence of exchanges advances the lifecycle."""
    devices, peripheral, peer, data, command = await linked(accept_any_token=False)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    for raw in (
        k3(TOKEN16),
        j3(TOKEN16),
        b"\x01\x03\x00",
        PlaudSyncTimeState(stamp=1).encode_request(),
    ):
        await peer.write_value(command, raw, with_response=True)

    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED
    assert PlaudLifecycle.BOUND.value not in peripheral.lifecycle_log
    assert peripheral.packet_log[1]["direction"] == "error"


@pytest.mark.asyncio
async def test_nonzero_handshake_status_does_not_bind() -> None:
    """`l3.status != 0` becomes `s0$d.a(status)` on the client and fails the
    connect, so the emulator must not advance either."""
    devices, peripheral, peer, data, command = await linked(handshake_status=1)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, k3(TOKEN16), with_response=True)
    assert parse_l3(responses[0])["status"] == 1
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED

    await peer.write_value(
        command, PlaudSyncTimeState(stamp=1).encode_request(), with_response=True
    )
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED


@pytest.mark.asyncio
async def test_ssn_matches_the_advertised_serial() -> None:
    """`q.b(ssn)` feeds the response into `checkSn`, which compares it against
    the ADVERTISED serial number and raises SN_NOT_MATCH on a mismatch. So a
    device whose x2 disagrees with its own advertisement fails the connect."""
    fields = ScanFields(
        project_code=881, version_type="V", version_code=42,
        serial_number="8820000009", bind_info=0, port_version=7,
    )
    devices, peripheral, peer, data, command = await linked(scan_fields=fields)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, b"\x01\x02\x00", with_response=True)

    parsed = parse_x2(responses[0])
    assert parsed["ssn"] == fields.serial_number
    assert parsed["version_name"] == fields.version_name == "V0042"


@pytest.mark.asyncio
async def test_handshake_response_advertises_the_same_port_version() -> None:
    """`q` caches `l3.portVersion` into `q.m`, which then selects the file-list
    entry stride and the data-frame layout. If it disagreed with the advertised
    value the client would parse later frames with the wrong layout."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, k3(TOKEN16), with_response=True)
    assert parse_l3(responses[0])["port_version"] == peripheral.port_version
