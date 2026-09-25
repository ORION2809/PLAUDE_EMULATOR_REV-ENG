# R6-S3: authentic Plaud audio discovery (read-only evidence sweep)

Verdict: **NO AUTHENTIC PLAUD AUDIO FIXTURE FOUND; EXTERNAL EVIDENCE REQUIRED.**

No code changed, no fixture created, no test added (per §10-§11: nothing
authentic exists to regress against). Full suite re-run green (214).
`reference/**` untouched. Claims A/B stand from R6-S2; claim C stays OPEN.

## 1. Locations searched

Whole worktree `find` for audio extensions (`.ogg .opus .wav .pcm .mp3
.m4a .aac .flac .asr`) and `.bin/.dat`; files >1 MB and >100 kB in work
dirs; `reference/plaud-org/plaud-sdk-public` (AAR listing, `res/raw`,
iOS `Resources`, template `RecordingStore` + file-detail activities);
`reference/third-party/*`; `data/`; `docs/`; `emulator/ tests/ scripts/`;
`r4-s2..r6-s2` evidence dirs; repo git history for added audio files.

## 2-3. Candidate register (path | size | magic | sha256 | class | why rejected)

| Candidate | Size | Magic | sha256 (short) | Class | Disposition |
|---|---|---|---|---|---|
| `tests/fixtures/r6s2_16k_mono/stereo.ogg` | 11 271 / 13 201 B | OggS | `0f45367b…`, `e13d47c5…` | SYNTHETIC | ours (R6-S2); valid Ogg/Opus, not a capture |
| `reference/plaud-org/plaud-opik/.../audio01.wav` | 10 544 134 B | RIFF/WAVE | `9d8e1192…` | THIRD_PARTY_PLAUD | observability-fork e2e attachment (conftest.py), generic media |
| `.../audio02.mp3` | 725 240 B | ID3 | `45c182f5…` | THIRD_PARTY_PLAUD | same; not a device recording |
| `reference/.../xiaozhi-esp32/.../tr-TR/welcome.ogg` (+~700 siblings) | 3 010 B | OggS/OpusHead ch1 preSkip 312 | `f991c3b5…` | THIRD_PARTY_PLAUD | ESP32 chatbot TTS UI prompts, not Plaud-recorder captures |
| `reference/third-party/riffado/.../sample.mp3` | 4 510 B | ID3 | `07b50245…` | THIRD_PARTY_PLAUD | compression-test fixture (160-plaud-ogg-opus.test.ts et al.) |
| upstream assets (pyroomacoustics/livekit/xiaozhi-server music, vad, SenseVoice examples) | various | various | — | NON-PLAUD UPSTREAM | not Plaud-related at all (taxonomy note: no class fits; provenance known, relevance none) |
| `r4-s2/.../build/**/*.bin`, `.gradle` | various | Gradle/dex | — | NOT AUDIO | build artifacts, rejected by content |
| AAR `res/raw/` | 1 267 B | XML | — | OFFICIAL_SDK_NON_AUDIO | `logback.xml` only: the SDK ships no audio resources |
| iOS `Resources/` | — | — | — | OFFICIAL_SDK_NON_AUDIO | `Assets.xcassets` only |
| Template `RecordingStore` | 0 binaries | — | — | REFERENCE_ONLY | `{sessionId}.opus` (Android) / `.ogg` (iOS) paths referenced, zero files shipped |
| `data/corpora/` | empty | — | — | — | nothing ever fetched |

Git history: our repo has a single squashed commit; no audio file was
ever added (`git log --all --diff-filter=A -- <audio globs>` empty).

## 4-7. No authentic fixture → no pipeline run, no framing test

Sections 5-7 of the sprint require authentic bytes and therefore do not
execute. In particular: no 512-B header to inspect, no OggS sniff to
run, nothing to decrypt (and no bypass was attempted nor is any needed
for this verdict). The xiaozhi Ogg/Opus prompts were deliberately NOT
run through the pipeline: they would only re-prove claim A, which R6-S2
already closed with cleaner fixtures.

## 8-9. Claims ledger (unchanged)

A. Generic Ogg/Opus works → PROVEN (R6-S2). B. SDK-side contract
consistent with Ogg/Opus → PROVEN (R6-S1/R6-S2). C. Actual Plaud bytes
are Ogg/Opus and decodable → OPEN (no bytes found).

## 10-12. Code/tests/contradictions

None added, none needed, none found. No frozen reconstruction is
questioned by this sweep (absence of bytes contradicts nothing).

## 13-16. Blocker (exact)

To close claim C, exactly one of: (a) a real Plaud device export
(`syncFile`/`exportAudio` output: `{sessionId}.opus/.ogg/.mp3/.wav`)
obtained through the owner's legitimate workflow; (b) an officially
supported Plaud API/download containing device bytes; (c) another
legitimate source of actual Plaud recording bytes, with provenance.
Estimated minimum: one short recording in any exported format, plus its
transfer session parameters (sessionId, channels, source rate).
