# Cloud layer — surfaces, identity, registry, storage, processing

Scope: everything Plaud's servers do, as far as it can be established without
calling anything. Sources by class: **OFFICIAL_DOC** (docs.plaud.ai, fetched
2026-09-23, hashes in `build/docs-plaud-ai/manifest.json`), **OFFICIAL_SOURCE**
(template apps, wrappers, skills, demo backends), **OFFICIAL_BINARY** (AAR
bytecode; the `@plaud-ai/mcp` and `@plaud-ai/cli` npm bundles,
`build/npm-plaud/manifest.json`), **CLOUD_OBSERVED** (community clients that
talked to the live cloud with their own accounts: riffado, plaud-api,
plaud-toolkit, applaud, python-plaud-ai), **CORROBORATED** (≥ 2 independent
sources), **INFERRED**, **UNKNOWN**. No endpoint was exercised in this project;
every response shape below comes from a fixture, a type declaration, a doc, or
a client that reads it.

## 1. Five surfaces, not one

| | base | auth | documented by | who uses it |
|---|---|---|---|---|
| **A. SDK-internal** `/api/…` | `ServerEnvironment` enum in the AAR; default `US_PROD = https://platform-jp.plaud.ai` (also `platform.plaud.cn`, `dev-api-us-test.plaud.work`, `platform-beta.plaud.ai`) | 3-hop Bearer chain: `sdk-token` ← `Basic-as-Bearer base64(appKey:appSecret)`; `api-token` ← sdk_token; rest ← api_token | bytecode only (R7 inventory §A) | the AAR's own `checkFirmwareUpdate`/workflow code paths |
| **B. Partner (Embedded)** `/developer/api/open/partner/…` + `/developer/api/oauth/partner/…` | `platform-us.plaud.ai`, `platform-jp.plaud.ai` (OpenAPI servers); `platform-eu`, `platform-sg` by sales contact; templates also know `platform-us-pre` and default to **`platform-test`** | user JWT (Bearer) for SDK/binding/upload; `X-Client-Id` + `X-Client-Api-Key` for AI; `X-Device-Signature` for firmware/metadata | OpenAPI ×5, docs, template apps, wrappers | partner apps + partner backends |
| **B′. Third-party (MCP/CLI)** `/developer/api/open/third-party/…` + `/developer/api/oauth/third-party/…` | `platform.plaud.ai` (CLI default `api_base`) | OAuth authorization-code + PKCE at `web.plaud.ai/platform/oauth`; dynamic client registration with HMAC-signed client ids; hosted remote MCP `mcp.plaud.ai/mcp` | official npm bundles + docs | a **consumer's own** recordings, from AI clients / terminal |
| **C. Consumer web** `api.plaud.ai` (+ `api-euc1`, `api-apse1`, `api-usw1`, `api.plaud.cn`) | in-band region redirect `{status:-302, data.domains.api}` | user token (UT) / workspace token (WT) / cookie session (`pld_ut`, `pld_urt`) | community clients only | `web.plaud.ai` and the Plaud app (INFERRED) |
| **D. Developer platform (docs form of A)** `platform.plaud.ai/api` | OAuth client-credential `api_token` | python-plaud-ai (docs-mirroring; its shipped client cannot authenticate) | partner backends (older workflow model) |
| **E. Legacy TntAgent** `/recorder/device/{checkSn,checkCustomer,saveOperation}` | query signature over a hard-coded secret; **host UNKNOWN** | bytecode only | never seen exercised |

Two consequences the evidence forces:

1. **Surfaces A and B target the same operations with different paths and
   tokens** (upload, device bind, latest version, AI processing). Plaud's own
   Android template says the A-side token endpoint `/api/oauth/sdk-token`
   "404s on platform-us" and routes around it; the official changelog
   deprecates the A-side workflow queries on 2026-09-14 (v1.0.14). **Plaud's
   own low-level SDK reference acknowledges the split**: it states that
   `NiceBuildSdk.bindDevice/unbindDevice` "are not the cloud bind documented
   in the Android SDK reference — they post to a different service", and that
   Android's own firmware check queries `GET /api/sdk/latest-version`
   (`build/docs-plaud-ai/plaud-embedded_advanced-android-sdk.md:274-276,440-442`;
   OFFICIAL_DOC). Reading: A is an older internal contract still compiled into
   the AAR and now being retired; B is the product. Whether one host aliases
   the other is UNKNOWN.
