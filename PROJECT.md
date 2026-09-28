# PROJECT.md — plaud-harness

> **This file is the single source of truth for the project.**
> Any new session, any new collaborator, any future me: read this file first and you
> have full context. Update it at the end of every working session. Nothing important
> lives only in a chat log.

---

## 0. One-line pitch

A hardware-free, production-grade reverse-engineering harness for the Plaud Note Pro /
NotePin BLE recorder: reconstruct the device protocol from shipped artifacts, emulate the
device in software, and prove the reconstruction is correct by making Plaud's own SDK talk
to the emulator.

## 1. Why this exists

This is a **personal portfolio project** demonstrating reverse-engineering expertise. It is
not a Mivi project and has no commercial deliverable. Constraints that follow from that:

- **No hardware.** Zero. The whole point is that the harness substitutes for the device.
- **Production-grade.** CI, metrics, gates, reproducibility. Not a notebook.
- **Falsifiable.** Every claim in the README has a green check behind it or it doesn't ship.

### The money claim

> *"I reconstructed Plaud's BLE protocol from their shipped binaries and built an emulator
> that their own SDK connects to and successfully pulls a recording from."*

That is binary — it works or it doesn't. Everything else in this repo exists to support
that sentence or to prove the pipeline behind it is real.

## 2. Legal posture — read once, then stop worrying

| Thing | Status |
|---|---|
| `plaud-sdk-public` repo | Apache 2.0. Template apps are source. Safe to read and quote. |
| `sdk/` binaries inside it | Explicitly **proprietary, separate license**. Analysing them for interoperability is the classic protected case; do not redistribute them. |
| Vendoring the binaries into this repo | **No.** `scripts/fetch-references.sh` fetches them at analysis time (`scripts/fetch-sdk.sh` is a deprecated shim). They are git-ignored, and so are Gradle build folders, which hold a dexed copy. |
| AGPL (`openplaud`, `riffado`) | Personal use is unconstrained. If we publish this repo and derive from their code, this repo goes AGPL too. That is fine and costs us nothing. Decision: **AGPL-3.0 this repo** so the option stays open. |
| Firmware / app decompilation | **Not needed, not doing it.** Everything so far came from artifacts Plaud published themselves. Keep it that way — it is both the cleaner story and the stronger one. |

## 3. Architecture — four layers

```
  Layer 1  Device emulator        Bumble BLE peripheral, virtual-link transport, no radio
  Layer 2  Synthetic meetings     TTS + pyroomacoustics, ground truth by construction
  Layer 3  Eval harness           jiwer / pyannote.metrics / meeteval + CI gates
  Layer 4  Mock cloud             FastAPI mock of api.plaud.ai + GoReplay
```

Layers 1 and 2 are independent and can be built in parallel. 3 depends on 2. 4 is last
and cheapest.

## 4. Milestones

Two different things used to share the labels R1–R6, which made "R3" ambiguous
(file-sync reconstruction, or the real-SDK integration?). They are now separate.

### 4a. Reconstruction milestones — what we have recovered and emulate

A milestone is **FROZEN — AUDITED** only when: the implementation exists, tests
exist, the tests challenge failure modes, the docs agree with the code, evidence
supports every claim, unknowns are explicitly preserved, no circular validation
dominates, `reference/**` is unchanged, and the repo is clean. Otherwise
**PARTIAL — NOT FROZEN**.

