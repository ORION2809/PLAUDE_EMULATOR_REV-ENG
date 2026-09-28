"""R7-S14 Wi-Fi capture device: the R7-S13 BLE peripheral plus the Wi-Fi pen.

One process, two transports, both ours:

* BLE: `PlaudPeripheral.for_real_sdk` (portVersion 7, legacy, the R7-S13
  configuration) on Bumble `android-netsim`, serving
  tests/fixtures/r6s2_16k_mono.ogg as session 1700000000. Every 2BB1 write and
  every notification the peripheral emits is mirrored (time, hex, opcode).
* Wi-Fi: `wifi_device_factory = phone_dialer("127.0.0.1", WIFICAP_HOST_PORT)`.
  An accepted opcode 10 starts a `plaudsim.wifi_device.WifiDevice` (the pen
  side, a WebSocket CLIENT) serving the same file table; it dials
  ws://127.0.0.1:<WIFICAP_HOST_PORT>, which the run script forwards to the
  AVD with `adb -s emulator-5554 forward tcp:<port> tcp:8081` (the phone's
  java-websocket server port, WebSocketOperation.txt:174-181). Every WebSocket
  frame in both directions is recorded as hex, with every dial attempt.

Environment knobs (HARNESS_POLICY, documented in r7/r7-s14-wifi-real-sdk.md):
    WIFICAP_HOST_PORT         host side of the adb forward (default 18081)
    WIFICAP_CONNECT_ATTEMPTS  dial attempts per Wi-Fi device (default 90)
    WIFICAP_RETRY_S           seconds between attempts (default 1.0)
    WIFICAP_FILE / WIFICAP_SESSION_ID   served recording (defaults as R7-S13)
    WIFICAP_CAPTURE           capture JSON (default <repo>/r7/wifi-capture.json)
    WIFICAP_CLOSE_RSP_OPCODE  EXPERIMENT (run 4 only): answer CloseWiFi (13) with this
                              response opcode instead of 13; the l0 body is unchanged.
                              Probes q.P's response bean {10} (ALL.txt:46011-46030);
                              not a device claim and not an emulator change.
    K3CAP_BUMBLE_LOG          bumble log level (default WARNING)

NOTHING here is authentication or a network join: the BLE token is the
synthetic identifier the app was told to use, the Wi-Fi device accepts any
handshake token (WifiDevice default), no SoftAP exists, and no Plaud
infrastructure is touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference" / "upstream" / "bumble"))

from bumble import logging as bumble_logging

bumble_logging.setup_basic_logging(os.environ.get("K3CAP_BUMBLE_LOG", "WARNING"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("R7S14")

from bumble import data_types
from bumble.core import AdvertisingData
from bumble.device import Device, DeviceConfiguration
from bumble.hci import Address
from bumble.transport import open_transport

from plaudsim.profile import (
    OPCODE_CLOSE_WIFI,
    OPCODE_OPEN_WIFI,
    PlaudBatteryState,
    PlaudDeviceState,
    PlaudPeripheral,
    PlaudStorageState,
    PlaudSyncTimeState,
)
from plaudsim.wifi import MESSAGE_NAMES, looks_encrypted, parse_pdu
from plaudsim.wifi_device import WifiDevice, phone_dialer

FILE_PATH = os.environ.get("WIFICAP_FILE", str(ROOT / "tests" / "fixtures" / "r6s2_16k_mono.ogg"))
SESSION_ID = int(os.environ.get("WIFICAP_SESSION_ID", str(1700000000)))
HOST_PORT = int(os.environ.get("WIFICAP_HOST_PORT", "18081"))
CONNECT_ATTEMPTS = int(os.environ.get("WIFICAP_CONNECT_ATTEMPTS", "90"))
RETRY_S = float(os.environ.get("WIFICAP_RETRY_S", "1.0"))
CAPTURE_PATH = os.environ.get("WIFICAP_CAPTURE", str(ROOT / "r7" / "wifi-capture.json"))
CLOSE_RSP_OPCODE = os.environ.get("WIFICAP_CLOSE_RSP_OPCODE")

FILE_BYTES = open(FILE_PATH, "rb").read()
FILE_SHA = hashlib.sha256(FILE_BYTES).hexdigest()
T0 = time.time()

BLE: list[dict[str, Any]] = []        # every 2BB1 write and every emitted notification
WIFI: list[dict[str, Any]] = []       # every WebSocket frame / dial event, all Wi-Fi devices
DEVICES: list["CapturingWifiDevice"] = []
PERIPHERAL: dict[str, Any] = {}


def _rel() -> float:
    return round(time.time() - T0, 3)


def _describe_pdu(raw: bytes) -> dict[str, Any]:
    if looks_encrypted(raw):
        return {"sealed_guess": True}
    try:
        pdu = parse_pdu(raw)
    except ValueError as exc:
        return {"parse_error": str(exc)}
    out: dict[str, Any] = {"type": pdu.msg_type, "name": MESSAGE_NAMES.get(pdu.msg_type), "pdu_version": pdu.pdu_version}
    try:
        out["json"] = pdu.json
    except Exception:  # diagnostic only
        pass
    if pdu.tail:
        out["tail_len"] = len(pdu.tail)
    return out


def _dump() -> None:
    p = PERIPHERAL.get("p")
    doc = {
        "stats": {
            "file": FILE_PATH, "file_len": len(FILE_BYTES), "file_sha256": FILE_SHA,
            "session_id": SESSION_ID, "host_port": HOST_PORT,
            "connect_attempts": CONNECT_ATTEMPTS, "retry_s": RETRY_S, "t0": T0,
        },
        "ble": BLE,
        "wifi": WIFI,
        "wifi_devices": [
            {"uri": d.uri, "state": d.state.value, "state_log": d.state_log, "close_reason": d.close_reason}
            for d in DEVICES
        ],
        "peripheral_wifi_log": list(getattr(p, "wifi_log", [])) if p else [],
        "peripheral_wifi_session_log": list(getattr(p, "wifi_session_log", [])) if p else [],
        "hotspot_on": getattr(p, "wifi_hotspot_on", None) if p else None,
    }
    tmp = CAPTURE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1, default=str)
    os.replace(tmp, CAPTURE_PATH)


class CapturingWifiDevice(WifiDevice):
    """Records every frame on the wire (hex, both directions) and every event
    with a timestamp. Behaviour is the shipped WifiDevice's."""

    def _event(self, kind: str, **fields: Any) -> None:
        super()._event(kind, **fields)
        WIFI.append({"t": _rel(), "dir": "event", "kind": kind, **fields})
        logger.info("WIFICAP_WIFI_EVENT %s %s", kind, fields)
        _dump()

    def _set_state(self, state: Any) -> None:
        before = self.state
        super()._set_state(state)
        if self.state is not before:
            WIFI.append({"t": _rel(), "dir": "state", "state": self.state.value})
            logger.info("WIFICAP_WIFI_STATE %s", self.state.value)
            _dump()

    async def send(self, message: Any) -> bytes:
        wire = await super().send(message)
        WIFI.append({"t": _rel(), "dir": "out", "len": len(wire), "hex": wire.hex(), **_describe_pdu(wire)})
        logger.info("WIFICAP_WIFI_OUT type=%s len=%d", getattr(message, "TYPE", "?"), len(wire))
        _dump()
        return wire

    async def _on_frame(self, raw: bytes) -> None:
        WIFI.append({"t": _rel(), "dir": "in", "len": len(raw), "hex": bytes(raw).hex(), **_describe_pdu(bytes(raw))})
        logger.info("WIFICAP_WIFI_IN len=%d", len(raw))
        _dump()
        await super()._on_frame(raw)


