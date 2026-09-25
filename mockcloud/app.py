"""create_app(): the FastAPI application, request logging middleware, error
rendering. See mockcloud/__init__.py for the evidence-class vocabulary.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Awaitable, Callable, MutableMapping

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .common import MockContext, MockHTTPError, new_id
from .routers import auth, binding, files, mock, objectstore, sdkinternal, transcription
from .settings import MockSettings
from .state import MockState

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

#: HARNESS_POLICY: header names recorded verbatim into the log for replay.
#: Everything the mock issues is synthetic, so Authorization values are kept.
LOGGED_HEADER_SKIP = {"host", "content-length", "connection", "accept-encoding", "user-agent"}


class RequestLogMiddleware:
    """Pure-ASGI logger: method, path, query, status, X-Request-Id, latency,
    headers and (capped) body -- enough for GET /_mock/log?format=gor."""

    def __init__(self, app: Any, ctx: MockContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/_mock"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or new_id("mockreq_", 6)
        cap = self.ctx.settings.log_body_cap
        keep_body = not scope["path"].startswith("/s3/")
        holder: dict[str, Any] = {"status": None, "body": bytearray(), "size": 0}

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
        finally:
            body_bytes = bytes(holder["body"])
            try:
                body_text: str | None = body_bytes.decode("utf-8") if keep_body else None
            except UnicodeDecodeError:
                body_text = None
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
                "body_truncated": holder["size"] > len(body_bytes),
            }
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

    app = FastAPI(
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
        return JSONResponse({"code": 422, "message": "request validation failed", "detail": exc.errors()},
                            status_code=422)

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
