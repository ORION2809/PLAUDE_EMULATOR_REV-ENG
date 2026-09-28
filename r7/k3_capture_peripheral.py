"""R7-S12 capture peripheral: frozen PlaudPeripheral (portVersion 7, legacy)
attached to the netsim transport, instrumented to record every host->device
write on 1910/2BB1 so the genuine k3 frame the real SDK constructs can be
captured byte-for-byte.

Reuses emulator/plaudsim/profile.py UNCHANGED except a thin _on_command_write
wrapper that timestamps and mirrors each write to a JSON capture file and to
stdout with a K3CAP_ prefix (for logcat correlation). The device still answers
l3(status=0) so the SDK's genuine k3 is accepted and the handshake advances —
which additionally proves EMULATOR_INTEGRATION (the emulator accepts a real
SDK-built k3), not merely that k3 was written.

NOTHING here is authentication: the token inside k3 is a synthetic identifier
the app was told to use; the peripheral is ours; no Plaud infrastructure is
touched.

Usage:
    python r7/k3_capture_peripheral.py [transport-spec] [capture-json-path]
    (defaults: android-netsim, <repo>/r7/k3-capture.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# The repository root is derived from this file's location (<root>/r7/<file>),
# so the script runs from any checkout; nothing machine-specific is hard-coded.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference" / "upstream" / "bumble"))

from bumble import logging as bumble_logging

bumble_logging.setup_basic_logging(__import__("os").environ.get("K3CAP_BUMBLE_LOG", "WARNING"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("R7S12")

from bumble.core import AdvertisingData
from bumble import data_types
from bumble.device import Device, DeviceConfiguration
from bumble.hci import Address
from bumble.transport import open_transport

from plaudsim.profile import (
    PlaudBatteryState,
    PlaudDeviceState,
    PlaudPeripheral,
    PlaudStorageState,
    PlaudSyncTimeState,
)
from plaudsim.handshake import parse_handshake_request

CAPTURE_PATH = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "r7" / "k3-capture.json")
CAPTURES: list[dict[str, Any]] = []


class CapturingPeripheral(PlaudPeripheral):
    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        raw = bytes(value)
        rec: dict[str, Any] = {
            "t": time.time(),
            "char": "2BB1",
            "len": len(raw),
            "hex": raw.hex(),
        }
        # Best-effort parse as a k3/j3 handshake (opcode 1) using OUR advertised pv.
        try:
            if len(raw) >= 3 and raw[0] == 1:
                opcode = int.from_bytes(raw[1:3], "little")
                rec["opcode"] = opcode
                if opcode == 1:
                    rec["parsed_k3"] = parse_handshake_request(raw, self.port_version)
        except Exception as exc:  # parsing is diagnostic only
            rec["parse_error"] = str(exc)
        CAPTURES.append(rec)
        with open(CAPTURE_PATH, "w") as fh:
            json.dump({"port_version": self.port_version, "writes": CAPTURES}, fh, indent=1)
        logger.info("K3CAP_WRITE char=2BB1 len=%d hex=%s%s",
                    len(raw), raw.hex(),
                    f" opcode={rec.get('opcode')}" if "opcode" in rec else "")
        if "parsed_k3" in rec:
            logger.info("K3CAP_PARSED %s", json.dumps(rec["parsed_k3"], default=str))
        await super()._on_command_write(connection, value)


async def main() -> None:
    spec = sys.argv[1] if len(sys.argv) > 1 else "android-netsim"
    async with await open_transport(spec) as hci:
        config = DeviceConfiguration(
            name="PlaudNotePro", address=Address("F0:1A:2B:3C:4D:5E")
        )
        device = Device.from_config_with_hci(config, hci.source, hci.sink)
        # for_real_sdk(): like every rig that faces the unmodified SDK
        # (r4-s3/plaud_netsim_peripheral.py, r7/pull_capture_peripheral.py),
        # take the R7-S13 transfer policy (task streaming, 4 ms pacing, abort
        # on restart/stop). R7-S12 runs 1-4 predate R7-S13 and used the bare,
        # inline/unpaced constructor; the policy only changes how a syncFile
        # transfer is emitted, not the handshake writes this rig captures.
        peripheral = CapturingPeripheral.for_real_sdk(
            device,
            state=PlaudDeviceState(
                state=0x1001, privacy_enabled=True, key_state=1,
                scene=4, session_id=0x12345678,
            ),
            storage=PlaudStorageState(free=8589934592, total=17179869184, duration=36000),
            synctime=PlaudSyncTimeState(stamp=1700000000),
            battery=PlaudBatteryState(charging=False, level=87),
            file_bytes=bytes(range(64)),
            file_table=[{"session_id": 0x12345678, "file_size": 64, "scene": 2, "attribute": 1}],
            tail_crc=0xBEEF,
            # R7-S12 run 4: non-zero l3 timezone falsifies the claim that the
            # facade forwards q.n (= l3 tz byte) as bleBind's protVersion.
            l3_timezone=5,
        )
        # accept_any_token is the default HARNESS_POLICY; the device answers l3(0).
        peripheral.install()
        await device.power_on()
        # netsimd rejects extended advertising; publish a legacy 29-byte payload.
        device.advertising_data = bytes(
            AdvertisingData([
                data_types.Flags(0x06),
                data_types.ManufacturerSpecificData(0xFFFF, peripheral.manufacturer_data()),
            ])
        )
        await device.start_advertising(auto_restart=True)
        logger.info("K3CAP_PERIPHERAL_READY port_version=%s addr=F0:1A:2B:3C:4D:5E capture=%s",
                    peripheral.port_version, CAPTURE_PATH)
        await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("K3CAP_STOP writes=%d", len(CAPTURES))
