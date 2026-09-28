"""One session across both transports: BLE opcode 10 starts the Wi-Fi device.

The pen side is `PlaudPeripheral` (BLE, over a Bumble TwoDevices link brought
up the way the Android SDK does it) with a `wifi_device_factory`; the phone
side is `tests/wifi_support.PhoneWifiServer` on loopback.

Evidence split (see docs/wifi-transport.md section 8):
  * BLE side -- opcode 10 i4/j4 and opcode 13 k0/l0 layouts are BYTECODE_PROVEN
    (ledger section 7; ALL.txt ranges at the constants in plaudsim/profile.py);
    "status 4 while streaming / while a Wi-Fi session is already open" is a
    CLAIM from the template app (SyncManager.kt:31-32, :142-148, :173-176).
  * Wi-Fi side -- the phone's server behaviour is BYTECODE_PROVEN
    (wifi_support.py cites it); the pen's is HARNESS_POLICY.
  * The hand-over itself is HARNESS_POLICY: real hardware raises a SoftAP and
    the phone joins it before the pen dials; here IP connectivity is assumed,
    the phone's server is already listening, and the pen dials a configured
    host:port as soon as it has answered opcode 10 with status 0. BLE stays up
    throughout (the template claims the pen drops BLE during Wi-Fi,
    SyncManager.kt:184; not modelled) so opcode 13 can close the session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from struct import unpack_from

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from support import connect_like_the_sdk  # noqa: E402
from wifi_support import PhoneWifiServer  # noqa: E402

from plaudsim.filesync import (  # noqa: E402
    OPCODE_FILE_LIST,
    pack_file_list_request,
    pack_stop_sync_request,
    pack_sync_start,
)
from plaudsim.profile import (  # noqa: E402
    WIFI_OPEN_STATUS_BUSY,
    PlaudPeripheral,
    encode_close_wifi_response,
    encode_open_wifi_response,
)
from plaudsim.transfer import FileListAccumulator  # noqa: E402
from plaudsim.wifi_device import WifiDeviceState, phone_dialer  # noqa: E402

pytestmark = pytest.mark.timeout(60)

OPEN_ON = bytes.fromhex("010a0001")
OPEN_OFF = bytes.fromhex("010a0000")
CLOSE = bytes.fromhex("010d00")
TOKEN = "SYNTHETIC-WIFI-TOKEN"
SID = 1700000000
FILE = bytes((i * 37 + 11) & 0xFF for i in range(6000))
TABLE = [{"session_id": SID, "file_size": len(FILE), "scene": 2, "attribute": 1}]
STAMP = 0x655A1B00
QUIET = dict(heartbeat_interval=0.05, idle_heartbeats_before_close=None, exit_timeout=None)


async def ble_link(factory):
    return await connect_like_the_sdk(
        lambda device: PlaudPeripheral.for_real_sdk(
            device, file_bytes=FILE, file_table=[dict(e) for e in TABLE], wifi_device_factory=factory
        )
    )


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_ble_session_opens_wifi_pulls_the_file_and_closes_it_over_ble() -> None:
    async with PhoneWifiServer(token=TOKEN) as phone:
        factory = phone_dialer("127.0.0.1", phone.port, chunk_size=1000, **QUIET)
        devices, peripheral, peer, data, command = await ble_link(factory)
        responses: list[bytes] = []
        await peer.subscribe(data, responses.append, prefer_notify=True)

        # 1. BLE: the file list (opcode 26).
        await peer.write_value(command, pack_file_list_request(STAMP, 0), with_response=True)
        acc = FileListAccumulator(request_stamp=STAMP, port_version=7)
        for frame in responses:
            if frame[0] == 1 and unpack_from("<H", frame, 1)[0] == OPCODE_FILE_LIST:
                acc.ingest_frame(frame)
        assert acc.complete and [(e["session_id"], e["file_size"]) for e in acc.entries] == [(SID, len(FILE))]

        # 2. BLE: a paced transfer is running -> opcode 10 is refused (CLAIM: status 4) and
        #    no Wi-Fi device is started.
        await peer.write_value(command, pack_sync_start(SID, 0, 0), with_response=True)
        await asyncio.sleep(0.03)
        assert peripheral.transfer_streaming
        await peer.write_value(command, OPEN_ON, with_response=True)
        assert peripheral.wifi_device is None

        # 3. The template's order: stopSync, then openWiFi -> status 0 + passphrase; the pen dials.
        await peer.write_value(command, pack_stop_sync_request(), with_response=True)
        await peer.write_value(command, OPEN_ON, with_response=True)
        await asyncio.sleep(0.02)
        j4 = [r for r in responses if r[:3] == b"\x01\x0a\x00"]
        assert j4 == [encode_open_wifi_response(WIFI_OPEN_STATUS_BUSY), encode_open_wifi_response(0, "10000001")]
        dev = peripheral.wifi_device
        assert dev is not None and peripheral.wifi_active
        await phone.wait_ready(5)
        assert phone.hellos[0].sn == peripheral.scan_fields.serial_number
        assert phone.hellos[0].p_ver == peripheral.port_version

        # 4. Wi-Fi: the same file table and the same bytes.
        listing = await phone.get_file_list()
        assert [(r.session_id, r.file_size, r.scene) for r in listing.records] == [(SID, len(FILE), 2)]
        dl = await phone.download(SID, len(FILE))
        assert dl.data == FILE and len(dl.chunks) == 6 and dl.chunks[-1].last

        # 5. BLE opcode 13 closes the Wi-Fi session: the pen announces WifiClose.
        await peer.write_value(command, CLOSE, with_response=True)
        await phone.wait_device_closed(5)
        await dev.wait_closed(5)
        await asyncio.sleep(0.02)
        assert phone.closes[0].reason == "ble_close_wifi"
        assert dev.state is WifiDeviceState.CLOSED and dev.close_reason == "ble_close_wifi"
        assert responses[-1] == encode_close_wifi_response(0)
        assert not peripheral.wifi_hotspot_on and not peripheral.wifi_active
        ended = [e for e in peripheral.wifi_session_log if e["event"] == "ended"]
        assert ended == [{"event": "ended", "state": "closed", "reason": "ble_close_wifi", "error": None}]
        assert not [e for e in peripheral.packet_log if e.get("direction") == "error"]


@pytest.mark.asyncio
async def test_second_open_does_not_start_a_second_wifi_device() -> None:
    async with PhoneWifiServer(token=TOKEN) as phone:
        devices, peripheral, peer, data, command = await ble_link(phone_dialer("127.0.0.1", phone.port, **QUIET))
        responses: list[bytes] = []
        await peer.subscribe(data, responses.append, prefer_notify=True)
        await peer.write_value(command, OPEN_ON, with_response=True)
        first = peripheral.wifi_device
        await phone.wait_ready(5)
        await peer.write_value(command, OPEN_ON, with_response=True)
        assert peripheral.wifi_device is first
        assert responses == [encode_open_wifi_response(0, "10000001"), encode_open_wifi_response(WIFI_OPEN_STATUS_BUSY)]
        assert [e["event"] for e in peripheral.wifi_session_log] == ["started"]
        # R7-S14: opcode 10 with mode 0 is the SDK's fast-transfer OPEN, not a close:
        # while the hotspot is up it is refused like any second open.
        await peer.write_value(command, OPEN_OFF, with_response=True)
        assert peripheral.wifi_device is first and first.state is WifiDeviceState.HANDSHAKED
        assert responses[-1] == encode_open_wifi_response(WIFI_OPEN_STATUS_BUSY)
        await peer.write_value(command, CLOSE, with_response=True)
        await first.wait_closed(5)
        assert first.close_reason == "ble_close_wifi"


@pytest.mark.asyncio
async def test_close_wifi_while_the_pen_is_still_dialling() -> None:
    """Opcode 13 before the dial completed (review T3's ordering): no
    exception, no late CONNECTED/HANDSHAKED, the hotspot flag drops."""
    factory = phone_dialer("127.0.0.1", 9, connect_attempts=100, connect_retry_interval=0.05, **QUIET)
    devices, peripheral, peer, data, command = await ble_link(factory)
    await peer.subscribe(data, lambda _v: None, prefer_notify=True)
    await peer.write_value(command, OPEN_ON, with_response=True)
    dev = peripheral.wifi_device
    await asyncio.sleep(0.12)
    assert dev.state is WifiDeviceState.CONNECTING
    await peer.write_value(command, CLOSE, with_response=True)
    await dev.wait_closed(3)
    await wait_for(lambda: not peripheral.wifi_active)
    assert dev.state_log == ["idle", "connecting", "closing", "closed"]
    assert not peripheral.wifi_hotspot_on
    assert peripheral.wifi_session_log[-1] == {"event": "ended", "state": "closed", "reason": "ble_close_wifi", "error": None}


@pytest.mark.asyncio
async def test_a_failed_dial_drops_the_hotspot_and_a_later_open_starts_a_new_device() -> None:
    """HARNESS_POLICY: when the pen's Wi-Fi session ends on its own (dial
    failure, idle self-close, exit timeout) it drops the hotspot, so the next
    opcode 10 is accepted and gets a NEW single-use WifiDevice."""
    async with PhoneWifiServer(token=TOKEN) as phone:
        targets = iter([9, phone.port])

        def factory(p):
            return phone_dialer("127.0.0.1", next(targets), open_timeout=1.0, **QUIET)(p)

        devices, peripheral, peer, data, command = await ble_link(factory)
        responses: list[bytes] = []
        await peer.subscribe(data, responses.append, prefer_notify=True)
        await peer.write_value(command, OPEN_ON, with_response=True)
        failed = peripheral.wifi_device
        await failed.wait_closed(3)
        await wait_for(lambda: not peripheral.wifi_hotspot_on)
        assert failed.state is WifiDeviceState.FAILED
        await peer.write_value(command, OPEN_ON, with_response=True)
        assert peripheral.wifi_device is not failed
        await phone.wait_ready(5)
        assert responses == [encode_open_wifi_response(0, "10000001")] * 2
        await peer.write_value(command, CLOSE, with_response=True)
        await peripheral.wifi_device.wait_closed(5)
        assert [e["event"] for e in peripheral.wifi_session_log] == ["started", "ended", "started", "ended"]
        assert peripheral.wifi_session_log[1]["state"] == "failed"


def test_without_a_factory_opcode_10_only_records_the_request() -> None:
    """Default behaviour is unchanged: no factory, no Wi-Fi device."""

    class _Stub:
        def on(self, *_a, **_k):
            return None

    p = PlaudPeripheral(_Stub())
    assert p._open_wifi(OPEN_ON) == [encode_open_wifi_response(0, "10000001")]
    assert p.wifi_device is None and not p.wifi_active and p.wifi_session_log == []


@pytest.mark.asyncio
async def test_close_then_immediate_reopen_starts_a_fresh_device() -> None:
    """Opcode 13 immediately followed by opcode 10: the old device is still
    winding down when the new open is accepted; the new open must still get
    its own device (not a status 0 with nothing behind it). The phone stops
    its server on WifiClose (WifiAgentImpl.txt:1187-1292) and starts a new one
    for the next transfer, hence two phone doubles."""
    async with PhoneWifiServer(token=TOKEN) as phone1, PhoneWifiServer(token=TOKEN) as phone2:
        targets = iter([phone1.port, phone2.port])

        def factory(p):
            return phone_dialer("127.0.0.1", next(targets), **QUIET)(p)

        devices, peripheral, peer, data, command = await ble_link(factory)
        await peer.subscribe(data, lambda _v: None, prefer_notify=True)
        await peer.write_value(command, OPEN_ON, with_response=True)
        first = peripheral.wifi_device
        await phone1.wait_ready(5)
        await peer.write_value(command, CLOSE, with_response=True)
        await peer.write_value(command, OPEN_ON, with_response=True)
        second = peripheral.wifi_device
        assert second is not first
        await first.wait_closed(5)
        await phone2.wait_ready(5)
        await asyncio.sleep(0.02)
        assert peripheral.wifi_hotspot_on and peripheral.wifi_active
        await peer.write_value(command, CLOSE, with_response=True)
        await second.wait_closed(5)
        await wait_for(lambda: not peripheral.wifi_active)
        assert not peripheral.wifi_hotspot_on
        assert (first.close_reason, second.close_reason) == ("ble_close_wifi", "ble_close_wifi")


@pytest.mark.asyncio
async def test_r7_s14_the_sdks_recorded_ble_sequence_opens_a_pullable_wifi_session() -> None:
    """R7-S14 regression, over GATT: replay the BLE writes the genuine SDK made
    for startWifiTransfer + the template's teardown (r7/r7-s14-evidence,
    capture-run1b-transfer-shipped.json: stopSync `011d00`, open `010a0000`,
    close `010d00`). Before the fix, mode 0 read as "hotspot off": status 0
    without a passphrase and no Wi-Fi device. Now the pen starts, dials the
    phone double, and the file is pulled byte-exact. (On the AVD the SDK never
    gets this far: it blocks on the SoftAP join, r7/r7-s14-wifi-real-sdk.md.)"""
    async with PhoneWifiServer(token=TOKEN) as phone:
        factory = phone_dialer("127.0.0.1", phone.port, chunk_size=1000, **QUIET)
        devices, peripheral, peer, data, command = await ble_link(factory)
        responses: list[bytes] = []
        await peer.subscribe(data, responses.append, prefer_notify=True)
        await peer.write_value(command, pack_stop_sync_request(), with_response=True)   # 011d00
        await peer.write_value(command, bytes.fromhex("010a0000"), with_response=True)
        await asyncio.sleep(0.02)
        j4 = [r for r in responses if r[:3] == b"\x01\x0a\x00"]
        assert j4 == [encode_open_wifi_response(0, "10000001")]
        dev = peripheral.wifi_device
        assert dev is not None and peripheral.wifi_active and peripheral.wifi_hotspot_on
        assert peripheral.wifi_log[0]["mode"] == 0
        await phone.wait_ready(5)
        listing = await phone.get_file_list()
        assert [(r.session_id, r.file_size, r.scene) for r in listing.records] == [(SID, len(FILE), 2)]
        dl = await phone.download(SID, len(FILE))
        assert dl.data == FILE
        await peer.write_value(command, CLOSE, with_response=True)                     # 010d00
        await dev.wait_closed(5)
        assert dev.close_reason == "ble_close_wifi"
        await phone.wait_device_closed(5)
