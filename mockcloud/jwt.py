"""Minimal HS256 JSON Web Tokens on the standard library.

Why hand-rolled: no PyJWT in the venv, and the whole point is a token whose
payload the shipped SDK can parse. The SDK (NiceBuildSdk.resolveHandshakeToken,
build/evidence/jadx-out/sources/sdk/NiceBuildSdk.java:138-160; bytecode
build/evidence/javap/ALL.txt:6440-6500) splits the token on ".", Base64-decodes
segment 1 with android.util.Base64 flags 9 (URL_SAFE | NO_PADDING) and then
runs the regex  "sub"\\s*:\\s*"([^"]+)"  over the decoded text. So the payload
must be base64url without padding and must carry a string `sub` claim. Both
are ordinary JWT (RFC 7519) and both are honoured here.

The algorithm choice (HS256 with MOCK_JWT_SECRET) is HARNESS_POLICY; the real
tokens are RS-something ("eyJhbGciOiJSUz..." in openapi_auth.json) and nobody
outside Plaud can verify them. The mock's secret is public on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


class JwtError(ValueError):
    """Malformed token, bad signature, or expired."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encode(claims: dict[str, Any], secret: str) -> str:
    """HS256-sign `claims`. Compact JSON so the payload is small and stable."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def decode(token: str, secret: str, now: float | None = None) -> dict[str, Any]:
    """Verify the signature (and `exp` when `now` is given) and return the claims."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JwtError("token must have three segments")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise JwtError(f"bad header: {exc}") from exc
    if header.get("alg") != "HS256":
        raise JwtError(f"unsupported alg {header.get('alg')!r}")
    expected = hmac.new(
        secret.encode("utf-8"), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    try:
        actual = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise JwtError(f"bad signature encoding: {exc}") from exc
    if not hmac.compare_digest(expected, actual):
        raise JwtError("signature mismatch")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise JwtError(f"bad payload: {exc}") from exc
    if not isinstance(claims, dict):
        raise JwtError("payload is not an object")
    if now is not None:
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            raise JwtError("missing exp")
        if now >= exp:
            raise JwtError("token expired")
    return claims


def peek_claims(token: str) -> dict[str, Any]:
    """Payload WITHOUT verification -- the template's JwtUtils does exactly this
    (reference/plaud-org/plaud-sdk-public/android/.../common/JwtUtils.kt:16-33)."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JwtError("token must have three segments")
    claims = json.loads(_b64url_decode(parts[1]))
    if not isinstance(claims, dict):
        raise JwtError("payload is not an object")
    return claims
