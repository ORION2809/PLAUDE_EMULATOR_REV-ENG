# AI layer — transcription, diarization, summarisation, voice agents

What Plaud's AI does can be established at three very different depths:
the **partner Transcription API** (official contract, OFFICIAL_DOC + OpenAPI +
template code), the **consumer transcript/summary pipeline** (CLOUD_OBSERVED
through community clients that hold real responses), and the **Live Agent
voice-companion stack** (PROVEN_OFFICIAL_SOURCE: Plaud-authored server code in
the `live-agent` fork, an experimental hardware line, not the recorder).
Nothing here is a model weight, a prompt, or a pipeline we ran; every claim is
what the artefacts state or enforce.

## 1. Transcription — partner API (surface B)

| aspect | fact | class |
|---|---|---|
| entry | `POST /open/partner/ai/transcriptions/` `{file_url, params?}`; `GET …/{transcription_id}`; auth `X-Client-Id` + `X-Client-Api-Key` | OFFICIAL_DOC / OpenAPI |
| models | `transcribe.model` ∈ **`plaud-fast-whisper`** (default), **`plaud-omni-3`**, **`azure-fast-transcribe`**; playground names `plaud-transcribe-1`. A model router that includes a **third-party provider** (Azure AI Speech fast transcription). | OpenAPI `transcription-model.json` |
| language | `transcribe.language` BCP-47 or `auto` (default); `detection_level` `segment` \| `chapter` (language identification granularity); "112 languages" | OFFICIAL_DOC |
| VAD | `vad.decode_silence` (default false) | OpenAPI |
| diarization | `diarization.enabled` (default false), `return_embedding` → `embeddings: {"Speaker N": float[256]}` — a 256-dimensional speaker-embedding model | OpenAPI example |
| custom vocabulary | `hotwords: "a,b,c"` (only in `transcription.json`) | OpenAPI |
| pipeline steps (stated) | "noise reduction, speaker detection, language recognition, and other speech-to-text steps" | OFFICIAL_DOC |
| task model | states `PENDING/RECEIVED/STARTED/PROGRESS/SUCCESS/FAILURE/REVOKED` = **Celery's canonical state set**; ids `task_exec_*` | OpenAPI; Celery INFERRED (high) |
| result | `{text, language, duration s, results[{start, end, text, speaker_id "Speaker 1", language, language_probability}]}`; prose pages show `segments[{…, speaker}]` instead (doc-internal inconsistency); the iOS binary's older Workflow schema uses `speaker, index` | OpenAPI / swiftinterface |
| limits | 60 req/min; 24 h max recording; 6 h max with diarization; "chunk > 5 h"; input M4A/MP3/WAV (upload API accepts `mp3|opus`); results retained **7 days** | OFFICIAL_DOC |
| billing | $0.28 per transcription hour after 300 free hours | OFFICIAL_DOC |
| what the template does with it | pins `plaud-fast-whisper`, `language auto`, diarization **off**; polls 5 s × 240; stores `results[]` locally; **no summary call exists on the partner surface** | OFFICIAL_SOURCE |

The iOS SDK binary additionally embeds an **undocumented Workflow API**
(`taskType: audioTranscribe | aiSummarize | aiEtl (clinicalReport, dealAnalysis) | audioMerge | custom`,
metadata `organizationId/ownerId/deviceSn`) matching surface A's
`/api/workflows/*`, deprecated on 2026-09-14. It is the only official trace
of partner-side *summarisation* and *ETL* task types; their contracts are
UNKNOWN.

## 2. Transcript + summary — consumer product (surface C, CLOUD_OBSERVED)

Observed by four community clients; single-client items are marked.

- **Start** is a metadata write (plaud-api): `PATCH /file/{id}
  {extra_data: {tranConfig: {language, type_type: "system", type: "REASONING-NOTE", diarization: 1, llm: "auto"}}}`.
  No dedicated job endpoint. `type` is a **summary template id**; observed
  values `REASONING-NOTE`, `CASUAL-CONVERSATION`; `type_type: "system"`
  implies user/custom templates exist (UNKNOWN catalogue — the marketing
  "10,000+ templates" is UNVERIFIABLE from the corpus); `llm: "auto"` implies
  server-side model routing (UNKNOWN choices).
- **Poll**: `POST /ai/transsumm/{id}` (`{is_reload:0, summ_type, summ_type_type, info, support_mul_summ:true}` or `{}`) →
  `status 0 "task processing"` → `1 "task complete"` (a live `-111 "success"`
  variant exists; `-12` for recordings older than ~March 2026 whose artefacts
  remain reachable via `content_list` — a storage migration is INFERRED).
  Returns `data_result[{start_time, end_time, content, speaker, original_speaker}]`,
  `data_result_summ` (JSON string, or an object for very short recordings),
  `data_result_summ_mul`, `outline_result[{start_time, end_time, topic}]`,
  `task_id_info`.
- **Persist**: plaud-api's README says the client must `PATCH /file/{id}`
  the results back "to persist" — i.e. on the web surface the *client* commits
  AI output (single client; whether the Plaud app does the same is UNKNOWN).
