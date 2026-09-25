"""R1b: the post-bind getStorage exchange, plus the opcode-9 battery exchange.

Request `01 06 00` (a3, protocol type 1, opcode 6, empty payload) on 2BB1;
response is the opcode-6 n6/StorageRsp (u64le free/total/duration) on 2BB0.

Transport: the 27-byte response exceeds the 20-byte default ATT payload, and
the control path performs NO reassembly -- every notification is parsed as one
complete logical message at literal offsets. The Android SDK therefore calls
`requestMtu(255)` the instant the link comes up and only calls
`discoverServices()` from `onMtuChanged`. The MTU exchange in these tests is
modelling that, not working around an emulator limitation.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk, load_fixture

from plaudsim.profile import PlaudBatteryState, PlaudPeripheral, PlaudStorageState

FX = load_fixture("post-bind-get-storage.json")
REQUEST_BYTES = bytes(FX["request"]["example_bytes"])
assert REQUEST_BYTES == b"\x01\x06\x00"

DET = FX["response"]["emulator_deterministic"]
EXPECTED_RESPONSE = bytes.fromhex(DET["example_hex"])
assert len(EXPECTED_RESPONSE) == 27

R1B_STATE = PlaudStorageState(free=8589934592, total=17179869184, duration=36000)
assert R1B_STATE.free == DET["free"]["value"]
assert R1B_STATE.total == DET["total"]["value"]
assert R1B_STATE.duration == DET["duration"]["value"]


def check_response(response: bytes) -> None:
    assert response == EXPECTED_RESPONSE
    assert response[:3] == b"\x01\x06\x00"
    assert int.from_bytes(response[3:11], "little") == R1B_STATE.free
    assert int.from_bytes(response[11:19], "little") == R1B_STATE.total
    assert int.from_bytes(response[19:27], "little") == R1B_STATE.duration


async def linked(**kwargs):
    return await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, storage=R1B_STATE, **kwargs)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prefer_notify", [True, False], ids=["notify", "indicate"])
async def test_get_storage_exchange(prefer_notify: bool) -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=prefer_notify)
    await peer.write_value(command, REQUEST_BYTES, with_response=True)

    assert responses == [EXPECTED_RESPONSE]
    assert peripheral.packet_log[0] == {"direction": "request", "bytes": "010600"}
    check_response(responses[0])


@pytest.mark.asyncio
async def test_storage_response_arrives_as_one_notification() -> None:
    """There is no application-layer reassembly anywhere on the control path,
    so a 27-byte response MUST arrive whole. If the emulator ever split it,
    the real SDK would parse the first fragment at literal offsets and get
    garbage rather than waiting for more."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, REQUEST_BYTES, with_response=True)
    assert len(responses) == 1
    assert len(responses[0]) == 27 > 20


@pytest.mark.asyncio
async def test_free_and_total_are_not_swapped() -> None:
    """n6's accessors are shuffled -- c() returns the field at offset 3 and d()
    the one at 11 -- so free/total is the easiest pair in the protocol to
    invert. The toString literal settles it: offset 3 is FREE."""
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, REQUEST_BYTES, with_response=True)
    free = int.from_bytes(responses[0][3:11], "little")
    total = int.from_bytes(responses[0][11:19], "little")
    assert free == 8589934592 and total == 17179869184
    assert free < total, "a swap would make 'free' larger than 'total'"


@pytest.mark.asyncio
async def test_battery_status_opcode_9_round_trip() -> None:
    """`q` gates connection success on this exchange whenever portVersion >= 5,
    so a device that answers the handshake but not opcode 9 is reported to the
    app as a failed connect."""
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device, battery=PlaudBatteryState(charging=True, level=87)
        )
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, b"\x01\x09\x00", with_response=True)

    assert len(responses) == 1
    assert responses[0] == bytes([0x01, 0x09, 0x00, 0x01, 87])
    assert responses[0][3] == 1, "charging is (u8@3 == 1)"
    assert responses[0][4] == 87, "level is u8@4"


@pytest.mark.asyncio
async def test_battery_can_be_pushed_unsolicited() -> None:
    """The device pushes opcode 9 on its own; the SDK has a push handler that
    updates the cache behind getChargingState()."""
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peripheral.push_battery(PlaudBatteryState(charging=False, level=42))
    await asyncio.sleep(0.05)  # let the virtual link deliver the notification
    assert responses == [bytes([0x01, 0x09, 0x00, 0x00, 42])]
    assert peripheral.battery.level == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [b"\x01\x06\x00\x00", b"\x01\x09\x00\xff", b"\x01\x07\x00", b"\x03\x06\x00"]
)
async def test_storage_and_battery_reject_malformed_requests(bad: bytes) -> None:
    devices, peripheral, peer, data, command = await linked()
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, bad, with_response=True)
    assert responses == []
    assert peripheral.packet_log[-1]["direction"] == "error"
