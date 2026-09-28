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
import re
from typing import Any


class JwtError(ValueError):
    """Malformed token, bad signature, or expired."""


#: RFC 7515 section 2 / RFC 4648 section 5: base64url, no padding. Anything else
#: in a segment (including '=' padding, '+', '/', whitespace or a non-ASCII
#: character) makes the token malformed.
_B64URL_SEGMENT = re.compile(r"[A-Za-z0-9_-]*")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Strict, canonical base64url decode (HARNESS_POLICY strictness).

    Rejects characters outside the base64url alphabet, padding, impossible
    lengths (len % 4 == 1) and non-canonical encodings whose unused low bits
    are set, so exactly one segment string maps to each byte string and a
    signature segment cannot be altered without failing verification.
    """
    if not isinstance(text, str) or not _B64URL_SEGMENT.fullmatch(text):
        raise JwtError("segment is not base64url without padding")
    if len(text) % 4 == 1:
        raise JwtError("segment has an impossible base64url length")
    data = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    if _b64url(data) != text:
        raise JwtError("segment is not canonical base64url")
    return data


def encode(claims: dict[str, Any], secret: str) -> str:
    """HS256-sign `claims`. Compact JSON so the payload is small and stable."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def _split(token: Any) -> list[str]:
    if not isinstance(token, str):
        raise JwtError("token must be a string")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JwtError("token must have three segments")
    for part in parts:
        if not _B64URL_SEGMENT.fullmatch(part):
            raise JwtError("token segments must be base64url without padding")
    return parts


def _json_object(segment: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(_b64url_decode(segment))
    except JwtError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise JwtError(f"bad {what}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise JwtError(f"{what} is not a JSON object")
    return value


def decode(token: str, secret: str, now: float | None = None) -> dict[str, Any]:
    """Verify the signature (and `exp` when `now` is given) and return the claims.

    Every malformed input raises JwtError -- never another exception type -- so
    callers can map it to a 401 (review finding MC-2: a non-ASCII byte or a
    list-shaped header used to escape as a 500).
    """
    try:
        header_b64, payload_b64, sig_b64 = _split(token)
        header = _json_object(header_b64, "header")
        if header.get("alg") != "HS256":
            raise JwtError(f"unsupported alg {header.get('alg')!r}")
        expected = hmac.new(
            secret.encode("utf-8"), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        actual = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, actual):
            raise JwtError("signature mismatch")
        claims = _json_object(payload_b64, "payload")
        if now is not None:
            exp = claims.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                raise JwtError("missing exp")
            if not now < exp:  # also rejects a NaN exp
                raise JwtError("token expired")
        return claims
    except JwtError:
        raise
    except (ValueError, TypeError, AttributeError, UnicodeError, OverflowError) as exc:
        raise JwtError(f"malformed token: {type(exc).__name__}") from exc


def peek_claims(token: str) -> dict[str, Any]:
    """Payload WITHOUT verification -- the template's JwtUtils does exactly this
    (reference/plaud-org/plaud-sdk-public/android/.../common/JwtUtils.kt:16-33)."""
    _, payload_b64, _ = _split(token)
    return _json_object(payload_b64, "payload")
