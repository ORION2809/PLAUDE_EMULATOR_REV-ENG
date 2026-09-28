"""Endpoints the SDK binary itself calls (r7/cloud-endpoint-inventory.md
sections A and B; docs/architecture/cloud.md sections 1-3; protocol-ledger.md
section 4.5).

Two path families coexist in the AAR and the mock serves both:

  A. `/api/...`                     sdk/network/ApiService (Retrofit, BYTECODE_PROVEN)
  B. `/developer/api/open/partner/sdk/...`  sdk/network/PartnerApiService (BYTECODE_PROVEN)

Only the SHAPES below are evidence-backed (Gson @SerializedName literals in
build/evidence/jadx-out/sources/sdk/network/model/*.java). Every VALUE is
HARNESS_POLICY: the real cloud's key sizes, signature algorithm, token
formats and "no update" answer are UNKNOWN.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..common import (
    MockContext,
    MockHTTPError,
    ctx_of,
    new_id,
    require_device_signature,
    require_opaque_token,
    require_sdk_basic_as_bearer,
    require_user_token,
)
from ..settings import MOCK_FIRMWARE_BUCKET
from ..state import StoredObject

router = APIRouter(tags=["SDK internal"])
B = "/developer/api/open/partner/sdk"

# --------------------------------------------------------------- surface A

@router.post("/api/oauth/sdk-token")
async def sdk_token(request: Request) -> dict[str, Any]:
    """First hop of the AAR's 3-hop chain. Path BYTECODE_PROVEN
    (ApiService.java @POST("/api/oauth/sdk-token")); response field names from
    SdkTokenResponse.java (`sdk_token`, `token_type`, `expires_in`). The
    template notes this endpoint 404s on platform-us (DeviceManager.kt:527-528);
    the mock serves it because the AAR is written to call it."""
    ctx = ctx_of(request)
    app = require_sdk_basic_as_bearer(request)
    tok = new_id("mock_sdk_token_", 12)
    ttl = ctx.settings.sdk_token_ttl_s
    ctx.state.sdk_tokens[tok] = {"client_id": app.client_id, "exp": ctx.now() + ttl}
    return {"sdk_token": tok, "token_type": "bearer", "expires_in": ttl}


@router.post("/api/sdk/api-token")
async def api_token(request: Request) -> dict[str, Any]:
    """Second hop: Bearer sdk_token -> api_token (ApiTokenResponse.java)."""
    ctx = ctx_of(request)
    rec = require_opaque_token(request, ctx.state.sdk_tokens, "sdk_token")
    tok = new_id("mock_api_token_", 12)
    ttl = ctx.settings.sdk_token_ttl_s
    ctx.state.api_tokens[tok] = {"client_id": rec["client_id"], "exp": ctx.now() + ttl}
    return {"api_token": tok, "token_type": "bearer", "expires_in": ttl}


@router.get("/api/sdk/config")
async def sdk_config(request: Request) -> dict[str, Any]:
    """TokenPermissionResponse {permissions: {isSupportBase, isSupportDownload}}
    -- field names BYTECODE_PROVEN (Permissions.java); values HARNESS_POLICY."""
    ctx = ctx_of(request)
    require_opaque_token(request, ctx.state.api_tokens, "api_token")
    return {"permissions": {"isSupportBase": True, "isSupportDownload": True}}


@router.get("/api/sdk/latest-version")
async def latest_version_a(
    request: Request,
    sn_type: str = Query("notepin"),
    model: str = Query(...),
    version_type: str = Query("V"),
) -> dict[str, Any]:
    """Surface-A firmware check: query names and defaults BYTECODE_PROVEN
    (ApiService.getLatestDeviceVersionNew: sn_type default "notepin",
    version_type default "V"). Same DeviceVersionResponse shape as B."""
    ctx = ctx_of(request)
    require_opaque_token(request, ctx.state.api_tokens, "api_token")
    return device_version_response(request, sn_type, model)


# --------------------------------------------------------------- surface B

def _pem_pair(bits: int) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return public_pem, private_pem


@router.post(f"{B}/gen-key")
async def gen_key(request: Request) -> dict[str, Any]:
    """`{public_key, private_key}` PEM -- field names BYTECODE_PROVEN
    (GenKeyResponse.java @SerializedName), PKCS#8 for the private key per
    protocol-ledger.md 4.5. The cloud hands the PRIVATE key to the client; the
    mock does the same with a FRESH pair per call (iOS doc: every
    setUserAccessToken "fetch[es] a fresh RSA key pair", advanced-ios-sdk.md:514).
    Key size 2048 and SPKI encoding for the public key are HARNESS_POLICY.

    The same doc sentence goes on: "A new key pair invalidates every cached
    sn-sign signature, since signatures are bound to the key pair that
    produced them" (DOC-EXACT statement; it describes SDK cache behaviour, and
    what the real server does with a stale signature is UNKNOWN). The mock
    models it (review finding MC-7): each sn-sign signature covers the
    fingerprint of the caller's current pair, and this call revokes every
    signature previously issued to the caller."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    sub = str(claims["sub"])
    public_pem, private_pem = _pem_pair(ctx.settings.rsa_key_bits)
    now = ctx.now()
    ctx.state.keypairs[sub] = {"public_key": public_pem, "private_key": private_pem, "created_at": now}
    for rec in ctx.state.signatures.values():
        if rec.get("sub") == sub and rec.get("revoked_at") is None:
            rec["revoked_at"] = now
    return {"public_key": public_pem, "private_key": private_pem}


