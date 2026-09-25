# Plaud / Tinno BLE Protocol Specification

> **SUPERSEDED IN PART.** [`protocol-ledger.md`](protocol-ledger.md) is the
> authoritative record as of the 2026-09-22 audit; where the two disagree, the
> ledger wins. This document is kept for its narrative of how each area was
> approached, but several of its layouts were corrected — in particular the
> pre-handshake framing, `l3`'s version endianness, `x2`'s versionName, the
> file-list entry offset and stride, the scene/attribute order, and the
> attribution of protocol type 4. See
> [`reconstruction-log.md`](reconstruction-log.md) for the full list.

Status: evidence-backed reconstruction, 2026-09-21. This document is the
human-readable companion to `docs/protocol.json`. It separates public SDK facts,
decompiled-artifact facts, and inferences. No hardware capture is available.

## Recording/file synchronization forensics (R3)

Forensic reconstruction only — no emulator behavior was added. Full record:
[file-sync.json](fixtures/file-sync.json). Pure codecs/parsers live in
`emulator/plaudsim/filesync.py` with synthetic-vector tests; nothing sends,
assembles, or transitions state.

### syncFile is a facade operation, not a packet

`PlaudDeviceAgent.syncFile(sessionId, start, end)` builds a transfer task
(`t7.d` → `v3`), wires voice-data/finish receivers (`u3$f`/`u3$d` + app
lambdas), then starts transfer via `q.a(JJJZ…)` (guards: null creator →
'syncFileStart ignored'; null BLE op → 'aborted … (device disconnected)').
iOS calls it from `blePenState` at `state == 4099` and from
`bleRecordStart(sessionId, start, …)` (start passed through, end always 0);
Android from recording flows. Same 3-long shape on both platforms.
`start` is a SOURCE-DERIVED byte offset (resend path restarts at
`lastPosition`; `g4` assembles at absolute offsets); `end=0` is an OBSERVED
literal in both iOS call sites, INFERRED as an open-ended sentinel — exact
semantics UNKNOWN.

### Transfer framing (all bytecode-verified)

```text
start:  y6(sessionId, start, end), opcode 28
        [01][1C 00][u32 sessionId][u32 start][u32 end] (15 B, longs→u32)
        listens {28, 29}
head:   s6 SyncFileHeadRsp{sessionId, status}: u32@3, u8@7 (8 B, no guards)
        status<=0 continues (next chunk request); nonzero → finish path
        (exact codebook UNKNOWN)
tail:   t6 SyncFileTailRsp{sessionId, crc}: u32@3, u16@7 (9 B, no guards)
        forwarded as bleSyncFileTail(sessionId, crc); NO calculate-and-
        compare exists in the sources: UNVERIFIED, not a validated CRC
data:   two frame families (relationship UNKNOWN); see below
```

No aliasing: s6/t6 are the sole `a()==28`/`a()==29` classes.

### Transfer data frames: q$a family (primary) and type-4 family (puzzle)

The transfer session (`q.a(JJJZ…)`) registers its `q$a` handler for opcodes
{28, 29}. `q$a.a([B)` is self-consistent and complete:

```text
[type u8 ∈{1,2}][session u32le@1][offset u32le@5][code u8@9][payload@10..]
```

Session gate (match or '---sessionId miss match---' return); `offset ==
0xFFFFFFFF` marks EMPTY_PACKAGE (no payload); cursor-advance copy with
received-count accumulation; loss detection (`---isPacketLossStopSync---`,
`start resend syncFileStart(sessionId:,lastPosition:)`,
`fileSyncLossPkgStop`); `u3$b` progress callbacks; inline control branches —
word `==28` parses `s6` ('---SyncFileHeadRsp---', status-gated
continuation), word `==29` parses `t6` ('---SyncFileTailRsp---', finish
events). Loss is handled by session resend-from-lastPosition restarts, not
per-packet ACKs (no ACK/NAK frames found; absence noted, not assumed).

The parallel `q.a(v3, [B)` path gates `byte[0]==4`, reads offset `u32le@1`,
clamps with `u8@1`, and delivers `raw[2 : 2+min(cap, len-2)]` to
`z3.receiveVoiceData(payload, offset)`. The mechanics are exact; the
semantics are an open puzzle (cap shares byte 1 with the offset's low
byte). Related feeders (`g4/h4/k4/l4/o4`, `r4.a([SJ)`, `q$a.a` itself) all
terminate in `z3.receiveVoiceData`; per-variant family assignment UNKNOWN.

Session task `g4` accumulates into a `ByteBuffer` with offset tracking
(`start:`, `lastDataStart:`, `dataLossLen`), `flush()`/`finish()`/
`hasCompleteTail()`, under an `ogg2opus` conversion tag.

### File list and metadata (corrected: p2, not b5)

