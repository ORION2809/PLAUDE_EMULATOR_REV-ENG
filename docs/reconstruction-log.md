# Reconstruction log

Durable record of what was actually learned, when, and what changed as a
result. Not a progress diary — every entry records a technical discovery, a
corrected assumption, or a decision with a reason.

Canonical protocol facts live in [`protocol-ledger.md`](protocol-ledger.md).
This file records *how we got there* and what was wrong on the way.

---

## 2026-09-22 — Full forensic re-audit of R1a–R3

**Objective.** Take ownership of the repository, re-establish ground truth
independently, audit every FROZEN milestone from first principles, repair what
is wrong, and preserve what is genuinely unknown.

### Evidence base re-established from scratch

The prior session's decompiled evidence lived only in `/tmp` and was not
reproducible from the repository. Rebuilt it:

* `scripts/build-evidence.sh` now regenerates everything from
  `reference/**` into `build/` (gitignored), **pinned to the AAR's sha256** so
  a republished SDK fails loudly instead of silently invalidating claims.
* Added a `javap -p -c -constants` dump of all 674 classes (131 922 lines)
  alongside the jadx output. jadx is a decompiler's *interpretation*;
  `javap` is the compiler's own reader. Every claim in this audit was
  confirmed in `javap`.
* Verified `classes.jar` sha256 matches the artifact the previous session
  analysed (`27795d1f…`), so the earlier findings and these are about the
  same bytes.
* Verified all **33 reference repositories are pristine** (`git status
  --porcelain` clean in each). No previous agent modified evidence.

### The circular-validation problem, and the fix

Every pre-existing test compared the emulator against fixtures that had been
written from the same reading of the protocol. Three real bugs were green
under that regime:

* `parse_l3` read `version` **big-endian**; the SDK reads it little-endian.
* `pack_chunk_frame` prepended a fabricated `0x01` protocol-type byte to
  marker frames, which the SDK never emits and never expects.
* `parse_file_list_frame` took the entry offset as a *caller-supplied
  parameter*, and every caller passed 9; the SDK uses 11.

Fix: `scripts/extract_evidence_digest.py` walks the `javap` output and emits
`docs/evidence-digest.json` **mechanically** — per class, the constant returned
by `getBleProtocolType`/`getBleRequestType`/`a()`, every `TntBleCommUtils`
reader call as (width, literal offset), the `bArr.length` guard chain, and the
`toString` format literal. `tests/test_evidence_conformance.py` asserts the
emulator against that file. A layout error now fails even when every fixture
agrees with it.

The extractor needed a small constant-propagation pass: ProGuard reuses local
slots aggressively, so a parse offset is often `iload_2` rather than a literal
(`z2.<init>` stores 8 into slot 2 at bytecode offset 77 and reads it back at
95). A naive "last literal before the call" scan reports `null` for most
offsets.

### Corrected findings

**C1 — Marker frames have no protocol-type byte.** `v4.enPkg`/`w4.enPkg`
override `enPkg` and never call `packHead`. The layout is
`[u16le marker][u8 count][u8 index][chunk]`, with the marker at offset **0**.
Three independent confirmations: the two `enPkg` bodies; `z$c`'s
`b(value2, 0)`; `q.n`'s `b(bArr2, 0) == 65041`. The emulator's five-byte
header was wrong in both directions.

**C2 — `l3.version` is little-endian u24.** Bytecode:
`b[len-3] | (b[len-2]<<8) | (b[len-1]<<16)`.

**C3 — `x2.versionName` comes from fixed offsets 59 and 60**, not from the
bytes after the SSN's NUL: one character at 59 plus `"%04d" % u24le@60`, e.g.
`"V0123"`. Cross-confirmed by `BleDevice.getVersionName()`, which builds the
identical string from *advertising* data — which is what makes the SDK's
`checkSn` comparison meaningful. The old parser returned a run of NULs.

**C4 — File-list entries start at offset 11, not 9**, and there is a
`u16le` at offset 9 that is a resumption gate: `q2.a` contributes nothing
unless it equals the number of entries already accumulated.

