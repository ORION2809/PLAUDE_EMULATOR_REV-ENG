"""R5-S10: FE11 completion semantics, device side (mechanically established).

R5-S10 proved from SDK bytecode that the host acts on an FE11 only via
the last-chunk registration (newest-match dispatch + i3==length-1 gate):
an FE11 arriving before the final v4 chunk is queued is swallowed. The
device-side mirror, implemented by ModernTestPeripheral and asserted
here: no FE11 is emitted until all v4 chunks of the batch arrive.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from plaudsim.handshake import MARKER_PRE_HANDSHAKE, MARKER_RSA_MARKER
from sealed_support import SYNTHETIC_SN_SIGNATURE, ModernTestPeripheral, build_marker_frames


@pytest.mark.asyncio
async def test_no_fe11_until_all_v4_chunks_received() -> None:
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices

    devices = await TwoDevices.create_with_connection()
    peripheral = ModernTestPeripheral(devices[1])
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(s for s in peer.services if str(s.uuid).lower().startswith("00001910"))
    data = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    command = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb1"))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    v4 = build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE)
    assert len(v4) == 3
    await peer.write_value(command, v4[0], with_response=True)
    await peer.write_value(command, v4[1], with_response=True)
    await asyncio.sleep(0.3)
    assert responses == []  # partial batch: nothing solicited yet
    assert peripheral.sn_received is None
    await peer.write_value(command, v4[2], with_response=True)
    for _ in range(100):
        if responses:
            break
        await asyncio.sleep(0.05)
    assert len(responses) == 1
    assert int.from_bytes(responses[0][0:2], "little") == MARKER_RSA_MARKER
    assert peripheral.sn_received == SYNTHETIC_SN_SIGNATURE