`getFileList(sessionId)` → `q.d` → `q.a(J, Z)` → **p2(sessionId, page,
flag)**, opcode 26: `[01][1A 00][u32 session][u32 page][u8 flag]` (12 B).
Second u32 reads the `s5.e` counter (pagination candidate, writer
unmapped: UNKNOWN); flag UNKNOWN. Listens {26}, single registration.
Response: **q2** `GetRecSessionsRsp{totals u16@7, entries}` accumulated
**across multiple opcode-26 notifications** by the session-gated `s5`
holder (fresh `s5.c()` per request = reset on new request: OBSERVED);
entries appended per frame via `q2.a(bytes, offset)`. Entry stride 10:
`[u32 sessionId][u32 fileSize][u8 attribute][u8 scene]` →
`BleFile(sessionId, fileSize, attribute, scene)`. The entity carries NO
filename, timestamp, duration, or format. Completion = `list.size()` vs
totals (exact terminal frame UNKNOWN); ordering/sequence NOT frame-encoded
(UNKNOWN); missing-frame detection UNKNOWN beyond the size mismatch.
Facade: `process_item_data(q2)` → `cacheFileS…` → `bleFileList(List)`.
`b5` (opcode 22) is the **resume-record** request (`resumeRecord(sid,
scene=0)` literals → `t3.a(J,I)` → `q.a(J,I)` →
`[01][16 00][u32 session][u8 scene]`), answered by `c5`
`RecordResumeRsp{sessionId u32@3, start u32@7, status u8@11, scene u8@12,
startTime u32@13}` via the `q.E` lambda bound by that registration.
Opcode-22 alias resolved by mechanism: resume responses route via the
`q.E` lambda of the `q.a(J,I)` registration (news `c5`); file-list
responses route via the `q.a(s5,…)` lambda of the `q.a(J,Z)`
registration (accumulates `q2`); `b8`/WifiOTAPackageRsp is constructed
only in `w7` (Wi-Fi agent), unreachable from BLE registrations. Opcode
alone is ambiguous across domains; registration context disambiguates
within BLE.

### Audio decode chain

`g4` bytes → `OggUtils.init/putPkg` streaming OGG assembly (native;
callers `k4.a`, `AudioExporter`) → `OpusUtils` native Opus
`decode(handle, opus, pcm[])` (MS variants exist; encoder unused here).
Boundaries, separately evidenced: (A) one `getValue()` byte[] per
`onCharacteristicChanged`, each ≤ MTU-3; (B) one logical frame per
notification in the `q$a` path (concatenation NOT evidenced — UNKNOWN
whether a notification can hold multiple app frames); (C) absolute-offset
`ByteBuffer` appends completing at TAIL/finish; (D) Ogg pages internal to
native `putPkg` (UNKNOWN mapping); (E) Opus packets internal to native
`decode` (UNKNOWN mapping). No two boundaries proven identical. File bytes
travel inside the post-handshake ChaCha envelope like everything else on
2BB0. CRC validation site unmapped (see t6 downgrade above).

### Transport boundary (R3)

Control + HEAD/TAIL: single ATT payloads. File data: application-segmented
stream of per-session notifications reassembled by g4 ByteBuffer with offset
tracking; per-frame size bounded by negotiated ATT MTU (SDK asks 255).
Resumption (`syncFile(start,end)`, `startFileSyncFromOffset`,
`syncHistoricalData`) is a strongly inferred capability with unmapped
handshake.

### R3 emulator behavior (NOT FROZEN — harness policy, not device claims)

`emulator/plaudsim/transfer.py` + peripheral dispatch implement the q$a
family, file-list, resume, and transfer-start paths with synthetic content:

- `y6` starts (or replaces) the session and emits HEAD (`status=0`) +
  32 B DATA frames from the cursor + TAIL (arbitrary CRC passthrough) as
  one deterministic sequence. `start>0` resumes from that offset,
  mirroring the observed resend shape.
- `p2` is answered with the full synthetic table in one q2 frame;
  `page`/`flag` are parsed but inert (their semantics are UNKNOWN, so they
  change nothing — asserted by tests).
- `b5` is answered with session/scene echoed from the request plus fixed
  synthetic fields.
- Malformed shapes (right opcode, wrong length) are rejected explicitly
  with an error log entry and no response.
- Pacing, payload size, session replacement, and full-table answers are
  harness choices. No auth, no bind, no real-device compatibility. The
  `q.a(v3)` family stays parser-only with UNKNOWN semantics.

## BLE handshake reconstruction (R2)

Outcome B: packet/state machinery without authentication semantics. Every
cryptographic input (RSA pair, SN signature, SN, token, session keys) is
account/hardware-issued and absent here, so no bind is producible. R2
implements framing codecs, response parsers, and the dispatch rule with
synthetic vectors only. Full record: [handshake.json](fixtures/handshake.json).

Recovered driver sequence (`q` methods, bytecode-verified):

```text
GATT_CONNECTED
  -> q.n() preHandshake: v4(Base64(snSignature), 100B chunks)
       -> 0xFE11: proceed to sendRSAPublic
       -> 0xFE12: collect chunks -> RSA-decrypt (user private key)
                   -> J=[0:32] key, K=[32:44] nonce, L=[44:56] AD
                   -> self-check decrypts to 'PLAUD.AI', else abort
  -> q.a0() sendRSAPublic: w4(userRSAPublicKey bytes, 100B chunks)
  -> q.a() firstHandshake: k3 (registers opcodes {1,2})
       -> type word 1: 'old HandShakeRsp', new l3 (status==0: legacy done)
       -> type word 2: 'handshake_get_ssn', new x2 (store SSN, then q.d0())
       -> else: fail callbacks ('HandShakeReq:fail-callbackType:')
  -> q.d0() twoHandshake: j3 (k3 + user field)
       -> l3.status==0: record capabilities -> syncTime
  -> syncTime (R1c) -> bleBind(sn, status, protVersion, timezone)
```

