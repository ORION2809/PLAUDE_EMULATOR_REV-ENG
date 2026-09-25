# Plaud Harness Progress

**Progress report · plaud-harness**

| | |
|---|---|
| Period | 21–24 September 2026 |
| State as of | 24 September 2026 |
| Repository | `~/Desktop/plaud-harness`, branch `master` |
| Compiled by | Claude Code from the working tree, tests and run logs |

## Contents

1. [Summary](#1-summary)
2. [Validation rungs](#2-validation-rungs)
3. [Timeline](#3-timeline)
4. [Phase 1 · protocol reconstruction](#4-phase-1--protocol-reconstruction)
5. [Phase 2 · product reconstruction](#5-phase-2--product-reconstruction)
6. [Phase 3 · the remaining harness layers](#6-phase-3--the-remaining-harness-layers)
7. [The real-SDK pull (R7-S13)](#7-the-real-sdk-pull-r7-s13)
8. [What was wrong, and has been corrected](#8-what-was-wrong-and-has-been-corrected)
9. [How the work was checked, and what was not](#9-how-the-work-was-checked-and-what-was-not)
10. [Numbers](#10-numbers)
11. [Open items](#11-open-items)
12. [Rules kept throughout](#12-rules-kept-throughout)
13. [Repository state](#13-repository-state)
14. [Where things are](#14-where-things-are)

---

## 1. Summary

The project set out to build a hardware-free test harness for Plaud voice
recorders: a software device that Plaud's own app SDK will talk to, synthetic
meetings with exact ground truth, an evaluation harness, and a mock of the
cloud. All four layers now exist as tested code. The official Android SDK has
been run against the emulated device and pulled a recording byte-for-byte. No
real Plaud device and no real Plaud cloud service has been used at any point,
so nothing here proves how real hardware behaves.

| At a glance | |
|---|---|
| Automated tests | **978 pass** (301 existed after phase 1) |
| Protocol verifiers | **12 of 12 pass** (one needed a fix today) |
| Reference corpus | **33 of 33 pinned**, unmodified, AAR hash matches |
| Real hardware used | **None**: no device, no cloud call, no credential |
| Committed | **Nothing new**: one scaffold commit; the rest is uncommitted |

What changed the picture most this week:

- **The real SDK pulled a file from the emulator.** On an Android emulator,
  the unmodified Plaud AAR listed the served recording and downloaded it
  through both public download paths, and the bytes it handed the app hash
  identically to the served file. This moved validation rung V3 from partial
  to done for the Bluetooth transfer path.
- **That run showed our own protocol model was wrong in one important place.**
  The emulator ended a transfer with HEAD, data and TAIL frames. The real
  client never finishes on that sequence. It finishes only on a separate
  end-of-data frame (EMPTY_PACKAGE) sent before the TAIL. Our earlier tests
  passed because they used our own Python client model, which did not need
  that frame. This is the clearest example in the project of why tests against
  our own model are not enough.
- **The remaining layers were written**: a fault-injection matrix, the Wi-Fi
  transfer emulator, the synthetic meeting generator, the evaluation harness,
  the pipeline interfaces with baseline systems, the mock cloud, cross-layer
  integration tests and a compose topology.

What is not true yet:

- No speech recognition or diarization system has been evaluated (rung V5).
  There is no AMI data or model on this machine, and the generator's speech is
  pseudo-speech that no recogniser can transcribe.
- The Docker compose path has never been run, because Docker is not installed
  here (rung V6).
- The encrypted protocol used by newer devices (port version 20 and above) has
  only been exercised with synthetic keys, never with the real SDK.
- The iOS SDK has never been executed. The Wi-Fi emulator has never been run
  against the real SDK.
- Most of the phase-3 code was written by delegated agents, and a planned
  independent review stage did not run. Section 9 explains what checking was
  done instead.

## 2. Validation rungs

PROJECT.md defines six pass/fail rungs. This table states what each one shows
and, as importantly, what it does not.

| Rung | Claim | Status | What it shows | What it does not show |
|---|---|---|---|---|
| V1 | Emulator is discovered and completes a session | **Done** | A Bumble central scans, parses the advertisement with the SDK's own rules, connects, exchanges MTU, discovers services and runs the control and file-sync messages. | The central is our Python code. It passed while the transfer sequence was still wrong, which is why V3 matters. |
| V2 | Survives fault injection without corrupting a transfer | **Done** | 61 matrix rows over a virtual Bluetooth link: drops, duplicates, reordering, truncation, wrong session, disconnects, stop mid-transfer, MTU 23–517, both subscription modes and more. Each row ends byte-exact or with a detected failure. | The receiving side is a Python model of the SDK's receiver derived from bytecode. Only four behaviours in it were confirmed against the real SDK. One row is a documented corruption risk and payload bit-flips are undetectable, because data frames carry no checksum. |
| V3 | Plaud's own SDK connects and pulls a recording | **Done** (Android BLE, legacy protocol) | The real AAR bound, listed the file and pulled it through `syncFile` and `exportAudio(OPUS)`, byte-exact. Its gap recovery converged byte-exact in all four gap runs. | Nothing about real devices. Not iOS, not the encrypted protocol, not Wi-Fi, and not our own client binding to real hardware, which needs cloud-issued keys. |
| V4 | Synthetic ground truth round-trips at DER 0 | **Done** | The reference annotation scores DER and JER 0.0 against itself and against one rebuilt from the sample-level activity mask. End to end, generator → oracle pipeline → evals gives DER, JER, cpWER and tcpWER all 0.0. Perturbations raise every metric. | This validates plumbing and metric direction only. The oracle returns the ground truth by construction, so zero is expected. It says nothing about any real system's quality. |
| V5 | A full pipeline hits target cpWER/DER on AMI | **Not done** | Metrics, gates, pipeline interfaces, baseline systems and the AMI procedure exist. | No AMI audio, no ASR model and no converter from AMI's annotation format. The gate thresholds are uncalibrated placeholders. No number exists. |
| V6 | `docker compose up` is live in under 60 s | **Partial** | Compose file, three Dockerfiles, healthchecks and a smoke script are written. The same topology on the local Python environment came up in 0.96 s and ran its job in 6.7 s (re-measured today). | Docker is not installed here, so no image has been built and compose has never run. Build time and wheel availability are unknown. |

## 3. Timeline

| Date | Work | Main output |
|---|---|---|
| 21 Sep | Scaffold, corpus fetch | 33 source repositories fetched and commit-pinned; PROJECT.md; the only commit, `833f531`. |
| 22 Sep | Protocol re-audit from first principles; first runtime spikes | `javap` bytecode adopted as ground truth; mechanically extracted evidence digest; milestones R1–R4; the real SDK reached the Bluetooth handshake on an Android emulator (R4-S3). |
| 23 Sep | Protocol audits, synthetic encrypted path, audio; phase-1 closure; phase-2 product reconstruction; first real-SDK pull runs | R5–R7-S12 (handshake frame byte-exact at runtime, opcode 8 found); closure report; 10 architecture documents, product ledger, source map, evidence graph; R7-S13 runs 1–10. |
| 24 Sep | Remaining harness layers; confirmation runs; this report | R7-S13 runs 11–13 on the shipped emulator code; V2 matrix, Wi-Fi emulator, generator, evals, pipeline, mock cloud, integration tests, compose. |

## 4. Phase 1 · protocol reconstruction

### Method

- The official Android SDK archive (`plaud-sdk.aar`, SHA-256 `041a6f88…aedce`)
  was decompiled. `javap` output is treated as ground truth. `jadx` output is
  used only for orientation, because it misrenders overloads, signedness and
  control flow in documented ways.
- Protocol facts were extracted by script into `docs/evidence-digest.json`.
  Tests assert the emulator against that digest rather than against
  hand-written fixtures.
- The iOS `.swiftinterface` files supply names. The 33 reference repositories
  are pinned by commit and never modified.

### What is established

- The GATT layout, the advertisement parse rules, and the control messages:
  `getState`, `syncTime`, `getStorage`, battery, file list, file sync, stop,
  delete and resume.
- The legacy handshake from the device side. Its token derivation was
  confirmed byte-for-byte at runtime: the SDK's own first handshake frame,
  captured in four runs, matched our model exactly (R7-S12).
- The encrypted handshake and ChaCha20-Poly1305 framing for port version 20
  and above, as structure. It has only been exercised with synthetic keys.
- Opcode 8 (21 device settings, found at runtime), opcode 138 (capability
  exchange), the Wi-Fi transfer design, the OTA messages and the audio format
  contracts the SDK enforces.
- A facade quirk: the public `bleBind` callback passes the timezone byte as its
  "protocol version" argument. This was proven at runtime.

### What is not established

The uncertainty register holds **24 open questions**, plus one entry that
turned out to be a duplicate. Fifteen need a real device, two need credentials
the project does not hold, one needs a real recording, and six have no known
source. Examples: the storage units in `getStorage`, which advertising branch
real hardware uses, what the end-of-transfer code values mean, and which of
four audio file shapes real firmware writes.

One block is structural. Binding a client *we* wrote to *real* hardware needs
an RSA key pair, a serial-number signature and a token that only Plaud's cloud
issues. The reverse direction, Plaud's client binding to our device on the
legacy path, needs none of that. That is why the emulator can work while a
custom client cannot.

> **Note.** Phase 1 closed with the verdict "reconstruction complete with
> external-evidence blockers". One of its frozen claims, the transfer
> sequence, was later shown incomplete by the real SDK (section 7).
> "Complete" should be read as "complete as far as static evidence reached".

## 5. Phase 2 · product reconstruction

The Bluetooth layer was frozen, and the rest of the Plaud product was mapped
from the assembled corpus: mobile app, cloud, recording lifecycle, AI
pipeline, memory, search, subscription, device lifecycle, firmware as seen
from outside, and the web app.

### Inputs and method

- The 33 pinned repositories. Also 43 pages of Plaud's public developer
  documentation and two published Plaud npm packages, all fetched read-only
  and archived with hashes under `build/`.
- Sixteen extraction agents produced structured findings with file-and-line
  citations and evidence classes. Fork lineage was decided by comparing file
  contents against the upstream snapshot, not by searching for the word
  "plaud", which misled twice.
- Outputs: ten architecture documents, a product ledger, a source map, and an
  evidence graph built by script. The graph holds 397 claims, 221 unknowns,
  138 contradictions and 2 072 edges.

### Main findings, with their limits

- The partner product (Plaud's embedded SDK offering) is described almost
  completely by Plaud's own published code and documents.
- The recorder runs on a TinnoTech platform. The SDK recognises MediaTek and
  Nordic radio vendor codes and contains no ESP32 strings. The two ESP32
  repositories in Plaud's organisation are voice-agent prototypes on
  Espressif development boards, not recorder firmware.
- No server-side search, retrieval or vector store appears on any observed
  surface. The memory libraries appear only in an experimental voice-agent
  repository, where the API side is hard-coded to a mock.
- The consumer cloud is visible only through four community clients that
  replay the web app's traffic. Those findings are classed as
  observed-by-others, never as verified.
- The 38 claims in the original research dump were audited. The ESP32 claim
  and the claim that `live-agent` derives from LiveKit's agent framework were
  refuted. Several others were rated partial or unverifiable.

*Limits: no Plaud cloud endpoint was called and no consumer app binary was
examined. The evidence graph links sources to claims by matching citation
text, so some edges may be misattributed.*

## 6. Phase 3 · the remaining harness layers

Everything PROJECT.md listed as "not started" or partial was implemented on
23–24 September. The table gives each component's state and main limitation.

| Component | What exists | Tests | Main limitations |
|---|---|---:|---|
| BLE emulator corrections | End-of-data frame emitted before TAIL by default. Transfers can stream from a paced task that a restart or stop cancels. | 15 | The frame's code value (0) and the 4 ms pacing are harness choices. What real firmware sends is unknown. |
| V2 fault matrix | 61 rows, plus a bytecode-derived SDK receiver model corrected to match the runtime runs. | 109 | Model-scored. Some faults appear twice, once unpaced and once paced, so there are fewer than 61 distinct faults. |
| Wi-Fi transfer emulator | Message codecs; the device side as a WebSocket client; a phone-side test double rebuilt from bytecode; sessions encrypted with both AEAD ciphers. | 135 | Never run against the real SDK. No access-point or DHCP layer. Opening Wi-Fi over Bluetooth does not yet start the Wi-Fi device. 74 of the tests pin constants to bytecode. |
| Generator (layer 2) | Scenarios, a model-free formant "voice", room simulation with pyroomacoustics, exact word timings, RTTM/STM references, and Ogg/Opus output in the device's shapes. | 82 | The speech is not intelligible, so it is unusable for word-error evaluation. Real text-to-speech backends are import-guarded and have no weights here. |
| Evals (layer 3) | DER, JER, WER, cpWER and tcpWER via pyannote.metrics, jiwer and meeteval; gates; a batch runner; a CI workflow file. | 137 | Gate thresholds are placeholders. The CI workflow has never run. The count includes parametrised cases from about 65 test functions. |
| Pipeline | Interfaces; an oracle; a deterministic perturbed oracle; a model-free energy-VAD plus clustering diarizer; adapters for faster-whisper, pyannote.audio and whisperx. | 104 | The adapters are untested because no models are installed. The baseline diarizer merges the generator's two voices unless told the speaker count; with the hint its DER is about 0.29. |
| Mock cloud (layer 4) | A FastAPI mock of the partner contract: identity chain, device binding, SDK key/sign/metadata/version endpoints, multipart upload, object store, transcription jobs. | 43 | Built from documentation, never compared with real responses. Its hook into the pipeline oracle does not resolve because of a function-name mismatch, so it reads the ground truth directly. |
| Integration tests | Generator → BLE and Wi-Fi emulators → pipeline → evals; mock-cloud upload and transcription round trip; mock-cloud identity → emulator handshake. | 21 | All components are ours. Only the plain mono Ogg shape travels the whole chain. |
| Compose (V6) | Compose file, three Dockerfiles, healthchecks, a smoke script, a Python entrypoint for the emulator on a TCP transport, and a local runner. | 31 | Never run under Docker. |

## 7. The real-SDK pull (R7-S13)

This is the only part of phase 3 that ran Plaud's own code, so it carries the
most weight.

### Setup

An API-34 Android emulator ran our copy of Plaud's template app. It was
extended with one debug screen that calls only public SDK functions and uses a
synthetic identifier. The emulated device ran in a Python process attached
through Android's netsim Bluetooth simulator and served a synthetic Ogg/Opus
file of 11 271 bytes. Seventeen runs are archived with logs, captures and
hashes. Before each run, app data was cleared, because the SDK silently skips
the download when an output file already exists.

### Findings

1. **Completion needs the end-of-data frame before the TAIL.** With HEAD, data
   and TAIL only, the SDK received every byte but never reported completion
   (three runs). With the end-of-data frame after the TAIL, it also never
   completed. Placed before the TAIL, it completed with code 0 and with code 1.
2. **On a gap, the SDK sends stop at once, then restarts from its cursor about
   35 ms later.** The ledger had said stop is sent only after a 5-second
   stall. That describes a different path and has been corrected.
3. **An emulator that answers with zero delay confuses the SDK's request
   queue**, producing error codes −98 and −99. About 4 ms between frames
   removed the errors. This is a harness setting; real device timing is
   unknown.
4. **If the device keeps sending the old stream after a restart, the SDK's
   export can come out corrupted.** One run produced a file that repeated
   1 600 bytes. The emulator now abandons the old stream. This was seen with
   our emulator only; whether real firmware can trigger it is unknown.
5. **OPUS export of a plain Ogg file is a byte-for-byte passthrough.** The SDK
   does not re-encode or re-tag it. This narrows where the cloud's different
   audio tag can come from.

Runs 11–13 repeated the export, raw download and gap-recovery cases on the
emulator code as shipped. All three were byte-exact. Across the whole series,
gap recovery ended byte-exact in all four runs where the emulator abandoned
the old stream (6b, 6c, 6d and 13).

## 8. What was wrong, and has been corrected

This list exists so that nobody has to take the rest of the report on trust.

| Error | How it was found | State |
|---|---|---|
| The frozen transfer sequence lacked the end-of-data frame. Phase 1 had registered the ordering question (U17), but gave a wrong reason: that nothing in the client orders the two frames. | The real-SDK runs | Emulator, ledger, register and the frame-count assertions in five existing test files corrected. The old sequence remains reachable for a negative test. |
| The ledger said stop is sent only after a 5-second stall. | The real-SDK runs | Ledger corrected, and the emulator's own description too. |
| A duplicate unknown (U23) was registered yesterday. It repeats U15. | Re-reading the register while writing this report | Marked as a duplicate; U15 and U17 now carry the runtime facts. |
| The R5-S8 protocol verifier still expected the old frame count after the sequence change. It went unnoticed for about a day. | Re-running all verifiers today | Updated; all 12 pass. |
| The product reconstruction stated 2 004 evidence-graph edges; the rebuilt graph has 2 072. | Checking today | Corrected. |
| Phase 2 first misread `langfuse` as a Chinese-localised fork, and first treated `live-agent`'s zero "plaud" strings as meaning no Plaud work (it has 227 Plaud-authored files). | Content comparison against upstream | Withdrawn and corrected in the source map. |
| The Wi-Fi test double deadlocked on close. The Wi-Fi code also cited bytecode offsets where file line numbers belonged. | Hanging test run | Fixed; the whole Wi-Fi test file runs in under 5 s. |
| The README said V3 needed a Bluetooth radio. | Phase 2 review | Corrected; netsim needs none. |

*Earlier corrections from before this week, including five to the file-sync
milestone, are in `docs/reconstruction-log.md`.*

## 9. How the work was checked, and what was not

### Checked, today

- The full suite: 978 tests pass in about 65 seconds with a 120-second
  per-test timeout.
- All 12 standalone protocol verifiers pass.
- All 33 reference repositories sit at their pinned commits with no modified
  or untracked files. No bytecode caches were written into them, and the SDK
  archive's hash matches.
- The local topology run was repeated: services live in 0.96 s, job finished
  in 6.7 s, clean teardown.
- Every byte-exact claim in section 7 is backed by a SHA-256 in
  `r7/r7-s13-evidence/SHA256SUMS`.

### Not checked, or checked more weakly than it may look

- **Authorship and review.** About 13 000 lines of phase-3 package code and
  most of the new tests were written by delegated agents. The orchestration
  planned an adversarial review of each component and a completeness
  critique. That stage never ran: the run hit a usage limit and 14 of its 17
  agents failed. The unfinished Wi-Fi and fault-matrix work was completed by
  follow-up agents, and integration and compose were redone from scratch. The
  generator's author never delivered a final report; its tests pass and its
  documentation exists. The checks that did happen are each component's own
  tests, the cross-layer integration tests, targeted re-runs and the full
  suite. The code has not been read line by line.
- **Circularity.** Most tests check our components against each other, or
  against models derived from bytecode. Only the R7 runtime runs involve
  Plaud's real code, and they cover Android, the legacy protocol and
  Bluetooth only.
- **Test counts** include parametrised cases, so the count of distinct test
  functions is lower.
- **Evidence-graph edges** are produced by pattern-matching citation text.

## 10. Numbers

| Tests by area | Count |
|---|---:|
| Phase 1 protocol and emulator | 301 |
| End-of-data frame and stream abort (R7-S13) | 15 |
| V2 fault matrix and receiver model | 109 |
| Wi-Fi transfer | 135 |
| Generator and V4 round trip | 82 |
| Evals | 137 |
| Pipeline | 104 |
| Mock cloud | 43 |
| Cross-layer integration and V4 end to end | 21 |
| Compose | 31 |
| **Total** | **978** |

| Code and documents | Lines |
|---|---:|
| `emulator/plaudsim/`, including phase-1 code | 6 148 |
| `generator/` | 2 910 |
| `evals/` | 2 504 |
| `pipeline/` | 2 501 |
| `mockcloud/` | 2 307 |
| `emulator/serve.py` and `docker/` Python | 561 |
| Tests, 71 files | 17 086 |
| Kotlin runtime drivers | 377 |
| Documents in `docs/` and `docs/architecture/` | ≈ 7 500 |

| V2 matrix outcomes | Rows |
|---|---:|
| Recovered byte-exact | 38 |
| Recovered after the app resumes from its cursor | 6 |
| Failure detected, no corrupted output | 15 |
| Corruption risk (a device that never abandons a stream) | 1 |
| Undetectable by design (payload bit-flip) | 1 |

## 11. Open items

| Item | Blocked on | What would unblock it |
|---|---|---|
| V5: a real system scored on AMI | No AMI audio, no ASR model, no annotation converter; placeholder gates | Download AMI (CC BY 4.0), write the converter, install one system, calibrate the gates. |
| V6 under Docker | Docker not installed | Run `scripts/compose-smoke.sh` on a Docker host. |
| Real device behaviour: advertising branch, end-of-data code and TAIL (U15, U17, U18), storage units, settings values | No device | One Bluetooth capture of any legacy Plaud recorder. |
| What real firmware records (U20) | No real recording | One recording the operator lawfully owns. |
| Encrypted protocol with the real SDK (CRED-1) | Key material issued by Plaud's cloud | Legitimate partner credentials. Deliberately not pursued otherwise. |
| iOS SDK at runtime | Not attempted | An iOS simulator or device plus a Bluetooth bridge. |
| Wi-Fi path with the real SDK | No access-point emulation; not attempted | A host access point the emulated phone can join, or a physical phone. |
| Bluetooth-to-Wi-Fi handoff | Not wired | Connect opcode 10 to the Wi-Fi device emulator. |
| Intelligible synthetic speech | No text-to-speech weights on this machine | Install a TTS backend's weights; the hook exists. |
| Commit the work | Awaiting the owner's decision | Review, then commit; the tree is in a committable state. |

## 12. Rules kept throughout

- No Plaud endpoint was called, no Plaud device was touched, and no credential
  was used or sought. Every identifier and token is synthetic and labelled as
  such.
- Nothing under `reference/` was modified. That was checked after every phase
  and again today.
- Evidence classes are kept apart: bytecode-proven, runtime-proven, official
  documentation, observed by third parties, inferred, harness policy and
  unknown. Harness choices are labelled in code and in documents.
- A test Wi-Fi password committed in one of Plaud's public repositories is
  noted by existence only and not reproduced anywhere.

## 13. Repository state

Git history holds a single commit, the 21 September scaffold. Everything
described here sits in the working tree as 45 modified or untracked entries.
`build/`, `data/` and `reference/` are git-ignored. To reproduce:

```bash
./scripts/fetch-references.sh      # 33 pinned repositories into reference/
./scripts/build-evidence.sh        # decompile the SDK into build/evidence/ (checks the AAR hash)
python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q --timeout=120
bash scripts/local-up.sh            # whole topology without Docker
```

The real-SDK runs also need the Android SDK, an API-34 emulator image, JDK 17
and Gradle 8.2. `r7/r7-s12-k3-runtime-capture.md` and
`r7/r7-s13-recording-pull.md` give the exact steps.

## 14. Where things are

| Question | Document |
|---|---|
| Standing status and next steps | `PROJECT.md` §8 |
| The Bluetooth protocol, with evidence | `docs/protocol-ledger.md` (§15 covers the real-SDK pull) |
| What earlier work got wrong | `docs/reconstruction-log.md` |
| Open questions | `docs/final-uncertainty-matrix.json` |
| Real-SDK runs | `r7/r7-s12-k3-runtime-capture.md`, `r7/r7-s13-recording-pull.md`, `r7/r7-s13-evidence/` |
| The product above the radio | `docs/final-product-reconstruction.md`, `docs/product-ledger.md`, `docs/architecture/`, `docs/source-map.md` |
| Harness layers | `docs/v2-fault-matrix.md`, `docs/wifi-transport.md`, `docs/generator.md`, `docs/evals.md`, `docs/pipeline.md`, `docs/mockcloud.md`, `docs/integration.md`, `docs/compose.md` |

---

*Compiled on 24 September 2026 from the repository working tree, test runs,
verifier runs and the archived runtime logs. Figures were re-measured on the
day unless the text says otherwise.*
