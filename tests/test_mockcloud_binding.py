"""Device Binding API against the mock: the documented 200/403/404 semantics,
the tri-state GET, bind_history ordering and per-event duplicates.

Contract: build/docs-plaud-ai/openapi_binding.json and
plaud-embedded_device-binding-api-overview.md. Where the mock chooses
(non-owner unbind -> 403, type/sn registry key, auto-register), the test says so.
"""

from __future__ import annotations

import pytest

from test_mockcloud_helpers import (
    PARTNER,
    SN_NOTEPINS,
    SN_NOTEPRO,
    bearer,
    client,
    make_app,
    user_token,
)

SDK = f"{PARTNER}/open/partner/sdk"


@pytest.mark.asyncio
async def test_bind_requires_a_user_token_and_rejects_partner_tokens() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        r = await c.post(f"{SDK}/bind", json={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.status_code == 401 and r.json()["code"] == 401
        # A PARTNER token is not a user token: the binding spec's bearerAuth is
        # "a User Token" (openapi_binding.json securitySchemes).
        from test_mockcloud_helpers import partner_token
        pt = (await partner_token(c))["access_token"]
        r = await c.post(f"{SDK}/bind", headers=bearer(pt), json={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.status_code == 401
        # Garbage bearer
        r = await c.post(f"{SDK}/bind", headers=bearer("eyJ.not.a-jwt"), json={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_unknown_sn_is_a_bare_404_on_all_three_endpoints() -> None:
    """DOC-EXACT: "an unknown serial number returns a bare 404 with no error body"."""
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        ref = {"type": "notepro", "sn": "8819999999999999"}
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json=ref)
        assert (r.status_code, r.content) == (404, b"")
        r = await c.post(f"{SDK}/unbind", headers=bearer(tok), json=ref)
        assert (r.status_code, r.content) == (404, b"")
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params=ref)
        assert (r.status_code, r.content) == (404, b"")
        # (type, sn) is the registry key: a known SN under the wrong type is unknown.
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepins", "sn": SN_NOTEPRO})
        assert (r.status_code, r.content) == (404, b"")


@pytest.mark.asyncio
async def test_auto_register_policy_turns_404_into_a_bind() -> None:
    app, _, _ = make_app(auto_register_unknown_sn=True)
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepro", "sn": "8815550000000001"})
        assert r.status_code == 200 and r.json()["is_bind"] is True


@pytest.mark.asyncio
async def test_bind_is_idempotent_for_the_owner_but_records_every_event() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c, "owner-user-000001")
        ref = {"type": "notepro", "sn": SN_NOTEPRO}
        for _ in range(3):
            r = await c.post(f"{SDK}/bind", headers=bearer(tok), json=ref)
            assert r.status_code == 200
            assert r.json() == {"type": "notepro", "sn": SN_NOTEPRO, "is_bind": True}
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params=ref)
        body = r.json()
        assert body["is_bind"] is True
        # "One entry is recorded per bind event, so the same client ID can
        # appear more than once" (openapi_binding.json) -- the template saw
        # 50+ duplicates live (DeviceManager.kt:1091-1094).
        assert len(body["bind_history"]) == 3
        assert len(set(body["bind_history"])) == 1
        entry = body["bind_history"][0]
        # The recorded id is the UUID part of the JWT sub (openapi example shape).
        assert not entry.startswith("client_user_")
        assert len(entry.replace("-", "")) == 32


@pytest.mark.asyncio
async def test_other_owner_gets_403_with_the_documented_error_body() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        a = await user_token(c, "owner-user-000001")
        b = await user_token(c, "other-user-000002")
        ref = {"type": "notepro", "sn": SN_NOTEPRO}
        assert (await c.post(f"{SDK}/bind", headers=bearer(a), json=ref)).status_code == 200
        r = await c.post(f"{SDK}/bind", headers=bearer(b), json=ref)
        assert r.status_code == 403
        assert r.json() == {"code": 403, "message": "device already bound to another account"}
        # A different user of the SAME partner app is still "another account".
        r = await c.get(f"{SDK}/binding", headers=bearer(b), params=ref)
        assert r.json()["is_bind"] is True
        # HARNESS_POLICY: a non-owner cannot unbind either.
        r = await c.post(f"{SDK}/unbind", headers=bearer(b), json=ref)
        assert r.status_code == 403
        # ...and the owner still owns it afterwards.
        r = await c.get(f"{SDK}/binding", headers=bearer(a), params=ref)
        assert r.json()["is_bind"] is True


@pytest.mark.asyncio
async def test_unbind_then_rebind_by_a_new_owner_orders_history_newest_first() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        a = await user_token(c, "owner-user-000001")
        b = await user_token(c, "other-user-000002")
        ref = {"type": "notepro", "sn": SN_NOTEPRO}
        assert (await c.post(f"{SDK}/bind", headers=bearer(a), json=ref)).status_code == 200
        r = await c.post(f"{SDK}/unbind", headers=bearer(a), json=ref)
        assert r.status_code == 200 and r.json() == {"type": "notepro", "sn": SN_NOTEPRO, "is_bind": False}
        # idempotent unbind
        r = await c.post(f"{SDK}/unbind", headers=bearer(a), json=ref)
        assert r.status_code == 200 and r.json()["is_bind"] is False
        # unbound: false (not null -- it HAS been bound before)
        r = await c.get(f"{SDK}/binding", headers=bearer(b), params=ref)
        assert r.json()["is_bind"] is False
        assert (await c.post(f"{SDK}/bind", headers=bearer(b), json=ref)).status_code == 200
        r = await c.get(f"{SDK}/binding", headers=bearer(a), params=ref)
        hist = r.json()["bind_history"]
        assert len(hist) == 2 and hist[0] != hist[1], "newest first: B then A"
        from mockcloud.routers.auth import sub_for
        from mockcloud import DEFAULT_PARTNER_APP as APP
        assert hist == [
            sub_for(APP.client_id, "other-user-000002").removeprefix("client_user_"),
            sub_for(APP.client_id, "owner-user-000001").removeprefix("client_user_"),
        ]


@pytest.mark.asyncio
async def test_never_bound_device_reports_null() -> None:
    """DOC-EXACT tri-state: null = signed but never bound."""
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params={"type": "notepins", "sn": SN_NOTEPINS})
        assert r.status_code == 200
        assert r.json() == {"is_bind": None, "bind_history": []}
        # after sn-sign it is still "signed but never bound" -> null
        r = await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json={"type": "notepins", "sn": SN_NOTEPINS})
        assert r.status_code == 200
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params={"type": "notepins", "sn": SN_NOTEPINS})
        assert r.json()["is_bind"] is None


@pytest.mark.asyncio
async def test_missing_query_or_body_fields_are_422() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params={"type": "notepro"})
        assert r.status_code == 422 and r.json()["code"] == 422
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json={"sn": SN_NOTEPRO})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_expired_user_token_is_rejected() -> None:
    app, _, clock = make_app()
    async with client(app) as c:
        tok = await user_token(c, expires_in=60)
        ref = {"type": "notepro", "sn": SN_NOTEPRO}
        assert (await c.post(f"{SDK}/bind", headers=bearer(tok), json=ref)).status_code == 200
        clock.advance(61)
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json=ref)
        assert r.status_code == 401 and "expired" in r.json()["message"]
