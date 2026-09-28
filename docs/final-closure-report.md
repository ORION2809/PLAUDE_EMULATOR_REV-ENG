# Final closure report — Plaud / TinnoTech BLE protocol reconstruction

Date: 2026-09-23 · SDK `plaud-sdk.aar` sha256 `041a6f8814d350dbc8bcd4a515487136529bb8bb4368fc88c9b36c9a386aedce`
Companion documents: `docs/final-architecture.md`, `docs/final-uncertainty-matrix.json`,
`docs/protocol-ledger.md` (the authoritative field-by-field record), `r7/r7-s12-k3-runtime-capture.md`,
`r7/cloud-endpoint-inventory.md`, `docs/reconstruction-log.md`.

Evidence classes are kept distinct throughout and never collapsed: **SDK_PROVEN**
(shipped template source / swiftinterface), **BYTECODE_PROVEN** (`javap` on the AAR),
**RUNTIME_PROVEN** (the unmodified SDK binary executed), **EMULATOR_INTEGRATION_PROVEN**
(SDK and emulator completed the exchange), **CLOUD_OBSERVED** (strings/docs/community, never
called), **INFERRED**, **HARNESS_POLICY**, **UNKNOWN**. **DEVICE_OBSERVED appears nowhere: no
Plaud hardware was ever used.**

## Executive conclusion

The Plaud recorder's BLE/GATT protocol, its legacy and modern handshakes, its
sealed transport, its file-transfer and file-list families, its audio processing
chain, and its cloud surface are reconstructed from Plaud's own published
artifacts. A software emulator (`emulator/plaudsim`) implements the device side
of every proven exchange. The reconstruction's central claim was upgraded this
session from a state-machine rehearsal to **runtime proof**: the unmodified
official SDK, running on an Android emulator and attached to our emulator over a
virtual Bluetooth link, built its first handshake frame from a synthetic
identifier, wrote it to the device, had it accepted, and reported a successful
bind — and the bytes it wrote match the reconstruction byte-for-byte.

What remains unresolved is exactly the set of facts that live only in real
hardware, a real recording, or a real Plaud account. Each is enumerated with the
experiment that would settle it and why that experiment is not legitimately
available here. No such fact is emulated as if it were known.

Final verdict: **RECONSTRUCTION COMPLETE WITH EXTERNAL-EVIDENCE BLOCKERS.**

## Proven protocol (BLE / GATT)

- **GATT profile** — service `1910`; `2BB0` notify/indicate (device→host); `2BB1`
  write (host→device); CCCD `2902`; battery `180F`/`2A19`. MTU 255 requested,
  negotiated to 517 on the AVD. BYTECODE_PROVEN; discovery and MTU RUNTIME_PROVEN.
- **Framing** — type-1 control frame `[u8 1][u16le opcode][payload]`; type-2/3/5
  marker frames `[u8 type][u32le 0xffffffff][payload]`; little-endian throughout;
  CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) for the OTA whole-file path.
  BYTECODE_PROVEN.
- **Opcodes** — request table `w$c`, response table `w$a` (the direction is the
  reverse of the alphabetical guess, settled by unique-opcode class mapping).
  Recovered: 1 handshake, 2 getSsn, 3 getState, 4 syncTime, 6 getStorage,
  **8 CommonSettings (new this session)**, 9 battStatus, 22 resumeRecord,
  26 fileList, 28 syncFile, 29 stopSync/TAIL, 30 deleteFile/stopAck, 101 rateTest,
  **138 capability exchange**; markers FE10/FE11/FE12/FE20. Request and response
  opcodes are not always equal. BYTECODE_PROVEN; opcodes 1/3/4/8/9 RUNTIME_PROVEN.
- **No application-layer reassembly**; one logical frame per write. SOURCE-DERIVED.

## Handshake