| | Milestone | Status | Evidence |
|---|---|---|---|
| R1a | `getState` — opcode 3, 18-byte GetStateRsp | FROZEN — AUDITED | DIRECT |
| R1b | `getStorage` — opcode 6, u64 free@3/total@11/duration@19; plus `battStatus` opcode 9 | FROZEN — AUDITED | DIRECT layout/names; units UNKNOWN |
| R1c | `syncTime` — opcode 4, u32le unix + **i8** tzH + **i8** tzM; MTU | FROZEN — AUDITED | DIRECT |
| R1d | Connection lifecycle, both platforms, handshake boundary guarded | FROZEN — AUDITED | SOURCE-DERIVED |
| R1e | Advertising: `u4.a(ScanResult)` parse rules and the portVersion declaration | FROZEN — AUDITED | SOURCE-DERIVED; which branch real hardware takes is UNKNOWN |
| R2a | BLE handshake, **device side, legacy path** — the emulator answers k3/j3 with l3 and w2 with x2, and reaches BOUND | FROZEN — AUDITED | DIRECT; accepting any token is POLICY |
| R2b | BLE handshake, **client side / portVersion ≥ 20** — marker framing, chunking, secret package, ChaCha envelope | **PARTIAL — NOT FROZEN**, structurally blocked | layouts DIRECT; **credentials are cloud-issued** |
| R3 | Recording/file sync — syncFile, stopSync, file list, resume, delete | FROZEN — AUDITED | DIRECT after 5 corrections |
| R4-S1 | Transport seam for real-SDK integration — SDK call chain traced, seam identified at the virtual-controller (netsim) layer, no replaceable GATT abstraction inside the SDK | COMPLETE — SEAM IDENTIFIED, INTEGRATION BLOCKED (pending: template APK build with valid partner token, booted AVD, `bumble[android]` wiring) | SOURCE-DERIVED; ledger §13 |
| R4-S2 | Android↔netsim↔Bumble transport spike — generic ping/pong app on AVD ↔ Bumble peripheral, both directions evidenced in logs | COMPLETE — VERIFIED (`r4-s2/`, disposable) | logcat + Bumble logs, 2026-09-22 |
| R4-S3 | Real Plaud Android SDK against the Bumble emulator — scan→connect→MTU 517→discover→subscribe 2BB0→first_handshake, stopping at `bind_token_empty` (no partner token) | COMPLETE — PLAUD SDK REACHED BLE RUNTIME (`r4-s3/`, disposable) | logcat + Bumble ATT logs, 2026-09-22 |
| R4-S4 | Credential provenance — token chain traced to `local.properties`/Settings override → `initSDK` → gen-key/sn-sign; placeholders only in repo, no test-credential mechanism | COMPLETE — LEGITIMATE CREDENTIAL UNAVAILABLE (`r4-s4/`, read-only) | source trace, 2026-09-22 |
| R5-S1 | SDK↔peripheral GATT audit — single-peripheral re-run reaches `first_handshake → bind_token_empty` with full 1910/2BB0+CCCD/2BB1/180F discovery in the Android GATT DB and CCCD 0200 on Bumble; no peripheral change | COMPLETE — SDK GATT BOUNDARY MATCHED (`r5-s1/`, disposable) | logcat + Bumble ATT logs, 2026-09-22 |
| R5-S2 | First-handshake outbound audit — `q.a` guard precedes any k3 build; construction (packHead/255/stage-iff-pv≥3/width-iff-pv≥9/`'0'`-pad/u4.b trim) and write path (1910/2BB1, with-response, waiters {1,2}) re-derived from javap; agrees with R2/R2a | COMPLETE — HANDSHAKE CONSTRUCTION AUDITED (`r5-s2/`, read-only) | local mechanical verifier, 2026-09-22 |
| R5-S3 | Second-handshake audit — `d0` sole caller is the x2 branch (r3.e gate); `e/i`+`f/j` provenance traced to `t3.a` params (template leaves both `""`); l3 status 0 → `c0()` → m7 syncTime; no contradiction | COMPLETE — J3 STRUCTURE AUDITED (`r5-s3/`, read-only) | local mechanical verifier, 2026-09-22 |
| R5-S4 | l3/response-transition audit — unguarded status@3/pv@4/tz@6, guard ladder 8–12, end-read LE u24 version, Java defaults; status 0 → `old_protocol_ok` → `c0()` → m7 (9 B, signed tz); no contradiction | COMPLETE — L3 AND POST-HANDSHAKE PATH AUDITED (`r5-s4/`, read-only) | local mechanical verifier, 2026-09-22 |
| R5-S5 | Modern-handshake audit — set_data_notify → `z.y()` → pv≥20 → `n()`/`a0()` marker pre-handshake → RSA-established J/K/L → shared post-key methods with per-frame ChaCha seal; no contradiction | COMPLETE — MODERN HANDSHAKE AUDITED (`r5-s5/`, read-only) | local mechanical verifier, 2026-09-22 |
| R5-S6 | Sealed-transport audit — M=1→pre-increment/seq (first sealed seq 2), N=-1→accept-and-set, seq-inside-AEAD proven by stack accounting, queue-add returns, no transport ACK; no contradiction | COMPLETE — MODERN TRANSPORT SEMANTICS AUDITED (`r5-s6/`, read-only) | local mechanical verifier, 2026-09-22 |
| R5-S7 | First sealed slice — synthetic J/K/L fixture, M/N wired per R5-S6, sealed GetState req seq 2 → rsp seq 3 in-process and over Bumble GATT (replay dropped, AEAD failure leaves N); RFC 8439 vector pins the AEAD wiring | COMPLETE — MODERN SEALED GETSTATE ROUND-TRIP VERIFIED (`r5-s7/`, synthetic test-only, no credentials) | new verifier + 16 tests, 2026-09-23 |
| R5-S8 | Independent sealed endpoints — M_host/N_host + M_device/N_device (first TX 2 each side); sealed FileList (totals/session/size/scene/attribute) + sealed SyncFile (HEAD + type-2 DATA + TAIL/CRC, payload reassembled) over frozen R3 framing; replay/stale/tamper + Bumble GATT legs | COMPLETE — INDEPENDENT SEALED FILE SYNC VERIFIED (`r5-s8/`, synthetic test-only, no credentials) | new verifier + 18 tests, 2026-09-23 |
| R5-S9 | Synthetic modern handshake-to-filesync — test-only pv≥20 peripheral (FE10/FE20→FE11→FE12→RSA→J/K/L→sealed); FE20 index-0 clear; sealed GetState/FileList/SyncFile with independent counters; inners byte-identical frozen R3; replay/tamper; all over Bumble GATT | COMPLETE — MODERN SYNTHETIC HANDSHAKE-TO-FILESYNC INTEGRATED (`r5-s9/`, synthetic test-only, no credentials) | new verifier + 7 tests, 2026-09-23 |
| R5-S10 | FE11/timing audit — marker-only validation, last-chunk gating, newest-match-wins with eviction, FE11-before-final-chunk swallowed, no-dedup retransmit, 1 s/-98 transport + 10 s/30 s global timeouts, FE20 device effect UNKNOWN (G touched only in z.y()); zero emulator changes | COMPLETE — FE11/TIMING MECHANICALLY AUDITED (`r5-s10/`, read-only, javap) | new verifier + 1 test, 2026-09-23 |
| R5-S11 | Uncertainty isolation — 33-behavior matrix (15/3/4/5/6 across SDK/INTEGRATION/INFERRED/POLICY/UNKNOWN); ModernProfile isolates fe20-clear/fe11-payload/device-TX-start; raw-authoritative trace schema + golden replay + emulator-vs-future comparison kinds; R5-S9 defaults unchanged | COMPLETE — MODERN UNCERTAINTY ISOLATED AND TRACE-READY (`r5-s11/`, synthetic only) | new verifier + 9 tests, 2026-09-23 |
| R6-S1 | Audio-path archaeology — BLE→file→decrypt→Ogg/Opus→PCM chain traced (o4/v3 passthrough, AudioExporter file path evidenced; g4/h4/k4/l4/m4/n4 + OggOpusParser + header/RSA/ChaCha contracts recovered); `audio.py` implements proven Java-layer contracts only; no decoder executed (no libopus here), no Plaud audio bytes in repo | COMPLETE — AUDIO CONTRACT PARTIALLY RECONSTRUCTED (`r6-s1/`, synthetic fixtures only) | new verifier + 16 tests, 2026-09-23 |
| R6-S2 | Independent validation — PyAV/libopus fixtures (16 kHz mono/stereo, 32 kbps CBR, sha-pinned, SYNTHETIC); R6 extraction byte-identical to demuxer; extracted packets decode independently (960 samples @48 kHz); full-decode correlation 0.9999; standard Ogg shown structurally distinct from h4 wire framing | COMPLETE — GENERIC OGG/OPUS VERIFIED; PLAUD FORMAT REMAINS UNCONFIRMED (`r6-s2/`) | new verifier + 10 tests, 2026-09-23 |
| R6-S3 | Authentic-fixture discovery sweep — whole worktree searched (audio globs, large binaries, SDK res/raw, iOS Resources, templates, third-party, git history); candidates classified+hashed (opik e2e media, xiaozhi TTS prompts, riffado sample.mp3, upstream assets all rejected with reasons); SDK ships zero audio resources; no code/tests added per stop rules | COMPLETE — NO AUTHENTIC PLAUD AUDIO FIXTURE FOUND; EXTERNAL EVIDENCE REQUIRED (`r6-s3/`, read-only) | discovery record only, 2026-09-23 |
| R6-S4 | Ingestion readiness — new-file scan since R6-S3 returns only project outputs; no legitimate external recording available; stop per stop rule (no synthetic fixture, no pipeline change); ingestion path + evidence requirements documented | BLOCKED — AUTHENTIC PLAUD RECORDING NOT AVAILABLE (`r6-s4/`, readiness note only) | readiness record only, 2026-09-23 |
| R7-S12a | Genuine SDK k3 over android-netsim — `recoveryConnectBleDevice(device, synthetic id)` builds k3 byte-exact vs `build_k3`; emulator accepts it; `bleBind status=0`; empty-id falsifier writes nothing; truncation confirmed | COMPLETE — RUNTIME_PROVEN + EMULATOR_INTEGRATION_PROVEN (`r7/r7-s12-k3-runtime-capture.md`) | 4 runtime runs + 8 tests, 2026-09-23 |
| R7-S12b | Opcode 8 CommonSettings (q0→r0) discovered at runtime and fully resolved — 21-entry non-ordinal CommonType table, consumer `q.f`; two READs RUNTIME_PROVEN | COMPLETE — OPCODE 8 RECONSTRUCTED + IMPLEMENTED (ledger §5.12) | 19 tests, 2026-09-23 |
| R7-S12c | Cloud endpoint inventory — 50 endpoints across 5 surfaces, static only, none exercised; cloud≠local-writer distinction fixed. **Correction 2026-09-25:** "none exercised" was wrong for `partner/sdk/gen-key` — the SDK, initialised by our drivers with a synthetic token, POSTed it once per app start (every visible response 401); drivers now pass a blank token (§8) | COMPLETE — CLOUD_OBSERVED, ALL EXTERNALLY BLOCKED (`r7/cloud-endpoint-inventory.md`) | inventory, 2026-09-23 |
| R7-S12d | Corrections — R3 resend is BYTECODE_PROVEN (gap→restart, stopSync only on 5 s timeout); AES-GCM scoped Wi-Fi-only; opcode 138 added to ledger; bleBind facade quirk proven | COMPLETE — LEDGER RECONCILED | ledger §5.8/§5.9/§4.3/§5.13/§14 |

