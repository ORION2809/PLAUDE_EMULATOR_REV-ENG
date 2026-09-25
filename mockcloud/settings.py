"""Mock-cloud settings. Every value here is HARNESS_POLICY unless a citation
next to it says the default is DOC-EXACT (in which case only the *ability to
change it* is policy).

No real credential appears in this file. The partner app below is a synthetic
registry entry so that the documented Basic / X-Client-* auth checks have
something to check against.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: HARNESS_POLICY. The HS256 secret every token this mock issues is signed
#: with. It is a public, fixed, SYNTHETIC string so that tests can verify the
#: tokens; the real cloud's example tokens start with "eyJhbGciOiJSUz" (RS256,
#: build/docs-plaud-ai/openapi_auth.json "access_token" example) and its key
#: is of course UNKNOWN.
MOCK_JWT_SECRET = "plaud-harness-mockcloud-HS256-SYNTHETIC-SECRET-not-a-credential"

#: DOC-EXACT: openapi_file.json GeneratePresignedUrlsResponse.ChunkSize
#: example 5242880 and the "5 MB" prose (plaud-embedded_file-api-overview.md).
DOC_CHUNK_SIZE = 5 * 1024 * 1024

#: DOC-EXACT: openapi_auth.json PartnerTokenResponse.expires_in example 3600.
DOC_PARTNER_TOKEN_TTL_S = 3600

#: DOC-EXACT: openapi_auth.json UserTokenResponse.expires_in example 86400.
DOC_USER_TOKEN_TTL_S = 86400

#: DOC-EXACT: "The returned DownloadUrl is valid for 24 hours"
#: (api-reference_file-upload-api_complete-multipart-upload.md).
DOC_DOWNLOAD_URL_TTL_S = 24 * 3600

#: HARNESS_POLICY: the object-store bucket name. The docs show
#: "https://plaud-bucket.s3.amazonaws.com/..." (openapi_file.json examples);
#: the mock deliberately uses a different name so a URL can never be mistaken
#: for a real one.
MOCK_BUCKET = "plaud-bucket-mock"

#: HARNESS_POLICY: bucket that serves configured OTA images without a
#: presigned signature (see routers/sdkinternal.py version/latest).
MOCK_FIRMWARE_BUCKET = "mock-firmware"


@dataclass(frozen=True)
class PartnerApp:
    """One synthetic developer-portal app.

    The three credential kinds are DOC-EXACT names: `client_id` / `secret_key`
    for the Basic partner-token exchange and `api_key` for the X-Client-Api-Key
    transcription header ("The api_key is NOT your client_secret",
    plaud-embedded_transcription-api-overview.md). Whether the AAR's internal
    appKey/appSecret (ProximaInterfaceRelay, r7/cloud-endpoint-inventory.md
    section A) are the same credentials is UNKNOWN; HARNESS_POLICY treats
    appKey == client_id and appSecret == secret_key.
    """

    client_id: str
    secret_key: str
    api_key: str


#: SYNTHETIC. Labelled by the string itself; not a real portal credential.
DEFAULT_PARTNER_APP = PartnerApp(
    client_id="mock_client_0001",
    secret_key="mock_secret_0001_SYNTHETIC",
    api_key="mock_api_key_0001_SYNTHETIC",
)


@dataclass
class MockSettings:
    # --- identity ---------------------------------------------------------
    jwt_secret: str = MOCK_JWT_SECRET
    partner_apps: tuple[PartnerApp, ...] = (DEFAULT_PARTNER_APP,)
    partner_token_ttl_s: int = DOC_PARTNER_TOKEN_TTL_S
    user_token_default_ttl_s: int = DOC_USER_TOKEN_TTL_S
    #: HARNESS_POLICY: TTL of the AAR-internal sdk_token / api_token. The
    #: SdkTokenResponse/ApiTokenResponse models carry `expires_in` (int) but no
    #: value is observed anywhere (BYTECODE_PROVEN field, UNKNOWN value).
    sdk_token_ttl_s: int = 3600
    #: HARNESS_POLICY: RSA modulus size for gen-key. The real size is UNKNOWN.
    rsa_key_bits: int = 2048

    # --- binding ----------------------------------------------------------
    #: HARNESS_POLICY: when True an unknown SN is registered on first bind /
    #: sn-sign instead of answering the documented bare 404.
    auto_register_unknown_sn: bool = False
    #: HARNESS_POLICY: SNs pre-registered at startup. The two values are the
    #: openapi_binding.json examples (notepro 881..., and a notepins 882...).
    seed_devices: tuple[tuple[str, str], ...] = (
        ("notepro", "8810000000000001"),
        ("notepins", "8820000000000001"),
    )

    # --- files / object store --------------------------------------------
    chunk_size: int = DOC_CHUNK_SIZE
    download_url_ttl_s: int = DOC_DOWNLOAD_URL_TTL_S
    #: HARNESS_POLICY: lifetime of a part PresignedUrl; UNKNOWN for the real
    #: cloud (S3 presigns commonly default to an hour).
    presign_ttl_s: int = 3600
    #: HARNESS_POLICY: absolute base for the URLs the mock hands out. None
    #: means "the base URL of the request that asked", which is what a client
    #: pointed at `customDomain` needs.
    public_base_url: str | None = None

    # --- transcription ----------------------------------------------------
    #: HARNESS_POLICY: seconds the worker spends in each Celery state.
    task_step_s: float = 1.0
    #: HARNESS_POLICY: directories a `file://` file_url may point into. Empty
    #: means no local file is ever opened.
    local_file_roots: tuple[Path, ...] = ()

    # --- OTA --------------------------------------------------------------
    #: HARNESS_POLICY: when set, version/latest advertises this image.
    ota_image_path: Path | None = None
    ota_version_code: int = 0
    ota_version_type: str = "V"

    # --- infrastructure ---------------------------------------------------
    #: Injectable clock so tests can expire tokens and URLs deterministically.
    clock: Callable[[], float] = field(default=time.time)
    #: HARNESS_POLICY: request bodies kept in the log for GoReplay export are
    #: capped at this many bytes (object-store PUTs keep only their size).
    log_body_cap: int = 64 * 1024

    def app_by_client_id(self, client_id: str) -> PartnerApp | None:
        for app in self.partner_apps:
            if app.client_id == client_id:
                return app
        return None

    def now(self) -> float:
        return float(self.clock())