**C5 — Entry stride is not fixed at 10.** A `tableswitch` on
`l3.portVersion`: 1 → 8, 2..6 → 9, default → 10. Because `q.m` has no
initialiser, an un-handshaken session (0) also takes the default branch, so the
rule must be written `pv == 1 ? 8 : 2 <= pv <= 6 ? 9 : 10`, never `pv >= 7`.

**C6 — Scene and attribute are swapped.** The 10-byte loop calls
`new BleFile(d5, d6, a3, a2)` with `a2` at `+8` and `a3` at `+9`, and
`BleFile(sessionId, fileSize, attribute, scene)`. So **+8 is scene, +9 is
attribute**.

**C7 — The "type-4 overlapping byte" puzzle was a misattribution.** Protocol
type 4 is the BLE *throughput probe* (opcode 101), not file sync. Its parser
genuinely reads a u32 and a u8 at the same offset 1 and copies from 2, so only
the u8 reading is self-consistent — almost certainly a copy-paste remnant of
the file-sync handler where the two reads are at `i` and `i+4`. File data is
**protocol type 2**, and its layout has two portVersion branches that the old
reading had collapsed into one confused frame. The overlap is preserved and
both values surfaced, with neither given a protocol name.

**C8 — The file-data frame's byte 9 is a LENGTH, not a "code".** The old
`parse_transfer_frame` named it `code` and returned the whole tail as payload;
the SDK reads `length = u8@(i+4)` and takes `payload = [i+5 : i+5+length]`
clamped to the frame end.

**C9 — `n7`'s offset-7 field is named `timezone`.** The previous
reconstruction called it `raw7` and recorded it as "unnamed in toString"; the
`toString` literal names it, and the consumer confirms the role
(`bleTimeSync(timezone, stamp)`). The previous session's own fixture already
recorded the consumer correctly — the code and the fixture disagreed and
nothing caught it.

**C10 — Both syncTime timezone fields are signed int8.** Java's `/` and `%`
truncate toward zero and keep the sign, so UTC−03:30 sends `FD E2` = (−3, −30),
not (−4, +30) and not (3, 30). A reimplementation using `abs(offset) % 60` is
wrong by up to 59 minutes for every half-hour zone west of Greenwich.

**C11 — Automatic loss-triggered retransmission is real.** The prior brief
warned against claiming it without proof. The proof is in `q$a.a`: on an offset
gap the SDK stops the sync and re-issues
`syncFileStart(sessionId, lastPosition = cursor, end)`, guarded by an
in-flight flag and a 5 s timeout, with up to 3 write retries. A TAIL arriving
while the cursor has not advanced also restarts.

**C12 — `w$c` is the request table and `w$a` the response table.** The two
~70-entry tables overlap heavily, so only the opcodes unique to one decide it:
13 are `w$a`-only and 11 of those are claimed by *response* classes; 11 are
`w$c`-only and 10 are claimed by *request* classes. The marker constants agree
(both host-sent markers are `w$c`-only; the device-sent `0xFE11` is
`w$a`-only). The alphabetical order invites the opposite guess — and one
recovery pass in this audit did guess the opposite, which is why this is now
asserted by a counting test rather than by prose.

**C13 — `k3` is byte-exact.** The previous session recorded "no k3 building…
request construction is still not byte-exact". Walking `k3.enPkg` gives the
whole frame: `[01][01 00][02][z.h()=0][stage]` then the token right-padded with
ASCII `'0'` to 16 or 32 characters, trimmed by `u4.b`. The fourth constructor
argument (portVersion) never reaches the wire — it only selects the layout.
What cannot be produced is a *valid token*, which is an account-issued
credential, not a protocol gap. Claim upgraded from "unresolved" to
"byte-exact, credential-blocked".

**C14 — The MTU exchange is not harness convenience; it gates the
connection.** `requestMtu(255)` is issued the instant the link comes up, a
false return **disconnects**, and `discoverServices()` has exactly one call
site in the entire artifact — inside `onMtuChanged`. The previous session
recorded MTU as "harness-only setup, hardware behavior UNKNOWN".