class SnRef(BaseModel):
    #: BYTECODE_PROVEN field names: SnSignRequest.java ("type", r3.A == "sn").
    type: str
    sn: str


#: HARNESS_POLICY: the key fingerprint a signature is bound to when the caller
#: never called gen-key. Allowing sn-sign without a key pair is an explicit
#: DEVIATION from advanced-ios-sdk.md:514 ("signatures are bound to the key
#: pair that produced them"), kept because the SDK's flows always gen-key first
#: and many harness flows only need a signature; such a signature is still
#: revoked by the caller's next gen-key.
NO_KEY_FINGERPRINT = "no-gen-key"


def key_fingerprint(public_pem: str | None) -> str:
    """sha256 hex of the SPKI PEM text (HARNESS_POLICY)."""
    return hashlib.sha256(public_pem.encode("ascii")).hexdigest() if public_pem else NO_KEY_FINGERPRINT


def current_key_fingerprint(ctx: MockContext, sub: str) -> str:
    pair = ctx.state.keypairs.get(sub)
    return key_fingerprint(pair["public_key"] if pair else None)


def sn_sign_payload(type_: str, sn: str, sub: str, key_fp: str) -> bytes:
    """What the mock signs. HARNESS_POLICY: the real payload is UNKNOWN (the
    signature is opaque base64 to the SDK, ledger 4.5; the device verifies it
    in the 0xFE10/0xFE20 pre-handshake, ledger 4.3). `key_fp` binds the
    signature to the caller's gen-key pair (advanced-ios-sdk.md:514)."""
    return f"plaud-harness mockcloud sn-sign v2\n{type_}\n{sn}\n{sub}\n{key_fp}\n".encode("utf-8")


@router.post(f"{B}/sn-sign")
async def sn_sign(request: Request, body: SnRef) -> dict[str, Any]:
    """`{signature}` -- BYTECODE_PROVEN field (SnSignResponse.java). RSA
    PKCS#1 v1.5 / SHA-256 over sn_sign_payload with the mock cloud's own key,
    base64 (standard alphabet) -- all HARNESS_POLICY. sn-sign does not require
    the SN to be registered (signing is stateless in the mock; UNKNOWN in the
    cloud) but records "signed" on known devices for the binding tri-state.
    The signature is bound to the caller's current gen-key pair
    (NO_KEY_FINGERPRINT when there is none -- see the deviation note there)."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    sub = str(claims["sub"])
    key_fp = current_key_fingerprint(ctx, sub)
    sig = ctx.signing_key().sign(sn_sign_payload(body.type, body.sn, sub, key_fp), padding.PKCS1v15(), hashes.SHA256())
    signature = base64.b64encode(sig).decode("ascii")
    ctx.state.signatures[signature] = {
        "type": body.type, "sn": body.sn, "sub": sub, "key_fp": key_fp, "issued_at": ctx.now(), "revoked_at": None,
    }
    rec = ctx.state.device(body.type, body.sn)
    if rec is None and ctx.settings.auto_register_unknown_sn:
        rec = ctx.state.register_device(body.type, body.sn, ctx.now())
    if rec is not None and sub not in rec.signed_by:
        rec.signed_by.append(sub)
    return {"signature": signature}


class SnVerifyRequest(SnRef):
    #: BYTECODE_PROVEN field names: SnVerifyRequest.java.
    signature: str


@router.post(f"{B}/sn-verify")
async def sn_verify(request: Request, body: SnVerifyRequest) -> dict[str, Any]:
    """`{is_valid}` -- BYTECODE_PROVEN field (SnVerifyResponse.java). The mock
    verifies against its own signer and the caller's CURRENT key pair: a
    signature for a different (type, sn), a forged one, or one made before the
    caller's latest gen-key is invalid."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    sub = str(claims["sub"])
    try:
        raw = base64.b64decode(body.signature, validate=True)
        payload = sn_sign_payload(body.type, body.sn, sub, current_key_fingerprint(ctx, sub))
        ctx.signing_key().public_key().verify(raw, payload, padding.PKCS1v15(), hashes.SHA256())
        valid = True
    except Exception:  # noqa: BLE001 - any verification failure is "invalid"
        valid = False
    return {"is_valid": valid}


