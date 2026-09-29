# plaud-harness

A hardware-free test harness for Plaud voice recorders (Note Pro / NotePin
class, Bluetooth LE). It rebuilds the device protocol from artifacts Plaud
published, emulates the recorder in software so Plaud's own app SDK will talk
to it, and adds synthetic meetings with exact ground truth, an evaluation
harness and a mock of the partner cloud.

It is research code. No real Plaud device and no Plaud account or credential has
been used, so nothing here proves how real hardware behaves. One correction: in
the R7 runtime runs (R7-S13 and R7-S14 logged it; R7-S12 very likely did too) the
official SDK, initialised with a synthetic token,
sent an automatic key-generation request to Plaud's partner server once per app
start; every response in the logs was a rejection (HTTP 401). The drivers now
pass a blank token. Offline re-runs of all three drivers (25 and 28 September)
sent no request (`r7/r7-s14-wifi-real-sdk.md`, finding D7;
`r7/r7-s14-evidence/blank-token-drivers-check/`;
[progress report §8.6](docs/progress-report.md#86-correction-the-sdk-contacted-plauds-partner-server)).
It is not affiliated with Plaud (see [Notice](#notice)).

**Start here: [`docs/project-state.md`](docs/project-state.md)**, the one document
that says where the project stands: what works, what does not, what is unknown,
the measured numbers, and how each was proven. [`docs/progress-report.md`](docs/progress-report.md)
is the dated account of the work, and [`PROJECT.md`](PROJECT.md) is the long-running plan and history.

## Status (as of 29 September 2026)

The strongest result: on an Android emulator, Plaud's unmodified Android SDK
(`plaud-sdk.aar`) bound to the emulated recorder, listed a served recording
and downloaded it through both public download paths. The bytes it handed the
app hash identically to the served file
([`r7/r7-s13-recording-pull.md`](r7/r7-s13-recording-pull.md)). That run also
showed the project's own protocol model was wrong about how a transfer ends,
which is now corrected.

| Rung | Claim | Status | What it does not show |
|---|---|---|---|
| V1 | Emulator is discovered and completes a session | Done | The central is our own Python code. |
| V2 | Survives fault injection without corrupting a transfer | Done: 62 counted cells plus 2 content-fault rows ([`docs/v2-fault-matrix.md`](docs/v2-fault-matrix.md)) | The receiver is a Python model of the SDK derived from bytecode; six of its behaviours were cross-checked against the real-SDK runs (docs/v2-fault-matrix.md). "No silent corruption" holds by construction in the counted cells, so the matrix tests completion, not content integrity. One cell is a documented corruption risk; payload bit-flips are undetectable by design. |
| V3 | Plaud's own SDK connects and pulls a recording | Done (Android, Bluetooth, legacy protocol) | Nothing about real devices. Not iOS, not the encrypted protocol. Not Wi-Fi: the SDK's Wi-Fi transfer stopped at the hotspot join ([`r7/r7-s14-wifi-real-sdk.md`](r7/r7-s14-wifi-real-sdk.md)). |
| V4 | Synthetic ground truth round-trips at DER 0 | Done | Plumbing and metric direction only. The oracle returns the ground truth by construction. |
| V5 | A full pipeline hits target cpWER/DER on AMI | Measured; **failed** | `whisper-sherpa` (Whisper small.en plus sherpa-onnx) on all 16 AMI test-split meetings (9.06 h), with settings calibrated on the 18 dev meetings and no speaker-count hint: macro DER 0.6472, cpWER 0.8428, against a target of 0.20 and 0.30. Speaker attribution is the main error ([`docs/v5-test-split.md`](docs/v5-test-split.md)). |
| V6 | `docker compose up` is live in under 60 s | Done | Healthy in 6.58 s on an Apple Silicon Mac in a Linux arm64 VM (25 September), and in 5.81 s on a GitHub x86_64 runner from a cold cache (28 September); job exit 0 both times. The job never pulls a recording over Bluetooth ([`docs/compose.md`](docs/compose.md)). |

Full suite on 29 September 2026:
`1420 passed, 5 skipped, 11 warnings in 244.15s (0:04:04)`. The count includes
parametrised cases; the 5 skips are tests that apply only when faster-whisper
or piper is absent, or that need a named real model. On GitHub the `tests` workflow runs on x86_64 Linux, arm64
Linux and macOS; the tests that need the decompiled SDK skip there by design
([`docs/project-state.md`](docs/project-state.md) §5.13).

### What does not work, or is not established

- V5 is not met. On all 16 AMI test meetings the real system scores about
  three times the target error rates, even with its speaker-count setting
  calibrated on the dev split ([`docs/v5-test-split.md`](docs/v5-test-split.md)).
  pyannote-based systems have not run: their models are gated behind a
  Hugging Face account.
- The encrypted protocol used at `portVersion` 20 and above is modelled as
  structure and exercised only with synthetic keys, never with the real SDK.
- The iOS SDK has never been executed; this machine has no Xcode. The real
  SDK's Wi-Fi transfer never reached our Wi-Fi emulator: it stopped at the
  hotspot join, and no Wi-Fi message was exchanged. With a
  `wifi_device_factory`, opening Wi-Fi over Bluetooth (opcode 10) now starts
  the Wi-Fi device emulator, over loopback. The whole hand-over has run only
  against our phone-side test double; in R7-S14, after the emulator's
  mode-0 fix, the real SDK's opcode 10 started the device (runs 2 and 5), but no
  Wi-Fi session followed
  ([`docs/wifi-transport.md`](docs/wifi-transport.md) §8).
- No bind of *our* client to *real* hardware is possible offline: the RSA key
  pair, serial-number signature and handshake token come from Plaud's cloud,
  and this repository holds no credentials. Separately, the emulator refuses
  to advertise `portVersion >= 20`, because at that level the SDK seals every
  frame with ChaCha20-Poly1305 and a cleartext emulator would misstate what it
  speaks.
- 24 protocol questions stay open, most of which need a real device
  ([`docs/final-uncertainty-matrix.json`](docs/final-uncertainty-matrix.json)).
- Most code was written by delegated agents. An independent review of the
  phase-3 layers on 25 September found 71 problems (69 confirmed, 2
  plausible); each was fixed or narrowed. A review of the phase-1 protocol
  code on 28 September found 28; the confirmed defects are fixed. The Android
  runtime rigs were not reviewed line by line
  ([progress report §11](docs/progress-report.md#11-how-the-work-was-checked-and-what-was-not)).

## What the emulator does

Over a Bumble virtual link, with no radio and no Plaud hardware, a central can:

* scan and discover it: the advertisement carries manufacturer data that the
  SDK's own parse rules accept, including the `portVersion` that declares
  whether the link is encrypted
* exchange MTU, discover services, subscribe to `2BB0` by notify or indicate
* run `getState` (3), `syncTime` (4), `getStorage` (6), `battStatus` (9) and
  receive unsolicited battery pushes
* list recordings with multi-frame paging, `resumeRecord`, `deleteFile`
* pull a file: `syncFile` → HEAD → data frames → EMPTY_PACKAGE → TAIL (the
  real SDK completes only on the EMPTY_PACKAGE frame before the TAIL, R7-S13),
  with resume-from-offset, paced and abortable streaming, and an injectable
  gap that exercises the SDK's own stop-and-restart recovery
* the same recording over the Wi-Fi bulk-transfer path, sealed with either
  AEAD, against our own bytecode-derived phone-side test double
  ([`docs/wifi-transport.md`](docs/wifi-transport.md))

On the legacy path the emulator reaches `BOUND` by accepting any handshake
token. That is a labelled harness policy, not a device fact.

## Why the tests are not only circular

Tests that compare an emulator against fixtures written from the same reading
of the protocol pass just as happily when the reading is wrong. Three real bugs
survived that way here. So `scripts/extract_evidence_digest.py` walks the
`javap` disassembly of the shipped AAR and emits
[`docs/evidence-digest.json`](docs/evidence-digest.json) with no human in the
path, and `tests/test_evidence_conformance.py` asserts the emulator against
that. The digest is pinned to the AAR's SHA-256.

That still leaves most tests checking our components against each other or
against bytecode-derived models. Only the R7 runtime runs involve Plaud's real
code, and they cover Android, Bluetooth and the legacy protocol only.

## Layout

| Path | What it is |
|---|---|
| `emulator/plaudsim/` | The emulated recorder: advertising, handshake, file sync, transfer, Bumble GATT profile, Wi-Fi device, fault injection |
| `generator/` | Synthetic meetings with ground truth by construction, exported in the device's Ogg/Opus shapes; optional Piper TTS voices ([`docs/generator.md`](docs/generator.md)) |
| `evals/` | DER, JER, WER, cpWER, tcpWER and gates ([`docs/evals.md`](docs/evals.md)) |
| `pipeline/` | The ASR/diarization stack under test: an oracle, a model-free baseline, `whisper-sherpa` with sha256-pinned weights, guarded model adapters ([`docs/pipeline.md`](docs/pipeline.md)) |
| `mockcloud/` | A FastAPI mock of the partner cloud contract ([`docs/mockcloud.md`](docs/mockcloud.md)) |
| `docker/`, `docker-compose.yml`, `scripts/local-up.sh` | The whole topology, with or without Docker ([`docs/compose.md`](docs/compose.md)) |
| `r4-*` … `r7/` | Milestone spikes and the real-SDK runtime runs, with their logs and captures |
| `reference/` | Fetched source corpus. Git-ignored and treated as immutable evidence |
| `build/` | Everything derived (decompiled SDK, archived docs, generated audio). Git-ignored, except the published V5 and V6 evidence in `build/v5/`, `build/v6/`, `build/v5-test/` and `build/v5-calib/` |

## Quick start

Needs Python 3.11 and git. The first script fetches each repository of the
reference corpus at its pinned commit (about 2.5 GB). The second uses the JDK
17 at `$JAVA_HOME` when set; otherwise it downloads a Temurin JDK 17 for macOS
or Linux (x86_64 or arm64), and it downloads jadx into `/tmp/plaud-tools`
(override with `PLAUD_TOOLS`).

```bash
./scripts/fetch-references.sh    # clones the SDK + reference corpus into reference/ (gitignored)
./scripts/build-evidence.sh      # decompiles into build/evidence/ (verifies the AAR sha256)

python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt   # all layers: emulator, generator, evals, pipeline, mock cloud
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120
bash scripts/local-up.sh         # the whole topology without Docker
./scripts/compose-smoke.sh       # the same under Docker, where a daemon exists
```

`PYTHONDONTWRITEBYTECODE=1` keeps bytecode caches out of `reference/`.
`requirements/models.txt` is optional: it pins faster-whisper, sherpa-onnx and
piper-tts (GPL-3.0-or-later) for `whisper-sherpa` and the Piper voices. CI does
not install it; the weights are fetched separately with
`python -m pipeline fetch-models` ([`docs/pipeline.md`](docs/pipeline.md) §11).

The real-SDK runs additionally need the Android SDK, an API-34 emulator image
with Bluetooth, JDK 17 and Gradle 8.2. The runtime scripts under `r4-s2/`,
`r4-s3/` and `r7/` find the repository from their own location; `r4-s2/run.sh`
reads `ANDROID_HOME`, `JAVA_HOME`, `ADB`, `EMU`, `AVD` and `PY` from the
environment. The runbooks
([`r7/r7-s12-k3-runtime-capture.md`](r7/r7-s12-k3-runtime-capture.md),
[`r7/r7-s13-recording-pull.md`](r7/r7-s13-recording-pull.md)) record the
toolchain of the machine they ran on; substitute your own paths.

## Documentation

| Document | What it is |
|---|---|
| [`docs/progress-report.md`](docs/progress-report.md) | **Where things stand**: rungs, what was wrong, how it was checked, open items |
| [`PROJECT.md`](PROJECT.md) | Goals, legal posture, decision log, status (§8) and next steps |
| [`docs/protocol-ledger.md`](docs/protocol-ledger.md) | The authoritative protocol record: every message and offset, with evidence and confidence |
| [`docs/reconstruction-log.md`](docs/reconstruction-log.md) | What was learned when, and what earlier work got wrong |
| [`docs/final-product-reconstruction.md`](docs/final-product-reconstruction.md) | The product above the radio: mobile, cloud, lifecycle, AI, web ([`docs/product-ledger.md`](docs/product-ledger.md), [`docs/architecture/`](docs/architecture/), [`docs/source-map.md`](docs/source-map.md)) |
| [`r7/r7-s13-recording-pull.md`](r7/r7-s13-recording-pull.md) | The real SDK pulling a recording, and what it corrected |
| [`r7/r7-s14-wifi-real-sdk.md`](r7/r7-s14-wifi-real-sdk.md) | The real SDK's Wi-Fi transfer, blocked at the hotspot join; the cloud-contact finding (D7) |
| [`docs/v5-test-split.md`](docs/v5-test-split.md) | V5 on the full AMI test split: the dev-split calibration, 16 meetings, and why the target is missed |
| [`docs/v5-results.md`](docs/v5-results.md) | The first real-model numbers (4 AMI test meetings, 4 Piper meetings, 25 September) |
| [`docs/integration.md`](docs/integration.md) | Cross-layer proofs |

The project convention is that device, SDK and cloud claims cite their
evidence (bytecode, a file and line in `reference/`, Plaud's public docs, or a
runtime log), and that harness choices are labelled `HARNESS_POLICY` rather than
presented as device facts.

## Notice

This project is independent. It is not affiliated with, endorsed by or
supported by Plaud; the name is used only to identify what the harness
interoperates with.

The Plaud SDK binaries are proprietary, under a licence separate from this
repository's. They are **not included here**. `scripts/fetch-references.sh`
fetches them into the git-ignored `reference/` directory for local
interoperability analysis only; do not redistribute them. Every repository
fetched into `reference/` keeps its own licence (see
[`docs/SOURCES.md`](docs/SOURCES.md)).

Apart from the standard Gradle wrapper files (Apache-2.0) in the two Android
projects, one directory is third-party code. `r7/android-app/` is a modified
copy of the Android template app in Plaud's `plaud-sdk-public` repository,
which is Apache-2.0 licensed apart from its `sdk/` binaries. The copy adds the
`debug/` drivers and changes the manifest. It stays under Apache-2.0, and the
SDK archive it builds against is not committed (`*.aar` is git-ignored).

No Plaud credential or device was used in this work, and every identifier and
token in the repository is synthetic. We never called a Plaud endpoint
ourselves; the SDK's automatic `gen-key` requests during the runtime runs are
described above and have been stopped.

## Licence

Except for `r7/android-app/` (Apache-2.0, see [Notice](#notice)), the code
and documents in this repository are licensed under the GNU Affero General
Public License, version 3. See [`LICENSE`](LICENSE).