**C15 — `getChargingState` is zero-packet on Android only.** iOS's
`BleAgent.getChargingState()` writes `01 09 00`. The previous finding was
stated unconditionally.

### The central architectural finding

**On portVersion ≥ 20 the entire control channel is encrypted.**
`z.d(byte[])` seals every outbound frame and `z$c.onCharacteristicChanged`
opens every inbound one with ChaCha20-Poly1305 over `[u32le seq][ordinary
frame]`, keyed by a 32-byte key, 12-byte nonce and 12-byte AAD that the device
delivers RSA-encrypted during the pre-handshake.

The previous session had noted this as a "post-bind ChaCha envelope finding"
but left the emulator silent about it. It is not a footnote: it decides whether
the emulator speaks the real protocol at all.

What rescues the work is that **portVersion is advertised, not negotiated**
(`u4.a(ScanResult)` → `BleDevice.getPortVersion()`, a `final` field never
refreshed from the handshake). A device declares before connection whether its
link is encrypted. So the cleartext emulator is a *faithful* model of a
sub-20 device, not an approximation of a modern one — provided it says so.
`PlaudPeripheral` now carries an explicit `port_version` and **refuses to
construct** with a value ≥ 20 rather than serving plaintext under a modern
banner.

Also recovered, and worth recording as observations rather than defects to
repair: the same key *and nonce* are reused for every frame in both directions
(keystream reuse across the session); the TX counter is pre-incremented from a
reset value of 1, so the host's first sealed frame carries seq 2; the RX guard
is `if (N >= seq) return` with `N` starting at −1 and **never reset on
disconnect**, so a device that restarts its own counter is silently ignored
forever; and an AEAD failure is rethrown as a `RuntimeException` out of the
GATT callback rather than dropping the frame.

### Credential provenance — settles the outcome of R2

Plaud's **cloud** issues the RSA key pair
(`POST /developer/api/open/partner/sdk/gen-key` returns both `public_key` and
`private_key`) and the SN signature (`…/sdk/sn-sign`). The SDK never calls
`KeyPairGenerator` for this purpose. The handshake token is an account-issued
32-hex string.

**No offline bind is possible.** R2's outcome is therefore structural, not a
matter of more analysis: the emulator's `BOUND` state is unreachable by
construction. This is now stated as a conclusion rather than a to-do.

### The R2 asymmetry — the finding that changes the roadmap

Earlier work recorded R2 as "outcome B: machinery without auth", with `BOUND`
unreachable. That is right for one direction and wrong for the other, and the
distinction had not been drawn.

`q.b0()` goes straight to `q.a()` (first_handshake) whenever the advertised
portVersion is below 20, and `q.a()`'s only precondition is a non-empty token
string. There is no cryptography on that path at all. So the legacy handshake
is: the host sends a token, the **device** returns a status — and the secret is
the device's, not the client's.

* Binding a client **we** wrote to **real Plaud hardware**: still blocked, and
  structurally so. The token, the RSA key pair and the SN signature are all
  cloud-issued.
* Binding **Plaud's real client** to a device **we** wrote: **not blocked.**
  Accepting the token is our decision to make.

The emulator now implements the device side, and
`test_full_sdk_connect_sequence_reaches_bound_and_pulls_a_file` drives the SDK's
own stage order — scan, connect, MTU, discover, subscribe, first_handshake,
handshake_get_ssn, battery, sync_time (which is where `bleBind` fires), then
getState, getStorage, the file list and a complete `syncFile` — against it.
`BOUND` is now reachable, and the lifecycle docstring says exactly what that
does and does not mean.

Accepting any token is HARNESS POLICY. `accept_any_token=False` and a non-zero
`handshake_status` exist so the refusal paths stay tested.

### Implementation changes