def capturing_dialer(host: str, port: int, **kwargs: Any):
    base = phone_dialer(host, port, **kwargs)

    def factory(peripheral: Any) -> WifiDevice:
        template = base(peripheral)   # resolves serial / version / pVer / store exactly as phone_dialer does
        dev = CapturingWifiDevice(
            host, port, store=template.store, serial=template.serial, version=template.version,
            p_ver=template.p_ver, **kwargs,
        )
        DEVICES.append(dev)
        WIFI.append({"t": _rel(), "dir": "event", "kind": "device_started", "uri": dev.uri,
                     "records": [(r.session_id, r.file_size, r.scene) for r in dev.store.records()]})
        logger.info("WIFICAP_WIFI_DEVICE_STARTED uri=%s", dev.uri)
        _dump()
        return dev

    return factory


class WifiCapturingPeripheral(PlaudPeripheral):
    """Mirrors 2BB1 writes and emitted notifications; behaviour unchanged."""

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        raw = bytes(value)
        rec: dict[str, Any] = {"t": _rel(), "dir": "write", "len": len(raw), "hex": raw.hex()}
        if len(raw) >= 3 and raw[0] == 1:
            rec["opcode"] = int.from_bytes(raw[1:3], "little")
            if rec["opcode"] in (OPCODE_OPEN_WIFI, OPCODE_CLOSE_WIFI):
                logger.info("WIFICAP_BLE_WIFI_WRITE opcode=%d hex=%s", rec["opcode"], raw.hex())
        BLE.append(rec)
        _dump()
        await super()._on_command_write(connection, value)
        _dump()

    def _close_wifi(self, request: bytes) -> list[bytes]:
        frames = super()._close_wifi(request)
        if CLOSE_RSP_OPCODE is None:
            return frames
        # EXPERIMENT (run 4): same l0 status byte, response opcode rewritten.
        op = int(CLOSE_RSP_OPCODE, 0).to_bytes(2, "little")
        rewritten = [f[:1] + op + f[3:] for f in frames]
        logger.info("WIFICAP_EXPERIMENT close response rewritten %s -> %s",
                    [f.hex() for f in frames], [f.hex() for f in rewritten])
        return rewritten

    async def _emit(self, connection: Any, payload: bytes) -> bool:
        ok = await super()._emit(connection, payload)
        raw = bytes(payload)
        rec: dict[str, Any] = {"t": _rel(), "dir": "notify", "len": len(raw), "hex": raw.hex(), "delivered": ok}
        if len(raw) >= 3 and raw[0] == 1:
            rec["opcode"] = int.from_bytes(raw[1:3], "little")
            if rec["opcode"] in (OPCODE_OPEN_WIFI, OPCODE_CLOSE_WIFI):
                logger.info("WIFICAP_BLE_WIFI_NOTIFY opcode=%d hex=%s", rec["opcode"], raw.hex())
        BLE.append(rec)
        return ok


