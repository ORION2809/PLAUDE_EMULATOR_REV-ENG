"""Background transcription worker.

Walks the documented Celery state names PENDING -> STARTED -> SUCCESS (or
FAILURE) with settings.task_step_s between hops. The state set is DOC-EXACT
(openapi_transcription.json TranscriptionStatus); that it is Celery's set and
that a real task would visit RECEIVED/PROGRESS is INFERRED (cloud.md 5.1). The
mock visits exactly the three the task brief asks for.

The worker NEVER fetches a URL. `file_url` is resolved locally:
  * a URL whose path is /s3/<bucket>/<key> -> this mock's object store
    (signature and expiry are checked like a real presigned GET would be);
  * a file:// URL inside settings.local_file_roots -> read from disk;
  * anything else -> FAILURE with an explicit "no outbound HTTP" message.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .common import MockContext, MockHTTPError
from .oracle import (
    find_meeting_dir,
    load_meeting,
    placeholder_result,
    result_from_meeting,
    try_pipeline_oracle,
)
from .state import TranscriptionTask

NO_NETWORK_MESSAGE = (
    "external URL not fetched: plaud-harness mockcloud performs no outbound HTTP; "
    "pass a DownloadUrl from this mock's complete-upload or a file:// path under an allowed root"
)


@dataclass
class Resolved:
    data: bytes | None
    meeting_dir: Path | None
    source: str
    error: str | None = None


def resolve_file_url(ctx: MockContext, file_url: str) -> Resolved:
    parsed = urlparse(file_url)
    path = unquote(parsed.path)
    if parsed.scheme in ("http", "https") and path.startswith("/s3/"):
        _, _, rest = path.partition("/s3/")
        bucket, _, key = rest.partition("/")
        obj = ctx.state.get_object(bucket, key)
        if obj is None:
            return Resolved(None, None, "objectstore", "NoSuchKey: the DownloadUrl does not exist in the mock store")
        if obj.acl != "public-read":
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                ctx.check_signed_query("GET", bucket, key, q)
            except MockHTTPError as exc:
                return Resolved(None, None, "objectstore", exc.message)
        md5 = hashlib.md5(obj.data).hexdigest()
        reg = ctx.state.meetings.get(md5)
        mdir = Path(reg["dir"]) if reg else None
        return Resolved(obj.data, mdir, "objectstore" + ("+meeting" if mdir else ""))
    if parsed.scheme == "file":
        local = Path(path).resolve()
        roots = [Path(r).resolve() for r in ctx.settings.local_file_roots]
        if not any(local == r or r in local.parents for r in roots):
            return Resolved(None, None, "file", "file:// path is outside the mock's local_file_roots")
        if not local.is_file():
            return Resolved(None, None, "file", "file:// path does not exist")
        data = local.read_bytes()
        mdir = find_meeting_dir(local)
        if mdir is None:
            reg = ctx.state.meetings.get(hashlib.md5(data).hexdigest())
            mdir = Path(reg["dir"]) if reg else None
        return Resolved(data, mdir, "file" + ("+meeting" if mdir else ""))
    return Resolved(None, None, "external", NO_NETWORK_MESSAGE)


def _set_status(ctx: MockContext, task: TranscriptionTask, status: str) -> None:
    task.status = status
    task.updated_at = ctx.now()
    task.history.append((status, task.updated_at))


async def run_task(ctx: MockContext, task: TranscriptionTask) -> None:
    step = max(0.0, float(ctx.settings.task_step_s))
    try:
        await asyncio.sleep(step)
        _set_status(ctx, task, "STARTED")
        resolved = resolve_file_url(ctx, task.file_url)
        task.source = resolved.source
        await asyncio.sleep(step)
        if resolved.error is not None:
            task.message = resolved.error
            task.data = {}
            _set_status(ctx, task, "FAILURE")
            return
        assert resolved.data is not None
        if resolved.meeting_dir is not None:
            meeting = load_meeting(resolved.meeting_dir)
            segs = try_pipeline_oracle(resolved.meeting_dir)
            task.data = result_from_meeting(meeting, task.params, segs)
            task.source = (task.source or "") + ("+pipeline-oracle" if segs is not None else "+meeting.json")
        else:
            task.data = placeholder_result(resolved.data, task.params)
        _set_status(ctx, task, "SUCCESS")
    except Exception as exc:  # noqa: BLE001 - surface as a terminal FAILURE, never crash the loop
        task.message = f"{type(exc).__name__}: {exc}"
        task.data = {}
        _set_status(ctx, task, "FAILURE")
    finally:
        ctx.commit()


def schedule(ctx: MockContext, task: TranscriptionTask) -> asyncio.Task[None]:
    handle = asyncio.create_task(run_task(ctx, task))
    ctx.worker_tasks.add(handle)
    handle.add_done_callback(ctx.worker_tasks.discard)
    return handle