2. **B′ and C are disjoint from B**: different hosts, nouns and tokens. A
   partner app never sees a consumer's library; a consumer's MCP never sees
   partner devices. The only bridge is the recording itself, which exists in
   C's library only if the *Plaud app* put it there — a path no official
   source shows (the partner surface has no library at all).

## 2. Identity and tokens

### 2.1 Partner chain (OFFICIAL_DOC + OFFICIAL_SOURCE + OpenAPI)

```
Developer Portal ──issues──▶ CLIENT_ID, CLIENT_SECRET (a.k.a. secret_key), API_KEY (App Settings › API Keys; "NOT your client_secret")
partner backend  ──POST /oauth/partner/access-token  Basic base64(client_id:secret_key), form-urlencoded, empty body
                 ◀── {access_token, refresh_token, token_type:"bearer", expires_in: 3600}
                 ──POST /oauth/partner/access-token/refresh {refresh_token}   ◀── same shape
                 ──POST /open/partner/users/access-token  Bearer <partner token>  {user_id (6–120 chars, stable), expires_in (e.g. 86400)}
                 ◀── {access_token (JWT), token_type, expires_in}   — no refresh token for user tokens
```

The user JWT carries `sub`, `user_id` (defaults to `sub`), `exp`, `client_id`.
`sub` is `client_user_<id>`; the SDK derives the BLE handshake token from it
by stripping the prefix and every `-` (bytecode + template comments + R7-S12
runtime), and the same `client_user_…` ids come back from the cloud in
`bind_history`. **X-Client-Id equals the JWT `client_id` claim.** Whether the
server honours `expires_in` values other than 86400, and the partner-token TTL
beyond the documented 3600 s example, are UNKNOWN.

### 2.2 Third-party (consumer data) OAuth — OFFICIAL_BINARY

`@plaud-ai/mcp` 0.3.13 / `@plaud-ai/cli` 0.3.14: browser authorization at
`https://web.plaud.ai/platform/oauth`; token exchange
`POST https://platform.plaud.ai/developer/api/oauth/third-party/access-token`
(+ `/refresh`); `grant_type=authorization_code`, PKCE (`code_verifier` /
`code_challenge`), RFC 8707 `resource`; the hosted MCP server implements RFC
7591 dynamic registration (`/register`) and issues client ids of the form
`<rawId>.<base64url(hmac)>` (env `PLAUD_DCR_HMAC_SECRET`), plus
`/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`,
and a ChatGPT-Apps challenge route. Local tokens: `~/.plaud/tokens.json`
(CLI) and `~/.plaud/tokens-mcp.json` (MCP), refreshed automatically; callback
port 8199. `GET /open/third-party/users/current`, `POST …/current/revoke`.

### 2.3 Consumer web tokens — CLOUD_OBSERVED, CORROBORATED

Three credential generations coexist (four clients agree on the mechanism,
disagree on lifetimes — itself evidence of a cloud-side migration):

| generation | how obtained | lifetime observed | notes |
|---|---|---|---|
| password login | `POST /auth/access-token` (form `username`, `password`) → `{status:0, access_token, token_type}`; `-2` wrong password | ~300 d historically → **~30 d now** (plaud-toolkit retracts its own 300-day assumption) | each login mints a new `sid` and **evicts the account's other sessions** (logs the phone app out) — single client's claim |
| OTP login | `POST /auth/otp-send-code {username, user_area}` → `{token}` (may `-302`); `POST /auth/otp-login {code, token, user_area}` → UT | ~300 d (riffado) | Google/Apple-created accounts get a separate empty "shadow" account under the same email |
| cookie session (newest) | `pld_ut` (~1 d) + `pld_urt` (~30 d), rotated by `POST /auth/refresh-user-token` (Set-Cookie, `Max-Age=0` clearers first, "erratic") | | `web.plaud.ai` moved from `localStorage.tokenstr`/`pld_tokenstr` (`"bearer eyJ…"`) to HttpOnly cookies for newer accounts |

