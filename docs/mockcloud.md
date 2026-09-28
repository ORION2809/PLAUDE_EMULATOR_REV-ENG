# Layer 4 — mock cloud (`mockcloud/`)

A **local** FastAPI mock of the Plaud partner cloud contract so the harness
(and, in principle, the official template app pointed at a `customDomain`)
can exercise the whole partner flow — identity chain, device binding, SDK
key/signature provisioning, multipart upload, transcription task — with no
Plaud account, no network, and no real host. Every value it issues is
synthetic and labelled; the package contains no HTTP client and never proxies
anywhere. `tests/test_mockcloud_mock.py` checks this statically (an AST scan
of `mockcloud/` for network imports, calls and dynamic imports), and every
`tests/test_mockcloud_*.py` test runs with outbound socket connects and name
lookups blocked. These are checks, not a proof: a code path no test reaches is
covered only by the static scan.

```
python -m mockcloud --port 8787 [--persist] [--auto-register-sn] [--task-step-seconds 1.0]
                    [--local-file-root DIR ...] [--ota-image PATH --ota-version-code N]
                    [--public-url http://host:port] [--chunk-size N]
```

Install: `.venv/bin/pip install -r requirements/mockcloud.txt` (fastapi,
uvicorn, cryptography; httpx, pytest-asyncio, numpy and soundfile for the
tests; pins as in `requirements/all.txt`, which a test checks). Bumble is also
needed for `tests/test_mockcloud_auth.py` (it drives the emulator).

