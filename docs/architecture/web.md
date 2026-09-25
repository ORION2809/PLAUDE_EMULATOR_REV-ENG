# Web layer — the consumer product seen from outside

**There is no Plaud web-app source in the corpus.** The two Plaud-org
candidates are unmodified templates (`vite-react-template` is Cloudflare's
React + Vite + Hono + Workers starter, byte-for-byte; `plaud-memU-ui` is
NevaMind's memU playground). Everything below about `web.plaud.ai` and its API
comes from four community clients that replay the web app's browser traffic
with the user's own account (riffado, plaud-api, plaud-toolkit,
applaud-rsteckler → **CLOUD_OBSERVED**, **CORROBORATED** where ≥ 2 agree) and
from Plaud's own MCP/CLI packages and docs (**OFFICIAL_BINARY / OFFICIAL_DOC**),
which sit on a different surface. `applaud-landoncrabtree` is unrelated
(self-hosted Whisper app; zero `api.plaud.ai` references).

## 1. Stack indicators

| indicator | evidence | class |
|---|---|---|
| hosts | `web.plaud.ai` (also `web.plaud.cn` / `app.plaud.cn`); API `api.plaud.ai`, `api-euc1` (Frankfurt), `api-apse1` (Singapore), `api.plaud.cn`; `api-usw1` accepted by riffado's gate but unlisted; assets/presigns on `resource.plaud.ai(.cn)` | CORROBORATED |
| edge | Cloudflare bot protection; non-browser UAs → 403 or HTML challenge; JA3 scoring (riffado changelog) | CORROBORATED 4/4 |
| request fingerprint | `Origin/Referer https://web.plaud.ai`, `app-platform: web`, `edit-from: web` (→ `edit_from` on rows), lowercase `bearer` accepted, random `r=` cache-buster | plaud-api (single) |
| backend hints | pydantic-style validation envelope byte-identical on `.ai` and `.cn` (riffado); HTTP 200 + `{status, msg, request_id}` envelope with negative business codes | INFERRED (framework) |
| storage | S3 presigned delivery; content blocks as gzip JSON objects; AWS regions `us-west-2`, `eu-central-1`, `ap-southeast-1`, `us-east-1` in JWT `region` | CORROBORATED |
| identity providers | email OTP, password, Google/Apple SSO (SSO creates a separate identity from the OTP email account — "shadow account") | riffado + toolkit |
| session storage | legacy `localStorage` `tokenstr` / `pld_tokenstr` = `"bearer eyJ…"` plus `PLADU_<email>_*` keys (applaud reads Chrome LevelDB — a *mechanism*, never executed here); newer accounts: HttpOnly cookies `pld_ut` (~1 d) + `pld_urt` (~30 d) on `*.plaud.ai` | CORROBORATED (names disputed) |

## 2. Auth and session model (see `cloud.md` §2.3 for the endpoint detail)

```
OTP:      POST /auth/otp-send-code {username, user_area} → {token}   →  POST /auth/otp-login {code, token} → UT (~300 d)
password: POST /auth/access-token (form) → UT (~30 d now); mints a new `sid` and EVICTS other sessions (logs the phone app out)
cookies:  pld_ut (~1 d) + pld_urt (~30 d)  ⇄  POST /auth/refresh-user-token (rotating Set-Cookie, "erratic")
workspace: GET /team-app/workspaces/list → POST /user-app/auth/workspace/token/{wid} → WT (~24 h; claims ut_ref/wid/wtype/mid/role/jti)
```

A `-302` envelope redirects to the account's regional host; accounts can be
migrated between regions. Whether data endpoints require a WT is disputed:
on EU/APAC a bare UT yields an *empty list* (silent scoping), on the global
host it works.

## 3. Feature surface implied by the API (what the web app can do)

