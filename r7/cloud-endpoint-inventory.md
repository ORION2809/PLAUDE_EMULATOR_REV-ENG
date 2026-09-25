# Plaud cloud / API endpoint inventory (static, R7 closure)

Scope: every HTTP endpoint referenced by the shipped SDK binary, the published
template apps, and the community clients in `reference/third-party/`. **Nothing
here was called.** Every entry is `Can legitimately test: NO` because each
requires a real Plaud account, partner or developer credential, or mutates /
bills production state; a URL string existing in a binary is not authorization.
Sources were read statically (raw `classes.jar` constant pools for the Retrofit
paths, since `javap -c` omits annotations; template Kotlin/Swift; community
TypeScript/Python). Evidence classes: **BYTECODE_PROVEN** (path/auth literal in
the SDK jar), **CLOUD_OBSERVED** (documented by shipped source or a community
client that talked to the cloud), **INFERRED** (structure follows but a detail
such as the host is not literal), **UNKNOWN**.

Three distinct surfaces exist and must not be conflated:

| surface | base | auth | who uses it |
|---|---|---|---|
| A. SDK internal `/api/...` | `ServerEnvironment` enum; default `US_PROD = https://platform-jp.plaud.ai` (others: `platform.plaud.cn`, `dev-api-us-test.plaud.work`, `platform-beta.plaud.ai`) | 3-hop Bearer chain: `sdk-token` ← `"Bearer "+Base64(appKey:appSecret)`; `api-token` ← `"Bearer "+sdk_token`; everything else ← `"Bearer "+api_token` | `sdk/network/ApiService` (Retrofit) inside `plaud-sdk.aar` |
| B. Partner `/developer/api/open/partner/...` | same `ServerEnvironment` host (SDK) / `platform-us.plaud.ai` (template source) | `"Bearer "+userAccessToken`; `metadata` uses `X-Device-Signature`; transcription uses `X-Client-Id` + `X-Client-Api-Key` | `sdk/network/PartnerApiService`, `NiceBuildSdk`, template `TranscriptionManager.kt` / `PlaudAPIService.swift` |
| C. Consumer web API | `https://api.plaud.ai` (regional `api-euc1`, `api-apse1`, `api-usw1`, `api.plaud.cn`; clients follow a `status:-302` redirect) | long-lived user-token JWT (UT; web stores `pld_tokenstr`), short-lived workspace token (WT, ~24 h) | riffado, plaud-api, plaud-toolkit, applaud (community) |
| D. Developer platform | `https://platform.plaud.ai/api` | OAuth `client_id:secret_key` → Bearer api-token | python-plaud-ai (community, follows docs.plaud.ai) |
| E. Legacy TntAgent | **host UNKNOWN** (empty prefix in the builder; ledger U21) | query signature `apikey=tinnotech&timestamp=<epoch>&sign=<hash(secret,…)>` over a hard-coded secret | `com.plaud.sdk.proto.r3`/`q3` (HttpURLConnection, form-urlencoded) |

Record format per endpoint follows the mission template, compressed to one
block per endpoint. `Observed` = what evidence shows; `Not observed` = what no
evidence here shows (typically every response body).

---

## A. SDK internal surface (`sdk/network/ApiService`, BYTECODE_PROVEN paths)

```
Endpoint: POST /api/oauth/sdk-token
Source: sdk/network/ApiService.class (Retrofit @POST) ; ProximaInterfaceRelay.java:41 builds the header
SDK caller: internal auth chain (com/plaud/sdk/internalimpl)
Authentication: Authorization: "Bearer " + Base64(appKey + ":" + appSecret)   (HTTP-Basic credentials under the Bearer scheme name)
Input: partner appKey/appSecret        Output: sdk_token (schema not observed)
Purpose: first hop of the SDK's own token chain
Observed: path + header construction (bytecode)   Not observed: response schema, TTL
Credential requirement: partner appKey/appSecret
Can legitimately test: NO — undocumented internal auth endpoint, needs real partner credentials
Status: CLOUD_OBSERVED (path BYTECODE_PROVEN)
```
```
Endpoint: POST /api/sdk/api-token
Authentication: Authorization: "Bearer " + sdk_token
Purpose: second hop; yields api_token used by all other /api/ calls
Observed: path, header   Not observed: body/response schema
Can legitimately test: NO (needs a valid sdk_token)      Status: CLOUD_OBSERVED
```
```
Endpoint: GET /api/sdk/config
Authentication: Authorization: "Bearer " + api_token
Purpose: SDK remote configuration
Observed: path, header   Not observed: config schema
Can legitimately test: NO                                 Status: CLOUD_OBSERVED
```
```
Endpoint: POST /api/files/upload-s3/generate-presigned-urls  ;  POST /api/files/upload-s3/complete-upload
Authentication: Bearer api_token / user token
Purpose: cloud upload of a recording (S3 presign → PUT parts → complete)
Observed: paths; request model class names GeneratePresignedUrlsRequest (fields not decompiled)
Not observed: JSON field names, response schema
Can legitimately test: NO — creates/mutates cloud storage sessions   Status: CLOUD_OBSERVED
```
```
Endpoint: GET /api/files/list
Authentication: Bearer   Purpose: list the account's cloud recordings
Can legitimately test: NO — reads private account storage             Status: CLOUD_OBSERVED
```
```
Endpoint: POST /api/devices/bind  ;  POST /api/devices/unbind
Authentication: Bearer   Purpose: cloud device ownership (DeviceBindingRequest model)
Can legitimately test: NO — mutates ownership                          Status: CLOUD_OBSERVED
```
```
Endpoint: POST /api/workflows/submit ; GET /api/workflows/{workflow_id}/status ; GET /api/workflows/{workflow_id}/result
Authentication: Bearer api_token   Purpose: SDK-driven AI processing (SubmitRequest model)
Can legitimately test: NO — billable AI work / private content         Status: CLOUD_OBSERVED
```
```
Endpoint: GET /api/sdk/latest-version
Authentication: Bearer   Purpose: SDK/firmware version check
Can legitimately test: NO by policy (undocumented, token required; lowest-risk read, still not exercised)   Status: CLOUD_OBSERVED
```