**R2 splits in two, and earlier work had collapsed both halves into one
"blocked".** `q.b0()` goes straight to first_handshake whenever the *advertised*
portVersion is below 20, and first_handshake's only precondition is a non-empty
token string — no key, no signature, no cryptography at all. So the legacy
handshake is: the host sends a token, and the **device** returns a status.

* Binding a client **we** wrote to **real Plaud hardware** — blocked, structurally.
  Plaud's cloud issues the RSA key pair (`/sdk/gen-key` returns the *private*
  key), the SN signature (`/sdk/sn-sign`) and the 32-hex token.
* Binding **Plaud's real client** to a device **we** wrote — **not blocked.**
  Accepting the token is the device's decision, and we are the device.

The emulator implements the device side and reaches BOUND. Accepting any token is
HARNESS POLICY, labelled as such; `accept_any_token=False` drives the refusal
path. `PlaudPeripheral` still refuses to construct with a portVersion ≥ 20,
because above that the SDK seals every frame with ChaCha20-Poly1305 and a
cleartext peripheral would be lying about what it speaks.

Full detail: [`docs/protocol-ledger.md`](docs/protocol-ledger.md).
What changed in the 2026-09-22 audit and why: [`docs/reconstruction-log.md`](docs/reconstruction-log.md).

### 4b. Validation rungs — what the project claims end to end

Each rung is a test that either passes or fails. Nothing subjective.

- [x] **V1** Emulator advertises, is discovered by scan, and completes a full
  control session following the SDK's connect stage order — first_handshake →
  handshake_get_ssn → battery → sync_time (where `bleBind` fires) →
  getState/getStorage/file list → a complete `syncFile`. 301 tests, no radio,
  no hardware. This is a state-machine rehearsal with Python-written bytes;
  the real SDK's own k3 was later captured against the emulator over
  android-netsim (V3-partial, R7-S12).
- [x] **V2** Emulator survives a fault-injection suite without corrupting a transfer —
  60 counted cells plus 2 content-fault rows over Bumble (`tests/test_v2_fault_matrix.py`,
  `docs/v2-fault-matrix.md`; 61 rows on 2026-09-24, when the bit-flip row was counted):
  drops, duplicates, reorder, truncation, wrong session, TAIL/HEAD faults, disconnect and
  resume, stopSync mid-transfer, MTU 23–517, CCCD modes, delete during sync, back-to-back
  syncs, and the R7-S13 sentinel cases; every cell is byte-exact or a detected failure,
  scored by a bytecode-derived receiver model (`tests/fault_support.py`) that was
  corrected against the runtime facts. Outcomes: 38 recovered, 6 recovered by app resume,
  15 detected, 1 corruption-risk (the run-6 device that never abandons its stream).
  Outside the count: a payload bit-flip (undetectable by the SDK: no CRC on DATA) and a
  restart serving another file revision; the harness's invariant check flags both. The
  2026-09-25 review (T10) showed that "no silent corruption" holds by construction in
  every counted cell, so the matrix tests completion, not content integrity.