- **Artefacts** (`GET /file/detail/{id}`): typed `content_list` blocks —
  `transaction` (segments), `transaction_polish` (**AI-cleaned transcript**),
  `outline`, `auto_sum_note` (summary), `sum_multi_note` (secondary notes),
  and inline `pre_download_content_list` (incl. early `marks` JSON =
  device highlight-button moments `mark_type/mark_content/timestamp`).
  Summary payload (real capture): `{ai_content: <markdown>, category: "Chat Note",
  summary_id: "20251119154839-v2@<hex>-1", summ_type, header: {headline, keywords[]}, state: 10}`;
  summaries ship with **unresolved template variables** (`$[audio_start_time]`,
  `[Insert …]`) and Plaud-cookie-gated image URLs — the templating engine is
  server-side and invisible.
- **Speakers**: default labels "Speaker N"; `original_speaker` preserved after
  user rename; a global `GET /speaker/list → {id, name, embedding[]}` "used for
  voice identification" exists, but no client links it to transcript labels
  and `/speaker/sync` is never called → speaker *identification* (vs
  diarization) is a product feature whose mechanics are UNKNOWN.
- The official MCP skill's `plaud-read` distinguishes `get_note` ("AI summary,
  action items, key topics") from `get_transcript` and names `high_light` notes
  — corroborating the consumer artefact vocabulary from an official source.

## 3. Live Agent — Plaud's voice-companion AI stack (PROVEN_OFFICIAL_SOURCE, experimental line)

`reference/plaud-org/live-agent` (HEAD "Merge branch feat_hardware", 2026-01-15)
is a fork of `xinnan-tech/xiaozhi-esp32-server` with 227 Plaud-authored files
(none exist upstream). It is **not** the recorder product: no `plaud.ai`,
NotePin or Tinno string exists in it; its device is the sibling
`xiaozhi-esp32` fork on an ESP32-S3-Korvo-2 dev kit with a "hi plaud" wake
word. What it shows about how Plaud engineers build realtime AI:

- **Pipeline** (default `custom_config.yaml`): SileroVAD (FSMN available) →
  **TEN turn detection** (own FastAPI microservice `POST /turn-detect`, GPU image
  `jasonyang1/turn-detection-serve:v2`, with a local fast path) → ASR
  **Groq `whisper-large-v3-turbo`** (BytePlus streaming ASR available) → LLM
  **OpenRouter `google/gemini-2.5-flash`** (fallback `openai/gpt-5.2-chat`) →
  TTS **MiniMax dual-stream** (Cartesia, Deepgram, ElevenLabs, Fish dual/single
  stream written by Plaud) → opus 16 kHz mono 60 ms to the device.
- **Orchestration**: `core/parallel`, an LLMCompiler-style planner/executor
  (arXiv 2312.04511) with feature flags for parallel tool execution, "smart
  interruption < 400 ms", guardrails and a performance tracer.
- **Prompting**: a roles system assembling `role.md` (voice-call persona,
  1–3 sentence brevity, no markdown, strict emotion tags, EN/ZH hints, MemU
  user persona); persona generation via `gemini-2.5-flash` / `gemini-3-pro-preview`
  framed as a "Character Designer" retrieving IP lore (Labubu / Pop Mart) —
  consumer character-companion positioning.
- **Live Agent API** (79-file FastAPI + Postgres 16 + S3/MinIO): users (bcrypt,
  HS256 JWT, 7-day TTL), agents (instruction, `voice_opening/closing`,
  `wake_word`, templates), devices with many-to-many agent bindings and a
  wake-word → agent resolver, voices (Fish Audio / MiniMax cloning + library),
  chat history with opus audio in S3, memory + memory-sharing (see `memory.md`),
  `/stt/transcribe` (Groq Whisper), `/tts/synthesize`.
- **Benchmarks**: a 2025-11-11 LLM TTFT/latency comparison across Groq,
  OpenRouter, OpenAI (incl. `anthropic/claude-sonnet-4.5`).
- **Deployment**: EC2 ("old region") and Dokploy ("new region"), Singapore
  compose file; hosts are third-party APIs (memu.so, fish.audio, minimax.io,
  openrouter.ai, groq.com, BytePlus). No Plaud production host appears.

Reading: this is an **experimental product line** (branch `feat_hardware`;
"hospital assistant" role files hint at a healthcare pilot). It does not
describe the recorder's cloud AI, but it is the only source that shows Plaud's
provider choices, and those are *aggregated third-party models*, not
proprietary ones — consistent with the partner API exposing
`azure-fast-transcribe` alongside `plaud-*` names.

## 4. What is proprietary vs aggregated (evidence-weighted)

| capability | evidence for Plaud-own | evidence for third-party |
|---|---|---|
| ASR (recorder) | model names `plaud-fast-whisper`, `plaud-omni-3` (a Whisper-derived and an "omni" model, names only) | `azure-fast-transcribe` option; Live Agent uses Groq Whisper / BytePlus |
| diarization + speaker embeddings | 256-d embeddings returned; `original_speaker`; global speaker voiceprints | none specific |
| summarisation / notes | template system (`summ_type`, `type_type`, multi-note), outline, polish, highlights | `llm: "auto"` routing; Live Agent uses Gemini/GPT via OpenRouter |
| TTS / voice | — | Fish Audio, MiniMax, ElevenLabs, Cartesia, Deepgram (Live Agent) |
| memory | — | MemU hosted API; mem0 fork packaged but unused (`memory.md`) |

## 5. Unknowns (all EXTERNAL / CREDENTIAL / CLOUD)

Model internals and hosting; the summary template catalogue and `llm:"auto"`
policy; how `speaker/list` voiceprints are applied; whether the Plaud app
persists AI results client-side like the web client; `-111`/`-12` semantics;
what `aiEtl` task types produce; whether any Live Agent component is in
production; AI usage metering (no quota field exists anywhere).