async def main() -> None:
    spec = sys.argv[1] if len(sys.argv) > 1 else "android-netsim"
    async with await open_transport(spec) as hci:
        config = DeviceConfiguration(name="PlaudNotePro", address=Address("F0:1A:2B:3C:4D:5E"))
        device = Device.from_config_with_hci(config, hci.source, hci.sink)
        peripheral = WifiCapturingPeripheral.for_real_sdk(
            device,
            state=PlaudDeviceState(state=0x1001, privacy_enabled=True, key_state=1, scene=4, session_id=SESSION_ID),
            storage=PlaudStorageState(free=8589934592, total=17179869184, duration=36000),
            synctime=PlaudSyncTimeState(stamp=1700000000),
            battery=PlaudBatteryState(charging=False, level=87),
            file_bytes=FILE_BYTES,
            file_table=[{"session_id": SESSION_ID, "file_size": len(FILE_BYTES), "scene": 2, "attribute": 1}],
            l3_timezone=5,
            wifi_device_factory=capturing_dialer(
                "127.0.0.1", HOST_PORT, connect_attempts=CONNECT_ATTEMPTS, connect_retry_interval=RETRY_S,
                open_timeout=3.0,
            ),
        )
        PERIPHERAL["p"] = peripheral
        peripheral.install()
        await device.power_on()
        device.advertising_data = bytes(AdvertisingData([
            data_types.Flags(0x06),
            data_types.ManufacturerSpecificData(0xFFFF, peripheral.manufacturer_data()),
        ]))
        await device.start_advertising(auto_restart=True)
        logger.info("WIFICAP_PERIPHERAL_READY pv=%s sn=%s file=%s len=%d sha256=%s session=%d wifi=ws://127.0.0.1:%d",
                    peripheral.port_version, peripheral.scan_fields.serial_number, FILE_PATH, len(FILE_BYTES),
                    FILE_SHA, SESSION_ID, HOST_PORT)
        _dump()
        await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _dump()
        logger.info("WIFICAP_STOP ble=%d wifi=%d", len(BLE), len(WIFI))
