"""SDK-internal endpoints: gen-key (a fresh, parsable RSA private key),
sn-sign / sn-verify (a real signature under the mock's own key), metadata
(X-Device-Signature required), version/latest ("no update" by default, an
OTA image when configured) and the AAR's /api token chain.

Shapes are BYTECODE_PROVEN (sdk/network/model/*.java); values HARNESS_POLICY.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from test_mockcloud_helpers import APP, BASIC, PARTNER, SN_NOTEPRO, bearer, client, make_app, user_token

SDK = f"{PARTNER}/open/partner/sdk"
REF = {"type": "notepro", "sn": SN_NOTEPRO}


@pytest.mark.asyncio
async def test_gen_key_returns_a_fresh_parsable_pkcs8_pair() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        assert (await c.post(f"{SDK}/gen-key")).status_code == 401
        r1 = await c.post(f"{SDK}/gen-key", headers=bearer(tok))
        r2 = await c.post(f"{SDK}/gen-key", headers=bearer(tok))
        assert r1.status_code == 200 and set(r1.json()) == {"public_key", "private_key"}
        priv = serialization.load_pem_private_key(r1.json()["private_key"].encode(), password=None)
        pub = serialization.load_pem_public_key(r1.json()["public_key"].encode())
        assert isinstance(priv, rsa.RSAPrivateKey) and isinstance(pub, rsa.RSAPublicKey)
        assert priv.key_size == 2048
        assert priv.public_key().public_numbers() == pub.public_numbers(), "pair must match"
        assert r1.json()["private_key"].startswith("-----BEGIN PRIVATE KEY-----"), "PKCS#8, ledger 4.5"
        assert r1.json()["private_key"] != r2.json()["private_key"], "a fresh pair per call"
        # The private key really can decrypt what the public key encrypts --
        # that is what the SDK does with the 0xFE12 secret package.
        secret = b"PLAUD.AI" + bytes(48)
        ct = pub.encrypt(secret, padding.PKCS1v15())
        assert priv.decrypt(ct, padding.PKCS1v15()) == secret


@pytest.mark.asyncio
async def test_sn_sign_signature_verifies_under_the_mock_signing_key() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json=REF)
        assert r.status_code == 200 and set(r.json()) == {"signature"}
        sig = r.json()["signature"]
        raw = base64.b64decode(sig, validate=True)
        key = serialization.load_pem_public_key((await c.get("/_mock/signing-key")).json()["public_key"].encode())
        from mockcloud.jwt import peek_claims
        from mockcloud.routers.sdkinternal import sn_sign_payload
        sub = peek_claims(tok)["sub"]
        key.verify(raw, sn_sign_payload("notepro", SN_NOTEPRO, sub), padding.PKCS1v15(), hashes.SHA256())
        # sn-verify agrees, and refuses the same signature for another SN
        r = await c.post(f"{SDK}/sn-verify", headers=bearer(tok), json={**REF, "signature": sig})
        assert r.json() == {"is_valid": True}
        r = await c.post(f"{SDK}/sn-verify", headers=bearer(tok), json={"type": "notepro", "sn": "8810000000000002", "signature": sig})
        assert r.json() == {"is_valid": False}
        r = await c.post(f"{SDK}/sn-verify", headers=bearer(tok), json={**REF, "signature": "not base64!"})
        assert r.json() == {"is_valid": False}
        # a signature is bound to the user: another user's sn-verify says no
        other = await user_token(c, "other-user-000002")
        r = await c.post(f"{SDK}/sn-verify", headers=bearer(other), json={**REF, "signature": sig})
        assert r.json() == {"is_valid": False}


@pytest.mark.asyncio
async def test_metadata_requires_a_signature_issued_for_that_device() -> None:
    app, _, _ = make_app()
    body = {**REF, "metadata": {"version": "V0001", "config": {"battery": 87}}}
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{SDK}/metadata", json=body)
        assert r.status_code == 401, "missing X-Device-Signature"
        r = await c.post(f"{SDK}/metadata", headers={"X-Device-Signature": "forged"}, json=body)
        assert r.status_code == 403
        sig = (await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json=REF)).json()["signature"]
        r = await c.post(f"{SDK}/metadata", headers={"X-Device-Signature": sig},
                         json={"type": "notepro", "sn": "8810000000000002", "metadata": body["metadata"]})
        assert r.status_code == 403, "signature for a different device"
        r = await c.post(f"{SDK}/metadata", headers={"X-Device-Signature": sig}, json=body)
        assert r.status_code == 200 and r.json() == {"ok": True}
        # a Bearer token alone is NOT the documented credential for metadata
        r = await c.post(f"{SDK}/metadata", headers=bearer(tok), json=body)
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_version_latest_says_no_update_by_default_using_the_template_rule() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        params = {**REF, "current_version": "-1"}
        r = await c.get(f"{SDK}/version/latest", params=params)
        assert r.status_code == 401
        sig = (await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json=REF)).json()["signature"]
        r = await c.get(f"{SDK}/version/latest", headers={"X-Device-Signature": sig}, params=params)
        assert r.status_code == 200
        j = r.json()
        assert set(j) == {"type", "model", "version_type", "version_code", "version_number", "version_description",
                          "is_force", "is_strong_guidance", "file_md5", "download_url"}, "DeviceVersionResponse.java"
        # DeviceManager.kt:571: hasUpdate = serverCode > deviceCode && downloadUrl.isNotBlank()
        device_code = 1
        has_update = int(j["version_code"]) > device_code and bool(j["download_url"].strip())
        assert has_update is False
        assert j["model"] == SN_NOTEPRO[:3]


@pytest.mark.asyncio
async def test_version_latest_serves_a_configured_ota_image_with_its_md5(tmp_path: Path) -> None:
    image = tmp_path / "fw_V0042.bin"
    image.write_bytes(bytes(range(256)) * 40)
    app, _, _ = make_app(ota_image_path=image, ota_version_code=42)
    async with client(app) as c:
        tok = await user_token(c)
        sig = (await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json=REF)).json()["signature"]
        r = await c.get(f"{SDK}/version/latest", headers={"X-Device-Signature": sig}, params=REF)
        j = r.json()
        assert j["version_code"] == 42 and j["file_md5"] == hashlib.md5(image.read_bytes()).hexdigest()
        assert j["download_url"].startswith("http://mock/s3/mock-firmware/")
        assert "amazonaws" not in j["download_url"] and "plaud.ai" not in j["download_url"]
        dl = await c.get(j["download_url"])
        assert dl.status_code == 200 and dl.content == image.read_bytes()
        assert int(j["version_code"]) > 1 and bool(j["download_url"].strip()), "template rule -> update"


@pytest.mark.asyncio
async def test_aar_internal_token_chain_and_surface_a_aliases() -> None:
    """/api/oauth/sdk-token <- Bearer base64(appKey:appSecret) -> /api/sdk/api-token
    -> /api/sdk/config and /api/sdk/latest-version (paths BYTECODE_PROVEN)."""
    app, _, clock = make_app()
    async with client(app) as c:
        r = await c.post("/api/oauth/sdk-token")
        assert r.status_code == 401
        r = await c.post("/api/oauth/sdk-token", headers={"Authorization": f"Basic {BASIC}"})
        assert r.status_code == 401, "the AAR sends the Basic pair under the Bearer scheme name"
        r = await c.post("/api/oauth/sdk-token", headers={"Authorization": f"Bearer {BASIC}"})
        assert r.status_code == 200 and set(r.json()) == {"sdk_token", "token_type", "expires_in"}
        sdk = r.json()["sdk_token"]
        assert sdk.startswith("mock_sdk_token_")
        r = await c.post("/api/sdk/api-token", headers=bearer(sdk))
        assert r.status_code == 200 and set(r.json()) == {"api_token", "token_type", "expires_in"}
        api = r.json()["api_token"]
        r = await c.post("/api/sdk/api-token", headers=bearer(api))
        assert r.status_code == 401, "an api_token is not an sdk_token"
        r = await c.get("/api/sdk/config", headers=bearer(api))
        assert r.json() == {"permissions": {"isSupportBase": True, "isSupportDownload": True}}
        r = await c.get("/api/sdk/latest-version", headers=bearer(api), params={"model": "881"})
        assert r.status_code == 200 and r.json()["version_code"] == 0
        clock.advance(3601)
        r = await c.get("/api/sdk/config", headers=bearer(api))
        assert r.status_code == 401 and "expired" in r.json()["message"]
        # sanity: the registry's api_key is not accepted as a secret anywhere here
        bad = base64.b64encode(f"{APP.client_id}:{APP.api_key}".encode()).decode()
        r = await c.post("/api/oauth/sdk-token", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401
