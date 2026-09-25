"""R1a: the post-bind getState exchange over a Bumble virtual link.

Virtual link only: no radio, no hardware, no official SDK client.

Protocol expectations (UUIDs, request bytes, deterministic values) come from
docs/fixtures/post-bind-get-state.json -- NOT from plaudsim.profile. The test
fails if 2BB0/2BB1 are swapped in the emulator: discovery resolves roles by
fixture UUID and the property asserts in tests/support.py reject a swap.

The field OFFSETS asserted here are additionally checked against bytecode by
tests/test_evidence_conformance.py, so agreement between this file and the
emulator is not the only evidence for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk, load_fixture

from plaudsim.profile import PlaudDeviceState, PlaudPeripheral

FX = load_fixture("post-bind-get-state.json")
REQUEST_BYTES = bytes(FX["request"]["example_bytes"])
assert REQUEST_BYTES == b"\x01\x03\x00"

DET = FX["response"]["emulator_deterministic"]
EXPECTED_RESPONSE = bytes.fromhex(DET["example_hex"])

# Independent literal copy of the deterministic state. Compared against the
# fixture's example_hex on the wire: encoder/fixture disagreement fails.
R1A_STATE = PlaudDeviceState(
    state=0x1001,
    privacy_enabled=True,
    key_state=1,
    usb_state=False,
    scene=4,
    session_id=0x12345678,
    find_my_state=1,
    unnamed_16=7,
    unnamed_17=8,
)
assert R1A_STATE.state == DET["state"]["value"]
assert R1A_STATE.key_state == DET["key_state"]["value"]
assert R1A_STATE.session_id == DET["session_id"]["value"]


def check_response(response: bytes) -> None:
    assert response == EXPECTED_RESPONSE
    assert response[:3] == b"\x01\x03\x00"
    assert int.from_bytes(response[3:7], "little") == R1A_STATE.state
    assert response[7] == int(R1A_STATE.privacy_enabled)
    assert response[8] == R1A_STATE.key_state
    assert response[9] == int(R1A_STATE.usb_state)
    assert response[10] == R1A_STATE.scene
    assert int.from_bytes(response[11:15], "little") == R1A_STATE.session_id
    assert response[15:18] == bytes(
        [R1A_STATE.find_my_state, R1A_STATE.unnamed_16, R1A_STATE.unnamed_17]
    )
    assert len(response) == 18


async def run_exchange(prefer_notify: bool):
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, R1A_STATE), "post-bind-get-state.json"
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=prefer_notify)
    await peer.write_value(command, REQUEST_BYTES, with_response=True)
    return peripheral, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("prefer_notify", [True, False], ids=["notify", "indicate"])
async def test_get_state_exchange(prefer_notify: bool) -> None:
    peripheral, responses = await run_exchange(prefer_notify)

    assert responses == [EXPECTED_RESPONSE]
    assert peripheral.packet_log[0] == {"direction": "request", "bytes": "010300"}
    assert peripheral.packet_log[1] == {
        "direction": "response",
        "bytes": EXPECTED_RESPONSE.hex(),
    }
    check_response(responses[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad,reason",
    [
        (b"\x01\x63\x00", "unsupported_opcode"),            # opcode 99: no class claims it
        (b"\x02\x03\x00", "not_a_type_1_control_frame"),    # right opcode, wrong protocol type
        (b"\x01\x03", "not_a_type_1_control_frame"),        # truncated header
        (b"", "not_a_type_1_control_frame"),                # empty write
        (b"\x01\x03\x00\x00", "malformed_request"),         # getState takes no payload
    ],
)
async def test_bad_requests_get_no_response(bad: bytes, reason: str) -> None:
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, R1A_STATE), "post-bind-get-state.json"
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, bad, with_response=True)

    assert responses == []
    last = peripheral.packet_log[-1]
    assert last["direction"] == "error"
    assert last["reason"].startswith(reason)


@pytest.mark.asyncio
async def test_opcode_is_read_little_endian() -> None:
    """The opcode is a u16le at offset 1. `01 00 03` is opcode 768, not 3."""
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, R1A_STATE), "post-bind-get-state.json"
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, b"\x01\x00\x03", with_response=True)
    assert responses == []
    assert peripheral.packet_log[-1]["reason"].startswith("unsupported_opcode")