Credential provenance (the fabrication line — none of this exists here):
RSA pair from `POST /developer/api/open/partner/sdk/gen-key`
(`{private_key, public_key}`; no `KeyPairGenerator` in the 674-class AAR, so
the phone mints nothing); SN signature from `POST .../sn-sign {sn}`; SN from
physical scan; token from the account `deviceToken`/`serToken` family;
ChaCha session keys device-generated per handshake. Phone holds the user
private key (q5 parses PKCS8 PEM) and uploads the user public key.

Post-bind encryption envelope (bounds R1a–R1c): once J/K/L are set, `z$c`
ChaCha20-decrypts **every** 2BB0 notification (`q5.b(J,K,L,value)`; invalid
→ 'ChaCha20 decrypt result invalid, skip'). R1a–R1c therefore reconstruct
the **inner plaintext layer** the parsers consume — our emulator speaks that
layer, not the wire ciphertext. No app-layer control-frame reassembly exists
(single `getValue()` per notification).

Opcode-4 aliasing resolved: `z7`/WifiCloseRsp (family `m`) shares `a()==4`
but has no constructor call-site in the AAR and is built in `w7` (Wi-Fi
path); the live BLE path constructs `n7` in `q.X`.

Implemented (`emulator/plaudsim/handshake.py`, `tests/test_r2_handshake.py`,
9 pass): v4/w4 framing round-trips, `(len+99)//100` chunk rule, l3/x2
parsers on synthetic vectors, `select_handshake_response` mirroring the q.a
type-word dispatch.

SUPERSEDED by the R2a outcome below: the legacy (portVersion < 20) device
side IS implemented — `profile.PlaudPeripheral._handshake` answers k3/j3
with l3, w2 with x2, and reaches BOUND by accepting the token (HARNESS
POLICY). RSA/ChaCha operations remain unimplemented; binding our own client
to real hardware remains blocked on cloud-issued credentials. See
`protocol-ledger.md` §4.6 and §5.5.

## Post-bind lifecycle (R1d)

Recovered from both templates; they differ. The full record is
[device-lifecycle.json](fixtures/device-lifecycle.json).

iOS (`DeviceManager.swift`): `scanning → connecting → GATT connected`
(`bleConnectState` case 1, explicitly *not* success) → handshake stages
(`ConnectStage` through `syncTime`, RSA per `sendRSAPublic`/`firstHandshake`/
`twoHandshake`/`handshakeGetSSN`) → `bleBind(status == 0)` (this *is*
connection success) → `refreshDeviceInfo()` (`getState` + `getStorage`) →
`blePenState` (if recording, `state == 4099`, then `syncFile`). Battery is
pushed (`blePowerChange`/`bleChargingState` after `setBatteryNotify`); the
template never calls `getChargingState()` or `syncTime()` directly.

Android (`DeviceManager.kt`): `Connected` (idempotent) → `syncTime()` →
`getConnectedDeviceVersion()` → `refreshDeviceInfo()` (`getChargingState` +
`getStorage`) → `refreshRecordState()` → `checkFirmwareUpdate()` (cloud,
sn-sign) → delayed `getFileList` 3 s after connect.

Platform differences: syncTime is an iOS connect-stage internal but an
Android explicit post-connect call; post-bind refresh is `getState`+`getStorage`
(iOS) vs `getChargingState`+`getStorage` (Android); battery is push-driven on
iOS and cache-polled on Android (same push path feeds the cache); recording
recovery is inline in `blePenState` (iOS) vs explicit `refreshRecordState`
(Android); file list is on-demand (iOS) vs auto-refresh (Android).

### getChargingState: zero BLE packets

`PlaudDeviceAgent.getChargingState()` calls `t3.b()Z` and `t3.c()I` — pure
cached-field getters (`q.b`→field `d`, `q.c`→field `c`) — then invokes
`listener.bleChargingState` synchronously. No request is constructed, nothing
is sent. The cache is fed by device-pushed `BattStatusRsp` (`p`, opcode 9)
parsed in the `q$f` device-event dispatcher. There is no charging-poll
exchange to implement; R1d records this as a finding, not a packet.

### Emulator state model

`PlaudLifecycle`: `DISCONNECTED → GATT_READY` (on `install()`, GATT DB
published) `→ CONNECTED` (on the Bumble connection event, mirroring
`bleConnectState(1)`) `→ DISCONNECTED` (on link loss). `BOUND` (bind status 0)
and `READY` exist for the handshake future and are unreachable: tests assert
bare R1a/R1b/R1c exchanges never advance past `CONNECTED`. Lifecycle events
go to a separate `lifecycle_log`; the raw-wire `packet_log` is untouched.

Confidence values used here are `CONFIRMED`, `STRONGLY INFERRED`, `HYPOTHESIS`,
and `UNKNOWN`.

## Evidence sources

