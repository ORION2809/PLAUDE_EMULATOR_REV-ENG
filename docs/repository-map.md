# Repository Map

This map records the supplied repositories as inspected on 2026-09-21. The
repositories under `reference/` retain their own `.git` metadata and remotes;
the project-owned implementation directories are currently empty.

## Project-owned directories

| Path | Current contents | Role |
|---|---|---|
| `emulator/plaudsim/` | empty | Reserved for the Plaud device emulator; no implementation exists yet. |
| `generator/` | empty | Reserved for synthetic audio and ground-truth generation. |
| `pipeline/` | empty | Reserved for audio/ASR/diarization processing. |
| `evals/` | empty | Reserved for evaluation metrics and gates. |
| `tests/` | empty | Reserved for harness tests. |
| `data/corpora/` | empty | No audio corpus is checked out locally. |
| `docs/` | `SOURCES.md`, `protocol.json`, `protocol-spec.md`, `reference-pins.txt` | Existing evidence and provenance documents. |
| `scripts/` | fetch/extraction scripts | Existing reproducible acquisition and protocol extraction tooling. |

## Plaud-AI repositories

| Repository | Relationship / purpose | Target relevance |
|---|---|---|
| `reference/plaud-org/plaud-sdk-public` | Official SDK and iOS/Android template apps; ships SDK binaries and public interfaces. | Authoritative Plaud device-client source. |
| `reference/plaud-org/client-sdk-esp32` | Plaud copy/fork of the LiveKit ESP32 client lineage. | Realtime ESP32 audio, not Note BLE protocol. |
| `reference/plaud-org/xiaozhi-esp32` | Plaud fork of `78/xiaozhi-esp32`. | Embedded voice-agent reference, not recorder protocol. |
| `reference/plaud-org/live-agent` | Plaud repository whose recorded parent is `xinnan-tech/xiaozhi-esp32-server`, not LiveKit Agents. | Server-side voice-agent reference. |
| `reference/plaud-org/live-agent-memory` | Archived Plaud copy of Mem0. | Memory integration reference. |
| `reference/plaud-org/plaud-memU-server` | Plaud copy/wrapper of MemU server. | Current memory integration reference. |
| `reference/plaud-org/plaud-memU-ui` | Plaud copy/wrapper of MemU UI. | Current memory UI reference. |
| `reference/plaud-org/langfuse` | Plaud copy of Langfuse. | Observability/evaluation platform, not device protocol. |
| `reference/plaud-org/plaud-opik` | Plaud copy of Opik. | Observability/evaluation platform, not device protocol. |
| `reference/plaud-org/goreplay` | Plaud copy of GoReplay. | HTTP capture/replay; useful for cloud mock testing. |
| `reference/plaud-org/plaud-embedded-skills` | Plaud Embedded agent skills. | Platform documentation/context only. |
| `reference/plaud-org/embedded-capacitor` | Plaud Embedded Capacitor wrapper. | SDK wrapper, not recorder protocol. |
| `reference/plaud-org/embedded-react-native` | Plaud Embedded React Native wrapper. | SDK wrapper, not recorder protocol. |
| `reference/plaud-org/embedded-flutter` | Plaud Embedded Flutter wrapper. | SDK wrapper, not recorder protocol. |
| `reference/plaud-org/vite-react-template` | Generic frontend template. | Not relevant to reconstruction. |
| `reference/plaud-org/yt-DeepResearch-Backend` | Archived/experimental research backend fork. | Not relevant to reconstruction. |

## Third-party Plaud ecosystem

| Repository | Purpose | Target relevance |
|---|---|---|
| `reference/third-party/riffado` | Self-hosted sync, audio decoding, transcription, diarization, storage, and UI. | Strong audio/sync/error-handling reference; no BLE device implementation found. |
| `reference/third-party/plaud-api` | Python client reverse-engineered from browser traffic. | Strong cloud schema, endpoint, and fixture source; not BLE. |
| `reference/third-party/python-plaud-ai` | Second Python cloud client with device/file/workflow models. | Cross-check cloud schema and API behavior. |
| `reference/third-party/applaud-rsteckler` | n8n nodes and local sync server. | Cloud sync/workflow reference; no Plaud BLE emulator. |
| `reference/third-party/plaud-toolkit` | TypeScript core, CLI, MCP server, and Obsidian plugin. | Cloud API and user workflow reference. |
| `reference/third-party/applaud-landoncrabtree` | Separate local Whisper/LLM flashcard application. | Audio/prompt workflow reference only. |

## Upstream repositories

| Repository | Why supplied |
|---|---|
| `reference/upstream/bumble` | Generic Python Bluetooth stack with GATT server/client and virtual/emulated transports; best emulator substrate. |
| `reference/upstream/livekit-client-esp32` | Upstream comparison for Plaud's ESP32 copy. |
| `reference/upstream/xiaozhi-esp32` | Upstream comparison for Plaud's ESP32 fork. |
| `reference/upstream/xiaozhi-esp32-server` | Upstream comparison for Plaud's `live-agent`; confirms the fork relationship. |
| `reference/upstream/livekit-agents` | Comparison target for the incorrect earlier `live-agent` identification. |
| `reference/upstream/mem0` | Upstream comparison for archived `live-agent-memory`. |
| `reference/upstream/opik` | Upstream comparison for `plaud-opik`. |
| `reference/upstream/goreplay` | Upstream comparison for Plaud's GoReplay copy. |
| `reference/upstream/pyroomacoustics` | Room/microphone simulation candidate; not yet used locally. |
| `reference/upstream/kokoro` | TTS candidate for synthetic audio; not yet used locally. |
| `reference/upstream/piper` | Alternative TTS candidate; GPL licensed and not yet used locally. |

## Evidence flow

```text
plaud-sdk-public
    -> shipped AAR + iOS frameworks/interfaces + source template apps
    -> docs/protocol.json and docs/protocol-spec.md
    -> future Plaud protocol adapter/emulator

bumble
    -> generic GATT server/client + virtual transport + tests
    -> future software-only BLE harness

plaud-api / python-plaud-ai / applaud / plaud-toolkit
    -> cloud entities, endpoints, fixtures, and sync behavior
    -> future local API/mock-cloud boundary

riffado / applaud-landoncrabtree
    -> audio sniffing, decoding, transcription, diarization, and storage patterns
    -> future pipeline/evaluation work

pyroomacoustics / kokoro / piper
    -> candidate synthetic-audio dependencies
    -> future generator, after local data and protocol work are settled
```