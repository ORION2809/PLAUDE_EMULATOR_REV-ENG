# Project state

**As of 30 September 2026.** Branch `main`, published at
`github.com/ORION2809/PLAUDE_EMULATOR_REV-ENG` (public). This document is
committed together with the 25–30 September work it describes. The `tests`
workflow failed on `70249ba`, the first pushed commit that carries it, and first passed on
`5a3d2ff` (run 36386441732, 28 Sep, x86_64 Linux). From `d566d43` it runs on
x86_64 Linux, arm64 Linux and macOS. The first runs there exposed portability
faults in four tests (two in the first run, two in the next), which were
fixed. Since `c0be32e`, every
pushed commit has passed on all three platforms except `3e46ad9`, whose arm64
job failed while installing dependencies ([§5.13](#513-ci)).

**Summary.** plaud-harness is a hardware-free test harness for Plaud's
Bluetooth LE voice recorders. It rebuilds the recorder's protocol from Plaud's
published Android SDK, emulates the recorder in Python, generates synthetic
meetings with exact ground truth, scores speech pipelines, and mocks the
partner cloud. The strongest result: Plaud's unmodified Android SDK, running on
an Android emulator, bound to the emulated recorder and pulled a recording whose
bytes hash identically to the served file. Most other checks compare our own
components with each other or with models we derived from the SDK's bytecode.
Real ASR and diarization were measured on all 16 AMI test meetings (9.06 h).
The V5 target (DER ≤ 0.20, cpWER ≤ 0.30) is missed, narrowly. The best
system, `faster-whisper+pyannote` (29 Sep), scores cpWER 0.3477 without a
speaker-count hint, and pyannote's own turns score DER 0.1295. That compares
with 0.8428 and 0.6472 for calibrated `whisper-sherpa` on 28 Sep.
pyannote's segmentation model was trained on data that includes AMI, so these numbers may be optimistic. On 29 Sep the
SDK's Wi-Fi transfer also completed byte-exact, after the emulated phone was
given a network named `PLAUD0001` to join (R7-S15). No real Plaud device, account,
credential or iOS runtime has been used. The official SDK did send automatic
`gen-key` requests to Plaud's server during the runtime runs, because our
drivers gave it a synthetic token. All three drivers now pass a blank token,
and each has been re-run offline without a request
([§8](#8-rules-and-how-they-held)).

**Verdict in plain words:** on an Android emulator, Plaud's real Android SDK
bound to our Bluetooth emulator and pulled one 11 271-byte synthetic file
byte-exact, on the legacy (unencrypted) protocol. It never used getStorage,
resume or delete against the emulator. Its Wi-Fi transfer also completed
byte-exact, on a simulated `PLAUD0001` network, though the bytes reached the
phone through `adb forward`, not the simulated radio. The speech side runs end to end on nine hours of real meetings,
with speaker attribution nearly solved by pyannote and cpWER 0.05 above the
target; the rest is the transcript. Nothing here shows how real
hardware behaves.

This document is meant to replace reading README, PROJECT.md, the progress
report and the layer documents together. Those stay as history and detail, and
are linked below. `README.md` and `PROJECT.md` point here. Numbers were taken
as follows:

| Measured or re-measured for this document on 28–30 Sep | Read from saved outputs (not re-run) |
|---|---|
| Full test suite and per-file counts; the 12 protocol verifiers; 33 reference pins and the AAR hash; the fault-matrix JSON (rewritten by the suite run); the R7-S13 evidence checksums; the `gen-key` counts in the runtime logs; V5 on the full AMI test split, its dev-split calibration and the md-eval cross-check (`build/v5-test/`, `build/v5-calib/`); the GoReplay replay of the mock cloud's export; the blank-token re-runs of the K3 and Wi-Fi drivers; the GitHub Actions status (public API); the `git status` counts and what `.gitignore` excludes | The 25 Sep V5 subset and the Piper numbers (`build/v5/`); the V6 Docker log (25 Sep); the GitHub V6 timings (check-run notices, 28 Sep); the Android runtime logs (23–25 Sep); review and verifier records (25 and 28 Sep journals) |

## Contents

1. [What the project is, and is not](#1-what-the-project-is-and-is-not)
2. [State at a glance](#2-state-at-a-glance)
3. [Validation rungs V1–V6](#3-validation-rungs-v1v6)
4. [Measured numbers](#4-measured-numbers)
5. [Components](#5-components)
6. [What is wrong, unknown or unresolved](#6-what-is-wrong-unknown-or-unresolved)
7. [Corrections that changed the picture](#7-corrections-that-changed-the-picture)
8. [Rules, and how they held](#8-rules-and-how-they-held)
9. [How to reproduce](#9-how-to-reproduce)
10. [Where things are](#10-where-things-are)
11. [What is next](#11-what-is-next)
12. [How to keep this document true](#12-how-to-keep-this-document-true)

---

## 1. What the project is, and is not

The goal, from [`PROJECT.md`](../PROJECT.md) §0–§1: reconstruct the recorder's
Bluetooth protocol from shipped artifacts, emulate the device in software, and
prove the reconstruction by making Plaud's own SDK talk to the emulator. It is
a personal portfolio project. It is not affiliated with Plaud.

| Part | Where | What it is |
|---|---|---|
| Layer 1: device emulator | `emulator/plaudsim/`, `emulator/serve.py` | The recorder's BLE side on a Bumble virtual link (no radio); the Wi-Fi device side; a fault-injecting variant |
| Layer 2: synthetic meetings | `generator/` | Meetings with ground truth by construction, exported in the recorder's Ogg/Opus shapes |
| Layer 3: evaluation | `evals/` | DER, JER, WER, cpWER, tcpWER; pass/fail gates; the AMI converter |
| System under test | `pipeline/` | Self-test oracles, a model-free diarizer, one real ASR+diarization stack, adapters for others |
| Layer 4: mock cloud | `mockcloud/` | A FastAPI mock of Plaud's partner-cloud contract |
| Runtime rig | `r7/`; earlier `r4-s3/` and `r5-s1/`; transport spike `r4-s2/` | `r7/android-app/`: a copy of Plaud's Android template app with three debug drivers, run on an API-34 Android emulator, with our Python peripheral attached through Android's netsim Bluetooth simulator. `r4-s3/` and `r5-s1/` ran the stock template APK with no token. `r4-s2/` is a ping/pong transport test with its own minimal app and no Plaud code |
| Topology | `docker/`, `docker-compose.yml`, `scripts/local-up.sh` | Emulator + mock cloud + a one-shot job, with or without Docker |

**What "emulator" means here.** It is our implementation of what Plaud's client
requires of a device. It is not a copy of firmware; no firmware was examined.
Where the SDK does not constrain a device-side value, the emulator's choice is
labelled `HARNESS_POLICY` ([`docs/protocol-ledger.md`](protocol-ledger.md) §12).

**What it cannot prove without hardware.** Which advertisement layout real
devices send. Real timing and frame sizes. Which end-of-transfer code real
firmware sends, and whether a TAIL follows it. The storage units. Which audio
container real firmware writes. Which capability bits and settings real
firmware reports. Everything on the device side of the encrypted protocol.
Whether a client we wrote could bind to a real device (that also needs
cloud-issued credentials).

**Evidence classes used in this document.** They are never merged.

| Class | Meaning |
|---|---|
| RUNTIME_PROVEN | Observed while running Plaud's real, unmodified SDK (Android only) |
| BYTECODE_PROVEN | Read from `javap` output of the SDK archive (`plaud-sdk.aar`, sha256 `041a6f88…aedce`) |
| OFFICIAL_DOC | Plaud's published documentation or template-app source |
| CLOUD_OBSERVED | Seen in third-party clients of Plaud's cloud; never verified by us |
| EMULATOR_INTEGRATION | Our components exercised against each other |
| HARNESS_POLICY | A choice we made; not a claim about Plaud |
| UNKNOWN | No evidence |
| MEASURED | A number from running our own code on data. It describes our system, not Plaud. (Added for this document.) |

## 2. State at a glance

Status words: **Works** = exists and passes its checks. **Partial** = exists,
with a named gap. **Blocked** = attempted, stopped by something outside the
code. **Not done** = never attempted or never run.

| Component | Status | Strongest evidence | Proven by | Main limitation |
|---|---|---|---|---|
| BLE protocol, legacy message set | Works | BYTECODE_PROVEN. RUNTIME_PROVEN for the requests the real SDK sent: opcodes 1, 3, 4, 8, 9, 26 (file list), 28 (syncFile) and 29 (stopSync) in R7-S12/S13, and 10 and 13 in R7-S14 | [`docs/protocol-ledger.md`](protocol-ledger.md), [`docs/evidence-digest.json`](evidence-digest.json), `tests/test_evidence_conformance.py`; opcode tallies of `r7/r7-s12-k3-capture-*.json` and `r7/r7-s1[34]-evidence/capture-*.json` | 24 open questions; 15 need a device ([§6.1](#61-open-questions-24)). The real SDK never sent getStorage (6), resumeRecord (22) or deleteFile (30), and never saw a file list longer than one entry |
| BLE device emulator | Works | RUNTIME_PROVEN (the real SDK used it) | [`r7/r7-s13-recording-pull.md`](../r7/r7-s13-recording-pull.md); the phase-1 code review and its fixes (28 Sep, `tests/test_review_phase1.py`) | Device-side values the SDK does not fix are HARNESS_POLICY |
| Legacy handshake (portVersion < 20) | Works | RUNTIME_PROVEN | [`r7/r7-s12-k3-runtime-capture.md`](../r7/r7-s12-k3-runtime-capture.md) | The emulator accepts any token (HARNESS_POLICY) |
| Encrypted path (portVersion ≥ 20) | Partial | BYTECODE_PROVEN layouts; EMULATOR_INTEGRATION with synthetic keys | `r5-s5/` … `r5-s11/` verifiers; `tests/test_r5_s*.py` | Never run with the real SDK; keys are cloud-issued (CRED-1) |
| BLE file transfer | Works | RUNTIME_PROVEN | [`r7/r7-s13-evidence/DELIVERED-OUTPUTS.sha256.txt`](../r7/r7-s13-evidence/DELIVERED-OUTPUTS.sha256.txt) | Real firmware's end-of-transfer behaviour unknown (U15, U17) |
| Wi-Fi transfer emulator | Partial | EMULATOR_INTEGRATION against a bytecode-derived phone double; RUNTIME_PROVEN for one session with the real SDK on an AVD (R7-S15) | [`docs/wifi-transport.md`](wifi-transport.md) §12, `tests/test_wifi_*.py`, [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) | The real SDK completed one unencrypted session with it on 29 Sep, through `adb forward`, not a radio link; never run with a real phone or pen |
| Real SDK: bind | Works (Android) | RUNTIME_PROVEN | [`r7/r7-s12-k3-runtime-capture.md`](../r7/r7-s12-k3-runtime-capture.md) | Android emulator only; legacy protocol only |
| Real SDK: file pull | Works (Android, BLE) | RUNTIME_PROVEN | [`r7/r7-s13-recording-pull.md`](../r7/r7-s13-recording-pull.md) | One synthetic 11 271-byte file; our emulator as the device |
| Real SDK: Wi-Fi transfer | Works (Android emulator, 29 Sep) | RUNTIME_PROVEN | [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md); [`r7/r7-s14-wifi-real-sdk.md`](../r7/r7-s14-wifi-real-sdk.md) | One run. The phone joined a simulated `PLAUD0001`, but the WebSocket bytes went through `adb forward`, not the simulated radio. The handshake token was empty (blank SDK token), which our emulator accepts. No encryption |
| Fault-injection matrix (V2) | Works (completion only) | EMULATOR_INTEGRATION, scored by a bytecode-derived receiver model | [`docs/v2-fault-matrix.md`](v2-fault-matrix.md), `build/v2-fault-matrix.json` (git-ignored) | Tests completion, not content integrity |
| Product reconstruction | Partial: done as documents, with external-evidence blockers (the phase-2 verdict) | OFFICIAL_DOC, BYTECODE_PROVEN, CLOUD_OBSERVED | [`docs/final-product-reconstruction.md`](final-product-reconstruction.md) | The consumer cloud is seen only through third-party clients |
| Generator, formant voice | Works | EMULATOR_INTEGRATION | [`docs/generator.md`](generator.md) | Not intelligible speech |
| Generator, Piper voice | Works (locally) | EMULATOR_INTEGRATION | `generator/tts/piper.py`, `tests/test_generator_piper.py`; `meeting.json` carries the voice hashes, timing method and attribution | Not in CI; voice licence unresolved |
| Evals | Works | EMULATOR_INTEGRATION (V4); MEASURED agreement with NIST md-eval (224 of 224 per-meeting DERs at collar 0) | [`docs/evals.md`](evals.md), `tests/test_evals_*.py`, `build/v5-test/md-eval-crosscheck.json` | Above 20 speakers meeteval refuses cpWER/tcpWER; the meeting is kept with DER, JER and WER, and a gate on the refused metric fails |
| Pipeline baselines | Works | EMULATOR_INTEGRATION | [`docs/pipeline.md`](pipeline.md) | `energy-vad-cluster` is tuned to synthetic voices |
| Real ASR + diarization (`whisper-sherpa`, `whisper-sherpa-ecapa`, `faster-whisper+pyannote`) | Partial: measured on all 16 AMI test meetings; target missed | MEASURED | [`docs/v5-test-split.md`](v5-test-split.md), [`docs/v5-results.md`](v5-results.md) | Best: `faster-whisper+pyannote`, cpWER 0.3477 without a hint (target 0.30); pyannote's turns alone DER 0.1295 (target 0.20). The transcript's WER (0.3221) now dominates. pyannote's segmentation model was trained on data that includes AMI |
| Other model adapters | Partial | MEASURED for `embedding-cluster` (SpeechBrain ECAPA), `pyannote-audio` and `faster-whisper+pyannote` (pyannote.audio 4.0.7, gated community-1 pipeline), in separate environments | [`docs/v5-test-split.md`](v5-test-split.md) §7, §7.2; `python -m pipeline list` | `whisperx` never ran against a model (not installed) |
| AMI converter | Works | MEASURED (cross-check against BUT RTTMs) | `evals/ami.py`, `tests/test_ami_converter.py` | Audio here for all 16 test and 18 dev meetings, headset mix only |
| Mock cloud | Works (as a contract mock) | EMULATOR_INTEGRATION; contract from OFFICIAL_DOC (OpenAPI pages, template-app source) and BYTECODE_PROVEN (SDK-internal routes). GoReplay replays its traffic export exactly (14 of 14 requests) | [`docs/mockcloud.md`](mockcloud.md), `scripts/crosscheck-goreplay.sh` | Never compared with real responses |
| Compose / V6 | Works (arm64 on a Mac; x86_64 on GitHub) | EMULATOR_INTEGRATION | `build/v6/compose-smoke-2026-09-25.log` (committed evidence); the `compose` workflow (first run 36396859985); [`docs/compose.md`](compose.md) | The job never pulls a recording over BLE, and no container runs the Android SDK |
| CI on GitHub | Works: `tests` on x86_64 Linux, arm64 Linux and macOS; `compose` (V6) and `evals` | – | [§5.13](#513-ci): runs from 36093783853 (the first failure, `70249ba`) to 36418829234 (`92146bf`, green on all three) | The decompiled-SDK tests skip in CI by design (§4.1). Job logs need a token; failures are read through check-run annotations |
| iOS SDK | Not done | – | – | No Xcode on this machine |
| Real hardware | Not done | – | – | None used |

## 3. Validation rungs V1–V6

The rungs are defined in [`PROJECT.md`](../PROJECT.md) §4b.

| Rung | Claim | Status | What it shows | What it does not show | Proven by |
|---|---|---|---|---|---|
| V1 | Emulator is discovered and completes a session | Done | A Bumble central scans, parses the advertisement with the SDK's parse rules, connects, sets MTU, discovers, subscribes and runs the control and file-sync messages | The central is our Python code; V1 passed while the transfer sequence was still wrong | `tests/test_v1_discovery_session.py` and the phase-1 tests |
| V2 | Survives fault injection without corrupting a transfer | Done | 61 of the 62 counted cells end byte-exact (39 directly, 6 after an app resume) or with a detected failure (16); the 62nd, `old_stream_not_abandoned[atomic]`, is a documented corruption risk. 2 content-fault rows show the harness's corruption check fires | The receiver is our model of the SDK. "No silent corruption" holds by construction in the counted cells (review finding T10) | `tests/test_v2_fault_matrix.py`, [§4.7](#47-fault-injection-matrix-v2) |
| V3 | Plaud's own SDK connects and pulls a recording | Done (Android, BLE, legacy protocol) | Bind, file list, and byte-exact download through `syncFile` and `exportAudio(OPUS)`; gap recovery converged byte-exact | Real devices, iOS, the encrypted protocol. Wi-Fi was done on 29 Sep, with caveats: the phone joined a simulated network, and the bytes went through `adb forward` ([§5.4](#54-real-sdk-runtime-runs-android-only)) | [`r7/r7-s13-recording-pull.md`](../r7/r7-s13-recording-pull.md), [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) |
| V4 | Synthetic ground truth round-trips at DER 0 | Done | Reference vs itself and vs a mask-rebuilt copy: DER = JER = 0.0; generator → oracle → evals gives DER, JER, cpWER, tcpWER all 0.0; perturbations raise each metric | Plumbing and metric direction only; the oracle returns the truth by construction | `tests/test_v4_der_roundtrip.py`, `tests/test_v4_e2e.py` |
| V5 | A full pipeline hits target cpWER/DER on held-out AMI | Measured; **failed, narrowly** | All 16 AMI test meetings (9.06 h), no hint. `whisper-sherpa`, dev-calibrated: macro DER 0.6472, cpWER 0.8428 (28 Sep). `whisper-sherpa-ecapa`: 0.4104, 0.5132. `faster-whisper+pyannote`: DER 0.3140 (word runs), cpWER 0.3477. `pyannote-audio` (no words): DER 0.1295 (29 Sep). Suite `ami-headset` (DER ≤ 0.20, cpWER ≤ 0.30, HARNESS_POLICY bounds) fails for every system with words | cpWER ≤ 0.30; generalisation beyond AMI (pyannote's segmentation model was trained on data that includes it) | [`docs/v5-test-split.md`](v5-test-split.md), `build/v5-test/gates/`, `evals/gates.yaml` |
| V6 | `docker compose up` is live in under 60 s | Done (arm64 on a Mac; x86_64 on a GitHub runner) | Healthy in 6.58 s (Colima, 25 Sep) and 5.81 s (GitHub, cold cache, 28 Sep); job exit 0 both times | A BLE pull inside the topology; the real SDK | `build/v6/compose-smoke-2026-09-25.log`; `compose` run 36396859985 |

## 4. Measured numbers

### 4.1 Tests

Full suite, run on 30 September 2026:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120 -rs
1432 passed, 5 skipped, 11 warnings in 271.52s (0:04:31)
```

- Counts include parametrised cases.
- All 5 skips fall under the documented `optional-engine-installed` gate
  (`tests/conftest.py`).
- Three of them test the branch for an absent faster-whisper or piper. Both
  packages are installed here.
- The other two, `test_real_faster_whisper_on_a_generator_clip` and
  `test_real_faster_whisper_seeded_sampling_repeats_across_processes`
  (`tests/test_pipeline_adapters.py:520`, `:553`), run the faster-whisper
  adapter against a real model. They run only when `PLAUD_HARNESS_FW_MODEL`
  names a model. With it set to the pinned `small.en` they passed on 29 Sep
  (2 passed in 67.01 s; [§6.4](#64-never-run)).
- The 11 warnings are pyannote.metrics' "uem was approximated" notices.
- The suite rewrites `docs/evidence-digest.json` in place (same bytes; only the
  file's modification time changes) and the git-ignored
  `build/v2-fault-matrix.json`. Both are expected.
- Pytest must be pointed at `tests/`. Run from the repository root without a
  path, it collects `reference/` and stops with an error.

Per area, from `pytest tests/ --collect-only` on 30 Sep. 74 of the 75
`test_*.py` modules yield tests; `test_mockcloud_helpers.py` holds shared
helpers only. `tests/` holds 81 Python files, including `conftest.py` and 5
support modules:

| Area | Files | Tests |
|---|---|---:|
| Phase-1 protocol and emulator | `test_r1*`–`test_r7_*` (except `test_r7_s13_*`), `test_evidence_conformance.py`, `test_v1_discovery_session.py`, `test_review_phase1.py` (21) | 325 |
| End-of-transfer frame and stream abort (R7-S13) | `test_r7_s13_transfer_close.py` | 20 |
| V2 fault matrix and receiver model | `test_v2_*.py` | 118 |
| Wi-Fi transfer | `test_wifi_*.py` (75 of them pin constants to bytecode) | 156 |
| Generator and V4 round trip | `test_generator_*.py`, `test_v4_der_roundtrip.py` | 137 |
| Evals | `test_evals_*.py` | 200 |
| AMI converter | `test_ami_converter.py` | 51 |
| V5 gate calibration | `test_v5_gates.py` | 19 |
| Pipeline | `test_pipeline_*.py` | 255 |
| Mock cloud | `test_mockcloud_*.py` | 76 |
| Cross-layer integration and V4 end to end | `test_integration_*.py`, `test_v4_e2e.py` | 21 |
| Compose | `test_compose_*.py` | 59 |
| **Total collected** | | **1437** |

History: 301 tests after phase 1; 978 on 24 Sep; 1365 passed on 25 Sep; 1377
on 28 Sep before the day's changes (12 added for R-CI-1); 1420 on 29 Sep; 1432 on 30 Sep.

Before GitHub ran the workflow, two CI-like runs were made on macOS arm64 on
25 Sep (from the follow-up and stage-2 journals):

1. Follow-up simulation. The workflow steps were replayed on a clean clone,
   with a fresh install from `requirements/ci.txt`. Bumble was fetched from the
   local `reference/upstream/bumble`, not from GitHub. Result:
   `1156 passed, 93 skipped, 11 warnings in 177.13s`, every skip a documented
   gate.
2. Stage-2 verifier. A clean clone with the local `.venv` and Bumble checkout
   linked in, `PLAUD_STRICT_SKIPS=1`, and the optional model packages hidden.
   Result: `1253 passed, 116 skipped, 11 warnings in 160.26s`, every skip a
   documented gate.

Since 28 Sep the workflow runs on GitHub, fetching Bumble from GitHub at its pin
([§5.13](#513-ci)). In CI the decompiled SDK is absent, so those tests skip by
design.

### 4.2 Protocol verifiers

Twelve standalone verifier scripts. On 29 Sep all 12 printed
`ALL CHECKS PASSED` with exit 0 (last run 09:53 IST with the loop in §9;
r5-s7 and r5-s8 print `(0 skipped)`).

| Group | Scripts | Reads `build/evidence/` (not in git) |
|---|---|---|
| Re-derive protocol facts from the decompiled SDK: `javap` text, plus jadx (r5-s5) or the AAR's native library (r6-s1) | `r5-s2/verify_first_handshake.py`, `r5-s3/verify_j3.py`, `r5-s4/verify_l3.py`, `r5-s5/verify_modern.py`, `r5-s6/verify_modern_transport.py`, `r5-s10/verify_fe11_timing.py`, `r6-s1/verify_audio_contracts.py` | Yes |
| Exercise the emulator's sealed path over Bumble (r5-s7, r5-s8, r5-s9), the R5-S11 matrix and traces (r5-s11), and the R6-S2 audio fixtures (r6-s2) | `r5-s7/verify_modern_sealed_roundtrip.py`, `r5-s8/verify_sealed_filesync.py`, `r5-s9/verify_modern_integration.py`, `r5-s11/verify_uncertainty_trace.py`, `r6-s2/verify_audio_validation.py` | No |

### 4.3 Reference corpus

- 33 of 33 repositories under `reference/` are at the commit pinned in
  [`docs/reference-pins.txt`](reference-pins.txt), with no modified or
  untracked files. Checked on 28 Sep before and after the test run, and on
  29 Sep.
- The SDK archive `reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar`
  hashes to `041a6f8814d350dbc8bcd4a515487136529bb8bb4368fc88c9b36c9a386aedce`,
  the value `scripts/build-evidence.sh` and `docs/evidence-digest.json` pin.

### 4.4 V5: the full AMI test split (28 September) and the 25 September subset

**Full test split.** Source: `build/v5-test/summary.md` and
`build/v5-test/reports/`, written up in
[`docs/v5-test-split.md`](v5-test-split.md). The split has 16 meetings (9.06 h,
90 149 reference words, headset mix). Numbers are macro means. "Hint" means the
reference speaker count was passed to the diarizer, an oracle value a recorder
does not have. "Calibrated" means `cluster_threshold=1.15` and
`assignment_tie_break=latest_start` for whisper-sherpa, and
`distance_threshold=0.55` for embedding-cluster, all chosen on the 18 AMI dev
meetings. Nothing was tuned on the test split.

| System | DER | JER | cpWER | tcpWER | WER (speaker-agnostic) | Mean speaker-count error |
|---|---:|---:|---:|---:|---:|---:|
| whisper-sherpa, calibrated, no hint | 0.6472 | 0.7508 | 0.8428 | 0.9628 | 0.3221 | −0.31 |
| whisper-sherpa, hint | 0.6396 | 0.7473 | 0.8567 | 0.9808 | 0.3221 | 0.00 |
| whisper-sherpa, defaults, no hint | 0.7632 | 0.7600 | not scored (meeteval refused 16 of 16) | not scored | 0.3221 | +51.25 |
| sherpa's own turns, calibrated (words not used) | 0.5263 | 0.6949 | n/a | n/a | n/a | −0.31 |
| sherpa's own turns, hint (words not used) | 0.5192 | 0.6779 | n/a | n/a | n/a | 0.00 |
| energy-vad-cluster, no hint (no ASR) | 0.5871 | 0.6842 | n/a | n/a | n/a | +0.94 |
| embedding-cluster, calibrated, no hint (no ASR) | 0.4967 | 0.6506 | n/a | n/a | n/a | −1.50 |
| embedding-cluster, hint (no ASR) | 0.3708 | 0.4548 | n/a | n/a | n/a | 0.00 |
| **whisper-sherpa-ecapa, no hint** (29 Sep) | **0.4104** | 0.5391 | **0.5132** | 0.5297 | 0.3221 | −0.31 |
| whisper-sherpa-ecapa, hint (29 Sep) | 0.3965 | 0.4968 | 0.4821 | 0.4994 | 0.3221 | 0.00 |
| **faster-whisper+pyannote, no hint** (29 Sep) | 0.3140 | 0.3358 | **0.3477** | 0.3562 | 0.3221 | +0.25 |
| **pyannote-audio, no hint (no ASR)** (29 Sep) | **0.1295** | 0.1773 | n/a | n/a | n/a | +0.25 |
| oracle (self-test) | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 |

- `ami-headset` (DER ≤ 0.20, cpWER ≤ 0.30) fails for the calibrated and the
  hinted system (`build/v5-test/gates/`).
- **Calibration** (`scripts/calibrate-sherpa.sh`, `build/v5-calib/`,
  [`docs/pipeline.md`](pipeline.md) §11.8). The threshold 1.15 gave sherpa's
  own turns the lowest dev macro DER without a hint (0.5431) over 25 values
  from 0.800 to 1.400.
  The sweep replays sherpa-onnx from cached segmentation and embeddings
  (`pipeline/sherpa_sweep.py`). The replay is bit-identical to real sherpa-onnx
  at every threshold checked (five on IS1008a; 1.15 on IS1008a and TS3004a).
  The tie-break `latest_start` gave the dev reference words a macro cpWER of
  0.9344, against 1.0075 for `floor`.
- **Calibration fixes the speaker count, not the attribution.** With the
  defaults, sherpa finds 35–108 clusters per meeting (31–95 of them receive
  words). Calibrated, it finds 2–4:
  10 meetings get the right count, 5 too few and 1 too many. DER is then within
  0.008 of the hinted run.
- **The tie-break is worth about 0.03 cpWER on test**, less than half the dev
  proxy's 0.07. Re-assigning the same
  words and turns, `latest_start` lowers cpWER from 0.8697 to 0.8428 on the
  calibrated turns and from 0.8567 to 0.8279 on the hinted ones.
- **Diarization dominates.** Speaker confusion is 0.34 of the reference time
  for the word runs and 0.42 for sherpa's own turns. Word runs also miss 0.26
  of the speech, against 0.07 for the turns. This is why sherpa's own turns
  score a lower DER than the words attributed to them.
- **ASR is repeatable.** Decoding is seeded, and one transcript per meeting is
  shared through a cache. All three whisper-sherpa systems score the same
  68 632 words. 79 of 9 221 segments used faster-whisper's sampled fallback. A
  fresh process reproduced EN2002a's 5 120 words exactly.
- **Pretrained speaker embeddings attribute better.** `embedding-cluster`
  (the harness's VAD and cells with SpeechBrain ECAPA embeddings; no ASR)
  scores DER 0.3708 with the hint, against 0.5192 for sherpa's own hinted
  turns, lower on 15 of 16 meetings. Its distance threshold was calibrated on
  the dev split (0.55). Without a hint it collapses to one speaker on 8 of 16
  meetings; its macro DER, 0.4967, mixes those with meetings where it finds 4–5
  speakers and scores 0.16–0.40 ([`docs/v5-test-split.md`](v5-test-split.md) §7).
- **`whisper-sherpa-ecapa` (29 Sep).** The whisper-sherpa words, assigned to
  ECAPA turns cut into the number of speakers calibrated sherpa-onnx finds.
  Chosen on the dev split first: there it scored DER 0.4314 and a
  reference-word cpWER of 0.6145, against 0.5431 and 0.9344 for sherpa alone
  (`scripts/check-sherpa-ecapa-dev.py`). On test: DER 0.4104 and cpWER 0.5132
  without a hint. cpWER improved on all 16 meetings, and speaker confusion fell
  from 0.342 to 0.107. All 32 real runs, with and without the hint, equal the
  replay. All 18 fresh ASR decodes (the 16 no-hint runs, and two hint runs of a
  first, stopped attempt) reproduced the cached transcripts word for word
  ([`docs/v5-test-split.md`](v5-test-split.md) §7.1).
- **pyannote (29 Sep).** `pyannote-audio` (pyannote.audio 4.0.7, gated
  `speaker-diarization-community-1`, library defaults, no hint) scores DER
  0.1295, and finds the right speaker count on 12 of 16 meetings.
  `faster-whisper+pyannote` puts the same transcript on pyannote's turns:
  cpWER 0.3477, speaker confusion 0.012, and cpWER ≤ 0.30 on 5 of 16 meetings.
  At collar 0 the pooled DER is 17.07 %, against 17.0 % on the model card for
  AMI (IHM): an outside check of the harness's scoring. The card does not list
  its training data; its segmentation model is segmentation-3.0, which was trained
  on data that includes AMI ([`docs/v5-test-split.md`](v5-test-split.md) §7.2).
- **Checks.** The oracle scores 0 everywhere. NIST md-eval gives the same DER
  for all 224 per-meeting values at collar 0. At collar 0.25, 213 of 224 agree
  and 11 differ by at most 0.0054.
- **Regression gates.** `ami-test-whisper-sherpa-cal`,
  `ami-test-whisper-sherpa-hint`, `ami-test-whisper-sherpa-ecapa`,
  `ami-test-whisper-pyannote` and `ami-test-pyannote-audio` are the measured
  macro values plus 0.03 (0.25 on the speaker-count error), rounded up. Passing them means "no worse than when measured", not "good".
- **Timing is not a speed measurement.** Up to four heavy jobs shared the 8 GB
  M1. On 28 Sep, RTF was 0.319–1.145 for runs that decoded speech and
  0.160–0.682 for diarization-only runs. On 29 Sep (`whisper-sherpa-ecapa`) it
  was 0.26–0.38 for runs that decoded speech, and 0.023–0.035 from the cached
  transcript (the hinted runs, repeated on 30 Sep). pyannote on the GPU ran at
  0.13–0.26.
- **Against the 25 Sep subset.** On all 16 meetings the hinted configuration
  scores DER 0.6396 and cpWER 0.8567; the 4-meeting subset scored 0.6557 and
  0.8533.

**The 25 September subset, and the only Piper numbers.** Source:
`build/v5/reports/` and `build/v5/summary.md` (committed evidence; see
`build/v5/README.md`), quoted in [`docs/v5-results.md`](v5-results.md). These
are macro means over 4 meetings per set. The AMI rows are superseded by the
table above. The Piper rows have not been re-run.

| Data | System | DER | JER | cpWER | tcpWER | WER (speaker-agnostic) |
|---|---|---:|---:|---:|---:|---:|
| AMI, 4 test meetings | whisper-sherpa, hint | 0.6557 | 0.8150 | 0.8533 | 0.9709 | 0.3250 |
| AMI, 4 test meetings | whisper-sherpa, no hint | not scorable: meeteval refused all 4 | – | – | – | – |
| AMI, 3 of 4 (supplementary, meeteval guard lifted) | whisper-sherpa, no hint | 0.7760 | 0.8020 | 1.1096 | 1.2290 | 0.2876 |
| AMI, 4 test meetings | sherpa's own turns, hint (words not used) | 0.5324 | 0.7424 | n/a | n/a | n/a |
| AMI, 4 test meetings | energy-vad-cluster, no hint (no ASR) | 0.6445 | 0.7566 | n/a | n/a | n/a |
| AMI, 4 test meetings | energy-vad-cluster, hint (no ASR) | 0.6709 | 0.7792 | n/a | n/a | n/a |
| Piper `mix.wav` | whisper-sherpa, hint (honoured on 3 of 4; s0103 returned 2 speakers for 3) | 0.6149 | 0.7699 | 1.0993 | 1.1233 | 0.2501 |
| Piper `mix.wav` | whisper-sherpa, no hint | 0.6760 | 0.7191 | 0.9962 | 1.0732 | 0.2501 |
| Piper `mix.wav` | energy-vad-cluster, no hint (no ASR) | 0.2304 | 0.3060 | n/a | n/a | n/a |
| Piper `mix.wav` | energy-vad-cluster, hint (no ASR) | 0.1920 | 0.2562 | n/a | n/a | n/a |
| Piper device Ogg/Opus | whisper-sherpa, hint (honoured on 3 of 4; s0103 returned 2 speakers for 3) | 0.6052 | 0.7434 | 1.0438 | 1.0599 | 0.2764 |
| Piper device Ogg/Opus | whisper-sherpa, no hint | 0.6819 | 0.7335 | 1.0242 | 1.0722 | 0.2764 |
| All 8 meetings | oracle (self-test) | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

- The "not scorable" row reflects the evals of 25 Sep, which dropped a meeting
  when meeteval refused. Evals now keep DER, JER and WER for such a meeting;
  the full-split table above shows the result.
- The segmentation model (pyannote segmentation-3.0) was trained on data that
  includes AMI, per its model card. Whether the AMI test split was held out
  cannot be verified here, so every AMI number may be optimistic.
- On `synth-piper-3spk-reverb-noise-s0103` a hint of 3 was not honoured: the
  diarizer returned 2 speakers, on both `mix.wav` and the device Ogg. That
  meeting has the highest hinted cpWER on `mix.wav` (1.3401). The hinted Piper
  rows' macro speaker-count error is −0.25, not 0.
- Two regression-gate suites, `ami-subset-whisper-sherpa` and
  `synthetic-piper-whisper-sherpa`, are the 25 Sep macro values plus 0.03
  (0.25 on speaker-count error), rounded up. Both passed on 25 Sep.
- A separate verifier (25 Sep, stage-2 journal) re-scored six configurations
  from the stored hypotheses, and each report was identical to the stored one.
  It also compared all 896 numeric table cells of `docs/v5-results.md` with the
  saved outputs and found no mismatch.

### 4.5 V6 Docker runs (25 and 28 September)

From `build/v6/compose-smoke-2026-09-25.log`, on an Apple M1 with Colima (Linux
arm64 VM):

| Measure | Value |
|---|---:|
| Image build (outside the timed window by design; base image already present) | 121.38 s |
| `docker compose up -d --wait` to healthy (target 60 s) | 6.58 s |
| Job, timed by the smoke script | 11.34 s, exit 0 |
| Job, timed inside the container | 10.09 s |
| Mock-cloud round trip | 49 161 bytes in 3 parts, byte-exact download, task states PENDING, STARTED, SUCCESS |
| Colima VM (from `build/v6/docker-env-2026-09-28.txt`, captured 28 Sep, not during the run) | 2 CPUs, 3 039 588 352 bytes of memory, Docker daemon 29.5.2 |

On 28 Sep the `compose` workflow ran the same script on a GitHub
`ubuntu-latest` runner (x86_64), with a cold cache. The numbers below are from
run 36396859985 (`94de5c9`), read from its check-run notices. The image build
took 102.56 s and pulled the base image. `up -d --wait` was healthy in
**5.81 s**. The job exited 0 in 14.66 s. The workflow has run again on later
commits that touch the images.

The job's zero scores come from the oracle self-test, not from a system.

### 4.6 R7-S13 hashes (the real SDK pulling a file)

- Served file: `tests/fixtures/r6s2_16k_mono.ogg` (synthetic), 11 271 bytes,
  sha256 `0f45367bd1eae540a51f2741e3150ffc705aace3054bfdaa4e134b71cbb48c3d`
  (re-hashed 28 Sep).
- [`r7/r7-s13-evidence/DELIVERED-OUTPUTS.sha256.txt`](../r7/r7-s13-evidence/DELIVERED-OUTPUTS.sha256.txt)
  lists 11 pulled outputs with exactly that hash. Raw path: runs 1b, 5, 6c, 12.
  Export path: runs 4b, 6b, 6d, 8, 10, 11, 13. The pulled files are not
  archived. The driver's own on-device `PULLCAP_*_DONE` log lines record the
  same hash in 13 runs: these 11 plus the raw path of runs 2 and 3.
- [`r7/r7-s13-evidence/SHA256SUMS`](../r7/r7-s13-evidence/SHA256SUMS) covers
  the 60 archived evidence files; `shasum -a 256 -c` passed for all 60 on
  28 Sep. Among them are the two failed outputs:
  `export-run3-both-concatenated-14311.opus` (14 311 B, `03d24231…`) and
  `export-run6-gap-corrupted-12871.opus` (12 871 B, `d93f8d40…`).
- The blank-token offline re-run (25 Sep) exported 11 271 bytes with the same
  sha256 ([`r7/r7-s14-evidence/blank-token-offline-check/`](../r7/r7-s14-evidence/blank-token-offline-check/)).

### 4.7 Fault-injection matrix (V2)

From `build/v2-fault-matrix.json` as written by the 29 Sep suite run: 64 rows.

| Outcome | Rows |
|---|---:|
| Recovered byte-exact | 39 |
| Recovered after the app resumes from its cursor | 6 |
| Failure detected, no corrupted output | 16 |
| Corruption risk (`old_stream_not_abandoned[atomic]`: a device that never abandons its stream) | 1 |
| **Counted cells** | **62** |
| Outside the count: `restart_serves_other_revision[paced]` (silent corruption; the harness check flags it) | 1 |
| Outside the count: `data_payload_bitflip[atomic]` (undetectable: DATA frames carry no checksum) | 1 |

Two rows were added on 28 Sep for two new emulator defaults from the phase-1
code review ([§5.2](#52-ble-emulator-and-legacy-handshake)):
`mtu_23_sized_frames[paced]` (recovered) and
`zero_length_file_head_failure[atomic]` (detected, with no restarts). The rows
for the old device behaviours stay, with those behaviours switched on
explicitly.

Every row's outcome is one of its accepted outcomes. For 63 rows that is a
single value. `old_stream_not_abandoned[paced]` accepts `corruption_risk` or
`detected` and gave `detected` (9 restarts, `restart_cap_exceeded`). The test
asserts membership in the accepted set (`tests/test_v2_fault_matrix.py`,
`record`). By transfer mode: 27 rows stream unpaced ("atomic"), 35 are paced
(19 of them streamed from a task, 1 without abort), and 2 file-list rows have
no transfer mode. Several faults appear in both an unpaced and a paced row.

### 4.8 Plaud cloud contact in the runtime logs

Recounted on 28 Sep with `grep -a` over the archived logs:

| Measure | Count |
|---|---:|
| Logs containing the SDK's `POST …/partner/sdk/gen-key` | 22 (17 R7-S13, 5 R7-S14) |
| … of which the server answered `Status: 401` / `ACCESS_TOKEN_INVALID` | 19 |
| … of which never reached the server: `UnknownHostException` for `platform-jp.plaud.ai` (R7-S13 runs 11, 12, 13) | 3 |
| R7-S14 run 1 (log truncated by the ring buffer; request inferred) | 1 more, not in the 22 |
| Other Plaud URLs requested | 0 (the other `plaud.ai` lines are base-URL configuration) |
| `gen-key` lines in the blank-token offline check of the pull driver (25 Sep), counted in the archived `logcat.filtered.log` (133 lines). That filter keeps the `PlaudSdk_SourceFile` tag, which logged every earlier request, and it shows the Partner API base-URL lines. The unfiltered logcat, which `run.sh` counted, was not archived | 0 |
| `gen-key` lines in the blank-token offline re-runs of the K3 driver and the Wi-Fi driver (modes open and transfer), 28 Sep. The full logcats are archived (gzip; 4 846, 1 492 and 1 479 lines) in [`r7/r7-s14-evidence/blank-token-drivers-check/`](../r7/r7-s14-evidence/blank-token-drivers-check/) | 0 |
| `gen-key` lines in the two R7-S15 Wi-Fi runs, 29 Sep. Run 1 (`--debug-no-network`): the phone associated with the simulated `PLAUD0001` but got no DHCP address. Run 2: the phone was on `PLAUD0001` (10.0.2.16), with DNS pointed at a dead address (127.0.0.1) and TCP at a dead proxy (127.0.0.1:9). Full logcats archived (gzip; 1 935 and 4 736 lines) in [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) | 0 |

The R7-S12 logs were filtered to other log tags, so they cannot show whether
the request was sent; that driver passed the same synthetic token. The R4-S3
and R5-S1 logs show `user access token not set`: those runs had no token.

## 5. Components

Each entry: what exists; how it was proven; what is not proven; where to read more.

### 5.1 Protocol reconstruction

- **Exists.** A field-by-field ledger: GATT profile, framing, both opcode tables,
  control messages, handshakes, file list and transfer, stop, delete, settings
  (opcode 8), capability exchange (opcode 138), advertising, Wi-Fi design, audio
  contracts. The recorder is a TinnoTech platform.
- **Proven.** `javap` is ground truth; jadx is orientation only (ledger §11
  lists 8 places it misleads). A script extracts facts into
  `docs/evidence-digest.json`, pinned to the AAR hash;
  `tests/test_evidence_conformance.py` asserts the emulator against it. The iOS
  `.swiftinterface` supplies names.
- **Reviewed.** An independent review of the phase-1 code on 28 Sep reported
  28 findings. The confirmed defects were fixed in `3fb2d27`, each with a
  test (`tests/test_review_phase1.py`, 21 tests, or the existing suites)
  ([§5.2](#52-ble-emulator-and-legacy-handshake)).
- **Not proven.** Any device-side fact ([§6.1](#61-open-questions-24)).
- **More.** [`docs/protocol-ledger.md`](protocol-ledger.md),
  [`docs/reconstruction-log.md`](reconstruction-log.md),
  [`docs/final-closure-report.md`](final-closure-report.md).

### 5.2 BLE emulator and legacy handshake

- **Exists.** `PlaudPeripheral` advertises portVersion 7; serves service `1910`
  (`2BB0` notify/indicate, `2BB1` write); answers the legacy handshake,
  `getState`, `syncTime`, `getStorage`, battery, settings, paged file list,
  resume, delete; transfers HEAD · DATA · EMPTY_PACKAGE(0) · TAIL.
  `for_real_sdk()` adds task streaming and 4 ms pacing; the plain constructor
  is inline and unpaced.
- **Changed by the phase-1 review (28 Sep).**
  - `advertise()` now sends the layout the runtime rigs used: 29 bytes of
    Flags plus manufacturer data under company id 0xFFFF, with the name and
    service UUID in the scan response. Before, it sent the blob without a
    company id, in 42–44 bytes, over the legacy limit.
  - The serial scrub, `versionCode` narrowing and the projectCode override
    now follow the SDK's `u4.a`.
  - The handshake checks lengths before reading, and treats the FE12 count
    and the receive counter as signed.
  - The file list is paged, and DATA payloads are capped, to fit ATT_MTU − 3.
  - A transfer with nothing to send is answered by one HEAD with status 1.
    The old HEAD · EMPTY_PACKAGE · TAIL made the SDK restart forever.
  - The advertised and served portVersion must agree.
  - Both new defaults are HARNESS_POLICY. The old behaviours stay available
    and are measured in the V2 matrix ([§4.7](#47-fault-injection-matrix-v2)).
- **Proven.** RUNTIME_PROVEN in R7-S12 and R7-S13 for connect and MTU, the
  legacy handshake, getState, syncTime, battery, settings (opcode 8), a
  one-entry file list, syncFile, stopSync and the transfer frames
  ([§5.4](#54-real-sdk-runtime-runs-android-only)).
- getStorage, resumeRecord, deleteFile and multi-frame file-list paging are
  EMULATOR_INTEGRATION only.
- The runtime runners did not use `PlaudPeripheral.advertise()`. netsimd
  rejects extended advertising, so they published a 29-byte legacy payload
  (flags plus the emulator's manufacturer data) instead
  (`r4-s3/plaud_netsim_peripheral.py:88-107`,
  `r7/pull_capture_peripheral.py:200-204`). `advertise()` has sent that
  layout since 28 Sep, but the real SDK has not scanned it.
- **Not proven.** Accepting any token, EMPTY_PACKAGE code 0, 4 ms pacing and the
  32-byte DATA payload are HARNESS_POLICY. The constructor refuses
  portVersion ≥ 20 on purpose: at that level the SDK seals every frame.
- **More.** Ledger §5, §12, §14, §15.

### 5.3 Encrypted path (portVersion ≥ 20)

- **Exists.** Marker framing, the RSA secret package, key derivation and
  ChaCha20-Poly1305 sealing with independent counters, in a test-only
  peripheral (R5-S7 to R5-S11), over Bumble GATT with synthetic keys.
- **Proven.** BYTECODE_PROVEN layouts; an RFC 8439 vector pins the AEAD wiring.
- **Not proven.** Never run with the real SDK. Real keys come from Plaud's cloud
  (`/sdk/gen-key` returns the private key, `/sdk/sn-sign` the signature). The
  FE11 payload, FE20 effect and device counter start are UNKNOWN.
- **More.** Ledger §4.3–§4.6; `r5-s5/` … `r5-s11/` READMEs.

### 5.4 Real-SDK runtime runs (Android only)

| Run set | Date | What happened | Evidence |
|---|---|---|---|
| R4-S3, R5-S1 | 22 Sep | The real SDK scanned, connected, set MTU 517, discovered and subscribed; it stopped at `bind_token_empty` | `r4-s3/`, `r5-s1/` |
| R7-S12 | 23 Sep | `recoveryConnectBleDevice(device, "SYNTH-HIST-0001")` wrote a 22-byte k3 equal to our model (`01 01 00 02 00 00` + `SYNTHHIST0001` + `000`); the emulator accepted it; `bleBind status=0`. An empty id writes nothing. Opcode 8 was found. 4 runs | [`r7/r7-s12-k3-runtime-capture.md`](../r7/r7-s12-k3-runtime-capture.md) |
| R7-S13 | 23–24 Sep | File list and byte-exact pull through `syncFile` and `exportAudio(OPUS)`; gap recovery converged byte-exact in all four runs where the emulator abandoned the old stream (6b, 6c, 6d, 13). 17 runs have archived logs | [`r7/r7-s13-recording-pull.md`](../r7/r7-s13-recording-pull.md) |
| R7-S14 | 25 Sep | 6 runs. Bind, OpenWiFi (opcode 10) and its answer worked. In the 4 transfer runs (1, 1b, 2, 5) the SDK asked Android to join `PLAUD0001`, and the request timed out after 30 s. In 1b, 2 and 5 the log shows `onError(1003)` (at +30.124 s, +30.051 s and +30.155 s); run 1's log is truncated. Runs 3 and 4 drove `setDeviceWiFi` open and close only and requested no join. In runs 1 and 1b the pre-fix emulator answered opcode 10 without a passphrase and started no Wi-Fi device (D1, fixed). The SDK never started its server on port 8081, and no Wi-Fi message went either way in any run | [`r7/r7-s14-wifi-real-sdk.md`](../r7/r7-s14-wifi-real-sdk.md) |
| Blank-token check | 25 Sep | Export re-run with the phone's Wi-Fi and mobile data off and a blank SDK token: byte-exact, no `gen-key` line | [`r7/r7-s14-evidence/blank-token-offline-check/`](../r7/r7-s14-evidence/blank-token-offline-check/) |
| Blank-token drivers | 28 Sep | The K3 driver and the Wi-Fi driver (modes open and transfer), from the debug build with the drivers in the debug source set, offline, with a blank token. K3 and Wi-Fi bind with status 0. OpenWiFi answers 0. With the phone's Wi-Fi off, the transfer's join fails at once (`onError(1003)` at +151 ms). No `gen-key` line in any full logcat | [`r7/r7-s14-evidence/blank-token-drivers-check/`](../r7/r7-s14-evidence/blank-token-drivers-check/) |
| R7-S15 | 29 Sep | The emulator's simulated Wi-Fi offered `PLAUD0001` (WPA2, passphrase `10000001`; `netsimd --wifi`). Run 1 (`--debug-no-network`): the phone associated and completed the WPA2 handshake with the SDK's passphrase, but got no DHCP address, and the SDK gave up at 30 s. Run 2 (DHCP on; DNS and TCP egress cut): joined in 3.9 s. The SDK started its WebSocket server on port 8081, completed the Wi-Fi handshake with our device and listed the file. It downloaded it over Wi-Fi byte-exact (11 271 B, `0f45367b…`), in the download and the OPUS export. No `gen-key` line. The bytes went through `adb forward`, not the simulated radio; the handshake token was empty; no encryption | [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) |

What only the real SDK could show (R7-S13): completion needs the EMPTY_PACKAGE
before the TAIL. On a gap the SDK sends stop at once and restarts from its
cursor about 35 ms later. Zero-latency answers confuse its request queue
(errors −98/−99); about 4 ms pacing removes that. A device that keeps sending
an abandoned stream can corrupt the export. OPUS export of a plain Ogg file is
a byte-for-byte passthrough.

### 5.5 Wi-Fi transfer

- **Exists.** PDU codecs; the pen as a WebSocket client; a phone-side test
  double rebuilt from bytecode; sessions sealed with either AEAD. With a
  `wifi_device_factory`, opcode 10 starts the Wi-Fi device over loopback and
  opcode 13 closes it.
- **Proven.** EMULATOR_INTEGRATION against the double. In R7-S14 runs 2–5
  (fixed emulator), the real SDK's opcode 10 started our Wi-Fi device. Runs 2
  and 5 sent mode 0 from `startWifiTransfer`, and runs 3 and 4 sent mode 1 from
  `setDeviceWiFi(true)`. No session followed. The pre-fix runs 1 and 1b started
  none (`wifi_devices` in `r7/r7-s14-evidence/capture-run*.json`).
- **Real SDK over Wi-Fi (R7-S15, 29 Sep).** With a simulated `PLAUD0001`
  network to join, the real SDK completed the whole session: handshake, file
  list, `FileSync` and three `FileSyncContent` frames. The download was
  byte-exact ([§5.4](#54-real-sdk-runtime-runs-android-only)). The runtime
  confirmed these bytecode readings: the SSID and passphrase; that the phone
  starts its server only once the network is available (the pen needed 6
  dials); and the PDU sequence.
- **Not proven.** The bytes crossing the simulated radio (they went through
  `adb forward`), a non-empty handshake token, the encrypted Wi-Fi session, a
  real pen's access point. The opcode-10 byte's meaning is UNKNOWN.
- **More.** [`docs/wifi-transport.md`](wifi-transport.md) §8, §11; ledger §7.

### 5.6 Fault-injection matrix (V2)

- **Exists.** A fault-injecting peripheral and 64 rows: drops, duplicates,
  reordering, truncation, wrong session, HEAD/TAIL faults, disconnect and
  resume, stop mid-transfer, MTU 23–517, both CCCD modes, delete during sync,
  back-to-back syncs, the R7-S13 sentinel cases, and the two device behaviours
  the emulator stopped using on 28 Sep (unsized frames, HEAD · EMPTY · TAIL for
  an empty transfer).
- **Proven.** Scored by `tests/fault_support.py`, a model of the SDK's `q$a`
  receiver derived from bytecode. Six of its rules were cross-checked against
  the R7-S13 logs ([`docs/v2-fault-matrix.md`](v2-fault-matrix.md) §3,
  "Runtime cross-checks of the model"): R2 completion, R9/R10 TAIL-only non-completion, the R12 after-TAIL
  sentinel, R6 immediate stop and the ~35 ms restart, R2 while recovering, and
  R13 CRC ignored.
- **Not proven.** The real SDK never ran this matrix.

### 5.7 Product reconstruction (phase 2, 23 Sep)

- **Exists.** Ten architecture documents, a claim ledger, a source map and an
  evidence graph built by script: 36 sources, 16 agents, 397 claims, 221
  unknowns, 138 contradictions, 153 verdicts, 2 072 edges.
- **Findings.** Plaud's own code and documents describe the partner product
  almost completely. The two ESP32 repositories are voice-agent prototypes, not
  recorder firmware. No server-side search or vector store appears on any
  observed surface.
- **Not proven.** The consumer cloud is seen only through four third-party
  clients (CLOUD_OBSERVED). Graph edges come from matching citation text. The
  endpoint inventory is static; [`r7/README.md`](../r7/README.md) calls it 50
  endpoints across five surfaces (not recounted here).
- **More.** [`docs/final-product-reconstruction.md`](final-product-reconstruction.md),
  [`docs/product-ledger.md`](product-ledger.md), [`docs/architecture/`](architecture/),
  [`docs/source-map.md`](source-map.md), [`r7/cloud-endpoint-inventory.md`](../r7/cloud-endpoint-inventory.md).

### 5.8 Generator

- **Exists.** Scenarios, a turn planner, room simulation (pyroomacoustics),
  word timings (exact for the formant voice; the voice's own alignment for
  Piper), RTTM/STM references, Ogg/Opus in the device's shapes
  (16 kHz, 20 ms frames, 32 kbps CBR). Voices: a model-free formant voice
  (default, offline) and Piper `en_US-libritts_r-medium` (optional).
- **Not proven.** Formant speech is not intelligible (small.en WER 0.9615).
  Piper text is seeded word salad; its byte-determinism was checked on one M1
  with one onnxruntime version. Since 28 Sep a Piper `meeting.json` carries
  `generator.tts`: the timing method, phonemization and noise settings, the
  voice and config sha256, and the LibriTTS-R CC BY 4.0 attribution. Its
  `timing_exact: true` means exact against the voice's own alignment
  ([`docs/generator.md`](generator.md) §6.2). Kokoro is untested.
- **More.** [`docs/generator.md`](generator.md) §6.2, §8.

### 5.9 Evals and the AMI converter

- **Exists.** DER and JER (pyannote.metrics), WER (jiwer), cpWER and tcpWER
  (meeteval 0.4.3), gates (`evals/gates.yaml`), a batch runner, and
  `python -m evals.ami` (AMI NXT → harness format).
- **Proven.** V4. The converter's speech turns equal the BUT `only_words` RTTMs
  line for line on 168 of 170 meetings, including all 18 dev and 16 test
  meetings ([`docs/evals.md`](evals.md), "AMI"). The DER agrees with NIST
  md-eval (`scripts/crosscheck-md-eval.sh`). On the full V5 test split, 224 of
  224 per-meeting values agree at collar 0 and 213 of 224 at collar 0.25 (the
  other 11 by at most 0.0054). This closes review finding EV-4.
- **Refusals.** meeteval 0.4.3 refuses cpWER above 20 hypothesis speakers.
  Since 28 Sep the meeting is kept: its DER, JER and WER are reported, and
  cpWER and tcpWER are marked not scored, with the reason. Aggregates leave the
  meeting out of those two pools and count it, and a gate on a refused metric
  fails. With the guard lifted, meeteval fails for another reason above about
  56 speakers ("too few fallback keys").
- **Data here.** The headset-mix audio and references of all 16 test and 18
  dev meetings (CC BY 4.0), sha256-pinned in `scripts/run-v5.sh` and
  `scripts/calibrate-sherpa.sh`.

### 5.10 Pipeline

- **Exists.** `python -m pipeline list` (29 Sep) shows 11 entries. Available in
  `.venv`: `oracle`, `perturbed-oracle`, `energy-vad-cluster`,
  `faster-whisper`, `sherpa-onnx-diarization`, `whisper-sherpa`.
  `embedding-cluster` is available in a separate environment with torch 2.14.0
  and SpeechBrain 1.1.1 (`.venv-torch`, git-ignored). A third environment,
  `.venv-pyannote` (git-ignored; 29 Sep), adds pyannote.audio 4.0.7 to those and
  to the model stack. There, 10 of the 11 are available, all but `whisperx`,
  including the new `whisper-sherpa-ecapa`.
- **`whisper-sherpa`.** faster-whisper 1.2.1 (`small.en`) plus sherpa-onnx
  1.13.8 (pyannote segmentation-3.0 ONNX, 3D-Speaker CAM++). Weights pinned by
  sha256 in `pipeline/model_store.py`; packages in the optional
  `requirements/models.txt`, which CI does not install. Since 28 Sep:
  - Decoding is seeded (`--param seed`, default 0). CTranslate2 is seeded
    before the model is built, which makes faster-whisper's sampled
    temperature fallback repeat across processes. The run records how many
    segments each temperature produced.
  - `--param asr_cache=DIR` shares one transcript per meeting between
    diarization settings.
  - `--param assignment_tie_break` picks the speaker of a word whose largest
    overlap is tied: `floor` (the default), `latest_start`, `first_seen` or
    `previous_word`.
  - `pipeline/sherpa_sweep.py` replays sherpa-onnx's clustering and
    reconstruction from cached segmentation and embeddings, bit-identical to
    the real library at every threshold checked. A brute-force sweep, one
    sherpa-onnx pass per threshold over the dev split, would have taken most
    of a day on this machine.
- **`embedding-cluster`.** Energy VAD, SpeechBrain ECAPA embeddings (ungated)
  and the model-free clustering. It first ran against the real model on
  28 Sep. PyTorch's NNPACK convolution made one ECAPA batch take 114.94 s on
  this M1, against 3.39 s without it, so the adapter disables NNPACK on CPU.
  Its distance threshold, 0.55, was calibrated on the dev split (29 Sep)
  through a replay that equals real runs at the default threshold (IS1008a;
  EN2002a–c), with the hint (IS1009a) and at 0.55 (EN2002b). On the test split it
  scores DER 0.3708 with the hint and 0.4967 without
  ([§4.4](#44-v5-the-full-ami-test-split-28-september-and-the-25-september-subset)).
- **`whisper-sherpa-ecapa` (29 Sep).** The `whisper-sherpa` transcript with
  turns from ECAPA embeddings cut into the number of speakers sherpa-onnx finds
  at `cluster_threshold` 1.15 (`SherpaCountedEmbeddingDiarizer`,
  `pipeline/whisper_sherpa.py`); with a hint, sherpa-onnx is not run. Chosen on
  the dev split ([`docs/pipeline.md`](pipeline.md) §11.10). Without a hint it
  scores macro DER 0.4104 and cpWER 0.5132. It was the best system measured until
  the pyannote runs later on 29 Sep (`faster-whisper+pyannote`: 0.3140, 0.3477).
- **pyannote (29 Sep).** The gated models ran with the owner's Hugging Face
  token. The token is read from the environment and never stored in the
  repository. The adapters turn pyannote.audio 4's usage telemetry off when
  `pipeline.adapters` is imported, and again before pyannote runs, unless the user
  set `PYANNOTE_METRICS_ENABLED` before pyannote.audio was imported (pyannote writes
  its own default, "true", on import). They record the model's cache snapshot
  (revision and file hashes), and accept `diarization_device` so pyannote can
  use the Mac GPU (4.7× faster than the CPU, same turns;
  `build/v5-test/pyannote-device-check/`) while Whisper stays on
  the CPU. Results: `pyannote-audio` DER 0.1295; `faster-whisper+pyannote`
  cpWER 0.3477. At collar 0, pyannote's pooled DER is 17.07 %, against 17.0 %
  on its model card for the same AMI condition
  ([`docs/pipeline.md`](pipeline.md) §11.11).
- **Not proven.** `whisperx` was tested only against stand-in modules (review
  finding PIPE-05). `energy-vad-cluster`'s pitch
  feature is tuned to synthetic voices.
- **Before V5.** Sanity checks on one AMI test meeting (ES2004a): a 60 s clip
  and excerpts ([`docs/pipeline.md`](pipeline.md) §11.4–§11.5). Across 19 runs
  of the 60 s clip, under varying machine load, RTF ranged from 0.291 to 1.273.
  They show the stack works; they are not a benchmark.
- **More.** [`docs/pipeline.md`](pipeline.md) §2, §8, §11;
  [`docs/v5-test-split.md`](v5-test-split.md); [`docs/v5-results.md`](v5-results.md).

### 5.11 Mock cloud

- **Exists.** 29 FastAPI operations on 28 paths: 21 for the partner contract
  (partner and user tokens; bind, unbind, binding; SDK token, config, latest
  version; `gen-key`, `sn-sign`, `sn-verify`, metadata, version; presigned
  multipart upload; an S3-style store; transcription jobs), 7 `/_mock` control
  routes, and an info route at `GET /`. Every value it issues is synthetic.
- **Proven.** EMULATOR_INTEGRATION: 76 tests drive it in-process
  (`tests/test_mockcloud_*.py`), and the V6 job round-trips one meeting through
  it over HTTP. [`docs/mockcloud.md`](mockcloud.md) tags each route's contract
  as DOC-EXACT, BYTECODE_PROVEN, OFFICIAL_SOURCE, INFERRED or HARNESS_POLICY.
- **Not proven.** Built from Plaud's OpenAPI documentation, template-app source
  and SDK bytecode; never compared with real
  responses; never driven by the real template app (that needs a TLS proxy).
- **GoReplay.** `scripts/crosscheck-goreplay.sh` builds `gor` (GoReplay 2.0.0)
  from the pinned `reference/upstream/goreplay` commit, out of tree. It then
  exports the mock's traffic log as `?format=gor` and replays it into a
  recording server. All 14 logged requests arrive with identical method,
  target and body (28 Sep; closes review finding MC-3).
- **More.** [`docs/mockcloud.md`](mockcloud.md).

### 5.12 Compose (V6)

- **Exists.** `docker-compose.yml`, three Dockerfiles, healthchecks,
  `scripts/compose-smoke.sh`, and `scripts/local-up.sh` (same topology, no
  Docker). The job probes both services, scans the emulator, generates 2
  meetings, runs the oracle pipeline and evals, and round-trips one meeting
  through the mock cloud.
- **Runs.** Colima on arm64 (25 Sep) and GitHub `ubuntu-latest` on x86_64 from
  a cold cache (28 Sep, the `compose` workflow; [§4.5](#45-v6-docker-runs-25-and-28-september)).
- **Not proven.** The job does not pull the recording
  over BLE. No container runs the Android SDK.
- **More.** [`docs/compose.md`](compose.md).

### 5.13 CI

Three workflows run on GitHub: `tests`, `compose` and `evals`.

- **`tests`.** Runs on `ubuntu-latest` (x86_64), `ubuntu-24.04-arm` and
  `macos-latest`. It installs `requirements/ci.txt`, fetches Bumble at its pin,
  generates one meeting, runs the suite with `PLAUD_STRICT_SKIPS=1`, and
  checks that `reference/` is unchanged. An "Annotate failures" step publishes
  failing tests as check-run annotations, named by platform. The public API
  returns those without a token; job logs need one.
- **`compose`.** Runs `scripts/compose-smoke.sh` on an x86_64 runner and
  publishes the V6 timings as notices
  ([§4.5](#45-v6-docker-runs-25-and-28-september)). It runs when the images,
  the compose file, the smoke script or the code the images carry changes.
- **`evals`.** Runs only on pushes and pull requests that touch `evals/**`,
  `tests/test_evals_*.py`, `tests/conftest.py`, `docs/evals.md`,
  `requirements/evals.txt` or the workflow itself, or on manual dispatch. It
  is not a general check.

History, all on 28 Sep unless dated:

| Commit | `tests` result | Cause and fix |
|---|---|---|
| `70249ba` (25 Sep) | Failed in `Tests` (exit code 2); the reference-tree check was skipped | The workflow installed only Bumble, pytest and pytest-asyncio, and the two synthetic Ogg fixtures were git-ignored. Fixed in `8b72dda`: install `requirements/ci.txt`, let `tests/fixtures/*.ogg` through `.gitignore`, and run the reference-tree check whenever its snapshot step succeeded |
| `8b72dda`, `7d01b1f` | One test failed (runs 36381797867, 36385278334): `test_cluster_embeddings_absorbs_an_outlier_island` | R-CI-1: three tied eigenvalues of 1.0, whose eigenvector basis differs between LAPACK builds. The clusterer cut inside the tie. An x86_64 container on this Mac passed, so only the real runner showed it. Fixed in `5a3d2ff` with a rotation test; the baseline's V5 outputs re-ran identical |
| `5a3d2ff` | Passed (run 36386441732): the first green run, x86_64 only | – |
| `d566d43` … `94de5c9` | Passed on x86_64; failed on macOS and arm64 (run 36396859963) | macOS runners have no docker CLI, so the `docker-cli-absent` gate now requires docker only on Linux. On arm64 the MFCC block-size test compared bits that differ in the last place; it now allows 1e-9. Fixed in `097eb99` |
| `097eb99` | Failed on macOS, two tests (run 36397868107) | The mock-cloud round trip can poll past STARTED on a slow runner; polled states must now be an ordered subset, and the mock's own history must hold STARTED then SUCCESS. The raw-Opus and Ogg decodes differed; the comparison was loosened to 1e-6. Fixed in `e9e73c8` |
| `ef5fb8d` (with `e9e73c8`) | Failed on macOS (run 36399591653) | The two Opus decodes differ on macOS over one stretch (7 807 of 576 000 samples, up to 0.059). The test now checks the pre-skip alignment instead: ≥ 98 % agreement at the offset, < 50 % one sample off. The cause of the decode difference is not established. Fixed in `c0be32e` |
| `c0be32e`, `ed03d14`, `853062b` | Passed on all three | – |
| `3e46ad9` | Failed on arm64 in the Install step (exit 1); x86_64 and macOS passed | Not established: the job log needs a token, and the annotations say only "Process completed with exit code 1". The next commit installed cleanly |
| `92146bf` | Passed on all three (run 36418829234) | – |

The decompiled SDK is absent in CI, so the tests that read it skip there by
design ([§4.1](#41-tests)).

### 5.14 Milestone spikes R4–R6

Each spike directory has a README that opens with its verdict.

| Spike | What it did | Verdict, quoted from its README |
|---|---|---|
| `r4-s2/` | Android ↔ netsim ↔ Bumble transport with a ping/pong payload; its own minimal Kotlin app, no Plaud code | "Results (2026-09-22 — VERIFIED)" |
| `r4-s3/` | The stock template APK (empty token) against the Bumble emulator | "the real SDK reaches the handshake-token boundary through the verified BLE transport" |
| `r4-s4/` | Credential provenance, read-only | "LEGITIMATE CREDENTIAL UNAVAILABLE — stopped after Phase 1." |
| `r5-s1/` | `PlaudPeripheral` against the real SDK at the GATT boundary | "SDK GATT BOUNDARY MATCHED — no emulator change required." |
| `r5-s2/` to `r5-s6/`, `r5-s10/` | Read-only bytecode audits: first handshake, j3, l3, modern handshake, modern transport, FE11 timing | Each "… AUDITED" (for example "MODERN TRANSPORT SEMANTICS AUDITED.") |
| `r5-s7/` to `r5-s9/` | Synthetic sealed (portVersion ≥ 20) round trips over Bumble GATT: GetState, file sync, handshake to file sync | "MODERN SEALED GETSTATE ROUND-TRIP VERIFIED, over Bumble GATT." and two similar |
| `r5-s11/` | Uncertainty isolation and capture-ready traces | "MODERN UNCERTAINTY ISOLATED AND TRACE-READY." |
| `r6-s1/` | Audio-path reconstruction | "AUDIO CONTRACT PARTIALLY RECONSTRUCTED" |
| `r6-s2/` | Independent Ogg/Opus validation of synthetic fixtures | "GENERIC OGG/OPUS VERIFIED; PLAUD FORMAT REMAINS UNCONFIRMED." |
| `r6-s3/` | Search for authentic Plaud audio | "NO AUTHENTIC PLAUD AUDIO FIXTURE FOUND; EXTERNAL EVIDENCE REQUIRED." |
| `r6-s4/` | Readiness to ingest a real export | "BLOCKED — AUTHENTIC PLAUD RECORDING NOT AVAILABLE." |

`r6-s3/` and `r6-s4/` are the evidence behind U20 ([§6.1](#61-open-questions-24)).
The verifiers are in [§4.2](#42-protocol-verifiers).

## 6. What is wrong, unknown or unresolved

### 6.1 Open questions (24)

[`docs/final-uncertainty-matrix.json`](final-uncertainty-matrix.json) lists 25
unresolved entries. U23 is marked `DUPLICATE_OF_U15`, which leaves 24.

| Required input | Count | Items |
|---|---:|---|
| A real device (`DEVICE_REQUIRED`) | 15 | U1 advertisement layout and branch (absorbs the ledger's U18); U2 storage units; U5 file-list filters; U6 FE12 fragment size; U7 RSA key validation; U9 MTU and receive-sequence checks; U10 FE20 effect; U12 file-list order; U14 transfer `end` semantics; U15 EMPTY_PACKAGE codes; U16 HEAD status values; U17 TAIL after EMPTY_PACKAGE; U22 capability bits and settings values; MODERN-1 FE11 payload; MODERN-2 sealed counter start and ACK timing |
| No known source (`UNKNOWN`) | 6 | U3 file-list `attribute`; U4 GetState bytes 16–17; U8 k3 constant 0x02; U11 resume `start`; U13 TAIL CRC coverage; U21 legacy TntAgent host |
| Credentials (`CREDENTIAL_REQUIRED`) | 2 | U19 how the cloud re-tags exported audio; CRED-1 token, snSignature and RSA key pair |
| A real recording (`EXTERNAL_DATA_REQUIRED`) | 1 | U20 which of four container shapes real firmware sends |

### 6.2 Known defects and limitations

| Item | Detail | Evidence |
|---|---|---|
| V5 target missed | On all 16 AMI test meetings the best realistic configuration, `faster-whisper+pyannote`, scores cpWER 0.3477 (target 0.30), with speaker confusion 0.012; pyannote's own turns score DER 0.1295 (target 0.20). The remaining gap is the transcript (WER 0.3221) and, for word-level DER, speech the word runs miss (0.26). pyannote's segmentation model was trained on data that includes AMI | [`docs/v5-test-split.md`](v5-test-split.md) §7.2 |
| sherpa's default threshold over-clusters | With `cluster_threshold` 0.5, sherpa finds 35–108 clusters on 3–4-speaker AMI meetings. The calibrated 1.15 is not the default: a caller must pass it | [`docs/v5-test-split.md`](v5-test-split.md) §5 |
| meeteval's speaker limits | meeteval 0.4.3 refuses cpWER above 20 speakers; with the guard lifted it fails above about 56. Such meetings keep DER, JER and WER but have no cpWER | [`docs/evals.md`](evals.md) |
| sherpa diarization near chance on Piper voices | confusion 0.50 of reference time even with the hint; not traced | [`docs/v5-results.md`](v5-results.md) §8.4 |
| Timing under contention | V5 RTFs were measured on a shared 8 GB M1 (25 Sep: 0.215–0.380; 28 Sep: 0.160–1.145; 29–30 Sep: 0.023–0.381); no quiet-machine figure | [`docs/v5-results.md`](v5-results.md) §4, [`docs/v5-test-split.md`](v5-test-split.md) §4 |
| Opus decode differs on macOS | On GitHub's macOS runner, FFmpeg's Opus decoder (same PyAV, same packets) gives different samples over one ~8-packet stretch of a test clip; Linux and this Mac agree bit for bit. Cause not established | `c0be32e`; [§5.13](#513-ci) |
| Pen dial default | The pen dials once by default; the real phone starts its server only after the join. In R7-S15 our device needed 6 dials; the runs use 90 attempts, and the default is unchanged | [`r7/r7-s14-wifi-real-sdk.md`](../r7/r7-s14-wifi-real-sdk.md) D4, [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) |
| SDK CloseWiFi quirk | The SDK waits for the close answer on opcode 10 but type-checks it as 13, so no close status reaches the app; an opcode-10 answer arrives as status −1 | same, D5 |
| SDK after a failed join | The Wi-Fi agent stays CONNECTING, and `endWiFiTransfer` sends nothing over BLE | same, D6 |
| V2 scoring | "No silent corruption" holds by construction in the counted cells; the matrix measures completion | review finding T10 |
| Remaining narrowed review findings | GEN-1 overlap target still short on one preset (worst −0.043); PIPE-05: the whisperx adapter and the pyannote.audio 3.x paths checked only against stand-ins; PIPE-01 still miscounts two held-out smoke seeds without the hint. EV-4 (md-eval) and MC-3 (GoReplay) were closed on 28 Sep | [`docs/progress-report.md`](progress-report.md) §8.1 |

Fixed on 28–29 Sep and removed from this table: ASR not repeatable on long audio
(now seeded); evals dropping a meeting that meeteval refused; the Wi-Fi join,
fixed on 29 Sep by giving the emulated phone a `PLAUD0001` network and approving
Android's network-request dialog with a scripted tap (R7-S15); Piper metadata
missing from `meeting.json`; the three debug activities exported from
`src/main` (now in the debug source set only; the merged release manifest has
none of them).

### 6.3 Licence questions

**The Piper voice (unresolved).** The voice `en_US-libritts_r-medium` is trained on LibriTTS-R (CC BY 4.0) and
fine-tuned from the lessac voice. The lessac dataset licence (Blizzard 2013) is
a research-only agreement that forbids distributing the materials. Whether that
reaches fine-tuned weights, or audio made with them, is not established. It
must be settled before any Piper-made audio is redistributed
([`docs/generator.md`](generator.md) §6.2). piper-tts itself is
GPL-3.0-or-later and optional.

**The repository.** The working tree declares AGPL-3.0 (`LICENSE`) for
everything except `r7/android-app/`, which is Apache-2.0
(`r7/android-app/LICENSE` and `NOTICE-MODIFICATIONS.md`). All three files were
added in `8b72dda`, and the GitHub API has reported AGPL-3.0 since (checked
29 Sep). `70249ba`, the first push (with its parent scaffold `09c8922`), had no licence file. Its 149
`r7/android-app/` files (147 copied from Plaud's Apache-2.0 template, plus our
two debug drivers) stay public in that commit's history without the
Apache-2.0 text (section 4(a)) or the notice of modifications (section 4(b)).
`NOTICE-MODIFICATIONS.md` also records the move of the drivers into the debug
source set (`7740153`).

### 6.4 Never run

- The iOS SDK. This machine has only the Xcode command-line tools
  (`xcode-select -p` → `/Library/Developer/CommandLineTools`).
- Any real Plaud device, account or credential.
- `whisperx` against a model (not installed).
- The real SDK's Wi-Fi bytes over the simulated radio (R7-S15 used `adb forward`),
  and an encrypted Wi-Fi session.
- The mock cloud against the real template app.
- A V5 speed measurement on a quiet machine.

Done on 28 Sep and removed from this list: CI on macOS and arm64 Linux; V6 on
x86_64, on a GitHub runner, from a cold cache; `embedding-cluster` against its
model; V5 on the full 16-meeting test split; the AMI dev-split calibration;
the blank-token K3 and Wi-Fi drivers; the CI step that checks `reference/`.
On 29 Sep, the two real-model faster-whisper tests passed with
`PLAUD_HARNESS_FW_MODEL` set to the pinned `small.en` (2 passed in 67.01 s);
they still skip in a default run ([§4.1](#41-tests)).

### 6.5 Review coverage

- **Phase-3 layers (25 Sep).** An independent review produced 71 findings
  (23 major, 48 minor; 69 confirmed, 2 plausible, none refuted; IDs in the
  25 Sep stage-1 journal). Each was fixed or narrowed.
- **Phase-1 protocol code (28 Sep).** A review of the advertising, handshake,
  sealed path, transfer, file list, audio, peripheral and `serve.py` code
  reported 28 findings. The confirmed defects were fixed in `3fb2d27`, each
  with a test ([§5.2](#52-ble-emulator-and-legacy-handshake)). The same pass
  withdrew one R6-S1 claim (the second witness for the 512-byte header) and
  corrected four ledger passages (§6.3, §5.8, §5.9 and the opcode-138 entry).
- **Not reviewed line by line.** The Android rigs, and the 28 Sep V5 and
  calibration scripts, which were checked by their outputs (replay identity,
  re-assignment identity, md-eval) rather than by a separate reviewer.
- Most code, including the fixes, was written by delegated agents.

### 6.6 Documents that disagreed with the evidence (fixed 28 and 29 Sep)

A check of every document against the evidence on 28 Sep found 15 stale or wrong
passages. All were corrected at the source before this commit:

- `docs/evals.md` and `docs/pipeline.md` said V5 and the Docker run never happened.
- `r7/README.md` called the endpoint inventory "none exercised" and did not say the
  debug activities ship in every build of the app copy.
- `r7/r7-s14-wifi-real-sdk.md` called the blank token untested, and lacked its
  checker's six qualifications (now its last section).
- `docs/final-uncertainty-matrix.json` still said stopSync is sent only after a 5 s
  stall, and that no unknown lets the emulator assume anything.
- `docs/protocol-ledger.md` §9 kept U1's done bytecode walk as open and did not say
  U18 is merged into U1.
- `docs/reconstruction-log.md`, `PROJECT.md` and `r7/r7-s13-recording-pull.md`
  undercounted the R7-S13 runs and the gap-recovery runs (17 runs; 4 gap runs).
- `PROJECT.md` named the deprecated `scripts/fetch-sdk.sh` as the SDK fetcher.
- `README.md` and the progress report said four receiver behaviours were checked
  against the real SDK; `docs/v2-fault-matrix.md` lists six.
- `README.md` described all of `build/` as git-ignored.
- `docs/repository-map.md` (a 21 Sep snapshot) and `dump.md` (the raw research
  conversation, with claims later refuted) now carry notices at the top.

**29 Sep.** Two more checks ran before this version was committed. Each used
independent checkers, one per part, and a skeptic that tried to refute every
finding against the evidence.

- The first covered this document, the progress report, README, PROJECT.md and
  the V5 documents: 708 claims checked, 32 findings.
- The second covered the `embedding-cluster` write-ups, the day's code changes
  and the consistency across documents: 464 claims checked, 24 findings.

Every confirmed finding was fixed at the source, including passages older than
this pass: `docs/evals.md` and `docs/pipeline.md` still said V5 had not run, and
the gen-key comment in the three Kotlin drivers was wrong. The code review found
one defect in recorded metadata. `hyp.extra.assignment.rule` described the
`floor` tie-break whatever tie-break ran; it now names the one used
(`pipeline/base.py` `assignment_rule`), and the 28 Sep hypotheses keep the old
text. It also found robustness gaps in `scripts/calibrate-embedding-cluster.py`.
Its 29 Sep reports were checked to be complete: all 45 cover 18 meetings with no
error.

No other disagreement was known when this commit was made. §12 says how to re-check.

## 7. Corrections that changed the picture

| What was believed | What is true | How it was found | Recorded in |
|---|---|---|---|
| A transfer ends HEAD · DATA · TAIL (phase-1 frozen claim) | The SDK completes only on an EMPTY_PACKAGE sent before the TAIL. TAIL alone, or EMPTY_PACKAGE after the TAIL, never completes | R7-S13 runtime (runs 1/1b/2/9 and 7 vs 3/4b/8) | Ledger §5.8, §15; U17 |
| On a gap the SDK sends stopSync (original ledger); then, from bytecode at R7-S12: stopSync only after a 5 s stall | On a gap the SDK writes stopSync at once and restarts from its cursor about 35 ms later (run 6b: +1.720 s → +1.755 s) | R7-S13 runtime | Ledger §5.8; the matrix's resolved entry (corrected in `8b72dda`) |
| The opcode-10 byte is on/off, and 0 means "hotspot off" | `startWifiTransfer` sends 0 to open; `setDeviceWiFi(true)` sends 1; closing uses opcode 13. The emulator now opens on every opcode 10 and logs the byte as `mode`; its meaning is UNKNOWN. 4 tests fail against the old handler | R7-S14 (D1) | `emulator/plaudsim/profile.py`, ledger §7 |
| SSID = `Plaud` + last 4 serial digits | SSID comes from `BleDevice.getWiFiName()`: for project 881 below portVersion 20 it is `PLAUD` + last 4 (`PLAUD0001` at runtime); the other rule is only a fallback | R7-S14 (D2): bytecode + runtime | [`docs/wifi-transport.md`](wifi-transport.md), ledger §7 |
| Wi-Fi handshake token = BLE token, with the same padding | On the recovery path they differ (21 vs 13 characters at runtime). The Wi-Fi token uses `padEnd(…, 32, '0')`, which never truncates; k3 truncates | R7-S14 (D3); review T12 | same |
| "No Plaud endpoint was called" | The SDK, initialised by our drivers with a synthetic token, POSTed `partner/sdk/gen-key` to `platform-jp.plaud.ai` once per app start; 22 logged requests, 19 answered 401, 3 failed at DNS ([§4.8](#48-plaud-cloud-contact-in-the-runtime-logs)). Fixed by passing a blank token; verified offline for the pull driver | R7-S14 (D7), recount 25 and 28 Sep | README, PROJECT.md §8, progress report, R7 documents |
| Bulk transfer is Wi-Fi only | A complete BLE transfer path exists (opcode 28 → HEAD → type-2 DATA → TAIL) | 22 Sep audit | PROJECT.md §5.8 |
| Phase-1 readings C1–C15 | Among them: marker frames have no protocol-type byte (C1); `l3.version` is little-endian u24 (C2); file-list entries start at offset 11 (C4); scene and attribute were swapped (C6); syncTime timezone bytes are signed (C10); `w$c` is the request table (C12); k3 is byte-exact (C13); MTU gates the connection (C14) | 22 Sep bytecode re-audit | [`docs/reconstruction-log.md`](reconstruction-log.md) |
| AES-GCM is a BLE alternative | BLE is always ChaCha20-Poly1305; AES-GCM is Wi-Fi only (opcode-138 bit 3) | R7-S12 | Ledger §4.3 |
| `bleBind(protVersion=0)` showed an emulator gap | The SDK facade passes the l3 timezone byte as both arguments | R7-S12 runtime (tz 5 ⇒ protVersion 5) | Ledger §14 |
| The R7-S13 report: `PlaudPeripheral` defaults to task streaming and 4 ms pacing | The constructor default is inline and unpaced; `for_real_sdk()` now packages the rig's settings | Review T2 | [`r7/r7-s13-recording-pull.md`](../r7/r7-s13-recording-pull.md) |
| The early research dump: the recorder is ESP32-class; `live-agent` derives from livekit/agents; xiaozhi is reference firmware for the recorder | All refuted, with others, in a 38-item audit | Phase-2 content comparison | [`docs/final-product-reconstruction.md`](final-product-reconstruction.md) §6 |
| Evidence graph has 2 004 edges | 2 072 | Recount 24 Sep | PROJECT.md §8 |
| 11 wording and precision points in `docs/v5-results.md` | Corrected; no table, threshold or conclusion changed | Stage-2 verifier | [`docs/v5-results.md`](v5-results.md) §12 |
| `advertise()` sends what the SDK scans for | It sent the blob without a company id, in 42–44 bytes, over the 31-byte legacy limit. Since `3fb2d27` it sends the 29-byte layout the runtime rigs used | Phase-1 code review, 28 Sep | [§5.2](#52-ble-emulator-and-legacy-handshake); `tests/test_review_phase1.py` |
| An empty transfer is HEAD · EMPTY_PACKAGE · TAIL (emulator policy) | By the receiver's rules R2 and R9 that makes the SDK restart forever. The emulator now answers with one failing HEAD (status 1) | Phase-1 code review, 28 Sep | [`docs/v2-fault-matrix.md`](v2-fault-matrix.md) §5a |
| A file list longer than one notification reaches the SDK | Bumble truncated an oversized frame (a 25-entry list at MTU 255 arrived with 24 entries), and the SDK never completed getFileList. Frames are now sized to the MTU | Phase-1 code review, 28 Sep | same |
| R6-S1 had two witnesses for the 512-byte audio header | One was an inference; `h4_payload_spans` is relabelled, and `h4_bytecode_payload_spans` models what `h4.a` executes | Phase-1 code review, 28 Sep | [`r6-s1/README.md`](../r6-s1/README.md) |
| ASR non-repeatability was probably the unseeded temperature fallback (inferred, 25 Sep) | Consistent with it: CTranslate2 seeded before the model is built repeats sampled output across processes, and re-seeding inside a process does not. With seeding, a fresh process reproduced EN2002a exactly | 28 Sep | [`docs/pipeline.md`](pipeline.md) §11.8 |
| sherpa's default `cluster_threshold` (0.5) is a usable default | On AMI it finds 35–108 clusters for 3–4 speakers. The dev split selects 1.15 | Dev-split calibration, 28 Sep | [`docs/v5-test-split.md`](v5-test-split.md) §3 |
| The 4-meeting V5 subset might not represent the split | It did: hinted DER 0.6557 and cpWER 0.8533 on 4 meetings; 0.6396 and 0.8567 on 16 | Full split, 28 Sep | same, §6 |
| The Android emulator cannot join the pen's hotspot, so the real SDK's Wi-Fi transfer needs a physical phone or access point (R7-S14) | The emulator's network simulator offers any SSID and passphrase (`netsimd --wifi`). With `PLAUD0001`/`10000001`, and Android's network-request dialog approved by a scripted tap, the SDK joined and completed the transfer byte-exact | 29 Sep, R7-S15 | [`r7/r7-s15-evidence/`](../r7/r7-s15-evidence/README.md) |
| sherpa's attribution was the best available (V5, 28 Sep) | Sherpa's speaker count with ECAPA turns cuts confusion from 0.342 to 0.107 and cpWER from 0.8428 to 0.5132 | Dev split, then test, 29 Sep | [`docs/v5-test-split.md`](v5-test-split.md) §7.1 |
| pyannote's diarization is out of reach (gated models; 28 Sep) | With the owner's Hugging Face token it ran: DER 0.1295 alone, cpWER 0.3477 with the transcript, and its collar-0 DER (17.07 %) matches its published 17.0 % | 29 Sep | [`docs/v5-test-split.md`](v5-test-split.md) §7.2 |

## 8. Rules, and how they held

| Rule | How it held | Evidence |
|---|---|---|
| `reference/**` is immutable | Held for tracked content: 33/33 repositories clean and at their pins on 28 and 29 Sep, and CI compares the tree before and after the suite. One lapse in untracked files: on 28 Sep an unguarded `python -c` that imported Bumble wrote `__pycache__` directories into `reference/upstream/bumble`. They were git-ignored, so `git status` did not show them. They were removed, and every Python call now sets `PYTHONDONTWRITEBYTECODE=1`. Still present and ignored there: the documented editable install's `bumble.egg-info/` and `bumble/_version.py` (23 Sep), and a `tests/.pytest_cache/` from 21–22 Sep | [§4.3](#43-reference-corpus); `.github/workflows/tests.yml`; `git -C reference/upstream/bumble status --porcelain --ignored` |
| `javap` beats jadx | Held; ledger §11 lists the traps | [`docs/protocol-ledger.md`](protocol-ledger.md) |
| No Plaud device, account or credential | Held. None was used | – |
| Identifiers are synthetic | Held for what this document checked: id `SYNTH-HIST-0001`, serial `8810000001`, session `1700000000`, a synthetic fixture file. The template app's `local.properties` holds a placeholder token per [`r7/README.md`](../r7/README.md); it was not opened for this document | R7 reports |
| Evidence classes stay distinct | Held in the protocol and product documents; harness choices carry `HARNESS_POLICY` | Ledger, product ledger |
| SDK binaries are not redistributed | Held. No commit in the history contains an `.aar`, `.dex`, `.apk`, `.so` or `.class` file; its only jars are the two Gradle wrappers (checked 29 Sep with `git log --all --name-only`). Near miss, 28 Sep: a `.gitignore` edit that published `build/v5/` and `build/v6/` also un-ignored the nested Android build folders, one of which holds a dexed copy of the SDK (`r7/android-app/app/build/.../0_plaud-sdk-runtime.jar`). A fact-checker caught it before any commit; the rule `*/**/build/` now keeps every nested build folder ignored, and the would-be commit was scanned for jar/dex/zip/class content (none) | `.gitignore`; `git check-ignore -v` on the jar |
| No contact with Plaud's cloud | **Not held.** We never called a Plaud endpoint from our own code. The official SDK did, because our drivers gave it a synthetic token. It sent one `gen-key` request per app start, logged in all 22 kept R7-S13 and R7-S14 logs that cover app start; R7-S14 run 1's truncated log does not cover it, so that request is inferred. 19 of the 22 logged requests were answered 401 and 3 failed at DNS. No response data was used. The 401 responses (status, Cloudflare headers, `x-request-id`) are archived in 14 of the 17 R7-S13 logs, which are public in `70249ba`. On 25 Sep the three drivers were changed to a blank token and the pull driver was re-run offline. On 28 Sep the K3 and Wi-Fi drivers were re-run the same way: no request in any full logcat. On 29 Sep (R7-S15) the Wi-Fi driver ran with the phone joined to a simulated network; DNS and TCP egress were cut on the host side, and again no request was logged. The template's own UI path still passes the `local.properties` token ([§9](#9-how-to-reproduce)) | [§4.8](#48-plaud-cloud-contact-in-the-runtime-logs); [`r7/r7-s14-wifi-real-sdk.md`](../r7/r7-s14-wifi-real-sdk.md) D7; `r7/android-app/app/src/main/java/com/plaud/template/managers/DeviceManager.kt:177-181` |
| Downloads of Plaud's public material (outside the rule above) | On 23 Sep, without authentication, the project downloaded 43 files from `docs.plaud.ai` (including the `llms.txt` index and 5 OpenAPI specs) into `build/docs-plaud-ai/`. It also fetched two official npm packages, `@plaud-ai/cli` 0.3.14 and `@plaud-ai/mcp` 0.3.13, from registry.npmjs.org into `build/npm-plaud/`. These are public downloads, not API calls. Both folders are git-ignored | `build/docs-plaud-ai/manifest.json`, `build/npm-plaud/manifest.json`, [`docs/source-map.md`](source-map.md) |

## 9. How to reproduce

Setup (Python 3.11, git; about 2.5 GB of reference repositories):

```bash
./scripts/fetch-references.sh      # 33 pinned repositories into reference/ (git-ignored)
./scripts/build-evidence.sh        # decompile the SDK into build/evidence/; checks the AAR sha256
python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt
.venv/bin/pip install -r requirements/models.txt   # optional: whisper-sherpa and Piper (piper-tts is GPL-3.0-or-later)
```

Tests and verifiers:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120
for v in r5-s*/verify_*.py r6-s*/verify_*.py; do (set -o pipefail; cd "$(dirname "$v")" && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python "$(basename "$v")" | tail -1; echo "  exit=$? $v"); done
```

Topology:

```bash
bash scripts/local-up.sh           # emulator + mock cloud + job, no Docker
./scripts/compose-smoke.sh         # the same under Docker; exit 2 if Docker is absent
```

V5-style measurement. It runs on macOS only: the script calls
`/usr/bin/time -l` and `sysctl`. It needs `requirements/models.txt`. The
`inputs` stage fetches and sha256-checks the model weights, the 4 pinned AMI
test meetings and the Piper voice, then renders the Piper meetings (piper-tts,
GPL-3.0-or-later). The rescore-only line works only where the git-ignored
references in `data/corpora/ami/meetings/` and `build/synthetic/piper/` already
exist; on a fresh clone, run the `inputs` stage first.

```bash
.venv/bin/python -m pipeline fetch-models
./scripts/run-v5.sh                               # all stages
V5_STAGES=score,gates,summary ./scripts/run-v5.sh # rescore saved hypotheses only
```

The rescore line rewrites `build/v5/reports/`, `build/v5/gates/`,
`build/v5/summary.md` and `build/v5/summary.json` in place. Since 28 Sep those
files are meant to be published. To rescore without touching them, copy
`build/v5/` elsewhere and point `V5_OUT` at the copy.

The full test split and the calibrations. The complete command lines are in
[`docs/v5-test-split.md`](v5-test-split.md) §11:

```bash
./scripts/calibrate-sherpa.sh                        # 18 dev meetings: threshold and tie-break (build/v5-calib/)
V5_OUT=build/v5-test V5_AMI_ROOT=data/corpora/ami/test V5_AMI_MEETINGS="<16 ids>" ... ./scripts/run-v5.sh
./scripts/crosscheck-md-eval.sh build/v5-test data/corpora/ami/test
./scripts/crosscheck-goreplay.sh                     # mock cloud export replayed by gor
# embedding-cluster needs torch and SpeechBrain; keep them out of .venv, and never install reference/ into it
python3.11 -m venv .venv-torch && .venv-torch/bin/pip install -r requirements/all.txt torch==2.14.0 torchaudio==2.11.0 speechbrain==1.1.1   # the versions used
PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python scripts/calibrate-embedding-cluster.py              # dev split
PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python scripts/calibrate-embedding-cluster.py --test-root data/corpora/ami/test
```

Runtime rig (Android SDK, an API-34 AVD, JDK 17, Gradle 8.2; full steps in
[`r7/r7-s12-k3-runtime-capture.md`](../r7/r7-s12-k3-runtime-capture.md)
"Reproduce" and [`r7/r7-s14-evidence/blank-token-offline-check/run.sh`](../r7/r7-s14-evidence/blank-token-offline-check/run.sh)):

```bash
cp reference/plaud-org/plaud-sdk-public/android/app/libs/plaud-sdk.aar r7/android-app/app/libs/   # git-ignored; never commit it
[ -f r7/android-app/local.properties ] || printf 'sdk.dir=%s\n' "$ANDROID_HOME" > r7/android-app/local.properties   # no PLAUD_* token keys
( cd r7/android-app && ./gradlew :app:assembleDebug )          # add --offline only once the Gradle cache is warm
emulator -avd <avd> -no-window -no-audio -no-snapshot -no-boot-anim &
adb -s emulator-5554 wait-for-device
until [ "$(adb -s emulator-5554 shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 5; done
adb -s emulator-5554 install -r r7/android-app/app/build/outputs/apk/debug/app-debug.apk
adb -s emulator-5554 shell pm clear com.plaud.template          # the SDK skips a download if the output exists
for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do adb -s emulator-5554 shell pm grant com.plaud.template android.permission.$p; done
adb -s emulator-5554 shell svc wifi disable; adb -s emulator-5554 shell svc data disable   # cut egress
adb -s emulator-5554 shell svc bluetooth enable
# On macOS, Bumble finds netsim through $TMPDIR/netsim.ini: run this from the shell
# that started the emulator, or export TMPDIR as in the R7-S12 "Reproduce" section.
PYTHONDONTWRITEBYTECODE=1 PULLCAP_CAPTURE=/tmp/capture.json .venv/bin/python r7/pull_capture_peripheral.py > /tmp/peripheral.log 2>&1 &
until grep -q PULLCAP_PERIPHERAL_READY /tmp/peripheral.log; do sleep 1; done
adb -s emulator-5554 logcat -c
adb -s emulator-5554 shell am start -n com.plaud.template/.debug.PullCaptureActivity --es id "SYNTH-HIST-0001" --es mode export
sleep 65; adb -s emulator-5554 logcat -d | grep -E 'PULLCAP_(BIND|EXPORT_DONE|EXPORT_ERROR)|gen-key'
```

**Blank-token rule.** The three drivers pass `""` to
`PlaudDeviceAgent.initSDK`. Never change that to a non-blank token: with one,
the SDK POSTs `gen-key` to Plaud's server at every app start. The template's
own UI path (`DeviceManager.kt:177-181`) still passes the token from
`local.properties` or from Settings → Replace token. Keep the `PLAUD_*` token
keys out of `local.properties`, and launch only the debug activities. Keep the
emulated phone offline as a second guard.

## 10. Where things are

| Kind of fact | Where it lives |
|---|---|
| Standing status, decisions, milestones | [`PROJECT.md`](../PROJECT.md) |
| Dated narrative of the work, review and corrections | [`docs/progress-report.md`](progress-report.md) |
| The Bluetooth protocol, field by field, with evidence | [`docs/protocol-ledger.md`](protocol-ledger.md) |
| Mechanically extracted bytecode facts | [`docs/evidence-digest.json`](evidence-digest.json); `scripts/extract_evidence_digest.py` |
| What earlier work got wrong | [`docs/reconstruction-log.md`](reconstruction-log.md) |
| Open questions | [`docs/final-uncertainty-matrix.json`](final-uncertainty-matrix.json) |
| Phase-1 closure | [`docs/final-closure-report.md`](final-closure-report.md), [`docs/final-architecture.md`](final-architecture.md) |
| Milestone spikes R4–R6 and their verifiers | `r4-s*/`, `r5-s*/`, `r6-s*/` (each has a README; summary in [§5.14](#514-milestone-spikes-r4r6)) |
| Real-SDK runs and raw logs | [`r7/`](../r7/README.md), [`r7/r7-s13-evidence/`](../r7/r7-s13-evidence/), [`r7/r7-s14-evidence/`](../r7/r7-s14-evidence/) |
| Plaud's cloud surfaces | [`r7/cloud-endpoint-inventory.md`](../r7/cloud-endpoint-inventory.md), [`docs/architecture/cloud.md`](architecture/cloud.md) |
| The product above the radio | [`docs/final-product-reconstruction.md`](final-product-reconstruction.md), [`docs/product-ledger.md`](product-ledger.md), [`docs/architecture/`](architecture/), [`docs/source-map.md`](source-map.md), [`docs/evidence-graph.json`](evidence-graph.json) |
| Harness layers | [`docs/v2-fault-matrix.md`](v2-fault-matrix.md), [`docs/wifi-transport.md`](wifi-transport.md), [`docs/generator.md`](generator.md), [`docs/evals.md`](evals.md), [`docs/pipeline.md`](pipeline.md), [`docs/mockcloud.md`](mockcloud.md), [`docs/integration.md`](integration.md), [`docs/compose.md`](compose.md) |
| Real-model numbers | [`docs/v5-test-split.md`](v5-test-split.md) (full test split, 28 Sep); `build/v5-test/` and `build/v5-calib/` (committed evidence; see their `README.md`); [`docs/v5-results.md`](v5-results.md) (25 Sep subset); `build/v5/` (committed evidence; see `build/v5/README.md`) |
| Docker run log | `build/v6/compose-smoke-2026-09-25.log`, and `build/v6/docker-env-2026-09-28.txt` for the VM size (both committed evidence) |
| Reference corpus | [`docs/SOURCES.md`](SOURCES.md), [`docs/reference-pins.txt`](reference-pins.txt); `reference/` (git-ignored) |
| Skip gates and CI | `tests/conftest.py`, `.github/workflows/tests.yml`, `.github/workflows/compose.yml`, `.github/workflows/evals.yml` |
| Scripts (`scripts/`) | `fetch-references.sh` (the 33 pinned repositories); `build-evidence.sh` (decompiles the SDK; checks the AAR sha256); `fetch-sdk.sh` (DEPRECATED shim); `fetch-datasets.sh` (`--list`, `--sample`, `--all`; never bulk-downloads by default); `make-synthetic-set.sh` (the CI `tests` workflow uses it); `generate_r6s2_fixture.py` (writes the two `tests/fixtures/r6s2_*.ogg` and their manifest); `extract_protocol.py` (writes `docs/protocol.json`); `extract_evidence_digest.py`; `build_evidence_graph.py`; `compose-smoke.sh`; `local-up.sh`; `run-v5.sh` (V5 runs, any pinned AMI set); `calibrate-sherpa.sh` (sherpa threshold and tie-break on the dev split); `calibrate-embedding-cluster.py` (the same for `embedding-cluster`, and its test-split hypotheses); `crosscheck-md-eval.sh` (DER against NIST md-eval); `crosscheck-goreplay.sh` and `tools/http_recorder.py` (the mock's export replayed by GoReplay) |
| Topology helpers | `docker/job.sh`; `docker/probe.py` (readiness probes); `docker/cloud_roundtrip.py` (the job's mock-cloud round trip); `docker/compose.host-ports.yml` (optional override that publishes the services on host loopback) |
| Runtime-rig devices | `r4-s3/plaud_netsim_peripheral.py` (R4-S3); `r7/k3_capture_peripheral.py` (R7-S12); `r7/pull_capture_peripheral.py` (R7-S13 and the offline check); `r7/wifi_capture_device.py` (R7-S14). The last one's default capture path, `r7/wifi-capture.json`, is not git-ignored ([`r7/README.md`](../r7/README.md)) |
| Test support and fixtures | `tests/conftest.py` (skip gates); `tests/fault_support.py` (the V2 receiver model); `tests/protocol_trace.py`, `sealed_support.py`, `support.py`, `wifi_support.py`; `tests/fixtures/r6s2_16k_mono.ogg`, `r6s2_16k_stereo.ogg` and `r6s2_manifest.json` (sha256 pins); `tests/fixtures/ami_tiny/` (a hand-made miniature, not AMI data) |
| Per-message protocol records | `docs/fixtures/` (6 JSON files: device lifecycle, file sync, handshake, post-bind getState, getStorage, syncTime) |
| Inputs to the evidence graph | `docs/product-evidence/` (8 JSON files). `scripts/build_evidence_graph.py` reads `agent-findings-2026-09-23.json` from it |
| Early and superseded records (all public in `70249ba`) | `docs/forensics-report.md` (21 Sep inventory); `docs/protocol-spec.md` and `docs/protocol.json` (both marked "SUPERSEDED IN PART"; the JSON is the class census from `scripts/extract_protocol.py`); `docs/repository-map.md` (21 Sep; says the code directories are empty); `dump.md` (the raw research chat, with claims refuted in [`docs/final-product-reconstruction.md`](final-product-reconstruction.md) §6) |
| Dependencies | `requirements/all.txt`, `requirements/ci.txt` (`-r all.txt`), `requirements/models.txt` (optional), and per-layer `evals.txt`, `faults.txt`, `generator.txt`, `mockcloud.txt`, `pipeline.txt`, `wifi.txt`. In `all.txt`, `cryptography`, `grpcio` and `protobuf` carry no version pin, and `websockets` is a range (`>=17,<18`) |
| Code size (lines of Python, 30 Sep) | `emulator/plaudsim/` 7 011; `generator/` 4 130; `evals/` 3 993; `pipeline/` 5 862; `mockcloud/` 2 766; tests 25 332 in 81 Python files (75 `test_*.py`); Kotlin drivers 678 (`r7/android-app/app/src/debug/`) |

## 11. What is next

In order. Each item names what blocks it and what would unblock it.

| # | Item | Blocked on | Unblocked by |
|---|---|---|---|
| 1 | Keep CI green on all three platforms | – | The "Annotate failures" step publishes any failing test through the public API ([§5.13](#513-ci)). An install failure (as on `3e46ad9`) needs the job log, which needs a token |
| 2 | Decide whether to keep absolute local paths in the published V5 evidence | Owner's call | 152 files in `build/v5/` contain `/Users/…` paths from the run; they are left byte-identical as produced. `build/v5-test/` and `build/v5-calib/` carry the same kind of paths |
| 3 | Close the rest of the V5 gap | Engineering, not blocked | Done 29 Sep: `whisper-sherpa-ecapa` (cpWER 0.5132) and `faster-whisper+pyannote` (cpWER 0.3477, confusion 0.012). What remains is the transcript: its WER is 0.3221 with `small.en`. Next: a larger Whisper model, chosen on the dev split (hours of CPU decoding here), then re-assigning its words to pyannote's turns |
| 4 | pyannote-based systems on the full split | Done 29 Sep for `pyannote-audio` and `faster-whisper+pyannote`; `whisperx` is not installed | Install whisperx in a separate environment if wanted; its alignment models are a large download |
| 5 | A V5 speed figure | This 8 GB M1 was always shared | One run with nothing else on the machine |
| 6 | Wi-Fi with the real SDK over the air | Done on 29 Sep through `adb forward` (R7-S15). The bytes have not crossed a Wi-Fi link | Route our device over netsim's network (the host is reached through slirp), or a physical phone and a real SoftAP |
| 7 | Device questions (U1, U15, U17, U2, U22 and the others in [§6.1](#61-open-questions-24)) | No hardware | One passive BLE scan capture and one transfer capture of any legacy Plaud recorder |
| 8 | Real firmware's audio shape (U20) | No real recording | One recording the operator lawfully owns |
| 9 | iOS SDK at runtime | No Xcode | Xcode with a simulator, plus a Bluetooth bridge |
| 10 | Piper voice licence | Research-only base-voice licence | An owner's decision before redistributing Piper audio |
| 11 | Encrypted protocol with the real SDK (CRED-1) | Cloud-issued key material | Legitimate partner credentials; deliberately not pursued |

Done since the 28 Sep morning version of this list: the blank-token K3 and
Wi-Fi re-runs; `evals` scoring DER/JER when meeteval refuses; sherpa
threshold and tie-break calibration; repeatable ASR; V5 on the full test
split; Piper metadata in `meeting.json`; the phase-1 code review.

## 12. How to keep this document true

Update rule: change a number only from a fresh output of the command below, and
change the "as of" date at the top when you do. If a number cannot be
re-measured, keep it and keep its date. Do not round.

| Number | Command | Where it goes |
|---|---|---|
| Test totals | `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120 -rs` (last line, verbatim) | §4.1 |
| Tests per area | `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ --collect-only -q -p no:cacheprovider \| grep '::' \| sed 's/::.*//' \| sort \| uniq -c` | §4.1 |
| Verifiers | the loop in §9 | §4.2 |
| Reference pins | for each `reference/*/*/`: `git rev-parse HEAD` equals its line in `docs/reference-pins.txt`, and `git status --porcelain` is empty; `shasum -a 256` of the AAR | §4.3 |
| V5 numbers, 25 Sep subset | `V5_STAGES=score,gates,summary ./scripts/run-v5.sh`, then `build/v5/summary.md`. This rewrites `build/v5/` in place; set `V5_OUT` to a copy to leave it alone (§9) | §4.4 |
| V5 numbers, full test split | the rescore line in [`docs/v5-test-split.md`](v5-test-split.md) §11 with `V5_STAGES=score,gates,summary`, then `build/v5-test/summary.md`; `./scripts/crosscheck-md-eval.sh build/v5-test data/corpora/ami/test` | §4.4 |
| Calibrations | `build/v5-calib/summary.md`, `build/v5-calib/tiebreak.md`, `build/v5-calib/embedding-cluster/selected.json` | §4.4, §5.10 |
| Real-model ASR tests | `PLAUD_HARNESS_FW_MODEL=$PWD/data/models/faster-whisper/small.en PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_pipeline_adapters.py -k real_faster_whisper -p no:cacheprovider` | §6.4 |
| V6 timings | `./scripts/compose-smoke.sh` (lines `build_s`, `up_wait_s`, `job_s`) | §4.5 |
| R7-S13 hashes | `cd r7/r7-s13-evidence && shasum -a 256 -c SHA256SUMS` (60 archived files); `grep -a -h PULLCAP_ r7/r7-s13-evidence/logcat-*.log \| grep -o 'sha256=[0-9a-f]*' \| sort \| uniq -c`; `shasum -a 256 tests/fixtures/r6s2_16k_mono.ogg` | §4.6 |
| Fault matrix | after a suite run, count `outcome` values in `build/v2-fault-matrix.json` | §4.7 |
| Cloud contact | `grep -a -l gen-key r7/r7-s1[34]-evidence/*.log \| wc -l`; `grep -a -l 'Status: 401' …`; `for f in r7/r7-s14-evidence/blank-token-drivers-check/*.gz; do gzip -dc $f \| grep -a -c gen-key; done` | §4.8 |
| Open questions | count `status` values in `docs/final-uncertainty-matrix.json` `unresolved` | §6.1 |
| GitHub CI | `curl -s "https://api.github.com/repos/ORION2809/PLAUDE_EMULATOR_REV-ENG/actions/runs?per_page=5"` | top, §5.13 |
| Working-tree state | `git status --porcelain \| wc -l`; `git status --porcelain -uall \| grep -c '^??'`; `git log --oneline` | top |
| SDK copies left unignored | `git check-ignore -v r7/android-app/app/build/intermediates/external_file_lib_dex_archives/debug/0_plaud-sdk-runtime.jar` (exit 1 means not ignored) | §8 |

When a rung, a component status or a correction changes, update §2, §3 and
§7 in the same edit. If a document is found to disagree with the evidence,
list it in §6.6 until it is fixed at the source.