| Source | What it contributes |
|---|---|
| `reference/plaud-org/plaud-sdk-public/sdk/ios/PlaudBleSDK.framework/Modules/PlaudBleSDK.swiftmodule/arm64-apple-ios.swiftinterface` | Public agent stages, callbacks, methods, device fields, RSA types, sequence properties. |
| `reference/plaud-org/plaud-sdk-public/sdk/ios/PlaudDeviceBasicSDK.framework/Modules/PlaudDeviceBasicSDK.swiftmodule/arm64-apple-ios.swiftinterface` | Public facade lifecycle and callback surface. |
| `reference/plaud-org/plaud-sdk-public/ios/PlaudTemplateApp/Managers/DeviceManager.swift` | Real iOS call ordering, handshake success/failure handling, post-bind queries. |
| `reference/plaud-org/plaud-sdk-public/android/app/src/main/java/com/plaud/template/managers/DeviceManager.kt` | Android token/key preparation, SN signing, callback handling, independent lifecycle confirmation. |
| `reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar` | Obfuscated protocol classes, native codec symbols, UUID/opcode artifacts. |
| `docs/protocol.json` and `scripts/extract_protocol.py` | Existing extracted opcode and payload-offset records. |

## GATT structure

| Element | Value | Confidence | Evidence / reasoning |
|---|---|---|---|
| Primary service | `00001910-0000-1000-8000-00805f9b34fb` | `CONFIRMED` | Recovered in `docs/protocol.json` from the Android SDK artifact. |
| Custom characteristic A | `00002BB0-0000-1000-8000-00805f9b34fb` | `CONFIRMED` | Recovered UUID constant. |
| Custom characteristic B | `00002BB1-0000-1000-8000-00805f9b34fb` | `CONFIRMED` | Recovered UUID constant. |
| CCCD | `00002902-0000-1000-8000-00805f9b34fb` | `CONFIRMED` | Standard descriptor used by notification subscription. |
| Battery service | `0000180F-0000-1000-8000-00805F9B34FB` | `CONFIRMED` | Standard service and public `setBatteryNotify` / `readBattery` stages. |
| Battery level | `00002A19-0000-1000-8000-00805F9B34FB` | `CONFIRMED` | Standard characteristic. |
| `2BB0` properties | Device-to-host data notification/indication channel | `CONFIRMED` for role; runtime mask unknown | Decompiled `z.onCharacteristicChanged` checks `w.d.b` (2BB0) for incoming data; descriptor setup chooses indication when runtime properties include `0x20`, otherwise notification. |
| `2BB1` properties | Host-to-device command write channel | `CONFIRMED` for role; write mode unknown | Decompiled `z.sendData` and `z.d(byte[])` resolve `w.d.c` (2BB1) and call `BluetoothGatt.writeCharacteristic`; the artifact does not expose a fixed `WRITE` versus `WRITE_WITHOUT_RESPONSE` requirement. |
| Advertising payload | Device name, serial, project/model metadata are surfaced after scan | `UNKNOWN` | Template code consumes `BleDevice` fields but does not expose raw advertisement bytes. |

The direction is confirmed. The fixed runtime property bitmask is not exposed:
the Android client chooses `ENABLE_INDICATION_VALUE` when the discovered
characteristic has property bit `0x20`, otherwise it chooses
`ENABLE_NOTIFICATION_VALUE`.

## Connection and initialization lifecycle

The iOS public `BleAgent.ConnectStage` enum gives this ordered sequence:

```text
start
  -> gattConnect
  -> setNotify
  -> setBatteryNotify
  -> readBattery
  -> setDataNotify
  -> preHandshake
  -> sendRSAPublic
  -> firstHandshake
  -> twoHandshake
  -> handshakeGetSSN
  -> changeHandshakeTimeout
  -> syncTime
```

| Transition / behavior | Confidence | Evidence |
|---|---|---|
| Scan precedes connect | `CONFIRMED` | iOS and Android template managers call `startScan`, consume `BleDevice`, then call `connectBleDevice`. |
| GATT connection precedes notification setup | `CONFIRMED` | Ordered public `ConnectStage` values. |
| Battery notification/read occurs before data notification | `CONFIRMED` | Same enum order. |
| RSA/public-key exchange occurs after GATT setup | `CONFIRMED` | `sendRSAPublic` follows `preHandshake`; iOS SDK exposes RSA key/message types. |
| Handshake has two phases | `CONFIRMED` | `firstHandshake` and `twoHandshake` are distinct public stage values. |
| SN is obtained during handshake | `CONFIRMED` | `handshakeGetSSN` stage and `bleBind(sn:...)` callback. |
| `bleConnectState == 1` means handshake success | `DISPROVED` | Both template managers explicitly wait for `bleBind(status == 0)`. |
| `bleBind(status == 0)` means usable connection | `CONFIRMED` | iOS and Android transition to connected only at this callback. |
| Time synchronization follows handshake | `STRONGLY INFERRED` | `syncTime` is the final public connect stage; exact request is not mapped. |
| `getState` and `getStorage` follow successful bind | `CONFIRMED` | iOS `bleBind` calls `refreshDeviceInfo`; that calls both methods. Android independently calls them in its refresh path. |

Android adds an account-side prerequisite before the BLE call: wait for partner
RSA material, sign and store the device serial number using the device type, then
call `PlaudDeviceAgent.connectBleDevice`. This is not itself a BLE packet, but it
means a faithful SDK-level emulator must account for the expected SN signature or
provide a controlled test path that bypasses the real account service.

## Commands and responses

The extracted protocol table identifies message classes and opcodes, but semantic
name recovery is incomplete. The following records are safe to use as response
layout evidence, not as complete exchanges:

| Response | Opcode | Payload layout after byte 3 | Confidence |
|---|---:|---|---|
| `UniversalErrRsp` | 0 | no parsed fields | `CONFIRMED` class/opcode record |
| `HandshakeRsp` | 1 | no parsed fields | `CONFIRMED` class/opcode record |
| `SayHelloRsp` | 2 | no parsed fields | `CONFIRMED` class/opcode record |
| `HeartbeatPing` | 3 | class/opcode record; another opcode-3 response has parsed fields | `CONFIRMED` record, semantics unresolved |
| `TimeSyncRsp` | 4 | u32 `stamp` at offset 3; bool `hasStatistics` at offset 8 | `STRONGLY INFERRED` layout from literal parser offsets |
| `DepairRsp` | 5 | u8 `status` at offset 3 | `STRONGLY INFERRED` layout |
| `StorageRsp` | 6 | u64 `free` at 3, u64 `total` at 11, u64 `duration` at 19 | `STRONGLY INFERRED` layout |
| `CommonSettingsRsp` | 8 | u16 `type` at 3, u32 `value` at 5 | `STRONGLY INFERRED` layout |
| `BattStatusRsp` | 9 | u8 `charging` at 3, u8 `level` at 4 | `STRONGLY INFERRED` layout |
| `RecordPauseRsp` | 21 | u32 `sessionId` at 3, u8 `reason` at 7, bool `fileExist` at 8, u32 `fileSize` at 9 | `STRONGLY INFERRED` layout |
| `RecordStopRsp` | 23 | same field pattern as pause | `STRONGLY INFERRED` layout |

The request table contains 74 entries. Request/response pairing is now
recovered per message in [`protocol-ledger.md`](protocol-ledger.md) §5, and
the machine-readable class census lives in `docs/protocol.json` and
`docs/evidence-digest.json`. The paragraph that used to stand here claiming
the requesters of `TimeSyncRsp`, `StorageRsp` and `BattStatusRsp` could not
be identified is superseded: they are `m7`, `a3` and `o` respectively.

The Android decompilation supplies this concrete handshake mapping:

| Stage | Request class | Opcode / marker | Response class | Response opcode | Success / next stage |
|---|---|---|---|---:|---|
| `preHandshake` | `v4` | `0xFE10` normal, `0xFE20` force-clear | marker `0xFE11` or encrypted `0xFE12` chunks | special marker | Marker dispatch selects RSA-public stage or decrypts the secret package. |
| `sendRSAPublic` | `w4` | `0xFE12` | per-chunk callback / secret response | `0xFE12` | Last chunk triggers `firstHandshake`. |
| `firstHandshake` | `k3` | `1` | `l3` legacy or `x2` SSN response | `1` or `2` | `l3.status == 0` completes legacy path; `x2` supplies SSN and triggers second handshake. |
| `twoHandshake` | `j3` | `1` | `l3` | `1` | `l3.status == 0` records capabilities and proceeds to time sync. |
| `syncTime` | `m7` | `4` | `n7` | `4` | Parsed response completes connection; send/parse failure retries up to three times. |

### Handshake byte layouts (corrected 2026-09-22 — supersedes all earlier revisions of this section)

All integer fields below are little-endian. Marker frames carry NO
protocol-type prefix: `v4.enPkg`/`w4.enPkg` override `enPkg` and never call
`packHead`, and the receive side reads the marker as u16le at offset 0.
Any `[01]`-prefixed marker layout seen in older revisions of this document
is wrong; see `protocol-ledger.md` §2 and
`test_marker_frame_has_no_protocol_type_prefix`.

```text
preHandshake v4 normal:
   [10 FE][u8 chunk_count][u8 chunk_index][chunk ≤100B]

preHandshake v4 force-clear:
   [20 FE][u8 chunk_count][u8 chunk_index][chunk ≤100B]
   (counts are util.c(int) single bytes, not u24; chunk rule (len+99)//100
   verified in q.n; wire order is count-then-index although the constructor
   takes (index, count, chunk, forceClear))

sendRSAPublic w4:
   [12 FE][u8 chunk_count][u8 chunk_index][up to 100 key bytes]

firstHandshake k3 base (SOURCE-DERIVED, byte-exact structure):
   [01][01 00][u8 constant 0x02][u8 agent = z.h() = 0][u8 stage iff pv>=3]
   [token ASCII right-padded with '0' to 32 chars iff pv>=9 else 16;
    longer tokens truncated]
   (packHead + 255-byte scratch + pad loop verified instruction-by-
   instruction; stage/token rule triangulated with the template-side fact
   that handshake tokens are 32 hex chars. Covered mechanically by
   evidence-digest k3.enpkg_structure. VALUES remain credential-blocked:
   the token is account-issued.)

twoHandshake j3 extension (SOURCE-DERIVED):
   k3 base
   [8 raw devToken bytes, zero-padded/truncated]
   [u8 len(userName)][userName bytes]
   (length widths verified as util.c(int) single bytes; the earlier u24 and
   "8-byte-field omitted when empty" readings are withdrawn — the shipped
   wrappers always append the block)

syncTime m7:
   [01][04 00][u32le Unix seconds]
   [i8 timezone hours][i8 timezone minutes]   (SIGNED — see below)
   (9 bytes total)
```

Timezone fields are SIGNED int8 both ways: `m7` computes
`offset/60000 → (offset/60, offset%60)` with truncating division and `n7`
casts through `(byte)`, so UTC−03:30 sends `FD E2` = (−3, −30).

