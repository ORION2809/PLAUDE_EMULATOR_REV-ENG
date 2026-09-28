# Plaud product reconstruction — final report (R1, 2026-09-23)

**Verdict: PRODUCT RECONSTRUCTION COMPLETE WITH EXTERNAL-EVIDENCE BLOCKERS.**
Every layer of the Plaud product that the assembled corpus can speak to has
been mapped, each claim carries its evidence and class, and the remaining
unknowns are listed with what would settle them. This reconstruction called no
Plaud endpoint (but see the 25 Sep correction: the runtime rig's SDK sent
automatic `gen-key` requests; 19 of the 22 logs that show them record a 401 and the
other 3 never reached the server — `r7/r7-s14-wifi-real-sdk.md` D7),
no Plaud device was touched, no credential was used, no file under
`reference/**` was modified (33/33 pins verified before and after).

This report sits on top of the frozen protocol work (`docs/protocol-ledger.md`,
`docs/final-closure-report.md`). It does not re-derive the BLE layer.

## 0. The document set

| file | what it is |
|---|---|
| `docs/product-ledger.md` | the claim table: Subsystem · Behavior · Evidence · Confidence · Source |
| `docs/architecture/hardware.md` | product line, specs, platform (Tinno, not ESP32), on-device behaviour |
| `docs/architecture/firmware.md` | what the recorder firmware provably does, other hardware lines, the ESP32 prototypes |
| `docs/architecture/bluetooth.md` | lifecycle-to-message map, connection semantics, doc-vs-code |
| `docs/architecture/mobile.md` | the partner app: ownership, persistence, sync, transcription, recovery, OTA |
| `docs/architecture/cloud.md` | five surfaces, identity chains, registry, storage, processing, data model, infra |
| `docs/architecture/audio.md` | capture → transfer → export → upload → cloud artefacts |
| `docs/architecture/ai.md` | partner transcription, consumer transcript/summary, Live Agent stack |
| `docs/architecture/memory.md` | mem0 / memU lineage and what is actually integrated |
| `docs/architecture/search.md` | why there is no server-side search or RAG in the evidence |
| `docs/architecture/web.md` | the consumer web product seen through its clients |
| `docs/source-map.md` | every source → role, contribution, lineage verdict, corrections to `SOURCES.md` |
| `docs/evidence-graph.json` | 36 sources · 16 agents · 397 claims · 221 unknowns · 138 contradictions · 153 verdicts · 2 072 edges |
| `docs/product-evidence/` | the 16 agents' structured findings (1 MB JSON) and the six fork-delta manifests |

Method: two orchestrated passes over the corpus (12 extraction agents by
source cluster, then 4 by product question), each producing structured
findings with file:line citations, evidence classes, unexplained behaviours
and doc-vs-code contradictions; the fork deltas were computed content-wise
against upstream snapshots (not marker grep, which misled twice); the
official docs site and the two npm packages were fetched read-only and
archived with sha256 manifests under `build/`. All syntheses were then
written against those findings and spot-checked against the artefacts.

## 1. The product, in one picture

```
                         ┌──────────────────────────────────────────────────────────┐
                         │  PLAUD CLOUD                                             │
   ┌──────────────┐      │  C  consumer api.plaud.ai (+euc1/apse1/usw1, .cn)         │
   │ Recorder     │      │     UT/WT/cookies · file rows · content blocks · S3 gzip │
   │ (Tinno pen   │ BLE  │     PATCH tranConfig → transsumm → client PATCH          │
   │  platform)   │◄────►│  B  partner platform-{us,jp,eu,sg}.plaud.ai              │
   │ 880/881/882/ │      │     Basic→partner tok→user JWT · sdk/bind|unbind|binding │
   │ 888; G101 &  │ SoftAP│     gen-key · sn-sign · metadata · version/latest        │
   │ Heili lines  │ WS   │     file presign/complete · ai/transcriptions (Celery)   │
   └──────┬───────┘      │  B′ third-party platform.plaud.ai/developer/api/open/…   │
          │ Wi-Fi cloud  │     OAuth PKCE · files/notes/transcripts · hosted MCP    │
          │ upload (bits │  A  SDK-internal /api/… (3-hop Bearer, platform-jp)      │
          │ 5/6, dest ?) │  E  legacy TntAgent /recorder/device/* (host ?)          │
          ▼              └───────────────┬──────────────────────┬───────────────────┘
   ┌──────────────┐                      │                      │
   │ Phone        │  ── S3 multipart ──► │                      │  ◄── browser/clients
   │ partner app  │                      │                      │
   │ (SDK 1.0.13) │  ◄─ presigned dl ──  │              ┌───────┴────────┐
   │ recordings.  │                      │              │ web.plaud.ai   │
   │ json + MP3   │                      │              │ MCP/CLI, 4     │
   └──────────────┘                      │              │ community apps │
                                         │              └────────────────┘
   experimental line (not the recorder): ESP32-S3 Korvo dev kits ⇄ live-agent
   server (xiaozhi fork + Plaud FastAPI/turn-detection/LLMCompiler/memU)
```