| feature | endpoint(s) | notes |
|---|---|---|
| library (list, sort, trash) | `GET /file/simple/web?skip&limit&is_trash=0|1|2&sort_by=start_time|edit_time&is_desc` | no text query; `data_file_total` |
| recording detail, transcript, notes | `POST /file/list [ids]` (embedded `trans_result`, `ai_content`, `summary_list`) · `GET /file/detail/{id}` (`content_list` blocks with `task_status`, presigned `data_link`; inline `pre_download_content_list`) | two representations; canonical UNKNOWN |
| player / download | `GET /file/temp-url/{id}?is_opus=0|1`, `GET /file/download/{id}` | Ogg/Opus served as `.mp3` (`PALUD.AI`) |
| rename, trash | `PATCH /file/{id} {filename}` · `{is_trash:true}` (fallback `POST /file/trash` UNVERIFIED) | |
| start transcription + summary; choose language/template/diarization/LLM | `PATCH /file/{id} {extra_data.tranConfig{language, type:"REASONING-NOTE", type_type:"system", diarization:1, llm:"auto"}}` → `POST /ai/transsumm/{id}` poll → client `PATCH` results back | template ids observed: `REASONING-NOTE`, `CASUAL-CONVERSATION`; multi-note (`sum_multi_note`), outline, polished transcript |
| speaker rename | string rewrite of `trans_result[].speaker` then `PATCH` | `original_speaker` preserved; global `GET /speaker/list` voiceprints exist |
| tags / folders | `GET /filetag/` + client-side filtering on `filetag_id_list` | |
| upload from web | presign → S3 → merge → confirm (`scene: 101`) | |
| devices | `GET /device/list` | list only; no bind/OTA/settings on the web surface |
| workspaces / teams | `GET /team-app/workspaces/list` (`workspace_type 0` = personal; other = team) | |
| account | `GET /user/me` → `data_user{id, nickname, email, country}`, `membership_type` (`"starter"`) | only entitlement signal |
| highlights | `is_markmemo` on rows; `marks` JSON / `mark_memo` block | device highlight button |
| sharing, export, folders, search, "ask" | **no endpoint observed** | marketed features with no evidence |

## 4. Web vs mobile-partner vs third-party surfaces

| operation | consumer web (C) | partner SDK/API (B) | MCP/CLI (B′) |
|---|---|---|---|
| list recordings | `/file/simple/web` | none (device file list over BLE only) | `/open/third-party/files/` |
| transcript | embedded or `content_list` blocks | `ai/transcriptions/{id}` result of *your own* upload | `get_transcript` blocks incl. `transaction_polish`, `mark_memo` |
| summary/notes | `ai_content`, multi-note, outline | **none** | `get_note` |
| start processing | `PATCH tranConfig` + `transsumm` | `POST ai/transcriptions/` | none (read-only) |
| device management | list only | bind/unbind/binding, OTA, settings, Wi-Fi | none |
| identity | UT/WT/cookies (user account) | partner-minted user JWT | OAuth (user account) |
| search | none server-side | none | client-side keyword |

Reading: C and B′ are two views of the **same consumer library** with
different auth; B is a **separate tenancy** (partner users, partner storage)
that never sees the consumer library. The MCP/CLI's region-less
`platform.plaud.ai/developer/api` default versus C's regional `api.plaud.ai`
hosts shows the consumer library is also reachable through the developer
platform's gateway — the hosted MCP server proxies it.

## 5. Companion ecosystem (what third parties build on C)

riffado (self-hosted, AGPL; Stripe-billed hosted tier; own multi-provider
transcription/summary with Plaud-transcript import, versioned sync on
`version_ms`, tombstones, encrypted-at-rest tokens, Webshare proxying and
rate limiting against Cloudflare), plaud-toolkit (core/CLI/MCP/Obsidian;
sync by `syncedIds`, whisper bridge), applaud-rsteckler (n8n nodes + local
sync poller with `first_party.ts` detection of Plaud-native transcripts and a
cookie-session port from "rovenotes-cloud"), plaud-api (Python, Pydantic
schema dump). All four **poll**; none receives pushes.

## 6. Unknowns

The web front-end stack itself; canonical detail representation; whether the
app persists AI results client-side; trash mechanism; `has_thought_partner`,
`ori_ready`, `wait_pull`, `embeddings`, `download_path_mapping` semantics;
`edit_from` value space; single-session policy scope; template catalogue,
sharing, export, folders and search endpoints; `api-usw1` existence.