Workspace tokens: `GET /team-app/workspaces/list?need_personal_workspace=true`
(Bearer UT) → `workspaces[]{workspace_id "ws_*", member_id "mem_*", role, status,
workspace_type "0" = personal, region, api_domain}`; `POST
/user-app/auth/workspace/token/{wid}` → `{workspace_token, expires_in (~24 h),
refresh_token, refresh_expires_in (~30 d), member_id, role}`. WT JWT claims:
`sub, aud, client_id, region, jti, mfa_method, mid, role, ut_ref, ver, wid,
wtype, exp` (`typ=WT`). UT claims: `sub` (32-hex), `aud:""`, `client_id:"web"`,
`region ("aws:us-west-2" | "aws:eu-central-1" | "aws:ap-southeast-1" | "aws:us-east-1")`,
`iat`, `exp`, `sid`. **Which data endpoints require a WT is disputed**: riffado
and applaud mint one (on EU/APAC a bare UT returns HTTP 200 with an *empty
list*), plaud-api/plaud-toolkit send the UT and report success. No client uses
the WT refresh token.

Edge: `api*.plaud.ai` is behind Cloudflare; non-browser User-Agents get 403 or
an HTML challenge; riffado sends full Chrome client hints, an
`Origin: https://web.plaud.ai`, routes through a **rotating Webshare proxy
pool** and rate-limits itself — i.e. the consumer API is actively defended
against automation (CORROBORATED 4/4 on the UA rule).

## 3. Device registry and binding (surface B)