- [x] **V3** **Real `plaud-sdk-public` connects to the emulator over
  `android-netsim` and pulls a recording** — ACHIEVED for the transfer path
  (R7-S13, 2026-09-23/24). The unmodified AAR on an AVD, driven through the
  public API with a SYNTHETIC id, bound (R7-S12), listed the served file,
  pulled it through both `syncFile` (raw collector) and `exportAudio(OPUS)`,
  and delivered bytes hashing identically to the served Ogg/Opus fixture;
  its gap recovery (stopSync → syncFileStart from cursor) converged
  byte-exact in all four gap runs (6b, 6c, 6d, 13). Doing so falsified the frozen HEAD·DATA·TAIL sequence:
  the client completes only on an EMPTY_PACKAGE sentinel before the TAIL
  (emulator corrected). Still NOT done: binding OUR client to real hardware
  (blocked — token/snSignature/RSA keys are cloud-issued, CRED-1) and anything
  above portVersion 20. See `r7/r7-s13-recording-pull.md`, ledger §15.
- [x] **V4** Synthetic generator's ground truth round-trips through `pyannote.metrics` at DER 0 —
  `tests/test_v4_der_roundtrip.py` (RTTM vs itself and vs an annotation rebuilt from the
  sample-level activity mask: DER = JER = 0.0 exactly; a shifted copy > 0) and end to end
  `tests/test_v4_e2e.py` (generator → oracle pipeline on the device Ogg → evals: DER, JER,
  cpWER, tcpWER all 0.0; perturbations move every metric the right way; a speaker swap
  leaves cpWER at 0 while literal WER lies — the decision-log rationale, asserted).
- [ ] **V5** Full pipeline hits target cpWER / DER on held-out AMI — **NOT MET; a subset
  was measured (2026-09-25).** `whisper-sherpa` (faster-whisper small.en + sherpa-onnx
  diarization, weights sha256-pinned) ran at full length on 4 of the 16 AMI test-split
  meetings (EN2002a, ES2004a, IS1009a, TS3003a; the only ones with audio here) and on a
  4-meeting synthetic Piper set (`docs/v5-results.md`, `scripts/run-v5.sh`). With the
  reference speaker count as a hint (an oracle value): AMI macro DER 0.6557, cpWER 0.8533,
  WER 0.3250; Piper `mix.wav` DER 0.6149, cpWER 1.0993, WER 0.2501. These numbers are
  poor. Without the hint sherpa found 95/35/36/47 clusters on the 4-speaker AMI meetings,
  and `evals batch` could score none of them (meeteval refuses > 20 speakers). Two
  regression-gate suites are calibrated on the hinted runs. The V5 target — the full
  16-meeting test split, suite `ami-headset` — remains unmeasured.
- [x] **V6** `docker compose up` brings the whole system live in under 60 seconds —
  **executed under Docker once (2026-09-25).** `scripts/compose-smoke.sh` on this M1 with
  Colima (Linux arm64 VM, Docker daemon 29.5.2): images built in 121.38 s (excluded from the
  timed window by design), `up -d --wait` healthy in **6.58 s** (target 60 s), job exit 0 in
  11.34 s — probes, scan, 2 generated meetings, oracle pipeline + evals, and a mock-cloud
  round trip (`build/v6/compose-smoke-2026-09-25.log`, `docs/compose.md`). Not run on
  x86_64 or on a GitHub runner. The job's zeros are harness self-tests, not system scores.

---

## 5. CONFIRMED FINDINGS

> Everything in this section was recovered from published artifacts. Each row names its source
> so any claim can be re-derived from scratch. Full detail in `docs/protocol-spec.md`.

### 5.1 The source-vs-binary fork — RESOLVED

`plaud-sdk-public` ships **pre-compiled** SDKs, not source:

- iOS: `PlaudBleSDK.framework`, `PlaudWiFiSDK.framework`, `PlaudDeviceBasicSDK.framework`
- Android: `plaud-sdk.aar` (2.7 MB)
- Template apps **are** source: Swift (`ios/PlaudTemplateApp/`) and Kotlin (`android/app/src/main/java/com/plaud/template/`)

This looked like it meant grinding Mach-O disassembly. **It does not.** Two artifacts make
that unnecessary:

1. **`arm64-apple-ios.swiftinterface`** ships inside every `.framework`. That is a textual,
   source-level, **unobfuscated** declaration of the entire public Swift API — type names,
   enum cases, method signatures. Handed to us for free.
2. **`plaud-sdk.aar`** decompiles cleanly with jadx. ProGuard renamed the identifiers but
   **constants keep their values** — UUIDs, opcodes, magic numbers are all intact.

### 5.2 The technique that makes this project interesting

> **The Android jar is obfuscated but structurally complete. The Swift interface is
> unobfuscated but structurally thin. Cross-reference the two and you recover both the
> structure and the names.**

`com/plaud/sdk/proto/w$a.java` gives an opcode table of ~80 integers with names mangled to
`a, b, c, … z, a0, b0`. The Swift interface gives the same domain concepts with real names
(`RecScene`, `VadSensitivity`, `CommonAction.Read/Set`). Aligning them recovers the semantic
map that neither artifact contains on its own. This is the part worth writing up.

### 5.3 The device is a Tinno ODM platform

The decompiled jar contains `com/tinnotech/penblesdk/**` — `TntBleCommUtils`, `BleFile`,
`BleDevice`. Plaud's device is built on **TinnoTech's Pen BLE SDK**, an ODM platform. The
protocol we are reconstructing is substantially Tinno's, not Plaud's. Consequences:

- Other Tinno-based pen recorders likely speak a near-identical protocol → the emulator
  generalises, which is a much better portfolio story than "I cloned one device."
- Worth a search for other Tinno pen SDKs as a cross-check on our reconstruction.

### 5.4 GATT profile — RECOVERED

Source: `com/plaud/sdk/proto/w$d.java`

| UUID | Role |
|---|---|
| `00001910-…` | Plaud/Tinno primary service |
| `00002BB0-…` | characteristic — **Notify/Indicate** (device → host responses/stream) |
| `00002BB1-…` | characteristic — **Write** (host → device commands) |
| `00002902-…` | CCCD (standard) |
| `0000180F-…` | Battery Service (standard) |
| `00002A19-…` | Battery Level (standard) |

Confirmed from decompiled Android `com.plaud.sdk.proto.z`: incoming `onCharacteristicChanged` dispatches on `2BB0`; command writes resolve `2BB1`. Notification versus indication is selected dynamically from discovered properties.

### 5.5 Framing — PARTIAL

Source: `com/plaud/sdk/proto/w$a`, `w$c`

Magic / sync constants:

| Value | Hex | Appears in |
|---|---|---|
| 65040 | `0xFE10` | `w$c` |
| 65041 | `0xFE11` | `w$a` |
| 65042 | `0xFE12` | both |
| 65056 | `0xFE20` | `w$c` |

Contiguous `0xFE1x`/`0xFE2x` block ⇒ these are channel or frame-type discriminators, not
opcodes.

`TntBleCommUtils` (JNI, `libtnt_ble_utils.so`) exposes the codec primitives. ARM64 disassembly confirms CRC-16/CCITT-FALSE (`poly=0x1021`, `init=0xFFFF`, non-reflected, `xorout=0`). `NiceBuildSdk` calls it as `tntGetCrc(bytes, bytes.length, 65535)`; `tntGetFileCrc(path, 65535)` iterates the entire file.

```
native int   packInt(int widthBits, byte[] buf, int off, long value)   // 8/16/24/32/64
native long  readInt(int widthBits, byte[] buf, int off)
native short tntGetCrc(byte[] buf, int off, int len)                   // CRC-16
native int   tntGetFileCrc(String path, int len)                       // whole-file CRC
```

A pure-Java helper in the same class serialises little-endian, so **the wire format is
little-endian**. The CRC helper's control-frame coverage remains unknown.

### 5.6 Two opcode tables — DIRECTION SETTLED

`w$a` and `w$c` each hold ~70 distinct values and overlap heavily, so the overlap
says nothing about direction. The opcodes **unique** to one table settle it: of the
13 unique to `w$a`, 11 are claimed by *response* classes; of the 11 unique to `w$c`,
10 are claimed by *request* classes.

> **`w$c` is the host→device (request) table. `w$a` is the device→host
> (response/notify) table.** The alphabetical order invites the opposite guess.

The marker constants agree: both host-sent markers (`0xFE10`, `0xFE20`) are
`w$c`-only, and the device-sent `0xFE11` is `w$a`-only. Asserted mechanically by
`test_w_c_is_the_request_table_and_w_a_the_response_table`.

Request and response opcodes are **not** always equal. The familiar symmetric pairs
(3, 4, 6, 9, 22, 26, 28) invite a false rule; counter-examples include request 29
(`z6` stopSync) → response 30 (`a7`), request 30 (`w6` deleteFile) → response 31
(`x6`), request 24 (`k2`) → response 33 (`l2`).

### 5.7 Device capability surface

From the Swift interface (`PlaudBleSDK`) — real names, no guessing:

- `CommonAction`: `Read`, `Set` — a generic get/set settings channel
- `CommonType`: 21 settings — `LightDuration`, `LightBright`, `Language`, `AutoClear`, `VAD`,
  `RecScene`, `RecMode`, `VadSensitivity`, `VpuGain`, `BatteryMode`, `MicGain`, `WiFiChannel`,
  `SwitchHandle`, `AutoPowerOff`, `RawWaveEnabled`, `RecordingAfterDisConnet`, `SyncWhenIdle`,
  `FindMyState`, `VPUCLK`, `StopRecordAfterCharging`, `IBeaconWakeup`
- `RecScene`: `Unknown, Normal, Interview, Classroom, Music, Meeting, Memo`
- `RecMode`: `Normal, NC` (noise cancelling)
- `VadSensitivity`: `Quality, lowBitrate, Normal, Aggressive`
- `WebsocketType`: `url, serToken, devToken` — the Wi-Fi fast-transfer handshake
- Crypto: `SwiftyRSA` is vendored → **pairing/auth involves RSA**. → Q3

`VPUCLK` / `VpuGain` / `MicGain` as separate settings confirm a voice-pickup unit distinct
from the mics — the vibration-conduction sensor, exposed as a first-class tunable.

### 5.8 Two transports

`PlaudBleSDK` + `PlaudWiFiSDK` (which vendors `JXWebSocketServer.h`).

**Correction 2026-09-22:** bulk recording transfer is **not** Wi-Fi-only. There is a
complete BLE transfer path — `syncFile` (opcode 28) → HEAD → protocol-type-2 data
frames → TAIL (opcode 29) — with its own cursor tracking and loss recovery, and it is
what `PlaudDeviceAgent.syncFile(sessionId, start, end)` drives. Wi-Fi is an
*additional*, faster path (`startWifiTransfer` / `exportAudioViaWiFi`), not the only
one. The emulator implements the BLE path; the Wi-Fi path is not implemented.
(Correction 2026-09-25: the Wi-Fi path is now implemented on the pen side and tested
against a bytecode-derived phone double only — `docs/wifi-transport.md`; the real SDK's
Wi-Fi transfer stopped at the hotspot join in R7-S14.)