The first-handshake `v4` request carries the base64-decoded SN signature in
100-byte chunks. Marker `0xFE11` selects the RSA-public-key stage; marker
`0xFE12` carries the encrypted secret package (reassemble → RSA-decrypt
with the stored private key → 32-byte ChaCha20 key, 12-byte nonce, 12-byte
associated data, then a `PLAUD.AI` self-check). The public-key upload uses
the same 100-byte chunking with `w4` and marker `0xFE12`.

## First post-bind exchange

The first deterministic query in the iOS template is `getState`:

```text
bleBind(status == 0)
   -> refreshDeviceInfo()
   -> PlaudDeviceAgent.shared.getState()
   -> q.f(...)
   -> y2.enPkg() = 01 03 00
   -> BluetoothGatt.writeCharacteristic(2BB1)
   -> notification on 2BB0 with opcode 3
   -> z2(byte[]) parser
   -> PlaudDeviceAgent listener.blePenState(...)
```

The exact fixture is [post-bind-get-state.json](fixtures/post-bind-get-state.json).
It contains exact request bytes and an exact response structure with unknown
runtime values; no response bytes are invented. The Android template has a
different post-connect order: it calls `syncTime()` before `refreshDeviceInfo()`.

## Third exchange (R1c): syncTime

Fully recoverable despite runtime-dependent values — no invention required,
only fixed emulator stand-ins for wall-clock and timezone.

```text
syncTime()                           # iOS ConnectStage tail; Android post-connect
  -> PlaudDeviceAgent.syncTime()
  -> t3.m(...) -> q.m(...)
  -> m7.enPkg() = 01 04 00 | u32le unix | u8 tzH | u8 tzM   (9 bytes)
  -> BluetoothGatt.writeCharacteristic(2BB1)
  -> notification on 2BB0 with opcode 4
  -> n7(byte[]) parser               # real name TimeSyncRsp
  -> syncTime$1.onCallback(n7) -> listener.bleTimeSync(n7.c(), n7.b())
```

| Element | Value | Provenance |
|---|---|---|
| Request bytes | `01 04 00` + u32le + u8 + u8 (9 B) | SDK-derived: `m7.enPkg` bytecode; `a(J)`→4B-LE, `c(I)`→1B each (widths verified in `TntBleCommUtils` bodies) |
| Request values | unix = `currentTimeMillis/1000`; tz = `getOffset/60000` split h/m via `m7$a` | SDK-derived structure; emulator uses fixed stamp=1700000000, UTC 0/0 |
| Response layout | u32 stamp@3, u8 raw7@7, bool hasStats@8 (len ≥ 9) | SDK-derived: `n7<init>` bytecode; unconditional prefix 8 bytes |
| Field names | stamp/hasStatistics, class TimeSyncRsp | SDK-derived: `n7.toString` literal (raw7 unnamed) |
| Consumer | `bleTimeSync(timezone=n7.c(), stamp=n7.b())`, getters unshuffled | SDK-derived: `syncTime$1` bytecode; Android only logs, iOS consumes in connect path |
| Opcode-4 alias | `z7` (WifiCloseRsp, family `m`) shares `a()==4` but has **no constructor call-site** in the AAR and is built in `w7` (Wi-Fi path) | SDK-derived: full-AAR scan; live BLE path constructs `n7` in `q.X` — no runtime ambiguity |
| Response stamp | FIXED emulator value, not an echo claim | explicit gap: echo-vs-clock UNKNOWN |
| Lifecycle position | iOS connect-stage tail vs Android post-connect | differs by platform; frame needs no auth bytes either way |

The exact fixture is [post-bind-sync-time.json](fixtures/post-bind-sync-time.json).
9 bytes fit the default ATT MTU, so R1c needs no MTU exchange. Notify and
indicate paths both tested; R1a/R1b untouched and green.

## ATT transport boundary (Part A finding, 2026-09-21)

Application payloads larger than the default 20-byte ATT payload need no
Plaud-specific fragmentation. What the shipped Android SDK establishes:

- On every `STATE_CONNECTED`, `z.f` calls `BluetoothGatt.requestMtu(255)`
  (field default set in `z.<init>`); `onMtuChanged` stores the negotiated
  value and forwards `(mtu, status)` to the listener. No MTU references exist
  in the templates or Swift interfaces — this is SDK-binary behavior.
- Each notification is consumed whole: `onCharacteristicChanged` passes a
  single `getValue()` `byte[]` to the registered `s$b` callback. No
  accumulation buffers exist in `z`/`z$c`.
- The `getPortVersion() < 20` branch in the callback is a legacy-device path,
  not a payload-length gate (early misread corrected during review).

Therefore the 27-byte `StorageRsp` is expected to arrive as **one GATT
notification/indication after MTU negotiation** (needs MTU ≥ 30; the SDK asks
255). The one unverified link is device-side MTU acceptance — firmware
behavior, invisible in app artifacts. iOS has no code-level MTU evidence
(binary SDK); CoreBluetooth negotiates automatically as platform behavior.
The emulator's virtual MTU exchange is the source-backed equivalent, and no
segmentation was added to `plaudsim`.

## Second post-bind exchange (R1b): getStorage

Selected over `syncTime` because it is the more defensible increment: fixed
request bytes, both-platform lifecycle, no opcode alias, no time dependence.
`syncTime` (m7→n7, opcode 4) was rejected for now: its request embeds
wall-clock + timezone (weaker determinism) and opcode 4 is shared with `z7`.