## B. Partner surface (`sdk/network/PartnerApiService` + template source)

```
Endpoint: POST /developer/api/open/partner/sdk/sn-sign
SDK caller: NiceBuildSdk.signAndStoreDeviceSn → PartnerApiManager   (template DeviceManager.kt:829-835)
Authentication: Authorization: "Bearer " + userAccessToken (empty token short-circuits before HTTP)
Input: device SN (+ type)  Output: snSignature (Base64) — chunked into the 0xFE10/0xFE20 pre-handshake frames (ledger §4.3)
Purpose: cloud-issued signature the device verifies on the portVersion ≥ 20 path
Credential requirement: valid partner userAccessToken     Can legitimately test: NO      Status: CLOUD_OBSERVED (path BYTECODE_PROVEN)
```
```
Endpoint: POST /developer/api/open/partner/sdk/sn-verify
Authentication: Bearer userAccessToken   Purpose: verify a device signature   Can legitimately test: NO   Status: CLOUD_OBSERVED
```
```
Endpoint: POST /developer/api/open/partner/sdk/gen-key
SDK caller: initSDK → PartnerApiManager (async; NiceBuildSdk.isPartnerDataReady gates connect)
Authentication: Bearer userAccessToken
Output: RSA key pair — the PRIVATE key is returned to the client (PKCS#8 PEM; ledger §4.5)
Purpose: provisions the RSA material used to decrypt the 0xFE12 secret package (J/K/L)
Can legitimately test: NO — provisions key material server-side       Status: CLOUD_OBSERVED (path BYTECODE_PROVEN)
```
```
Endpoint: POST /developer/api/open/partner/sdk/metadata
Authentication: X-Device-Signature header (not Bearer); computation site not located (UNKNOWN)
Purpose: device metadata post   Can legitimately test: NO — requires a device-attested signature   Status: CLOUD_OBSERVED / signature UNKNOWN
```
```
Endpoint: POST https://platform-us.plaud.ai/developer/api/open/partner/ai/transcriptions/  ;  GET .../ai/transcriptions/{transcription_id}
Source: template TranscriptionManager.kt:188-189, PlaudAPIService.swift
Authentication: X-Client-Id (from the userAccessToken JWT client_id claim) + X-Client-Api-Key
Purpose: partner AI transcription   Can legitimately test: NO — documented developer-portal API, still needs issued keys and bills   Status: CLOUD_OBSERVED
```
```
Endpoint: POST https://platform-us.plaud.ai/developer/api/open/partner/files/upload/generate-presigned-urls ; .../complete-upload
Endpoint: POST .../partner/sdk/bind | /unbind   (README also shows /binding)
Endpoint: GET  .../partner/sdk/version/latest
Source: template PlaudAPIService.swift:64,218 ; README
Authentication: Bearer user_access_token
Can legitimately test: NO                                                 Status: CLOUD_OBSERVED
```

**Contradiction recorded (not resolved — cannot be without a live account):**
for the same operations the SDK binary uses `/api/files/upload-s3/…`,
`/api/devices/bind`, `/api/sdk/latest-version`, `/api/workflows/submit`
(Bearer api_token) while the published template source uses
`/developer/api/open/partner/files/upload/…`, `/…/sdk/bind`,
`/…/sdk/version/latest`, `/…/ai/transcriptions` (Bearer user token or
X-Client keys). Whether `/api/...` is an internal gateway distinct from the
public partner API, or the two hosts alias the same backend, is **UNKNOWN**.