Audio: the SDK **enforces** an Opus codec geometry of 16 kHz, 20 ms frames,
32 kbps CBR, exactly 80 bytes per frame per channel natively in `libjni_ogg`
(BYTECODE_PROVEN). The container is selected by a **sniff** (512-byte `PLAUD.AI`
header test, then an `OggS` 4-byte test), either a complete Ogg stream or a bare
concatenation of fixed-size Opus packets, optionally behind the encryption header.
**Which of those shapes real firmware actually emits is UNCONFIRMED against a real
recording** (U20); the codec geometry is proven, the emitted container is not. MP3 conversion happens on the phone via
`liblame`. Full detail, including the several non-conformant quirks in the SDK's own Ogg
writer, is in `docs/protocol-ledger.md` §8.

---

## 6. OPEN QUESTIONS

Q1–Q3 and Q5 are **closed**; see `docs/protocol-ledger.md` for the answers and
`docs/reconstruction-log.md` for how they were settled.

| # | Question | Status |
|---|---|---|
| Q1 | Fixed runtime properties of `2BB0`/`2BB1` | **Still open.** Roles are DIRECT (`w$d`); the exact discovered property bitmask needs hardware or a trace. Flagged as emulator POLICY. |
| Q2 | CRC coverage and wire byte order | **Closed.** Little-endian is DIRECT (`TntBleCommUtils.a(long)` is pure Java). The transfer-tail CRC is **never verified anywhere in the SDK**; the only CRC primitive in use is `tntGetFileCrc` (CRC-16/CCITT-FALSE, init 0xFFFF) on the OTA path. |
| Q3 | Exact RSA payloads and token roles | **Closed structurally.** Layouts are DIRECT. The values are cloud-issued (`/sdk/gen-key`, `/sdk/sn-sign`) and cannot be produced offline. |
| Q4 | Is the payload protobuf? | **Closed: no.** Every message is a hand-rolled fixed-offset binary layout built through `TntBleCommUtils`. There is no `GeneratedMessageLite`, no `dynamicMethod`, and no descriptor string anywhere in `com/plaud/sdk/proto/**`. |
| Q5 | Do `w$a`/`w$c` map 1:1 to request/response? | **Closed: no, and the direction is the reverse of the obvious guess.** See §5.6. |

The open questions that matter now live in `docs/protocol-ledger.md` §9 as a
numbered register (U1–U18), each with what would settle it.

## 7. DECISION LOG

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-21 | Android AAR is the primary protocol source, not iOS Mach-O | jadx on a jar beats Ghidra on a 22 MB Mach-O; constants survive ProGuard |
| 2026-09-21 | iOS `.swiftinterface` is the primary *naming* source | Unobfuscated, textual, shipped in the framework |
| 2026-09-21 | Do not vendor SDK binaries; fetch at analysis time | They are proprietary under a separate licence |
| 2026-09-21 | Repo licence: AGPL-3.0 | Keeps the option of deriving from openplaud/riffado open at zero cost |
| 2026-09-21 | Bumble over bleak/bluez for the emulator | Virtual-link transport ⇒ full BLE tests in CI with no radio |
| 2026-09-21 | cpWER (meeteval) as the headline ASR metric, not WER | Plain WER lies when speakers are swapped; using cpWER signals domain knowledge |
| 2026-09-22 | `javap` bytecode is ground truth; jadx is orientation only | jadx silently misrenders overload resolution, signedness and control flow. Ledger §11 lists the specific lies. |
| 2026-09-22 | `reference/**` is immutable; every derived artifact goes to `build/` | The old `fetch-sdk.sh` wrote a second, unpinned SDK copy into `reference/`. It is now a deprecation shim. |
| 2026-09-22 | Protocol facts are extracted mechanically into a committed digest, and the tests assert against *that* | Fixture-based tests are circular by construction. Three real bugs shipped green under the old regime. |
| 2026-09-22 | The emulator declares portVersion 7 and refuses to construct at ≥ 20 | Above 20 the SDK encrypts every frame. A cleartext peripheral claiming a modern portVersion would be lying about what it speaks. |
| 2026-09-22 | The emulator accepts any handshake token, labelled HARNESS POLICY | On the legacy path the device decides. Being a device to their client is a different problem from being a client to their device, and only the latter needs credentials. |

---

## 8. STATUS

**Phase 4 — review, fixes and first measurements, 2026-09-25.** Full account:
[`docs/progress-report.md`](docs/progress-report.md) §8.
Commit `70249ba` (the work to 2026-09-24) was pushed to
`github.com/ORION2809/PLAUDE_EMULATOR_REV-ENG` (public); its `tests` workflow failed
(the install line lacked the dependencies, and the two Ogg fixtures were git-ignored) and
its `evals` workflow passed. The fix is uncommitted and has passed only in local
clean-clone simulations. An independent review of the phase-3 layers found **71 findings**
(23 major, 48 minor; 69 confirmed, 2 plausible, none refuted); each was fixed or narrowed,
among them the mock cloud's pipeline-oracle hook (MC-1). The same pass closed the 24 Sep
known gap of the opcode-10 → Wi-Fi hand-over (with a `wifi_device_factory`, opcode 10 now starts the Wi-Fi device over
loopback; a full session has run only against our phone double, never with the real SDK). **V6 ran under Docker**: live in 6.58 s, job exit 0.
**First real-model numbers** (`docs/v5-results.md`): `whisper-sherpa` on 4 of the 16 AMI
test meetings, hinted macro DER 0.6557 / cpWER 0.8533 — poor, and not V5. **R7-S14**
(`r7/r7-s14-wifi-real-sdk.md`): the real SDK's Wi-Fi transfer stopped at the SoftAP join;
no Wi-Fi PDU was exchanged; one emulator bug (the opcode-10 mode byte read as on/off) was
fixed. **Correction — cloud contact:** earlier statements that no Plaud endpoint was
called were wrong for the Android runtime runs. The SDK, initialised by our debug drivers
with a synthetic token, POSTed `partner/sdk/gen-key` to `platform-jp.plaud.ai` once per
app start; the request is visible in 22 archived R7-S13/R7-S14 logs, and every visible
response (19) was 401. The drivers now pass a blank token; an offline re-run pulled the
file byte-exact with no `gen-key` line. Suite: `1365 passed, 4 skipped, 11 warnings in 339.00s (0:05:38)`. Rung status: V1 ✅ V2 ✅ V3 ✅
(BLE transfer path) V4 ✅ V5 ◻ (subset measured, target unmeasured) V6 ✅ (one Docker run,
arm64).

