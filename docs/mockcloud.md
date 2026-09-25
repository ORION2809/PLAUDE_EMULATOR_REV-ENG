# Layer 4 — mock cloud (`mockcloud/`)

A **local** FastAPI mock of the Plaud partner cloud contract so the harness
(and, in principle, the official template app pointed at a `customDomain`)
can exercise the whole partner flow — identity chain, device binding, SDK
key/signature provisioning, multipart upload, transcription task — with no
Plaud account, no network, and no real host. Every value it issues is
synthetic and labelled; the package contains no HTTP client and never proxies
anywhere (`tests/test_mockcloud_mock.py` proves both statically and at
runtime).

```
python -m mockcloud --port 8787 [--persist] [--auto-register-sn] [--task-step-seconds 1.0]
                    [--local-file-root DIR ...] [--ota-image PATH --ota-version-code N]
                    [--public-url http://host:port] [--chunk-size N]
```

Tests: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_mockcloud_*.py -q -p no:cacheprovider`
(43 tests, all in-process through `httpx.ASGITransport`; no port is bound).

## Evidence classes used in the code

| class | meaning |
|---|---|
| DOC-EXACT | path / field / status code stated by the official OpenAPI or docs page cited in the comment (`build/docs-plaud-ai/openapi_*.json`, `*.md`) |
| BYTECODE_PROVEN | literal in the shipped AAR (`build/evidence/javap/ALL.txt`, `build/evidence/jadx-out/sources/sdk/**`) |
| OFFICIAL_SOURCE | read from the published template apps / demo backends under `reference/plaud-org/` |
| INFERRED | follows from evidence, one detail is not literal (e.g. S3 semantics) |
| HARNESS_POLICY | the mock's own choice where the evidence is silent — labelled in code and listed below |
| UNKNOWN | evidence is silent; the mock does not pretend to know |

## Endpoint table

Prefix `P` = `/developer/api` (OpenAPI `servers[].url` minus the host).

| method / path | source of contract | fidelity |
|---|---|---|
| `POST P/oauth/partner/access-token` (Basic `client_id:secret_key`, form) | `openapi_auth.json`, `user-token-script.ts:22-28` | DOC-EXACT shape/status; HS256 claims HARNESS_POLICY |
| `POST P/oauth/partner/access-token/refresh` (Basic + form `refresh_token`) | `openapi_auth.json` | DOC-EXACT; refresh rotation HARNESS_POLICY |
| `POST P/open/partner/users/access-token` `{user_id 6-120, expires_in}` (Bearer partner) | `openapi_auth.json`; claims per `cloud.md` 2.1 + `JwtUtils.kt:16-33` | DOC-EXACT shape; `sub = client_user_<uuid5>` HARNESS_POLICY |
| `POST P/open/partner/sdk/bind` `{type, sn}` (Bearer user) | `openapi_binding.json`; per-event history `DeviceManager.kt:1091-1094` | DOC-EXACT 200 / 403 `{code,message}` / bare 404 |
| `POST P/open/partner/sdk/unbind` | `openapi_binding.json` | DOC-EXACT 200 idempotent / 404; non-owner 403 HARNESS_POLICY |
| `GET P/open/partner/sdk/binding?type&sn` | `openapi_binding.json` | DOC-EXACT tri-state + newest-first history; caller-independence of `is_bind` INFERRED; entry = UUID part of `sub` INFERRED (doc example vs `cloud.md` contradiction recorded) |
| `POST P/open/partner/sdk/gen-key` (Bearer user) → `{public_key, private_key}` PEM | `GenKeyResponse.java`; `protocol-ledger.md` 4.5 (PKCS#8, private key to client) | BYTECODE_PROVEN shape; fresh RSA-2048 / SPKI HARNESS_POLICY |
| `POST P/open/partner/sdk/sn-sign` `{type, sn}` → `{signature}` | `SnSignRequest.java`, `SnSignResponse.java`, `r3.A == "sn"` | BYTECODE_PROVEN shape; RSA-PKCS1v15/SHA-256 over a mock payload HARNESS_POLICY |
| `POST P/open/partner/sdk/sn-verify` `{type, sn, signature}` → `{is_valid}` | `SnVerifyRequest.java`, `SnVerifyResponse.java` | BYTECODE_PROVEN shape; verification against the mock signer |
| `POST P/open/partner/sdk/metadata` (`X-Device-Signature`) `{type, sn, metadata{version, config{battery}}}` | `PartnerApiService.java`, `MetadataRequest.java`, `DeviceMetadata*.java`, `ALL.txt:20772` | BYTECODE_PROVEN header + body; 200 `{"ok":true}` HARNESS_POLICY (AAR only checks `isSuccessful`); missing 401 / foreign 403 HARNESS_POLICY |
| `GET P/open/partner/sdk/version/latest?type&sn&current_version` (`X-Device-Signature`) | `DeviceManager.kt:555-558`; fields `DeviceVersionResponse.java`; rule `DeviceManager.kt:571` | OFFICIAL_SOURCE request, BYTECODE_PROVEN fields; "no update" = `version_code 0` + empty `download_url` HARNESS_POLICY; OTA image via `--ota-image` |
| `POST /api/oauth/sdk-token` (`Bearer base64(appKey:appSecret)`) → `{sdk_token, token_type, expires_in}` | `ApiService.java`, `SdkTokenResponse.java`, inventory §A | BYTECODE_PROVEN path/header/fields; opaque token values HARNESS_POLICY; served although the template says it 404s on platform-us |
| `POST /api/sdk/api-token` (Bearer sdk_token) → `{api_token, …}` | `ApiService.java`, `ApiTokenResponse.java` | BYTECODE_PROVEN shape; values HARNESS_POLICY |
| `GET /api/sdk/config` → `{permissions{isSupportBase, isSupportDownload}}` | `TokenPermissionResponse.java`, `Permissions.java` | BYTECODE_PROVEN shape; `true/true` HARNESS_POLICY |
| `GET /api/sdk/latest-version?sn_type&model&version_type` (Bearer api_token) | `ApiService.getLatestDeviceVersionNew` (defaults `notepin`, `V`) | BYTECODE_PROVEN query names; same body as version/latest |
| `POST P/open/partner/files/upload/generate-presigned-urls` `{filesize, filetype mp3\|opus}` → `{FileId, UploadId, ChunkSize 5242880, Parts[{PartNumber, PresignedUrl}]}` | `openapi_file.json`; `FILE_TYPE_INVALID` from `TranscriptionManager.kt:100` | DOC-EXACT keys/ChunkSize; part count = ceil INFERRED; 400 codes HARNESS_POLICY; URLs point at `/s3/plaud-bucket-mock/…` on the mock |
| `PUT /s3/{bucket}/{key}?uploadId&partNumber&X-Mock-Expires&X-Mock-Signature` → `ETag` | `openapi_file.json` description ("PUT raw bytes … read the ETag"); S3 public behaviour | INFERRED (quoted MD5 ETag, 403 AccessDenied XML, EntityTooLarge over ChunkSize) |
| `POST P/open/partner/files/upload/complete-upload` `{file_id, upload_id, part_list[{PartNumber, ETag}], filetype, file_md5?}` → `{FileId, FileType, DownloadUrl (24 h), FileMd5}` | `openapi_file.json`; quote-stripping `TranscriptionManager.kt:161`, `PlaudAPIService.swift:105` | DOC-EXACT keys and 24 h; ascending-PartNumber assembly INFERRED (S3); 400/404 codes HARNESS_POLICY |
| `GET /s3/{bucket}/{key}?X-Mock-Expires&X-Mock-Signature` | the `DownloadUrl` | INFERRED S3 semantics; content types HARNESS_POLICY (`mp3→audio/mpeg`, `opus→audio/ogg`) |
| `POST P/open/partner/ai/transcriptions/` `{file_url, params}` (`X-Client-Id` + `X-Client-Api-Key`) → `{transcription_id "task_exec_…", status "PENDING", data {}}` | `openapi_transcription.json`, `-model.json`; templates `TranscriptionManager.kt:255-310` | DOC-EXACT; parameter union of both spec files; 401 HARNESS_POLICY |
| `GET P/open/partner/ai/transcriptions/{id}` → `{transcription_id, status, data}` (+ `message` on FAILURE) | `openapi_transcription.json`; `message` read by `TranscriptionManager.kt:299` | DOC-EXACT envelope, status enum (Celery names), result schema; walk `PENDING→STARTED→SUCCESS` HARNESS_POLICY; `message` INFERRED; 404 HARNESS_POLICY |
| `/_mock/health`, `/_mock/log[?format=gor]`, `/_mock/reset` (GET/POST), `/_mock/state`, `/_mock/devices`, `/_mock/meetings`, `/_mock/signing-key`, `/_mock/docs` | none — mock administration | HARNESS_POLICY (does not exist on any Plaud host) |

Response bodies for errors follow the binding spec's `Error` object
`{"code": int, "message": str}` (DOC-EXACT for 403 bind; reused elsewhere as
HARNESS_POLICY); the unknown-SN 404 is bare (DOC-EXACT); request validation
is 422 with `code`, `message`, `detail` (HARNESS_POLICY).

## The identity chain and the emulator

`tests/test_mockcloud_auth.py::test_user_jwt_sub_derives_a_32_hex_token_the_emulator_accepts`
reproduces `NiceBuildSdk.resolveHandshakeToken` byte-for-byte (split on `.`,
base64url no-padding decode, regex `"sub"\s*:\s*"([^"]+)"`, strip
`client_user_`, drop `-`), asserts it equals
`emulator.plaudsim.handshake.normalize_historical_id(sub)`, that the result is
exactly 32 hex characters (`protocol-ledger.md` 4.5) so `build_k3` at
`portVersion 9` neither pads nor truncates, and then writes that k3 frame to a
`PlaudPeripheral` over Bumble's virtual link and receives `l3 status 0`. The
emulator accepting the token is its own HARNESS_POLICY (`accept_any_token`);
this proves the *plumbing* between Layer 4 and Layer 1, not that real hardware
would accept it.

## Transcription results

* **Meeting directory** (shared contract `plaud-harness/meeting/1`): when the
  audio resolves to a file inside a meeting dir (`file://` under
  `--local-file-root`) or its MD5 matches a directory registered through
  `POST /_mock/meetings {"dir"}` (so an ordinary presign → PUT → complete →
  transcribe flow of the device file works), the result is the ground truth:
  `results[]` = the segments (`start`, `end`, `text`), speakers relabelled
  `Speaker N` in order of first appearance when `diarization.enabled`, 256-dim
  deterministic unit vectors under `embeddings` when `return_embedding`,
  `duration` as the documented integer. `pipeline.oracle` is preferred when
  importable (`mockcloud/oracle.py:try_pipeline_oracle` tries
  `oracle_hypothesis` / `hypothesis_from_meeting_dir` / `oracle` / `run_oracle`);
  otherwise `meeting.json` is read directly, which is the same ground truth.
* **Placeholder**: any other audio gets one segment spanning the file
  (duration parsed from the Ogg/Opus container: last granule minus pre-skip,
  RFC 7845) with the fixed text `plaud harness mock cloud placeholder transcript no asr was run`.
* **FAILURE**: `http(s)://` URLs not pointing at the mock's `/s3/` store,
  `file://` outside the allowed roots, unknown or expired `DownloadUrl`s. The
  message says explicitly that no outbound HTTP was performed.
* `mockcloud.oracle.result_to_hypothesis(data, meeting_id)` converts a SUCCESS
  `data` into the shared `plaud-harness/hypothesis/1` shape for Layer 3.

## Pointing the template app at the mock

The published templates derive **every** cloud URL from one server-domain
setting:

* Android: `RecordingStore.activeServerDomain` (default `platform-test.plaud.ai`,
  `RecordingStore.kt:110-135`), switchable at runtime in Settings → Environment
  (`SettingsFragment.kt:237-264`, restart to apply). It becomes
  `https://$domain/developer/api` for binding/upload/transcription
  (`TranscriptionManager.kt` `BASE_URL`, `DeviceManager.kt:149`), the SDK's
  `customDomain` (`DeviceManager.kt:180`, "domain only, no https://",
  `plaud-embedded_android-sdk.md:118`) and, explicitly, the Partner API base via
  `NiceBuildSdk.getPartnerApiManager().updateBaseUrl(platformHost)`
  (`DeviceManager.kt:164-170` — the SDK otherwise defaults gen-key/sn-sign to
  `platform-jp`).
* iOS: `RecordingStore.shared.activeServerDomain` → `PlaudAPIService.baseURL`
  (`PlaudAPIService.swift:15`) and `initSDK(userAccessToken:customDomain:)`
  (`DeviceManager.swift:148-154`).

Two practical caveats, both outside the mock: the templates hard-code
`https://`, so a plain-HTTP mock needs a TLS-terminating proxy in front of it
(or a one-word scheme change in `RecordingStore`), and Android additionally
needs cleartext/user-CA network-security config for that. From an Android
emulator the host machine is `10.0.2.2`; pass `--public-url http://10.0.2.2:8787`
so the presigned part URLs and the `DownloadUrl` carry a base the emulator can
reach (by default the mock uses the requesting URL's base, which is correct
when the client and the URLs share a host).

The mock's synthetic app is `client_id mock_client_0001`,
`secret_key mock_secret_0001_SYNTHETIC`, `api_key mock_api_key_0001_SYNTHETIC`
(`mockcloud/settings.py`); mint a user token exactly as `user-token-script.ts`
does against `http://127.0.0.1:8787/developer/api` and paste it where the
template expects `UserAccessToken` / `PLAUD_API_KEY`. SNs must be registered
first (`POST /_mock/devices {"sn": …}`; `8810000000000001` and
`8820000000000001` are pre-seeded) or start the mock with `--auto-register-sn`.

## Request log and GoReplay

`GET /_mock/log` returns `{count, entries[]}` with `seq`, `request_id`
(`X-Request-Id` echoed or `mockreq_…` minted, also set on the response),
`ts`, `method`, `path`, `query`, `status`, `duration_ms`, `headers`, `body`
(UTF-8, capped at 64 KiB; object-store PUT bodies keep only `body_size`).
`?format=gor` renders it in GoReplay's file format (`reference/upstream/goreplay/protocol.go:31,50-53`:
`1 <id> <ts_ns> <latency_ns>\n<raw HTTP>` records joined by `\n🐵🙈🙉\n`) so a
captured session can later be replayed against the mock with
`gor --input-file … --output-http http://127.0.0.1:8787`. Plaud's own use of
GoReplay (`cloud.md` §6, `source-map.md`) mirrors *their* traffic; what it
mirrors is UNKNOWN and nothing here claims otherwise. `/_mock/*` requests are
not logged.

## Persistence

`--persist [PATH]` writes `build/mockcloud/state.json` (gitignored via
`build/`) after every non-GET request and every worker transition; a new
process loads it (bindings, objects as base64, tasks, signatures, the mock's
RSA signing key, the log). Schema `plaud-harness/mockcloud-state/1`.

## HARNESS_POLICY register (everything the mock merely chooses)

1. HS256 JWT with the fixed public secret `MOCK_JWT_SECRET`; claims `token_use`, `iat`, `jti`; partner-token claim set entirely.
2. `sub = "client_user_" + uuid5(client_id, user_id)` (32 hex after normalisation); `user_id` claim echoes the partner id; `expires_in` honoured verbatim, default 86400.
3. Refresh-token rotation; 401 `{code, message}` for every auth failure; 422 for validation.
4. Partner-app registry with one synthetic app; the AAR's `appKey/appSecret` treated as `client_id/secret_key`.
5. Registry key `(type, sn)`; seed devices; `--auto-register-sn`; non-owner unbind → 403; `is_bind` tri-state computed from the device (`True` bound, `False` bound-before, `None` never bound); history entries are the UUID part of `sub`.
6. gen-key: fresh RSA-2048 per call, SPKI public PEM, PKCS#8 private PEM; keypairs stored per `sub` for inspection only.
7. sn-sign: PKCS#1 v1.5 / SHA-256 by a mock RSA key over `"plaud-harness mockcloud sn-sign v1\n{type}\n{sn}\n{sub}\n"`; issued signatures remembered so `X-Device-Signature` can be checked by lookup; foreign device → 403; not gated on gen-key.
8. metadata 200 body `{"ok": true}`; unknown SN → bare 404 (mirrors binding).
9. version/latest "no update" = `version_code 0`, empty `download_url`; `version_code` emitted as a JSON number; OTA image served public-read from bucket `mock-firmware`.
10. sdk_token/api_token: opaque `mock_*` strings, 3600 s; `/api/sdk/config` permissions both `true`.
11. Object store: bucket `plaud-bucket-mock`, HMAC query signature (`X-Mock-Expires`, `X-Mock-Signature`), presign TTL 3600 s, S3-style XML errors, part ETag = quoted MD5, `EntityTooLarge` above ChunkSize, part count = ceil(filesize/ChunkSize), `filesize <= 0` → 400, `FILE_TYPE_INVALID` → 400, `FILE_MD5_MISMATCH` → 400, idempotent second complete, uploads scoped to the minting `sub`.
12. Transcription: 401 on bad client keys; tasks scoped to `client_id`; walk `PENDING→STARTED→SUCCESS` with `--task-step-seconds` per hop; `message` on FAILURE; `language` `auto → ("en", "en-US")`; `language_probability` 1.0 for ground truth / 0.0 for the placeholder; `Speaker N` numbering by first appearance; embeddings = deterministic sha256-seeded unit vectors; placeholder text; `file://` roots; `duration` rounded to int.
13. Request log: `X-Request-Id` minted/echoed, `/_mock/*` excluded, 64 KiB body cap, `/s3/` bodies not retained; persistence after non-GET requests.

## Deliberately not mocked, and why

* **Billing / entitlement** (`plaud-embedded_billing.md`): prices and free
  tiers are documented prose; no endpoint, quota field or enforcement point
  exists in any source (`cloud.md` §7, §10.4). Mocking enforcement would invent
  behaviour.
* **Consumer surface C** (`api.plaud.ai`: OTP login, `/file/simple/web`,
  `/ai/transsumm`, workspace tokens): CLOUD_OBSERVED only through community
  clients, disputed token requirements, Cloudflare-defended, and irrelevant to
  the SDK/partner contract this harness targets. Time did not permit even a
  minimal subset.
* **Webhooks** (`Plaud-Signature`, surface D): event names and payloads are
  absent from the corpus (`cloud.md` §6); the partner surface polls.
* **Surface A `/api/devices/bind|unbind`, `/api/files/upload-s3/*`,
  `/api/workflows/*`**: deprecated by the v1.0.14 changelog and unused by the
  templates; the `SubmitRequest`/workflow schemas are only partially decompiled.
* **Third-party (B′) OAuth/MCP** and **legacy TntAgent (E)**: different
  products (consumer data access; host UNKNOWN and a hard-coded secret).
* **Real ASR**: no model runs; results are ground truth or a labelled
  placeholder.
* **TLS / certificates**: the template's `https://` requirement is left to a
  proxy.
* **sn-sign semantics real firmware verifies**: the true payload/algorithm is
  UNKNOWN; `PlaudPeripheral` does not consume signatures (portVersion ≥ 20 is
  refused by the emulator), so nothing downstream depends on the mock's choice.

## Not done

* Consumer subset (optional in the brief) — not started.
* `pipeline.oracle` integration is by duck-typed import; the pipeline track
  had not landed a module at the time of writing, so only the direct
  `meeting.json` path is exercised by tests.
* The mock has not been driven by the real template app or the R7 Android
  driver (would need the TLS proxy above); the contract fidelity rests on the
  cited sources, not on an observed session.

## Files

`mockcloud/{__init__,__main__,app,common,jwt,oracle,settings,state,worker}.py`,
`mockcloud/routers/{auth,binding,sdkinternal,files,objectstore,transcription,mock}.py`,
`tests/test_mockcloud_{helpers,auth,binding,sdkinternal,files,transcription,mock}.py`,
`requirements/mockcloud.txt`, this file.
