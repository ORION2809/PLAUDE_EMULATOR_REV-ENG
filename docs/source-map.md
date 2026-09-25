# Source map — every source, its role, and what it contributes to the product model

How to read: **role** is what the source *is*; **class** is the strongest
evidence class it can carry; **contributes** is the part of the Plaud product
it uniquely explains; **delta verdict** (forks only) is the content-based
comparison against the upstream in the corpus (all clones are shallow, so
history was never used). Corrections to `docs/SOURCES.md` are marked ✎.

Role vocabulary: OFFICIAL_SOURCE · OFFICIAL_BINARY · OFFICIAL_DOC ·
THIRD_PARTY_CLOUD_CLIENT · LINEAGE_FORK · UPSTREAM_REFERENCE ·
EMULATION_SUBSTRATE · UNRELATED.

## 1. Plaud-AI organisation (16 repos)

| repo | role | contributes | delta verdict / notes |
|---|---|---|---|
| `plaud-sdk-public` | OFFICIAL_SOURCE (templates) + OFFICIAL_BINARY (AAR sha `041a6f88…`, 3 iOS frameworks 1.0.13 + swiftinterface) | the whole BLE protocol (R1–R7), the partner mobile product (`mobile.md`), device lifecycle/OTA/Wi-Fi (`bluetooth.md`, `hardware.md`), the recovery flow, cloud call shapes | ✎ the *iOS* frameworks here differ byte-wise from the copies in the three wrappers (same 1.0.13; packaging unknown); the AAR is identical everywhere |
| `plaud-embedded-skills` | OFFICIAL_DOC (agent skills) + OFFICIAL_SOURCE (`user-token-script.ts`) | the Plaud Embedded product definition, identity chain, device constraints, Transcription/File API contracts | — |
| `embedded-capacitor` | OFFICIAL_SOURCE | the web-app-wrapper architecture, a **partner backend reference** (Next.js routes minting tokens and proxying AI calls), the bridged 9-method/12-event surface | demo backend routes are unauthenticated token-minting oracles (design note, not a Plaud cloud fact) |
| `embedded-react-native`, `embedded-flutter` | OFFICIAL_SOURCE | typed wrapper surfaces (TS/Dart), what wrappers *drop* vs native (recovery, Wi-Fi, OTA, settings), Android handshake prerequisites | credentials baked client-side "DEMO ONLY" |
| `live-agent` | LINEAGE_FORK of `xinnan-tech/xiaozhi-esp32-server` **with substantial Plaud-authored additions** | Plaud's realtime AI stack and provider choices; Live Agent API (agents, devices, voices, chat, memory sharing); the ESP32 voice-companion product line | 227 Plaud-only files verified absent upstream (FastAPI service, turn-detection service, orchestrator, providers, roles); 0 `plaud.ai` strings; ✎ dump.md's "livekit/agents" is wrong (SOURCES.md already corrected) |
| `xiaozhi-esp32` | LINEAGE_FORK of `78/xiaozhi-esp32` (v2.0.3 snapshot) | the device side of that prototype: Korvo-2 dev kit, "hi plaud"/"hi nicebuild" wake words, private LAN server | true delta < 10 files; the manifest's 376 only-in-Plaud paths are upstream relocations |
| `client-sdk-esp32` | LINEAGE_FORK of `livekit/client-sdk-esp32` (0.3.0) | a LiveKit voice-agent prototype with audio diagnostics | 19 Plaud-only files; committed test Wi-Fi credential (noted, not reproduced) |
| `live-agent-memory` | LINEAGE_FORK of `mem0ai/mem0` (0.1.117) | proves Plaud packaged and versioned a mem0 fork (`v1.0.0`) | packaging-only delta; unused by any corpus repo |
| `plaud-memU-server`, `plaud-memU-ui` | LINEAGE_FORK (verbatim NevaMind memU) | the memU data model and its "no vector DB" dependency profile | 0 Plaud strings; ✎ SOURCES.md names `Jununn/*` as upstream; the READMEs name `NevaMind-AI/*` |
| `langfuse` | LINEAGE_FORK (unmodified v3.124.1 snapshot) | nothing product-specific | ✎ not a Chinese-localised fork — `README.cn.md` is upstream's own |
| `plaud-opik` | LINEAGE_FORK (unmodified v1.9.48 snapshot) | nothing product-specific | pure drift vs upstream 2.2.73 |
| `goreplay` | LINEAGE_FORK of `probelabs/goreplay` 2.0.0 **with a Plaud deployment delta** | evidence that Plaud deploys traffic capture/replay (Jenkins, build-from-source with libpcap, `--input-raw :8080 → localhost:7100`) | commit by `shuo@plaud.ai` 2025-11-25; ✎ "shadow prod into test" is an inference, targets are unnamed |
| `vite-react-template` | UNRELATED (byte-identical Cloudflare template) | — | — |
| `yt-DeepResearch-Backend` | UNRELATED (tutorial copy, single "test demo" commit) | — | ✎ upstream is `ShenSeanChen/yt-DeepResearch-Backend` per its README |

