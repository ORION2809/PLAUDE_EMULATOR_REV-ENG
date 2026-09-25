"""The generator's mono Ogg is served by the emulator's transfer machinery.

Pure state machine first (emulator/plaudsim/transfer.TransferSession), then
the real Bumble virtual GATT link brought up the way the Android SDK does.
The payload size is HARNESS_POLICY (transfer.py); the file bytes are the
generator's device/recording.ogg, reassembled byte-exact on the host side and
classified with the SDK's own two-test shape rule.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from struct import pack

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).parents[1]))

from support import connect_like_the_sdk

from plaudsim.audio import classify_recording
from plaudsim.filesync import parse_file_data_frame, parse_sync_head, parse_sync_tail
from plaudsim.profile import PlaudPeripheral
from plaudsim.transfer import MAX_DATA_PAYLOAD_SIZE, TransferSession

from generator.export import DEVICE_FILES
from generator.opus import decode_ogg_opus
from generator.testing import fixture_meeting

SESSION = 0x0655A1B0


@pytest.fixture(scope="module")
def ogg(tmp_path_factory) -> bytes:
    d, _ = fixture_meeting(tmp_path_factory.mktemp("gen"), "smoke", 3)
    return (d / DEVICE_FILES["ogg_opus"]).read_bytes()


def reassemble(frames: list[bytes]) -> bytes:
    body = bytearray()
    cursor = 0
    for frame in frames:
        parsed = parse_file_data_frame(frame, port_version=7)
        assert parsed["offset"] == cursor, "offset gap: the SDK would resend from its cursor"
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    return bytes(body)


def test_transfer_session_serves_the_ogg_byte_exact(ogg) -> None:
    session = TransferSession(file_bytes=ogg, payload_size=MAX_DATA_PAYLOAD_SIZE, crc=0x0BEE)
    session.start(SESSION, 0, 0)
    frames = session.frames()
    assert parse_sync_head(frames[0]) == {"session_id": SESSION, "status": 0}
    assert parse_sync_tail(frames[-1]) == {"session_id": SESSION, "crc": 0x0BEE}
    assert len(frames) == 3 + -(-len(ogg) // MAX_DATA_PAYLOAD_SIZE)  # HEAD + DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    body = reassemble(frames[1:-2])
    assert body == ogg
    assert classify_recording(body)["shape"] == "plain_ogg"


def test_transfer_session_resume_from_offset(ogg) -> None:
    session = TransferSession(file_bytes=ogg, payload_size=200)
    session.start(SESSION, 12345, 0)
    frames = session.frames()
    parsed = parse_file_data_frame(frames[1], port_version=7)
    assert parsed["offset"] == 12345
    tail = b"".join(parse_file_data_frame(f, port_version=7)["payload"] for f in frames[1:-2])  # skip EMPTY_PACKAGE (R7-S13)
    assert tail == ogg[12345:]


@pytest.mark.asyncio
async def test_ogg_transfers_over_the_virtual_gatt_link_and_decodes(ogg) -> None:
    # HARNESS_POLICY payload: within the u8 length field and under the ATT MTU
    # the SDK negotiates (255) minus the notification/frame overheads.
    payload = 240
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device,
            file_bytes=ogg,
            file_table=[{"session_id": SESSION, "file_size": len(ogg), "scene": 2, "attribute": 1}],
            tail_crc=0x1234,
            data_payload_size=payload,
        )
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, b"\x01\x1c\x00" + pack("<III", SESSION, 0, 0), with_response=True)
    expected = 3 + -(-len(ogg) // payload)  # HEAD + DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    for _ in range(2000):
        if len(responses) >= expected:
            break
        await asyncio.sleep(0.005)
    assert len(responses) == expected
    assert parse_sync_head(responses[0])["session_id"] == SESSION
    body = reassemble(responses[1:-2])  # skip the EMPTY_PACKAGE sentinel (R7-S13)
    assert body == ogg
    assert classify_recording(body)["shape"] == "plain_ogg"
    decoded, rate = decode_ogg_opus(body)
    assert abs(decoded.shape[1] / rate - 12.0) <= 0.020
