"""Transcription API -- build/docs-plaud-ai/openapi_transcription.json and
openapi_transcription-model.json (DOC-EXACT paths, headers, status enum,
`task_exec_` id prefix, `{transcription_id, status, data}` envelope).

The two spec files disagree on the parameter set (`hotwords` vs `model`,
cloud.md 5.1); the mock accepts the union and ignores what it cannot honour.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from ..common import MockHTTPError, ctx_of, new_id, require_client_keys
from ..worker import TERMINAL, schedule  # noqa: F401 - TERMINAL re-exported
from ..state import TranscriptionTask

router = APIRouter(tags=["Transcription API"])
PREFIX = "/developer/api/open/partner/ai/transcriptions"

#: DOC-EXACT enum.
STATUSES = ("PENDING", "RECEIVED", "STARTED", "PROGRESS", "SUCCESS", "FAILURE", "REVOKED")


# Parameter objects: field names and JSON types are DOC-EXACT
# (openapi_transcription.json TranscriptionRequest.params, :181-229, plus
# `transcribe.model` from openapi_transcription-model.json:186-190). A value of
# the wrong JSON type is refused at submit with 422 -- HARNESS_POLICY status,
# the docs list no error responses (review finding MC-11: it used to be
# accepted and fail later with a Python exception text as the task message).
# Scalars are strict (a JSON string for `string`, a JSON true/false for
# `boolean`: "yes" or 1 is a 422, not a coercion). Unknown keys are allowed and
# kept (extra="allow"): the two spec files disagree on the parameter set, so
# the mock accepts the union and more.
class _Params(BaseModel):
    model_config = ConfigDict(extra="allow")


class TranscribeParams(_Params):
    language: StrictStr | None = None
    model: StrictStr | None = None
    detection_level: StrictStr | None = None


class VadParams(_Params):
    decode_silence: StrictBool | None = None


class DiarizationParams(_Params):
    enabled: StrictBool | None = None
    return_embedding: StrictBool | None = None


class TranscriptionParams(_Params):
    transcribe: TranscribeParams | None = None
    vad: VadParams | None = None
    diarization: DiarizationParams | None = None
    #: DOC-EXACT type: "comma separated string of hotwords".
    hotwords: StrictStr | None = None


class TranscriptionRequest(BaseModel):
    file_url: str
    params: TranscriptionParams | None = None


def _envelope(task: TranscriptionTask) -> dict[str, Any]:
    out: dict[str, Any] = {"transcription_id": task.transcription_id, "status": task.status, "data": task.data}
    if task.message is not None:
        # HARNESS_POLICY: a top-level `message` on FAILURE. The Android
        # template reads exactly `resp.optString("message", status)` on
        # FAILURE/REVOKED (TranscriptionManager.kt:231) -- INFERRED shape.
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
        # What the client sent (unset fields omitted, unknown keys kept).
        params=body.params.model_dump(exclude_unset=True) if body.params is not None else {},
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
