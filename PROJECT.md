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
| Vendoring the binaries into this repo | **No.** `scripts/fetch-sdk.sh` pulls them at analysis time. They are gitignored. |
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

## 4. Validation ladder — the README's checkmarks

Each rung is a test that either passes or fails. Nothing subjective.

- [ ] **R1** Emulator advertises; a Bumble central connects and completes a full session
- [ ] **R2** Emulator survives the fault-injection suite without corrupting a transfer
- [ ] **R3** **Real `plaud-sdk-public` connects to the emulator and pulls a recording** ← the money shot
- [ ] **R4** Synthetic generator's ground truth round-trips through `pyannote.metrics` at DER 0
- [ ] **R5** Full pipeline hits target cpWER / DER on held-out AMI
- [ ] **R6** `docker compose up` brings the whole system live in under 60 seconds

R3 needs a real radio at the far end (a $10 USB BLE dongle or the Mac's built-in Bluetooth),
but **not a Plaud device**. That distinction is the project.

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
| `00002BB0-…` | characteristic — **presumed Write** (host → device commands) |
| `00002BB1-…` | characteristic — **presumed Notify** (device → host responses/stream) |
| `00002902-…` | CCCD (standard) |
| `0000180F-…` | Battery Service (standard) |
| `00002A19-…` | Battery Level (standard) |

**Open:** the Write/Notify assignment is inferred from convention, not yet confirmed from
code. Confirm before building the GATT tree. → see Open Questions Q1.

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

`TntBleCommUtils` (JNI, `libtnt_ble_utils.so`) exposes the codec primitives:

```
native int   packInt(int widthBits, byte[] buf, int off, long value)   // 8/16/24/32/64
native long  readInt(int widthBits, byte[] buf, int off)
native short tntGetCrc(byte[] buf, int off, int len)                   // CRC-16
native int   tntGetFileCrc(String path, int len)                       // whole-file CRC
```

A pure-Java helper in the same class serialises little-endian, so **the wire format is
little-endian** and integrity is **CRC-16, variant unknown**. → Q2.

### 5.6 Two opcode tables

`w$a` (~80 entries) and `w$c` (~80 entries) — almost certainly **request** and
**response/notify** opcode spaces. They overlap heavily but not exactly, which is what you
would expect from req/rsp pairs plus device-initiated events. Values run 1–151 with gaps.
Duplicate values under different names (`o=13, p=13`) suggest aliases or sub-codes.

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

`PlaudBleSDK` + `PlaudWiFiSDK` (which vendors `JXWebSocketServer.h`). BLE is the control
plane; **bulk recording transfer goes over Wi-Fi via a WebSocket where the phone is the
server**. The emulator therefore needs both: a BLE peripheral *and* a WebSocket client.

Audio: `JXOpusDecoder`, `OggUtil`, `Mp3Convert`, `liblame.so` ⇒ device records **Opus in Ogg**,
converted to MP3 on the phone. This is the origin of the "Opus bytes in a .mp3 file" quirk —
it is a conversion-path artifact, and our generator must reproduce it.

---

## 6. OPEN QUESTIONS

| # | Question | How to settle it | Blocks |
|---|---|---|---|
| Q1 | Is `2BB0` write and `2BB1` notify, or reversed? | Find the characteristic-property reads in the decompiled `sdk/bluetooth/**`; confirm against the iOS `PlaudBleSDK-Swift.h` | R1 |
| Q2 | Which CRC-16 variant is `tntGetCrc`? | `libtnt_ble_utils.so` is 6.5 KB arm64 ELF — disassemble it, or brute-force all 24 catalogued CRC-16s against any packet in the template-app logs | R2, R3 |
| Q3 | What is the RSA pairing handshake? | `SwiftyRSA` usage in the Swift interface + `sdk/bluetooth/**` auth path | R3 |
| Q4 | Is the payload protobuf? | 240 classes in `com/plaud/sdk/proto/` — check for `dynamicMethod`/`GeneratedMessageLite` signatures; if yes, recover the `.proto` schema from the embedded descriptor strings | R1 |
| Q5 | Do the `w$a` / `w$c` tables map 1:1 to request/response? | Diff the two value sets; cross-reference against Swift method names | R1 |

Q4 is the highest-leverage open question. If the payload is protobuf, the schema is
recoverable in full and the emulator becomes provably correct rather than approximately
correct.

---

## 7. DECISION LOG

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-21 | Android AAR is the primary protocol source, not iOS Mach-O | jadx on a jar beats Ghidra on a 22 MB Mach-O; constants survive ProGuard |
| 2026-09-21 | iOS `.swiftinterface` is the primary *naming* source | Unobfuscated, textual, shipped in the framework |
| 2026-09-21 | Do not vendor SDK binaries; fetch at analysis time | They are proprietary under a separate licence |
| 2026-09-21 | Repo licence: AGPL-3.0 | Keeps the option of deriving from openplaud/riffado open at zero cost |
| 2026-09-21 | Bumble over bleak/bluez for the emulator | Virtual-link transport ⇒ full BLE tests in CI with no radio |
| 2026-09-21 | cpWER (meeteval) as the headline ASR metric, not WER | Plain WER lies when speakers are swapped; using cpWER signals domain knowledge |

---

## 8. STATUS

**Phase: 1 — protocol reconstruction. Day 1.**

Done:
- Project scaffolded at `~/Desktop/plaud-harness`
- SDK fetched and inventoried; source-vs-binary fork resolved (§5.1)
- jadx toolchain up; AAR fully decompiled (493 classes)
- GATT profile recovered (§5.4)
- Opcode tables and framing magics located (§5.5, §5.6)
- Capability surface extracted from the Swift interface (§5.7)

Next, in order:
1. **Q4** — determine whether `com/plaud/sdk/proto/**` is protobuf; if so, recover the schema
2. **Q1** — confirm characteristic properties
3. **Q2** — identify the CRC-16 variant
4. Write `docs/protocol-spec.md` up to a buildable state
5. Start the Bumble emulator skeleton (R1)

In parallel, unblocked by all of the above: the synthetic meeting generator (Layer 2).

## 9. Repo layout

```
plaud-harness/
├── PROJECT.md            ← you are here; update every session
├── docs/
│   └── protocol-spec.md  ← the reconstruction, in our own words
├── reference/            ← fetched SDK + jadx output (gitignored)
├── emulator/             ← Layer 1: Bumble BLE peripheral + WS server
├── generator/            ← Layer 2: synthetic meetings with ground truth
├── evals/                ← Layer 3: metrics + gates
├── pipeline/             ← the ASR/diarization/summarization stack under test
├── tests/
├── scripts/
└── .github/workflows/
```

## 10. How to resume

```bash
cd ~/Desktop/plaud-harness
./scripts/fetch-sdk.sh      # re-fetch + re-decompile the SDK (gitignored, ~5 min)
```

Then read §6 Open Questions and §8 Status. They are the working set.