| Before | After | Why |
|---|---|---|
| `pack_chunk_frame` with a `0x01` prefix | `pack_marker_frame`, 4-byte header | C1 |
| `parse_l3` big-endian version, `None` for unreached fields | little-endian; SDK Java defaults (`audioChannel` = **1**, `versionType` = `"V"`) | C2 |
| `parse_x2` "bytes after the NUL" | fixed offsets 59/60, bounded NUL scan | C3 |
| `parse_file_list_frame(data, entry_offset)` | offset fixed at 11; `frame_start_index` surfaced | C4 |
| `FILE_ENTRY_STRIDE = 10` | `file_entry_stride(port_version)` | C5 |
| `parse_file_entry` attribute@8 scene@9 | scene@8 attribute@9 | C6 |
| `parse_data_frame` (type 4, "cap") | `parse_file_data_frame` (type 2, both branches) **and** `parse_rate_test_frame` (type 4, unnamed fields) | C7, C8 |
| `PlaudSyncTimeState.raw7` | `.timezone`, signed int8 both ways | C9, C10 |
| `pack_resume_request` in **two** modules meaning **two different messages** | `pack_resume_record_request` (b5/22) and `pack_sync_start` (y6/28) | naming ambiguity flagged in the brief |
| `FileListAccumulator` ingesting pre-parsed tuples | ingests raw frames and reproduces the real gates, including the unrecoverable overshoot | C4 |
| Whole-request byte equality dispatch | dispatch on `[u8 type][u16le opcode]` | a real SDK sends the current wall clock, which the old emulator ignored |
| no opcode 9 | `PlaudBatteryState` + `push_battery()` | connection success is gated on it when portVersion ≥ 5 |
| `PlaudR1aPeripheral` | `PlaudPeripheral` (old name kept as an alias) | it outgrew R1a three milestones ago |

### Tests

40 → **116**. The new ones that matter:

* `test_evidence_conformance.py` (23) — the anti-circularity layer. Asserts the
  emulator against `docs/evidence-digest.json`, regenerates the digest when the
  evidence tree is present and fails if it differs, and fails if the digest's
  pinned AAR hash does not match the checked-out SDK.
* Falsification tests that distinguish the old reading from the new one:
  `test_l3_version_endianness_is_falsifiable`,
  `test_marker_frame_has_no_protocol_type_byte`,
  `test_file_list_entries_start_at_offset_11`,
  `test_file_entry_scene_and_attribute_are_not_swapped`,
  `test_file_data_frame_decoded_with_the_wrong_branch_is_garbage`.
* Behaviour that must **not** be "fixed": `test_accumulator_overshoot_never_completes`,
  `test_replay_window_never_resets_across_a_reconnect`,
  `test_empty_package_reads_its_code_at_i_plus_5_skipping_the_length_byte`,
  `test_duplicate_index_with_different_payload_is_not_deduplicated`.
* `test_injected_gap_is_detectable_by_the_sdk_rule` — the emulator can now
  inject a DATA gap, and the test proves a cursor-tracking host sees the
  discontinuity the SDK's resend path keys on. The emulator does **not**
  retransmit, and nothing claims it does.

### Method note

Eleven independent recovery passes ran over the bytecode in parallel, each
followed by adversarial verification of its own DIRECT/SOURCE-DERIVED claims.
Two things this caught that a single pass would not have:

* A recovery pass **fabricated a citation** — "confirmed byte-for-byte against
  iOS `BleAgent.dataOfGetStorage()`" — for a symbol that does not exist, and
  attributed it to a `.swiftinterface`, which contains declarations only and no
  function bodies. The verifier refuted it. The claim was never propagated.
* Two passes reached **opposite conclusions** about which opcode table is
  which direction. That disagreement is what prompted the counting test in
  §C12 instead of accepting either.

### Confidence after the audit

| Milestone | Status | Evidence |
|---|---|---|
| R1a getState | FROZEN — AUDITED | DIRECT |
| R1b getStorage | FROZEN — AUDITED | DIRECT layout/names; units UNKNOWN |
| R1c syncTime + MTU | FROZEN — AUDITED | DIRECT; signedness corrected |
| R1d lifecycle | FROZEN — AUDITED | SOURCE-DERIVED; platform split corrected |
| R2 handshake | PARTIAL — NOT FROZEN | framing/parsers DIRECT; crypto DIRECT; **bind impossible offline** |
| R3 file sync | FROZEN — AUDITED | DIRECT after 5 corrections |
| R3 emulator | PARTIAL — NOT FROZEN | policy-bounded; no pacing or payload-size evidence |

