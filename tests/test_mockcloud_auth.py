"""Identity chain against the mock, and the proof that its user JWT is one the
shipped SDK would turn into a handshake token this project's emulator accepts.

    Basic(client_id:secret_key) -> partner token (+refresh)
      -> POST /open/partner/users/access-token {user_id, expires_in}
      -> user JWT {sub: "client_user_<id>", user_id, exp, client_id}
      -> SDK: strip "client_user_", drop "-"  (NiceBuildSdk.resolveHandshakeToken)
      -> k3 frame (emulator.plaudsim.handshake.build_k3_from_historical_id)
      -> PlaudPeripheral answers l3 status 0

Contract: build/docs-plaud-ai/openapi_auth.json; docs/architecture/cloud.md 2.1.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk  # noqa: E402

from plaudsim.handshake import (  # noqa: E402
    build_k3,
    build_k3_from_historical_id,
    normalize_historical_id,
    parse_handshake_request,
    parse_l3,
    token_width,
)
from plaudsim.profile import PlaudLifecycle, PlaudPeripheral, PlaudSyncTimeState  # noqa: E402

from test_mockcloud_helpers import (  # noqa: E402,F401 - no_outbound_network is an autouse fixture
    APP,
    BASIC,
    PARTNER,
    bearer,
    client,
    make_app,
    no_outbound_network,
    partner_token,
    user_token,
)
from mockcloud import MOCK_JWT_SECRET  # noqa: E402
from mockcloud import jwt as mjwt  # noqa: E402


# --- partner token ----------------------------------------------------------

@pytest.mark.asyncio
async def test_partner_token_exchange_shape_and_basic_auth_failures() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        j = await partner_token(c)
        assert set(j) == {"access_token", "refresh_token", "token_type", "expires_in"}
        assert j["token_type"] == "bearer" and j["expires_in"] == 3600
        claims = mjwt.decode(j["access_token"], MOCK_JWT_SECRET)
        assert claims["client_id"] == APP.client_id and claims["exp"] - claims["iat"] == 3600
        assert j["refresh_token"].startswith("mock_refresh_")
        url = f"{PARTNER}/oauth/partner/access-token"
        assert (await c.post(url)).status_code == 401
        wrong = base64.b64encode(f"{APP.client_id}:nope".encode()).decode()
        assert (await c.post(url, headers={"Authorization": f"Basic {wrong}"})).status_code == 401
        unknown = base64.b64encode(b"who:ever").decode()
        assert (await c.post(url, headers={"Authorization": f"Basic {unknown}"})).status_code == 401
        assert (await c.post(url, headers={"Authorization": "Basic not*base64"})).status_code == 401
        # the api_key is NOT the secret_key (transcription-api-overview.md)
        keyed = base64.b64encode(f"{APP.client_id}:{APP.api_key}".encode()).decode()
        assert (await c.post(url, headers={"Authorization": f"Basic {keyed}"})).status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_and_requires_basic_auth() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        first = await partner_token(c)
        url = f"{PARTNER}/oauth/partner/access-token/refresh"
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        # DOC-EXACT: refresh is also under basicAuth
        r = await c.post(url, headers=form, content=f"refresh_token={first['refresh_token']}")
        assert r.status_code == 401
        r = await c.post(url, headers={"Authorization": f"Basic {BASIC}", **form},
                         content=f"refresh_token={first['refresh_token']}")
        assert r.status_code == 200 and set(r.json()) == {"access_token", "refresh_token", "token_type", "expires_in"}
        second = r.json()
        assert second["access_token"] != first["access_token"]
        # HARNESS_POLICY rotation: the used refresh token is dead
        r = await c.post(url, headers={"Authorization": f"Basic {BASIC}", **form},
                         content=f"refresh_token={first['refresh_token']}")
        assert r.status_code == 401
        r = await c.post(url, headers={"Authorization": f"Basic {BASIC}", **form}, content="")
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_expired_partner_token_cannot_mint_user_tokens() -> None:
    app, _, clock = make_app()
    async with client(app) as c:
        pt = (await partner_token(c))["access_token"]
        clock.advance(3601)
        r = await c.post(f"{PARTNER}/open/partner/users/access-token", headers=bearer(pt),
                         json={"user_id": "harness-user-0001"})
        assert r.status_code == 401


# --- user token -------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_token_claims_and_validation() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        pt = (await partner_token(c))["access_token"]
        url = f"{PARTNER}/open/partner/users/access-token"
        # DOC-EXACT: user_id 6..120 chars
        assert (await c.post(url, headers=bearer(pt), json={"user_id": "short"})).status_code == 422
        assert (await c.post(url, headers=bearer(pt), json={"user_id": "x" * 121})).status_code == 422
        assert (await c.post(url, headers=bearer(pt), json={"user_id": "harness-user-0001", "expires_in": 0})).status_code == 422
        assert (await c.post(url, json={"user_id": "harness-user-0001"})).status_code == 401
        r = await c.post(url, headers=bearer(pt), json={"user_id": "harness-user-0001", "expires_in": 7200})
        assert r.status_code == 200
        j = r.json()
        assert set(j) == {"access_token", "token_type", "expires_in"}, "no refresh token for user tokens"
        assert j["token_type"] == "bearer" and j["expires_in"] == 7200
        claims = mjwt.decode(j["access_token"], MOCK_JWT_SECRET)
        assert claims["sub"].startswith("client_user_")
        assert claims["user_id"] == "harness-user-0001"
        assert claims["client_id"] == APP.client_id
        assert claims["exp"] - claims["iat"] == 7200
        # default lifetime is the documented 86400
        r = await c.post(url, headers=bearer(pt), json={"user_id": "harness-user-0001"})
        assert r.json()["expires_in"] == 86400
        # the same partner user_id always yields the same sub (stable identity)
        assert mjwt.decode(r.json()["access_token"], MOCK_JWT_SECRET)["sub"] == claims["sub"]
        # different users -> different subs
        other = await user_token(c, "other-user-000002")
        assert mjwt.peek_claims(other)["sub"] != claims["sub"]
        # a user token cannot mint user tokens
        assert (await c.post(url, headers=bearer(other), json={"user_id": "harness-user-0001"})).status_code == 401


def test_jwt_module_rejects_tampering() -> None:
    tok = mjwt.encode({"sub": "client_user_x", "exp": 10**10}, MOCK_JWT_SECRET)
    assert mjwt.decode(tok, MOCK_JWT_SECRET)["sub"] == "client_user_x"
    h, p, s = tok.split(".")
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(f"{h}.{p}.{s[:-2]}AA", MOCK_JWT_SECRET)
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(tok, "another secret")
    forged_payload = base64.urlsafe_b64encode(b'{"sub":"client_user_y","exp":9999999999}').rstrip(b"=").decode()
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(f"{h}.{forged_payload}.{s}", MOCK_JWT_SECRET)
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(tok, MOCK_JWT_SECRET, now=10**10 + 1)
    with pytest.raises(mjwt.JwtError):
        mjwt.decode("only.two", MOCK_JWT_SECRET)


# --- the handshake proof ----------------------------------------------------

def sdk_resolve_handshake_token(user_access_token: str) -> str:
    """Byte-for-byte what NiceBuildSdk.resolveHandshakeToken does
    (jadx NiceBuildSdk.java:138-160; ALL.txt:6440-6500): split on '.', decode
    segment 1 with android Base64 flags 9 (URL_SAFE | NO_PADDING), regex the
    `sub`, removePrefix("client_user_"), replace("-", "")."""
    parts = user_access_token.split(".")
    assert len(parts) == 3
    seg = parts[1]
    decoded = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8")
    m = re.search(r'"sub"\s*:\s*"([^"]+)"', decoded)
    assert m is not None
    sub = m.group(1)
    if sub.startswith("client_user_"):
        sub = sub[len("client_user_"):]
    return sub.replace("-", "")


@pytest.mark.asyncio
async def test_user_jwt_sub_derives_a_32_hex_token_the_emulator_accepts() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c, "harness-user-0001")

    # 1. The SDK's own derivation and the emulator's model of it agree.
    sub = mjwt.peek_claims(tok)["sub"]
    sdk_token = sdk_resolve_handshake_token(tok)
    assert sdk_token == normalize_historical_id(sub)
    # 2. It is exactly the 32-hex shape the ledger says real tokens have
    #    (protocol-ledger.md 4.5), so k3 neither pads nor truncates at pv >= 9.
    assert re.fullmatch(r"[0-9a-f]{32}", sdk_token), sdk_token
    assert token_width(9) == 32
    frame = build_k3_from_historical_id(sub, port_version=9)
    assert frame is not None
    assert frame == build_k3(sdk_token, 9)
    assert frame[6:] == sdk_token.encode("ascii"), "no '0' padding needed at width 32"
    assert parse_handshake_request(frame, port_version=9)["token"] == sdk_token
    # 3. A pv-7 device (width 16) sees the first 16 hex chars -- the SDK's own
    #    truncation rule (R7-S12 run 3) -- and the mock's sub survives it too.
    assert parse_handshake_request(build_k3_from_historical_id(sub, 7), 7)["token"] == sdk_token[:16]

    # 4. The emulator, over Bumble's virtual link, accepts the frame with l3 status 0.
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, port_version=9, synctime=PlaudSyncTimeState(stamp=1700000000))
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    await peer.write_value(command, frame, with_response=True)
    assert parse_l3(responses[0])["status"] == 0
    assert peripheral.lifecycle == PlaudLifecycle.HANDSHAKED
    assert peripheral.handshake_log[0]["token"] == sdk_token

    # 5. Falsifier: the SDK writes NOTHING for an empty sub (R7-S12 run 2).
    assert build_k3_from_historical_id("client_user_", 9) is None
