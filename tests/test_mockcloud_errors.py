"""The mock's error contract under malformed input: every bad request is a
401 / 403 / 422 / 400 with an X-Request-Id and a logged status, never an HTTP
500 that the request log records as `status: null` (review findings MC-2 and
MC-8). Also the strict, canonical base64url rule for JWT segments.

Status codes for malformed input are HARNESS_POLICY (the docs list no error
responses beyond the binding spec's 403/404); what is asserted is that the mock
answers them deliberately.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Awaitable, Callable

import pytest

from test_mockcloud_helpers import (  # noqa: F401 - no_outbound_network is an autouse fixture
    APP,
    CLIENT_KEYS,
    PARTNER,
    SN_NOTEPRO,
    bearer,
    make_app,
    no_outbound_network,
    raw_client,
    user_token,
)
from mockcloud import MOCK_JWT_SECRET
from mockcloud import jwt as mjwt

SDK = f"{PARTNER}/open/partner/sdk"
FILES = f"{PARTNER}/open/partner/files/upload"
AI = f"{PARTNER}/open/partner/ai/transcriptions"
REF = {"type": "notepro", "sn": SN_NOTEPRO}
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

Send = Callable[[Any, str, dict[str, str]], Awaitable[Any]]


def _noncanonical(sig_b64: str) -> str:
    """Same bytes, different text: flip an unused low bit of the last char
    (32-byte HMAC = 43 base64url chars, the last carrying 2 unused bits)."""
    last = _ALPHABET.index(sig_b64[-1])
    return sig_b64[:-1] + _ALPHABET[last ^ 1]


def _list_header_token() -> str:
    h = base64.urlsafe_b64encode(b"[1]").rstrip(b"=").decode()
    return f"{h}.e30.x"


async def _text_plain_bind(c, tok, h):
    return await c.post(f"{SDK}/bind", headers={**bearer(tok), **h, "Content-Type": "text/plain"},
                        content=b"type=notepro&sn=8810000000000001")


async def _form_body_user_token(c, tok, h):
    return await c.post(f"{PARTNER}/open/partner/users/access-token",
                        headers={"Authorization": "Bearer x", **h,
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        content=b"user_id=abcdefgh")


async def _nan_filesize(c, tok, h):
    return await c.post(f"{FILES}/generate-presigned-urls",
                        headers={**bearer(tok), **h, "Content-Type": "application/json"},
                        content=b'{"filesize": NaN, "filetype": "mp3"}')


async def _non_ascii_jwt(c, tok, h):
    head, payload, sig = tok.split(".")
    bad = f"{head}.{payload}\xe9.{sig}"
    return await c.post(f"{SDK}/bind", headers={"Authorization": ("Bearer " + bad).encode("latin-1"), **h}, json=REF)


async def _list_jwt_header(c, tok, h):
    return await c.post(f"{SDK}/gen-key", headers={**bearer(_list_header_token()), **h})


async def _junk_after_signature(c, tok, h):
    return await c.post(f"{SDK}/bind", headers={**bearer(tok + "!!"), **h}, json=REF)


async def _noncanonical_signature(c, tok, h):
    head, payload, sig = tok.split(".")
    return await c.post(f"{SDK}/bind", headers={**bearer(f"{head}.{payload}.{_noncanonical(sig)}"), **h}, json=REF)


async def _non_ascii_api_key(c, tok, h):
    return await c.get(f"{AI}/task_exec_x", headers={"X-Client-Id": APP.client_id,
                                                     "X-Client-Api-Key": "\xe9".encode("latin-1"), **h})


async def _non_ascii_basic_secret(c, tok, h):
    basic = base64.b64encode(f"{APP.client_id}:sécret".encode()).decode()
    return await c.post(f"{PARTNER}/oauth/partner/access-token", headers={"Authorization": f"Basic {basic}", **h})


async def _non_ascii_sdk_pair(c, tok, h):
    pair = base64.b64encode(f"{APP.client_id}:sécret".encode()).decode()
    return await c.post("/api/oauth/sdk-token", headers={"Authorization": f"Bearer {pair}", **h})


async def _non_ascii_presign_signature(c, tok, h):
    pres = (await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                         json={"filesize": 10, "filetype": "mp3"})).json()
    url = pres["Parts"][0]["PresignedUrl"].rsplit("X-Mock-Signature=", 1)[0] + "X-Mock-Signature=%C3%A9"
    return await c.put(url, content=b"x", headers=h)


async def _signed_token_without_sub(c, tok, h):
    # the mock's HS256 secret is public, so anyone can sign a token; one that
    # lacks the claims the routes read must still be a 401
    forged = mjwt.encode({"token_use": "user", "exp": 4_000_000_000}, MOCK_JWT_SECRET)  # unexpired on the test clock
    return await c.post(f"{SDK}/bind", headers={**bearer(forged), **h}, json=REF)


async def _params_wrong_type(c, tok, h):
    return await c.post(f"{AI}/", headers={**CLIENT_KEYS, **h},
                        json={"file_url": "https://example.invalid/a.mp3", "params": {"diarization": True}})


CASES: list[tuple[str, Send, int]] = [
    ("text/plain body to a JSON endpoint", _text_plain_bind, 422),
    ("form body to a JSON endpoint", _form_body_user_token, 422),
    ("NaN in a JSON number", _nan_filesize, 422),
    ("non-ASCII byte in the JWT payload", _non_ascii_jwt, 401),
    ("JWT whose header is a JSON list", _list_jwt_header, 401),
    ("junk appended to the JWT signature", _junk_after_signature, 401),
    ("non-canonical base64url JWT signature", _noncanonical_signature, 401),
    ("non-ASCII X-Client-Api-Key", _non_ascii_api_key, 401),
    ("non-ASCII Basic secret", _non_ascii_basic_secret, 401),
    ("non-ASCII appSecret in the sdk-token Bearer pair", _non_ascii_sdk_pair, 401),
    ("non-ASCII presigned-URL signature", _non_ascii_presign_signature, 403),
    ("validly signed user token without sub", _signed_token_without_sub, 401),
    ("wrongly typed transcription params", _params_wrong_type, 422),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,send,expected", CASES, ids=[c[0] for c in CASES])
async def test_malformed_input_is_answered_not_crashed(name: str, send: Send, expected: int) -> None:
    app, ctx, _ = make_app(chunk_size=1024)
    async with raw_client(app) as c:
        tok = await user_token(c)
        rid = f"case-{abs(hash(name)) % 10**8}"
        r = await send(c, tok, {"X-Request-Id": rid})
        assert r.status_code == expected, (name, r.status_code, r.text[:200])
        assert r.headers.get("x-request-id") == rid, "the response carries the request id"
        entry = next(e for e in ctx.state.request_log if e["request_id"] == rid)
        assert entry["status"] == expected, "the log records the status that was sent"
        if expected == 422:
            body = r.json()
            assert body["code"] == 422 and isinstance(body["detail"], list) and body["detail"]
            assert all({"type", "loc", "msg"} <= set(d) for d in body["detail"])


@pytest.mark.asyncio
async def test_an_unexpected_handler_error_is_a_logged_500_with_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: whatever escapes a route becomes a 500 the request log
    records (status 500, the error text) and that carries X-Request-Id."""
    import mockcloud.routers.binding as binding

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("synthetic failure for the test")

    monkeypatch.setattr(binding, "lookup_device", boom)
    app, ctx, _ = make_app()
    async with raw_client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{SDK}/bind", headers={**bearer(tok), "X-Request-Id": "boom-1"}, json=REF)
        assert r.status_code == 500
        assert r.headers.get("x-request-id") == "boom-1"
        assert r.json() == {"code": 500, "message": "mock internal error (RuntimeError)"}
        entry = ctx.state.request_log[-1]
        assert entry["request_id"] == "boom-1" and entry["status"] == 500
        assert entry["error"] == "RuntimeError: synthetic failure for the test"
        # the app keeps serving
        assert (await c.post(f"{SDK}/gen-key", headers=bearer(tok))).status_code == 200


