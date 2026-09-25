# plaud-harness

A hardware-free harness for reverse-engineering the Plaud Note Pro / NotePin BLE
recorder: reconstruct the device protocol from shipped artifacts, emulate the
device in software, and prove the reconstruction is correct.

**Status: protocol reconstruction, audited 2026-09-22.** Start with
[`PROJECT.md`](PROJECT.md) for context, then:

| Document | What it is |
|---|---|
| [`docs/protocol-ledger.md`](docs/protocol-ledger.md) | **The authoritative protocol record.** Every message, every offset, every unknown, each with its evidence and confidence. Where anything else disagrees, this wins. |
| [`docs/reconstruction-log.md`](docs/reconstruction-log.md) | What was learned when, and what earlier work got wrong. |
| [`docs/evidence-digest.json`](docs/evidence-digest.json) | Protocol facts extracted *mechanically* from SDK bytecode. Not hand-written. |

## What the emulator does today

Over a Bumble virtual link, with no radio and no Plaud hardware, a central can:

* **scan and discover it** — the advertisement carries manufacturer-specific
  data that the SDK's own parse rules accept, including the `portVersion` that
  declares whether the link is encrypted
* exchange MTU, discover services, subscribe to `2BB0` by notify *or* indicate
* run the control protocol: `getState` (3), `syncTime` (4), `getStorage` (6),
  `battStatus` (9), and receive unsolicited battery pushes
* list recordings with multi-frame paging, `resumeRecord`, `deleteFile`
* pull a file: `syncFile` → HEAD → type-2 data frames → EMPTY_PACKAGE sentinel
  → TAIL (the sentinel is what the real SDK completes on, R7-S13), including
  resume-from-offset, abortable paced streaming, and an injectable DATA gap so
  the SDK's own stopSync-and-restart recovery path can be exercised
* the same recording over the **Wi-Fi bulk-transfer** path: the device dials the
  phone's WebSocket server, handshakes, lists, syncs in chunks, deletes, closes;
  sealed sessions for both AEADs (`docs/wifi-transport.md`)

What it deliberately does **not** do: complete a bind. Plaud's cloud issues the
RSA key pair, the SN signature and the handshake token, so no offline bind is
possible. The emulator refuses to advertise `portVersion >= 20`, because above
that the SDK seals every frame with ChaCha20-Poly1305 and a cleartext
peripheral would be lying about what it speaks.

## Evidence, and why the tests are not circular

Most protocol tests compare an emulator against fixtures written from the same
reading of the protocol — which passes just as happily when the reading is
wrong. Three real bugs survived that way here (a big-endian version field, a
fabricated framing byte, a file-list offset off by two).

`scripts/extract_evidence_digest.py` walks the `javap` disassembly of the
shipped AAR and emits `docs/evidence-digest.json` with no human in the path:
per class, the opcode constants, every codec read as (width, literal offset),
the length-guard chain, and the `toString` format literal.
`tests/test_evidence_conformance.py` asserts the emulator against *that*. The
digest is pinned to the AAR's sha256 and is regenerated and diffed whenever the
decompiled tree is present, so a stale digest fails too.

## Validation rungs

| | Claim | Status |
|---|---|---|
| V1 | Emulator advertises; a central discovers it and completes a full session | ✅ |
| V2 | Emulator survives a fault-injection suite without corrupting a transfer | ✅ 61-cell matrix, bytecode-derived receiver model (`docs/v2-fault-matrix.md`) |
| V3 | **Plaud's own SDK connects to the emulator and pulls a recording** | ✅ real AAR on an AVD over `android-netsim`: bind, file list, raw and `exportAudio` pulls byte-exact, gap recovery converges (R7-S13) |
| V4 | Synthetic ground truth round-trips through `pyannote.metrics` at DER 0 | ✅ unit and end-to-end (generator → oracle pipeline → evals: DER/JER/cpWER/tcpWER 0.0) |
| V5 | Full pipeline hits target cpWER / DER on held-out AMI | ◻ procedure and tooling in place; no AMI data or ASR model here |
| V6 | `docker compose up` brings the system live in under 60 s | ◻ written; rehearsed on the venv (live in 0.94 s); Docker not installed here |

No Plaud hardware is required for any rung. V3 runs the real SDK inside an Android
emulator and reaches the peripheral over Bumble's `android-netsim` transport, so it
needs no radio at all (see `r7/r7-s12-k3-runtime-capture.md`).

## The other layers

* `generator/` — synthetic meetings with ground truth by construction, exported in
  the device's own Ogg/Opus shapes (`docs/generator.md`)
* `evals/` — DER, JER, WER, cpWER, tcpWER and CI gates (`docs/evals.md`)
* `pipeline/` — the ASR/diarization stack under test, with an oracle and a
  model-free baseline (`docs/pipeline.md`)
* `mockcloud/` — a FastAPI mock of the partner cloud contract, identity chain
  compatible with the emulator's handshake (`docs/mockcloud.md`)
* `docker/` + `scripts/local-up.sh` — the whole topology, with or without Docker
  (`docs/compose.md`); cross-layer proofs in `docs/integration.md`

## Reading the reconstruction

* [`docs/protocol-ledger.md`](docs/protocol-ledger.md) — the BLE/device protocol (Phase 1, frozen).
* [`docs/final-product-reconstruction.md`](docs/final-product-reconstruction.md) — everything above the radio:
  mobile, cloud, lifecycle, AI, memory, search, web, firmware-as-observable, with
  [`docs/product-ledger.md`](docs/product-ledger.md), [`docs/architecture/`](docs/architecture/),
  [`docs/source-map.md`](docs/source-map.md) and [`docs/evidence-graph.json`](docs/evidence-graph.json) behind it.

## Quick start

```bash
./scripts/fetch-references.sh    # clones the SDK + reference corpus into reference/ (gitignored)
./scripts/build-evidence.sh      # decompiles into build/evidence/ (verifies the AAR sha256)

python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt   # all layers: emulator, generator, evals, pipeline, mock cloud
.venv/bin/python -m pytest tests/ -v
```

The SDK binaries are proprietary under a licence separate from this repo's.
They are fetched on demand and never committed, and `reference/` is treated as
immutable evidence — every derived artifact goes to `build/`.