## C. Consumer web API (community clients; CLOUD_OBSERVED)

Sources: `reference/third-party/riffado` (TypeScript, incl. regression tests),
`plaud-api`, `plaud-toolkit`, `applaud-rsteckler`, `applaud-landoncrabtree`.
Base `https://api.plaud.ai` with regional variants. All require a real account
token; none exercised.

| endpoint | auth | purpose |
|---|---|---|
| POST `/auth/otp-send-code`, POST `/auth/otp-login` | none / otp token | OTP login (would dispatch a live OTP) |
| POST `/auth/access-token` (form) | none | password login (logs out other sessions) |
| POST `/auth/refresh-user-token` | Bearer UT | refresh |
| GET `/user/me` | Bearer UT | account |
| GET `/device/list` | Bearer WT/UT | device enumeration `{status, data_devices[]}` |
| GET `/file/simple/web?skip&limit&is_trash&sort_by&is_desc` | Bearer WT/UT | recording listing |
| POST `/file/list` (body: file_ids[]) ; GET `/file/detail/{id}` | Bearer | detail; exposes `pre_download_content_list` presigns |
| GET `/file/temp-url/{id}?is_opus=0|1` ; GET `/file/download/{id}` | Bearer | audio download via presigned S3 URL (`temp_url`, `temp_url_opus`) |
| PATCH `/file/{id}` | Bearer | metadata / start transcription (`extra_data.tranConfig`) / persist results |
| POST `/file/get_upload_presigned_url` → S3 PUT parts → POST `/file/merge_multipart` → POST `/file/confirm_upload` | Bearer UT | upload (`scene:101`, `serial_number=uuid`) |
| POST `/ai/transsumm/{id}` | Bearer UT | poll transcription/summary |
| GET `/filetag/`, GET `/speaker/list`, `/speaker/sync` | Bearer UT | tags, speakers |
| GET `/team-app/workspaces/list?need_personal_workspace=true` → POST `/user-app/auth/workspace/token/{wid}` | Bearer UT | mint a workspace token (WT, claims `ut_ref/wid/wtype`) |

## D. Developer platform (python-plaud-ai; CLOUD_OBSERVED, mirrors docs.plaud.ai)

`POST platform.plaud.ai/api/oauth/api-token` (Bearer base64(client_id:secret_key)); `devices`, `devices/{id}`, `devices/bind`, `devices/unbind`; `files/upload-s3/generate-presigned-urls`, `complete-upload`; `workflows/submit`, `workflows/{id}/status`, `workflows/{id}/result`. This is the *documented* form of surface A. Requires issued developer credentials; not exercised.

## E. Legacy TntAgent (BYTECODE_PROVEN structure; host UNKNOWN; never to be exercised)

`POST <host>/recorder/device/checkSn`, `checkCustomer` (issues handshake tokens),
`saveOperation` (telemetry). Form-urlencoded over `HttpURLConnection`,
authenticated by `apikey=tinnotech&timestamp&sign` where `sign` is a hash over
a secret string literal in the jar. Status: INFERRED (structure) / UNKNOWN
(host). Must not be called: production, undocumented, hard-coded secret.

---

## Cloud ↔ SDK ↔ BLE correlation (what can and cannot be said)

```
gen-key (B)  ──► RSA private key ─┐
sn-sign (B)  ──► snSignature ─────┼─► pv≥20 pre-handshake: 0xFE10/20 chunks → 0xFE11 → 0xFE12 → J/K/L (ledger §4.3)   [BYTECODE_PROVEN chain; values CREDENTIAL_REQUIRED]
                                  └─► pv<20: unused; k3 token is local (R7-S12)                                          [RUNTIME_PROVEN]
BLE syncFile DATA payloads ──► local file (o4 pass-through, byte-identical)  [BYTECODE_PROVEN]
local file ──► upload-s3 (A/B/C) ──► cloud recording ──► /file/temp-url → .ogg/.mp3   [CLOUD_OBSERVED paths only]
```

**Are BLE bytes, SDK-local bytes, cloud bytes and cloud exports the same
artifact?** BLE bytes = SDK-local bytes: **yes** (pass-through, bytecode).
SDK-local = cloud recording = cloud export: **not established, and evidence
argues against equivalence** — the SDK's native writer tags OpusTags vendor
`TinnoTech123456789012` while real cloud downloads carry `PALUD.AI` (riffado
regression #160) and are served as `.mp3`/`audio/mpeg` despite Ogg/Opus bytes.
`PALUD.AI` appears nowhere in the SDK, so the cloud re-tags or transcodes.
Cloud artifacts must therefore **not** be used as fixtures for the local
writer or the BLE path (ledger U19/U20).