**Phase 3 — the remaining layers, completed 2026-09-24.** Everything PROJECT.md
listed as "not started" or partial now exists and is tested: the V2 fault matrix,
the Wi-Fi bulk-transfer emulator (ledger §7, now implemented: `emulator/plaudsim/wifi*.py`,
`docs/wifi-transport.md`), Layer 2 (`generator/`, ground truth by construction,
device-shaped Ogg/Opus, `docs/generator.md`), Layer 3 (`evals/`, `docs/evals.md`),
the stack under test (`pipeline/`, `docs/pipeline.md`), Layer 4 (`mockcloud/`, a
FastAPI mock of the partner cloud contract, `docs/mockcloud.md`), cross-layer
integration tests (`docs/integration.md`) and the compose topology (`docs/compose.md`).
**V3 was completed** by driving the real AAR to pull a recording (R7-S13,
`r7/r7-s13-recording-pull.md`), which falsified the frozen HEAD·DATA·TAIL sequence:
the client completes only on an EMPTY_PACKAGE sentinel before the TAIL (ledger §5.8,
§15; emulator corrected; U15/U17 updated). Suite as of 2026-09-24: **978 tests, all passing**
(`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ --timeout=120`), 33/33
reference pins, `reference/**` unmodified. Rung status as of 2026-09-24: V1 ✅ V2 ✅ V3 ✅
(transfer path) V4 ✅ V5 ◻ (procedure only, no AMI/model here) V6 ◻ (written, rehearsed
on the venv, Docker absent).

**Phase 2 — product reconstruction (R1), completed 2026-09-23.** The BLE layer
was frozen and the rest of the product was reconstructed from the corpus:
mobile app, cloud (five surfaces), recording lifecycle, AI pipeline, memory,
search, subscription, device lifecycle, firmware-as-observable, web app, and
the fork lineage of every Plaud-org repository. Verdict: **PRODUCT
RECONSTRUCTION COMPLETE WITH EXTERNAL-EVIDENCE BLOCKERS**. Entry point:
[`docs/final-product-reconstruction.md`](docs/final-product-reconstruction.md);
claims in [`docs/product-ledger.md`](docs/product-ledger.md); one file per
layer under [`docs/architecture/`](docs/architecture/); sources in
[`docs/source-map.md`](docs/source-map.md); machine-readable graph in
[`docs/evidence-graph.json`](docs/evidence-graph.json) (397 claims, 2 072
edges — corrected 2026-09-25, this file said 2 004 — built by `scripts/build_evidence_graph.py` from
`docs/product-evidence/`). Nothing below this line was re-derived.

**Phase 1 — protocol reconstruction. Re-audited from first principles 2026-09-22.**

The blow-by-blow is in [`docs/reconstruction-log.md`](docs/reconstruction-log.md);
this is the standing summary.

### Where the evidence lives

* `reference/` — 33 pristine repositories, fetched by `./scripts/fetch-references.sh`,
  pinned in `docs/reference-pins.txt`, manifest in `docs/SOURCES.md`. **Immutable.**
  Verified clean at the start and end of the audit.
* `build/evidence/` — everything derived, rebuilt by `./scripts/build-evidence.sh`,
  which verifies the AAR's sha256 before it starts. Contains a `javap -p -c` dump
  of all 674 classes (131 922 lines) alongside the jadx output. **javap is ground
  truth; jadx is an interpretation and is wrong in documented ways.**
* `docs/evidence-digest.json` — protocol facts extracted *mechanically* from that
  bytecode. Committed, pinned to the AAR hash, regenerated and diffed by the tests.

### What the emulator can do

Over a Bumble virtual link, with no radio: advertise with parseable
manufacturer data → be discovered by scan → connect → MTU → discover → subscribe
(notify or indicate) → `getState` / `syncTime` / `getStorage` / `battStatus`,
unsolicited battery pushes, paged file list, `resumeRecord`, `deleteFile`,
`syncFile` with HEAD/DATA/TAIL, resume-from-offset, injectable DATA gap, and the
`stopSync` → restart recovery the SDK performs on its own.

**301 tests, all passing**, of which 69 (across 6 files) assert the emulator and
the reconstruction against bytecode-derived facts rather than against our own
fixtures. The official SDK's own k3 was additionally captured at runtime and
matches byte-for-byte (R7-S12).

### What it cannot do, and why

Bind **our own client to real Plaud hardware**: the RSA key pair, the SN
signature and the handshake token are issued by Plaud's cloud, none can be
produced offline, and no credentials live in this repository. Above
`portVersion 20` the SDK seals every frame with ChaCha20-Poly1305 on top of
that, so `PlaudPeripheral` refuses to construct with such a value rather
than serve cleartext under a modern banner.

What it **can** do is be a device to their client: on the legacy
(portVersion < 20) path the handshake carries no cryptography — the host
sends a token and the device returns a status — so the emulator reaches
`BOUND` by accepting the token (HARNESS POLICY, labelled as such; the
refusal path is tested too). That asymmetry is the point: we can be a
device to their client, not a client to their device.

### Next, in order

(Refreshed 2026-09-25. The 2026-09-24 items "V6 under Docker", "Wi-Fi ↔ BLE handoff" and
"mock-cloud round trip in the compose job" are done.)