```text
refreshDeviceInfo()              # both templates post-bind
  -> PlaudDeviceAgent.getStorage()
  -> t3.F(...) -> q.F(...)
  -> a3.enPkg() = 01 06 00       # type 1, opcode 6, empty payload (bytecode-verified)
  -> BluetoothGatt.writeCharacteristic(2BB1)
  -> notification on 2BB0 with opcode 6
  -> n6(byte[]) parser           # real name StorageRsp
  -> process_item_data(n6) -> listener.bleStorage(total, free, duration)
```

| Element | Value | Provenance |
|---|---|---|
| Request bytes | `01 06 00` | SDK-derived: a3 type 1 / opcode 6 / no-arg init, inherits `l.enPkg` |
| Response layout | u64 free@3, u64 total@11, u64 duration@19 (len ≥ 27) | SDK-derived: `n6<init>` bytecode; unconditional prefix is 19 bytes |
| Field names | free/total/duration, class StorageRsp | SDK-derived: `n6.toString` literal |
| Consumer arg order | `bleStorage(total, free, duration)` | SDK-derived: verified through shuffled getters (`c()->b`, `d()->c`, `b()->d`) against both templates' signatures |
| Opcode-6 alias | none — n6 is the sole `a()==6` class of 240 | SDK-derived: bytecode scan |
| free/total units | bytes (strongly inferred: `storageUsed = total - free`) | template behavior |
| duration unit | UNKNOWN (both templates ignore it) | explicit gap |
| Field values | emulator-controlled (8 GiB / 16 GiB / 36000) | NOT hardware-observed |
| >20 B transfer | UNKNOWN for hardware; the virtual test performs a real ATT MTU exchange as harness-only transport setup | explicit gap |

The exact fixture is [post-bind-get-storage.json](fixtures/post-bind-get-storage.json).
R1b reuses the frozen R1a peripheral dispatch (extended with the `01 06 00`
branch only), discovery, CCCD handling, dual-mode `_respond`, packet logging,
and fixture-driven asserts; both notify and indicate paths are tested.

### R1a implementation status (2026-09-21, findings closed)

Implemented and tested over a Bumble virtual link, no hardware, no official
SDK client:

- Peripheral: `emulator/plaudsim/profile.py` (`PlaudR1aPeripheral`) exposes the
  recovered service/characteristics plus battery service, parses `01 03 00`
  exactly, and emits the structural opcode-3 response with deterministic
  emulator-controlled values from the fixture's `emulator_deterministic`
  block. Property masks (`NOTIFY|INDICATE` on `2BB0`, `WRITE` on `2BB1`) are
  virtual-test choices, documented in code, not hardware claims.
- Subscription modes: the peripheral answers through whichever mode the
  central selected (CCCD `0x01` → notify, `0x02` → indicate), verified by
  bearer-identity check. Both paths are tested over the real ATT/LocalLink
  path. This is emulator compatibility behavior; it does not claim the
  physical device exposes both modes.
- UUID independence: the test loads service/characteristic UUIDs, request
  bytes, and deterministic values from the fixture, and asserts discovered
  characteristic properties (2BB0 NOTIFY+INDICATE without WRITE, 2BB1 WRITE
  without NOTIFY/INDICATE). A 2BB0/2BB1 swap in the emulator fails the test.
- Offset 8 resolved as SDK-derived: `z2<init>` bytecode parses `d=u8@8` and
  `e=(u8@8)==1` when length >= 9, and `toString` gives the real class name
  `GetStateRsp` with `keyState<-e`. The fixture's `key_state` is therefore
  bool-valued; the deterministic value is 1 so a real parse reports
  keyState=1.
- Minimum length reconciled: 8 = unconditional prefix (header + u32 + u8);
  9 was the first-guard heuristic (offset 8 needs length >= 9). Full guard
  chain is recorded in the fixture (`z2_guard_chain`) and `protocol.json`.
- Opcode 3 is shared with `o3` (HeartbeatPing); `z2` is selected by the
  outstanding-request callback in `q.f`, not by opcode alone.
- Status: virtual-link validated. NOT hardware validated. NOT SDK integration
  validated.

## Frame format

The current extraction supports this model:

```text
protocol type 1 request: [u8 protocol_type=1][u16le opcode][payload]
protocol type 2:         [u8 protocol_type=2][u32le 0xffffffff][payload]
protocol type 3:         [u8 protocol_type=3][u32le 0xffffffff][payload]
protocol type 5:         [u8 protocol_type=5][u32le 0xffffffff][payload]
```

| Field / behavior | Confidence | Evidence / limitation |
|---|---|---|
| Little-endian integer encoding | `CONFIRMED` | `TntBleCommUtils` helper usage and extraction output. |
| Type-1 header width is 3 bytes | `STRONGLY INFERRED` | Request base-class extraction and response field offsets beginning at byte 3. |
| Type-2/3/5 sentinel is u32 `0xffffffff` | `STRONGLY INFERRED` | Recovered header constants. |
| Universal length field | `UNKNOWN` | No extracted field is identified as a universal length; response minimum-length guards are message-specific. |
| Universal sequence field | `UNKNOWN` | `globalSendSeq` and `globalReceiveSeq` are public internal agent properties, but their wire placement is not exposed. |
| Response protocol type | `UNKNOWN` | Existing response records recover opcode/layout but do not prove a single response header for all types. |
| BLE fragmentation/reassembly | `UNKNOWN` | SDK exposes data callbacks and transfer offsets, but no checked-out source gives the BLE chunk envelope. |

