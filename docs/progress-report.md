# Plaud Harness Progress

**Progress report · plaud-harness**

| | |
|---|---|
| Period | 21–25 September 2026 |
| State as of | 28 September 2026 (work of 25 Sep; counts re-checked 28 Sep) |
| Repository | `plaud-harness` (any clone), branch `main`. Two commits are on GitHub; the 25 September work is uncommitted (section 14) |
| Compiled by | Claude Code from the working tree, test runs, run logs and the 25 September review and verification records |

## Contents

1. [Summary](#1-summary)
2. [Validation rungs](#2-validation-rungs)
3. [Timeline](#3-timeline)
4. [Phase 1 · protocol reconstruction](#4-phase-1--protocol-reconstruction)
5. [Phase 2 · product reconstruction](#5-phase-2--product-reconstruction)
6. [Phase 3 · the remaining harness layers](#6-phase-3--the-remaining-harness-layers)
7. [The real-SDK pull (R7-S13)](#7-the-real-sdk-pull-r7-s13)
8. [25 September · review, fixes and first measurements](#8-25-september--review-fixes-and-first-measurements)
9. [What was wrong, and has been corrected](#9-what-was-wrong-and-has-been-corrected)
10. [How the work was checked, and what was not](#10-how-the-work-was-checked-and-what-was-not)
11. [Numbers](#11-numbers)
12. [Open items](#12-open-items)
13. [Rules kept throughout](#13-rules-kept-throughout)
14. [Repository state](#14-repository-state)
15. [Where things are](#15-where-things-are)

---

## 1. Summary

The project set out to build a hardware-free test harness for Plaud voice
recorders: a software device that Plaud's own app SDK will talk to, synthetic
meetings with exact ground truth, an evaluation harness, and a mock of the
cloud. All four layers exist as tested code. The official Android SDK has
been run against the emulated device and pulled a recording byte-for-byte. No
real Plaud device has been used at any point, so nothing here proves how real
hardware behaves.

> **Correction, 25 September 2026.** Earlier versions of this report said that
> no Plaud cloud service was used. That was wrong for the Android runtime runs.
> Our debug drivers initialised the official SDK with a synthetic token. The
> SDK then sent one automatic key-generation request to Plaud's partner server
> per app start. Every response visible in the logs was HTTP 401. Section 8.6
> gives the evidence and the fix.

| At a glance | |
|---|---|
| Automated tests | **1365 pass, 4 skipped** on 25 Sep (978 on 24 Sep; 301 after phase 1) |
| Independent review | **71 findings** on the phase-3 layers: 69 confirmed, 2 plausible, none refuted (section 8.1) |
| Protocol verifiers | **12 of 12 pass** (re-run 25 Sep) |
| Reference corpus | **33 of 33 at their pins**, unmodified, AAR hash matches (checked 25 Sep) |
| Real hardware used | **None**: no device and no credential |
| Plaud cloud contact | **Yes, not intended**: in the R7 runtime runs, one SDK `gen-key` request per app start (logged in R7-S13 and R7-S14; very likely in R7-S12). 19 of the 22 logged requests got HTTP 401; 3 failed at DNS. Stopped on 25 Sep; checked in one offline run (section 8.6) |
| Committed | **Two commits on `main`**, pushed to GitHub on 25 Sep; the 25 Sep work is not committed |

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
- **An independent review found 71 problems in those layers.** Reviewers
  demonstrated most findings with a script and showed citation and
  documentation errors by file and line. A few, among them the Docker build
  failure, were argued without a demonstration (section 8.1). A separate
  verifier re-checked every finding. Examples: a scoring crash that aborted a whole batch, a turn planner
  that let three speakers talk at once, an emulator service that one badly
  behaved client could break, and Docker images that could not have built.
  Each was fixed or narrowed. Nearly every fix has a test shown to fail
  before it.
- **The first real-model numbers exist, and they are poor.** A pipeline of
  Whisper small.en and sherpa-onnx diarization was scored on 4 of the 16 AMI
  test-split meetings and on four synthetic Piper TTS meetings. Given the true
  speaker count, AMI macro DER is 0.6557 and cpWER 0.8533. Without it, the
  diarizer found 35 to 95 speaker clusters in 4-speaker meetings. This is not
  the V5 target.
- **The Docker path ran once.** `docker compose up` brought the system live in
  6.58 s against a 60 s target, and the end-to-end job exited 0.
- **The real SDK's Wi-Fi transfer was tried and stopped at the hotspot join.**
  No Wi-Fi message reached our Wi-Fi device. The run exposed one emulator bug,
  which is fixed.

What is not true yet:

- V5 is not met. The measurement covers 4 of the 16 AMI test meetings. The
  standard scoring path could score the AMI meetings only with the oracle
  speaker count as a hint. The numbers are poor.
- The Docker run happened once, on an Apple Silicon Mac inside a Linux VM. It
  has not run on x86_64 or on a GitHub runner.
- The GitHub `tests` workflow failed on the pushed commit. The fix is
  uncommitted and has passed only in local clean-clone simulations.
- The encrypted protocol used by newer devices (port version 20 and above) has
  only been exercised with synthetic keys, never with the real SDK.
- The iOS SDK has never been executed; this machine has no Xcode. The real SDK
  has never exchanged a Wi-Fi message with our emulator.
- The review covered the phase-3 layers only. The phase-1 protocol code
  (advertising, handshake and file-sync codecs, sealed link) was not part of it.

## 2. Validation rungs

PROJECT.md defines six pass/fail rungs. This table states what each one shows
and, as importantly, what it does not.

| Rung | Claim | Status | What it shows | What it does not show |
|---|---|---|---|---|
| V1 | Emulator is discovered and completes a session | **Done** | A Bumble central scans, parses the advertisement with the SDK's own rules, connects, exchanges MTU, discovers services and runs the control and file-sync messages. | The central is our Python code. It passed while the transfer sequence was still wrong, which is why V3 matters. |
| V2 | Survives fault injection without corrupting a transfer | **Done** | 60 counted cells over a virtual Bluetooth link: drops, duplicates, reordering, truncation, wrong session, disconnects, stop mid-transfer, MTU 23–517, both subscription modes and more. Each ends byte-exact or with a detected failure. Two further rows put wrong bytes at the right offsets and show that the corruption check fires. | The receiving side is a Python model of the SDK's receiver derived from bytecode. Six of its behaviours were cross-checked against the real-SDK runs (docs/v2-fault-matrix.md). The review (T10) showed that "no silent corruption" holds by construction in every counted cell, so the matrix tests completion, not content integrity. One cell is a documented corruption risk. Payload bit-flips are undetectable, because data frames carry no checksum. |
| V3 | Plaud's own SDK connects and pulls a recording | **Done** (Android BLE, legacy protocol) | The real AAR bound, listed the file and pulled it through `syncFile` and `exportAudio(OPUS)`, byte-exact. Its gap recovery converged byte-exact in all four gap runs. | Nothing about real devices. Not iOS, not the encrypted protocol, and not Wi-Fi (R7-S14 stopped at the hotspot join). Not our own client binding to real hardware, which needs cloud-issued keys. |
| V4 | Synthetic ground truth round-trips at DER 0 | **Done** | The reference annotation scores DER and JER 0.0 against itself and against one rebuilt from the sample-level activity mask. End to end, generator → oracle pipeline → evals gives DER, JER, cpWER and tcpWER all 0.0. Perturbations raise every metric. | This validates plumbing and metric direction only. The oracle returns the ground truth by construction, so zero is expected. It says nothing about any real system's quality. |
| V5 | A full pipeline hits target cpWER/DER on AMI | **Not done** (a subset was measured) | `whisper-sherpa` ran at full length on 4 of the 16 AMI test-split meetings (5536.54 s) and on 4 synthetic Piper meetings. With the reference speaker count as a hint, AMI macro DER 0.6557, cpWER 0.8533, speaker-agnostic WER 0.3250. Piper `mix.wav` with the hint: DER 0.6149, cpWER 1.0993, WER 0.2501. Two regression-gate suites are calibrated on these runs (`docs/v5-results.md`). | These numbers are poor. They are not the V5 target, which needs the full 16-meeting test split; the `ami-headset` suite stays unmeasured. The hint is an oracle value. Without it, `evals batch` scored none of the AMI meetings; a supplementary path with meeteval's speaker guard lifted scored 3 of 4 (DER 0.7760, cpWER 1.1096). The segmentation model was trained on data that includes AMI. n = 4 per set. |
| V6 | `docker compose up` is live in under 60 s | **Done** (one run, 25 Sep) | `scripts/compose-smoke.sh` built the three images (121.38 s, outside the timed window by design), brought emulator and mock cloud to healthy in 6.58 s, and ran the job to exit 0 in 11.34 s (`build/v6/compose-smoke-2026-09-25.log`). | One run on one machine: Apple M1 with a Linux arm64 VM (Colima). Not x86_64, not a GitHub runner. The job's zeros are harness self-tests (oracle), not system scores. |

## 3. Timeline

| Date | Work | Main output |
|---|---|---|
| 21 Sep | Scaffold, corpus fetch | 33 source repositories fetched and commit-pinned; PROJECT.md; the scaffold commit. The 24 Sep report cited it as `833f531`. The same scaffold now has hash `09c8922` on `main`; the two have the same tree (2b74f53d) and differ in their author and committer fields. |
| 22 Sep | Protocol re-audit from first principles; first runtime spikes | `javap` bytecode adopted as ground truth; mechanically extracted evidence digest; milestones R1–R4; the real SDK reached the Bluetooth handshake on an Android emulator (R4-S3). |
| 23 Sep | Protocol audits, synthetic encrypted path, audio; phase-1 closure; phase-2 product reconstruction; first real-SDK pull runs | R5–R7-S12 (handshake frame byte-exact at runtime, opcode 8 found); closure report; 10 architecture documents, product ledger, source map, evidence graph; R7-S13 runs 1–10. |
| 24 Sep | Remaining harness layers; confirmation runs; the first version of this report | R7-S13 runs 11–13 on the shipped emulator code; V2 matrix, Wi-Fi emulator, generator, evals, pipeline, mock cloud, integration tests, compose. |
| 25 Sep | Commit `70249ba` pushed to GitHub; independent review of the phase-3 layers and fixes; CI diagnosis; first Docker run; R7-S14 Wi-Fi run with the real SDK; Piper TTS, `whisper-sherpa` and the AMI converter; V5-style measurement; cloud-contact correction | 71 review findings addressed; V6 measured; `docs/v5-results.md`; `r7/r7-s14-wifi-real-sdk.md`; drivers changed to pass a blank SDK token. |

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
> sequence, was later shown incomplete by the real SDK (section 7). Another,
> the meaning of the opcode-10 byte, was shown wrong by R7-S14 (section 9).
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

*Limits: phase 2 itself called no Plaud cloud endpoint and examined no
consumer app binary. The evidence graph links sources to claims by matching
citation text, so some edges may be misattributed.*

## 6. Phase 3 · the remaining harness layers

Everything PROJECT.md listed as "not started" or partial was implemented on
23–24 September. The 25 September review and stage-2 work changed most rows.
Test counts were taken with `pytest --collect-only` on 25 Sep and include
parametrised cases; the 24 Sep figure is in brackets.

| Component | What exists | Tests | Main limitations |
|---|---|---:|---|
| BLE emulator corrections | End-of-data frame emitted before TAIL by default. Transfers can stream from a paced task that a restart or stop cancels. `PlaudPeripheral.for_real_sdk()` packages the settings the real SDK needed (task streaming, 4 ms pacing); the plain constructor stays inline and unpaced. | 20 (15) | The frame's code value (0) and the 4 ms pacing are harness choices. What real firmware sends is unknown. |
| V2 fault matrix | 60 counted cells plus 2 content-fault rows, and a bytecode-derived SDK receiver model corrected to match the runtime runs. | 115 (109) | Model-scored. "No silent corruption" holds by construction in the counted cells (review T10). Some faults appear twice, once unpaced and once paced. |
| Wi-Fi transfer emulator | Message codecs; the device side as a WebSocket client; a phone-side test double rebuilt from bytecode; sessions encrypted with both AEAD ciphers. With a `wifi_device_factory`, opcode 10 now starts the Wi-Fi device and opcode 13 closes it, over loopback. | 156 (135) | The real SDK stopped at the hotspot join (R7-S14), so no Wi-Fi message was exchanged with it. No access-point or DHCP layer. 75 of the tests pin constants to bytecode. |
| Generator (layer 2) | Scenarios, a model-free formant "voice", room simulation with pyroomacoustics, exact word timings, RTTM/STM references, and Ogg/Opus output in the device's shapes. New: a Piper TTS backend whose word times come from the voice's own phoneme alignment. | 136 (82) | Formant speech is not intelligible. Piper speech is intelligible but is seeded word salad from one multi-speaker TTS model. The Piper voice's licence is unresolved (section 10). The Kokoro backend is untested. Count includes the 9 V4 round-trip tests. |
| Evals (layer 3) | DER, JER, WER, cpWER and tcpWER via pyannote.metrics, jiwer and meeteval; gates; a batch runner; a CI workflow. New: the AMI converter (`python -m evals.ami`) and two calibrated regression-gate suites. | 198 + 51 AMI + 8 V5 gates (137) | meeteval 0.4.3 refuses cpWER above 20 speakers, and `evals batch` then drops the whole meeting, DER included (not yet changed). The new gates are regression gates, not quality targets. |
| Pipeline | Interfaces; an oracle; a perturbed oracle; the model-free `energy-vad-cluster` diarizer, reworked on 25 Sep (f0 feature, spectral clustering); adapters for faster-whisper, pyannote.audio and whisperx. New: `whisper-sherpa`, a real composed system with sha256-pinned weights. | 228 (104) | Its default clustering threshold over-clusters long meetings. Its ASR is not reproducible on one long meeting. The energy-vad-cluster f0 feature is tuned to synthetic voices. The pyannote.audio and whisperx adapters are still untested against the real packages. |
| Mock cloud (layer 4) | A FastAPI mock of the partner contract: identity chain, device binding, SDK key/sign/metadata/version endpoints, multipart upload, object store, transcription jobs. The hook into the pipeline oracle now runs `pipeline.oracle.OraclePipeline` (review MC-1). | 76 (43) | Built from documentation, never compared with real responses. |
| Integration tests | Generator → BLE and Wi-Fi emulators → pipeline → evals; mock-cloud upload and transcription round trip; mock-cloud identity → emulator handshake. | 21 (21) | All components are ours. |
| Compose (V6) | Compose file, three Dockerfiles, healthchecks, a smoke script, the emulator on a TCP transport with one virtual controller per client, a local runner, and a cloud round trip in the job. | 59 (31) | Run under Docker once, on arm64 (section 8.3). |

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

*Correction, 25 September 2026: the debug screen initialised the SDK with a
synthetic token, and every one of these runs also attempted one `gen-key`
request to Plaud's partner server. In 14 runs the server answered HTTP 401. In
runs 11–13 the host name did not resolve, so the request did not reach it
(section 8.6).*

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

Runs 11–13 repeated the export, raw download and gap-recovery cases with the
pull rig's own settings (task streaming, 4 ms pacing, stream abort). All three
were byte-exact. Across the whole series, gap recovery ended byte-exact in all
four runs where the emulator abandoned the old stream (6b, 6c, 6d and 13).

## 8. 25 September · review, fixes and first measurements

### 8.1 Independent review of the phase-3 layers

The 24 Sep version of this report (its section 9) said a planned review had
not run. It ran on 25 Sep. Six reviewers each read one area. Most behavioural
findings were demonstrated with a script. Some were argued without one: for
example C3 (the Docker images could not build), because no Docker daemon was
reachable, and T10 and T11 (test checks that cannot fail), from the test code.
Citation and documentation findings (for example GEN-7, T2, T9, T12, EV-9) were
shown by quoting the files and lines. For each area a separate verifier then
re-checked every finding.

| Area | Findings (major / minor) | Verdicts | Examples |
|---|---|---|---|
| Evals | 11 (3 / 8) | 11 confirmed | A meeting with no scorable reference speech crashed the batch (EV-2); the per-speaker DER breakdown was wrong when labels shared names (EV-1). |
| Generator | 9 (3 / 6) | 9 confirmed | Grid rounding let three speakers stack and a speaker overlap themself (GEN-1); device outputs were 3–6 dB cleaner than the recorded SNR (GEN-2). |
| Pipeline | 13 (6 / 7) | 12 confirmed, 1 plausible | The diarizer's unaided speaker count was wrong both ways (PIPE-01); the VAD found no speech in audio with little silence (PIPE-02); truncated Ogg files were accepted silently (PIPE-03). |
| Mock cloud | 12 (4 / 8) | 12 confirmed | The pipeline-oracle hook never ran (MC-1); malformed input returned 500 (MC-2). |
| Transports (BLE, Wi-Fi, fault matrix) | 12 (3 / 9) | 12 confirmed | The Wi-Fi "busy" status could never be returned (T1); the R7-S13 document misstated the emulator's defaults (T2). |
| Compose | 14 (4 / 10) | 13 confirmed, 1 plausible | One central that left uncleanly broke the emulator service while health said ready (C1); the images could not have built, because the base image has no compiler (C3). |
| **Total** | **71 (23 / 48)** | **69 confirmed, 2 plausible, none refuted** | |

The two plausible findings are PIPE-05 (the pyannote adapter would likely
break on pyannote.audio 4.x) and C11 (the mock's chunk size in the running
compose configuration).

What changed:

- One fixer per area worked through the findings. For nearly every fix, the
  fixer showed that its new test fails on the unfixed code. The exceptions:
  three compose guards already held (parts of C12, C13 and C14), and the three
  strengthened Wi-Fi assertions (T11) were shown to fail against a
  deliberately broken implementation instead.
- A final check ran the full suite four times after the fixes: 1247 passed in
  each run, with no skips on this machine.
- Some findings were narrowed rather than closed. GEN-1's overlap target still
  falls short on one preset (worst −0.043). EV-4 was not compared with NIST
  md-eval. PIPE-05 was checked only against stand-in modules. PIPE-01 still
  miscounts two held-out smoke seeds without the hint. MC-3 was checked with
  h11, not with GoReplay itself.
- C11 was not on the compose fixer's list. Its main point, the mock's chunk
  size, was addressed separately: the compose file now passes
  `--chunk-size 20000`, and the Docker job's upload had 3 parts. Its side
  point, the mock's 1 s task step, is unchanged.
- C3 (no compiler in the images) was checked only statically at the time. The
  Docker build of 25 Sep (section 8.3) then succeeded on linux/arm64.
- A follow-up pass applied the fixes the final check had listed as outstanding:
  the fixture negation in `.gitignore`, pinned fetching in
  `scripts/fetch-references.sh`, the primary device file in the pipeline and
  the emulator entrypoint, `for_real_sdk()` in the three real-SDK rigs, and
  the document corrections (ledger section 7, R7-S13 section 6, the
  integration document, the evals CI notes).

### 8.2 GitHub CI

- Commit `70249ba` was pushed to `github.com/ORION2809/PLAUDE_EMULATOR_REV-ENG`
  (public) on 25 Sep. Its `evals` workflow passed. Its `tests` workflow failed.
- The Actions log was not read. The cause was reconstructed locally. The
  workflow installed only Bumble, pytest and pytest-asyncio. Collecting the
  suite in such an environment gave "640 tests collected, 31 errors" (numpy,
  httpx, pyannote and yaml missing).
- Behind that, the two synthetic Ogg test fixtures were git-ignored by `*.ogg`,
  so a clean clone lacked them.
- The fix is uncommitted. `tests.yml` now installs `requirements/ci.txt` (which
  includes `all.txt`) into `.venv`, fetches Bumble at its pin, generates one
  synthetic meeting and fails on any undocumented skip. `.gitignore` now lets
  `tests/fixtures/*.ogg` through.
- It was checked only by simulation on this Mac. The last run of the workflow's
  steps on a clean clone ended "1156 passed, 93 skipped", with every step
  exiting 0. After stage 2, a clean-clone run with the model packages hidden,
  as in CI, ended "1253 passed, 116 skipped" with the strict skip policy.
  Neither was a Linux x86_64 GitHub runner.

### 8.3 Docker (V6)

- The toolchain was installed without admin rights. Colima 0.10.3, Lima 2.2.0
  and Docker CLI 29.8.1 are under `~/.local`. Compose v5.5.1 and Buildx v0.37.1
  are CLI plugins in `~/.docker/cli-plugins`.
  The VM runs Ubuntu 24.04 with Docker daemon 29.5.2, 2 CPUs and 3 GB.
- Lima, Colima and Compose were checked against their published sha256 files.
  Buildx was checked against GitHub's asset digest. The Docker CLI tarball has
  no published checksum; it came over HTTPS from download.docker.com.
- The run (`build/v6/compose-smoke-2026-09-25.log`) printed
  `build_s=121.38 (not part of the V6 window)`,
  `up_wait_s=6.58 target_s=60 up_rc=0` and `job_s=11.34 job_rc=0`, then
  `PASS system live in 6.58s (target 60s); job exit 0`.
- The base image was already on the machine (its `FROM` step took 0.1 s), so
  121.38 s does not include pulling `python:3.11-slim`.
- The job probed both services, scanned the emulator's advertisement, generated
  two meetings, ran the oracle pipeline and evals (oracle suite PASS), and
  pushed one meeting through the mock cloud: 49 161 bytes in 3 parts,
  byte-exact download, task states PENDING, STARTED, SUCCESS.
- Each image is 1.06 GB as `docker images` reported it; the three share layers.
- Not verified: x86_64, a GitHub runner, and other compose versions.

### 8.4 Wi-Fi with the real SDK (R7-S14)

- The unmodified SDK bound to our BLE emulator, sent OpenWiFi (opcode 10) and
  took the answer. It then asked Android to join the pen's hotspot
  `PLAUD0001`. The emulated phone could see only the emulator's built-in
  `AndroidWifi` network. After 30 s the request was released and the SDK reported
  `onError(1003)`. The phone's WebSocket server on port 8081 never started.
- Not one Wi-Fi message was exchanged in either direction, in six runs. No file
  reached the app, so nothing could be hashed.
- A checker upheld this result and listed six overstated points in the report.
  The main one: nobody approved Android's network-request dialog, and an
  unapproved request would also time out. A missing hotspot is enough to block
  the join, but the runs do not show it is the only cause. That the join request
  used WPA2 with passphrase `10000001` comes from bytecode; the runtime log of
  the request shows only the SSID. As of this writing these
  points are not yet folded into `r7/r7-s14-wifi-real-sdk.md`.
- One emulator bug was found and fixed. The emulator read the opcode-10 byte as
  on/off and treated 0 as "hotspot off". The SDK's own `startWifiTransfer`
  sends 0 to open. The emulator now opens on every opcode 10 and logs the byte
  as `mode`; its meaning is unknown. Four tests fail against the old handler.
- Discrepancies between the SDK and our documents or emulator, listed in
  `r7/r7-s14-wifi-real-sdk.md` §5 (the SSID, token and opcode rows are also
  corrected in the ledger and `docs/wifi-transport.md`):
  - The SSID comes from `BleDevice.getWiFiName()`. For the emulated device
    (project 881, port version below 20) it is `PLAUD` plus the last four
    serial digits. Our documents had `Plaud` plus four, from a fallback path.
  - On the recovery path the Wi-Fi handshake token (21 characters at runtime)
    differs from the BLE token (13 characters).
  - The phone starts its server only after a successful join, which can take
    up to 30 s. Our pen dials once by default, so against the real SDK it
    would fail. The runs used 90 attempts; the default was left unchanged.
  - The SDK waits for the CloseWiFi answer on opcode 10 but type-checks it as
    opcode 13, so no valid close status can reach the app: an opcode-13 answer
    is never delivered, and an opcode-10 answer arrives as status −1
    (`l0 Mismatch`, run 4).
  - After a failed join the SDK's agent stays in CONNECTING, and
    `endWiFiTransfer` sends nothing over BLE.
  - The string argument of `startWifiTransfer` is never used.
- What would unblock it: a hotspot the emulated phone can join, or a physical
  phone.

### 8.5 Stage 2: real speech, real models, AMI references and first numbers

What was built:

- **Piper TTS backend** (`generator/tts/piper.py`): piper-tts 1.8.0 with the
  voice `en_US-libritts_r-medium`. Word times come from the voice's own
  phoneme-to-sample alignment. Byte-determinism was checked only on this M1
  with onnxruntime 1.30.0 on one thread.
- **`whisper-sherpa`** (`pipeline/whisper_sherpa.py`): faster-whisper 1.2.1
  with Systran `small.en`, and sherpa-onnx 1.13.8 with an ONNX conversion of
  pyannote segmentation-3.0 and 3D-Speaker CAM++ embeddings. Weights are
  pinned by sha256 in `pipeline/model_store.py` and fetched with
  `python -m pipeline fetch-models`. The packages are in the optional
  `requirements/models.txt`; CI does not install them.
- **AMI converter** (`evals/ami.py`): converts AMI's NXT annotations into the
  harness's meeting format. Its speech turns match the BUT `only_words` RTTMs
  line for line on 168 of 170 meetings, including all 18 dev and 16 test
  meetings. Only 4 test meetings have audio on this machine.

How it was checked:

- One reviewer per component found 24 defects in total (7 TTS, 10 ASR, 7 AMI),
  plus one informational note.
- The refixers fixed 22 of them. The ASR skip-gate registration was done later
  by the measurement agent. One TTS defect is open: `meeting.json` does not
  record the Piper voice hash or timing method, and `generator/export.py` was
  outside that fixer's scope.
- A verifier re-scored a sample of the runs from the stored hypotheses, with
  identical results. The sample was the AMI hinted, no-hint and word-run runs,
  AMI energy-vad-cluster, Piper `mix.wav` without the hint, the Piper device Ogg
  with the hint, two AMI scoring variants and the four gate commands. It also
  compared all 896 numeric cells of the results tables (sections 4 and 6 of that
  document) with the stored reports, hypotheses and logs; none differed.
  A fresh inference on one Piper meeting reproduced the stored result exactly.
  The verifier listed 11 wording and precision corrections to
  `docs/v5-results.md`; none changes a table, a threshold or a conclusion.
  They are applied there, with the old wording listed in its section 12.

Results (`docs/v5-results.md`, macro over 4 meetings per set, evals defaults):

| Data | Run | DER | JER | cpWER | tcpWER | WER |
|---|---|---:|---:|---:|---:|---:|
| AMI, 4 test meetings | whisper-sherpa, hint | 0.6557 | 0.8150 | 0.8533 | 0.9709 | 0.3250 |
| AMI, 4 test meetings | whisper-sherpa, no hint | not scorable | – | – | – | – |
| AMI, 3 of 4 meetings | whisper-sherpa, no hint, meeteval guard lifted (supplementary) | 0.7760 | 0.8020 | 1.1096 | 1.2290 | 0.2876 |
| AMI, 4 test meetings | energy-vad-cluster, no hint | 0.6445 | 0.7566 | n/a | n/a | n/a |
| Piper, `mix.wav` | whisper-sherpa, hint | 0.6149 | 0.7699 | 1.0993 | 1.1233 | 0.2501 |
| Piper, `mix.wav` | whisper-sherpa, no hint | 0.6760 | 0.7191 | 0.9962 | 1.0732 | 0.2501 |
| Piper, `mix.wav` | energy-vad-cluster, hint | 0.1920 | 0.2562 | n/a | n/a | n/a |
| All 8 meetings | oracle (self-test) | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

What the numbers say:

- They are poor. Diarization, not ASR, is the dominant error on both sets.
- The "hint" is the reference speaker count, an oracle value that a real
  recorder does not have.
- Without the hint, sherpa's default threshold found 95, 35, 36 and 47 clusters
  in the four 4-speaker AMI meetings.
- meeteval 0.4.3 refuses cpWER when a side has more than 20 speakers, and
  `evals batch` then drops the whole meeting. That is why no no-hint AMI
  meeting could be scored normally.
- On the Piper voices, sherpa diarization is near chance even with the hint
  (confusion 0.50 of reference time). The model-free baseline does better
  there. An embedding probe found whole-turn embeddings near chance on
  all four Piper meetings and IS1009a. Fixed-length 6 s chunks separated the
  voices where probed: s0101 (the only Piper meeting probed), IS1009a and
  ES2004a. This was not traced.
- ASR was not reproducible on EN2002a: three runs with the same ASR settings (the hint run, the
  no-hint run and a diagnostic re-run) gave
  5504, 5118 and 5223 words (WER 0.4072, 0.4371 and 0.4245). The likely cause
  is faster-whisper's unseeded temperature fallback. That is inferred, not
  proven. On ES2004a, IS1009a, TS3003a and all 8 Piper runs (4 `mix.wav`, 4
  device Ogg) the ASR words matched between the hint and no-hint runs, and a
  full repeat of the 20 Piper runs was identical. The AMI diarization was
  never repeated.
- Speed was measured under heavy contention: RTF 0.215 to 0.380. Peak memory
  was 1621 MB on EN2002a (35.7 min) with the hint.
- Two regression-gate suites now exist: `ami-subset-whisper-sherpa` and
  `synthetic-piper-whisper-sherpa`. Each threshold is the measured macro value
  plus 0.03 (0.25 on the speaker-count error), rounded up. They gate the hinted
  runs. Passing means "no worse than on 25 Sep", not "good". A regression
  smaller than 0.03 on the 4-meeting mean passes.
- This is not V5. The V5 target needs all 16 test-split meetings.

### 8.6 Correction: the SDK contacted Plaud's partner server

- **What happened.** Our debug drivers called
  `PlaudDeviceAgent.initSDK(context, <synthetic token>, "api.plaud.ai")`. With
  a non-blank token, `NiceBuildSdk.initSdk` fetches an RSA key pair. It POSTs
  `https://platform-jp.plaud.ai/developer/api/open/partner/sdk/gen-key`, once
  per app start.
- **Evidence.** The request appears in 22 archived logs: 17 from R7-S13 and 5
  from R7-S14. Recounted for this report, 19 of them also show the response
  line `Status: 401` with `ACCESS_TOKEN_INVALID`. The other three (R7-S13 runs
  11–13, 24 Sep) show the request being built, then, about 20 s later,
  `java.net.UnknownHostException: Unable to resolve host "platform-jp.plaud.ai"`.
  The host name did not resolve, so those three requests never reached Plaud's
  server. The server was reached, and answered 401, in 19 of the 22 logged app
  starts. No other Plaud URL was requested in these
  logs; the remaining `plaud.ai` lines name the base URLs the SDK configures.
- The dated correction in `r7/cloud-endpoint-inventory.md` said "15 explicit
  401 lines". The recount finds 19. The first count probably used a search
  that skips files it classes as binary. `file` classes four of the R7-S14 logs
  as `data`, and each holds one 401 line (19 − 4 = 15). That note was corrected
  on 28 Sep, together with the DNS failures of runs 11–13 and its R4/R5
  inference. `r7/r7-s13-recording-pull.md` was corrected the same day for runs
  11–13.
- **Earlier runs.** R4-S3 and R5-S1 ran the stock template app with an empty
  partner token. Their logs show `Partner API: Cannot sign device SN - user
  access token not set`. That is the blank-token branch, so those runs very
  likely sent no `gen-key` request. The R7-S12 driver passed the same synthetic
  token, but its logs were filtered to other tags. R7-S12 very likely sent one
  request per app start; the number is unknown.
- **What it did not do.** The token was synthetic. Every visible response was a
  rejection. No response data was used.
- **Fix.** The three debug drivers now pass a blank token, which takes the
  SDK's no-partner branch. On 25 Sep one export run was repeated with the
  emulated phone's Wi-Fi and mobile data turned off. The pull was byte-exact
  (11 271 bytes, sha256 `0f45367b…cbb48c3d`), and the log has no `gen-key`
  line (`r7/r7-s14-evidence/blank-token-offline-check/`). The recording-pull
  driver is the only one run with the blank token so far.
- **Where it was corrected.** Dated corrections are in
  `r7/cloud-endpoint-inventory.md`, `r7/r7-s12-k3-runtime-capture.md`,
  `r7/r7-s13-recording-pull.md`, `docs/final-closure-report.md`,
  `docs/final-product-reconstruction.md`, `README.md`, `PROJECT.md` and this
  report.

## 9. What was wrong, and has been corrected

This list exists so that nobody has to take the rest of the report on trust.

| Error | How it was found | State |
|---|---|---|
| The frozen transfer sequence lacked the end-of-data frame. Phase 1 had registered the ordering question (U17), but gave a wrong reason: that nothing in the client orders the two frames. | The real-SDK runs | Emulator, ledger, register and the frame-count assertions in five existing test files corrected. The old sequence remains reachable for a negative test. |
| The ledger said stop is sent only after a 5-second stall. | The real-SDK runs | Ledger corrected, and the emulator's own description too. |
| A duplicate unknown (U23) was registered. It repeats U15. | Re-reading the register on 24 Sep | Marked as a duplicate; U15 and U17 now carry the runtime facts. |
| The R5-S8 protocol verifier still expected the old frame count after the sequence change. It went unnoticed for about a day. | Re-running all verifiers on 24 Sep | Updated; all 12 pass (re-run 25 Sep). |
| The product reconstruction stated 2 004 evidence-graph edges; the rebuilt graph has 2 072. | Checking on 24 Sep | Corrected there. PROJECT.md still said 2 004 until 25 Sep; corrected. |
| Phase 2 first misread `langfuse` as a Chinese-localised fork, and first treated `live-agent`'s zero "plaud" strings as meaning no Plaud work (it has 227 Plaud-authored files). | Content comparison against upstream | Withdrawn and corrected in the source map. |
| The Wi-Fi test double deadlocked on close. The Wi-Fi code also cited bytecode offsets where file line numbers belonged. | Hanging test run | Fixed; on 24 Sep the Wi-Fi session test file ran in under 5 s. With 27 tests it now takes about 5.5 s. |
| The README said V3 needed a Bluetooth radio. | Phase 2 review | Corrected; netsim needs none. |
| The 24 Sep version of this report (its §1 "no cloud call" and its §12 "No Plaud endpoint was called") and several documents said the runtime runs touched no Plaud service. The SDK, initialised by our drivers with a synthetic token, sent one `gen-key` request per app start; every visible response was 401. | R7-S14 run logs (finding D7) | Drivers pass a blank token; checked offline with a byte-exact pull. Dated corrections in the documents listed in section 8.6. |
| `r7/r7-s13-recording-pull.md` said `PlaudPeripheral` defaults to task streaming with 4 ms pacing and stream abort, and cited a parameter that does not exist (`abort_stream_on_restart`). The class defaults to inline, unpaced streaming. Runs 11–13 used the pull rig's own settings. | Review finding T2 | Document corrected. The real-SDK settings are now `PlaudPeripheral.for_real_sdk()`, which the three rigs use. |
| The ledger read the opcode-10 byte as on/off, and the emulator treated 0 as "hotspot off". The SDK's own `startWifiTransfer` sends 0 to open. | R7-S14 runs (D1) | Emulator opens on every opcode 10 and logs the byte as `mode`; ledger rows corrected; four regression tests. |
| Ledger section 7 was titled "NOT IMPLEMENTED". It said the Wi-Fi handshake token uses "the same padding rule" as BLE `k3`; the two agree only up to 32 characters, because `padEnd` never truncates and `k3` does. Our documents also gave the SSID as `Plaud` plus four digits, and assumed the Wi-Fi token equals the BLE token. | Review finding T12; R7-S14 (D2, D3) | Ledger section 7 and `docs/wifi-transport.md` corrected: title, padding, SSID source (`BleDevice.getWiFiName()`) and token source. |
| The 24 Sep report cited the scaffold commit as `833f531`. On `main` the same scaffold is `09c8922` (same tree; the author and committer fields differ). | Checking git history on 25 Sep | Corrected in section 3. |

*Earlier corrections from before this week, including five to the file-sync
milestone, are in `docs/reconstruction-log.md`.*

## 10. How the work was checked, and what was not

### Checked on 25 September

- The full suite, run once at the end of this update with a 120-second
  per-test timeout: `1365 passed, 4 skipped, 11 warnings in 339.00s (0:05:38)`.
  All 4 skips are the documented `optional-engine-installed` gate: they test
  the branch for an absent faster-whisper or piper, which are installed here.
- The phase-3 layers by an independent review with a verifier per area
  (section 8.1). Nearly every fix has a test shown to fail on the unfixed
  code.
- The stage-2 measurement by a separate verifier, who re-scored a sample of
  the runs, compared every table cell with the stored outputs and repeated one
  inference (section 8.5).
- The R7-S14 report by a checker, who recomputed all 60 evidence hashes and
  opened about 30 cited bytecode ranges (section 8.4).
- All 12 standalone protocol verifiers pass.
- All 33 reference repositories sit at their pinned commits with no modified
  or untracked files, and the SDK archive's hash matches.
- Every byte-exact claim in section 7 is backed by a SHA-256 recorded in
  `r7/r7-s13-evidence/SHA256SUMS`. 11 of its 13 entries are hashes of pulled
  files that were not archived, so only 2 can be re-checked with `shasum -c`;
  the blank-token check has its own `SHA256SUMS`, and all 4 of its entries
  verify.

### Not checked, or checked more weakly than it may look

- **Review scope.** The review covered the phase-3 layers. The phase-1
  protocol code and the Android runtime rigs were not reviewed line by line.
  Most code, including the fixes, was written by delegated agents.
- **Circularity.** Most tests still check our components against each other,
  or against models derived from bytecode. Only the R7 runtime runs involve
  Plaud's real code, and they cover Android, the legacy protocol and Bluetooth
  only. R7-S14 exchanged no Wi-Fi message.
- **CI.** Clean-clone CI runs were simulated on macOS arm64. No GitHub run of
  the fixed workflow exists. Linux numerics could flip the diarizer's
  speaker-count tests; that was not observed and cannot be tested here.
- **Docker.** One run, on arm64, with a base image already present.
- **Model numbers.** n = 4 meetings per set, measured under contention, with
  one observed ASR divergence. The segmentation model may have seen AMI in
  training.
- **Piper voice licence.** The voice is fine-tuned from the lessac base voice.
  The lessac dataset's licence page is a research-only agreement. Whether it
  reaches fine-tuned weights, and audio made with them, is unresolved
  (`docs/generator.md` §6.2).
- **Test counts** include parametrised cases, so the count of distinct test
  functions is lower.
- **Evidence-graph edges** are produced by pattern-matching citation text.

## 11. Numbers

| Tests by area (collected 25 Sep) | 24 Sep | 25 Sep |
|---|---:|---:|
| Phase 1 protocol and emulator | 301 | 301 |
| End-of-data frame and stream abort (R7-S13) | 15 | 20 |
| V2 fault matrix and receiver model | 109 | 115 |
| Wi-Fi transfer | 135 | 156 |
| Generator and V4 round trip | 82 | 136 |
| Evals | 137 | 198 |
| AMI converter | – | 51 |
| V5 gate calibration | – | 8 |
| Pipeline | 104 | 228 |
| Mock cloud | 43 | 76 |
| Cross-layer integration and V4 end to end | 21 | 21 |
| Compose | 31 | 59 |
| **Total** | **978** | **1369** |

The last full run: `1365 passed, 4 skipped, 11 warnings in 339.00s (0:05:38)`.

| Code and documents (`wc -l`, re-counted 28 Sep) | Lines |
|---|---:|
| `emulator/plaudsim/`, including phase-1 code | 6 731 |
| `generator/` | 4 081 |
| `evals/` | 3 902 |
| `pipeline/` | 4 934 |
| `mockcloud/` | 2 766 |
| `emulator/serve.py` and `docker/` Python | 1 202 |
| Tests, 80 Python files | 24 444 |
| Kotlin runtime drivers, 3 files | 678 |
| Documents in `docs/` and `docs/architecture/` | 11 411 |

| V2 matrix outcomes | Rows |
|---|---:|
| Recovered byte-exact | 38 |
| Recovered after the app resumes from its cursor | 6 |
| Failure detected, no corrupted output | 15 |
| Corruption risk (a device that never abandons a stream) | 1 |
| *Outside the count:* payload bit-flip; the SDK cannot detect it, the harness's invariant check flags it | 1 |
| *Outside the count:* restart that serves another file revision; the same | 1 |

| V6 run, 25 Sep | Value |
|---|---:|
| Image build (not timed against the target) | 121.38 s |
| `up -d --wait` to healthy | 6.58 s |
| Job, as timed by the smoke script | 11.34 s |
| Job, as timed inside the container | 10.09 s |

## 12. Open items

| Item | Blocked on | What would unblock it |
|---|---|---|
| V5 target on the full AMI test split | Audio for 12 of the 16 test meetings is not on this machine | Download the other 12 headset-mix WAVs (CC BY 4.0), convert them, run `scripts/run-v5.sh` and score the `ami-headset` suite. |
| Diarizer calibration | The default threshold gives 35–95 clusters on 4-speaker meetings; calibrating on test meetings would be wrong | Calibrate `cluster_threshold` on a development split (AMI dev, 18 meetings). |
| ASR nondeterminism on long audio | EN2002a gave three different transcripts | Seed CTranslate2 per run, or decode at temperature 0 only, or at least record per-segment temperatures. |
| meeteval's 20-speaker limit | `evals batch` drops a whole meeting when a side has more than 20 speakers | Report DER and JER anyway and record cpWER as unavailable with the reason. |
| sherpa diarization near chance on the Piper voices | Whole-turn embeddings near chance; not traced | A focused investigation before the Piper set is used to judge any diarizer. |
| Wi-Fi path with the real SDK | The emulated phone cannot join a hotspot | A real access point the emulated phone can join, or a physical phone. |
| iOS SDK at runtime | No Xcode here (command-line tools only; 22 GB free; Xcode needs about 15 GB and an Apple ID) | Xcode with a simulator, plus a Bluetooth bridge. |
| Real device behaviour: advertising branch, end-of-data code and TAIL (U15, U17, U18), storage units, settings values, the opcode-10 mode byte | No device | One Bluetooth capture of any legacy Plaud recorder. |
| What real firmware records (U20) | No real recording | One recording the operator lawfully owns. |
| Encrypted protocol with the real SDK (CRED-1) | Key material issued by Plaud's cloud | Legitimate partner credentials. Deliberately not pursued otherwise. |
| Piper voice licence | The lessac base voice's dataset licence is research-only; its reach is unresolved | An owner's decision before any audio made with this voice is redistributed. |
| GitHub CI | The fixed workflow is uncommitted | Commit, then read the first GitHub run. |
| Document follow-ups | Not in this pass's scope | Fold the checker's six points and the blank-token check into `r7/r7-s14-wifi-real-sdk.md`. Note that 3 of the 22 logged requests (R7-S13 runs 11–13) never reached the server, wherever a document still says every request was rejected with 401: `r7/r7-s12-k3-runtime-capture.md`, `docs/final-product-reconstruction.md`, D7 in `r7/r7-s14-wifi-real-sdk.md`, and the comments in the three Kotlin debug drivers. (`r7/cloud-endpoint-inventory.md` and `r7/r7-s13-recording-pull.md` were corrected on 28 Sep.) In `docs/compose.md` §2.1, say that Compose and Buildx are CLI plugins in `~/.docker/cli-plugins`, not under `~/.local`. |
| Commit the work | The owner's decision | Review, then commit. |

## 13. Rules kept throughout

- No Plaud device was touched, and no credential was used or sought. Every
  identifier and token is synthetic and labelled as such.
- ~~No Plaud endpoint was called.~~ **Correction, 25 September 2026:** this
  rule was not kept in the Android runtime runs. We never called a Plaud
  endpoint from our own code. The official SDK, initialised by our debug
  drivers with a synthetic token, sent one `gen-key` request per app start to
  `platform-jp.plaud.ai`. The request is visible in 22 archived logs, and all
  19 visible responses were 401. The drivers now pass a blank token (section
  8.6).
- Nothing under `reference/` was modified. That was checked after every phase
  and again on 25 Sep.
- Evidence classes are kept apart: bytecode-proven, runtime-proven, official
  documentation, observed by third parties, inferred, harness policy and
  unknown. Harness choices are labelled in code and in documents.
- A test Wi-Fi password committed in one of Plaud's public repositories is
  noted by existence only and not reproduced anywhere.

## 14. Repository state

- `main` holds two commits: the 21 Sep scaffold `09c8922` and `70249ba`, the
  work to 24 Sep. Both were pushed to `github.com/ORION2809/PLAUDE_EMULATOR_REV-ENG`
  (public) on 25 Sep.
- On GitHub, the `tests` workflow failed on `70249ba` and the `evals`
  workflow passed (section 8.2).
- Everything from 25 Sep is uncommitted. At the time of writing, `git status`
  lists 157 entries: 128 modified tracked files and 29 untracked entries
  holding 98 files. They include the CI fix, the two test fixtures, the review
  fixes, stage 2, R7-S14 and these documents.
- `build/`, `data/` and `reference/` are git-ignored. `*.ogg` and `*.opus`
  are ignored too, except the two test fixtures and the two R7-S13 export
  files the R7-S13 report cites.

To reproduce:

```bash
cd plaud-harness                    # your clone
./scripts/fetch-references.sh      # 33 pinned repositories into reference/
./scripts/build-evidence.sh        # decompile the SDK into build/evidence/ (checks the AAR hash)
python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt
.venv/bin/pip install -r requirements/models.txt   # optional: real models and Piper (piper-tts is GPL-3.0-or-later)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120
bash scripts/local-up.sh            # whole topology without Docker
./scripts/compose-smoke.sh          # the same under Docker, where a daemon exists
```

The real-SDK runs also need the Android SDK, an API-34 emulator image, JDK 17
and Gradle 8.2. `r7/r7-s12-k3-runtime-capture.md` and
`r7/r7-s13-recording-pull.md` give the exact steps. `scripts/run-v5.sh`
reproduces the V5-style measurement; it needs the model weights, the AMI
files and the Piper voice.

## 15. Where things are

| Question | Document |
|---|---|
| Standing status and next steps | `PROJECT.md` §8 |
| The Bluetooth protocol, with evidence | `docs/protocol-ledger.md` (§15 covers the real-SDK pull) |
| What earlier work got wrong | `docs/reconstruction-log.md` |
| Open questions | `docs/final-uncertainty-matrix.json` |
| Real-SDK runs | `r7/r7-s12-k3-runtime-capture.md`, `r7/r7-s13-recording-pull.md`, `r7/r7-s13-evidence/`, `r7/r7-s14-wifi-real-sdk.md`, `r7/r7-s14-evidence/` |
| Cloud contact by the SDK | `r7/r7-s14-wifi-real-sdk.md` (D7), `r7/cloud-endpoint-inventory.md`, `r7/r7-s14-evidence/blank-token-offline-check/` |
| First real-model numbers | `docs/v5-results.md`, `scripts/run-v5.sh`, `build/v5/` (committed evidence) |
| Optional model and TTS packages | `requirements/models.txt` |
| The product above the radio | `docs/final-product-reconstruction.md`, `docs/product-ledger.md`, `docs/architecture/`, `docs/source-map.md` |
| Harness layers | `docs/v2-fault-matrix.md`, `docs/wifi-transport.md`, `docs/generator.md`, `docs/evals.md`, `docs/pipeline.md`, `docs/mockcloud.md`, `docs/integration.md`, `docs/compose.md` |
| The Docker run | `docs/compose.md`, `build/v6/compose-smoke-2026-09-25.log` (committed evidence) |

---

*Compiled on 25 September 2026 from the repository working tree, test runs,
verifier runs, the archived runtime logs and the review and verification
records of 25 September. Figures were measured on the day unless the text
says otherwise.*