Binding is **two-layered** (OFFICIAL_DOC, OFFICIAL_SOURCE, bytecode): a *cloud
bind* records the device/owner association; a *local bind* over BLE "verifies
and generates the key pair on-device" and locks the firmware to one
`client_user_id` ("necessary for offline encryption; a stolen device stays
inaccessible"). The SDK never calls the cloud bind — the **partner app** does,
after every successful handshake.

| endpoint | request | response | semantics |
|---|---|---|---|
| `POST /open/partner/sdk/bind` | Bearer user token; `{type: notepro\|notepins, sn}` | `{type, sn, is_bind:true}`; `403 {code, message}` = bound to another account; bare `404` = unknown SN | idempotent for the same owner |
| `POST /open/partner/sdk/unbind` | same | `{…, is_bind:false}` | idempotent; "unbind over both cloud and BLE" |
| `GET /open/partner/sdk/binding?type&sn` | Bearer user token | `{is_bind: true\|false\|null, bind_history: [client_user_id…]}` | `true` = another account owns it (stop); `false` = unbound; `null` = "signed but never bound"; history newest-first, **one entry per bind event** (dedupe), and — per a template comment — **not client-scoped** |

`type` derives from the SN prefix (`881→notepro`, `882→notepins`) and "is the
cloud registry key together with the SN". The consumer side lists devices
separately: `GET /device/list` → `data_devices[{sn, name, model ("888"…),
version_number}]` (CLOUD_OBSERVED); the developer-platform form is
`POST devices/bind {owner_id, sn_type ∈ note|notepin|notepro, sn}` (docs-mirroring).
Whether `file.serial_number` on consumer recordings equals the device SN is
plausible but unverified.

Recovery: the cloud supplies history, the app iterates it through
`recoveryConnectBleDevice` (force-clear), the device wipes the stale lock on
`depair`. R7-S12 proved the SDK half; the cloud half is OFFICIAL_DOC.

## 4. Recording storage, upload and delivery

### 4.1 Partner file upload (surface B; OpenAPI `file.json`)

```
POST /open/partner/files/upload/generate-presigned-urls  Bearer user token  {filesize, filetype: "mp3"|"opus"}
  → {FileId, UploadId, ChunkSize: 5242880, Parts[{PartNumber, PresignedUrl}]}      (bucket plaud-bucket.s3.amazonaws.com)
PUT  <PresignedUrl>  raw chunk, no auth → ETag header (keep it)
POST /open/partner/files/upload/complete-upload  {file_id, upload_id, part_list[{PartNumber, ETag}], filetype, file_md5?}
  → {FileId, FileType, DownloadUrl (≈24 h), FileMd5}
```

Docs list transcription inputs as M4A/MP3/WAV while the upload API accepts
only `mp3|opus` — a doc-internal inconsistency; the templates export MP3 for
exactly this reason (comments: WAV is rejected as `FILE_TYPE_INVALID`; "the
backend's opus pipeline currently returns empty results" — CLAIMS).

### 4.2 Consumer upload and delivery (surface C; CLOUD_OBSERVED)

Upload: `POST /file/get_upload_presigned_url {filesize, file_type: MP3|OPUS}` →
`{part_urls[], upload_id, object_name}`; S3 PUT (octet-stream, ETag);
`POST /file/merge_multipart {upload_id, object_name, parts[{Etag, PartNumber}]}`;
`POST /file/confirm_upload {upload_id, object_name, scene: 101, is_tmp: 0,
support_mul_summ: true, file_type, filename, start_time, session_id (epoch s),
serial_number (uuid4 for web uploads)}` → a list row with `is_trans = is_summary = 0`.

Delivery: `GET /file/temp-url/{id}?is_opus=0|1` → `{temp_url, temp_url_opus?}`
presigned on `resource.plaud.ai` / `resource.plaud.cn` (fetched without
auth); `GET /file/download/{id}` streams bytes directly. **The bytes are
Ogg/Opus with OpusTags vendor `PALUD.AI`, served as `.mp3` / `audio/mpeg`
even for `is_opus=0`** (riffado regression #160; applaud writes `audio.ogg`;
plaud-toolkit sniffs magic bytes). The device/SDK writer tags
`TinnoTech123456789012`, so cloud artefacts are re-encoded or re-tagged
server-side (ledger U19). plaud-toolkit calls the raw `.opus` "encrypted" and
says opening the recording in the Plaud app makes an MP3 appear — UNVERIFIED,
and contradicted by the plain Ogg/Opus riffado captured.

## 5. AI processing — two system models

### 5.1 Partner transcription task (surface B; OpenAPI `transcription*.json`)

```
POST /open/partner/ai/transcriptions/     X-Client-Id + X-Client-Api-Key
  {file_url, params?: {transcribe: {language: "auto"|BCP-47, model, detection_level: "segment"|"chapter"},
                       vad: {decode_silence: false}, diarization: {enabled: false, return_embedding: false}, hotwords: "a,b"}}
  → {transcription_id: "task_exec_…", status: "PENDING", data: {}}
GET  /open/partner/ai/transcriptions/{id} → {transcription_id, status, data}
```

- `status` ∈ `PENDING, RECEIVED, STARTED, PROGRESS` (in flight) · `SUCCESS` ·
  `FAILURE, REVOKED` (terminal). This is **exactly Celery's task-state set**,
  including the two states only Celery names (`RECEIVED`, `REVOKED`) — strong
  INFERRED evidence for a Celery task backend; the `task_exec_` id prefix fits.
- `data` on success: `{text, language, duration (s), results[{start, end, text,
  speaker_id, language, language_probability}], embeddings: {"Speaker N": [… 256 dim]}}`.
- Models (`transcription-model.json`): **`plaud-fast-whisper`** (default),
  **`plaud-omni-3`**, **`azure-fast-transcribe`** — a router that includes a
  third-party provider (Azure AI Speech fast transcription). The playground
  names a fourth, `plaud-transcribe-1`. The two spec files disagree on which
  parameters exist (`hotwords` + `embeddings` vs `model`).
- Doc-stated limits: 60 req/min; 24 h max recording; 6 h max with diarization;
  "chunk recordings > 5 h"; results **retained 7 days** by default; pipeline
  steps "noise reduction, speaker detection, language recognition"; 112 languages.
- Clients: template polls 5 s × 240; Capacitor 5 s × 120; RN/Flutter 3 s × 60
  and defensively parse alternate shapes (`data.task_id`, `data.task_status`,
  `segments[]`, `full_text`) that no doc describes — a Flutter comment cites
  "an original bug where SUCCESS tasks hung".
- Ownership: the partner backend keeps `API_KEY` off the client and proxies
  submit/poll (Capacitor demo routes `user-token`, `presign`, `complete`,
  `submit`, `status/[id]`); the cloud owns the job.

The iOS SDK binary additionally carries an **undocumented Workflow API**
(`WorkflowSubmitRequest{workflows[{taskType: audioTranscribe|aiSummarize|aiEtl|audioMerge|custom,
taskParams}], metadata{organizationId, ownerId, deviceSn}}`, with ETL types such
as `clinicalReport`/`dealAnalysis`) that matches surface A's
`/api/workflows/*` and is what v1.0.14 deprecated. Its `TranscriptResult`
segment (`speaker, index`) differs from the Embedded segment (`speaker_id`) —
two Plaud transcription result schemas exist.

### 5.2 Consumer transcript + summary (surface C; CLOUD_OBSERVED, single-client where noted)

There is **no dedicated "start job" endpoint**. Starting analysis is a
metadata write, polling is a POST, and persisting the result is the
*client's* job (plaud-api, single client):

```
PATCH /file/{id}  {extra_data: {tranConfig: {language, type_type: "system", type: "REASONING-NOTE", diarization: 1, llm: "auto"}}}
POST  /ai/transsumm/{id}  {is_reload: 0, summ_type: "REASONING-NOTE", summ_type_type: "system", info: "<json>", support_mul_summ: true}
      → {status: 0 "task processing"} … {status: 1 "task complete" | -111 "success", data_result[], data_result_summ, outline_result[], task_id_info}
PATCH /file/{id}  {trans_result, ai_content, outline_result, support_mul_summ: true, extra_data: {task_id_info, aiContentHeader}}   ("required to persist")
```

Other clients read results by two different representations of the same
content (which is canonical is UNKNOWN): `POST /file/list [ids]` rows embed
`trans_result[]`, `ai_content`, `summary_list[]`; `GET /file/detail/{id}`
returns `content_list[{data_id "source_transaction:…|auto_sum:…|sum_multi:…",
data_type, task_status (1 = ready), data_link (presigned, gzip JSON)}]` plus
inline `pre_download_content_list[{data_id, data_content}]`. `data_type`
vocabulary, **CORROBORATED across the official MCP skill and two community
clients**: `transaction` (segments), `transaction_polish` (AI-cleaned
transcript), `outline`, `auto_sum_note` (summary), `sum_multi_note`
(secondary notes), `mark_memo` / `high_light` (device highlight-button marks).
applaud observes `transsumm` returning `-12` with empty data for recordings
older than ~March 2026 while their `content_list` artefacts remain — a
storage migration is INFERRED.

Summary payload (riffado real capture): `{ai_content: <markdown>, category:
"Chat Note", summary_id: "20251119154839-v2@<hex>-1", summ_type:
"CASUAL-CONVERSATION" | "REASONING-NOTE", header: {headline, keywords[]}, state: 10}`;
`ai_content` is polymorphic (markdown, `{markdown}`, `{content:{markdown}}`,
legacy `{summary}`); summaries ship with unresolved template variables
(`$[audio_start_time]`, `[Insert …]`) and image URLs requiring Plaud cookies —
the templating engine is server-side and invisible. Template catalogue,
`llm:"auto"` routing, and `summ_type_type:"system"` vs custom are UNKNOWN.

Speakers: transcript `speaker` strings ("Speaker 1"), an `original_speaker`
field on `transsumm` segments, and a global `GET /speaker/list` →
`data_speaker_list[{id, name, embedding[]}]` "used for voice identification"
— no client links the two; `/speaker/sync` exists uncalled. Tags:
`GET /filetag/` → `{id, name, file_count}`; tag→recording is client-side.

### 5.3 Consumer data model (list row, CORROBORATED 4/4 on the core fields)

`GET /file/simple/web?skip&limit&is_trash=0|1|2&sort_by=start_time|edit_time&is_desc` →
`{status, msg, request_id?, data_file_total, data_file_list[]}` with `id`
(32-hex), `filename`, `fullname`, `filetype`, `filesize`, `file_md5`,
`ori_ready`, `wait_pull`, `is_markmemo`, `version`, `version_ms`, `edit_time`,
`edit_from`, `is_trash`, `is_trans`, `is_summary`, `start_time`/`end_time`
(epoch ms), `duration` (ms), `timezone`, `zonemins`, `scene` (101 = web
upload; device value UNKNOWN), `filetag_id_list[]`, `serial_number`, `keywords[]`.
Envelope convention: HTTP 200 with `{status, msg}`; negative `status` is a
business error (`-302` region, `-1` "session not authorized", `-12`, `-111`).
`GET /user/me` → `data_user{id, nickname, email, country}` and
`membership_type` (`"starter"` observed) — the **only entitlement signal in
the whole corpus**; no quota, credit or limit field exists anywhere, and no
client is gated (server-side enforcement UNKNOWN).

### 5.4 Official third-party model (surface B′; OFFICIAL_BINARY + OFFICIAL_DOC)

`GET /open/third-party/files/?page&page_size` → `{id, name, created_at,
start_at, duration (ms), serial_number}`; `GET /open/third-party/files/{id}`
adds `presigned_url` (24 h), `source_list[]`, `note_list[]` with the same
`data_type` blocks, `data_content` inline or `data_link`-backed (the MCP
resolves links itself; `application/missing-blocks+cbor-seq` is a server
content type for absent blocks); `get_transcript` pages with `next_cursor` and
takes `block: transaction | transaction_polish | mark_memo`. **`list_files`
ignores `query`/`date_from`/`date_to` server-side** — the MCP filters
client-side over ≤ 5 pages, the CLI over the 500 most recent names. There is
no server-side search on this surface (see `search.md`).

## 6. Async jobs, webhooks and infrastructure indicators

- Task states = Celery (§5.1). `transsumm` status codes are a different
  (in-band, negative) scheme → two job systems, one per surface (INFERRED).
- Webhooks exist only on surface D: python-plaud-ai verifies
  `Plaud-Signature = hex(HMAC-SHA256(webhook_secret, raw_body))`; event names
  and payloads are absent from the corpus. **All four consumer clients poll**
  — no push API is exposed to users.
- Storage: S3 (`plaud-bucket`), presigned delivery hosts `resource.plaud.ai(.cn)`;
  content blocks stored as gzip JSON objects.
- Regions: partner `us` default, `jp`, `eu`, `sg`; consumer `us-west-2`,
  `eu-central-1`, `ap-southeast-1`, `us-east-1`, plus `.cn`; accounts can move
  regions (stale JWT `region` corrected by `-302`).
- Edge: Cloudflare WAF with UA/JA3 scoring (riffado changelog).
- The hosted MCP server (Express 5) exports OpenTelemetry traces (OTLP gRPC),
  Prometheus `/metrics`, PostHog analytics (US/EU hosts), Sentry, and a
  **telemetry warehouse** (`PLAUD_WAREHOUSE_ENDPOINT/API_KEY/SERVICE_NAME`).
  This is the only Plaud service whose instrumentation is visible; nothing
  ties Langfuse or Opik to production (see `source-map.md`).
- GoReplay: Plaud engineers deploy it via Jenkins (capture `:8080` → replay
  `localhost:7100`); what it mirrors is UNKNOWN.

## 7. Retention, regions, billing (OFFICIAL_DOC)

- Transcriptions on the partner surface are retained **7 days** by default
  (configurable on custom plans); "full data custody" = device-only integration
  (audio never leaves device + partner app). Device storage is 64 GB, encrypted.
- Embedded billing: PAYG **$15 per connected device per month** (peak devices
  in the month) + **$0.28 per transcription hour**; free tier **50 devices +
  300 hours** (hours unlock after the first device connection); charged at
  month end; 7-day grace on payment failure; no PAYG caps; enterprise for
  HIPAA/volume. "No Plaud App subscription needed" for SDK users. Consumer
  plan tiers are not in any source beyond `membership_type: "starter"`.
- Partner program via an affiliate platform (bixgrow) for device purchases.

## 8. Endpoint relationship graph

```
[B] client_id:secret ─▶ partner token ─▶ user token(user_id) ─┬─▶ initSDK ─▶ gen-key (RSA pair, PRIVATE KEY TO CLIENT) ─▶ sn-sign ─▶ BLE handshake (pv≥20)
                                                              ├─▶ sdk/bind ◀── bleBind(0)     sdk/unbind ◀── unpair     sdk/binding ─▶ recovery
                                                              ├─▶ files/upload presign ─▶ S3 PUT ─▶ complete ─▶ DownloadUrl ─┐
                                                              └─▶ sdk/version/latest (X-Device-Signature) ─▶ firmware bin (MD5) ─▶ OTA
  client_id + api_key ───────────────────────────────────────────────▶ ai/transcriptions/ {file_url} ─▶ task (Celery) ─▶ GET status ─▶ results/embeddings
[C] login/OTP/cookie ─▶ UT ─▶ workspaces/list ─▶ workspace token ─▶ file/simple/web ─▶ file/list | file/detail ─▶ data_link (S3) ─▶ segments/notes
                                                                    └─▶ file/temp-url ─▶ resource.plaud.ai (Ogg/Opus "PALUD.AI")
      upload presign ─▶ S3 ─▶ merge ─▶ confirm (scene 101) ─▶ row ─▶ PATCH tranConfig ─▶ ai/transsumm poll ─▶ PATCH persist ─▶ is_trans/is_summary
[B′] web.plaud.ai/platform/oauth (PKCE) ─▶ oauth/third-party/access-token ─▶ third-party/files/ ─▶ files/{id} ─▶ blocks (transaction | polish | marks | notes)
```

## 9. Ownership map

| capability | owner | class |
|---|---|---|
| issue app credentials; mint tokens; region assignment; session eviction | cloud | OFFICIAL_DOC / CLOUD_OBSERVED |
| choose stable `user_id`; keep `API_KEY` server-side; proxy AI calls | partner backend | OFFICIAL_SOURCE |
| device ownership registry, `bind_history`, 403 exclusivity | cloud | OFFICIAL_DOC |
| calling bind/unbind; brick recovery | partner app (never the SDK) | OFFICIAL_SOURCE |
| RSA pair + SN signature provisioning | cloud (private key handed to the client) | bytecode + swiftinterface |
| audio object storage, presigning, Opus/MP3 variants, re-tagging | cloud | CLOUD_OBSERVED |
| ASR/VAD/LID/diarization/embeddings; summaries; outline; templates | cloud | OFFICIAL_DOC / CLOUD_OBSERVED |
| analysis configuration (language, template, diarization, `llm`) and **committing AI results** on the consumer surface | web/app client | CLOUD_OBSERVED (single client) |
| speaker naming (string rewrite), tag→recording filtering, search | client | CLOUD_OBSERVED / OFFICIAL_BINARY |
| firmware metadata + binaries | cloud | OFFICIAL_SOURCE |
| entitlement enforcement | **UNKNOWN** | — |
| device-to-cloud background sync (Wi-Fi sync configs, `sendApiToken`, `Get/SetWebsocket`) | UNKNOWN destination | bytecode/swiftinterface only |

## 10. What the corpus does not show (consolidated)

1. The consumer library's write path from the Plaud app (no official source; the SDK's `uploadRecording` destination is unknown).
2. Which of `/file/list` (embedded results) and `/file/detail` (content blocks) is canonical, and how `is_trans/is_summary` relate to per-block `task_status`.
3. Whether the Plaud app also PATCHes AI results back (client-persistence) or only the web client does.
4. Entitlement enforcement, plan tiers, quotas, AI credits (`membership_type` only).
5. Webhook events/payloads (surface D) and `workflows/submit` schema.
6. Legacy TntAgent host (E); relation of A's `/api/...` host to B.
7. Cloud transcode vs re-tag (`PALUD.AI`), and what `is_opus` toggles.
8. Trash mechanism (`PATCH is_trash` vs `POST /file/trash`), `/speaker/sync`, `r=` cache-bust param, `edit_from`/`version_ms` versioning rules, `-111`/`-12` status meanings.
9. Whether `bind_history` really returns other partners' ids (privacy model) and what "signed but never bound" means server-side.
10. Which token (UT vs WT) each consumer endpoint actually requires, and the current lifetime policy.

## 11. Contradictions recorded (not resolved)

- Surface A vs B paths for the same operations; A's token endpoint 404s on B's host (template comment).
- `sdk/version/latest` auth: R7 inventory said Bearer; official template uses `X-Device-Signature` → **corrected here**.
- `/sdk/binding` is a distinct GET (not a README variant of bind) → **corrected here**.
- Ledger §4.4 "cloud bind is never called by the SDK" stands for the SDK; the *product* binds at the app layer.
- Transcription inputs M4A/MP3/WAV (docs) vs upload `mp3|opus` (API); OpenAPI `speaker_id` vs prose `speaker`; `model` list vs `hotwords`/`embeddings` split across two spec files; playground model name `plaud-transcribe-1`.
- iOS minimum 14.0 (README) vs 15.0/15.1 (every wrapper).
- Android SDK partner client defaults to `platform-jp`; templates default to `platform-test`; docs say `platform-us`.
