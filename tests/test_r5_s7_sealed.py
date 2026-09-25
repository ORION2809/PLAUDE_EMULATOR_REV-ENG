"""R5-S7: synthetic modern sealed transport + GetState round-trip.

All key material here is the deterministic synthetic fixture from
plaudsim.sealed (SYNTHETIC_J/K/L) -- no Plaud credentials, no backend, no
live handshake. The fixture stands in for channel state *after* secret
establishment. Legacy tests must keep passing unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from cryptography.exceptions import InvalidTag

from plaudsim.profile import PlaudDeviceState
from plaudsim.sealed import (
    SYNTHETIC_J,
    SYNTHETIC_K,
    SYNTHETIC_L,
    SYNTHETIC_PORT_VERSION,
    SealedSession,
    open_raw,
    seal_raw,
    sealed_getstate_roundtrip,
)

# RFC 8439 section 2.8.2 AEAD_CHACHA20_POLY1305 test vector (external,
# published; proves our key/nonce/aad/plaintext wiring, not our byte order).
RFC_KEY = bytes.fromhex(
    "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
)
RFC_NONCE = bytes.fromhex("070000004041424344454647")
RFC_AAD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
RFC_PT = (
    b"Ladies and Gentlemen of the class of '99: If I could offer you "
    b"only one tip for the future, sunscreen would be it."
)
RFC_CT = (
    "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
    "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
    "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
    "3ff4def08e4b7a9de576d26586cec64b6116"
)
RFC_TAG = "1ae10b594f09e26a7e902ecbd0600691"

SEALED_STATE = PlaudDeviceState(
    state=0x1003,
    privacy_enabled=False,
    key_state=0,
    usb_state=False,
    scene=5,
    session_id=0x01020304,
    find_my_state=0,
    unnamed_16=0,
    unnamed_17=0,
)


def test_fixture_is_deterministic_and_clearly_synthetic() -> None:
    assert len(SYNTHETIC_J) == 32
    assert len(SYNTHETIC_K) == 12
    assert len(SYNTHETIC_L) == 12
    assert SYNTHETIC_J.isascii() and SYNTHETIC_K.isascii() and SYNTHETIC_L.isascii()
    assert SYNTHETIC_J.startswith(b"SYNTHETIC")
    assert SYNTHETIC_PORT_VERSION >= 20
    again = SealedSession()
    assert (again.key, again.nonce, again.aad) == (SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)


def test_aead_wiring_matches_rfc8439_vector() -> None:
    wire = seal_raw(RFC_KEY, RFC_NONCE, RFC_AAD, RFC_PT)
    assert wire.hex() == RFC_CT + RFC_TAG
    assert open_raw(RFC_KEY, RFC_NONCE, RFC_AAD, wire) == RFC_PT


def test_cross_library_vector() -> None:
    ChaCha20_Poly1305 = pytest.importorskip("Crypto.Cipher.ChaCha20_Poly1305")
    cipher = ChaCha20_Poly1305.new(key=RFC_KEY, nonce=RFC_NONCE)
    cipher.update(RFC_AAD)
    ct, tag = cipher.encrypt_and_digest(RFC_PT)
    assert (ct + tag).hex() == RFC_CT + RFC_TAG


def test_first_sealed_frame_is_seq_2() -> None:
    s = SealedSession()
    assert (s.tx_seq, s.rx_seq) == (1, -1)
    wire = s.seal(b"\x01\x03\x00")
    assert s.tx_seq == 2
    assert open_raw(s.key, s.nonce, s.aad, wire)[:4] == b"\x02\x00\x00\x00"


def test_sequences_increment_2_to_3() -> None:
    s = SealedSession()
    first = s.seal(b"\x01\x03\x00")
    second = s.seal(b"\x01\x03\x00")
    assert first != second  # same nonce, different seq => different keystream input
    assert open_raw(s.key, s.nonce, s.aad, first)[:4] == b"\x02\x00\x00\x00"
    assert open_raw(s.key, s.nonce, s.aad, second)[:4] == b"\x03\x00\x00\x00"


def test_retry_does_not_increment_or_reseal() -> None:
    s = SealedSession()
    wire = s.seal(b"\x01\x03\x00")
    calls = s.seal_calls
    resend = bytes(wire)  # retry is a byte copy; no seal call happens
    assert resend == wire
    assert s.seal_calls == calls
    assert s.tx_seq == 2
    assert s.open(resend) == b"\x01\x03\x00"


def test_rx_duplicate_is_silently_dropped() -> None:
    s = SealedSession()
    wire = s.seal(b"\x01\x03\x00")
    assert s.open(wire) == b"\x01\x03\x00"
    assert s.rx_seq == 2
    assert s.open(bytes(wire)) is None
    assert s.rx_seq == 2


def test_rx_stale_is_silently_dropped() -> None:
    s = SealedSession()
    older = s.seal(b"\x01\x03\x00")
    newer = s.seal(b"\x01\x06\x00")
    assert s.open(newer) == b"\x01\x06\x00"
    assert s.open(older) is None
    assert s.rx_seq == 3


def test_rx_forward_is_accepted() -> None:
    s = SealedSession()
    first = s.seal(b"\x01\x03\x00")
    second = s.seal(b"\x01\x06\x00")
    assert s.open(first) == b"\x01\x03\x00"
    assert s.open(second) == b"\x01\x06\x00"
    assert s.rx_seq == 3


def test_aead_failure_propagates_and_leaves_n() -> None:
    s = SealedSession()
    wire = bytearray(s.seal(b"\x01\x03\x00"))
    wire[6] ^= 0xFF
    with pytest.raises(InvalidTag):
        s.open(bytes(wire))
    assert s.rx_seq == -1


def test_short_plaintext_is_skipped_without_advancing_n() -> None:
    s = SealedSession()
    tiny = seal_raw(s.key, s.nonce, s.aad, b"\x01\x02")
    assert s.open(tiny) is None
    assert s.rx_seq == -1


def test_sequence_prefix_is_authenticated_not_adjacent() -> None:
    s = SealedSession()
    wire = s.seal(b"\x01\x03\x00")
    # open() takes the wire alone: no external sequence argument exists, so
    # the sequence can only come from inside the authenticated plaintext.
    inner = s.open(wire)
    assert inner == b"\x01\x03\x00"
    # Same frame at a different sequence seals to different bytes ...
    other = SealedSession()
    other.seal(b"\x01\x06\x00")  # consume seq 2
    wire3 = other.seal(b"\x01\x03\x00")  # same frame, seq 3
    assert wire3 != wire
    assert other.open(wire3) == b"\x01\x03\x00"
    # ... and flipping any ciphertext byte breaks authentication.
    tampered = bytearray(wire3)
    tampered[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        other.open(bytes(tampered))
    assert other.rx_seq == 3


def test_reset_restores_m1_n_1() -> None:
    s = SealedSession()
    s.open(s.seal(b"\x01\x03\x00"))
    s.seal(b"\x01\x06\x00")
    s.reset()
    assert (s.tx_seq, s.rx_seq) == (1, -1)
    assert open_raw(s.key, s.nonce, s.aad, s.seal(b"\x01\x03\x00"))[:4] == b"\x02\x00\x00\x00"
    assert s.open(s.seal(b"\x01\x03\x00")) is not None


def test_sealed_getstate_roundtrip_in_process() -> None:
    out = sealed_getstate_roundtrip(SealedSession(), SEALED_STATE)
    assert out["request_seq"] == 2
    assert out["response_seq"] == 3
    inner = bytes(out["response_inner"])
    assert inner[:3] == b"\x01\x03\x00"
    assert len(inner) == 18
    assert int.from_bytes(inner[3:7], "little") == SEALED_STATE.state
    assert inner[10] == SEALED_STATE.scene
    assert int.from_bytes(inner[11:15], "little") == SEALED_STATE.session_id


@pytest.mark.asyncio
async def test_sealed_getstate_over_bumble_gatt() -> None:
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices

    from sealed_support import SealedTestPeripheral

    session = SealedSession()
    devices = await TwoDevices.create_with_connection()
    peripheral = SealedTestPeripheral(devices[1], session, SEALED_STATE)
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(
        s for s in peer.services if str(s.uuid).lower().startswith("00001910")
    )
    data = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    command = next(
        c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb1")
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, session.seal(b"\x01\x03\x00"), with_response=True)

    assert len(responses) == 1
    inner = session.open(responses[0])
    assert inner is not None and inner[:3] == b"\x01\x03\x00" and len(inner) == 18
    assert int.from_bytes(inner[3:7], "little") == SEALED_STATE.state
    assert peripheral.requests == [b"\x01\x03\x00"]
    assert peripheral.drops == 0 and peripheral.errors == []


@pytest.mark.asyncio
async def test_sealed_peripheral_drops_replays_over_gatt() -> None:
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices

    from sealed_support import SealedTestPeripheral

    session = SealedSession()
    devices = await TwoDevices.create_with_connection()
    peripheral = SealedTestPeripheral(devices[1], session, SEALED_STATE)
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(
        s for s in peer.services if str(s.uuid).lower().startswith("00001910")
    )
    data = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    command = next(
        c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb1")
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    wire = session.seal(b"\x01\x03\x00")
    await peer.write_value(command, wire, with_response=True)
    await peer.write_value(command, bytes(wire), with_response=True)  # replay

    assert len(responses) == 1  # replay produced no second response
    assert peripheral.drops == 1
    assert session.rx_seq == 2