Tests: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_mockcloud_*.py -q -p no:cacheprovider --timeout=120`
(76 tests, all in-process through `httpx.ASGITransport`; no port is bound).
Without the third-party packages the modules skip instead of aborting
collection; CI's `PLAUD_STRICT_SKIPS=1` turns such a skip into a failure.
`tests/test_mockcloud_citations.py` needs `reference/plaud-org` and skips
without it under the documented `sdk-aar` gate.

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
| `POST P/open/partner/users/access-token` `{user_id 6-120, expires_in}` (Bearer partner) | `openapi_auth.json`; claims per `cloud.md` 2.1 + `JwtUtils.kt:16-33` | DOC-EXACT shape; `client_user_` prefix BYTECODE_PROVEN (`NiceBuildSdk.java:151-155` strips it), `sub = client_user_<id>` format INFERRED (no official page states it), `<id>` = uuid5 HARNESS_POLICY |
| `POST P/open/partner/sdk/bind` `{type, sn}` (Bearer user) | `openapi_binding.json`; per-event history `DeviceManager.kt:1091-1094` | DOC-EXACT 200 / 403 `{code,message}` / bare 404 |
| `POST P/open/partner/sdk/unbind` | `openapi_binding.json` | DOC-EXACT 200 idempotent / 404; non-owner 403 HARNESS_POLICY |
| `GET P/open/partner/sdk/binding?type&sn` | `openapi_binding.json` | DOC-EXACT tri-state + newest-first history; caller-independence of `is_bind` INFERRED; entry = UUID part of `sub` INFERRED (doc example vs `cloud.md` contradiction recorded) |
| `POST P/open/partner/sdk/gen-key` (Bearer user) → `{public_key, private_key}` PEM | `GenKeyResponse.java`; `protocol-ledger.md` 4.5 (PKCS#8, private key to client); `advanced-ios-sdk.md:514` | BYTECODE_PROVEN shape; fresh RSA-2048 / SPKI HARNESS_POLICY; revokes the caller's earlier sn-sign signatures (doc sentence, see below) |
| `POST P/open/partner/sdk/sn-sign` `{type, sn}` → `{signature}` | `SnSignRequest.java`, `SnSignResponse.java`, `r3.A == "sn"` | BYTECODE_PROVEN shape; RSA-PKCS1v15/SHA-256 over a mock payload that includes the caller's current key-pair fingerprint HARNESS_POLICY |
| `POST P/open/partner/sdk/sn-verify` `{type, sn, signature}` → `{is_valid}` | `SnVerifyRequest.java`, `SnVerifyResponse.java` | BYTECODE_PROVEN shape; verification against the mock signer AND the caller's current key pair |
| `POST P/open/partner/sdk/metadata` (`X-Device-Signature`) `{type, sn, metadata{version, config{battery}}}` | `PartnerApiService.java:36` (the `@Header`), `MetadataRequest.java`, `DeviceMetadata*.java`; the literal also in an OkHttp interceptor at `ALL.txt:20772` | BYTECODE_PROVEN header + body; 200 `{"ok":true}` HARNESS_POLICY (AAR only checks `isSuccessful`); missing 401 / foreign or revoked 403 HARNESS_POLICY |
| `GET P/open/partner/sdk/version/latest?type&sn&current_version` (`X-Device-Signature`) | `DeviceManager.kt:555-558`; fields `DeviceVersionResponse.java`; rule `DeviceManager.kt:571` | OFFICIAL_SOURCE request, BYTECODE_PROVEN fields; "no update" = `version_code 0` + empty `download_url` HARNESS_POLICY; OTA image via `--ota-image` |
| `POST /api/oauth/sdk-token` (`Bearer base64(appKey:appSecret)`) → `{sdk_token, token_type, expires_in}` | `ApiService.java`, `SdkTokenResponse.java`, inventory §A | BYTECODE_PROVEN path/header/fields; opaque token values HARNESS_POLICY; served although the template says it 404s on platform-us |
| `POST /api/sdk/api-token` (Bearer sdk_token) → `{api_token, …}` | `ApiService.java`, `ApiTokenResponse.java` | BYTECODE_PROVEN shape; values HARNESS_POLICY |
| `GET /api/sdk/config` → `{permissions{isSupportBase, isSupportDownload}}` | `TokenPermissionResponse.java`, `Permissions.java` | BYTECODE_PROVEN shape; `true/true` HARNESS_POLICY |
| `GET /api/sdk/latest-version?sn_type&model&version_type` (Bearer api_token) | `ApiService.getLatestDeviceVersionNew` (defaults `notepin`, `V`) | BYTECODE_PROVEN query names; same body as version/latest |
| `POST P/open/partner/files/upload/generate-presigned-urls` `{filesize, filetype mp3\|opus}` → `{FileId, UploadId, ChunkSize 5242880, Parts[{PartNumber, PresignedUrl}]}` | `openapi_file.json`; `FILE_TYPE_INVALID` from `TranscriptionManager.kt:100` | DOC-EXACT keys/ChunkSize; part count = ceil INFERRED; at most 10,000 parts (S3's limit, INFERRED; not in the Plaud corpus), more → 400 `FILE_TOO_LARGE`; 400 codes HARNESS_POLICY; `file_`/`upload_` id prefixes INFERRED from the `file_xxx`/`upload_xxx` examples; URLs point at `/s3/plaud-bucket-mock/…` on the mock |
| `PUT /s3/{bucket}/{key}?uploadId&partNumber&X-Mock-Expires&X-Mock-Signature` → `ETag` | `openapi_file.json` description ("PUT raw bytes … read the ETag"); S3 public behaviour | INFERRED (quoted MD5 ETag, 403 AccessDenied XML, EntityTooLarge over ChunkSize; the docs say both "<ChunkSize" and "up to the ChunkSize", the mock accepts exactly ChunkSize) |
| `POST P/open/partner/files/upload/complete-upload` `{file_id, upload_id, part_list[{PartNumber, ETag}], filetype, file_md5?}` → `{FileId, FileType, DownloadUrl (24 h), FileMd5}` | `openapi_file.json`; quote-stripping `TranscriptionManager.kt:158`, `PlaudAPIService.swift:118` | DOC-EXACT keys and 24 h; ascending-PartNumber assembly and discarding the parts afterwards INFERRED (S3); 400/404 codes HARNESS_POLICY |
| `GET /s3/{bucket}/{key}?X-Mock-Expires&X-Mock-Signature` | the `DownloadUrl` | INFERRED S3 semantics; content types HARNESS_POLICY (`mp3→audio/mpeg`, `opus→audio/ogg`) |
| `POST P/open/partner/ai/transcriptions/` `{file_url, params}` (`X-Client-Id` + `X-Client-Api-Key`) → `{transcription_id "task_exec_…", status "PENDING", data {}}` | `openapi_transcription.json`, `-model.json`; templates `TranscriptionManager.kt:185-238` (submit and poll) | DOC-EXACT envelope; `task_exec_` prefix INFERRED from the `task_exec_xxx` example; parameter names and JSON types DOC-EXACT (union of both spec files, unknown keys kept), a wrong type → 422 at submit HARNESS_POLICY; 401 HARNESS_POLICY |
| `GET P/open/partner/ai/transcriptions/{id}` → `{transcription_id, status, data}` (+ `message` on FAILURE) | `openapi_transcription.json`; `data.results` read at `TranscriptionManager.kt:225`, `message` at `:231` | DOC-EXACT envelope, status enum (Celery names), result schema; walk `PENDING→STARTED→SUCCESS` HARNESS_POLICY; `message` INFERRED; 404 HARNESS_POLICY |
| `/_mock/health`, `/_mock/log[?format=gor]`, `POST /_mock/reset`, `/_mock/state`, `/_mock/devices`, `/_mock/meetings`, `/_mock/signing-key`, `/_mock/docs` | none — mock administration | HARNESS_POLICY (does not exist on any Plaud host) |

Response bodies for errors follow the binding spec's `Error` object
`{"code": int, "message": str}` (DOC-EXACT for 403 bind; reused elsewhere as
HARNESS_POLICY); the unknown-SN 404 is bare (DOC-EXACT); request validation
is 422 with `code`, `message`, `detail` (HARNESS_POLICY; `detail` holds each
error's `type`, `loc`, `msg` and a JSON-safe, truncated `input`). Malformed
credentials (a non-ASCII byte, a list-shaped or non-canonical JWT) are 401,
and a malformed presigned signature is 403, never a crash. Anything that still
escapes a route is answered by the request-log middleware as
`500 {"code": 500, "message": "mock internal error (<type>)"}` with
`X-Request-Id`, logged with status 500 and an `error` field, and the traceback
goes to the `mockcloud` logger. Starlette's own framing errors keep FastAPI's
`{"detail": …}` body: 404/405 for unknown routes or methods, and 400 for a body
that is not UTF-8 JSON.

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
  `--local-file-root`, with the meeting dir itself inside an allowed root) or
  its MD5 matches a directory registered through `POST /_mock/meetings {"dir"}`
  (so an ordinary presign → PUT → complete → transcribe flow of the device
  file works), the result is the ground truth: `results[]` = the segments
  (`start`, `end`, `text`), speakers relabelled `Speaker N` in order of first
  appearance when `diarization.enabled`, 256-dim deterministic unit vectors
  under `embeddings` when `return_embedding`, `duration` as the documented
  integer.
* **Which ground-truth path ran** is recorded in the task's `source` (shown by
  `GET /_mock/state`). `mockcloud/oracle.py:try_pipeline_oracle` calls the
  pipeline's own oracle, `pipeline.oracle.OraclePipeline().run(audio, meeting_dir)`
  (`pipeline/oracle.py:47-71`), and serves its `Hypothesis.segments`:
  `source` ends `+pipeline-oracle`. If `pipeline` does not import, `source`
  ends `+meeting.json(pipeline-unavailable:<ExcType>)`; if it imports but
  refuses the meeting (`pipeline.meeting.read_meeting` validates more strictly
  than the mock, e.g. it requires `channels`), `source` ends
  `+meeting.json(pipeline-error:<ExcType>)`. In both fallbacks the mock reads
  `meeting.json` itself, which is the same ground truth. Full values are
  `file+meeting+…` or `objectstore+meeting+…`; `file` / `objectstore` alone
  mean the placeholder below.
* **file:// and ground truth** (HARNESS_POLICY): `meeting.json` is found by
  walking up to two directories above the file, and is used only if that
  meeting directory is itself inside a `--local-file-root`. With
  `--local-file-root <meeting>/device` the device file is served but its
  transcript is the placeholder, unless the meeting was registered through
  `/_mock/meetings`.
* **Placeholder**: any other audio gets one segment spanning the file
  (duration parsed from the Ogg/Opus container: last granule minus pre-skip,
  RFC 7845) with the fixed text `plaud harness mock cloud placeholder transcript no asr was run`.
* **FAILURE**: `http(s)://` URLs not pointing at the mock's `/s3/` store,
  `file://` outside the allowed roots, unknown or expired `DownloadUrl`s. The
  message says explicitly that no outbound HTTP was performed.
