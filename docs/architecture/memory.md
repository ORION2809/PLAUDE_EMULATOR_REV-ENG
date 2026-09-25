# Memory layer — what the forks prove, and what they do not

Three Plaud-org repositories concern agent memory. Content comparison (all
clones are shallow; lineage was settled by files, not history) gives a precise
picture, and its most important property is negative: **nothing in the corpus
connects any memory system to the Plaud recorder product** (Note / NotePin,
`api.plaud.ai`, the Plaud app). Everything below belongs to the experimental
Live Agent voice-companion line (`ai.md` §3).

## 1. `live-agent-memory` — a packaged mem0 fork (LINEAGE)

- Fork of `mem0ai/mem0` pinned at **0.1.117** (upstream clone is 2.1.0; the
  964/1237/350 file deltas are version drift). Plaud-specific content is
  packaging only: `pyproject.toml` "Plaud AI Fork", author `harold.guo@plaud.ai`,
  README install lines `git+https://github.com/Plaud-AI/live-agent-memory.git@v1.0.0`
  (also `v1.0.0-beta.x`, `rc.x`), a Plaud copyright line in LICENSE, one commit
  "remove openai dependency constraint in another place" (2025-11-05).
- The mem0 0.1.117 architecture as forked: LLM fact extraction → embed →
  vector-store search → LLM ADD/UPDATE/DELETE decision → SQLite history.
- **No repo in the corpus depends on the fork**; `live-agent` pins
  `mem0ai==1.0.0` from PyPI and does not select it. The research dump's
  "first-gen memory layer, since abandoned" is PARTIAL: it was packaged for
  installation (tagged releases), so it was *used* somewhere not in the corpus,
  then superseded.

## 2. `plaud-memU-server` / `plaud-memU-ui` — verbatim memU wrappers (LINEAGE)

- Byte-for-byte NevaMind `memU-server` (43-line FastAPI over `memu-py==0.6.0`:
  `POST /memorize` writes the conversation JSON to disk and calls
  `MemoryService.memorize(modality="conversation")`; `POST /retrieve` →
  `service.retrieve([query])`) and `memU-ui` (React 19 playground, package
  name `hippocampus-playground`). Zero Plaud strings; AGPL-3.0.
- `memu-py 0.6.0` depends only on `httpx, numpy, openai, pydantic` and ships
  as a compiled wheel → OpenAI-API embeddings with in-process numpy
  similarity and file storage; **no vector database, no graph database**
  (INFERRED from dependencies).
- memU data model (from the UI types): `Resource(id, url, modality, local_path, caption)` →
  `MemoryItem(id, resource_id, memory_type, summary, score)` →
  `MemoryCategory(id, name, description, summary, score)` + relations;
  retrieve response `{resources[], items[], categories[], original_query,
  rewritten_query, needs_retrieval, next_step_query}` — i.e. query rewriting
  and a retrieval-necessity gate live in memU.

## 3. `live-agent` — the memU integration Plaud actually wrote (PROVEN_OFFICIAL_SOURCE)

- Adds a `memu` memory provider (absent upstream) wrapping the **hosted**
  `https://api.memu.so` (`MEMU_API_KEY`), selected as `Memory: memu` in
  `custom_config.yaml`; the inherited `mem0ai` provider remains configured but
  unselected, and Plaud patched it with `get_user_persona()`.
- Runtime use: (1) the system prompt gets a **user persona** built from two
  memU categories (`profile`, `event`; a unit test names `profiles/events/
  activities/preferences`, so the category names vary); (2) memories are
  **prefetched on partial ASR text with a 1.5 s timeout** during
  pseudo-streaming; (3) conversations of ≥ 3 turns are memorised as
  "Name: text" lines.
- The Plaud-authored Live Agent API proxies memU behind
  `/api/live_agent/v1/memories/{search, create, update, delete, sharing/get, sharing/update}`
  with Postgres tables `memory_sharing (user_id, agent_id, share_type none|specific|all)`
  and `memory_sharing_targets` — **cross-agent memory sharing** is a product
  concept. `memory_service.use_mock` is hard-coded `True` in the API (mock
  memories); whether production runs against memU is UNKNOWN.
- memU `user_id` defaults to a device MAC (`51:3F:8D:59:D3:DB`, alias
  `xiaozhi-web-test`) — identity keyed by device, not by Plaud account.

## 4. Verdicts on the research-dump claims

| claim | verdict | basis |
|---|---|---|
| `live-agent-memory` is mem0 | SUPPORTED | pyproject/README/LICENSE |
| first-gen, abandoned | PARTIAL | packaged with release tags, then unselected in favour of memU; no consumer found |
| memU wrappers are the second-gen layer | PARTIAL | verbatim copies; the *integration* is in live-agent, using hosted memU |
| "migrated from mem0 to memU" | SUPPORTED **within the Live Agent project only** | selected provider switched; no recorder relevance |
| memory/RAG powers "ask your recordings" | **UNSUPPORTED** | no source indexes transcripts, chunks, embeds, reranks or cites; see `search.md` |

## 5. Unknowns

Whether any memory system exists on the recorder side; what `has_memory`
(stub, always false) was meant to check; who consumed the tagged mem0 fork;
what the `hippocampus` codename denotes; production vs mock memory in the API.