## 2. What each layer owns

**Device (firmware).** Recording, session ids (unix seconds), storage,
markers, the identity lock (one bound `client_user_id`; other tokens are
refused), the encryption secret (pv ≥ 20), settings (21 CommonSettings),
telemetry counters, OTA application, Wi-Fi SoftAP, and two cloud-upload
modes (status bits 5/6, `Get/SetWebsocket`, `sendApiToken`) whose destination
never appears in any artefact. The device is the source of truth for the
recording inventory; the phone mirrors it.

**Phone (partner app + SDK).** BLE/Wi-Fi transport, bind/recovery
orchestration, file pull with delete-after-sync, decode/export to MP3
(Android: evictable cache), the *only* copy of AI results in the partner
product (`recordings.json`), and OTA delivery (download + MD5 + BLE push).
No entitlement logic exists anywhere client-side.

**Partner cloud (B).** Identity issuance (partner token → per-user JWT whose
`sub` becomes the handshake token), the device registry with `bind_history`,
the RSA key pair *and* the SN signature that make pv ≥ 20 binding possible,
S3 multipart storage, Celery-style transcription tasks with three named
models, 7-day result retention, PAYG billing. This is a separate tenancy:
partner users never see the consumer library.

**Consumer cloud (C / B′).** The consumer library (`file` rows, `content_list`
blocks as gzip JSON on S3), the transcript + summary job (`transsumm`),
speakers/voiceprints, workspaces, regions, cookie sessions, Cloudflare edge.
The third-party OAuth surface and hosted MCP server are a read-only view of
this same library through the developer-platform gateway.

**AI.** Two documented families (`plaud-fast-whisper`, `plaud-omni-3`,
`azure-fast-transcribe`; summary via `llm:"auto"` with `summ_type` templates
and an internal Workflow API naming `azure-gpt-4o-sc`/`gpt-4.1`) plus a
separate, fully readable experimental realtime stack (Live Agent) whose
production status is unknown.

**Memory / search.** No vector store, no embeddings pipeline, no server-side
search on any observed surface. The two memory lineages (mem0 packaging,
memU wrappers) are integrated only in the experimental Live Agent, with the
API side hard-coded to a mock.

## 3. Recording lifecycle (per transition: trigger · API · format · id · status · retry/failure · owner)

