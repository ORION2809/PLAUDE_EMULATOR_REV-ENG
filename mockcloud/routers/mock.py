"""Mock-only administration surface under /_mock. Nothing here exists on any
Plaud host; it is how tests and operators inspect and steer the mock.
Requests to /_mock/* are excluded from the request log (HARNESS_POLICY) so
reading the log does not perturb it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from ..common import MockHTTPError, ctx_of
from ..oracle import MEETING_SCHEMA, load_meeting
from ..settings import MOCK_BUCKET

router = APIRouter(tags=["Mock admin"])


@router.get("/_mock/health")
async def health(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    return {
        "mock": True,
        "name": "plaud-harness mockcloud",
        "warning": "every value issued by this service is synthetic; it never contacts a real Plaud host",
        "now": ctx.now(),
        "persist": str(ctx.state.persist_path) if ctx.state.persist_path else None,
    }


@router.get("/_mock/log")
async def get_log(request: Request, format: str = Query("json")) -> Any:
    ctx = ctx_of(request)
    if format == "gor":
        return Response(content=ctx.state.log_as_gor(), media_type="text/plain; charset=utf-8")
    return {"count": len(ctx.state.request_log), "entries": ctx.state.request_log}


@router.api_route("/_mock/reset", methods=["GET", "POST"])
async def reset(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    await ctx.drain()
    ctx.state.reset()
    for type_, sn in ctx.settings.seed_devices:
        ctx.state.register_device(type_, sn, ctx.now())
    ctx.commit()
    return {"reset": True}


@router.get("/_mock/state")
async def state_summary(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    st = ctx.state
    return {
        "devices": {k: {"owner": v.owner, "bind_history": v.bind_history, "signed_by": v.signed_by}
                    for k, v in st.devices.items()},
        "tasks": {k: {"status": v.status, "source": v.source, "history": v.history} for k, v in st.tasks.items()},
        "objects": {k: {"size": len(v.data), "etag": v.etag, "acl": v.acl} for k, v in st.objects.items()},
        "multipart": {k: {"file_id": v.file_id, "parts": sorted(v.parts), "completed": v.completed is not None}
                      for k, v in st.multipart.items()},
        "meetings": st.meetings,
        "keypairs": sorted(st.keypairs),
        "signatures": len(st.signatures),
        "user_tokens": len(st.user_tokens),
        "partner_tokens": len(st.partner_tokens),
        "request_log": len(st.request_log),
    }


class RegisterDevice(BaseModel):
    sn: str
    type: str | None = None


@router.post("/_mock/devices")
async def register_device(request: Request, body: RegisterDevice) -> dict[str, Any]:
    """Register an SN in the mock registry (what only Plaud's manufacturing
    pipeline can do for real). `type` defaults from the SN prefix per the
    documented table (881 -> notepro, 882 -> notepins)."""
    ctx = ctx_of(request)
    type_ = body.type or {"881": "notepro", "882": "notepins"}.get(body.sn[:3])
    if not type_:
        raise MockHTTPError(422, "type is required for an SN whose prefix is not 881/882")
    rec = ctx.state.register_device(type_, body.sn, ctx.now())
    ctx.commit()
    return {"type": rec.type, "sn": rec.sn, "registered": True}


class RegisterMeeting(BaseModel):
    dir: str


@router.post("/_mock/meetings")
async def register_meeting(request: Request, body: RegisterMeeting) -> dict[str, Any]:
    """Register a meeting directory: every audio file it lists (device files,
    mix, stems) is fingerprinted by MD5 so that an upload of those exact bytes
    through presign/complete is recognised and answered with ground truth."""
    ctx = ctx_of(request)
    mdir = Path(body.dir).resolve()
    try:
        meeting = load_meeting(mdir)
    except Exception as exc:  # noqa: BLE001
        raise MockHTTPError(422, f"not a {MEETING_SCHEMA} directory: {exc}") from exc
    audio = meeting.get("audio") or {}
    candidates: list[str] = []
    if audio.get("mix_wav"):
        candidates.append(str(audio["mix_wav"]))
    candidates += [str(v) for v in (audio.get("stems") or {}).values()]
    candidates += [str(v) for v in (audio.get("device") or {}).values()]
    registered = []
    for rel in candidates:
        f = (mdir / rel).resolve()
        if not f.is_file():
            continue
        md5 = hashlib.md5(f.read_bytes()).hexdigest()
        ctx.state.meetings[md5] = {"dir": str(mdir), "file": str(f), "meeting_id": meeting.get("meeting_id")}
        registered.append({"file": rel, "md5": md5})
    ctx.commit()
    return {"meeting_id": meeting.get("meeting_id"), "dir": str(mdir), "registered": registered}


@router.get("/_mock/signing-key")
async def signing_key(request: Request) -> dict[str, Any]:
    """The mock cloud's sn-sign PUBLIC key (SPKI PEM) so a test or an emulator
    can verify a signature the way real firmware would verify Plaud's."""
    ctx = ctx_of(request)
    return {"algorithm": "RSA PKCS#1 v1.5 / SHA-256 (HARNESS_POLICY)", "public_key": ctx.signing_public_pem(),
            "bucket": MOCK_BUCKET}
