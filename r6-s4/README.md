# R6-S4: legitimate export ingestion readiness

Verdict: **BLOCKED — AUTHENTIC PLAUD RECORDING NOT AVAILABLE.**

New-file scan since R6-S3 (mtime > 2026-09-23, excluding `.git` and
`__pycache__`) returns only this project's own sprint outputs: R5-S7…
R6-S3 verifiers/README/tests, the R6-S2 synthetic fixtures + manifest,
`emulator/plaudsim/{sealed,audio}.py`, and PROJECT/ledger row updates.
No user-supplied recording, no device export, no API output, no new
third-party bytes. Per the stop rule: no synthetic fixture, no pipeline
change, no further reconstruction. Suite re-run green (214).

## Exact evidence required (any ONE of)

1. Device export via the owner's normal workflow (`syncFile` /
   `exportAudio` output: `{sessionId}.opus/.ogg/.mp3/.wav`), with:
   export timestamp, session ID if shown, stated duration, export
   workflow used (BLE sync vs Wi-Fi vs cloud download).
2. Officially supported Plaud API/download output with response headers
   or API logs establishing provenance.
3. Any other legitimate file explicitly identified as originating from a
   Plaud recording, with acquisition method stated.

Accepted types: `.opus .ogg .wav .mp3 .pcm` (or raw BLE-transfer bytes
with session parameters). Required provenance: acquisition method +
which layer the bytes represent (BLE-transferred vs SDK-local file vs
cloud/user-facing export — a cloud export proves export format, not BLE
format, per the hard §8 requirement).

## Ingestion path, ready on arrival

`sprint §5 pipeline` maps 1:1 onto existing components: header sniff
(`audio.parse_ogg_page` magic/strictness), 512-B handling
(`audio.h4_frame_params`/`h4_payload_spans`), packet extraction
(`audio.reassemble_page_packets`), independent decode (PyAV/libopus as
in R6-S2), PCM checks. On arrival: hash + provenance record, run
unmodified pipeline, record first failure or full decode, fill the §6
hypothesis table. No new code is needed before that point.
