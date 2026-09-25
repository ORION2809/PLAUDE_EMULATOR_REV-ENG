# SOURCES — every repo and dataset named in the research dump

> Fetched on demand into `reference/` (gitignored, never committed).
> Reproduce with `./scripts/fetch-references.sh` (code) and
> `./scripts/fetch-datasets.sh --list` (audio corpora).
> Licence column is the SPDX id reported by the GitHub API at fetch time.
> **2026-09-23:** the "why it matters" column was written before the corpus was read;
> content-based lineage verdicts and corrections (marked ✎) are in [`source-map.md`](source-map.md).

## 1. Plaud-AI org — 16 public repos (`reference/plaud-org/`)

| Repo | Fork of | Licence | Stars¹ | Why it matters |
|---|---|---|---|---|
| `plaud-sdk-public` | — (own) | Apache-2.0 (repo); `sdk/` binaries proprietary | 4 | Official device SDK. Template apps are source; `.framework`/`.aar` are pre-compiled. Primary protocol source (see `docs/protocol.json`). |
| `client-sdk-esp32` | `hayden-xiong/client-sdk-esp32` → `livekit/client-sdk-esp32` | — | 2 | ESP32 WebRTC audio path |
| `xiaozhi-esp32` | `78/xiaozhi-esp32` | MIT (upstream) | 1 | On-device MCP voice chatbot reference firmware |
| `live-agent` | `xinnan-tech/xiaozhi-esp32-server` ⚠️ | MIT (upstream) | 1 | Server side of the xiaozhi voice agent. **Correction to dump.md:** it is *not* `livekit/agents` — the fork parent is the xiaozhi server. `livekit/agents` is fetched separately under upstream for comparison. |
| `live-agent-memory` | `mem0ai/mem0` (archived) | Apache-2.0 (upstream) | 4 | First-gen memory layer, abandoned |
| `plaud-memU-server` | `Jununn/memU-server` | — | 0 | Second-gen memory backend wrapper |
| `plaud-memU-ui` | `Jununn/memU-ui` | — | 0 | Second-gen memory frontend |
| `langfuse` | `langfuse/langfuse` | NOASSERTION (upstream, source-available) | 0 | LLM tracing/evals, instance 1 of 2 |
| `plaud-opik` | `comet-ml/opik` | Apache-2.0 (upstream) | 0 | LLM tracing/evals, instance 2 of 2 |
| `goreplay` | — (own copy, mirrors `probelabs/goreplay`) | — | 1 | HTTP capture/replay; shadow prod traffic into test |
| `plaud-embedded-skills` | — (own) | — | 11 | Agent skills for the Plaud Embedded platform |
| `embedded-capacitor` | — (own) | — | 0 | Capacitor plugin for Embedded iOS SDK |
| `embedded-react-native` | — (own) | — | 0 | React-Native wrapper |
| `embedded-flutter` | — (own) | — | 0 | Flutter wrapper |
| `vite-react-template` | — (own) | — | 0 | Template, low value, fetched for completeness |
| `yt-DeepResearch-Backend` | `ShenSeanChen/launch-DeepResearch-Backend` (archived) | — | 2 | Unrelated experiment, fetched for completeness |

¹ Stars as of 2026-09-21, Plaud fork counts, not upstreams.

## 2. Third-party Plaud ecosystem (`reference/third-party/`)

