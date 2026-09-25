"""Transcription API -- build/docs-plaud-ai/openapi_transcription.json and
openapi_transcription-model.json (DOC-EXACT paths, headers, status enum,
`task_exec_` id prefix, `{transcription_id, status, data}` envelope).

The two spec files disagree on the parameter set (`hotwords` vs `model`,
cloud.md 5.1); the mock accepts the union and ignores what it cannot honour.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..common import MockHTTPError, ctx_of, new_id, require_client_keys
from ..worker import schedule
from ..state import TranscriptionTask

router = APIRouter(tags=["Transcription API"])
PREFIX = "/developer/api/open/partner/ai/transcriptions"

#: DOC-EXACT enum.
STATUSES = ("PENDING", "RECEIVED", "STARTED", "PROGRESS", "SUCCESS", "FAILURE", "REVOKED")
TERMINAL = ("SUCCESS", "FAILURE", "REVOKED")


class TranscriptionRequest(BaseModel):
    file_url: str
    params: dict[str, Any] | None = None


def _envelope(task: TranscriptionTask) -> dict[str, Any]:
    out: dict[str, Any] = {"transcription_id": task.transcription_id, "status": task.status, "data": task.data}
    if task.message is not None:
        # HARNESS_POLICY: a top-level `message` on FAILURE. The Android
        # template reads exactly `resp.optString("message", status)` on
        # FAILURE/REVOKED (TranscriptionManager.kt:299) -- INFERRED shape.
        out["message"] = task.message
    return out


@router.post(f"{PREFIX}/")
async def create_transcription(request: Request, body: TranscriptionRequest) -> dict[str, Any]:
    """200 {transcription_id: "task_exec_...", status: "PENDING", data: {}} --
    DOC-EXACT. Auth: X-Client-Id + X-Client-Api-Key (401 on failure,
    HARNESS_POLICY code). The worker is scheduled immediately."""
    ctx = ctx_of(request)
    app = require_client_keys(request)
    if not body.file_url.strip():
        raise MockHTTPError(422, "file_url is required")
    now = ctx.now()
    task = TranscriptionTask(
        transcription_id=new_id("task_exec_"),
        status="PENDING",
        file_url=body.file_url,
        params=body.params or {},
        client_id=app.client_id,
        created_at=now,
        updated_at=now,
        history=[("PENDING", now)],
    )
    ctx.state.tasks[task.transcription_id] = task
    schedule(ctx, task)
    return _envelope(task)


@router.get(f"{PREFIX}/{{transcription_id}}")
async def get_transcription(request: Request, transcription_id: str) -> dict[str, Any]:
    """{transcription_id, status, data} -- DOC-EXACT; data populated on
    SUCCESS. Unknown id -> 404 (HARNESS_POLICY; the docs list no error).
    Tasks are visible only to the client_id that created them (HARNESS_POLICY)."""
    ctx = ctx_of(request)
    app = require_client_keys(request)
    task = ctx.state.tasks.get(transcription_id)
    if task is None or task.client_id != app.client_id:
        raise MockHTTPError(404, "unknown transcription_id")
    return _envelope(task)
