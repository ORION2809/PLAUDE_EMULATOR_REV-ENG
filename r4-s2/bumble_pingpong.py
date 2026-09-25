"""R4-S2 Bumble side: ping/pong peripheral on the netsim transport.

One service, one write char, one notify char (generic UUIDs).
Android writes b"ping" -> peripheral notifies b"pong".

Usage:
    /tmp/r4s2-venv/bin/python bumble_pingpong.py [transport-spec]
Default spec "android-netsim" resolves the netsim endpoint via .ini file.
Logs every GATT operation with a PINGPONG_ prefix for evidence capture.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("PINGPONG")

from bumble.core import UUID, AdvertisingData
from bumble import data_types
from bumble.device import Device, DeviceConfiguration
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.transport import open_transport

SERVICE_UUID = "0000feed-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fe01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fe02-0000-1000-8000-00805f9b34fb"


async def main() -> None:
    spec = sys.argv[1] if len(sys.argv) > 1 else "android-netsim"
    async with await open_transport(spec) as hci_transport:
        config = DeviceConfiguration(name="R4S2-PingPong")
        device = Device.from_config_with_hci(
            config, hci_transport.source, hci_transport.sink
        )

        async def on_write(connection, value: bytes) -> None:
            logger.info("PINGPONG_WRITE_RX bytes=%s", bytes(value).hex())
            if bytes(value) == b"ping":
                logger.info("PINGPONG_NOTIF_TX pong")
                await device.notify_subscribers(data_characteristic, b"pong")

        data_characteristic = Characteristic(
            NOTIFY_UUID,
            Characteristic.Properties.NOTIFY | Characteristic.Properties.READ,
            Characteristic.READABLE,
            b"",
        )
        write_characteristic = Characteristic(
            WRITE_UUID,
            Characteristic.Properties.WRITE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=on_write),
        )
        device.add_service(Service(SERVICE_UUID, [data_characteristic, write_characteristic]))
        device.advertising_data = bytes(
            AdvertisingData(
                [
                    data_types.CompleteLocalName("R4S2-PingPong"),
                    data_types.IncompleteListOf16BitServiceUUIDs(
                        [UUID.from_16_bits(0xFEED)]
                    ),
                ]
            )
        )
        await device.power_on()
        await device.start_advertising(auto_restart=True)
        logger.info("PINGPONG_PERIPHERAL_READY")
        await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