## Integrity and checksum

The Android native library exports `tnt_get_crc`, `tnt_get_file_crc`, and JNI
wrappers `tntGetCrc`/`tntGetFileCrc`. The iOS public interface exposes
`getCrc(path:)` and `checkCrc(crc:ofFile:)`. This confirms checksum capability,
but not a wire-level checksum on every control frame.

| Question | Current result |
|---|---|
| Algorithm family | CRC-16/CCITT-FALSE: `CONFIRMED`. |
| Polynomial | `0x1021`: `CONFIRMED`. |
| Initial value | `0xFFFF`: `CONFIRMED`; callers pass `65535`. |
| Reflection | None: `CONFIRMED`; the disassembled nibble recurrence is the non-reflected CCITT form. |
| Xorout | `0x0000`: `CONFIRMED`. |
| Byte order | Integer helpers serialize little-endian; CRC wire serialization is not shown in the helper. |
| Control-frame coverage | `UNKNOWN`; no control-frame caller appends or verifies this CRC. |
| File-transfer coverage | `CONFIRMED` for whole-file byte iteration in `tnt_get_file_crc`; transfer placement remains unknown. |

## Authentication and pairing

Authentication is not a simple unauthenticated GATT connect:

1. Android initializes the partner API and waits for generated RSA material.
2. Android signs and stores the device serial number (`sn-sign`) before calling
   the BLE facade.
3. The BLE agent performs `preHandshake`, `sendRSAPublic`, `firstHandshake`, and
   `twoHandshake` stages.
4. The agent obtains the serial number and emits `bleBind(sn, status, protVersion,
   timezone)`.
5. Only status 0 is accepted as success.

The exact RSA payloads, key size used by this product, signatures, token encoding,
and whether encryption covers all subsequent frames are `UNKNOWN`. The iOS SDK
publicly exposes RSA encryption/signature types and mutable secret/chacha fields,
but those declarations do not reveal the wire messages.

## State, transfer, errors, and retries

The minimum client-visible state machine is:

```text
DISCONNECTED
  -> SCANNING
  -> GATT_CONNECTED
  -> NOTIFICATIONS_READY
  -> HANDSHAKING
  -> BOUND(status=0)
  -> READY
  -> STATE_QUERIED
  -> FILE_LIST_REQUESTED
  -> FILE_LIST_RECEIVED
  -> TRANSFERRING
  -> COMPLETE
```

Failure transitions from any connected pre-ready state are disconnect, connect
failure, handshake timeout, or bind status != 0. The templates then surface a
failure or schedule reconnect. Android uses a 30-second connect timeout and a
60-second handshake timeout; recovery has additional bounded waits. These are
application policy, not proven device protocol timers.

The SDK exposes `repeatCommondInterval`, `bleHandshakeWait`, transfer stop APIs,
and reconnect logic. The templates prove retries/reconnect at the application
boundary, but do not expose a packet ACK format, retry counter, idempotency key,
or error envelope beyond `UniversalErrRsp` and status callbacks. Those remain
`UNKNOWN`.

BLE file transfer is initiated by `getFileList` and `syncFile`; callbacks expose
file-list entries, head/tail status, session ID, start offset, data, completion,
and CRC. Wi-Fi fast transfer is a separate path: the device opens a hotspot,
BLE may drop, the phone joins Wi-Fi, a WebSocket handshake completes, and the
client explicitly requests the Wi-Fi file list. Exact Wi-Fi frames are outside
the first BLE milestone.

## Emulator readiness boundary

The first software-only milestone should be **R1a: a deterministic GATT and
post-bind state probe**:

1. Advertise the recovered service UUID and both custom characteristics.
2. Accept a central connection and service/characteristic discovery.
3. Allow the central to subscribe to the presumed data-notify characteristic and
   battery notification characteristic.
4. Record every write and notification with raw bytes.
5. Accept the recovered request `01 03 00` on `2BB1`.
6. Emit a structural opcode-3 response on `2BB0` using deterministic fixture values.
7. Assert the raw request and response header against
   `docs/fixtures/post-bind-get-state.json`.
8. Inject one controlled malformed response or dropped response and assert the
   client-facing timeout/error path.

Required emulator state is `ADVERTISING`, `CONNECTED`, `NOTIFICATIONS_READY`,
`HANDSHAKING`, `BOUND`, `STATE_QUERY`, and `DISCONNECTED`. The response
characteristic must support `NOTIFY` or `INDICATE`; the command characteristic
must support a `BluetoothGatt.writeCharacteristic`-compatible write mode, but no
single fixed write property is justified yet.

R1a should reuse `reference/upstream/bumble/bumble/gatt.py` (`Service`,
`Characteristic`, `Descriptor`), `gatt_server.py` notification/subscription
handling, `device.py` device lifecycle, and `link.py:LocalLink` for an in-process
client/server test. It should not copy or rewrite Bumble infrastructure.

## Evidence gaps blocking a full emulator

- No fixed runtime characteristic property bitmask; the SDK dynamically chooses notification versus indication from discovered properties.
- No complete post-handshake recording-transfer exchange.
- No captured authentication bytes from physical hardware.
- CRC control-frame coverage and on-wire byte order remain unknown.
- No universal length/sequence/chunk envelope.
- No packet-level ACK/retry/error rules.
- No hardware or official SDK integration run in this environment.