class DeviceMetadataConfig(BaseModel):
    #: BYTECODE_PROVEN: DeviceMetadataConfig.java ("battery", Integer).
    battery: int | None = None


class DeviceMetadata(BaseModel):
    #: BYTECODE_PROVEN: DeviceMetadata.java ("version", "config").
    version: str
    config: DeviceMetadataConfig


class MetadataRequest(BaseModel):
    #: BYTECODE_PROVEN: MetadataRequest.java ("type", "sn", "metadata").
    type: str
    sn: str
    metadata: DeviceMetadata


@router.post(f"{B}/metadata")
async def post_metadata(request: Request, body: MetadataRequest) -> dict[str, Any]:
    """Requires X-Device-Signature (BYTECODE_PROVEN header). The AAR only
    checks `isSuccessful` (PartnerApiManager.postDeviceMetadata returns
    Boolean), so the 200 body is UNKNOWN; HARNESS_POLICY returns {"ok": true}.
    A signature issued for a different (type, sn) is refused with 403."""
    ctx = ctx_of(request)
    sig = require_device_signature(request)
    if sig["type"] != body.type or sig["sn"] != body.sn:
        raise MockHTTPError(403, "X-Device-Signature was issued for a different device")
    rec = ctx.state.device(body.type, body.sn)
    if rec is None and ctx.settings.auto_register_unknown_sn:
        rec = ctx.state.register_device(body.type, body.sn, ctx.now())
    if rec is None:
        raise MockHTTPError(404, bare=True)
    ctx.state.files.setdefault("_metadata", {})[f"{body.type}/{body.sn}"] = {
        "version": body.metadata.version,
        "battery": body.metadata.config.battery,
        "reported_at": ctx.now(),
        "by": sig["sub"],
    }
    return {"ok": True}


def device_version_response(request: Request, type_: str, sn_or_model: str) -> dict[str, Any]:
    """DeviceVersionResponse -- ten field names BYTECODE_PROVEN
    (DeviceVersionResponse.java). Default answer is "no update": version_code 0
    and an empty download_url, which the template's rule
    `serverCode > deviceCode && downloadUrl.isNotBlank()` (DeviceManager.kt:571)
    evaluates to no update. With settings.ota_image_path the mock advertises
    that file from its own object store. `version_code` is emitted as a JSON
    number; the Gson model declares it String and the template reads it with
    optLong -- both accept a number (HARNESS_POLICY)."""
    ctx = ctx_of(request)
    s = ctx.settings
    model = sn_or_model[:3]
    if s.ota_image_path is None:
        return {
            "type": type_, "model": model, "version_type": s.ota_version_type,
            "version_code": 0, "version_number": "", "version_description": "no update (plaud-harness mockcloud)",
            "is_force": False, "is_strong_guidance": False, "file_md5": "", "download_url": "",
        }
    image = Path(s.ota_image_path)
    data = image.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    key = image.name
    if ctx.state.get_object(MOCK_FIRMWARE_BUCKET, key) is None:
        ctx.state.put_object(StoredObject(
            bucket=MOCK_FIRMWARE_BUCKET, key=key, data=data, content_type="application/octet-stream",
            etag=f'"{md5}"', acl="public-read", created_at=ctx.now(),
        ))
    return {
        "type": type_, "model": model, "version_type": s.ota_version_type,
        "version_code": s.ota_version_code, "version_number": str(s.ota_version_code),
        "version_description": "mock OTA image (plaud-harness mockcloud)",
        "is_force": False, "is_strong_guidance": False, "file_md5": md5,
        "download_url": f"{ctx.base_url(request)}s3/{MOCK_FIRMWARE_BUCKET}/{key}",
    }


@router.get(f"{B}/version/latest")
async def version_latest(
    request: Request,
    type: str = Query(...),
    sn: str = Query(...),
    current_version: str = Query("-1"),
) -> dict[str, Any]:
    """OFFICIAL_SOURCE: DeviceManager.kt:555-558 builds
    `.../sdk/version/latest?type=&sn=&current_version=-1` with X-Device-Signature;
    PlaudAPIService.swift header comment lists version/latest under
    X-Device-Signature. Response shape as device_version_response."""
    sig = require_device_signature(request)
    if sig["type"] != type or sig["sn"] != sn:
        raise MockHTTPError(403, "X-Device-Signature was issued for a different device")
    return device_version_response(request, type, sn)
