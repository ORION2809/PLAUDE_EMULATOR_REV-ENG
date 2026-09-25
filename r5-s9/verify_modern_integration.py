#!/usr/bin/env python3
"""R5-S9 verifier: synthetic modern handshake-to-filesync over Bumble GATT.

Standalone (no pytest): FE10 framing + FE11, FE20 clear, FE12/RSA/J-K-L,
sealed GetState/FileList/SyncFile with independent counters, replay and
tamper behavior. SYNTHETIC-ONLY material throughout. Exit 0 = verified.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "tests"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from plaudsim.filesync import pack_file_list_request, pack_sync_start  # noqa: E402
from plaudsim.handshake import (  # noqa: E402
    MARKER_FORCE_CLEAR,
    MARKER_PRE_HANDSHAKE,
    MARKER_RSA_MARKER,
    MARKER_SECRET,
    parse_marker_frame,
)
from plaudsim.profile import PlaudDeviceState  # noqa: E402
from plaudsim.sealed import SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SealedSession  # noqa: E402
from plaudsim.transfer import FileTable, TransferSession  # noqa: E402
from sealed_support import (  # noqa: E402
    SYNTHETIC_SN_SIGNATURE,
    ModernTestPeripheral,
    build_marker_frames,
    decrypt_secret_package,
)

ENTRIES = [
    {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
    {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
]
FILE_BYTES = bytes((i * 13 + 7) & 0xFF for i in range(96))


async def make_link():
    from bumble.device import Peer  # noqa: E402
    from bumble.testing.test_utils import TwoDevices  # noqa: E402

    devices = await TwoDevices.create_with_connection()
    peripheral = ModernTestPeripheral(
        devices[1],
        state=PlaudDeviceState(state=0x1003, scene=5, session_id=0x01020304),
        file_table=FileTable([dict(e) for e in ENTRIES], port_version=21),
        file_bytes=FILE_BYTES,
        tail_crc=0x5678,
        port_version=21,
    )
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
    raise AssertionError(f"only {len(responses)} responses, wanted {count}")


def of_marker(responses: list[bytes], marker: int) -> list[bytes]:
    return [r for r in responses if len(r) >= 4 and int.from_bytes(r[0:2], "little") == marker]


async def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # --- FE10 + FE11 ---------------------------------------------------------
    _, peripheral, peer, _, command, responses = await make_link()
    v4 = build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE)
    check("v4 chunks 100/100/47 with count 3", len(v4) == 3 and v4[0][2] == 3)
    for frame in v4:
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, 1)
    check("FE11 solicitation received",
          len(of_marker(responses, MARKER_RSA_MARKER)) == 1
          and parse_marker_frame(of_marker(responses, MARKER_RSA_MARKER)[0])["marker"] == MARKER_RSA_MARKER)
    check("device reassembled the exact sn payload", peripheral.sn_received == SYNTHETIC_SN_SIGNATURE)

    # --- FE20 clears a stale partial batch ------------------------------------
    _, peripheral2, peer2, _, command2, responses2 = await make_link()
    partial = build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE)
    await peer2.write_value(command2, partial[0], with_response=True)
    second = b"SECOND-PAYLOAD-" + b"0123456789ABCDEF" * 9
    for frame in build_marker_frames(MARKER_FORCE_CLEAR, second):
        await peer2.write_value(command2, frame, with_response=True)
    await wait_for(responses2, 1)
    check("FE20 batch completes without stale pollution", peripheral2.sn_received == second)

    # --- FE12 + RSA + J/K/L ----------------------------------------------------
    w4 = build_marker_frames(MARKER_SECRET, pem)
    for frame in w4:
        await peer.write_value(command, frame, with_response=True)
    await wait_for(responses, 1 + 3)
    fe12 = of_marker(responses, MARKER_SECRET)
    check("FE12 arrives as 100/100/56 chunks", len(fe12) == 3)
    parts = decrypt_secret_package(key, fe12)
    check("J/K/L match the synthetic fixture + self-check passed",
          (parts["chacha_key"], parts["chacha_nonce"], parts["chacha_aad"])
          == (SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    check("device entered sealed mode with reset counters",
          peripheral.sealed_ready and (peripheral.session.tx_seq, peripheral.session.rx_seq) == (1, -1))
    host = SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)

    # --- sealed filesync with independent counters -------------------------------
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

    getstate = await sealed_roundtrip(b"\x01\x03\x00", 1)
    check("sealed GetState round-trips (req seq 2, rsp seq 2, independent)",
          getstate[0][:3] == b"\x01\x03\x00" and host.tx_seq == 2
          and peripheral.session.rx_seq == 2 and peripheral.session.tx_seq == 2
          and host.rx_seq == 2)
    table = FileTable([dict(e) for e in ENTRIES], port_version=21)
    filelist = await sealed_roundtrip(pack_file_list_request(0x12345678, 0), 1)
    check("sealed FileList payload is byte-identical frozen R3",
          filelist == table.frames(0x12345678))
    transfer = TransferSession(file_bytes=FILE_BYTES, crc=0x5678, port_version=21)
    transfer.start(2001, 0, 0)
    expected = transfer.frames()
    sync = await sealed_roundtrip(pack_sync_start(2001, 0, 0), len(expected))
    check("sealed SyncFile payload is byte-identical frozen R3", sync == expected)
    payload = bytearray()
    for frame in sync[1:-1]:
        payload[int.from_bytes(frame[5:9], "little") :] = frame[10 : 10 + frame[9]]
    check("host reassembles the original synthetic file", bytes(payload) == FILE_BYTES)

    # --- replay + tamper ----------------------------------------------------------
    probe = host.seal(b"\x01\x03\x00")
    base = len(responses)
    await peer.write_value(command, bytes(probe), with_response=True)
    await wait_for(responses, base + 1)  # accepted once
    rx_before = peripheral.session.rx_seq
    n0 = len(responses)
    await peer.write_value(command, bytes(probe), with_response=True)  # redeliver
    await asyncio.sleep(0.3)
    check("duplicate sealed frame dropped with no new response and N pinned",
          len(responses) == n0 and peripheral.session.rx_seq == rx_before)
    reqs = len(peripheral.requests)
    rx_before_tamper = peripheral.session.rx_seq
    bad = bytearray(host.seal(b"\x01\x03\x00"))
    bad[10] ^= 0xFF
    await peer.write_value(command, bytes(bad), with_response=True)
    await asyncio.sleep(0.3)
    check("ciphertext tamper: no dispatch, N pinned",
          len(peripheral.requests) == reqs
          and peripheral.session.rx_seq == rx_before_tamper
          and any(e.startswith("aead_failure") for e in peripheral.errors))


asyncio.run(main())
print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
