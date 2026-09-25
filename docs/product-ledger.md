# Plaud product ledger

The product-level counterpart of `docs/protocol-ledger.md`. One row per
behaviour, with the evidence that carries it. **Confidence** uses the R7
vocabulary: PROVEN_OFFICIAL_SOURCE (Plaud-published source), PROVEN_BYTECODE
(javap of the AAR), OFFICIAL_DOC (docs.plaud.ai, a claim by Plaud), OFFICIAL_BINARY
(shipped frameworks / npm bundles), RUNTIME (observed executing the real SDK),
CORROBORATED (≥ 2 independent sources), CLOUD_OBSERVED (community client that
talked to the cloud), LINEAGE (a Plaud fork's delta), INFERRED, UNKNOWN, and
HARNESS_POLICY where our emulator chooses. **Source** is the file:line or
artefact; long citations live in the subsystem docs under `docs/architecture/`
and in `docs/product-evidence/agent-findings-2026-09-23.json` (machine-readable,
graphed by `docs/evidence-graph.json`). Nothing in this table was obtained by
calling a Plaud endpoint or from a Plaud device.

## Device

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Device | Recorder platform is TinnoTech Pen BLE (ODM); native libs carry only Tinno symbols; Ogg writer vendor `TinnoTech123456789012` | `com.tinnotech.penblesdk` ×1301; `libtnt_ble_utils.so`; `libjni_ogg.so`@0x1648 | PROVEN_BYTECODE | AAR |
| Device | BLE company ID mapped to 70 = MediaTek, 89 = Nordic; no Espressif string anywhere | `s0$h` static init, ALL.txt:35466-35476 | PROVEN_BYTECODE | AAR |
| Device | Models: 880 NotePin, 881 Note Pro (`notepro`), 882 NotePin S (`notepins`), 888 Note; Embedded supports 881/882 only; SDK defaults unknown prefixes to `notepro` | README:269-276; `resolveDeviceType` ALL.txt:1624-1666; `u4` ledger §6.2 | PROVEN_OFFICIAL_SOURCE + PROVEN_BYTECODE | plaud-sdk-public |
| Device | Note Pro: 30–50 h battery, 4 MEMS + 1 VPU, dual-band Wi-Fi + BT, 64 GB encrypted, calls + in-person, 5 h/file. NotePin S: 20 h, 2 MEMS, 64 GB, 5 h/file | `plaud-embedded_devices.md:29-41` | OFFICIAL_DOC | docs.plaud.ai |
| Device | VPU is a distinct tunable pickup: `VPU_GAIN` (19, Low/Medium/High), `VPU_CLK` (30) separate from `MIC_GAIN` (20); `SwitchHandlerID` includes call-scene switching | ledger §5.12; swiftinterface:296-339 | PROVEN_BYTECODE | AAR + iOS |
| Device | What the VPU physically is (vibration conduction) | no string expands "VPU" | UNKNOWN | — |
| Device | 21 CommonSettings with non-ordinal wire values (ProGuard kept names); SDK reads ENABLE_VAD and REC_MODE after connect; consumer `q.f` maps VAD→`setVadOpen(v==1)`, REC_MODE→`setNcClose(v!=2)` | ledger §5.12; R7-S12 run 1/4 | PROVEN_BYTECODE + RUNTIME | AAR |
| Device | Value ranges/defaults of every setting | none documented | UNKNOWN (U22) | — |
| Device | RecScene {Unknown, Normal, Interview, Classroom, Music, Meeting, Memo}; RecMode {Normal, NC}; VadSensitivity {Quality, lowBitrate, Normal, Aggressive} | PlaudBleSDK swiftinterface | OFFICIAL_BINARY (names) | iOS |
| Device | Recording is device-authoritative; app mirrors `bleRecordStart/Stop/Pause/Resume` (`sessionId` = unix s, `reason`, `fileExist`, `fileSize`); templates pass `scene=0` | RecordingManager.kt:64-164 / .swift:42-124 | PROVEN_OFFICIAL_SOURCE | templates |
| Device | Device status bitfield: BLE transfer, Wi-Fi fast transfer, Wi-Fi test, wired transfer, USB-disk mode, Wi-Fi cloud upload, "pan" cloud upload, BLE-OTA download, Wi-Fi OTA | PlaudBleSDK-Swift.h:1432-1440 | OFFICIAL_BINARY | iOS header |
| Device | Direct device-to-cloud channel exists (Get/SetWebsocket 16/17 `url/serToken/devToken`, Wi-Fi sync-when-idle configs, `sendApiToken`); destination/auth | swiftinterface:341-402; ledger §7 | OFFICIAL_BINARY; destination UNKNOWN | iOS/AAR |
| Device | Telemetry counters `TntStatUserItem` (rec/off-rec counts, keyPressCnt, uDiskCnt, earphoneCnt, mp3PlayCnt…) | ALL.txt:27309 | PROVEN_BYTECODE | AAR |
| Device | Press-to-highlight device side: `getMarking → markList:[UInt32]`, `getRecordMarkingTags → BleRecordMarkingTag{timestamp,type,status}`; consumer `mark_memo`/`high_light` blocks | swiftinterface:57,125-126; MCP skill plaud-read | OFFICIAL_BINARY + CORROBORATED | iOS + npm + clients |
| Device | Ownership lock: firmware bound to one `client_user_id`; other tokens refused (`TOKEN_NOT_MARCH`, GATT-then-drop); `depair` (opcode 5) changes advertised MAC | templates; ALL.txt:61149-61200 | PROVEN_OFFICIAL_SOURCE (claims on MAC) | templates/AAR |
| Device | `restoreFactory`, `clearAllFiles` (104), `resetPassword`, `resetFindmy`, `setDeviceActive` exist; no caller | swiftinterface; ALL.txt:3371-3390 | OFFICIAL_BINARY; effect UNKNOWN | iOS/AAR |
| Device | Other hardware lines: "G101 glasses" (charging-only OTA, `thirdVersion`, `GlassProtocol`), "Heili three-way-switch pen" (status 255); codename "NiceBuild" shared by the SDK and the ESP32 prototype | OTA status strings; PlaudBleSDK-Swift.h:1395; custom_wake_word.cc:108 | PROVEN_BYTECODE + LINEAGE | AAR/iOS/xiaozhi fork |
| Device | ESP32 forks are prototypes on Espressif Korvo dev kits, Bluetooth compiled out; not the recorder | xiaozhi sdkconfig:633,1068; client-sdk-esp32 sdkconfig:602 | LINEAGE | plaud-org forks |

## Bluetooth / lifecycle (packet layer frozen; see protocol-ledger)

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| BLE | GATT 1910/2BB0/2BB1/2902/180F; MTU 255 requested, 517 on AVD; discovery and stage sequence observed live | ledger §1, §14 | RUNTIME | R4–R7 |
| BLE | Handshake token = JWT `sub` minus `client_user_` minus `-`, padded/truncated to 16 (pv<9) or 32; recovery pins a historical id; k3 byte-exact vs real SDK | `NiceBuildSdk.resolveHandshakeToken`; ALL.txt:2653-2761; R7-S12 runs 1–4 | RUNTIME + PROVEN_BYTECODE | AAR |
| BLE | pv < 20: cleartext token-only bind; pv ≥ 20: FE10/FE20 snSignature chunks → FE11 → FE12 RSA secret → J/K/L → ChaCha20-Poly1305 sealing, first seq 2, no ACK | ledger §4.3, §5.5 | PROVEN_BYTECODE | AAR |
| BLE | `bleConnectState 1` is not success; `bleBind(status 0)` is; a locked device drops after GATT (`status_1`), Android reports 2 after ~10 s | android-sdk.md:204-207,357; templates | OFFICIAL_DOC + PROVEN_OFFICIAL_SOURCE | docs/templates |
| BLE | `bleBind(protVersion, timezone)` are BOTH the l3 timezone byte on the facade path (status literal 0) | ledger §14 run 4 | RUNTIME + PROVEN_BYTECODE | R7-S12 |
| BLE | Post-bind order: syncTime → battStatus → getState → CommonSettings(ENABLE_VAD, REC_MODE) | R7-S12 capture | RUNTIME | R7-S12 |
| BLE | Capability exchange opcode 138 (`01 8A 00 FF`); bit 3 → Wi-Fi AES-GCM; BLE always ChaCha | ledger §5.13, §4.3 | PROVEN_BYTECODE | AAR |
| BLE | Transfer: HEAD → DATA (type 2) → **EMPTY_PACKAGE sentinel (what the client completes on; must precede TAIL)** → TAIL (u16 never verified); on a gap the client sends stopSync then restarts from its cursor (~35 ms), accepting only cursor-matching frames; the 5 s stall path also ends in stopSync | ledger §5.8, §15; r7/r7-s13-recording-pull.md | PROVEN_BYTECODE + RUNTIME | AAR + R7-S13 |
| BLE | Real SDK pulled a recording from the emulator byte-exact on both public paths (`syncFile` collector, `exportAudio(OPUS)`); OPUS export of plain-Ogg input is a passthrough; `exportAudio` skips the download if the output file exists; zero-latency device responses race the phone's op-queue (-98/-99) | r7/r7-s13-evidence/SHA256SUMS; logcat runs 4b/6b/6d | RUNTIME (EMULATOR_INTEGRATION) | R7-S13 |
| BLE | Recovery: `GET sdk/binding` → dedupe `bind_history` → ≤5 × `recoveryConnectBleDevice` (force-clear, inert < pv 20) → depair → rescan → reconnect | templates; docs; R7-S12 | PROVEN_OFFICIAL_SOURCE + RUNTIME (host half) | templates/AAR |
| BLE | OTA push: FotaInfo (50) `uid, from/to version, thirdVersion, fileSize, CRC-16[, isSilent]`; 80-byte packets, pacing 40/160; 52 per-pack; 51 verdict; status 0–12 (+13 silent, iOS); 20 refusal reasons; "updates wipe recordings" | ALL.txt:125590-128653; PlaudBleSDK-Swift.h:759-793; android-sdk.md | PROVEN_BYTECODE + OFFICIAL_DOC | AAR/iOS/docs |
| BLE | Wi-Fi fast transfer: setDeviceWiFi (10) → SoftAP (SSID PLAUD+SN4 / pass SN8) → phone WebSocket **server** :8081 → handshake with the BLE token → 20 PDU types → batch delete → close; iOS `WiFiAgent` is `JXWebSocketServerDelegate` | ledger §7; WIFI swiftinterface:137-146 | PROVEN_BYTECODE + OFFICIAL_BINARY | AAR/iOS |
| BLE | Wi-Fi payload crypto: AES-GCM iff capability bit 3 else ChaCha20-Poly1305, keys copied from the BLE session | ALL.txt:121356-121485 | PROVEN_BYTECODE | AAR |
| BLE | iOS `WebsocketType` raw 0/1/2 vs Android wire 1/2/3 | BLE:714-717 vs ledger §7 | contradiction, UNKNOWN | iOS/AAR |

## Mobile (partner app)

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Mobile | Launch routing: `(paired OR skipped) AND userId` → main; else onboarding; `configure(userId)` first | SceneDelegate.swift:14-21; WelcomeActivity.kt:30-36 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Identity = partner JWT; `userId = sub` (random UUID fallback); `X-Client-Id = client_id`; no signature check; token replace live if same `sub` | JwtUtils.*; DeviceManager.swift:127-135 | PROVEN_OFFICIAL_SOURCE | templates |
| Storage | Persistence = key-value (UserDefaults / SharedPreferences `plaud_template_prefs`) + `recordings.json` `[RecordingFile{id, sessionId, deviceSN, name, duration, createdAt, syncedAt, localPath, summaryText, transcriptJSON}]`; no DB, no app-level encryption | RecordingStore.swift:10-249; RecordingStore.kt:12-265 | PROVEN_OFFICIAL_SOURCE | templates |
| Storage | Audio: MP3 mono in Documents (iOS) / **evictable** `cacheDir/export` (Android); `.opus`/`.ogg` helpers dead | SyncManager.*; RecordingStore.kt:224-244 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Device is the inventory: every file list rebuilds `recordings.json`; unsynced rows dropped when absent from device | SyncManager.kt:341-356; .swift:270-380 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Delete-after-sync unconditional (BLE per file, Wi-Fi batched with 15 s/4 s confirm + 20 s close wait); user delete is local only | SyncManager.kt:746-764; .swift:377-379 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Auto-download on connect: iOS yes; Android list-only (README claim REFUTED for Android); iOS auto-sync toggle dead | SyncManager.swift:309-324; SyncManager.kt:650-654 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Auto-reconnect foreground-only: 2 s after foreground; 30 s × 10 on drop; no services/notifications/WorkManager | PlaudTemplateApp.kt:40-71; DeviceManager.swift:233-292 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Multi-device: paired SN list + active SN; one BLE link; switch = disconnect+scan | DeviceManager.*.switchDevice | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | PCM waveform: iOS via `syncFile(sid,start,0)` + `blePcmData`; Android never fed (no callback in AAR) | RecordingManager.kt:166-199; ALL.txt grep | PROVEN_OFFICIAL_SOURCE + PROVEN_BYTECODE | templates/AAR |
| Mobile | Cloud bind after every `bleBind(0)`; 403 → alert but stays connected; unbind fire-and-forget before depair | DeviceManager.kt:1197-1243; .swift:474-497 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Transcription: presign → PUT parts (5 MiB) → complete → submit (`plaud-fast-whisper`, auto, diarization off) → poll 5 s × 240; no retry; `transcription_id` not persisted; **summary not implemented** | TranscriptionManager.*; FileDetail* | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | OTA: iOS SDK one-call; Android app-side `version/latest` + `X-Device-Signature` because AAR path needs `/api/oauth/sdk-token` ("404s on platform-us") | DeviceManager.kt:520-603 | PROVEN_OFFICIAL_SOURCE | templates |
| Mobile | Both templates default to `platform-test.plaud.ai` | RecordingStore.*:130-135 | PROVEN_OFFICIAL_SOURCE; reason UNKNOWN | templates |
| Mobile | No subscription/entitlement/quota logic anywhere | grep negative | PROVEN (absence) | templates/wrappers |
| Mobile | Wrappers bridge 9 methods / 12 events; drop recovery, deleteFile, token refresh, Wi-Fi, OTA, settings, recording control; drop `bleBind.timezone` | plaud-sdk.ts; PlaudSdk.types.ts; plaud_sdk.dart | PROVEN_OFFICIAL_SOURCE | wrappers |
| Mobile | Android wrappers must repoint the partner API from the SDK's hard-coded `platform-jp` and sign the SN themselves; iOS facade does both internally | PlaudSdkPlugin.java:166-194; PlaudSdkModule.kt:135-146 | CORROBORATED (4 impls) | wrappers/templates |
| Mobile | Capacitor shell loads a remote Vercel origin; native plugin does SDK/BLE/file reads/S3 PUT; Next.js routes mint tokens and proxy AI (unauthenticated in the demo) | capacitor.config.ts; app/api/**/route.ts | PROVEN_OFFICIAL_SOURCE | embedded-capacitor |
| Mobile | Shipped iOS frameworks are 1.0.13 while docs describe 1.0.14; docs list callbacks/params that do not exist (`bleState`, `logIndex`) | Info.plist; BASIC:1152-1203 | OFFICIAL_BINARY vs OFFICIAL_DOC | iOS |

## Cloud (identity, registry, storage, jobs)

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Identity | Partner chain: Basic(client_id:secret) → partner token (3600 s + refresh) → user token per stable `user_id` (6–120 chars, `expires_in` e.g. 86400, no refresh); JWT claims `sub=client_user_*`, `user_id`, `exp`, `client_id` | openapi_auth.json; user-token-script.ts; plaud-auth.ts | OFFICIAL_DOC + PROVEN_OFFICIAL_SOURCE | docs/skills/demo |
| Identity | Third-party OAuth (PKCE, DCR with HMAC client ids) at `web.plaud.ai/platform/oauth` → `platform.plaud.ai/developer/api/oauth/third-party/access-token(/refresh)`; hosted MCP `mcp.plaud.ai/mcp` | @plaud-ai/mcp bundle strings | OFFICIAL_BINARY | npm |
| Identity | Consumer: password login (`/auth/access-token`, ~30 d, mints `sid`, evicts other sessions), OTP (`/auth/otp-*`, ~300 d), cookies `pld_ut` ~1 d + `pld_urt` ~30 d (`/auth/refresh-user-token`); SSO creates a separate identity | plaud-toolkit auth.ts:5-13; riffado auth.ts; applaud session.ts | CLOUD_OBSERVED (lifetimes disagree = migration) | clients |
| Identity | Workspaces: `/team-app/workspaces/list` → `POST /user-app/auth/workspace/token/{wid}` → WT ~24 h (`ut_ref, wid, wtype, mid, role, jti`) | riffado workspace.ts; applaud session.ts | CORROBORATED | clients |
| Identity | Region: `-302` in-band redirect; consumer regions us-west-2/eu-central-1/ap-southeast-1/us-east-1 + `.cn`; partner regions us (default), jp, eu, sg | 4 clients; data-retention.md | CORROBORATED + OFFICIAL_DOC | clients/docs |
| Cloud | Device registry: `POST sdk/bind` (idempotent; 403 other owner; bare 404 unknown SN), `POST sdk/unbind`, `GET sdk/binding → {is_bind: true|false|null, bind_history[] newest-first, per-event duplicates, not client-scoped (claim)}` | openapi_binding.json; device-binding-api-overview.md | OFFICIAL_DOC | docs |
| Cloud | Binding is two-layered: cloud record + local firmware lock ("necessary for offline encryption") | plaud-embedded_overview.md:123-125 | OFFICIAL_DOC | docs |
| Cloud | `gen-key` returns the RSA **private** key to the client; `sn-sign` returns the signature; `metadata` with `X-Device-Signature`; sn-sign is memory-only on Android ("re-sign every launch"), RSA pair in Keystore | ledger §4.5; advanced-android-sdk.md:127-152,373-438 | PROVEN_BYTECODE + OFFICIAL_DOC | AAR/docs |
| Cloud | Partner upload: presign `{filesize, filetype mp3|opus}` → `ChunkSize 5242880`, presigned parts on `plaud-bucket.s3.amazonaws.com` → complete `{file_id, upload_id, part_list, filetype, file_md5?}` → `DownloadUrl` ~24 h | openapi_file.json | OFFICIAL_DOC | docs |
| Cloud | Consumer upload: presign → S3 → merge → confirm (`scene 101`, `serial_number uuid4`) → row with `is_trans=is_summary=0` | plaud-api recordings.py:93-166 | CLOUD_OBSERVED (single) | plaud-api |
| Cloud | Consumer list row fields (`id`, `filename`, `filesize`, `file_md5`, `version(_ms)`, `edit_time`, `edit_from`, `is_trash`, `is_trans`, `is_summary`, `start/end_time` ms, `duration` ms, `scene`, `filetag_id_list`, `serial_number`, `keywords`, `is_markmemo`, `wait_pull`, `ori_ready`) | 4 clients | CORROBORATED | clients |
| Cloud | Two detail representations (`POST /file/list` embedded vs `GET /file/detail` `content_list` + `data_link` gzip JSON + inline `pre_download_content_list`); canonical UNKNOWN | plaud-api vs riffado/toolkit | CORROBORATED (each) | clients |
| Cloud | Content block vocabulary `transaction`, `transaction_polish`, `outline`, `auto_sum_note`, `sum_multi_note`, `mark_memo`/`high_light`; `task_status 1` = ready | MCP skill; riffado content.ts; applaud detail.ts | CORROBORATED (official + 2) | npm/clients |
| Cloud | Audio downloads: `temp-url` presigned on `resource.plaud.ai`; bytes are Ogg/Opus tagged `PALUD.AI` served as `.mp3` even for `is_opus=0` | riffado #160; applaud layout.ts; toolkit sniff | CORROBORATED | clients |
| Cloud | Cloud artefact ≠ SDK-local file (vendor divergence) → cloud re-encodes/re-tags | ledger U19 | INFERRED (from CORROBORATED facts) | — |
| Cloud | Edge: Cloudflare; non-browser UA → 403/challenge; riffado proxies via Webshare and self-rate-limits | 4 clients; riffado proxy.ts | CORROBORATED | clients |
| Cloud | Third-party surface: `GET /open/third-party/files/?page&page_size`, `GET …/files/{id}`, `GET …/users/current`, `POST …/revoke`; blocks link-backed; `next_cursor` transcript paging; `application/missing-blocks+cbor-seq` | @plaud-ai/mcp + cli bundles | OFFICIAL_BINARY | npm |
| Cloud | Hosted MCP server: Express 5, OpenTelemetry OTLP, Prometheus `/metrics`, PostHog, Sentry, telemetry **warehouse** env, token-verify rate limits | mcp bundle deps/env | OFFICIAL_BINARY | npm |
| Cloud | SDK-internal `/api/...` surface (3-hop Bearer, default `platform-jp`) coexists with the partner surface; docs acknowledge it; workflow queries deprecated 2026-09-14 | R7 inventory §A; advanced-android-sdk.md:274-276,440-442; changelog | PROVEN_BYTECODE + OFFICIAL_DOC | AAR/docs |
| Cloud | Legacy TntAgent `/recorder/device/*` with hard-coded query-signature secret; host UNKNOWN | ledger U21 | PROVEN_BYTECODE (structure) | AAR |
| Cloud | Webhooks (developer platform): `Plaud-Signature = HMAC-SHA256(secret, body)`; events UNKNOWN; consumer clients all poll | python-plaud-ai webhook.py; 4 clients | CLOUD_OBSERVED (mechanism) | clients |
| Cloud | GoReplay deployed via Jenkins (`--input-raw :8080 → localhost:7100`); targets unnamed | goreplay Jenkinsfile (shuo@plaud.ai, 2025-11-25) | LINEAGE (deployment) | plaud-org |
| Cloud | Langfuse/Opik: unmodified snapshots; no Plaud repo imports either | delta manifests; grep | LINEAGE (fork-button); production use UNKNOWN | plaud-org |

## AI

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| AI | Partner transcription: `POST /open/partner/ai/transcriptions/` `{file_url, params{transcribe{language auto, model, detection_level segment|chapter}, vad{decode_silence}, diarization{enabled, return_embedding}, hotwords}}`; `X-Client-Id` + `X-Client-Api-Key` | openapi_transcription*.json | OFFICIAL_DOC | docs |
| AI | Models: `plaud-fast-whisper` (default), `plaud-omni-3`, `azure-fast-transcribe`; playground `plaud-transcribe-1` | openapi_transcription-model.json:186-190; api-playground.md | OFFICIAL_DOC | docs |
| AI | Task states = Celery set (`PENDING/RECEIVED/STARTED/PROGRESS/SUCCESS/FAILURE/REVOKED`), ids `task_exec_*` | openapi status enum | OFFICIAL_DOC; Celery INFERRED | docs |
| AI | Result: `text, language, duration s, results[{start, end, text, speaker_id, language, language_probability}]`, `embeddings{Speaker N: 256-d}`; prose shows `segments[].speaker` | openapi example; how-it-works.md | OFFICIAL_DOC (internally inconsistent) | docs |
| AI | Limits: 60 req/min, 24 h max, 6 h with diarization, chunk > 5 h, M4A/MP3/WAV, results retained 7 d; 112 languages; pipeline "noise reduction, speaker detection, language recognition" | transcription docs; overview.md | OFFICIAL_DOC | docs |
| AI | Consumer processing: `PATCH /file/{id} {extra_data.tranConfig{language, type "REASONING-NOTE", type_type "system", diarization 1, llm "auto"}}` → `POST /ai/transsumm/{id}` (0 processing / 1 complete / -111 success / -12 old) → client `PATCH` results back | plaud-api transcriptions.py:28-144; applaud poller.ts | CLOUD_OBSERVED (single / corroborated parts) | clients |
| AI | Summary payload `{ai_content markdown, category "Chat Note", summary_id "…-v2@hex-N", summ_type CASUAL-CONVERSATION|REASONING-NOTE, header{headline, keywords[]}, state 10}`; unresolved template variables; polymorphic `ai_content` | riffado plaud-content.test.ts:24-31; applaud summarySanitize.ts | CORROBORATED | clients |
| AI | Official summary result schema sample: `summary_type 'MEETING'`, `select_prompt_type`, `speaker_mapping`, `use_persona`, `recommend_questions`, `industry_category`, version `0.5.0.24`, endpoint `azure-gpt-4o-sc`, model `gpt-4.1` | `PlaudDeviceBasicSDK.framework/plaud_ai_data.txt` | OFFICIAL_BINARY (bundled sample) | iOS framework |
| AI | Undocumented Workflow API in the iOS binary: `audioTranscribe | aiSummarize | aiEtl (clinicalReport, dealAnalysis) | audioMerge | custom`; `templateId 'MEETING'`, `model 'openai'`; deprecated 2026-09-14 | swiftinterface:443-663 | OFFICIAL_BINARY | iOS |
| AI | Speakers: default "Speaker N"; `original_speaker`; global voiceprints `GET /speaker/list {id, name, embedding[]}` "for voice identification"; `/speaker/sync` uncalled; linkage UNKNOWN | plaud-api speakers.py; conftest.py:54 | CLOUD_OBSERVED (single) | plaud-api |
| AI | Template catalogue ("10,000+"), `llm:"auto"` routing, custom templates | only ids/flags observed | UNVERIFIABLE | — |
| AI | Live Agent stack (experimental line): SileroVAD → TEN turn detection (own GPU service) → Groq `whisper-large-v3-turbo` → OpenRouter `gemini-2.5-flash` (fallback `gpt-5.2-chat`) → MiniMax dual-stream TTS; LLMCompiler orchestrator; persona prompts; Fish/MiniMax voice cloning; 2025-11 LLM benchmark | live-agent custom_config.yaml:643-655; core/parallel; core/roles | PROVEN_OFFICIAL_SOURCE (Plaud-authored) | live-agent |
| AI | Whether any Live Agent component is in production; relation to recorder | no plaud.ai host; private LAN/EC2/Dokploy | UNKNOWN | — |

## Memory / Search

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Memory | mem0 fork packaged with release tags (`v1.0.0`) by harold.guo@plaud.ai; unused by any corpus repo | pyproject.toml:6-13; README:85-116 | LINEAGE | live-agent-memory |
| Memory | memU (hosted `api.memu.so`) selected in live-agent; `profile`/`event` persona categories; prefetch on partial ASR (1.5 s); cross-agent memory sharing tables; `use_mock` hard-coded true in the API | memu.py; connection.py:728-745; init_db.sql:214-232 | PROVEN_OFFICIAL_SOURCE | live-agent |
| Memory | memU wrappers verbatim; `memu-py` = OpenAI embeddings + numpy, no vector DB | plaud-memU-server uv.lock:382-397 | LINEAGE + INFERRED | plaud-org |
| Search | Official MCP/CLI: `list_files` ignores `query/date_*` server-side; filtering client-side (≤ 5 pages / 500 names) | plaud-find SKILL.md; cli.md:76-83 | OFFICIAL_BINARY + OFFICIAL_DOC | npm/docs |
| Search | Consumer list has no text query; tags filtered client-side; `keywords[]` exist but no search endpoint | plaud-api tags.py:26-49; 4 clients | CORROBORATED | clients |
| Search | No transcript chunking/embedding/vector store/rerank/citation anywhere; MCP host LLM does retrieval + answer | absence across corpus; MCP skills | PROVEN (absence in corpus) | — |
| Search | `has_thought_partner` on `/file/detail` hints at an in-app "ask" feature | applaud detail.ts:30 | UNKNOWN | — |

## Subscription / billing / retention

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Subscription | Embedded PAYG: $15/connected device/month (peak), $0.28/transcription hour; free 50 devices + 300 h (after first connection); month-end billing; 7-day grace; enterprise/HIPAA | billing.md | OFFICIAL_DOC | docs |
| Subscription | Consumer: only `membership_type` (`"starter"`) on `/user/me`; no quota/credit fields; no client gated | plaud-toolkit client.ts:92 | CLOUD_OBSERVED; enforcement UNKNOWN | clients |
| Subscription | What increments the "connected device" meter | not defined beyond "connected via the SDK" | UNKNOWN | — |
| Retention | Partner transcripts 7 d default; audio retention after 24 h URL UNKNOWN; full-custody (device-only) mode | data-retention.md | OFFICIAL_DOC | docs |

## Audio (see `audio.md`)

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Audio | Device format the SDK enforces: Opus 16 kHz/20 ms/80 B/frame/ch, 1–2 ch; Ogg or raw; optional 512-B `PLAUD.AI` E2EE header; selection sniff-based (`isOggAudio` never read) | ledger §8; test pin | PROVEN_BYTECODE | AAR |
| Audio | Export PCM/WAV/OPUS/MP3 (MP3 recommended); `ExportStage DOWNLOADING → TRANSCODING`; raw Wi-Fi download keeps E2EE | android-sdk.md; swiftinterface | OFFICIAL_DOC + OFFICIAL_BINARY | docs/iOS |
| Audio | iOS toolchain: `JXFileDecoder` (avc↔ogg/opus/mp3/pcm/wav), licensed "SoundPlus" noise reduction (`setSoundPlusToken`) | BLE swiftinterface:808-1068 | OFFICIAL_BINARY | iOS |
| Audio | Which shape real firmware emits; cloud transcode vs re-tag | — | UNKNOWN (U20, U19) | — |

## Observability

| Subsystem | Behavior | Evidence | Confidence | Source |
|---|---|---|---|---|
| Observability | SDK: Timber/Logback file logs, encrypted `.plaud` export (ChaCha20 with a hard-coded key literal), `PlaudLogUploadManager` auto-upload (disabled by templates), `PlaudSDKLogger.logEvent` analytics, `BleEventReporter` | ALL.txt:92300-92420; BASIC:356-390 | PROVEN_BYTECODE + OFFICIAL_BINARY | AAR/iOS |
| Observability | Hosted MCP: OTel + Prometheus + PostHog + Sentry + warehouse | npm bundle | OFFICIAL_BINARY | npm |
| Observability | Langfuse/Opik in production | no evidence | UNKNOWN | — |