* `mockcloud.oracle.result_to_hypothesis(data, meeting_id)` converts a SUCCESS
  `data` into the shared `plaud-harness/hypothesis/1` shape for Layer 3.
* **Parameters**: `params.transcribe{language, model, detection_level}`,
  `vad{decode_silence}`, `diarization{enabled, return_embedding}` and
  `hotwords` (a comma-separated string) are type-checked at submit
  (`openapi_transcription.json:181-229` plus `model` from
  `openapi_transcription-model.json:186-190`). A wrong JSON type, e.g.
  `"diarization": true`, is a 422 and no task is created. Scalars are strict:
  `"yes"` or `1` for a boolean, or a number for a string, is also a 422
  (HARNESS_POLICY). Unknown keys are accepted and stored as sent.
* **Speaker labels**: the OpenAPI example uses `"Speaker 1"` (DOC-EXACT), which
  the mock emits. A template comment expects `"SPEAKER_00"` and rewrites
  `SPEAKER_` to `Speaker ` for display (`FileDetailActivity.kt:235-239`,
  `FileDetailViewController.swift:570`). The contradiction is recorded, not
  resolved; both forms display as `Speaker N` in the templates.

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
`ts`, `method`, `path`, `query`, `status`, `duration_ms`, `headers` (including
`host`; `content-length`, `transfer-encoding`, `connection`,
`accept-encoding` and `user-agent` are not kept), `body` (UTF-8, capped at
64 KiB), `body_size` and `body_truncated`. Object-store PUT bodies keep only
`body_size`. A body that is not UTF-8 is dropped and marked
(`body_truncated: true`, `body_undecodable: true`). A request that ended in an
unhandled error also carries `error`.