| # | transition | trigger | API / message | format | id | status | retry / failure | owner |
|---|---|---|---|---|---|---|---|---|
| 1 | capture | device button or app `startRecord(scene)` | `bleRecordStart{sessionId, reason}` | Opus 16 kHz/20 ms, 1–2 ch, Ogg or raw, optional `PLAUD.AI` E2EE header | `sessionId` = unix s | `bleRecordStop{fileExist, fileSize}` | device-authoritative; app mirrors | device |
| 2 | inventory | connect (iOS auto / Android list) | `syncFileList` paged | list rows `{sessionId, size, duration, mark}` | sessionId | — | list rebuilt from device every time; missing rows dropped | device → phone |
| 3 | pull (BLE) | user or iOS auto | HEAD / DATA (type 2) / **EMPTY_PACKAGE sentinel** / TAIL; on a gap the client sends stopSync and restarts from its cursor (R7-S13, live) | raw chunks → local file | sessionId | completion = the sentinel (`bleDataComplete`); `stopSync` only after a 5 s stall or on a gap | restart from cursor; TAIL u16 unverified; real SDK pulled the emulator's file byte-exact | phone |
| 3′ | pull (Wi-Fi) | user | `setDeviceWiFi` → SoftAP → phone WS server :8081 → 20 PDUs | AES-GCM iff cap bit 3 else ChaCha | sessionId | 8-stage `WiFiConnectStage` | batch delete confirm 15 s/4 s; close wait 20 s | phone |
| 4 | delete on device | unconditional after 3/3′ | `deleteFile` / Wi-Fi batch delete | — | sessionId | — | no rollback | phone |
| 5 | decode/export | after 3 | `exportFile` PCM/WAV/OPUS/MP3 | MP3 mono (templates) | `RecordingFile.id` (UUID) | `ExportStage` | Android cache eviction loses audio; row survives | phone |
| 6 | upload (partner) | user taps transcribe | presign → S3 PUT 5 MiB parts → complete | mp3 \| opus | `file_id`, `upload_id` | `DownloadUrl` ~24 h | none in templates | phone → partner cloud |
| 6′ | upload (consumer) | web/app | presign → S3 → merge → confirm (`scene 101`) | audio | `file.id`, `serial_number` uuid4 | `is_trans=is_summary=0` | client-side | web/app → consumer cloud |
| 7 | transcription (partner) | `POST ai/transcriptions/` | `{file_url, params{transcribe{model…}, diarization, hotwords}}` | JSON result + 256-d embeddings | `task_exec_*` | Celery states | template polls 5 s × 240, no retry; results kept 7 d | partner cloud |
| 7′ | transcript+summary (consumer) | `PATCH file/{id} tranConfig` → `POST ai/transsumm/{id}` | `REASONING-NOTE`/`CASUAL-CONVERSATION`, `diarization 1`, `llm auto` | `content_list` blocks (`transaction`, `transaction_polish`, `outline`, `auto_sum_note`, `sum_multi_note`, `mark_memo`) as gzip JSON | `file.id`, `summary_id …-v2@hex-N` | 0/1/-111/-12; `task_status 1` | client PATCHes results back; -12 for old files | consumer cloud (+client persist) |
| 8 | diarization | inside 7/7′ | `diarization{enabled, return_embedding}` | `speaker_id` per segment; global `/speaker/list` voiceprints | — | — | speaker rename = string rewrite + PATCH | cloud |
| 9 | index / search | — | none server-side; MCP/CLI filter client-side | — | — | — | — | client |
| 10 | access | UT/WT/cookies (C), user JWT (B), OAuth (B′) | list/detail/temp-url/download | Ogg/Opus tagged `PALUD.AI` served as `.mp3` | `file.id` | — | Cloudflare UA gating | cloud |
| 11 | retention | — | 7 d transcripts (B); audio after URL expiry UNKNOWN; device wipe on OTA | — | — | — | — | cloud / device |

Encryption along the path: BLE frames sealed pv ≥ 20 (ChaCha20-Poly1305);
Wi-Fi payloads AES-GCM or ChaCha; device storage "encrypted" (docs) with an
optional E2EE file header the SDK can strip or keep; phone storage plaintext
(template); cloud at rest UNKNOWN beyond S3 presigned delivery; SDK log
export ChaCha with a hard-coded key.

## 4. What the corpus was for (the assembled-corpus hypothesis)

The corpus explains the product in four distinct ways, and each repository's
inclusion can be justified by exactly one of them (`docs/source-map.md`):

1. **Official truth** — `plaud-sdk-public` (+ AAR/frameworks), the Embedded
   skills and three wrapper repos, docs.plaud.ai, the npm MCP/CLI. These
   settle the partner product completely and the device contract almost
   completely.
2. **Consumer-cloud observation** — riffado, plaud-api, plaud-toolkit,
   applaud-rsteckler, python-plaud-ai. They are the only window on
   `api.plaud.ai`; where ≥ 2 agree the fact is CORROBORATED, where one
   speaks it is single-client.
3. **Lineage** — the Plaud-org forks. Their yield is uneven and only
   content-diffing reveals it: `live-agent` (227 Plaud-authored files) and
   `goreplay` (a deployment) are strong; `xiaozhi-esp32`/`client-sdk-esp32`
   are dev-kit prototypes; `live-agent-memory` is packaging; `langfuse`,
   `plaud-opik`, both memU repos are unmodified snapshots. Two repos are
   unrelated (`vite-react-template`, `yt-DeepResearch-Backend`), one is a
   name clash (`applaud-landoncrabtree`).
4. **Substrate** — bumble (emulation), the upstream baselines, and the
   audio-synthesis tools for later harness layers.

## 5. Contradictions worth knowing (doc vs code, source vs source)

- `bleBind(protVersion, timezone)`: docs say protocol version; the facade
  passes the l3 timezone byte in both (RUNTIME).
- "Local bind generates the key pair on the device" (docs) vs cloud `gen-key`
  returning the RSA private key to the phone (bytecode).
- "All BLE traffic encrypted" (docs) vs pv-gated sealing (bytecode); force-clear
  "wipes key material" vs FE20 inert below pv 20.
- iOS docs describe 1.0.14 and list `bleState(powered:)`, `logIndex:`; the
  shipped frameworks are 1.0.13 without them, but contain an undocumented
  Workflow API, recording control, Wi-Fi sync UI, and a legacy appKey/appSecret
  `initSDK`.