## 2. Third-party ecosystem (6 repos)

| repo | role | contributes | notes |
|---|---|---|---|
| `riffado` (AGPL) | THIRD_PARTY_CLOUD_CLIENT + companion product | the consumer cloud as a syncing companion sees it: regions/`-302`, UT/WT/cookie generations, workspaces, list/detail/`content_list`, `data_link` gzip artefacts, real captures (`PALUD.AI`, `-12`, summary payloads), Cloudflare countermeasures (Webshare proxies, rate limit), its own transcription/summary pipeline and Stripe billing (labelled companion, not Plaud) | ✎ SOURCES.md's "repetition-loop guard" does not exist in riffado |
| `plaud-api` (Python) | THIRD_PARTY_CLOUD_CLIENT | the consumer AI flow (`PATCH tranConfig` → `transsumm` → client `PATCH` persist), upload flow, Pydantic entities, browser fingerprint headers | single-client for several facts |
| `plaud-toolkit` (TS) | THIRD_PARTY_CLOUD_CLIENT | password login, session eviction (`sid`), token-lifetime drift (300 d → 30 d), `membership_type`, MCP/Obsidian sync patterns | ✎ SOURCES.md's "~300 d tokens" is contradicted by the toolkit's own `auth.ts` |
| `applaud-rsteckler` (n8n) | THIRD_PARTY_CLOUD_CLIENT | cookie-session model (`pld_ut`/`pld_urt`, refresh), browser-storage mechanism (documented, never run), `first_party.ts` transcript detection, `transsumm -12` for old recordings, summary sanitisation of template variables | |
| `python-plaud-ai` | THIRD_PARTY_CLOUD_CLIENT (docs-mirroring) | the developer-platform form of surface A; webhook HMAC (`Plaud-Signature`) | as shipped it cannot authenticate (bug) |
| `applaud-landoncrabtree` | UNRELATED (self-hosted Whisper app) | — | name clash only |

## 3. Upstream references (11 repos)

| repo | role | used for |
|---|---|---|
| `bumble` | EMULATION_SUBSTRATE | the emulator's BLE stack, virtual link, android-netsim transport (R4–R7) |
| `xiaozhi-esp32`, `xiaozhi-esp32-server`, `livekit-client-sdk-esp32`, `mem0`, `opik`, `goreplay` | UPSTREAM_REFERENCE | content-based fork-delta baselines |
| `livekit-agents` | UPSTREAM_REFERENCE | refutes dump.md's `live-agent` parentage (3 common paths) |
| `pyroomacoustics`, `kokoro`, `piper` | UPSTREAM_REFERENCE | synthetic-audio tooling for the harness's later layers; no product evidence |

## 4. Official material fetched read-only during R1 (not in `reference/`)

| source | role | archived at | contributes |
|---|---|---|---|
| `docs.plaud.ai` — 43 pages incl. `llms.txt` index and 5 OpenAPI specs | OFFICIAL_DOC | `build/docs-plaud-ai/` (sha256 manifest) | platform model, device specs, binding registry, upload/transcription contracts, models, limits, billing, retention, regions, changelog, MCP/CLI, low-level SDK references |
| `@plaud-ai/mcp` 0.3.13, `@plaud-ai/cli` 0.3.14 | OFFICIAL_BINARY | `build/npm-plaud/` (sha256 manifest) | the third-party OAuth surface, `/open/third-party/*` endpoints, content-block vocabulary, client-side search, hosted MCP infrastructure |

## 5. Corpus-level conclusions

1. **Why the corpus was assembled** (SOURCES.md) holds, with the corrections
   marked ✎. Its real yield for the product model is uneven: the official
   SDK repo + Embedded materials explain the *partner* product completely;
   the community clients are the *only* window on the consumer product; the
   AI/memory/observability forks are lineage signals of varying strength
   (two deployment-grade: `goreplay`, `live-agent`; three fork-button:
   `langfuse`, `plaud-opik`, memU wrappers; one packaged-but-unused:
   `live-agent-memory`).
2. **Marker-based fork triage is necessary but not sufficient**: `live-agent`
   has zero "plaud" tokens yet 227 Plaud-authored files; `xiaozhi-esp32` has
   376 "Plaud-only" paths of which < 10 are Plaud's. Content diff against the
   upstream snapshot is the only reliable method.
3. **Independent agreement** was found for: the consumer list/detail model
   (4 clients), the `-302` region envelope (4), UA-based edge blocking (4),
   the `data_type` block vocabulary (official skill + 2 clients), the Tinno
   platform (bytecode + native libs + OTA strings), the token derivation
   (bytecode + templates + runtime), and the Embedded identity chain (skills +
   demo backend + docs + OpenAPI).
