# plaud-harness

A hardware-free harness for reverse-engineering the Plaud Note Pro / NotePin BLE recorder.

Reconstruct the device protocol from shipped artifacts, emulate the device in software, and
prove the reconstruction is correct by making Plaud's own SDK talk to the emulator.

**Status: phase 1, protocol reconstruction.** See [`PROJECT.md`](PROJECT.md) for the full
context, findings, open questions and decision log — it is the source of truth for this repo.

## Validation ladder

| | Claim | Status |
|---|---|---|
| R1 | Emulator advertises; a Bumble central completes a full session | ⬜ |
| R2 | Emulator survives the fault-injection suite without corrupting a transfer | ⬜ |
| R3 | **Plaud's own SDK connects to the emulator and pulls a recording** | ⬜ |
| R4 | Synthetic ground truth round-trips through `pyannote.metrics` at DER 0 | ⬜ |
| R5 | Full pipeline hits target cpWER / DER on held-out AMI | ⬜ |
| R6 | `docker compose up` brings the system live in under 60 s | ⬜ |

No Plaud hardware is required for any rung. R3 needs a BLE radio at the far end — a generic
USB dongle or a laptop's built-in Bluetooth — but not a Plaud device.

## Quick start

```bash
./scripts/fetch-sdk.sh
```

The SDK binaries are proprietary under a licence separate from this repo's. They are fetched
on demand and never committed.