`?format=gor` renders it in GoReplay's file format (`reference/upstream/goreplay/protocol.go:31,50-53`:
`1 <id> <ts_ns> <latency_ns>\n<raw HTTP>` records joined by `\n🐵🙈🙉\n`).
Every record carries `Host` (the logged one, or `127.0.0.1:8787` for old
entries without it) and a `Content-Length` equal to the UTF-8 length of the
body written after it. gor's output-http parses records with Go's
`http.ReadRequest` (`reference/upstream/goreplay/output_http.go:402`), and a
request with neither Content-Length nor Transfer-Encoding has an empty body,
so without the header every replayed POST would lose its body. Bodies that
were not kept are marked `X-Mock-Body-Truncated: <original size>`. The
intended replay is `gor --input-file … --output-http http://127.0.0.1:8787`.
The test parses every exported record with h11 (an RFC 9112 parser) and checks
that the delivered body equals the logged one. **Since 28 Sep 2026 gor itself
has replayed it:** `scripts/crosscheck-goreplay.sh` builds `gor` (GoReplay
2.0.0) from `reference/upstream/goreplay` at its pinned commit (a `git archive`
copy, with Go 1.27.1 fetched and sha256-checked into git-ignored
`data/tools/`), drives the mock with `docker/cloud_roundtrip.py`, exports
`?format=gor`, replays it with `gor --input-file … --output-http` into a
recording server, and compares as multisets. All 14 logged requests (13 of the
round trip plus the readiness probe) arrived with identical method, target and
body. The three object-store part uploads, whose bodies are not kept, arrived
with `X-Mock-Body-Truncated: 20000` and `9161`. gor replays concurrently, so
order is not preserved. Plaud's own use of GoReplay (`cloud.md` §6, `source-map.md`)
mirrors *their* traffic; what it mirrors is UNKNOWN and nothing here claims
otherwise. `/_mock/*` requests are not logged.

## Persistence

`--persist [PATH]` (default `build/mockcloud/state.json`, gitignored via
`build/`) saves after every non-GET request and after every worker status
transition. Schema `plaud-harness/mockcloud-state/1`. Layout:

