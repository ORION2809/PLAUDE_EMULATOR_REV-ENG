"""Shared virtual-link helpers for the protocol tests.

Fixture-driven: service/characteristic UUIDs and request bytes come from the
docs/fixtures/*.json protocol records, never from plaudsim.profile, so a
swapped UUID in the emulator fails discovery rather than passing silently.

MTU note: `z$c.onConnectionStateChange` calls `requestMtu(z.m)` with z.m
initialised to 255 the instant the link comes up, and `discoverServices()` has
exactly ONE call site in the whole artifact -- inside `onMtuChanged`. The real
Android SDK therefore never discovers services on a link where the MTU
exchange did not complete. Tests that exchange MTU are modelling the real
central, not papering over a limitation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from bumble.device import Peer
from bumble.gatt import Characteristic
from bumble.testing.test_utils import TwoDevices

from plaudsim.profile import PlaudPeripheral

FIXTURES_DIR = Path(__file__).parents[1] / "docs/fixtures"

#: What the Android SDK asks for on every connect (z.m initialiser, z$c
#: onConnectionStateChange STATE_CONNECTED branch).
SDK_REQUESTED_MTU = 255


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


def role_uuids(fixture: dict[str, Any]) -> tuple[str, str, str]:
    """(service, data_notify, command_write) UUIDs, independently sourced."""
    service = fixture["request"]["ble_write"]["service"].lower()
    assert service == fixture["response"]["received_on"]["service"].lower()
    return (
        service,
        fixture["response"]["received_on"]["characteristic"].lower(),
        fixture["request"]["ble_write"]["characteristic"].lower(),
    )


async def discover(
    devices: TwoDevices, service_uuid: str, data_uuid: str, command_uuid: str
) -> tuple[Peer, object, object]:
    """Real ATT discovery + swap-detecting role/property asserts."""
    peer = Peer(devices.connections[0])
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(s for s in peer.services if str(s.uuid).lower() == service_uuid)
    data = next(c for c in service.characteristics if str(c.uuid).lower() == data_uuid)
    command = next(c for c in service.characteristics if str(c.uuid).lower() == command_uuid)
    props = Characteristic.Properties
    assert data.properties & props.NOTIFY, "data char must support NOTIFY"
    assert data.properties & props.INDICATE, "data char must support INDICATE"
    assert not data.properties & props.WRITE, "data char must not be writable"
    assert command.properties & props.WRITE, "command char must support WRITE"
    assert not command.properties & (props.NOTIFY | props.INDICATE), (
        "command char must not notify/indicate"
    )
    return peer, data, command


async def make_linked_peripheral(
    peripheral_factory: Any,
) -> tuple[TwoDevices, PlaudPeripheral]:
    devices = await TwoDevices.create_with_connection()
    peripheral = peripheral_factory(devices[1])
    peripheral.install()
    return devices, peripheral


async def connect_like_the_sdk(
    peripheral_factory: Any, fixture_name: str = "post-bind-get-storage.json"
):
    """Bring a link up the way the Android SDK does: MTU first, then discovery.

    Returns (devices, peripheral, peer, data_char, command_char).
    """
    fixture = load_fixture(fixture_name)
    service_uuid, data_uuid, command_uuid = role_uuids(fixture)
    devices, peripheral = await make_linked_peripheral(peripheral_factory)
    peer = Peer(devices.connections[0])
    await peer.request_mtu(SDK_REQUESTED_MTU)
    peer, data, command = await discover(devices, service_uuid, data_uuid, command_uuid)
    return devices, peripheral, peer, data, command
