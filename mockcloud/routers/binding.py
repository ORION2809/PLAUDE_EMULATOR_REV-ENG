"""Device Binding API -- build/docs-plaud-ai/openapi_binding.json and
plaud-embedded_device-binding-api-overview.md (DOC-EXACT unless noted).

Registry key is (type, sn): "type ... is the cloud registry key together with
the SN" (docs/architecture/cloud.md section 3, INFERRED from the docs'
"derived from the SN prefix" wording). A known SN queried under the wrong type
is therefore an unknown device (404) -- HARNESS_POLICY reading.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..common import MockContext, MockHTTPError, ctx_of, require_user_token
from ..state import DeviceRecord
from .auth import SUB_PREFIX

router = APIRouter(tags=["Device Binding API"])
PREFIX = "/developer/api/open/partner/sdk"

#: DOC-EXACT prefix table (openapi_binding.json DeviceRef.type description).
SN_PREFIX_TO_TYPE = {"881": "notepro", "882": "notepins"}


class DeviceRef(BaseModel):
    type: str
    sn: str


def history_entry(sub: str) -> str:
    """What one bind event records. The OpenAPI example lists bare UUIDs
    ("b7c1f0a2-4d5e-..."), while cloud.md 2.1 says "the same client_user_ ids
    come back in bind_history" -- a recorded contradiction. The mock follows
    the OpenAPI example (INFERRED); both forms normalise to the same handshake
    token in the SDK (removePrefix is a no-op on a bare id)."""
    return sub[len(SUB_PREFIX):] if sub.startswith(SUB_PREFIX) else sub


def lookup_device(ctx: MockContext, type_: str, sn: str, *, register: bool) -> DeviceRecord:
    """Bare 404 for an unknown SN -- DOC-EXACT ("an unknown serial number
    returns a bare 404 with no error body", openapi_binding.json). With the
    auto-register policy the device is created instead (HARNESS_POLICY)."""
    rec = ctx.state.device(type_, sn)
    if rec is None:
        if register and ctx.settings.auto_register_unknown_sn:
            rec = ctx.state.register_device(type_, sn, ctx.now())
        else:
            raise MockHTTPError(404, bare=True)
    return rec


def _bind_response(rec: DeviceRecord) -> dict[str, Any]:
    return {"type": rec.type, "sn": rec.sn, "is_bind": rec.owner is not None}


@router.post(f"{PREFIX}/bind")
async def bind_device(request: Request, body: DeviceRef) -> dict[str, Any]:
    """200 BindResponse; 403 Error when bound to ANOTHER account; 404 unknown.
    Same-owner rebind is idempotent (DOC-EXACT) but still records one
    bind_history entry per call: "one entry is recorded per bind event"
    (openapi_binding.json) and the template's live observation "50+ duplicates
    of one id" (DeviceManager.kt:1091-1094, OFFICIAL_SOURCE)."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    sub = str(claims["sub"])
    rec = lookup_device(ctx, body.type, body.sn, register=True)
    if rec.owner is not None and rec.owner != sub:
        # DOC-EXACT status and message example.
        raise MockHTTPError(403, "device already bound to another account", code=403)
    rec.owner = sub
    rec.bind_history.insert(0, history_entry(sub))
    rec.updated_at = ctx.now()
    return _bind_response(rec)


@router.post(f"{PREFIX}/unbind")
async def unbind_device(request: Request, body: DeviceRef) -> dict[str, Any]:
    """200 with is_bind false, idempotent (DOC-EXACT); 404 unknown. A caller
    that is not the owner gets 403 (HARNESS_POLICY: the docs only say "that
    owner must unbind it"; what the cloud does for a stranger is UNKNOWN)."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    sub = str(claims["sub"])
    rec = lookup_device(ctx, body.type, body.sn, register=False)
    if rec.owner is not None and rec.owner != sub:
        raise MockHTTPError(403, "device is bound to another account", code=403)
    rec.owner = None
    rec.updated_at = ctx.now()
    return _bind_response(rec)


@router.get(f"{PREFIX}/binding")
async def get_device_binding(
    request: Request,
    type: str = Query(..., description="notepro | notepins"),
    sn: str = Query(...),
) -> dict[str, Any]:
    """BindingResponse {is_bind: true|false|null, bind_history: [...newest first]}.

    Tri-state (DOC-EXACT wording): true = bound (to another account, from the
    recovering caller's point of view), false = unbound, null = "signed but
    never bound". The mock models the device-side reading: true iff currently
    bound, false iff it was bound before and is not now, null iff it has never
    had a bind event. Whether the real flag depends on WHO asks is UNKNOWN.
    History is not client-scoped (template comment, PlaudAPIService.swift:
    "bind_history is NOT client-scoped").
    """
    ctx = ctx_of(request)
    require_user_token(request)
    rec = lookup_device(ctx, type, sn, register=False)
    if rec.owner is not None:
        is_bind: bool | None = True
    elif rec.bind_history:
        is_bind = False
    else:
        is_bind = None
    return {"is_bind": is_bind, "bind_history": list(rec.bind_history)}

