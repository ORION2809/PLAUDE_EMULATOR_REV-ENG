"""R5-S9: synthetic modern handshake-to-filesync integration over Bumble GATT.

SYNTHETIC-ONLY: snSignature, RSA keypair, secret package, J/K/L, tokens
are harness-generated. Proves the recovered FE10/FE20→FE11→FE12→J/K/L→
sealed-transport→filesync chain integrates; proves NOTHING about
real-device compatibility. Legacy/frozen behavior untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from plaudsim.filesync import pack_file_list_request, pack_sync_start
from plaudsim.handshake import (
    MARKER_FORCE_CLEAR,
    MARKER_PRE_HANDSHAKE,
    MARKER_RSA_MARKER,
    MARKER_SECRET,
    parse_marker_frame,
)
from plaudsim.profile import PlaudDeviceState
from plaudsim.sealed import (
    SYNTHETIC_J,
    SYNTHETIC_K,
    SYNTHETIC_L,
    SealedSession,
)
from plaudsim.transfer import FileTable, TransferSession
from sealed_support import (
    SYNTHETIC_SN_SIGNATURE,
    ModernTestPeripheral,
    build_marker_frames,
    decrypt_secret_package,
)

STATE = PlaudDeviceState(state=0x1003, scene=5, session_id=0x01020304)
ENTRIES = [
    {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
    {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
]
FILE_BYTES = bytes((i * 13 + 7) & 0xFF for i in range(96))
SESSION = 2001
CRC = 0x5678
STAMP = 0x12345678
PV = 21


@pytest.fixture(scope="module")
def rsa_key():
    # SYNTHETIC-ONLY keypair (runtime-generated; wire bytes vary per run by
    # PKCS#1 padding randomness, structure asserted instead).
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def host_pubkey_pem(rsa_key) -> bytes:
    return rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


async def make_link(peripheral_factory):
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices

    devices = await TwoDevices.create_with_connection()
    peripheral = peripheral_factory(devices[1])
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(s for s in peer.services if str(s.uuid).lower().startswith("00001910"))
    data = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    command = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb1"))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    return devices, peripheral, peer, data, command, responses


async def wait_for(responses: list[bytes], count: int, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if len(responses) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"only {len(responses)} responses after {timeout}s, wanted {count}")


def markers(responses: list[bytes], marker: int) -> list[bytes]:
    out = []
    for raw in responses:
        if len(raw) >= 4 and int.from_bytes(raw[0:2], "little") == marker:
            out.append(raw)
    return out


async def do_handshake(peer, command, responses, peripheral, rsa_key, marker=MARKER_PRE_HANDSHAKE):
    """Drive the synthetic pre-key handshake; return the keyed host session."""
    base = len(responses)
    v4 = build_marker_frames(marker, SYNTHETIC_SN_SIGNATURE)
    assert all(len(f) <= 104 for f in v4)  # 4-byte header + <=100 B chunk
    for frame in v4:
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, base + 1)
    fe11 = markers(responses, MARKER_RSA_MARKER)
    assert len(fe11) == 1
    assert parse_marker_frame(fe11[0])["marker"] == MARKER_RSA_MARKER

    w4 = build_marker_frames(MARKER_SECRET, host_pubkey_pem(rsa_key))
    for frame in w4:
        await peer.write_value(command, frame, with_response=True)
    first = None
    for _ in range(100):
        got = markers(responses, MARKER_SECRET)
        if got:
            first = got[0]
            break
        await asyncio.sleep(0.05)
    assert first is not None
    count = first[2]
    await wait_for(responses, len(responses) + (count - len(markers(responses, MARKER_SECRET))))
    parts = decrypt_secret_package(rsa_key, markers(responses, MARKER_SECRET))
    assert parts["chacha_key"] == SYNTHETIC_J
    assert parts["chacha_nonce"] == SYNTHETIC_K
    assert parts["chacha_aad"] == SYNTHETIC_L
    assert peripheral.sealed_ready is True
    return SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)


def modern_peripheral(device):
    return ModernTestPeripheral(
        device,
        state=STATE,
        file_table=FileTable([dict(e) for e in ENTRIES], port_version=PV),
        file_bytes=FILE_BYTES,
        tail_crc=CRC,
        port_version=PV,
    )


# --- pre-key handshake ---------------------------------------------------------


@pytest.mark.asyncio
async def test_fe10_framing_and_fe11(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    v4 = build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE)
    assert len(v4) == 3  # 250 B -> 100/100/50
    for frame in v4:
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, 1)
    # Device-side framing record: marker, count, ascending indices, <=100 B.
    assert len(peripheral.v4_frames) == 3
    assert [int(f["marker"]) for f in peripheral.v4_frames] == [MARKER_PRE_HANDSHAKE] * 3
    assert [int(f["count"]) for f in peripheral.v4_frames] == [3, 3, 3]
    assert [int(f["index"]) for f in peripheral.v4_frames] == [0, 1, 2]
    assert all(len(bytes(f["chunk"])) <= 100 for f in peripheral.v4_frames)
    assert peripheral.sn_received == SYNTHETIC_SN_SIGNATURE
    assert markers(responses, MARKER_RSA_MARKER) != []


@pytest.mark.asyncio
async def test_fe20_clears_stale_v4_chunks(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    partial = build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE)
    await peer.write_value(command, partial[0], with_response=True)  # 1 of 3
    second = b"SECOND-PAYLOAD-" + b"0123456789ABCDEF" * 9  # 159 B -> 2 chunks
    for frame in build_marker_frames(MARKER_FORCE_CLEAR, second):
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, 1)
    assert peripheral.sn_received == second  # stale FE10 chunk did not pollute
    assert [int(f["marker"]) for f in peripheral.v4_frames] == [
        MARKER_PRE_HANDSHAKE,
        MARKER_FORCE_CLEAR,
        MARKER_FORCE_CLEAR,
    ]


@pytest.mark.asyncio
async def test_fe12_reassembly_duplicate_and_jkl(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    for frame in build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE):
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, 1)
    w4 = build_marker_frames(MARKER_SECRET, host_pubkey_pem(rsa_key))
    base = len(responses)
    await peer.write_value(command, w4[0], with_response=True)
    await peer.write_value(command, w4[1], with_response=True)
    await peer.write_value(command, bytes(w4[1]), with_response=True)  # duplicate
    for frame in w4[2:]:
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, base + 3)  # 256-B RSA block -> 100/100/56
    assert peripheral.marker_drops >= 1
    parts = decrypt_secret_package(rsa_key, markers(responses, MARKER_SECRET))
    assert (parts["chacha_key"], parts["chacha_nonce"], parts["chacha_aad"]) == (
        SYNTHETIC_J,
        SYNTHETIC_K,
        SYNTHETIC_L,
    )
    assert peripheral.sealed_ready is True
    # Device session reset at install: first sealed response must be seq 2.
    assert peripheral.session.tx_seq == 1 and peripheral.session.rx_seq == -1


# --- sealed mode over the integrated peripheral ---------------------------------


@pytest.mark.asyncio
async def test_sealed_getstate_independent_counters(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    host = await do_handshake(peer, command, responses, peripheral, rsa_key)
    assert (host.tx_seq, host.rx_seq) == (1, -1)  # M reset at key install
    base = len(responses)
    await peer.write_value(command, host.seal(b"\x01\x03\x00"), with_response=True)
    await wait_for(responses, base + 1)
    inner = host.open(responses[-1])
    assert inner is not None and inner[:3] == b"\x01\x03\x00" and len(inner) == 18
    assert host.tx_seq == 2 and peripheral.session.rx_seq == 2  # req seq 2 accepted
    assert peripheral.session.tx_seq == 2 and host.rx_seq == 2  # rsp seq 2, independent


@pytest.mark.asyncio
async def test_full_handshake_to_filesync(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    host = await do_handshake(peer, command, responses, peripheral, rsa_key)

    async def sealed_roundtrip(plaintext: bytes, expected_new: int) -> list[bytes]:
        base = len(responses)
        await peer.write_value(command, host.seal(plaintext), with_response=True)
        await wait_for(responses, base + expected_new)
        out = []
        for wire in responses[base:]:
            inner = host.open(wire)
            assert inner is not None
            out.append(inner)
        return out

    assert (await sealed_roundtrip(b"\x01\x03\x00", 1))[0][:3] == b"\x01\x03\x00"
    filelist = await sealed_roundtrip(pack_file_list_request(STAMP, 0), 1)
    table = FileTable([dict(e) for e in ENTRIES], port_version=PV)
    assert filelist == table.frames(STAMP)  # byte-for-byte frozen R3 payload
    transfer = TransferSession(file_bytes=FILE_BYTES, crc=CRC, port_version=PV)
    transfer.start(SESSION, 0, 0)
    expected = transfer.frames()
    sync = await sealed_roundtrip(pack_sync_start(SESSION, 0, 0), len(expected))
    assert sync == expected  # HEAD + type-2 DATA + TAIL, R3 bytes unchanged
    assert sync[0][:3] == b"\x01\x1c\x00" and sync[-1][:3] == b"\x01\x1d\x00"
    assert sync[-1][7:9] == (CRC & 0xFFFF).to_bytes(2, "little")
    payload = bytearray()
    for frame in sync[1:-1]:
        assert frame[0] == 2 and int.from_bytes(frame[1:5], "little") == SESSION
        length = frame[9]
        payload[int.from_bytes(frame[5:9], "little") :] = frame[10 : 10 + length]
    assert bytes(payload) == FILE_BYTES
    assert peripheral.errors == []


# --- replay / forward / tamper through the integrated path ----------------------


@pytest.mark.asyncio
async def test_replay_forward_stale(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    host = await do_handshake(peer, command, responses, peripheral, rsa_key)
    first = host.seal(b"\x01\x03\x00")  # seq 2
    second = host.seal(pack_file_list_request(STAMP, 0))  # seq 3
    await peer.write_value(command, second, with_response=True)  # future first
    n_after_future = len(responses)
    assert n_after_future >= 1  # accepted per the N rule
    await peer.write_value(command, first, with_response=True)  # stale
    await peer.write_value(command, bytes(second), with_response=True)  # duplicate
    await asyncio.sleep(0.3)
    assert len(responses) == n_after_future  # both dropped, no new responses
    assert peripheral.session.rx_seq == 3
    assert peripheral.drops == 2


@pytest.mark.asyncio
async def test_tamper_before_dispatch(rsa_key) -> None:
    _, peripheral, peer, _, command, responses = await make_link(modern_peripheral)
    host = await do_handshake(peer, command, responses, peripheral, rsa_key)
    n_requests = len(peripheral.requests)
    bad = bytearray(host.seal(b"\x01\x03\x00"))
    bad[10] ^= 0xFF  # ciphertext byte
    await peer.write_value(command, bytes(bad), with_response=True)
    bad_seq = bytearray(host.seal(pack_file_list_request(STAMP, 0)))
    bad_seq[0] ^= 0x01  # authenticated sequence region
    await peer.write_value(command, bytes(bad_seq), with_response=True)
    await asyncio.sleep(0.3)
    assert responses == [r for r in responses if int.from_bytes(r[0:2], "little") in (MARKER_RSA_MARKER, MARKER_SECRET)]
    assert len(peripheral.requests) == n_requests  # dispatcher never ran
    assert sum(1 for e in peripheral.errors if e.startswith("aead_failure")) == 2
    assert peripheral.session.rx_seq == -1  # N unchanged
    with pytest.raises(InvalidTag):
        host.open(bad)  # same tamper class fails on any conforming open
