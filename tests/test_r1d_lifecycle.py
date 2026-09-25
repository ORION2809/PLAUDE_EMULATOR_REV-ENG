"""R1d: the emulator's connection lifecycle, and the handshake boundary.

No new packet exchange. getChargingState() is a zero-BLE-packet facade (a
synchronous cache read plus a listener callback; see
docs/fixtures/device-lifecycle.json), so there is nothing to put on the wire.
R1d instead establishes the first coherent lifecycle --
DISCONNECTED -> GATT_READY -> CONNECTED -> DISCONNECTED over the real Bumble
virtual path -- and guards the boundary that matters: bare command exchanges
must never reach BOUND or READY, because reaching them requires an
account-issued handshake token this repository does not have.

Peripheral construction precedes the virtual connect here (unlike the helpers
in support.py) so the connection event is observed in order, mirroring a real
peripheral that publishes GATT before any central connects.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from bumble.testing.test_utils import TwoDevices

from support import discover, load_fixture, role_uuids

from plaudsim.profile import (
    ENCRYPTED_PORT_VERSION,
    PORT_VERSION,
    PlaudLifecycle,
    PlaudPeripheral,
    PlaudStorageState,
    PlaudSyncTimeState,
)

FX_STATE = load_fixture("post-bind-get-state.json")
SERVICE_UUID, DATA_UUID, COMMAND_UUID = role_uuids(FX_STATE)
REQUEST_BYTES = bytes(FX_STATE["request"]["example_bytes"])

FX_LIFE = load_fixture("device-lifecycle.json")
assert FX_LIFE["getChargingState_finding"]["verdict"].startswith("ZERO BLE packets")


async def make_presubscribed() -> tuple[TwoDevices, PlaudPeripheral]:
    devices = TwoDevices()
    det_storage = load_fixture("post-bind-get-storage.json")["response"]["emulator_deterministic"]
    det_sync = load_fixture("post-bind-sync-time.json")["response"]["emulator_deterministic"]
    peripheral = PlaudPeripheral(
        devices[1],
        storage=PlaudStorageState(
            free=det_storage["free"]["value"],
            total=det_storage["total"]["value"],
            duration=det_storage["duration"]["value"],
        ),
        synctime=PlaudSyncTimeState(
            stamp=det_sync["stamp"]["value"],
            tz_hours=det_sync["tz_hours"]["value"],
            tz_mins=det_sync["tz_mins"]["value"],
            timezone=det_sync["timezone"]["value"],
            has_statistics=det_sync["has_statistics"]["value"],
        ),
    )
    assert peripheral.lifecycle == PlaudLifecycle.DISCONNECTED
    peripheral.install()
    assert peripheral.lifecycle == PlaudLifecycle.GATT_READY
    await devices.setup_connection()
    return devices, peripheral


async def wait_for(peripheral: PlaudPeripheral, state: PlaudLifecycle) -> None:
    for _ in range(200):
        if peripheral.lifecycle == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"lifecycle never reached {state}")


@pytest.mark.asyncio
async def test_lifecycle_connect_exchange_disconnect() -> None:
    devices, peripheral = await make_presubscribed()
    await wait_for(peripheral, PlaudLifecycle.CONNECTED)
    assert peripheral.lifecycle_log == ["gatt_ready", "connected"]

    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, REQUEST_BYTES, with_response=True)

    assert len(responses) == 1
    assert responses[0][:3] == b"\x01\x03\x00"
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED
    assert peripheral.lifecycle not in (PlaudLifecycle.BOUND, PlaudLifecycle.READY)

    await devices.connections[0].disconnect()
    await wait_for(peripheral, PlaudLifecycle.DISCONNECTED)
    assert peripheral.lifecycle_log == ["gatt_ready", "connected", "disconnected"]


@pytest.mark.asyncio
async def test_no_bind_without_handshake() -> None:
    devices, peripheral = await make_presubscribed()
    await wait_for(peripheral, PlaudLifecycle.CONNECTED)

    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=False)
    for raw in (
        REQUEST_BYTES,
        bytes(load_fixture("post-bind-get-storage.json")["request"]["example_bytes"]),
        bytes(load_fixture("post-bind-sync-time.json")["request"]["example_bytes"]),
    ):
        await peer.write_value(command, raw, with_response=True)

    assert len(responses) == 3
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED


def test_peripheral_refuses_to_claim_a_modern_port_version() -> None:
    """Above portVersion 20 the SDK seals every frame with ChaCha20-Poly1305,
    so a cleartext peripheral advertising 20+ would be lying about what it
    speaks. The constructor rejects it rather than serving plaintext."""
    assert PORT_VERSION < ENCRYPTED_PORT_VERSION
    # The guard fires before the device is touched, so no link is needed.
    for pv in (ENCRYPTED_PORT_VERSION, ENCRYPTED_PORT_VERSION + 5):
        with pytest.raises(ValueError, match="ChaCha20-Poly1305"):
            PlaudPeripheral(None, port_version=pv)
