"""Layer 4 -> Layer 1: a user JWT minted by the mock cloud becomes the BLE
handshake token the shipped SDK would write, and the emulator accepts it.

    mock: Basic(client_id:secret) -> partner token
          -> POST users/access-token {user_id} -> user JWT {sub: "client_user_<uuid>"}
    SDK model (emulator.plaudsim.handshake):
          normalize_historical_id(sub)   strip "client_user_", drop "-"  -> 32 hex
          build_k3_from_historical_id    [01][01 00][02][00][stage][token padded/truncated]
    emulator over Bumble (real ATT discovery, MTU 255):
          k3 -> l3 status 0 -> HANDSHAKED
          then the SDK's own connect order: getSsn -> battery -> syncTime -> BOUND
          and a post-bind pull of the generator's device Ogg, byte-exact

Evidence class: EMULATOR_INTEGRATION_PROVEN for our own components.  The
derivation itself is BYTECODE_PROVEN + RUNTIME_PROVEN against the genuine SDK
(tests/test_r7_s12_k3_runtime.py, R7-S12 captures).  Accepting the token is the
emulator's HARNESS_POLICY (`accept_any_token`); this proves the plumbing from
Layer 4 to Layer 1, not that real hardware would accept a mock-minted token.
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
from pathlib import Path
from struct import pack

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from support import connect_like_the_sdk  # noqa: E402
from test_mockcloud_helpers import client, make_app, user_token  # noqa: E402

from plaudsim.filesync import parse_file_data_frame, parse_file_list_frame, parse_sync_head  # noqa: E402
from plaudsim.handshake import (  # noqa: E402
    build_k3,
    build_k3_from_historical_id,
    normalize_historical_id,
    parse_handshake_request,
    parse_l3,
    parse_x2,
    token_width,
)
from plaudsim.profile import (  # noqa: E402
    PlaudBatteryState,
    PlaudLifecycle,
    PlaudPeripheral,
    PlaudSyncTimeState,
)

from generator.export import DEVICE_FILES  # noqa: E402
from generator.testing import fixture_meeting  # noqa: E402
from mockcloud import MOCK_JWT_SECRET  # noqa: E402
from mockcloud import jwt as mjwt  # noqa: E402

pytestmark = pytest.mark.timeout(120)

SESSION = 0x0655A1B0
PAYLOAD = 240  # HARNESS_POLICY


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("id-gen"), "smoke", 3)


@pytest.fixture(scope="module")
def ogg(meeting) -> bytes:
    d, _ = meeting
    return (d / DEVICE_FILES["ogg_opus"]).read_bytes()


async def mint_user_jwt(user_id: str = "harness-user-0001") -> str:
    app, _, _ = make_app()
    async with client(app) as c:
        return await user_token(c, user_id)


def sdk_resolve_handshake_token(user_access_token: str) -> str:
    """NiceBuildSdk.resolveHandshakeToken, independently of the emulator's model:
    split on '.', base64url-decode segment 1 (no padding), regex the `sub`,
    removePrefix("client_user_"), replace("-", "")."""
    seg = user_access_token.split(".")[1]
    payload = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8")
    sub = re.search(r'"sub"\s*:\s*"([^"]+)"', payload).group(1)
    if sub.startswith("client_user_"):
        sub = sub[len("client_user_"):]
    return sub.replace("-", "")


# --- the pure derivation, both models agree ----------------------------------------------


@pytest.mark.asyncio
async def test_mock_jwt_sub_derives_the_k3_token_at_both_widths() -> None:
    tok = await mint_user_jwt()
    sub = mjwt.decode(tok, MOCK_JWT_SECRET)["sub"]
    sdk_token = sdk_resolve_handshake_token(tok)
    assert sdk_token == normalize_historical_id(sub)
    assert re.fullmatch(r"[0-9a-f]{32}", sdk_token), sdk_token
    # pv >= 9: 32-char field, the token fits exactly (no '0' padding, no truncation)
    f9 = build_k3_from_historical_id(sub, 9)
    assert f9 == build_k3(sdk_token, 9) and len(f9) == 6 + token_width(9) == 38
    assert parse_handshake_request(f9, 9)["token"] == sdk_token
    # pv < 9: 16-char field, the SDK truncates (R7-S12 run 3)
    f7 = build_k3_from_historical_id(sub, 7)
    assert len(f7) == 6 + 16 and parse_handshake_request(f7, 7)["token"] == sdk_token[:16]
    # stability: the same partner user always yields the same token
    assert sdk_resolve_handshake_token(await mint_user_jwt()) == sdk_token
    assert sdk_resolve_handshake_token(await mint_user_jwt("other-user-000002")) != sdk_token
    # falsifier: an empty sub makes the SDK write nothing at all (R7-S12 run 2)
    assert build_k3_from_historical_id("client_user_", 9) is None


# --- over Bumble: handshake, the SDK's connect order, a post-bind pull ------------------------


@pytest.mark.asyncio
async def test_k3_from_the_mock_jwt_is_accepted_and_the_session_reaches_bound(ogg) -> None:
    tok = await mint_user_jwt()
    sub = mjwt.decode(tok, MOCK_JWT_SECRET)["sub"]
    sdk_token = normalize_historical_id(sub)
    frame = build_k3_from_historical_id(sub, 9)
    assert frame is not None

    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device,
            port_version=9,
            synctime=PlaudSyncTimeState(stamp=1700000000),
            battery=PlaudBatteryState(charging=False, level=63),
            file_bytes=ogg,
            file_table=[{"session_id": SESSION, "file_size": len(ogg), "scene": 2, "attribute": 1}],
            data_payload_size=PAYLOAD,
        )
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    # first_handshake
    await peer.write_value(command, frame, with_response=True)
    l3 = parse_l3(responses[-1])
    assert l3["status"] == 0 and l3["port_version"] == 9
    assert peripheral.lifecycle == PlaudLifecycle.HANDSHAKED
    assert peripheral.handshake_log == [{
        "agent_value": 0, "stage": 0, "is_second": False,
        "token": sdk_token, "dev_token": None, "user_name": None,
    }]

    # handshake_get_ssn: checkSn compares with the ADVERTISED serial
    await peer.write_value(command, b"\x01\x02\x00", with_response=True)
    assert parse_x2(responses[-1])["ssn"] == peripheral.scan_fields.serial_number

    # battery (pv >= 5 gates connect success on it), then syncTime -> bleBind
    await peer.write_value(command, b"\x01\x09\x00", with_response=True)
    assert responses[-1][3:] == bytes([0x00, 63])
    await peer.write_value(
        command, PlaudSyncTimeState(stamp=1893456000, tz_hours=1, tz_mins=0).encode_request(), with_response=True
    )
    assert peripheral.lifecycle == PlaudLifecycle.BOUND
    assert peripheral.lifecycle_log[-2:] == ["handshaked", "bound"]

    # post-bind: list and pull the generator's file, byte-exact
    await peer.write_value(command, b"\x01\x1a\x00" + pack("<II", 0x655A1B00, 0) + b"\x00", with_response=True)
    listing = parse_file_list_frame(responses[-1], port_version=9)
    assert [e["session_id"] for e in listing["entries"]] == [SESSION]
    n = len(responses)
    await peer.write_value(command, b"\x01\x1c\x00" + pack("<III", SESSION, 0, 0), with_response=True)
    expected = 3 + -(-len(ogg) // PAYLOAD)
    for _ in range(4000):
        if len(responses) - n >= expected:
            break
        await asyncio.sleep(0.005)
    transfer = responses[n:]
    assert len(transfer) == expected
    assert parse_sync_head(transfer[0])["status"] == 0
    assert transfer[-2][5:9] == b"\xff\xff\xff\xff" and transfer[-1][:3] == b"\x01\x1d\x00"
    body = b"".join(parse_file_data_frame(f, port_version=9)["payload"] for f in transfer[1:-2])
    assert body == ogg
    await devices.connections[0].disconnect()


@pytest.mark.asyncio
async def test_pv7_device_sees_the_truncated_token_and_still_accepts() -> None:
    tok = await mint_user_jwt()
    sub = mjwt.decode(tok, MOCK_JWT_SECRET)["sub"]
    frame = build_k3_from_historical_id(sub, 7)
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, port_version=7)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, frame, with_response=True)
    assert parse_l3(responses[-1])["status"] == 0
    assert peripheral.handshake_log[0]["token"] == normalize_historical_id(sub)[:16]
    await devices.connections[0].disconnect()


# --- falsifiers: the acceptance is the device's decision -------------------------------------------


@pytest.mark.asyncio
async def test_refusing_or_failing_peripheral_does_not_reach_handshaked() -> None:
    tok = await mint_user_jwt()
    sub = mjwt.decode(tok, MOCK_JWT_SECRET)["sub"]
    frame = build_k3_from_historical_id(sub, 9)

    # accept_any_token=False: the request is logged as rejected and NOTHING is answered
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, port_version=9, accept_any_token=False)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, frame, with_response=True)
    await asyncio.sleep(0.05)
    assert responses == []
    assert peripheral.lifecycle != PlaudLifecycle.HANDSHAKED
    assert any(e["direction"] == "error" and "refuse" in e["reason"] for e in peripheral.packet_log)
    await devices.connections[0].disconnect()

    # handshake_status=1: an l3 with a non-zero status, which the SDK turns into a failed connect
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, port_version=9, handshake_status=1)
    )
    responses = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, frame, with_response=True)
    assert parse_l3(responses[-1])["status"] == 1
    assert peripheral.lifecycle != PlaudLifecycle.HANDSHAKED
    await devices.connections[0].disconnect()