def test_jwt_decode_raises_only_jwt_error_and_requires_canonical_segments() -> None:
    now = time.time()
    good = mjwt.encode({"sub": "client_user_x", "exp": now + 60}, MOCK_JWT_SECRET)
    assert mjwt.decode(good, MOCK_JWT_SECRET, now=now)["sub"] == "client_user_x"
    head, payload, sig = good.split(".")
    bad_tokens = [
        good + "!!", good + "=", f"{head}.{payload}.{sig}A", f"{head}.{payload}.{_noncanonical(sig)}",
        f"{head}.{payload}\xe9.{sig}", _list_header_token(), "a.b", "", "..", f"{head}=.{payload}.{sig}",
        f"{head}.{payload}.{sig[:-1]}", ".".join([head, base64.urlsafe_b64encode(b"[]").rstrip(b"=").decode(), sig]),
    ]
    for tok in bad_tokens:
        with pytest.raises(mjwt.JwtError):
            mjwt.decode(tok, MOCK_JWT_SECRET, now=now)
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(123, MOCK_JWT_SECRET)  # type: ignore[arg-type]
    with pytest.raises(mjwt.JwtError):
        mjwt.peek_claims(f"{head}.{payload}\xe9.{sig}")
    nan_exp = mjwt.encode({"sub": "s", "exp": float("nan")}, MOCK_JWT_SECRET)
    with pytest.raises(mjwt.JwtError):
        mjwt.decode(nan_exp, MOCK_JWT_SECRET, now=now)


@pytest.mark.asyncio
async def test_presign_part_count_is_capped_at_the_s3_limit() -> None:
    """MC-8: count = ceil(filesize / ChunkSize) in integers, at most 10,000
    parts (INFERRED S3 limit); above it 400 FILE_TOO_LARGE, answered at once."""
    from mockcloud.routers.files import MAX_PARTS

    assert MAX_PARTS == 10_000
    app, _, _ = make_app(chunk_size=1024)
    async with raw_client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                         json={"filesize": MAX_PARTS * 1024, "filetype": "mp3"})
        assert r.status_code == 200 and len(r.json()["Parts"]) == MAX_PARTS
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                         json={"filesize": MAX_PARTS * 1024 + 1, "filetype": "mp3"})
        assert r.status_code == 400 and r.json()["message"].startswith("FILE_TOO_LARGE")
        for size in (10**15, 10**400):
            t0 = time.perf_counter()
            r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                             json={"filesize": size, "filetype": "mp3"})
            assert r.status_code in (400, 422), (size, r.status_code)
            assert time.perf_counter() - t0 < 2.0, "refused without building the parts"
