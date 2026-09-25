"""R4-S3 Bumble side: frozen PlaudPeripheral on the netsim transport.

Reuses emulator/plaudsim/profile.py UNCHANGED: the same PlaudPeripheral the
virtual-link tests drive, attached to a netsim-backed Bumble controller
instead of a virtual link. Advertises the Plaud service/characteristics so
the real Android SDK can discover it.

Usage:
    /tmp/r4s2-venv/bin/python plaud_netsim_peripheral.py [transport-spec]
Logs with R4S3_ prefix for evidence correlation with logcat.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bumble import logging as bumble_logging

bumble_logging.setup_basic_logging("DEBUG")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("R4S3")

sys.path.insert(0, "/Users/mivi/Desktop/plaud-harness/emulator")

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


async def main() -> None:
    spec = sys.argv[1] if len(sys.argv) > 1 else "android-netsim"
    async with await open_transport(spec) as hci_transport:
        from bumble.hci import Address

        # Static random address: restarts must NOT rotate the MAC, or the
        # app's cached scan entry points at a dead address and connect()
        # never produces any GATT traffic (observed failure mode).
        config = DeviceConfiguration(
            name="PlaudNotePro", address=Address("F0:1A:2B:3C:4D:5E")
        )
        device = Device.from_config_with_hci(
            config, hci_transport.source, hci_transport.sink
        )
        peripheral = PlaudPeripheral(
            device,
            state=PlaudDeviceState(
                state=0x1001,
                privacy_enabled=True,
                key_state=1,
                scene=4,
                session_id=0x12345678,
            ),
            storage=PlaudStorageState(
                free=8589934592, total=17179869184, duration=36000
            ),
            synctime=PlaudSyncTimeState(stamp=1700000000),
            battery=PlaudBatteryState(charging=False, level=87),
            file_bytes=bytes(range(64)),
            file_table=[
                {"session_id": 0x12345678, "file_size": 64, "scene": 2, "attribute": 1}
            ],
            tail_crc=0xBEEF,
        )
        peripheral.install()
        await device.power_on()
        # R4-S3 transport note: netsimd rejects extended advertising, so the
        # runner publishes a legacy-compatible payload (flags + manufacturer
        # data only, 29 B) INSTEAD of peripheral.advertise(). The SDK does not
        # filter on service UUIDs (empty ScanFilter; software filtering on
        # manufacturer data), so nothing the client needs is lost. Frozen
        # profile.advertise() is untouched.
        from bumble.core import AdvertisingData
        from bumble import data_types

        device.advertising_data = bytes(
            AdvertisingData(
                [
                    data_types.Flags(0x06),
                    data_types.ManufacturerSpecificData(
                        0xFFFF, peripheral.manufacturer_data()
                    ),
                ]
            )
        )
        await device.start_advertising(auto_restart=True)
        logger.info("R4S3_PERIPHERAL_READY port_version=%s", peripheral.port_version)
        await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