| file | holds | written |
|---|---|---|
| `state.json` | every table (tokens, devices, signatures, uploads, files, tasks, meetings, the mock's RSA signing key) with object metadata only | rewritten at each save (metadata only) |
| `state.objects/<sha256>` | one file per distinct object body | once, when first stored; deleted when no object references it |
| `state.log.jsonl` | the request log, one JSON entry per line | appended; rewritten after a reset |

A save costs the metadata plus whatever is new, not a base64 rewrite of every
stored byte. Part objects are deleted when `complete-upload` merges them, so
an upload is stored once. An older single-file snapshot (objects as
`data_b64`, the log inline) still loads, and the next save converts it.

Tasks restored in a non-terminal state (the previous process stopped while
they were `PENDING` or `STARTED`) are rescheduled when the new process starts:
by the app's lifespan hook under uvicorn, or on the first HTTP request when
the transport sends no lifespan events (`httpx.ASGITransport`). The walk
continues from the persisted state (HARNESS_POLICY: resume, not fail).

## HARNESS_POLICY register (everything the mock merely chooses)

1. HS256 JWT with the fixed public secret `MOCK_JWT_SECRET`; claims `token_use`, `iat`, `jti`; partner-token claim set entirely.
2. `sub = "client_user_" + uuid5(client_id, user_id)` (32 hex after normalisation); `user_id` claim echoes the partner id; `expires_in` honoured verbatim, default 86400. The refresh token is an opaque `mock_refresh_…` string, although the doc example looks like a JWT (`eyJhbGci...`).
3. Refresh-token rotation; 401 `{code, message}` for every auth failure, including malformed tokens and secrets; 422 for validation; strict, canonical base64url JWT segments (padding, `+`/`/`, junk or unused low bits set → 401).
4. Partner-app registry with one synthetic app; the AAR's `appKey/appSecret` treated as `client_id/secret_key`.
5. Registry key `(type, sn)`; seed devices; `--auto-register-sn`; non-owner unbind → 403; `is_bind` tri-state computed from the device (`True` bound, `False` bound-before, `None` never bound); history entries are the UUID part of `sub`.
6. gen-key: fresh RSA-2048 per call, SPKI public PEM, PKCS#8 private PEM; the latest pair is stored per `sub`; each call revokes every signature issued earlier to that `sub`.
7. sn-sign: PKCS#1 v1.5 / SHA-256 by a mock RSA key over `"plaud-harness mockcloud sn-sign v2\n{type}\n{sn}\n{sub}\n{key_fp}\n"`, where `key_fp` is the sha256 of the caller's current gen-key public PEM. This models `advanced-ios-sdk.md:514`: "A new key pair invalidates every cached sn-sign signature, since signatures are bound to the key pair that produced them". The sentence is DOC-EXACT, but it describes SDK cache behaviour; what the real server does with a stale signature is UNKNOWN, and the mock refuses one (sn-verify → `is_valid: false`; metadata and version/latest → 403 "revoked"). Issued signatures are remembered so `X-Device-Signature` can be checked by lookup; foreign device → 403. **Deviation:** sn-sign without any earlier gen-key is still allowed, bound to the fixed fingerprint `no-gen-key`, because harness flows often need only a signature. The doc binds signatures to a key pair; such a signature is also revoked by the caller's next gen-key.
8. metadata 200 body `{"ok": true}`; unknown SN → bare 404 (mirrors binding).
9. version/latest "no update" = `version_code 0`, empty `download_url`; `version_code` emitted as a JSON number; OTA image served public-read from bucket `mock-firmware`.
10. sdk_token/api_token: opaque `mock_*` strings, 3600 s; `/api/sdk/config` permissions both `true`.
11. Object store: bucket `plaud-bucket-mock`, HMAC query signature (`X-Mock-Expires`, `X-Mock-Signature`), presign TTL 3600 s, S3-style XML errors, part ETag = quoted MD5, `EntityTooLarge` above ChunkSize, part count = ceil(filesize/ChunkSize) capped at 10,000 (`FILE_TOO_LARGE` → 400), `filesize <= 0` → 400, `FILE_TYPE_INVALID` → 400, `FILE_MD5_MISMATCH` → 400, idempotent second complete, part objects deleted after complete, uploads scoped to the minting `sub`.
12. Transcription: 401 on bad client keys; params type-checked at submit (422); tasks scoped to `client_id`; walk `PENDING→STARTED→SUCCESS` with `--task-step-seconds` per hop; `message` on FAILURE; `language` `auto → ("en", "en-US")`; `language_probability` 1.0 for ground truth / 0.0 for the placeholder; `Speaker N` numbering by first appearance; embeddings = deterministic sha256-seeded unit vectors; placeholder text; `file://` roots, with ground truth only from a meeting dir inside a root; `duration` rounded to int; `source` records the oracle path.
13. Request log: `X-Request-Id` minted/echoed, `/_mock/*` excluded, `host` kept, 64 KiB body cap, `/s3/` bodies not retained, non-UTF-8 bodies dropped and marked; GoReplay export writes `Host` and `Content-Length`, fallback host `127.0.0.1:8787`; an unhandled error becomes a logged 500.
14. Persistence: split layout (`state.json`, `state.objects/`, `state.log.jsonl`); a save after each non-GET request and each worker transition; restored non-terminal tasks resumed on start.
15. Admin surface: `/_mock/reset` is POST-only (a GET could come from any web page in the operator's browser); `/_mock/meetings` reads only files that resolve inside the meeting directory and lists the rest under `skipped`.

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

## Fidelity-label review (2026-09-25)

Labels were re-checked against `build/docs-plaud-ai/` and the template
sources. Changes:

* `new_id` prefixes `task_exec_` / `file_` / `upload_`: DOC-EXACT → INFERRED.
  The docs give only placeholder examples (`task_exec_xxx`, `file_xxx`,
  `upload_xxx`) and never state an id format.
* `sub = client_user_<id>`: DOC-EXACT → BYTECODE_PROVEN prefix + INFERRED
  format. `docs/architecture/cloud.md` §2.1 is the harness's own synthesis. The
  official pages mention only a "raw `client_user_id`"
  (`plaud-embedded_ios-sdk.md:379`, `plaud-embedded_android-sdk.md:444`).
* Seed SNs: only `8810000000000001` is the `openapi_binding.json` example;
  `8820000000000001` is the mock's notepins analogue.
* sn-sign "not gated on gen-key" was listed as policy where the evidence is
  silent. `advanced-ios-sdk.md:514` is not silent, so the gen-key binding is
  now modelled and the remaining gen-key-less case is labelled a deviation
  (register item 7).
* Template cites corrected: `TranscriptionManager.kt` `:225` (results),
  `:231` (message), `:185-238` (submit and poll) and `:158` (ETag strip);
  `PlaudAPIService.swift:118` (ETag strip); `route.ts:28-33`;
  `PartnerApiService.java:36` for the `X-Device-Signature` `@Header`. The
  earlier cites (lines 294, 299 and 255-310 of `TranscriptionManager.kt`)
  pointed past the end of a 270-line file. `tests/test_mockcloud_citations.py` now checks that every
  template cite in `mockcloud/` and this file is inside its file, and that
  the corrected ones land on the quoted construct.
* Unchanged after checking: the DOC-EXACT paths, bodies and response keys of
  all four OpenAPI files; the 3600 / 86400 / 5 242 880 / 24 h values (doc
  examples or prose, labelled as such); the bare 404 and the 403 body; the
  tri-state `is_bind` wording; the `mp3|opus` filetype set; the Celery status
  enum; the 256-dim embedding annotation.

## Not done / known limits

* Consumer subset (optional in the brief) — not started.
* The mock has not been driven by the real template app or the R7 Android
  driver (would need the TLS proxy above); the contract fidelity rests on the
  cited sources, not on an observed session.
* Saves are synchronous on the event loop. After the split layout they cost
  the metadata JSON plus what is new, but `state.json` still grows with tokens
  and task results, and `state.log.jsonl` grows without rotation.
* The GoReplay export was replayed by gor itself on 28 Sep (above). Header
  values are re-emitted as text, so a non-ASCII header byte is re-encoded as
  UTF-8.
* Starlette-level errors (unknown route 404, wrong method 405, undecodable
  body 400) keep FastAPI's `{"detail": …}` body instead of the Error schema.
* Rejecting a stale sn-sign signature at metadata / version/latest follows the
  doc's "invalidates" sentence. The real server's behaviour is UNKNOWN.

## Files

`mockcloud/{__init__,__main__,app,common,jwt,oracle,settings,state,worker}.py`,
`mockcloud/routers/{auth,binding,sdkinternal,files,objectstore,transcription,mock}.py`,
`tests/test_mockcloud_{helpers,auth,binding,sdkinternal,files,transcription,mock,errors,citations}.py`,
`requirements/mockcloud.txt`, this file.
