"""Shared plumbing for the routers: the per-app context, the error type, the
authentication dependencies, and mock URL signing.

Where a status code is stated by the docs it is cited; the rest is
HARNESS_POLICY and says so.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from . import jwt
from .settings import MockSettings, PartnerApp
from .state import MockState


class MockHTTPError(Exception):
    """Rendered by app.py as `{"code": <code>, "message": <message>}` -- the
    Error schema of openapi_binding.json (DOC-EXACT for the 403 bind case) --
    or as an empty body when `bare` is set (the documented bare 404)."""

    def __init__(self, status: int, message: str = "", code: int | None = None, bare: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = status if code is None else code
        self.bare = bare


def new_id(prefix: str, nbytes: int = 8) -> str:
    """SYNTHETIC identifiers. The `task_exec_` / `file_` / `upload_` prefixes
    are INFERRED from the placeholder examples "task_exec_xxx", "file_xxx" and
    "upload_xxx" (openapi_transcription.json, openapi_file.json): the docs never
    state an id format, only these examples. The hex tail is HARNESS_POLICY."""
    return f"{prefix}{secrets.token_hex(nbytes)}"


def secrets_equal(expected: str, supplied: str) -> bool:
    """Constant-time comparison of two text secrets.

    hmac.compare_digest raises TypeError on a str that is not pure ASCII, so a
    header or query value carrying e.g. "\xe9" used to escape as HTTP 500
    (review finding MC-2). Both sides are compared as UTF-8 bytes instead;
    surrogateescape keeps any code point encodable.
    """
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogateescape"), supplied.encode("utf-8", "surrogateescape")
    )


@dataclass
class MockContext:
    settings: MockSettings
    state: MockState
    worker_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    #: set once restored non-terminal tasks have been rescheduled (worker.resume_pending)
    resumed: bool = False

    # ---------------------------------------------------------------- time
    def now(self) -> float:
        return self.settings.now()

    # --------------------------------------------------------- persistence
    def commit(self) -> None:
        self.state.save()

    async def drain(self) -> None:
        """Await every background transcription task (tests call this)."""
        while self.worker_tasks:
            pending = [t for t in self.worker_tasks if not t.done()]
            if not pending:
                self.worker_tasks.clear()
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------------------------------------------------------------- urls
    def base_url(self, request: Request) -> str:
        base = self.settings.public_base_url or str(request.base_url)
        return base if base.endswith("/") else base + "/"

    def sign(self, *parts: str) -> str:
        """HMAC over the URL facts. Stands in for S3's SigV4 query signature
        (HARNESS_POLICY: 32 hex chars, mock secret)."""
        msg = "\n".join(parts).encode("utf-8")
        return hmac.new(self.settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]

    def check_signed_query(self, method: str, bucket: str, key: str, query: Any, *extra: str) -> None:
        """403 (S3 AccessDenied semantics, INFERRED) when the presigned query is
        missing, forged, or past X-Mock-Expires."""
        exp = query.get("X-Mock-Expires")
        sig = query.get("X-Mock-Signature")
        if not exp or not sig:
            raise MockHTTPError(403, "AccessDenied: URL is not presigned")
        try:
            exp_f = float(exp)
        except ValueError as exc:
            raise MockHTTPError(403, "AccessDenied: bad expiry") from exc
        if self.now() >= exp_f:
            raise MockHTTPError(403, "AccessDenied: presigned URL expired")
        expected = self.sign(method, bucket, key, *extra, exp)
        if not secrets_equal(expected, sig):
            raise MockHTTPError(403, "AccessDenied: signature mismatch")

    # -------------------------------------------------------- mock signer
    def signing_key(self) -> rsa.RSAPrivateKey:
        """The mock cloud's own RSA key used for sn-sign. Generated once and
        persisted with the state so signatures verify across restarts."""
        pem = self.state.signing_key_pem
        if pem is None:
            key = rsa.generate_private_key(public_exponent=65537, key_size=self.settings.rsa_key_bits)
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii")
            self.state.signing_key_pem = pem
            return key
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key

    def signing_public_pem(self) -> str:
        return self.signing_key().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")


def ctx_of(request: Request) -> MockContext:
    return request.app.state.mock


# ------------------------------------------------------------------ auth
def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _decode_basic_pair(encoded: str) -> tuple[str, str] | None:
    try:
        raw = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in raw:
        return None
    user, _, pw = raw.partition(":")
    return user, pw


def require_partner_app_basic(request: Request) -> PartnerApp:
    """`Authorization: Basic base64(client_id:secret_key)` -- DOC-EXACT scheme
    (openapi_auth.json securitySchemes.basicAuth). A wrong or missing
    credential is 401 (HARNESS_POLICY: the docs list no error responses)."""
    ctx = ctx_of(request)
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    pair = _decode_basic_pair(value) if scheme.lower() == "basic" else None
    if pair is None:
        raise MockHTTPError(401, "missing or malformed Basic authorization")
    app = ctx.settings.app_by_client_id(pair[0])
    if app is None or not secrets_equal(app.secret_key, pair[1]):
        raise MockHTTPError(401, "unknown client_id or wrong secret_key")
    return app


def require_sdk_basic_as_bearer(request: Request) -> PartnerApp:
    """The AAR's first hop: `Authorization: "Bearer " + Base64(appKey:appSecret)`
    (r7/cloud-endpoint-inventory.md section A, ProximaInterfaceRelay.java:41;
    BYTECODE_PROVEN header construction). Same registry as the partner app
    (HARNESS_POLICY, see settings.PartnerApp)."""
    ctx = ctx_of(request)
    token = _bearer(request)
    pair = _decode_basic_pair(token) if token else None
    if pair is None:
        raise MockHTTPError(401, "expected Bearer base64(appKey:appSecret)")
    app = ctx.settings.app_by_client_id(pair[0])
    if app is None or not secrets_equal(app.secret_key, pair[1]):
        raise MockHTTPError(401, "unknown appKey or wrong appSecret")
    return app


def _require_jwt(request: Request, token_use: str) -> dict[str, Any]:
    ctx = ctx_of(request)
    token = _bearer(request)
    if token is None:
        raise MockHTTPError(401, f"missing Bearer {token_use} token")
    try:
        claims = jwt.decode(token, ctx.settings.jwt_secret, now=ctx.now())
    except jwt.JwtError as exc:
        # HARNESS_POLICY: 401 with the binding-spec Error body. The template
        # comment "401 CLIENT_USER_NOT_FOUND" (DeviceManager.kt:167) is the only
        # observed 401 text and it describes a token sent to the wrong region,
        # so it is not reused here.
        raise MockHTTPError(401, f"invalid or expired {token_use} token: {exc}") from exc
    if claims.get("token_use") != token_use:
        raise MockHTTPError(401, f"this endpoint needs a {token_use} token")
    # The HS256 secret is public (settings.MOCK_JWT_SECRET), so a correctly
    # signed token may still lack the claims the routes read: 401, not a KeyError.
    needed = ("client_id",) if token_use == "partner" else ("sub", "client_id")
    if any(not isinstance(claims.get(k), str) or not claims[k] for k in needed):
        raise MockHTTPError(401, f"{token_use} token lacks {'/'.join(needed)}")
    return claims


def require_partner_token(request: Request) -> dict[str, Any]:
    return _require_jwt(request, "partner")


def require_user_token(request: Request) -> dict[str, Any]:
    """User JWT (Bearer) -- DOC-EXACT scheme for binding / files / sdk endpoints
    (openapi_binding.json, openapi_file.json securitySchemes.bearerAuth)."""
    return _require_jwt(request, "user")


def require_opaque_token(request: Request, table: dict[str, dict[str, Any]], what: str) -> dict[str, Any]:
    """AAR-internal sdk_token / api_token (opaque strings, HARNESS_POLICY)."""
    ctx = ctx_of(request)
    token = _bearer(request)
    rec = table.get(token) if token else None
    if rec is None:
        raise MockHTTPError(401, f"missing or unknown {what}")
    if ctx.now() >= rec["exp"]:
        raise MockHTTPError(401, f"{what} expired")
    return rec


def require_client_keys(request: Request) -> PartnerApp:
    """`X-Client-Id` + `X-Client-Api-Key` -- DOC-EXACT header names
    (openapi_transcription.json securitySchemes clientId / clientApiKey).
    X-Client-Id equals the user JWT's client_id claim (template
    PlaudAPIService.swift:25-30, RecordingStore.kt:105-106)."""
    ctx = ctx_of(request)
    client_id = request.headers.get("x-client-id", "")
    api_key = request.headers.get("x-client-api-key", "")
    if not client_id or not api_key:
        raise MockHTTPError(401, "missing X-Client-Id / X-Client-Api-Key")
    app = ctx.settings.app_by_client_id(client_id)
    if app is None or not secrets_equal(app.api_key, api_key):
        raise MockHTTPError(401, "unknown client_id or wrong api_key")
    return app


def require_device_signature(request: Request) -> dict[str, Any]:
    """`X-Device-Signature` -- BYTECODE_PROVEN header name
    (sdk/network/PartnerApiService.java postDeviceMetadata @Header, jadx
    build/evidence/jadx-out/sources/sdk/network/PartnerApiService.java:36; the
    literal also appears in an OkHttp interceptor at
    build/evidence/javap/ALL.txt:20772) and the template's version/latest call
    (DeviceManager.kt:555-558). The value is the sn-sign signature the SDK
    stored (`PlaudDeviceAgent.getSnSignature()`, DeviceManager.kt:546).
    Missing -> 401; unknown, forged, or revoked by a later gen-key -> 403 (all
    HARNESS_POLICY codes; revocation follows the doc sentence "A new key pair
    invalidates every cached sn-sign signature", advanced-ios-sdk.md:514 -- what
    the real server does with a stale signature is UNKNOWN)."""
    ctx = ctx_of(request)
    sig = request.headers.get("x-device-signature", "").strip()
    if not sig:
        raise MockHTTPError(401, "missing X-Device-Signature")
    rec = ctx.state.signatures.get(sig)
    if rec is None:
        raise MockHTTPError(403, "X-Device-Signature was not issued by this mock")
    if rec.get("revoked_at") is not None:
        raise MockHTTPError(403, "X-Device-Signature was revoked by a later gen-key (re-sign the SN)")
    return rec
