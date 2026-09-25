"""Authentication API -- build/docs-plaud-ai/openapi_auth.json (DOC-EXACT
paths, request bodies, response shapes) and plaud-embedded_auth-api-overview.md.

Identity chain (docs/architecture/cloud.md section 2.1):

    Basic(client_id:secret_key) -> partner token (3600 s + refresh)
      -> POST /open/partner/users/access-token {user_id, expires_in}
      -> user JWT with claims sub="client_user_<id>", user_id, exp, client_id

The SDK derives the BLE handshake token from `sub` (strip "client_user_", drop
every "-": NiceBuildSdk.resolveHandshakeToken, jadx NiceBuildSdk.java:138-160;
recovery path PlaudDeviceAgent ALL.txt:2678-2699). The mock's `sub` is
"client_user_" + a UUID so that the derivation yields exactly 32 hex characters,
which is the token width k3 uses for portVersion >= 9 (protocol-ledger.md 4.5
"32 hex characters") -- see tests/test_mockcloud_auth.py for the proof against
emulator.plaudsim.handshake.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import jwt
from ..common import (
    MockContext,
    MockHTTPError,
    ctx_of,
    new_id,
    require_partner_app_basic,
    require_partner_token,
)
from ..settings import PartnerApp

router = APIRouter(tags=["Authentication API"])

PARTNER_PREFIX = "/developer/api"

#: DOC-EXACT: `sub` is `client_user_<id>` (cloud.md 2.1; the bytecode strips
#: exactly this prefix). What `<id>` is on the real cloud is UNKNOWN; the mock
#: uses a UUID5 of (client_id, user_id) so re-minting is stable (HARNESS_POLICY).
SUB_PREFIX = "client_user_"
_SUB_NAMESPACE = uuid.UUID("6f1c8f8e-5a0e-4a7d-9c3e-000000000000")  # fixed SYNTHETIC namespace


def sub_for(client_id: str, user_id: str) -> str:
    return SUB_PREFIX + str(uuid.uuid5(_SUB_NAMESPACE, f"mockcloud:{client_id}:{user_id}"))


def issue_partner_tokens(ctx: MockContext, app: PartnerApp) -> dict[str, Any]:
    """PartnerTokenResponse: access_token, refresh_token, token_type "bearer",
    expires_in 3600 -- DOC-EXACT (openapi_auth.json). Claims are HARNESS_POLICY."""
    now = int(ctx.now())
    ttl = ctx.settings.partner_token_ttl_s
    jti = new_id("mockpt_")
    claims = {"token_use": "partner", "client_id": app.client_id, "iat": now, "exp": now + ttl, "jti": jti}
    access = jwt.encode(claims, ctx.settings.jwt_secret)
    refresh = new_id("mock_refresh_", 16)
    ctx.state.partner_tokens[jti] = {"client_id": app.client_id, "exp": now + ttl}
    ctx.state.refresh_tokens[refresh] = {"client_id": app.client_id, "issued_at": now}
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": ttl}


@router.post(f"{PARTNER_PREFIX}/oauth/partner/access-token")
async def create_partner_access_token(request: Request) -> dict[str, Any]:
    """DOC-EXACT: Basic auth, form content type, empty body
    (user-token-script.ts:22-28 sends no body at all)."""
    ctx = ctx_of(request)
    app = require_partner_app_basic(request)
    return issue_partner_tokens(ctx, app)


@router.post(f"{PARTNER_PREFIX}/oauth/partner/access-token/refresh")
async def refresh_partner_access_token(request: Request) -> dict[str, Any]:
    """DOC-EXACT: Basic auth + form field `refresh_token`. Rotation of the
    refresh token (the old one stops working) is HARNESS_POLICY; the real
    behaviour is UNKNOWN."""
    ctx = ctx_of(request)
    app = require_partner_app_basic(request)
    body = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(body, keep_blank_values=True)
    refresh = (form.get("refresh_token") or [""])[0].strip()
    if not refresh:
        raise MockHTTPError(422, "refresh_token is required")
    rec = ctx.state.refresh_tokens.pop(refresh, None)
    if rec is None or rec["client_id"] != app.client_id:
        raise MockHTTPError(401, "unknown, rotated, or foreign refresh_token")
    return issue_partner_tokens(ctx, app)


class UserTokenRequest(BaseModel):
    #: DOC-EXACT: minLength 6, maxLength 120 (openapi_auth.json).
    user_id: str = Field(min_length=6, max_length=120)
    #: DOC-EXACT field; the default 86400 is the documented example. Whether
    #: the cloud honours other values is UNKNOWN; the mock honours any > 0.
    expires_in: int | None = Field(default=None, gt=0)


@router.post(f"{PARTNER_PREFIX}/open/partner/users/access-token")
async def create_user_access_token(request: Request, body: UserTokenRequest) -> dict[str, Any]:
    """UserTokenResponse: access_token (JWT), token_type, expires_in; no refresh
    token for user tokens -- DOC-EXACT (openapi_auth.json)."""
    ctx = ctx_of(request)
    partner = require_partner_token(request)
    client_id = str(partner["client_id"])
    now = int(ctx.now())
    ttl = body.expires_in or ctx.settings.user_token_default_ttl_s
    jti = new_id("mockut_")
    sub = sub_for(client_id, body.user_id)
    # Claim set: sub / user_id / exp / client_id are what the template reads
    # (JwtUtils.kt:16-33) and cloud.md 2.1 lists; iat, jti, token_use are
    # HARNESS_POLICY extras. `user_id` echoes the partner's id (INFERRED from
    # the field name; the template falls back to sub when it is absent).
    claims = {
        "sub": sub,
        "user_id": body.user_id,
        "client_id": client_id,
        "exp": now + ttl,
        "iat": now,
        "jti": jti,
        "token_use": "user",
    }
    ctx.state.user_tokens[jti] = {"sub": sub, "user_id": body.user_id, "client_id": client_id, "exp": now + ttl}
    return {"access_token": jwt.encode(claims, ctx.settings.jwt_secret), "token_type": "bearer", "expires_in": ttl}