### U1 was chosen, and done in the same session

`u4.a(ScanResult)` was the gating unknown, so it was walked in bytecode
(offsets 254–719) rather than deferred: without it the emulator cannot be
*discovered*, and no later milestone can be attempted. jadx is unusable for that
method. The result is `emulator/plaudsim/advertising.py` (a port of the parser
plus a builder constrained by it) and `tests/test_r1e_advertising.py`.

A separate recovery pass reached the same decode independently, including the
variant split on `b[0] == 2`, the fixed 15/18 portVersion offsets, and the
trailing `01 <bindInfo>` pair. What remains UNKNOWN (U18) is which of the three
branches real hardware's advertisement actually lands in — only two of them
assign `portVersion` at all, and the most plausible-looking layout (a serial of
10+ bytes) is the one that does not.

### Next experiment, chosen on evidence

**Drive the real `plaud-sdk-public` against the emulator over Bumble's
`android-netsim` transport.**

Bumble ships `bumble/transport/android_netsim.py`, which attaches a Bumble
device to the Android emulator's virtual Bluetooth network as a controller. The
Android SDK is an AAR that needs an Android runtime and `android.bluetooth.*`,
which an AVD provides — so the stack is:

```
Android emulator  (real plaud-sdk.aar + the template app)
        ↓ android.bluetooth.*
    netsim virtual Bluetooth network
        ↑ bumble android-netsim transport
    Bumble  ←  plaudsim.PlaudPeripheral
```

**No radio and no Plaud hardware anywhere in that diagram.** The earlier
assumption that the money shot needs "a $10 USB BLE dongle" is worth revisiting:
netsim is a fully virtual path.

Two things make this the right next step rather than a speculative one:

1. The emulator now answers every message the SDK's connect state machine needs
   for a portVersion-7 device — handshake, getSsn, battery, syncTime — and the
   in-process rehearsal of that exact sequence passes.
2. The remaining risk is concentrated and identifiable: whether the template app
   can be built and run without a valid partner `userAccessToken` (`initSDK`
   takes one), and whether U18's advertising branch is the one real firmware
   uses. Both are answered by the same experiment.

If the app cannot start without cloud credentials, the next best validation is
the **Wi-Fi transport** (ledger §7): the phone is the WebSocket *server* there,
so a software-only emulator can serve it with no BLE stack at all.

---

## 2026-09-23 — R7-S12 closure session: runtime proof, opcode 8, corrections

**What was proven at runtime (official, unmodified SDK ↔ our emulator, netsim).**
`recoveryConnectBleDevice(device, "SYNTH-HIST-0001")` made the real SDK build
and write its k3 to 1910/2BB1; the 22 bytes equal the bytecode model exactly
(`01 01 00 02 00 00` + `SYNTHHIST0001` + `000`). Run 3 proved truncation to 16
chars at pv 7; run 2 (empty id) proved the SDK writes nothing and aborts with
`bleConnectState(2)` — the empty-token guard, not a cloud guard, is the only
gate. The emulator accepted the genuine k3 and the SDK reported `bleBind
status=0`. Report: `r7/r7-s12-k3-runtime-capture.md`; ledger §14.

**What was discovered.** After getState the SDK sends two opcode-8 frames
(`01 08 00 01 0f 00`, `01 08 00 01 11 00`) the emulator had never seen. They are
CommonSettings READs of ENABLE_VAD and REC_MODE. Bytecode resolved the whole
channel (q0/r0 layouts, waiter `{8, type+1000}`, the consumer `q.f`), and —
because ProGuard kept the enum name strings — the full 21-entry CommonType
table with its **non-ordinal wire values**. Implemented (`_common_settings`),
19 tests incl. javap pins; run 4 answered both reads with zero rejects.