- **Legacy (advertised portVersion < 20).** `q.b0` goes straight to
  `first_handshake` (`q.a`), whose only precondition is a non-empty token. k3 is
  `[01][01 00][02][z.h()=0][stage iff pv≥3][token '0'-padded/truncated to 32 (pv≥9)
  else 16]`; `u4.b()` trims trailing 0x00 only. The device answers l3 (10 fields:
  status, portVersion, timezone, timezoneMin, audioChannel, supportWifi, noNsAgc,
  isOggAudio, versionType, version). status 0 → the client caches
  portVersion/timezone and proceeds to syncTime, where bleBind fires.
  **BYTECODE_PROVEN, and now RUNTIME_PROVEN end-to-end** (§ Runtime).
- **Modern (portVersion ≥ 20).** Before k3: the host chunks the cloud-issued
  snSignature into FE10/FE20 marker frames, receives FE11 (STICK_PREHANDSHAKE_CNF,
  marker 65041; payload bytes UNKNOWN), exchanges the FE12 secret package
  (RSA/ECB/PKCS1Padding, PKCS#8 private key from the cloud), and derives
  J=plaintext[0:32], K=[32:44], L=[44:56] with the remainder self-checking to
  `PLAUD.AI`. Then k3, then j3/SSN, then l3, syncTime, bleBind. Layouts
  BYTECODE_PROVEN; the values are cloud-issued (CRED-1) and the FE11 payload and
  FE20 device effect are UNKNOWN (U10, MODERN-1).

## Crypto

- **BLE sealed transport (pv ≥ 20): fixed ChaCha20-Poly1305.** key = J, nonce = K,
  AAD = L; the sequence number is authenticated inside the AEAD plaintext (proven
  by stack accounting, not adjacency); first sealed sequence is 2; one logical
  frame is sealed once and retries reuse the exact sealed bytes; duplicate/stale
  receive frames are dropped; there is no transport ACK — application responses
  serve as protocol-level responses. Counters: `w7` reads counters through the BLE
  transport; model prefix `881` (Note Pro) has private counters, other models share.
  BYTECODE_PROVEN; EMULATOR_INTEGRATION_PROVEN with synthetic J/K/L (R5-S7/S8/S9).
- **AES-GCM exists but is Wi-Fi-only** — selected by bit 3 of the opcode-138
  capability bitmap via `z.O`/`z.w()`, whose every call site is on the Wi-Fi path.
  The BLE seal helpers `q5.d`/`q5.b` take no algorithm parameter. Corrected this
  session (was recorded as a generic BLE alternate). BYTECODE_PROVEN.

## Transfer

- **syncFile** (opcode 28) `y6` = `[01][1C 00][u32le session][u32le start][u32le end]`;
  HEAD `s6` = `[…session][u8 status]` (status>0 = failure); DATA is **protocol type 2**
  (`[02][u32le session (pv≥7)][u32le offset][u8 length][payload]`); TAIL `t6` =
  `[…session][u16le crc]` (**crc never verified** — U13).
- **Loss handling is real client behaviour, BYTECODE_PROVEN** (previously held
  open): on a forward offset gap the client re-issues `syncFile` from its cursor
  (runnable `fileSyncLossPkgStop`); it has gap *detection* plus a whole-stream
  *restart* only — no selective/NAK retransmit and no per-frame ACK. The `stopSync`
  wire write (`q.k`) is reached only via a 5 000 ms stall timeout, not synchronously
  on the gap (§5.9 wording corrected). Consequence: the device must deliver DATA in
  monotonic offset order from the requested start. The emulator device performs no
  resend and no ACK; its `frames(drop_offsets=…)` is an explicitly-labelled test hook.
- **EMPTY_PACKAGE** sentinel offset `0xFFFFFFFF`, code byte at `i+5` (skipping the
  length byte); `code==1` suppresses the restart. **stopSync** request opcode 29 →
  response opcode 30 (no fields); **deleteFile** request 30 → response 31.
  BYTECODE_PROVEN. U14–U17 (end inclusivity, EOF semantics, status codes,
  TAIL-vs-EMPTY ordering) require a real capture.

## Audio

Chain: `syncFile → t7.d() → o4 (pure pass-through, so concatenated DATA payloads
equal the stored file byte-for-byte) → AudioExporter`. AudioExporter runs exactly
two tests in order: (1) `isE2eeEncrypted` = file ≥ 512 B AND `PlaudEncryptHeader`
magic `PLAUD.AI` → skip 512 B and ChaCha-decrypt; (2) read 4 bytes, compare to
`OggS` → Ogg branch, else raw/alternate branch. **Selection is sniff-based**: the
device-profile bit `isOggAudio` is written (`setOggAudio`) but its getter has zero
call sites, so it is never consulted (pinned by a falsification test). Codec
geometry the SDK enforces: 16 kHz, 20 ms/320-sample frames, 80 bytes/frame/channel
(= 32 kbps/channel CBR); g4 Ogg page geometry re-derived. BYTECODE_PROVEN.

Distinct artifacts, not to be conflated: the SDK's native writer tags OpusTags
vendor `TinnoTech123456789012` (`libjni_ogg.so`), while real cloud downloads carry
`PALUD.AI` (riffado regression #160) and are served as `.mp3` despite Ogg/Opus
bytes. Cloud artifacts therefore must not be used as fixtures for the local writer
or the BLE path. **Which of the four closed shapes real firmware emits (U20) and
what the cloud transcode does (U19) are the only audio residuals, both external.**

## Runtime (official SDK executed)

Toolchain: JDK 17, Android SDK, AVD `mivi_test_34` (API 34), gradle 8.2, Bumble
`android-netsim` to the emulator's `netsimd`. Driver: one debug Activity added to
our copy of the template app, using only public SDK API with a synthetic identifier.

`recoveryConnectBleDevice(device, historicalUserId)` derives the k3 token by a pure
local string transform of `historicalUserId` (`removePrefix "client_user_"`, drop
`-`) and, on the pv < 20 path, makes no cloud call. Four runs:
(Correction, 25 Sep 2026: the recovery path makes no cloud call, but the driver's
`initSDK` passed a synthetic token. With a non-blank token the SDK sends one
automatic `partner/sdk/gen-key` request per app start. That request appears in the
later R7-S13/R7-S14 logs, where every response shown was 401. The four runs here
very likely sent it too, but their logs were filtered and show neither request nor
response. See `r7/r7-s14-wifi-real-sdk.md` D7.)

| run | id | outcome |
|---|---|---|
| 1 | `SYNTH-HIST-0001` | k3 = `01 01 00 02 00 00` + `SYNTHHIST0001` + `000` (22 B), **byte-exact** vs the model; old_protocol_ok → syncTime → getState → CommonSettings ×2 → bind status 0 |
| 2 | `client_user_` (→ empty) | **nothing written**; SDK logs abort + `bleConnectState(2)` — falsifier: the empty-token guard, not a cloud guard, is the sole gate |
| 3 | 32-char id | token **truncated** to 16 chars, byte-exact |
| 4 | run 1 + emulator `l3_timezone=5`, opcode 8 answered | `bleBind protVersion=5 tz=5` (facade-quirk falsifier), 0 rejects |

This upgrades the k3 construction to SDK_PROVEN + BYTECODE_PROVEN + RUNTIME_PROVEN +
EMULATOR_INTEGRATION_PROVEN. It also produced the discovery of opcode 8, a live
round-trip of all ten l3 fields, and proof of the `bleBind` facade quirk (both
`protVersion` and `timezone` are the l3 timezone byte). Full write-up:
`r7/r7-s12-k3-runtime-capture.md`.

## Cloud

50 endpoints across five surfaces inventoried statically in
`r7/cloud-endpoint-inventory.md`: the SDK-internal `/api/...` family (Retrofit,
3-hop Bearer chain, default host `platform-jp.plaud.ai`); the partner
`/developer/api/open/partner/...` family (gen-key returns the RSA private key,
sn-sign returns the signature, both `Bearer userAccessToken`); the consumer web API
`api.plaud.ai` (community clients, user-token JWT); the developer platform
`platform.plaud.ai`; and a legacy TntAgent `/recorder/device/...` path (query-signature
auth, host UNKNOWN — U21). **None was called.** Every endpoint is CLOUD_OBSERVED or
weaker and requires a real credential; a URL string is not authorization. The
cloud↔SDK↔BLE correlation is stated with its one firm negative: BLE bytes equal
the SDK-local file, but cloud recordings/exports do **not** automatically equal the
local writer's output (the vendor-string divergence proves re-tagging/transcoding).

## Emulator (what is implemented)

`emulator/plaudsim`: advertising parse/build; the Bumble GATT peripheral with the
full lifecycle to BOUND; handshake parse + the new `build_k3` oracle + l3/x2 encode
+ marker framing + FE12 reassembly + J/K/L split; file-list paging (pv-dependent
stride), HEAD/DATA/TAIL/EMPTY/resume/delete; the ChaCha20-Poly1305 sealed link with
independent counters and replay/stale/tamper handling (synthetic keys, test-only
peripheral for pv ≥ 20); the Java-layer audio contracts; **opcode 8 CommonSettings**
and **opcode 138 capability exchange**. It refuses to advertise portVersion ≥ 20
(it would have to lie about sealing) and mints no real tokens/keys/signatures. Every
device "decision" is labelled HARNESS_POLICY in the ledger's policy register.

## Test evidence

| suite | result |
|---|---|
| `pytest tests/` | **301 passed** (from 265 at session start) |
| bytecode-asserting tests | 69 functions across 6 files read `build/evidence/javap` and assert the emulator/model against the shipped bytecode |
| protocol verifiers (`r5-s2…r6-s2`) | 12 / 12 `ALL CHECKS PASSED` |
| `docs/evidence-digest.json` | regenerates **identically** from bytecode; pinned to the AAR sha256 |
| runtime captures | 4 runs, byte-exact k3, all evidence files retained under `r7/` |

New this session: `tests/test_r7_s12_k3_runtime.py` (golden replay of the genuine
captures + the pure model), `tests/test_r7_s12_common_settings.py` (opcode 8, incl.
javap-pinned wire values), `tests/test_r7_s12_audio_selection_pin.py` (isOggAudio
zero-consumer falsification). `tests/conftest.py` disables bytecode writing so the
editable bumble import never touches `reference/**`.

## Remaining unknowns

Twenty-four items in `docs/final-uncertainty-matrix.json`, each with its resolving
experiment. They cluster as: **DEVICE_REQUIRED** (U1, U5, U6, U7, U9, U10, U12, U14,
U15, U16, U17, U22, MODERN-1/2 — advertising branch, device-side acceptance/ordering/
timing/values), **EXTERNAL_DATA_REQUIRED** (U20 — which recording shape real firmware
emits), **CREDENTIAL_REQUIRED** (U19 cloud transcode, CRED-1 real token/signature/keys),
and **UNKNOWN-from-static** (U3, U4, U8, U11, U13, U21 — fields the shipped SDK parses
but never consumes, and the legacy host).

## Why each remaining unknown cannot currently be resolved

Every one requires an input that does not exist in this repository and cannot be
produced legitimately offline: a passive scan or connection capture from a real
Plaud (or other Tinno-based) device; one authentic recording or cloud download from
an account the operator owns; or a real Plaud partner credential. The whole-worktree
sweep (R6-S3) confirmed zero authentic Plaud audio is present, and the credential
provenance trace (R4-S4) confirmed no test-credential mechanism exists. Obtaining any
of these is a matter of access, not of further code archaeology — the static evidence
is exhausted for these items, which is why they are closed as external rather than
left open for another investigation phase.

## Security / auth boundary (deliberately not attempted)

No authentication was bypassed, no credential extracted or forged, no real
customer/device impersonated, no Plaud server exploited or brute-forced, no cloud
endpoint called, and no "real device"/"real cloud" evidence fabricated. The SDK
binary was never modified to remove a check. The runtime experiment used a synthetic
identifier against our own emulator on the path that genuinely needs no credential;
it proves what the SDK constructs, not that any real device would accept it. Binding
our own client to real hardware stays structurally blocked because the token, the SN
signature and the RSA key pair are cloud-issued — the one line the project does not
cross.

## Reproducibility

```bash
cd plaud-harness                      # your clone
./scripts/fetch-references.sh        # 33 repos, commit-pinned (docs/reference-pins.txt)
./scripts/build-evidence.sh          # verifies AAR sha256, then javap + jadx into build/evidence/
python3 scripts/extract_evidence_digest.py   # regenerates docs/evidence-digest.json identically
python3.11 -m venv .venv
.venv/bin/pip install -e reference/upstream/bumble pytest pytest-asyncio grpcio protobuf
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/            # 301 passed
for v in r5-s*/verify_*.py r6-s*/verify_*.py; do (cd $(dirname $v) && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python $(basename $v)); done   # 12 ALL CHECKS PASSED
```

Runtime experiment (needs the local Android toolchain; see
`r7/r7-s12-k3-runtime-capture.md` for the exact recipe and the pitfalls met):
build `r7/android-app` with the synthetic `local.properties`, boot the AVD, start
`r7/k3_capture_peripheral.py` on `android-netsim`, then
`am start …/.debug.K3CaptureActivity --es id "SYNTH-HIST-0001"`.

Environment used: macOS (darwin), Python 3.11.16, JDK 17.0.20 (Temurin), Android SDK
with AVD `mivi_test_34` (API 34, arm64), gradle 8.2, Bumble `0.1.dev1+gd371a27eb`.

## Reference integrity

33 / 33 repositories at their pinned commits; AAR sha256 matches the pin; no tracked
evidence file modified (verified by `git status --porcelain` inside each repo). The
Gradle artifacts an earlier session left under
`reference/plaud-org/plaud-sdk-public/android/` (ignored by that repo's own
`.gitignore`, so never part of the evidence) are removed at closure, and
`sys.dont_write_bytecode` keeps the editable bumble import from writing `__pycache__`
into `reference/**`. Result: reference integrity PASS, evidence corruption NO, derived-
artifact containment PASS.

## Final verdict

**RECONSTRUCTION COMPLETE WITH EXTERNAL-EVIDENCE BLOCKERS.** No known evidence
contradiction remains; the residual unknowns are exactly those that require real
hardware, a real recording, or a real Plaud account, each documented with its
resolving experiment and closed as externally blocked rather than carried as open
research.

---

**Addendum 2026-09-24 (R7-S13).** Driving the unmodified SDK to *pull* a
recording (not only bind) showed that §5.8's HEAD·DATA·TAIL sequence was
incomplete: the client completes only on an EMPTY_PACKAGE sentinel before the
TAIL, sends stopSync synchronously on a gap, and races an over-fast device's
responses in its op-queue. The emulator was corrected and re-verified against
the SDK (byte-exact on both public paths, gap recovery converging). Totals
above (301 tests) are the Phase-1 figures; the tree now carries 978 tests
across all layers. See `r7/r7-s13-recording-pull.md`, ledger §15, and
PROJECT.md §8.

**Note, 25 September 2026.** The test counts in the body of this report are
Phase-1 figures from 23 September: "**301 passed**" under "Test evidence" and
"# 301 passed" under "Reproducibility". The addendum's "978 tests" is the
24 September figure. On 25 September the full suite gave `1365 passed, 4 skipped, 11 warnings in 339.00s (0:05:38)`. The
install line under "Reproducibility" (Bumble, pytest, pytest-asyncio, grpcio,
protobuf) is also the Phase-1 set; the current suite needs
`requirements/all.txt` (README, "Quick start"). The statement under
"Security / auth boundary" that no cloud endpoint was called is subject to the
25 September correction under "Runtime": the SDK, initialised by our driver
with a synthetic token, sent automatic `gen-key` requests, and every visible
response was 401 (`r7/r7-s14-wifi-real-sdk.md` D7; `docs/progress-report.md`
§8.6).
