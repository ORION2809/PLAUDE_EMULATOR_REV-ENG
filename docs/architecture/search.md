# Search and knowledge layer — what exists, evidenced

The mission asked whether "What did I discuss with John last month about the
project?" is answered by SQL + keyword + vectors + RAG + an LLM. The corpus
supports a much narrower, and fully evidenced, answer.

## 1. Official surfaces expose **no server-side search**

- **MCP** (`@plaud-ai/mcp` 0.3.13, OFFICIAL_BINARY + OFFICIAL_DOC): the shipped
  `plaud-find` skill states verbatim "Plaud's `list_files` API does **not**
  accept `query`/`date_from`/`date_to` server-side — unknown params are
  silently ignored. Filtering happens client-side." The tool paginates up to
  5 pages of `GET /open/third-party/files/?page&page_size` and matches
  case-insensitively on `name`, with an inclusive date window on `created_at`.
- **CLI** (`@plaud-ai/cli` 0.3.14): `plaud search <keyword>` is "client-side
  keyword search (case-insensitive) against recording names. Scans up to 500
  most recent recordings."
- The MCP's `plaud-digest` skill builds weekly roll-ups by fetching `get_note`
  for ≤ 50 recordings and synthesising in the *host* LLM; `plaud-read`
  answers content questions from `get_note`/`get_transcript`. **Retrieval,
  ranking and answer generation happen in the user's AI client, not in Plaud's
  cloud.** There is no `ask`, `search`, `embed` or `chat` tool.

## 2. Consumer web API (CLOUD_OBSERVED): metadata filters only

`GET /file/simple/web?skip&limit&is_trash&sort_by=start_time|edit_time&is_desc`
is the only listing; there is no text query parameter in any client. Tags
(`/filetag/`) are matched client-side against `filetag_id_list`; plaud-api
fetches `limit=99999` to filter by tag. `keywords[]` exists on file rows and
`header.keywords[]` on summaries (server-extracted keywords are a **feature**,
but no endpoint searches them). The `embeddings` field on `/file/detail` and
`/speaker/list` are *speaker* voiceprints, not text embeddings.

## 3. What would be needed for the hypothesised RAG, and what evidence exists

| component | evidence |
|---|---|
| transcript chunking / indexing granularity | none; transcripts are delivered whole as `transaction` blocks (gzip JSON) or paged by `next_cursor` |
| text embeddings / vector DB | none on the recorder side; memU (Live Agent) uses OpenAI-API embeddings with numpy similarity and no vector DB (`memory.md`) |
| lexical search | none server-side; client-side name matching only |
| reranking / citation | none; the MCP skills instruct the host LLM to cite by file name |
| conversation memory | Live Agent only (memU persona injection); nothing on the recorder |
| LLM answer generation over recordings | delegated to the MCP host (Claude, ChatGPT, Cursor…) |

Therefore the answer to the mission's question, **as far as any legitimate
artefact shows**, is: *SQL/metadata filter → client-side keyword match → the
user's own AI client reads notes/transcripts → the client's LLM answers.* No
vector store, no server RAG, no "Ask Plaud" endpoint is visible. This is a
statement about the evidence, not about Plaud's app: an in-app "ask" feature
would live on an endpoint no community client or official tool exposes
(UNKNOWN), and `has_thought_partner` on `/file/detail` (a field name only)
hints at one.

## 4. Search-adjacent product facts that *are* evidenced

- Server-extracted `keywords[]` per recording and per summary header.
- Summary categories (`category: "Chat Note"`), template ids, outlines with
  `topic` per time range — structured metadata an in-app search could use.
- Highlights (`mark_memo` block; device highlight button) with timestamps.
- Workspaces (`workspace_type 0` personal vs team) scope the recording set.
- Speaker voiceprints for identification (mechanics UNKNOWN).

## 5. Unknowns

The Plaud app's own search/ask UI and its endpoints; whether `keywords` are
indexed; the purpose of `has_thought_partner` and `download_path_mapping`;
whether the hosted MCP at `mcp.plaud.ai` adds any server-side retrieval beyond
the stdio tool set (its tool inventory is the same seven tools).