**What was wrong and is now fixed.**
* Ledger §5.8/§5.9 said the SDK "stops the sync" / "sends stopSync the moment
  it sees a gap". Bytecode: the gap reaction is to re-issue `syncFile` from
  the cursor; `stopSync` is sent only by the 5 s stall-timeout runnable.
  "Tears down the opcode-29 handler" was a plain putfield `z.n = 29`.
  `transfer.py`'s docstring carried the same imprecision. The client's
  automatic resend is BYTECODE_PROVEN, not INFERRED and not POLICY.
* Ledger §4.3 stated AES-GCM as a generic alternate inside the BLE section;
  it is Wi-Fi-only (opcode-138 bit 3). Scoped.
* Opcode 138 was implemented and tested but absent from the ledger. §5.13 added.
* `bleBind(... protVersion=0, tz=0)` in the first runs looked like an emulator
  gap. It is a facade quirk: both arguments are `q.n` = the l3 *timezone* byte
  (`l3.e()`), not the port version. Predicted, then observed (`l3 tz=5 ⇒
  protVersion=5`). §14.
* A Swift-ordinal reading would have named the two runtime reads
  AUTO_RECORD/FIND_MY; the Android wire table proves ENABLE_VAD/REC_MODE. The
  cross-reference works by ordinal, the wire byte is a separate field.
* Stale counts (154/170/188 tests; "25 bytecode-asserting tests") annotated;
  the closure report carries the current totals.
* PROJECT.md §5.8 stated the recording format as settled; the R6 rows say
  UNCONFIRMED against real recordings. Reconciled in the closure report: the
  *codec geometry the SDK enforces* is BYTECODE_PROVEN, *what real firmware
  emits* is U20.

**New unknowns registered.** U19 cloud OpusTags vendor `PALUD.AI` vs SDK
writer `TinnoTech…` (cloud artifacts ≠ local writer); U20 which of the four
closed recording shapes real firmware emits; U21 legacy TntAgent host; U22
real firmware's capability bits / settings values.

**Hygiene.** `tests/conftest.py` sets `sys.dont_write_bytecode` because bumble
is editable-installed from `reference/upstream/bumble`; the Gradle artifacts
R4-S3 left under `reference/plaud-org/plaud-sdk-public/android/` are removed
at closure. Reference integrity: 33/33 commit pins, AAR sha256 match, no
tracked evidence file modified (checked by `git status` inside each repo).

## R7-S13 (2026-09-23/24) — the real SDK pulls a recording; the frozen transfer sequence was incomplete

**What was done.** Extended the R7-S12 rig with a pull driver (public
`getFileList`/`syncFile`/`exportAudio`) and a peripheral serving a real
Ogg/Opus fixture; 17 runs with archived logs (`r7/r7-s13-evidence/`).

**What earlier work got wrong.**
* §5.8 documented HEAD·DATA·TAIL. The genuine client never completed on it.
  Completion is `v3.finish(code)` from the EMPTY_PACKAGE branch of `q$a.a`
  — the frame shape was in the ledger, its role was not. V1's Python client
  model never needed the sentinel, which is exactly the circularity the
  runtime rung exists to catch. Emulator default corrected; 8 existing
  count assertions updated; negative test keeps the old sequence reachable.
* §5.8 said stopSync is reached only via the 5 s stall runnable. The gap
  branch writes stopSync immediately (`q.k`) and restarts on the ack.
* The emulator answered faster than a phone's GATT write callback, which
  broke the tinnotech op-queue's request/response pairing (-98/-99). Pacing
  is now a documented policy knob.
* The emulator kept draining an abandoned stream after a restart; the SDK
  then produced a corrupted export. Streams are now abortable.

**What was confirmed.** Byte-exact delivery on both public paths; the facade
`bleBind` quirk again; `bleSyncFileTail(status)` = the TAIL u16; gap
recovery from the cursor; OPUS export passthrough for Ogg input.

**Unknowns.** U15 and U17 updated; U17's Phase-1 rationale ("nothing in the client orders TAIL and EMPTY_PACKAGE") was wrong and is corrected. U23, registered in a first draft, duplicates U15.