- `WebsocketType` raw 0/1/2 (iOS) vs wire 1/2/3 (Android); `bleSyncFileTail`
  exposes `crc` on iOS and `status` on Android; `wifiCommonErr(cmd:16)` is
  ExtendExitTime not sync success.
- README claims "auto-download on connect" for both templates; Android is
  list-only. `PlaudDownloadFormat.mp3` "unavailable" vs `AudioExportFormat.mp3`
  "recommended".
- Consumer token lifetimes: ~300 d (older clients) vs ~30 d (toolkit) — a
  migration, not an error; SSO vs OTP create separate identities.
- Two representations of recording detail (`POST /file/list` embedded vs
  `GET /file/detail` blocks); canonical one UNKNOWN.
- Official transcription docs show `speaker_id` in the schema and `speaker`
  in prose.
- Cloud audio bytes are Ogg/Opus with vendor `PALUD.AI`, while the SDK's local
  writer stamps `TinnoTech…`: the cloud re-encodes or re-tags; the device's own
  emitted shape is unknown.

## 6. Verdicts on the research dump's claims (from the 38-item audit)

REFUTED: recorder is ESP32-class; `live-agent` derives from livekit/agents;
riffado has a repetition-loop guard; xiaozhi is reference firmware for the
recorder. PARTIAL: VCS/"voice capture system" (the VPU settings exist, the
marketing term does not), GoReplay shadowing (deployed, targets unnamed),
press-to-highlight (device markers + `mark_memo` blocks proven, UI not),
"Opus as mp3" (cloud labelling proven, device side not), "10,000 templates"
(template ids exist, catalogue unobserved). UNVERIFIABLE from the corpus:
Langfuse/Opik in production, the two-pass transcription anecdote,
`azure-fast-transcribe` provenance. Full list with evidence in
`docs/product-evidence/agent-findings-2026-09-23.json` (`dump-claims-audit`)
and as `verdict:*` nodes in the evidence graph.

## 7. What observable behaviour is still unexplained

Consolidated from 221 flagged items; the ones that change the model if
answered:

1. **Where the device's own Wi-Fi/"pan" cloud uploads go** and how they
   authenticate (`SetWebsocket`, `sendApiToken`). Needs a device on a
   monitored network or the consumer app.
2. **Which advertising branch / radio vendor real hardware uses** (U1/U18) —
   one scan capture.
3. **What real firmware emits** (Ogg vs raw vs E2EE header; U20) and what the
   cloud does with it (transcode vs re-tag; U19).
4. **Why both templates default to `platform-test.plaud.ai`**, and why the
   Android AAR's OTA path depends on a `/api/oauth/sdk-token` that 404s.
5. **The consumer app's own client behaviour** (persistence, offline queue,
   push vs poll, template catalogue, sharing/export/search endpoints) — no
   consumer app binary is in the corpus.
6. **Entitlement enforcement** — only `membership_type` is visible; nothing
   gates on it client-side.
7. **The `has_thought_partner`, `ori_ready`, `wait_pull`, `embeddings`,
   `download_path_mapping` fields** and the speaker-voiceprint linkage.
8. **G101 glasses, the Heili pen, and "NiceBuild"** — which products they are.
9. **Whether any Live Agent component is in production**, and what
   Langfuse/Opik/GoReplay instrument.
10. **The legacy TntAgent host** (U21) and the internal `/api/...` surface's
    remaining routes after the 2026-09-14 deprecation.

## 8. What was *not* done, deliberately

No production endpoint was exercised, including ones whose URL is known and
whose call would have been harmless, because every one of them requires an
account or a partner credential we do not hold. No consumer app binary was
fetched or decompiled (none is in the corpus and it was outside the
mission's "legitimately available" bound as I read it — `plaud-sdk-public`
is published for this purpose, the consumer app is not). The committed test
Wi-Fi credential in `client-sdk-esp32` is noted by existence only. The
still-running AVD and `netsimd` from R7-S12 were left untouched during this
phase and shut down at the end.

## 9. State of the repository

- 978 tests pass (`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ --timeout=120`; 301 at the time of this report, the rest added by the 2026-09-24 layer work — see PROJECT.md §8 and `r7/r7-s13-recording-pull.md` for the transfer-sequence correction found after this report was written).
- `reference/`: 33/33 pins match; no modified or untracked files in any repo;
  no bytecode caches written during this phase.
- `build/` (docs archive, npm archive, decompiled evidence) is gitignored;
  `docs/product-evidence/` and `docs/evidence-graph.json` are committed inputs
  so the graph is reproducible with `scripts/build_evidence_graph.py`.
- Nothing has been committed; the tree is ready for a single commit.