| Repo | Licence | Stars | Why it matters |
|---|---|---|---|
| `riffado/riffado` | AGPL-3.0 | 378 | Self-hosted Plaud companion: sync → transcribe → summarise. Docker stack, AES-256-GCM pattern, magic-byte audio detection, repetition-loop guard. **Read for architecture; do not vendor into non-AGPL code.** (`openplaud/openplaud` 301-redirects here — same project, renamed. `gulleyjeremy/openplaud` is its old home.) |
| `arbuzmell/plaud-api` | MIT | 11 | Unofficial Python client for `api.plaud.ai`. Bearer-token auth. Value = free Pydantic schema dump (recordings, transcripts, speakers, tags, summaries). |
| `DmytroLitvinov/python-plaud-ai` | MIT | 4 | Second Python client, same role as above |
| `rsteckler/applaud` | MIT | 95 | n8n nodes + local sync for all Plaud devices |
| `sergivalverde/plaud-toolkit` | (no SPDX filed) | 42 | TS monorepo: core + CLI + MCP server + Obsidian plugin, auto token refresh (~300d tokens) |
| `landoncrabtree/applaud` | (no SPDX filed) | 87 | Name clash, different app: local Whisper + LLM flashcards/summaries. Useful as a pipeline reference, not Plaud-API related. |

## 3. Upstream originals for fork diffing (`reference/upstream/`)

| Repo | Licence | Why fetched |
|---|---|---|
| `livekit/client-sdk-esp32` | Apache-2.0 | True upstream behind Plaud's `client-sdk-esp32` (via hayden-xiong fork) |
| `78/xiaozhi-esp32` | MIT | True upstream behind Plaud's `xiaozhi-esp32` |
| `xinnan-tech/xiaozhi-esp32-server` | MIT | True upstream behind Plaud's `live-agent` — settles the dump.md misidentification |
| `livekit/agents` | Apache-2.0 | What dump.md *thought* `live-agent` was; kept for comparison |
| `mem0ai/mem0` | Apache-2.0 | Upstream behind archived `live-agent-memory` |
| `comet-ml/opik` | Apache-2.0 | Upstream behind `plaud-opik` |
| `probelabs/goreplay` | (moved from `buger/goreplay`) | Upstream behind Plaud's `goreplay` copy |
| `google/bumble` | Apache-2.0 | BLE emulator stack (virtual-link transport, no radio in CI) |
| `LCAV/pyroomacoustics` | MIT | Room-acoustic simulation for the synthetic generator (2-mic vs 4-mic arrays) |
| `hexgrad/kokoro` | Apache-2.0 | TTS voice for synthetic meetings |
| `OHF-Voice/piper1-gpl` | GPL-3.0 | Alt TTS voice (note GPL, keep at arm's length like AGPL) |

Deliberately **not** cloned (large / low leverage, diff on demand):
`langfuse/langfuse`, `Jununn/memU-server`, `Jununn/memU-ui`, `n8n-io/n8n`.

## 4. Eval / metric libraries — pip, not git

`jiwer` (WER/CER) · `pyannote.metrics` (DER/JER) · `meeteval` (cpWER — headline metric).
Pinned in `evals/` requirements when the harness lands. No repo clone needed.

## 5. Audio corpora — manifest only, NOT bulk-downloaded

Full corpora are tens of GB and live outside git. `scripts/fetch-datasets.sh`
documents exact URLs, licences and checksums; `--sample` pulls a tiny slice
for smoke tests, `--all` pulls everything (needs ~50 GB free).

| Corpus | Licence | Size | Use |
|---|---|---|---|
| AMI Meeting Corpus | CC BY 4.0 | ~100 h | Multi-party meetings + transcripts + speaker labels + human summaries (also evaluates summarisation) |
| LibriCSS | research-only² | ~10 h | Overlapped speech, 8-mic array — purpose-built for this |
| ICSI Meeting Corpus | LDC licence | ~72 h | Second real-meeting source, guards against AMI overfit |
| VoxConverse | CC BY 4.0 (chunks vary) | ~50 h | Diarization in messy conditions |
| MUSAN (OpenSLR SLR17) | CC0-ish³ | ~109 h | Standard noise corpus |
| RIRS_NOISES (OpenSLR SLR28) | CC BY 4.0 | ~2 GB | Standard RIR set if we skip simulation |

² LibriCSS inherits LibriSpeech terms; fine for a personal portfolio harness, re-check before any redistribution of derived audio.
³ MUSAN licence is permissive research use; see OpenSLR page for exact terms.
