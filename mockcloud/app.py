"""create_app(): the FastAPI application, request logging middleware, error
rendering. See mockcloud/__init__.py for the evidence-class vocabulary.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, MutableMapping

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .common import MockContext, MockHTTPError, new_id
from .routers import auth, binding, files, mock, objectstore, sdkinternal, transcription
from .settings import MockSettings
from .state import MockState
from .worker import resume_pending

log = logging.getLogger("mockcloud")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

#: HARNESS_POLICY: header names NOT recorded into the log. Everything the mock
#: issues is synthetic, so Authorization values are kept. `host` IS kept (a
#: replayed request needs one); content-length / transfer-encoding are dropped
#: because the GoReplay export recomputes Content-Length from the logged body
#: (state.MockState.log_as_gor).
LOGGED_HEADER_SKIP = {"content-length", "transfer-encoding", "connection", "accept-encoding", "user-agent"}


def _json_safe(value: Any) -> Any:
    """A value json.dumps(allow_nan=False) accepts: bytes and non-finite floats
    become their repr, containers are walked, anything else unknown is repr'd."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else repr(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return repr(value)


def validation_detail(errors: Any) -> list[dict[str, Any]]:
    """422 `detail` built from the JSON-safe parts of pydantic's errors only.

    Passing exc.errors() through verbatim crashed the handler (HTTP 500) when
    pydantic's `input` was bytes (a non-JSON Content-Type) or NaN (review
    finding MC-2). `input` is kept only in a JSON-safe form, truncated.
    """
    out: list[dict[str, Any]] = []
    for e in errors:
        item: dict[str, Any] = {
            "type": str(e.get("type", "")),
            "loc": [x if isinstance(x, (int, str)) else str(x) for x in e.get("loc", ())],
            "msg": str(e.get("msg", "")),
        }
        if "input" in e:
            safe = _json_safe(e["input"])
            text = json.dumps(safe, allow_nan=False)
            item["input"] = safe if len(text) <= 256 else text[:256] + "..."
        out.append(item)
    return out


class RequestLogMiddleware:
    """Pure-ASGI logger: method, path, query, status, X-Request-Id, latency,
    headers and (capped) body -- enough for GET /_mock/log?format=gor."""

    def __init__(self, app: Any, ctx: MockContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Restored in-flight tasks need a running loop to be rescheduled; the
            # lifespan hook does it under uvicorn, this covers transports that
            # never send lifespan events (httpx.ASGITransport). Idempotent.
            resume_pending(self.ctx)
        if scope["type"] != "http" or scope["path"].startswith("/_mock"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or new_id("mockreq_", 6)
        cap = self.ctx.settings.log_body_cap
        keep_body = not scope["path"].startswith("/s3/")
        holder: dict[str, Any] = {"status": None, "body": bytearray(), "size": 0, "error": None}

        async def receive_wrapper() -> MutableMapping[str, Any]:
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"")
                holder["size"] += len(chunk)
                if keep_body and len(holder["body"]) < cap:
                    holder["body"] += chunk[: cap - len(holder["body"])]
            return msg

        async def send_wrapper(msg: MutableMapping[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                holder["status"] = msg["status"]
                hdrs = list(msg.get("headers", [])) + [(b"x-request-id", request_id.encode("latin-1"))]
                msg = {**msg, "headers": hdrs}
            await send(msg)

        t0 = time.perf_counter()
        ts = time.time()
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - HARNESS_POLICY: a logged 500, never an unlogged crash
            # Starlette's ServerErrorMiddleware sits OUTSIDE this wrapper, so an
            # exception escaping here used to become a 500 with no X-Request-Id
            # and a log entry with status null (review finding MC-2). Answer it
            # here instead, and keep the traceback in the server log.
            log.exception("mockcloud: unhandled error on %s %s", scope.get("method"), scope.get("path"))
            holder["error"] = f"{type(exc).__name__}: {exc}"
            if holder["status"] is None:
                body = json.dumps({"code": 500, "message": f"mock internal error ({type(exc).__name__})"}).encode()
                await send_wrapper({"type": "http.response.start", "status": 500, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
                await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            body_bytes = bytes(holder["body"])
            undecodable = False
            try:
                body_text: str | None = body_bytes.decode("utf-8") if keep_body else None
            except UnicodeDecodeError:
                # HARNESS_POLICY: a non-UTF-8 body cannot be carried in the text
                # log; it is dropped AND marked (body_truncated) so a GoReplay
                # export never replays it silently as an empty body.
                body_text, undecodable = None, True
            entry = {
                "seq": self.ctx.state.next_seq(),
                "request_id": request_id,
                "ts": ts,
                "method": scope["method"],
                "path": scope["path"],
                "query": scope.get("query_string", b"").decode("latin-1"),
                "status": holder["status"],
                "duration_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                "headers": {k: v for k, v in headers.items() if k not in LOGGED_HEADER_SKIP},
                "body": body_text if body_text is not None else "",
                "body_size": holder["size"],
                "body_truncated": undecodable or holder["size"] > len(body_bytes),
            }
            if undecodable:
                entry["body_undecodable"] = True
            if holder["error"] is not None:
                entry["error"] = holder["error"]
            self.ctx.state.log_request(entry)
            if scope["method"] != "GET":
                self.ctx.commit()


def create_app(settings: MockSettings | None = None, state: MockState | None = None) -> FastAPI:
    settings = settings or MockSettings()
    state = state or MockState()
    ctx = MockContext(settings=settings, state=state)
    if not state.devices:
        for type_, sn in settings.seed_devices:
            state.register_device(type_, sn, settings.now())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Tasks restored from --persist that were PENDING/STARTED when the last
        # process stopped are rescheduled (HARNESS_POLICY, review finding MC-4).
        resume_pending(ctx)
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="plaud-harness mockcloud",
        description="LOCAL mock of the Plaud partner cloud. Every issued value is synthetic.",
        version="1",
        docs_url="/_mock/docs",
        openapi_url="/_mock/openapi.json",
        redoc_url=None,
    )
    app.state.mock = ctx

    @app.exception_handler(MockHTTPError)
    async def _mock_error(_: Request, exc: MockHTTPError) -> Response:
        if exc.bare:
            return Response(status_code=exc.status)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> Response:
        # HARNESS_POLICY: 422 with the binding-spec Error body plus detail.
        return JSONResponse({"code": 422, "message": "request validation failed",
                             "detail": validation_detail(exc.errors())}, status_code=422)

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "mock": True,
            "name": "plaud-harness mockcloud",
            "surfaces": ["/developer/api/oauth/partner/*", "/developer/api/open/partner/*", "/api/*", "/s3/*", "/_mock/*"],
            "warning": "synthetic values only; never a real Plaud host",
        }

    for r in (auth.router, binding.router, sdkinternal.router, files.router, objectstore.router,
              transcription.router, mock.router):
        app.include_router(r)

    app.add_middleware(RequestLogMiddleware, ctx=ctx)
    return app


def default_persist_path() -> Path:
    """build/mockcloud/state.json relative to the repo root (build/ is gitignored)."""
    return Path(__file__).resolve().parents[1] / "build" / "mockcloud" / "state.json"
