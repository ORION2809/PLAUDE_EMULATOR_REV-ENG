# R7 — runtime proof, cloud inventory, closure

| item | what | status |
|---|---|---|
| `android-app/` | Our copy of the Plaud Android template (SDK bundled at `app/libs/plaud-sdk.aar`, same sha256 as the reference copy) plus one debug driver `debug/K3CaptureActivity.kt` that uses only public SDK API with a synthetic identifier. `local.properties` holds a synthetic placeholder token only. | built, `--offline` |
| `k3_capture_peripheral.py` | Frozen `PlaudPeripheral` (portVersion 7) on Bumble `android-netsim`, mirroring every 2BB1 write to JSON. | used for runs 1-4 |
| `r7-s12-k3-runtime-capture.md` | **R7-S12 report**: the official SDK's k3 captured byte-exact; falsifiers; l3 round-trip; `bleBind` facade quirk; opcode-8 discovery. | RUNTIME_PROVEN |
| `r7-s12-k3-capture-*.json`, `r7-s12-logcat-*.log`, `r7-s12-peripheral-*.log` | Raw evidence per run (1, 2-negative, 3-truncate, 4-settings). `k3-capture.json` is the last run's live file. | evidence |
| `pull_capture_peripheral.py` | R7-S13 peripheral: serves `tests/fixtures/r6s2_16k_mono.ogg`, mirrors every 2BB1 write, knobs for EMPTY_PACKAGE code/position, DATA drop offset, TAIL field, abort-on-restart streaming and pacing. | used for runs 1–10 |
| `android-app/.../debug/PullCaptureActivity.kt` | R7-S13 driver: `getFileList()` then `syncFile` (raw collector) and/or `exportAudio(OPUS)`; hashes what the SDK delivers. Public API only, synthetic id. | built, `--offline` |
| `r7-s13-recording-pull.md` | **R7-S13 report**: recording pulled byte-exact through the genuine SDK on both public paths; EMPTY_PACKAGE closes the transfer; stopSync-then-restart on gaps observed live; op-queue pacing; OPUS export passthrough. | RUNTIME_PROVEN |
| `r7-s13-evidence/` | Per-run captures, filtered logcat, peripheral logs, batch scripts, SHA256SUMS, the two corrupted outputs (runs 3, 6). | evidence |
| `cloud-endpoint-inventory.md` | 50 endpoints across five surfaces, static only, none exercised; the cloud↔SDK↔BLE correlation statement. | CLOUD_OBSERVED |

Earlier R7 work already in the tree: `tests/test_r7_feature_exchange.py`
(opcode 138), `tests/test_r7_transport_scope.py` (AES-GCM is Wi-Fi-only),
`tests/test_r7_audio_shapes.py`. R7-S12 adds `tests/test_r7_s12_k3_runtime.py`,
`tests/test_r7_s12_common_settings.py`, `tests/test_r7_s12_audio_selection_pin.py`.
R7-S13 adds `tests/test_r7_s13_transfer_close.py`.

Closure documents: `docs/final-closure-report.md`, `docs/final-architecture.md`,
`docs/final-uncertainty-matrix.json`, `docs/protocol-ledger.md` §5.12/§5.13/§14.