1. **Commit, then read the first GitHub run** of the fixed `tests` workflow (Linux x86_64
   has never run it; the diarizer's speaker-count tests are the likeliest to differ).
2. **V5 for real.** Fetch the other 12 AMI test-split headset-mix WAVs (CC BY 4.0) and
   convert them (`python -m evals.ami`). Before scoring the test split: calibrate
   sherpa's `cluster_threshold` on the AMI dev split (never on test meetings), make
   `evals` report DER/JER when meeteval refuses > 20 speakers, and seed or pin the ASR
   decode (EN2002a was not reproducible). Then `scripts/run-v5.sh` and suite `ami-headset`.
3. **Wi-Fi with the real SDK**: a hotspot the AVD can join, or a physical phone; raise
   the pen's dial attempts for that run (the phone's server starts only after the join).
4. **U1 (with U18 merged into it), U15 and U17 need a device**: one scan capture settles the advertising branch;
   one transfer capture settles the EMPTY_PACKAGE code value (U15) and whether a TAIL follows it (U17).
5. **iOS** needs Xcode (not on this machine) and a Bluetooth bridge for the simulator.
6. **Piper voice licence**: decide before any Piper-made audio is redistributed (the
   lessac base voice's dataset licence is research-only; `docs/generator.md` §6.2).

## 9. Repo layout

```
plaud-harness/
├── PROJECT.md              ← you are here; update every session
├── docs/
│   ├── protocol-ledger.md  ← THE authoritative protocol record
│   ├── reconstruction-log.md ← what was learned, and what was wrong
│   ├── evidence-digest.json  ← protocol facts extracted mechanically from bytecode
│   ├── protocol-spec.md    ← superseded in part by the ledger
│   ├── protocol.json       ← machine-readable class census (generated)
│   ├── fixtures/           ← per-message protocol records with provenance
│   ├── SOURCES.md          ← reference corpus manifest
│   └── reference-pins.txt  ← exact commits fetched
├── reference/              ← IMMUTABLE evidence (gitignored, fetched)
├── build/evidence/         ← derived: javap, jadx, unpacked AAR (gitignored)
├── docs/
│   ├── protocol-ledger.md      ← Phase 1: the BLE/device protocol (frozen)
│   ├── product-ledger.md       ← Phase 2: the product claim table
│   ├── architecture/           ← one file per layer (hardware … web)
│   ├── source-map.md, evidence-graph.json, product-evidence/
│   └── final-product-reconstruction.md
├── emulator/plaudsim/
│   ├── advertising.py      ← scan-record parse rules + builder
│   ├── handshake.py        ← marker framing, secret package, ChaCha envelope, l3/x2
│   ├── filesync.py         ← pure codecs for the recording/file-sync family
│   ├── transfer.py         ← device-side transfer + file-list state
│   └── profile.py          ← the Bumble GATT peripheral
├── emulator/plaudsim/wifi.py, wifi_device.py  ← Wi-Fi bulk transfer (PDU codecs + device-side WebSocket client)
├── emulator/plaudsim/faults.py                ← fault-injecting peripheral for the V2 matrix
├── emulator/serve.py       ← standalone peripheral on a Bumble TCP transport (compose / local-up)
├── generator/              ← Layer 2: synthetic meetings, ground truth by construction, device-shaped audio
├── evals/                  ← Layer 3: DER/JER/WER/cpWER/tcpWER + gates + CLI
├── pipeline/               ← the ASR/diarization stack under test (oracle, perturbed-oracle, energy-vad-cluster, guarded adapters)
├── mockcloud/              ← Layer 4: FastAPI mock of the partner cloud contract (identity, binding, upload, transcription jobs)
├── docker/, docker-compose.yml, scripts/local-up.sh, scripts/compose-smoke.sh  ← V6 topology
├── requirements/           ← per-track pins + all.txt
├── tests/
├── scripts/
│   ├── fetch-references.sh ← clone the corpus into reference/
│   ├── build-evidence.sh   ← decompile into build/ (verifies the AAR sha256)
│   ├── extract_evidence_digest.py ← bytecode → docs/evidence-digest.json
│   ├── build_evidence_graph.py    ← docs/product-evidence/*.json → docs/evidence-graph.json
│   └── extract_protocol.py ← bytecode → docs/protocol.json (class census)
└── .github/workflows/
```

## 10. How to resume

```bash
cd plaud-harness                 # your clone
./scripts/fetch-references.sh    # if reference/ is missing (~2.5 GB; each repository at its pin)
./scripts/build-evidence.sh      # rebuild build/evidence/ (~2 min)

python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble
.venv/bin/pip install -r requirements/all.txt      # all layers: emulator, generator, evals, pipeline, mock cloud
.venv/bin/pip install -r requirements/models.txt   # optional: whisper-sherpa and Piper (piper-tts is GPL-3.0-or-later)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=120
```

Then read, in this order:

1. [`docs/project-state.md`](docs/project-state.md) — the current state in one document: what works, what does not, the numbers, how each was proven
2. [`docs/progress-report.md`](docs/progress-report.md) — the dated account: what was done, what was wrong, how it was checked
3. [`docs/protocol-ledger.md`](docs/protocol-ledger.md) — what we believe and why
4. [`docs/reconstruction-log.md`](docs/reconstruction-log.md) — what earlier work got wrong
5. [`docs/final-product-reconstruction.md`](docs/final-product-reconstruction.md) — the whole product above the radio
6. [`r7/r7-s13-recording-pull.md`](r7/r7-s13-recording-pull.md) — the real SDK pulling a recording, and what it corrected; [`r7/r7-s14-wifi-real-sdk.md`](r7/r7-s14-wifi-real-sdk.md) — its Wi-Fi transfer, blocked at the join
7. The layer docs: `docs/v2-fault-matrix.md`, `docs/wifi-transport.md`, `docs/generator.md`, `docs/evals.md`, `docs/pipeline.md`, `docs/mockcloud.md`, `docs/integration.md`, `docs/compose.md`, and `docs/v5-results.md`
8. §8 above — what is next

**Two rules that are not negotiable.** `reference/**` is immutable evidence; every
derived artifact goes to `build/`. And when jadx and javap disagree, javap wins —
`docs/protocol-ledger.md` §11 lists the specific places jadx lies.
