"""V1: a central scans, discovers the emulator, and completes a control session.

Every other test here starts from an already-established connection, which skips
the part the real client actually does first: scan, parse the manufacturer
specific data, decide whether the device is interesting, and only then connect.

This test closes that gap over Bumble's virtual link:

  emulator advertises  ->  central scans  ->  the advertisement is parsed with
  the SDK's own rules  ->  connect  ->  MTU exchange (which is what gates
  service discovery in the real client)  ->  discovery  ->  subscribe  ->
  a full sequence of control exchanges  ->  disconnect

It is the closest rehearsal of V3 (real SDK against the emulator) that is
possible without a radio, and it is what makes the V1 claim falsifiable rather
than assumed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from bumble.core import AdvertisingData
from bumble.device import Peer
from bumble.testing.test_utils import TwoDevices

from support import SDK_REQUESTED_MTU, discover, load_fixture, role_uuids

from plaudsim.advertising import parse_manufacturer_data
from plaudsim.filesync import parse_file_data_frame, parse_sync_head
from plaudsim.profile import (
    ENCRYPTED_PORT_VERSION,
    PlaudBatteryState,
    PlaudDeviceState,
    PlaudPeripheral,
    PlaudStorageState,
    PlaudSyncTimeState,
)

SERVICE_UUID, DATA_UUID, COMMAND_UUID = role_uuids(load_fixture("post-bind-get-storage.json"))
FILE_BYTES = bytes(range(64))
TABLE = [{"session_id": 0x12345678, "file_size": 64, "scene": 2, "attribute": 1}]


async def advertising_emulator() -> tuple[TwoDevices, PlaudPeripheral]:
    devices = TwoDevices()
    peripheral = PlaudPeripheral(
        devices[1],
        state=PlaudDeviceState(state=0x1001, key_state=1, scene=4, session_id=0x12345678),
        storage=PlaudStorageState(free=8589934592, total=17179869184, duration=36000),
        synctime=PlaudSyncTimeState(stamp=1700000000),
        battery=PlaudBatteryState(charging=True, level=87),
        file_bytes=FILE_BYTES,
        file_table=[dict(e) for e in TABLE],
        tail_crc=0xBEEF,
    )
    peripheral.install()
    for device in devices.devices:
        await device.power_on()
    await peripheral.advertise()
    return devices, peripheral


async def scan_for_plaud(central) -> tuple[object, dict]:
    """Scan until an advertisement parses as a Plaud device.

    Deliberately does NOT filter by service UUID, because the real SDK does not
    either: it hands `startScan` an empty ScanFilter list and filters in
    software on the manufacturer data.
    """
    found: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_advertisement(advertisement) -> None:
        if found.done():
            return
        blobs = advertisement.data.get(
            AdvertisingData.MANUFACTURER_SPECIFIC_DATA, raw=True
        )
        if not blobs:
            return
        blob = blobs if isinstance(blobs, (bytes, bytearray)) else blobs[0]
        try:
            fields = parse_manufacturer_data(bytes(blob))
        except ValueError:
            return                      # not one of ours; the SDK drops it too
        if not fields.serial_number:
            return                      # the SDK drops results with an empty serial
        found.set_result((advertisement, fields))

    central.on(central.EVENT_ADVERTISEMENT, on_advertisement)
    await central.start_scanning()
    try:
        return await asyncio.wait_for(found, timeout=10)
    finally:
        await central.stop_scanning()


@pytest.mark.asyncio
async def test_scan_discover_connect_and_run_a_control_session() -> None:
    devices, peripheral = await advertising_emulator()
    central = devices[0]

    # --- scan -------------------------------------------------------------
    advertisement, fields = await scan_for_plaud(central)
    assert fields.port_version == peripheral.port_version
    assert fields.port_version < ENCRYPTED_PORT_VERSION, (
        "a device advertising portVersion >= 20 promises an encrypted channel"
    )
    assert fields.serial_number == "8810000001"
    assert fields.project_code == 881
    assert fields.product_name == "Plaud Note Pro"
    assert fields.version_name == "V0001"

    # --- connect, then MTU, then discovery (the SDK's order) ---------------
    connection = await central.connect(advertisement.address)
    peer = Peer(connection)
    await peer.request_mtu(SDK_REQUESTED_MTU)
    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)

    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    # --- a full control session -------------------------------------------
    await peer.write_value(command, b"\x01\x03\x00", with_response=True)   # getState
    await peer.write_value(command, b"\x01\x09\x00", with_response=True)   # battStatus
    await peer.write_value(command, b"\x01\x06\x00", with_response=True)   # getStorage
    await peer.write_value(
        command, PlaudSyncTimeState(stamp=1893456000, tz_hours=5, tz_mins=30).encode_request(),
        with_response=True,
    )                                                                       # syncTime
    from struct import pack

    await peer.write_value(
        command, b"\x01\x1a\x00" + pack("<II", 0x655A1B00, 0) + b"\x00", with_response=True
    )                                                                       # getFileList
    await peer.write_value(
        command, b"\x01\x1c\x00" + pack("<III", 0x12345678, 0, 0), with_response=True
    )                                                                       # syncFile

    opcodes = [int.from_bytes(r[1:3], "little") if r[0] == 1 else None for r in responses]
    assert opcodes[:5] == [3, 9, 6, 4, 26], f"control answers out of order: {opcodes}"

    # getState
    assert len(responses[0]) == 18
    assert int.from_bytes(responses[0][3:7], "little") == 0x1001
    # battStatus
    assert responses[1] == bytes([0x01, 0x09, 0x00, 0x01, 87])
    # getStorage: 27 bytes in ONE notification -- only possible after the MTU exchange
    assert len(responses[2]) == 27
    assert int.from_bytes(responses[2][3:11], "little") == 8589934592
    # syncTime answered for a timestamp the emulator has never seen before
    assert len(responses[3]) == 9

    # the file transfer: HEAD, DATA..., EMPTY_PACKAGE, TAIL, contiguous and complete
    # (R7-S13: the genuine client completes on the EMPTY_PACKAGE sentinel)
    transfer = responses[5:]
    assert parse_sync_head(transfer[0]) == {"session_id": 0x12345678, "status": 0}
    assert transfer[-2][5:9] == b"\xff\xff\xff\xff"
    cursor, body = 0, b""
    for frame in transfer[1:-2]:
        parsed = parse_file_data_frame(frame, port_version=peripheral.port_version)
        assert parsed["session_id"] == 0x12345678
        assert parsed["offset"] == cursor
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    assert body == FILE_BYTES
    assert transfer[-1][:3] == b"\x01\x1d\x00"

    # --- the handshake boundary still holds -------------------------------
    from plaudsim.profile import PlaudLifecycle

    assert peripheral.lifecycle not in (PlaudLifecycle.BOUND, PlaudLifecycle.READY)

    await connection.disconnect()


@pytest.mark.asyncio
async def test_advertisement_carries_the_service_uuid_and_a_name() -> None:
    devices, peripheral = await advertising_emulator()
    advertisement, _ = await scan_for_plaud(devices[0])
    name = advertisement.data.get(AdvertisingData.COMPLETE_LOCAL_NAME)
    assert name == "Plaud Note Pro"
    uuids = advertisement.data.get(
        AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, raw=True
    )
    assert uuids is not None, "advertise the service so ordinary BLE tooling finds us"


@pytest.mark.asyncio
async def test_full_sdk_connect_sequence_reaches_bound_and_pulls_a_file() -> None:
    """Drive the emulator through the Android SDK's OWN connect state machine.

    The stage names are `com.plaud.sdk.proto.q`'s constants K..W, and the order
    is the one `q.b0()` / `q.a()` / `q.d0()` actually execute for a device that
    advertises portVersion 7:

        start -> gatt_connect -> set_notify -> set_data_notify
              -> first_handshake            (portVersion < 20, so NO pre-handshake)
              -> handshake_get_ssn
              -> battery  (portVersion >= 5 gates connect success on it)
              -> sync_time  -> bleBind fires here, not at the handshake
              -> getState, getStorage, file list, syncFile

    This is the closest rehearsal of V3 that is possible without running the
    real SDK: every request below is the byte sequence the SDK emits, and every
    response is checked with a parser written from the SDK's own layouts.
    """
    from struct import pack

    from plaudsim.handshake import parse_l3, parse_x2
    from plaudsim.profile import PlaudLifecycle

    devices, peripheral = await advertising_emulator()
    central = devices[0]

    advertisement, fields = await scan_for_plaud(central)            # start
    assert fields.port_version < ENCRYPTED_PORT_VERSION
    connection = await central.connect(advertisement.address)        # gatt_connect
    peer = Peer(connection)
    await peer.request_mtu(SDK_REQUESTED_MTU)
    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True) # set_data_notify

    token = "0123456789abcdef"
    k3 = b"\x01\x01\x00" + bytes([0x02, 0x00, 0x00]) + token.encode("ascii")
    await peer.write_value(command, k3, with_response=True)          # first_handshake
    handshake = parse_l3(responses[-1])
    assert handshake["status"] == 0
    assert handshake["port_version"] == peripheral.port_version
    assert peripheral.lifecycle == PlaudLifecycle.HANDSHAKED

    await peer.write_value(command, b"\x01\x02\x00", with_response=True)   # handshake_get_ssn
    ssn = parse_x2(responses[-1])
    # q.b(ssn) -> checkSn compares this against the ADVERTISED serial.
    assert ssn["ssn"] == fields.serial_number
    assert ssn["version_name"] == fields.version_name

    await peer.write_value(command, b"\x01\x09\x00", with_response=True)   # battery
    assert responses[-1][3:] == bytes([0x01, 87])

    await peer.write_value(
        command, PlaudSyncTimeState(stamp=1893456000, tz_hours=-3, tz_mins=-30).encode_request(),
        with_response=True,
    )                                                                      # sync_time
    assert peripheral.lifecycle == PlaudLifecycle.BOUND, "bleBind fires off syncTime"

    # --- post-bind, the part the money claim is about ---------------------
    await peer.write_value(command, b"\x01\x03\x00", with_response=True)
    assert len(responses[-1]) == 18
    await peer.write_value(command, b"\x01\x06\x00", with_response=True)
    assert len(responses[-1]) == 27

    await peer.write_value(
        command, b"\x01\x1a\x00" + pack("<II", 0x655A1B00, 0) + b"\x00", with_response=True
    )
    from plaudsim.filesync import parse_file_list_frame

    listing = parse_file_list_frame(responses[-1], port_version=peripheral.port_version)
    assert listing["totals"] == 1
    session = listing["entries"][0]["session_id"]

    n = len(responses)
    await peer.write_value(
        command, b"\x01\x1c\x00" + pack("<III", session, 0, 0), with_response=True
    )
    transfer = responses[n:]
    assert parse_sync_head(transfer[0])["status"] == 0
    assert transfer[-2][5:9] == b"\xff\xff\xff\xff"  # EMPTY_PACKAGE sentinel (R7-S13)
    body = b""
    cursor = 0
    for frame in transfer[1:-2]:
        parsed = parse_file_data_frame(frame, port_version=peripheral.port_version)
        assert parsed["offset"] == cursor
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    assert body == FILE_BYTES, "a complete recording, pulled end to end"
    assert transfer[-1][:3] == b"\x01\x1d\x00"

    assert peripheral.lifecycle_log == [
        "gatt_ready", "connected", "handshaked", "bound",
    ]
    await connection.disconnect()
