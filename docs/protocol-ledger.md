# Plaud / TinnoTech BLE protocol ledger

**This file is the single authoritative record of what we believe about the wire
protocol and why.** Where it disagrees with `docs/protocol.json`,
`docs/protocol-spec.md`, `PROJECT.md` or any code comment, this file wins and
the other should be corrected.

Last re-derived: **2026-09-22**, from scratch, against a fresh decompile.

## Evidence base

| Input | Identity |
|---|---|
| `reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar` | sha256 `041a6f88…6aedce` |
| └ `classes.jar` | sha256 `27795d1f…f93356` |
| iOS `.swiftinterface` × 3 | `PlaudBleSDK`, `PlaudWiFiSDK`, `PlaudDeviceBasicSDK` |
| Template app source | `ios/PlaudTemplateApp/`, `android/app/src/main/java/com/plaud/template/` |

Derived, reproducible with `./scripts/build-evidence.sh` (writes only to
`build/`, never to `reference/`):

* `build/evidence/javap/` — `javap -p -c -constants` for all 674 classes.
  **This is ground truth.** 131 922 lines.
* `build/evidence/jadx-out/` — jadx decompilation, 493 files. A decompiler's
  *interpretation*; it is wrong in specific, documented ways (see
  "jadx traps" below). **When jadx and javap disagree, javap wins.**
* `docs/evidence-digest.json` — protocol facts extracted *mechanically* from
  the javap output by `scripts/extract_evidence_digest.py`, with no human in
  the path. `tests/test_evidence_conformance.py` asserts the emulator against
  this file; that is what makes those assertions non-circular.

## Confidence vocabulary

| Tag | Meaning |
|---|---|
| **DIRECT** | A literal constant or an explicit code path in the shipped artifact. |
| **SOURCE-DERIVED** | Follows necessarily from reading the shipped code. |
| **INFERRED** | A reasonable reading the code does not force. |
| **POLICY** | A harness decision. Not a claim about the device. |
| **UNKNOWN** | Not recoverable from the available evidence. |

A harness decision must never be written down as recovered behaviour. Every
POLICY row below names the choice and what would settle it.

**R7 closure mapping.** The final documents (`docs/final-*.md`,
`docs/final-uncertainty-matrix.json`) use a finer vocabulary that this ledger's
tags map onto without collapsing: DIRECT → **BYTECODE_PROVEN** (from `javap`) or
**SDK_PROVEN** (from shipped template source / swiftinterface); SOURCE-DERIVED →
**SDK_PROVEN**; POLICY → **HARNESS_POLICY**; INFERRED and UNKNOWN keep their
names. Three classes are new and stronger than anything in this file before
R7: **RUNTIME_PROVEN** (observed by executing the official, unmodified SDK
binary), **EMULATOR_INTEGRATION_PROVEN** (the official SDK and this emulator
completed the exchange end-to-end), and **CLOUD_OBSERVED** (a cloud fact
recorded from strings/documentation/community clients, never exercised here).
**DEVICE_OBSERVED** is used nowhere: no real Plaud hardware was ever involved.

---

## 1. Transport

### 1.1 GATT profile — DIRECT

`com/plaud/sdk/proto/w$d`:

| UUID | Role |
|---|---|
| `00001910-0000-1000-8000-00805f9b34fb` | Plaud/Tinno primary service |
| `00002BB0-…` | device → host. Notify **or** indicate; the client writes whichever the discovered properties allow. |
| `00002BB1-…` | host → device. Write. |
| `00002902-…` | CCCD (standard) |
| `0000180F-…` / `00002A19-…` | Battery Service / Battery Level (standard) |

### 1.2 MTU gates the connection — DIRECT

This is not an optimisation and not harness convenience:

* `z$c.onConnectionStateChange`, `STATE_CONNECTED` branch: `if (this.f(this.m)) return; this.b("STATE_CONNECTED");`
  — `z.f(int)` is `BluetoothGatt.requestMtu`, and `z.m` is initialised to **255**.
  A `requestMtu` that returns false **disconnects**.
* `BluetoothGatt.discoverServices()` has **exactly one call site in the whole
  artifact** (`ALL.txt:74601`, reached only through the synthetic accessor
  `z.j(z)`, whose sole caller is `onMtuChanged` at offset 149).

So: **a peripheral that does not complete an ATT MTU exchange never gets its
services discovered by the real Android client.** On a reconnect the SDK
requests the *previously negotiated* value, because `onMtuChanged` writes it
back into `z.m`.

### 1.3 No application-layer reassembly — SOURCE-DERIVED

Every notification on 2BB0 is parsed as one complete logical message at literal
offsets. There is no accumulator, no length prefix and no continuation flag on
the control path. Consequences:

* A control response must arrive in a single notification.
* `x2`/GetSsnRsp reads through byte 62, so it needs a 63-byte notification and
  therefore ATT_MTU ≥ 66. With the 23-byte default the SDK cannot receive its
  own serial-number response.
* `n6`/StorageRsp is 27 bytes, also over the 20-byte default payload.

Bulk file data is *not* reassembled either — it is a stream of independently
addressed offset/length frames (§5.3).

### 1.4 Codec and endianness — DIRECT

`com/tinnotech/penblesdk/utils/TntBleCommUtils`, a JNI shim over
`libtnt_ble_utils.so`:

```
readInt(widthBits, buf, off)      a=u8  b=u16  c=u24  d=u32  e=u64
packInt(widthBits, buf, off, v)   same letters; returns the NEXT offset
a(long) -> byte[4]                pure Java, explicit little-endian shifts
c(int)->byte[1]  a(int)->byte[2]  b(int)->byte[3]   prefixes of a(long)
```

`a(long)` is the endianness proof: it is ordinary Java and writes
`v&0xFF, v>>8, v>>16, v>>24`. The three allocators copy a prefix of it, so
they are little-endian too. **The wire format is little-endian.**

> **jadx trap.** jadx renders `a(int)`, `b(int)`, `c(int)` as infinitely
> self-recursive. The bytecode is `iload_1; i2l; invokevirtual a:(J)[B` — a
> call to the *long* overload. This is a jadx overload-resolution bug, and it
> is what hides the endianness proof from a casual read.

### 1.5 Frame headers — DIRECT

`l.packHead()` (request base class):

| protocolType | head |
|---|---|
| 1 | `[u8 1][u16le opcode]` — 3 bytes |
| 2, 3, 5 | `[u8 type][u32le 0xFFFFFFFF]` — 5 bytes |
| 4 | **empty `byte[0]`**; subclasses check `packHead.length <= 0` and bail |

`n.<init>` (response base class) does exactly one thing:

```java
if (TntBleCommUtils.a().b(bArr, 1) != a()) throw new Exception(name + " Mismatch");
```

— it validates the **u16le at offset 1** and **never inspects byte 0**. The
protocol-type byte is checked one layer up, by the notification demultiplexer.
The emulator's parsers are deliberately *stricter* (they check byte 0 too);
that is a harness choice, not a mirror of the SDK.

Protocol types seen on 2BB0: **1** control, **2** file data, **4** BLE rate
test, **5** log package.

---

## 2. Two framings, not one

The largest single error in the pre-audit reconstruction was treating
pre-handshake marker frames as ordinary type-1 control frames.

| | Marker frames | Control frames |
|---|---|---|
| Built by | `v4.enPkg()`, `w4.enPkg()` — **override enPkg, never call packHead** | `l.packHead()` + payload |
| Byte 0 | low byte of the u16le marker | protocol type (1) |
| Discriminator read at | offset **0** (`b(value2, 0)`) | offset **1** (`n.<init>`) |
| Layout | `[u16le marker][u8 count][u8 index][chunk ≤100]` | `[u8 1][u16le opcode][payload]` |

Three independent confirmations that marker frames carry **no** protocol-type
prefix:

1. `v4.enPkg` / `w4.enPkg` bytecode: four merged arrays, the first from
   `a(getBleRequestType())` (the u16 allocator). `packHead` never appears.
2. `z$c.onCharacteristicChanged` pre-handshake arm reads `b(value2, 0)`.
3. `q.n`'s response lambda tests `b(bArr2, 0) == 65041`.

Corroborating detail: `z.c(byte[])` (the outbound command logger) and
`z.b(byte[])` (the send-pacing exemption) both bail unless `bArr[0] == 1`, so
marker frames produce no command log and *do* incur the send pacing delay.

### Marker constants — DIRECT

| Marker | Table | Direction | Meaning |
|---|---|---|---|
| `0xFE10` | `w$c.a` | host → device | pre-handshake, normal |
| `0xFE20` | `w$c.b` | host → device | pre-handshake **and clear** (`PlaudDeviceAgent` names it `PRE_HANDSHAKE_AND_CLEAR`) |
| `0xFE12` | `w$c.c` / `w$a.b` | **both** | up: RSA public key chunks. down: the encrypted secret package. |
| `0xFE11` | `w$a.a` | device → host | "send me your RSA public key" |

On the wire `0xFE10` is `10 FE`. A frame written big-endian is never recognised.

**Field order is count-then-index**, which is the reverse of the constructor
parameter order: `new v4(i /*index*/, length /*count*/, chunk, forceClear)` but
`enPkg` emits `c(this.b)` (count) before `c(this.a)` (index). The receive side
agrees: `z.I = value2[2]` is the count, `z.H = value2[3]` the index.

Refinements from the adversarial pass, worth stating because they change how
the fields should be *described* even though the bytes are unchanged:

* Bytes 0–1 are **one computed u16le field** (`a(getBleRequestType())`, the u16
  allocator), not two byte writes of a fixed marker. `v4` picks `0xFE20` vs
  `0xFE10` at runtime from its boolean.
* `v4` *declares* `getBleProtocolType() == 1`, but that method is invoked only
  from `l.packHead`, which `v4.enPkg` never calls — so the declared type never
  reaches the wire.
* `v4.enPkg` has **no guard chain at all**: no null check, no length check, no
  range check. `c(int)` truncates the count and index to their low 8 bits
  silently. The 1..100 chunk bound and the 0-based ascending index are
  properties of the **call site** `q.n`, not of `v4`.
* Both markers ship. `PlaudDeviceAgent.connectBleDevice` passes
  `isForceClear = false` (→ `0xFE10`); `recoveryConnectBleDevice` passes
  `true` (→ `0xFE20`).

Reassembly (`z$c`): drop a frame byte-identical to one already held, sort by
`bArr[3] & 255`, concatenate `frame[4:]` once `held.size() == count`.

> Signedness inconsistency, DIRECT: the count at `[2]` is loaded with `baload`
> into an int (**signed**), while the sort comparator masks the index at `[3]`
> with `& 255` (**unsigned**). A device announcing a count ≥ 128 makes
> `size() == count` unsatisfiable and wedges the handshake.

---

## 3. Which opcode table is which

`w$a` and `w$c` are two ~70-entry integer tables that overlap heavily, so the
overlap says nothing. The opcodes **unique** to one table settle it:

| | unique opcodes | claimed by a request class | claimed by a response class |
|---|---|---|---|
| `w$a` | 13 | 1 | **11** |
| `w$c` | 11 | **10** | 1 |

**`w$c` is the host→device (request) table; `w$a` is the device→host
(response/notify) table.** The marker constants agree: both host-sent markers
(`0xFE10`, `0xFE20`) are `w$c`-only and the device-sent `0xFE11` is `w$a`-only.
Asserted mechanically by
`test_w_c_is_the_request_table_and_w_a_the_response_table`.

The alphabetical order invites the opposite guess, and one recovery pass in
this audit did guess the opposite. Known exception: opcode 110 is `w$a`-only
yet is claimed by the request class `t4`.

**Request and response opcodes are NOT always equal.** The familiar pairs
(3/3, 4/4, 6/6, 9/9, 22/22, 26/26, 28/28) invite a false rule; counter-examples
include request 29 (`z6`) → response 30 (`a7`), request 24 (`k2`) → response 33
(`l2`), request 25 (`v5`) → response 34 (`w5`).

---

## 4. Connection lifecycle and the crypto boundary

### 4.1 Stage names — DIRECT

`com/plaud/sdk/proto/q` constants `K`…`W`, and the same ASCII strings appear in
the iOS Mach-O:

```
start → gatt_connect → set_notify → [set_battery_notify → read_battery] →
set_data_notify → [pre_handshake → send_rsa_public] →
first_handshake → two_handshake → handshake_get_ssn →
change_handshake_timeout → sync_time
```

Two branch points, both on the **advertised** portVersion:

| Threshold | Evidence | Effect |
|---|---|---|
| `portVersion >= 5` | `q` returns `bleDevice.getPortVersion() >= 5` | "new battery service": the standard `0x180F`/`0x2A19` subscribe+read is **skipped entirely**; battery comes from opcode 9. |
| `portVersion >= 20` | `z$c.onCharacteristicChanged`, `z.d(byte[])`, `q.b0` | the RSA pre-handshake runs, and afterwards the whole channel is encrypted. |

### 4.2 portVersion comes from the advertisement — SOURCE-DERIVED

`u4.a(ScanResult)` parses manufacturer-specific data and builds
`new BleDevice(name, mac, rssi, manufacturerCode, projectCode, versionType,
versionCode, serialNumber, bindInfo, portVersion)`. `z` then reads
`l().getPortVersion()` on **every** inbound notification and on **every** send.
The field is `final` and is never refreshed from the handshake response.

**A device declares, before a central ever connects, whether its link is
encrypted.** That is what lets a cleartext emulator be faithful rather than
merely convenient — provided it advertises a portVersion below 20.

### 4.3 The encrypted envelope (portVersion ≥ 20) — DIRECT

Algorithm literals from `q5` ("SecretUtil"): `RSA/ECB/PKCS1Padding`,
`SHA256withRSA`, `ChaCha20-Poly1305` (key alg `ChaCha20`, 12-byte
`IvParameterSpec`, AAD via `updateAAD`), with `AES/GCM/NoPadding` +
`GCMParameterSpec(128, iv)` as an alternate under the same key material —
**selected only on the Wi-Fi path.** The AES selector `z.O` is readable only
through `z.w()`, and every call site of `z.w()` is on the Wi-Fi transfer path;
it is driven by bit 3 of the opcode-138 capability bitmap (§5.13). The BLE
sealed path calls the four-argument `q5.d`/`q5.b` helpers, which take no
algorithm parameter, so **BLE is always ChaCha20-Poly1305** (R7,
`tests/test_r7_transport_scope.py`, BYTECODE_PROVEN).
Conscrypt is installed if the platform lacks ChaCha20-Poly1305. Private keys
are parsed as **PKCS#8 PEM**, base64 `NO_WRAP`.

Pre-handshake, in order:

1. Host chunks `Base64.decode(snSignature, NO_WRAP)` — the *cloud-issued*
   signature over the device SN — into 100-byte pieces and sends them as `v4`
   marker frames (`0xFE10`, or `0xFE20` when `isForceClear`). Waiters:
   `{0xFE11, 0xFE12}`.
2. On the last chunk's reply: if `u16le@0 == 0xFE11`, host sends its **RSA
   public key as PEM TEXT** (not DER) in 100-byte `w4` chunks under `0xFE12`.
   Otherwise it skips straight to the handshake.
3. Device replies with `0xFE12` chunks. Reassembled → RSA-decrypted with
   `userRSAPrivateKey` → plaintext `P`, which must be ≥ 56 bytes:

   ```
   P[0:32]   ChaCha20 key          -> z.J
   P[32:44]  12-byte nonce         -> z.K
   P[44:56]  12-byte associated data -> z.L    (AAD, NOT a second nonce)
   P[56:]    ChaCha20-Poly1305 ciphertext whose plaintext is ASCII "PLAUD.AI"
   ```

   `"PLAUD.AI"` is `PlaudEncryptHeader.MAGIC_STRING`. Since the sealed magic is
   8 bytes plus a 16-byte Poly1305 tag, `P` is **80 bytes** in practice, which
   fits one RSA-2048 block (245-byte PKCS#1 v1.5 ceiling) → one 256-byte
   ciphertext → 3 chunks of 100/100/56.

Afterwards, every frame in both directions:

```
wire = ChaCha20Poly1305_Seal(key=J, nonce=K, aad=L,
                             plaintext = [u32le seq][ordinary frame])
```

* Outbound (`z.d`): `z.M` is pre-incremented, so the host's first frame after
  `z.y()` (which resets `M = 1`, `N = -1`) carries **seq 2**.
* Inbound (`z$c`): `seq = (int) u32le@0`; `if (N >= seq) return;` then `N = seq`.
  The first accepted device sequence may be 0.

Observations that an interoperating emulator must reproduce, not repair:

* **The same key AND the same nonce are used for every frame in both
  directions.** Keystream is reused across the whole session. Do not derive a
  per-frame nonce.
* The receive counter is narrowed with `l2i`; a device counter ≥ `0x80000000`
  goes negative and every later frame is dropped forever.
* `z.M` / `z.N` are **static** on class `z` — process-global, shared across
  devices, reset only by `z.y()` immediately before the pre-handshake.
* An AEAD authentication failure is not a dropped frame: `z$c` catches and
  rethrows `new RuntimeException(e)` out of `onCharacteristicChanged`.

### 4.4 What "bind" means — SOURCE-DERIVED

There is **no BLE bind message**. Android fires `bleBind` from
`btStatusChange(CONNECTED)`, which fires from the *sync_time* success callback.
Connection success is gated on three separate device replies: handshake status
0, the opcode-9 battery exchange (when portVersion ≥ 5), and syncTime — each
retried a few times, with `SYNC_TIME_FAIL (-8)` if the last never lands.

The unrelated cloud bind (`POST /api/devices/bind`) is never called by the SDK.

### 4.5 Credential provenance — DIRECT, and decisive

| Credential | Origin |
|---|---|
| RSA key pair | **Plaud's cloud.** `POST /developer/api/open/partner/sdk/gen-key` returns `public_key` **and** `private_key` as PEM. The SDK never calls `KeyPairGenerator` for this. |
| SN signature | **Plaud's cloud.** `POST …/sdk/sn-sign`, returned as opaque base64. |
| Handshake token | Account/partner issued, 32 hex characters (`PlaudDeviceAgent` warns when the length differs and notes that "the lower layer pads with zeros / truncates" — which is exactly `k3`'s padding rule). |

**No offline bind is possible** for the direction that needs a credential,
and this repository contains none.

### 4.6 The asymmetry that decides what is reachable — SOURCE-DERIVED

`q.b0()` branches on the advertised portVersion: `>= 20` runs the RSA
pre-handshake (`q.n()`), below it goes **straight to `q.a()`**, first_handshake.
And `q.a()`'s only precondition is `!TextUtils.isEmpty(this.g)` — the token
string the caller passed to `connectBleDevice`. No network call, no key, no
signature, no cryptography of any kind.

On the legacy path the handshake is therefore: **the host sends a token, and the
DEVICE returns a status.** The secret belongs to the device.

That cuts the problem in two, and earlier work had collapsed both halves into a
single "blocked":

| Direction | Reachable? | Why |
|---|---|---|
| a client **we** wrote → **real Plaud hardware** | **No** | needs a token, RSA key pair and SN signature that only Plaud's cloud issues |
| **Plaud's real client** → a device **we** wrote | **Yes**, on the legacy path | accepting the token is the device's decision, and we are the device |

The emulator implements the device side (`handshake.encode_l3`,
`handshake.encode_x2`, `profile.PlaudPeripheral._handshake`). Accepting any
token is **HARNESS POLICY**, labelled as such, and says nothing about what real
hardware accepts; `accept_any_token=False` and a non-zero `handshake_status`
drive the refusal paths.

`tests/test_v1_discovery_session.py::test_full_sdk_connect_sequence_reaches_bound_and_pulls_a_file`
walks the SDK's own stage order end to end and pulls a complete file.

---

## 5. Message ledger

Layouts are given as absolute byte offsets. All multi-byte fields are
little-endian unless stated.

### 5.1 getState — opcode 3

```
message:        getState
request class:  y2          response class: z2 (real name GetStateRsp)
direction:      host->device / device->host
service/char:   1910 / write 2BB1, notify 2BB0
protocol type:  1
request:        [0]=01 [1..2]=03 00                                  3 bytes
response:       [0]=01 [1..2]=03 00
                [3..6]   u32le state
                [7]      u8   privacyEnable   (== 1)
                [8]      u8   statusCode; keyState = (statusCode == 1)
                [9]      u8   usbState        (== 1)
                [10]     u8   scene
                [11..14] u32le sessionId
                [15]     u8   findMyState
                [16]     u8   — parsed, unnamed in toString, no consumer found
                [17]     u8   — parsed, unnamed in toString, no consumer found
guards:         len>=9 -> [8]; >=10 -> [9]; >=15 -> [10],[11]; >=16 -> [15];
                >=17 -> [16]; >=18 -> [17].  [3] and [7] are UNGUARDED.
min length:     8 (the unguarded prefix). Full frame 18.
names:          z2.toString "GetStateRsp{state, privacyEnable, keyState, usbState, findMyState, scene, sessionId}"
confidence:     DIRECT (layout, names)
implemented:    yes — PlaudDeviceState
tests:          test_r1a_get_state.py, test_get_state_response_lands_on_z2_parse_offsets
```

**The enum called "state" is not driven by the field called "state".**
`z2.e()` calls `s0$g.a(statusCode /*u8@8*/, state /*u32@3*/)`, which returns
`RECORDING` iff the *state word* equals **4099 (0x1003)** and otherwise
switches on the *statusCode byte* (0 IDLE, 1 RECORD, 2 TRANSFER, 3 SWITCH_ON,
4 SWITCH_OFF). Both templates check 4099. — SOURCE-DERIVED.

**Opcode 3 is not a global message identifier, but it is unambiguous here.**
Two classes return 3 from `a()`, but `o3`/HeartbeatPing belongs to the
WebSocket PDU family (`m` base class, opcode at u16le@4 behind a different
header) and is only built inside `w7`. On GATT, protocol type 1 opcode 3 is
always GetStateRsp.

**There is no opcode→class table anywhere in the SDK.** Dispatch hands a raw
`byte[]` to a callback registered with an `int[]` of expected response opcodes;
the callback's own body chooses the class. `n.<init>`'s opcode check is a
post-hoc assertion inside a class the caller already picked.

### 5.2 syncTime — opcode 4

```
message:        syncTime
request class:  m7          response class: n7 (real name TimeSyncRsp)
request:        [0]=01 [1..2]=04 00
                [3..6] u32le  unix seconds (currentTimeMillis()/1000, TRUNCATED
                              to 32 bits -> wraps in 2038)
                [7]    i8     timezone hours     SIGNED
                [8]    i8     timezone minutes   SIGNED
                                                                     9 bytes
response:       [0]=01 [1..2]=04 00
                [3..6] u32le  stamp
                [7]    i8     timezone           SIGNED (i2b in the bytecode)
                [8]    u8     hasStatistics (== 1), guarded by len>=9
                                                                   8 or 9 bytes
confidence:     DIRECT
implemented:    yes — PlaudSyncTimeState
tests:          test_r1c_sync_time.py, test_timezone_fields_are_signed_int8
```

`m7.a(TimeZone,long)` computes `offset = getOffset(now)/60000` then
`(offset/60, offset%60)` with Java integer division, which truncates toward
zero and keeps the dividend's sign. **Both components go negative west of
UTC**: UTC−03:30 sends `FD E2` = (−3, −30), not (−4, +30) and not (3, 30).

**CORRECTION (2026-09-22).** The response field at offset 7 was previously
recorded as an unnamed `raw7`. `n7.toString` names it `timezone`
("TimeSyncRsp{stamp=%d, timezone=%d, hasStatistics=%s}"), and the consumer
confirms the role: `syncTime$1.onCallback` forwards `n7.c()` as the **first**
argument of `PlaudDeviceAgentListener.bleTimeSync(timezone, stamp)`.

`hasStatistics` has a **side effect**: when set, the SDK immediately starts a
statistics sync of its own accord. — SOURCE-DERIVED.

The opcode-4 "alias" is not real: `z7`/WifiCloseRsp also returns 4 from `a()`
but extends the WebSocket PDU base `m` and is only constructed from
`org.java_websocket`'s `onMessage`. It is unreachable from any BLE
notification.

### 5.3 getStorage — opcode 6

```
message:        getStorage
request class:  a3          response class: n6 (real name StorageRsp)
request:        [0]=01 [1..2]=06 00                                  3 bytes
response:       [0]=01 [1..2]=06 00
                [3..10]  u64le free
                [11..18] u64le total
                [19..26] u64le duration   (guarded by len >= 27)
                                                             19 or 27 bytes
confidence:     DIRECT (layout, names); units UNKNOWN
implemented:    yes — PlaudStorageState
tests:          test_r1b_get_storage.py, test_free_and_total_are_not_swapped
```

**The getter shuffle is real.** ProGuard renamed methods and fields in
independent namespaces: `n6.c()` returns the field at offset 3, `n6.d()` the
one at 11, `n6.b()` the one at 19. A reimplementer who matches getter letters
to field letters gets all three wrong. Resolved correctly, `n6.toString` and
both platforms' consumers agree: **offset 3 is `free`, offset 11 is `total`**.

The public API argument order is the **reverse** of the wire order for the
first two fields — `bleStorage(total, free, duration)` on Kotlin, Swift and
ObjC alike. This is the single easiest pair in the protocol to invert.

Units of `free`/`total` (bytes? sectors?) and of `duration` (seconds?) are
**UNKNOWN**: no formatter in either template app fixes them. A 19..26-byte
response leaves `duration` at the Java default 0, indistinguishable from a
device genuinely reporting 0 except by raw packet length.

### 5.4 battery / charging — opcode 9

```
message:        battStatus
request class:  o           response class: p (real name BattStatusRsp)
request:        [0]=01 [1..2]=09 00                                  3 bytes
response:       [0]=01 [1..2]=09 00
                [3] u8 charging (== 1)
                [4] u8 level                                         5 bytes
confidence:     DIRECT
implemented:    yes — PlaudBatteryState, plus push_battery() for the
                unsolicited form
tests:          test_battery_status_opcode_9_round_trip,
                test_battery_can_be_pushed_unsolicited
```

Load-bearing: connection success is gated on this exchange when portVersion ≥ 5
(§4.4). The device also **pushes** this frame unsolicited.

`getChargingState()` is the sharpest platform divergence found:
on **Android** it is a pure cached-field read that puts **nothing** on the
wire and returns stale zeros until an opcode-9 frame has arrived; on **iOS**
`BleAgent.getChargingState()` **writes `01 09 00`** and the answer arrives via
the delegate. The earlier "zero BLE packets" finding is true of Android only.

### 5.5 handshake — opcodes 1 and 2

```
message:        handshake #1 / #2
request class:  k3 (first), j3 extends k3 (second)
response class: l3 (opcode 1), x2 (opcode 2, real name GetSsnRsp)

k3.enPkg into a 255-byte buffer, then u4.b() trims the trailing zero run:
                [0]=01 [1..2]=01 00
                [3]    u8 constant 0x02        <- hard-coded, no reader found
                [4]    u8 z.h()                <- stubbed `return 0` in this build
                [5]    u8 stage (0 = k3, 1 = j3)  ONLY IF portVersion >= 3
                [6..]  ASCII token, right-padded with '0' (0x30) to
                       32 chars when portVersion >= 9, else 16; longer tokens
                       are truncated to the same width
                total: 22 bytes (pv 3..8) or 38 bytes (pv >= 9)
j3 appends:     [+0..+7] devToken bytes, zero-padded/truncated to 8
                [+8]     u8 len(userName)
                [+9..]   userName bytes
```

**`k3` is byte-exact.** Only the *values* of its arguments are unknown, and one
of them never reaches the wire at all: `portVersion` is a layout selector, not
a payload field. The pre-audit claim that k3 could not be built byte-exactly is
**withdrawn**; what cannot be produced is a *valid token*, which is an
account-issued credential, not a protocol gap. Driver literals confirm the
stage byte: `q.a` passes constant 0 as k3's second constructor argument while
`q.d0` passes constant 1 for j3 (both verified at the call site, not inferred
from field names).

`j3`'s null short-circuit needs **both** extra strings null. The shipped
wrappers pass empty *strings*, so the 8-byte field and the `00` length byte are
always appended in practice — modelling "empty means omitted" produces a frame
9 bytes short.

```
l3 response:    [0]=01 [1..2]=01 00
                [3]      u8    status (0 = success)
                [4..5]   u16le portVersion
                [6]      u8    timezone
                [7]      u8    timezoneMin      guarded len>=8
                [8]      u8    audioChannel     guarded len>=9
                [9]      u8    supportWifi(==1) guarded len>=10
                [10]     u8    noNsAgc(==1)     guarded len>=11
                [11]     u8    isOggAudio(==1)  guarded len>=12
                [len-4]  u8    versionType (as a char)
                [len-3..len-1] u24 LITTLE-ENDIAN version
min length:     7 (offsets 3,4,6 are unguarded)
```

**CORRECTION (2026-09-22): `version` is little-endian.** The bytecode is
`b[len-3] | (b[len-2]<<8) | (b[len-1]<<16)`. The pre-audit parser shifted the
other way, and every fixture agreed with it because the fixture was written
from the same mistake. `version` is rendered `"%04d"` next to `versionType` to
form `versionName`, so its real range is 0..9999.

Java defaults assigned **before** parsing, and therefore observed on a
truncated frame: `timezoneMin=0`, **`audioChannel=1`** (not 0),
`supportWifi=noNsAgc=isOggAudio=false`, `versionType="V"`, `version=0`. The
version fields are read from the frame **end**, so any padding or trimming of
the inbound buffer corrupts them. The nested `if (length >= 4)` is dead code.

```
x2 response:    [0]=01 [1..2]=02 00
                [3..58]  ASCII ssn, NUL-terminated; the scan is
                         `for (i=3; i<59; i++) if (b[i]==0) break;` and on
                         falling off the end it RESETS to 3 and substitutes the
                         literal 28 ASCII zeros
                [59]     u8  versionType character   (FIXED offset)
                [60..62] u24le versionCode           (FIXED offset)
                versionName = chr([59]) + "%04d" % u24le@60      e.g. "V0123"
full length:    63
```

**CORRECTION (2026-09-22): `versionName` is not "the bytes after the NUL".**
It is built from fixed offsets 59 and 60, independent of where the NUL was
found. Cross-confirmed by `BleDevice.getVersionName()`, which builds the
identical string from the *advertised* versionType and versionCode — which is
what makes the SDK's `checkSn` comparison meaningful.

The length guard is `> 59` while the u24 read needs 63 bytes, so a 60-, 61- or
62-byte frame reads out of bounds in native code without a Java exception.

Dispatch (`q.a(byte[])`): `if (u8@0 == 1) { switch (u16le@1) { 1 -> l3; 2 -> x2;
default -> HANDSHAKE_FAIL } }`. A frame whose byte 0 is not 1 is dropped in
complete silence. There is **no marker branch here** — markers are handled a
layer down, in `z$c`.

The first handshake registers waiters for **both** `{1, 2}`; the second
registers only `{1}` and builds `l3` unconditionally.

### 5.6 file list — opcode 26

```
message:        getFileList
request class:  p2 (GetRecSessionsReq)   response class: q2 (GetRecSessionsRsp)
request:        [0]=01 [1..2]=1A 00
                [3..6]  u32le requestStamp   (iOS API name: `uid`)
                [7..10] u32le startSessionId
                [11]    u8    flag           (iOS API name: `onlyOne`/`single`)
                                                                    12 bytes
response:       [0]=01 [1..2]=1A 00
                [3..6]  u32le requestStamp   — MUST echo the request
                [7..8]  u16le totals
                [9..10] u16le frameStartIndex
                [11..]  entries, stride from portVersion
```

**CORRECTION (2026-09-22): entries begin at offset 11, not 9.** `q2.a([B,I)`
guards `if (bArr.length < 11) return;`, initialises its cursor to `11`, and
computes the entry count as `(len - 11) / stride`.

**The u32 at offset 3 is not a recording session id.** `q` builds
`new p2(s5.e(), startSessionId, flag)` and `s5.a(long,int)` sets
`s5.a = System.currentTimeMillis() / 1000` at request time; `s5.a(byte[])`
then drops any frame whose `u32@3` differs. It is a **client-generated
correlation token**. The `startSessionId` the request asked for is stored in
`s5.b` and then never read — `s5.d()` has zero call sites.

Entry stride — DIRECT, a `tableswitch` with cases 1..6 and a default:

| portVersion | stride | layout |
|---|---|---|
| 1 | 8 | `[u32le sessionId][u32le fileSize]` |
| 2..6 | 9 | `… [u8 attribute @+8]` |
| anything else (incl. **0** and ≥ 7) | 10 | `… [u8 scene @+8][u8 attribute @+9]` |

Write it as `if pv == 1: 8 elif 2 <= pv <= 6: 9 else: 10` — **not**
`if pv >= 7`, because an un-handshaken session has `q.m == 0` and also takes
the default branch.

**CORRECTION (2026-09-22): scene is at +8 and attribute at +9, not the
reverse.** The 10-byte loop calls `new BleFile(d5, d6, a3, a2)` where `a2` is
the byte at `+8` and `a3` the byte at `+9`, and `BleFile`'s 4-argument
constructor is `(sessionId, fileSize, attribute, scene)`. `BleFile.isMusic()`
is `scene == 4`. Note that the 9-byte form leaves `scene` at **0**, not at
`BleFile`'s 3-argument default of 1.

Accumulation (`s5` + `q2`), all DIRECT:

* `s5.a(byte[])` drops a frame whose `requestStamp` mismatches — **entirely**.
* `q2.<init>` runs once, on the first accepted frame; `totals` is `final` and
  later frames' totals fields are never read.
* `q2.a` contributes nothing unless `u16le@9 == entries.size()`. This one gate
  is what makes duplicates and reordering harmless — and it is also why **a
  single lost frame stalls the stream forever**: every later frame has an index
  greater than the size and is discarded, with no gap-fill and no retransmit
  request.
* Completion is `q2.c() == q2.b().size()` — **equality**. Nothing caps
  accumulation at `totals`, so an overshoot is unrecoverable, and because
  opcode 26 is on the sticky-callback whitelist the registration is never
  removed and nothing times it out.
* The response carries **no per-frame entry count**; the count is
  `(len - 11) / stride` with no remainder check, so any suffix byte is misread
  as slack.
* `s5` is a process-wide **singleton shared with the Wi-Fi file list**, and
  `s5.a(long,int)` nulls both accumulators. A Wi-Fi getFileList started while a
  BLE one is streaming destroys the BLE accumulator.

The BLE file list carries **no duration, no timestamp, no filename and no
channel count**. Duration is computed client-side from `fileSize` by
`BleFile.calculateOpusDuration` / `calculateOggDuration`.

### 5.7 resume record — opcode 22

```
message:        resumeRecord
request class:  b5                       response class: c5 (RecordResumeRsp)
request:        [0]=01 [1..2]=16 00 [3..6] u32le sessionId [7] u8 scene
                8 bytes — but b5.enPkg emits the payload ONLY when
                sessionId >= 0; a negative id yields the bare 3-byte head
response:       [0]=01 [1..2]=16 00
                [3..6]   u32le sessionId
                [7..10]  u32le start
                [11]     u8    status
                [12]     u8    scene       guarded len>=17
                [13..16] u32le startTime   guarded len>=17
                                                            12 or 17 bytes
confidence:     DIRECT
implemented:    yes
```

`q` zeroes `b5`'s sessionId for portVersion < 7. `start` is a u32 alongside a
separate `startTime` u32, so it is probably a byte or frame offset rather than
a wall-clock value — **UNKNOWN**, nothing in the Android code consumes it.

Opcode 22 is **not** the file list. Inbound routing is by a per-request
registered callback list keyed on an `int[]` of expected response opcodes.

### 5.8 file transfer — opcodes 28/29 and protocol type 2

```
message:        syncFile
request class:  y6
request:        [0]=01 [1..2]=1C 00 [3..6] u32le sessionId
                [7..10] u32le start [11..14] u32le end             15 bytes
head (rsp 28):  s6 SyncFileHeadRsp [3..6] u32le sessionId [7] u8 status
                8 bytes.  status > 0 is a FAILURE: q$a calls `z.c(29)`
                before forwarding.  z.c(int) (com.plaud.sdk.proto.z,
                ALL.txt:76989) removes every pending response bean whose
                opcode list contains 29 and logs "removeResponseBean:"; it
                is also called on the TAIL (q$a.a 657-666) and on file-list
                completion (z.c(26)).  It is not a wire write.  (Until
                28 Sep this cited ALL.txt:50955-50960 as "a plain
                putfield"; that range is j6$a.c(int), another class.)
tail (rsp 29):  t6 SyncFileTailRsp [3..6] u32le sessionId [7..8] u16le crc
                9 bytes.
```

**The crc is never verified.** `t6` forwards it to the caller; no
`tntGetCrc`/`tntGetFileCrc` call site consumes it. Treat it as opaque.
RUNTIME (R7-S13, §15): 0 and 0xBEEF behave identically; Android surfaces the
field as `bleSyncFileTail(sessionId, status)`, iOS names it `crc` — same u16.

**What closes a transfer — RUNTIME_PROVEN (R7-S13, §15).** The TAIL does
NOT complete the client's sync. `q$a.a` reaches `v3.finish(code)` — which is
`bleDataComplete()` and `exportAudio`'s "download complete" — only from the
EMPTY_PACKAGE branch (ALL.txt:69024-69108). A device must therefore send

```
HEAD (28) · DATA … · EMPTY_PACKAGE [02][sid][FF FF FF FF][00][code] · TAIL (29)
```

with the sentinel *before* the TAIL: HEAD·DATA·TAIL alone left the genuine
SDK's request pending forever (runs 1, 2, 9), EMPTY after TAIL never
completed (run 7), codes 0 and 1 both completed (runs 4b, 8). The code value
real firmware sends is UNKNOWN (U15); the emulator sends 0 (HARNESS_POLICY,
`transfer.DEFAULT_EMPTY_PACKAGE_CODE`). The public raw `syncFile` session is
then closed by the app's `stopSyncFile()` (the op-queue cancels the pending
`[28,29]` bean with `-98`).

Bulk data is **protocol type 2** (`q$a.a` returns immediately unless the type
byte is 1 or 2), and its layout depends on portVersion:

```
portVersion >= 7: [0]=02 [1..4] u32le sessionId [5..8] u32le offset
                  [9] u8 length [10..] payload
portVersion  < 7: [0]=02 [1..4] u32le offset [5] u8 length [6..] payload
```

`q$a.a` sets `i = 1`, and only when `q.m >= 7` reads a session id at 1 and sets
`i = 5`; everything after is `offset = u32le@i`, `length = u8@(i+4)`,
`payload = [i+5 : i+5+length]` clamped to the frame end.

**EMPTY_PACKAGE**: when `offset == 0xFFFFFFFF` the SDK reads a code byte at
`i + 5` — **skipping `i + 4`**, the byte a normal frame uses for its length.
The two shapes are not a clean tagged union. Branches (jadx `q.java:257-282`,
javap ALL.txt:69024-69108): if no recovery is in flight (`!q.H`): return when
the cursor has not advanced past the request's start (`a == g`) or a resend
was already issued (`b`), else **`n.finish(code)`** — the completion. If a
recovery *is* in flight: `code == 1` is ignored, any other code triggers
`---isPacketLossStopSync---` and another `syncFileStart(cursor)`. Observed
live in both branches (R7-S13 runs 4b/8 and 6).

**Session gating** exists only on the modern branch; legacy frames carry no
session id and cannot be gated.

**Loss handling is real and automatic** — DIRECT, previously held open:

```java
if (d2 - this.a != 0) {                 // received offset != host cursor
    if (this.o.H) { a(); return; }      // recovery already in flight: arm a 5 s timeout
    if (!this.b) {
        this.b = true;
        log("start resend syncFileStart(sessionId:…,lastPosition:…)");
        this.o.k(null, … -> syncFileStart(sessionId, this.a /*cursor*/, end, true, …));
        this.o.H = true; a();
    }
}
```

The immediate reaction to a gap is **`stopSync` (29) on the wire, then, on
the `a7` ack, `syncFileStart` from the cursor**: `this.o.k(null, runnable)`
in the gap branch IS the stopSync write (`q.k`, §5.9) with the restart as its
completion (runnable `fileSyncLossPkgStop`, ALL.txt:69157-69182). RUNTIME
(R7-S13 runs 6b/6c/6d): `start resend syncFileStart(…,lastPosition:3200)` →
opcode 29 at +0 ms → `a7` → opcode 28 `start=3200` at +35 ms. The earlier
wording here ("stopSync is reached only through the 5 000 ms stall timeout")
was wrong: the gap branch (q$a.a 387-439) reaches *method* `q$a.f`
(ALL.txt:69396-69434), which calls `q.k` (stopSync) immediately; the 5 s
*stall* runnable is the lambda stored in *field* `q$a.f` (bootstrap #0,
method `q$a.b`), which never calls `q.k` and restarts directly through
`q$a.a(JJ…)` (review 2026-09-28; the two share a name). The client has gap
*detection* and a whole-stream *restart* primitive only — no selective/NAK
retransmit, no per-frame transport ACK (R5-S6). After the restart the client
accepts only frames whose offset equals its cursor; a device that keeps
draining the abandoned stream makes the old stream's EMPTY_PACKAGE fire the
"packet loss" branch and a second restart, and the SDK's export writer then
produced a corrupted file (run 6: fixture[0:4800] ‖ fixture[3200:]). A device
that abandons the old stream on the new `syncFileStart` converges byte-exact
with one restart (runs 6b/6d). Write errors on the restart are retried up to
3 times (`q.I`). Consequence for a device: deliver DATA in monotonic offset
order from the requested `start`, close with EMPTY_PACKAGE before TAIL, and
abandon the previous stream on a new `syncFileStart`/`stopSync`. Evidence
class: BYTECODE_PROVEN + RUNTIME_PROVEN (R7-S13).

### 5.9 stop sync — request opcode 29, response opcode 30

```
message:        stopSyncFile
request class:  z6                       response class: a7 (SyncRecFileStopRsp)
request:        [0]=01 [1..2]=1D 00      no payload                3 bytes
response:       [0]=01 [1..2]=1E 00      NO FIELDS AT ALL          3 bytes
                (a7.toString is literally "SyncRecFileStopRsp{}")
confidence:     DIRECT
implemented:    yes
```

**The response opcode is 30, not 29.** The SDK sends this at once on an
offset gap: the gap branch calls *method* `q$a.f`, which writes stopSync
(`q.k`) and re-issues `syncFile` from the cursor on the `a7` ack (§5.8;
RUNTIME_PROVEN in R7-S13, ~35 ms). The 5 000 ms stall runnable (*field*
`q$a.f`, method `q$a.b`) does NOT send stopSync; it restarts `syncFile`
directly. A peripheral that ignores the stop — or that echoes opcode 29 —
leaves the client waiting forever and its recovery path stalls. (Wording
corrected in R7 and again on 28 Sep, when an independent review read the
bytecode; BYTECODE_PROVEN.)

Note what this means for a dispatcher: **opcode 29 inbound is "stop syncing",
while opcode 29 outbound is the transfer TAIL.** Likewise opcode 30 inbound is
"delete file" and outbound is this stop acknowledgement. The request and
response opcode spaces are separate tables (§3).

### 5.10 delete file — request opcode 30, response opcode 31

```
message:        deleteFile
request class:  w6                       response class: x6 (SyncRecFileDelRsp)
request:        [0]=01 [1..2]=1E 00 [3..6] u32le sessionId         7 bytes
response:       portVersion >= 7: [0]=01 [1..2]=1F 00 [3..6] u32le sessionId [7] u8 status
                portVersion  < 7: [0]=01 [1..2]=1F 00 [3] u8 status
confidence:     DIRECT
implemented:    yes
```

`x6.<init>` takes the portVersion as its **second constructor argument** — a
third place in the protocol where the layout itself depends on it, after the
file-list entry stride (§5.6) and the file-data frame (§5.8).

### 5.11 BLE rate test — opcode 101, protocol type 4

**ATTRIBUTION CORRECTION (2026-09-22).** The type-4 parser with the puzzling
overlapping reads is the **throughput probe**, not file sync. Its only parser
is the notify lambda inside `q.a(int, c$b, v3)`, the handler for
`new x(i).enPkg()` on opcode 101; `PlaudDeviceAgent` exposes no rate-test entry
point and the default payload size is 80.

```java
long d2 = TntBleCommUtils.d(bArr2, 1);   // u32le @1
int  a2 = TntBleCommUtils.a(bArr2, 1);   // u8    @1   <- the SAME offset
… copy bArr2[2 : 2 + min(a2, len-2)]
```

Because the payload starts at 2, only the u8 reading is self-consistent; the
u32 read is almost certainly a copy-paste remnant of the file-sync handler,
where the two reads are at `i` and `i+4`. The emulator surfaces both values and
gives **neither** a protocol name. The pre-audit "type-4 byte[1] puzzle" was a
misattribution: there is no such puzzle in the file-transfer path.

---

### 5.12 common settings — opcode 8 (q0 → r0) — BYTECODE_PROVEN + RUNTIME_PROVEN

Discovered at runtime (R7-S12, §14): right after `getState` the official SDK
writes two opcode-8 frames the emulator had never seen. Bytecode resolves the
channel completely.

```
message:        CommonSettings (read / set one device setting)
request class:  q0                       response class: r0 ("CommonSettingsRsp")
request READ:   [0]=01 [1..2]=08 00 [3] u8 action=1 [4..5] u16le type        6 bytes
request SET:    [0]=01 [1..2]=08 00 [3] u8 action=2 [4..5] u16le type
                [6..9] u32le value                                          10 bytes
response:       [0]=01 [1..2]=08 00 [3..4] u16le type [5..8] u32le value     9 bytes
waiters:        {8, type + 1000}   (q.a(s0$b$a,int,long,long,…), ALL.txt:47514)
confidence:     layout DIRECT (q0.enPkg ALL.txt:59528+; r0.<init> ALL.txt:30994+)
                the two READs RUNTIME_PROVEN (r7/r7-s12-k3-capture-run1.json)
implemented:    yes — profile.py `_common_settings`; VALUES are POLICY (§12)
tests:          tests/test_r7_s12_common_settings.py (19, incl. javap pins)
```

`q0.enPkg` is `packHead + c(action)` (1 byte) `+ a(type)` (2 bytes LE), plus
`a(J)(value)` (4 bytes LE) on the 4-chunk SETTING path; the second long field
`d` is never emitted. `r0` reads `b(buf,3)` (16-bit) and `d(buf,5)` (32-bit).

**Wire values are the enums' int field, not their ordinal.** `s0$b$a`
(CommonAction): `READ = 1`, `SETTING = 2`. `s0$b$b` (CommonType): ProGuard
kept the 21 name strings; the wire value is the third constructor argument
(ALL.txt:22853-23260):

| ordinal | name (Android) | wire | Swift `CommonType` case (same ordinal) |
|---|---|---|---|
| 0 | BACK_LIGHT_TIME | 1 | LightDuration |
| 1 | BACK_LIGHT_BRIGHTNESS | 2 | LightBright |
| 2 | LANGUAGE | 3 | Language |
| 3 | AUTO_DELETE_RECORD_FILE | 4 | AutoClear |
| 4 | ENABLE_VAD | 15 | VAD |
| 5 | REC_SCENE | 16 | RecScene |
| 6 | REC_MODE | 17 | RecMode |
| 7 | VAD_SENSITIVITY | 18 | VadSensitivity |
| 8 | VPU_GAIN | 19 | VpuGain |
| 9 | BATTERY_MODE | 32 | BatteryMode |
| 10 | MIC_GAIN | 20 | MicGain |
| 11 | WIFI_CHANNEL | 21 | WiFiChannel |
| 12 | SWITCH_HANDLER_ID | 22 | SwitchHandle |
| 13 | AUTO_POWER_OFF | 23 | AutoPowerOff |
| 14 | SAVE_RAW_FILE | 24 | RawWaveEnabled |
| 15 | AUTO_RECORD | 25 | RecordingAfterDisConnet |
| 16 | AUTO_SYNC | 26 | SyncWhenIdle |
| 17 | FIND_MY | 27 | FindMyState |
| 18 | VPU_CLK | 30 | VPUCLK |
| 19 | AUTO_STOP_RECORD | 31 | StopRecordAfterCharging |
| 20 | IBEACON_WAKEUP | 49 | IBeaconWakeup |

The Swift and Android enums line up by **ordinal**, which is the project's
cross-reference technique working as designed; the naive reading "wire value =
Swift rawValue" would have mislabelled the two runtime reads (wire 15 and 17)
as AUTO_RECORD/FIND_MY. They are **ENABLE_VAD** and **REC_MODE**.

Consumer (`q.f(c$c, byte[])`, ALL.txt:41224+): `ENABLE_VAD → BleDevice.setVadOpen(v == 1)`;
`REC_MODE → setNcClose(v != 2)` (wire 2 = noise-cancelling); `VAD_SENSITIVITY`,
`VPU_GAIN`, `MIC_GAIN → int setters`. Dedicated wrappers exist per setting
(`q.X()` reads ENABLE_VAD; `q.f(boolean,…)` sets it). Which values real
firmware reports is UNKNOWN; the emulator's `DEFAULT_COMMON_SETTINGS` is POLICY.

### 5.13 capability exchange — opcode 138 (DEV_NEW_FEATURE_REQ / l1 FeatureReq) — DIRECT

```
device push:    [0]=01 [1..2]=8A 00 [3] u8 bitmap        4 bytes (read as payload=frame[3:])
host reply:     01 8A 00 FF   (l1.enPkg = packHead + c(1|2|4|8|16|32|64|128))
consumer:       q$f.a(byte[]) case 138: requires length > 3, forwards the payload
                to listener.deviceNewFeature, derives the Wi-Fi AES selector
                from bit 3 (§4.3); an empty payload logs "数据长度不足" and is
                then read as a bitmap from the whole frame: frame[0] & 8 =
                0x01 & 8 = 0, so it CLEARS the selector (q$f.a 477-484,
                642-670; corrected 28 Sep, it said "untouched")
confidence:     DIRECT
implemented:    yes — profile.py `push_new_feature` / `_feature_request`
tests:          tests/test_r7_feature_exchange.py (9)
```

Only bit 3 has a recovered consumer; the host advertises all eight bits and no
reader for the other seven was found. Which bits real firmware sets is UNKNOWN;
the emulator's `feature_bitmap` and its unsolicited push are POLICY (§12). This
section closes a documentation gap: the exchange was implemented and tested in
R7 but absent from this register until the closure audit.

## 6. Advertising

`u4.a(ScanResult)` parses manufacturer-specific data into
`BleDevice(name, mac, rssi, manufacturerCode, projectCode, versionType,
versionCode, serialNumber, bindInfo, portVersion)`.

### 6.1 The parse rules — SOURCE-DERIVED

Walked in `javap/com/plaud/sdk/proto/u4.txt`, bytecode offsets 254–719. jadx is
unusable for this method (it renders `bArr[0]` as `bArr[0] ? 1 : 0`).

It is a short chain of length-prefixed fields, **not** a general TLV:

```
[0]                u8    len0
[1 .. len0]        projectCode, readInt(len0*8) -- ONLY when len0 == 2 (offset 277)
[1+len0]           u8    len1                       (offset 300)
[len0+2] = [4]     char  versionType                (offset 331)
[5 .. 5+len1-2]    uint  versionCode, readInt((len1-1)*8) at offset 5   (offset 365)
[4+len1]           u8    len2   = serial length     (offset 383)
[5+len1 ...]       bytes serial number, len2 bytes  (offset 415)
```

then exactly one of three branches (offset 427):

| Condition | serial rendering | portVersion |
|---|---|---|
| projectCode ∈ {901, 705} | ASCII, from the first non-zero byte | **u16le at the cursor** `5+len1+len2` |
| `len2 < 10` | **HEX** (`s7.b`) | **u16le at a FIXED offset**: 18 for projectCode ∈ {888, 881}, else 15 |
| otherwise (`len2 ≥ 10`) | ASCII | **never assigned — stays 0** |

Then `bindInfo` (offset 655): `if (len >= gate+gateLen && u8@(gate-1) == 1)
bindInfo = u8@gate`, where for the first two branches `gate = len-1`, i.e.
**bindInfo is the last byte, gated on the second-to-last byte being 1**.

Finally (offset 699): `if (serial.length() >= 3) projectCode =
parseInt(serial[0:3])` — **the u16 at offset 1 is not the last word on the
project code.** With a hex-rendered serial this reparses hex digits as decimal,
and a non-numeric prefix throws into the method's own catch-all.

The third branch is the consequential one: **a device whose serial is 10 bytes
or longer is always read as portVersion 0**, i.e. cleartext. Which branch real
hardware takes is **UNKNOWN** (U18) — no capture exists.

`emulator/plaudsim/advertising.py` implements both a port of this parser and a
builder constrained by it; `tests/test_r1e_advertising.py` pins the fixed
offsets and the branch that silently drops portVersion.

The element chain has **two variants**, decided solely by `b[0] == 2`:

* **A** (model code present): element 0 is the 2-byte projectCode, as above.
* **B** (`len0 != 2`): there is no model-code element; the cursor stays at 1 and
  element 0 *is* the version element — `[0]=L1 [1]=versionType [2..L1]=versionCode`.

And on **every** branch the payload ends `… 01 <bindInfo>`: the `0x01` is an
element length byte doing double duty as a magic marker, and `bindInfo` stays
−1 (so `isBindInfoOk()` is false) if it is missing.

This decode was reached twice independently in the 2026-09-22 audit — once by
hand and once by a separate recovery pass — and the two agree, including the
variant split, the fixed 15/18 offsets and the trailing `01 <bindInfo>` pair.

### 6.2 Other recovered facts

* Model codes: **880** Plaud NotePin, **881** Plaud Note Pro, **882** Plaud
  NotePin S, **888** Plaud Note. Derived from the first three characters of the
  cleaned serial-number string, and used to synthesise a name when the device
  advertises none. Other project codes branch differently: 901/705, 888/881,
  708 (`isJT`).
* `versionName = versionType + "%04d" % versionCode` — the same construction
  `x2` performs on the wire.
* Scanning is **unfiltered at the BLE stack level**: `startScan` is handed a
  freshly allocated empty `ScanFilter` list, so the public
  `t3.a(ScanFilter)`/`t3.b(ScanFilter)` API has no effect. All filtering is
  software, on parsed manufacturer data, dropping results with an empty serial
  number or MAC.

**UNKNOWN:** the exact byte offsets inside the manufacturer data, including
where `portVersion` sits. The parser walks a TLV-ish structure with several
project-code-dependent branches (one branch reads a u16 at raw offset 15, and
another at 18). jadx renders this method very badly (`bArr[0] ? 1 : 0`), so it
must be walked in bytecode. **This is the gating unknown for R4** (§8).

### 6.3 A regex quirk that affects the product-name lookup

The serial is scrubbed with `[^a-zA-Z0-9_-\u2E80-\u9FFF]` (u4.a 795-805)
before the product-name lookup, and `BleDevice` is built from the scrubbed
string. `_-\u2E80` parses as a character **range** U+005F..U+2E80, not as a
literal `-`, so backtick, braces, tilde and all of Latin-1 survive while most
CJK is stripped. Run in a JDK 17 over the whole BMP (28 Sep), the kept set is
exactly `-`, `0-9`, `A-Z`, U+005F..U+2E80 and U+9FFF. The projectCode override
(699-719) parses the first three characters of the **unscrubbed** serial with
`Integer.parseInt`; a throw keeps the u16 project code. Until 28 Sep
`ScanFields` used a plain alphanumeric filter for the name and did not scrub
`serial_number` at all, so it differed from the SDK for ASCII serials too
(`88_1…`, trailing NULs); `plaudsim.advertising.scrub_serial` now applies the
SDK's kept set (review 2026-09-28).

---

## 7. Wi-Fi bulk transfer — SOURCE-DERIVED; emulated against a phone double only

Pen side now implemented in `emulator/plaudsim/wifi.py` + `wifi_device.py`, tested only against
a bytecode-derived phone double (`docs/wifi-transport.md`), never against the real SDK or a phone.

**The roles are inverted between the two layers, which is the thing to get
right:** the **pen** raises a WPA2 SoftAP and the **phone** joins it as a
station — but then the **phone** runs a `java-websocket` SERVER on TCP **8081**
and the **pen** dials in as the WebSocket client. A hardware-free emulator must
therefore run (or fake) an AP and **dial out** to its DHCP client on 8081, not
listen.

SoftAP credentials are not delivered by any protocol message: SSID is
`"PLAUD"`/`"Plaud"` + the last 4 characters of the advertised serial, and the
WPA2 passphrase is the last 8. `OpenWiFiRsp` can carry an 8-byte passphrase but
the state tracker discards it. **Anyone who can see the advertisement can
compute the Wi-Fi password.**

PDU envelope on every binary WebSocket frame, both directions:

```
[0..2]   u24le totalSize
[3]      u8    pduVersion
[4..5]   s16le messageType
[6..7]   s16le jsonSize
[8 .. 8+jsonSize-1]   UTF-8 JSON
[8+jsonSize ..]       optional raw binary tail
```

On portVersion ≥ 20 the whole PDU is wrapped exactly as the BLE channel is:
`AEAD(key=z.J, nonce=z.K, aad=z.L)` over `[u32le seq][PDU]`, reusing the keys the
**BLE** session established. Whether an inbound frame is encrypted is decided by
a **heuristic, not a flag**: the agent reads u16le at raw offset 4 and treats the
frame as ciphertext when that value exceeds 200 — every legitimate message type
is ≤ 101.

Message types: 0 UniversalErr, 1 Handshake, 2 SayHello, 3 Heartbeat,
4 WifiClose, 5 Tips, 11 GetFileList, 12 FileSync, 13 FileSyncContent,
14 FileDelete, 15 FileSyncStop, 16 ExtendExitTime, 20 SendOTAFileInfo,
21 RequestOTAPackage, 22 OTAStatusSync, 100 SpeedTest, 101 GetDeviceLog.

Two of them matter for a future audio milestone:

* **11 GetFileList** — request `{"uid", "start", "single"}`; response
  `{"uid","status","count","offset"}` plus a binary tail of **10-byte** records
  `[u32le sessionId][u32le fileSize][u16le scene]`. Note this is a *different*
  10-byte record from the BLE one (§5.6): here the trailing field is a **u16**
  scene, with no attribute byte.
* **13 FileSyncContent** — `{"session","offset","length","last"}` plus `length`
  raw bytes at `8+jsonSize`.

Handshake (type 1) carries `{"token": padEnd(token, 32, '0'), "stamp": <unix seconds>}` — identical
to BLE `k3` (pv ≥ 9) up to 32 chars only: `padEnd` never truncates, `k3` does (`docs/wifi-transport.md`).

The BLE side of the handoff:

| Opcode | Message | Layout |
|---|---|---|
| 10 | OpenWiFi req/rsp | req `[01][0A 00][u8 mode][8B ascii pass?]` — **not on/off** (R7-S14): `startWifiTransfer` sends mode 0, `setDeviceWiFi(true)` mode 1, and both "off" paths use opcode 13; rsp `[..][u8 status][8B pass]` when len ≥ 12 |
| 13 | CloseWiFi req/rsp | req header only; rsp `[u8 status]`. The SDK waits for the answer on opcode **10** (`q.P`, ALL.txt:46011-46026) while `l0` requires 13 (R7-S14) |
| 16 | Get/SetWebsocket req | `[01][10 00][u8 op 1=get 2=set][u8 type]` then, on set, 64 NUL-padded bytes for type 1 (url) or 16 for types 2/3 (tokens) |
| 17 | Get/SetWebsocket rsp | `[01][11 00][u8 type][value, width 64 for type 1 else 16, NUL-terminated]` |

`WebsocketType {url, serToken, devToken}` = types 1, 2, 3. Type 0 throws.

**R7-S14 (2026-09-25) — the genuine SDK's Wi-Fi path on the AVD, RUNTIME_PROVEN
where marked** (`r7/r7-s14-wifi-real-sdk.md`, evidence `r7/r7-s14-evidence/`):

* The phone JOINS the pen's SoftAP before it starts its server, and the join is
  mandatory: `LambdaTaskRunner` runs CONNECTING → BLE prerequisite (1001) →
  `saveEncryptionKeys` → `openDeviceWifi` (opcode 10, mode 0; 1002 on failure)
  → `WifiConnectionManager.connectToDeviceWifi` (1003 on false) → CONNECTED,
  HANDSHAKING → `startWebSocketAndHandshake` (server start + 30 s session wait;
  1004) (`LambdaTaskRunner.txt:64-220`). On API ≥ 29 the join is
  `ConnectivityManager.requestNetwork(NetworkRequest{WIFI, −INTERNET,
  WifiNetworkSpecifier{ssid, wpa2}}, cb, 30000)` (`WifiConnectionManager.txt:40-162`);
  nothing binds the process or the server socket to that network (no
  `bindProcessToNetwork` in ALL.txt; server = `new InetSocketAddress(8081)`).
  RUNTIME: request for `PLAUD0001` registered, the Settings
  `NetworkRequestDialogActivity` opened, request released after 30.0 s,
  `onError(1003)`; port 8081 never listened; zero Wi-Fi PDUs.
* SSID is `BleDevice.getWiFiName()` (project-code/portVersion dependent,
  ALL.txt:11094-11210) — for project 881 below portVersion 20 it is `PLAUD`+last 4
  (RUNTIME: `PLAUD0001`); `WifiAgentImpl.calculateWifiName` (`Plaud`+last 4) is
  only the fallback. The opcode-10 answer's passphrase is ignored.
* Emulator fix: `PlaudPeripheral._open_wifi` treated mode 0 as "hotspot off", so
  the SDK's own open never started the Wi-Fi device (RUNTIME, run 1b); now every
  opcode 10 opens (tests `test_r7_s14_*` in `tests/test_wifi_ble_handoff.py`,
  `tests/test_wifi_ble_crossover.py`).
* The Wi-Fi handshake token is `resolveHandshakeToken("")`; on the recovery
  path it differs from the BLE `k3` token (RUNTIME: len 21 vs `SYNTHHIST0001`).
  A SayHello token mismatch is only logged ("Token mismatch! … 握手会被设备拒绝");
  the phone still sends its HandshakeRequest; the pen decides
  (`WifiAgentImpl.txt:1376-1417`).

---

## 8. Recorded audio — SOURCE-DERIVED

`sdk/ble/util/PlaudEncryptHeader`, 512 bytes, little-endian:

```
[0..7]     8B   magic "PLAUD.AI"
[8..9]     u16  version
[10..11]   u16  headerSize
[12..15]   u32  crc            (never verified anywhere in the SDK)
[16..47]   32B  userId, ASCII, trimmed
[48..49]   u16  fileType
[50..51]   u16  channel
[52..53]   u16  encryptType
[54..57]   u32  duration (seconds — toString appends "s")
[58..127]  70B  reserved / skipped
[128..131] u32  counter
[132..143] 12B  nonce
[144..147] u32  segment
[148..255] 108B algParams
[256..511] 256B keyCipher      <- 256 bytes == RSA-2048 ciphertext
```

The 256-byte `keyCipher` is the strongest available evidence for **RSA-2048**.
`isEncrypted()` is `MAGIC_STRING.equals(new String(magic).trim())`.

Opus geometry, from `BleFile`'s static helpers — SOURCE-DERIVED:

```java
calculateOpusOffset(byteLen, ch)   = (byteLen/20) * 80 * ch
calculateOpusDuration(byteLen, ch) = (byteLen / (ch*80)) * 20
calculateOpusFileSize(ms)          = (ms/20) * 80
calculateOggOffset(a,b,ch,ms,x)    = a + ((ms/(ch*20)) * (b + (x*80*ch) + 51))
```

i.e. **20 ms frames, 80 bytes per frame per channel** (32 kbps/channel) and an
Ogg page overhead of **51 bytes**. `q` corroborates: `p7.l = audioChannel * 80`.

### Codec parameters — SOURCE-DERIVED

Opus at **16 000 Hz, 320-sample (20 ms) frames, 32 000 bps CBR, exactly 80
bytes per frame per channel**. The 80 is load-bearing in at least four
independent places: `libjni_ogg`'s `opus_frame_bytes` check
(`sampleRate * channels / 200` = 80), `q`'s `p7.l = audioChannel * 80`,
`AudioExporter`'s `frameSize = channels * 80`, and `BleFile`'s arithmetic
above. `libjni_ogg.putPkg` compares each packet's length against that value
and returns −2 on mismatch, so **there is no variable-bitrate path**: a
correctly formed but variable-size Opus stream is rejected at the repack stage.

### Quirks in files the SDK itself writes — from native disassembly

Recorded because a synthesiser that "fixes" them produces files the SDK's own
round trip would not. These come from disassembly of `libjni_ogg.so` rather
than from Java bytecode, so treat them as SOURCE-DERIVED from a second artifact
rather than DIRECT. To be explicit about the boundary: artifact/symbol evidence
was confirmed present in this environment, but full ARM instruction-level
verification was not performed here, so these remain lower-verifiability claims
than the bytecode-derived rows above. Do not promote them to mechanically
verified facts:

* **preSkip is 0**, not the 960 the frame size implies: the header builder
  multiplies by a hard-coded `0.0`.
* **Granule positions are inflated by two frames (1920 samples at 48 kHz)**:
  the OpusHead and OpusTags pages each increment the same packet counter that
  feeds the granule formula.
* **The OpusTags packet leaks uninitialised stack memory** — only the first 26
  of 403 bytes are written — so the SDK's Ogg output is not byte-reproducible.
* **The OpusTags packet is not spec-conformant**: the u32le that RFC 7845
  defines as `user_comment_list_length` carries a byte count (403) instead.
* **No EOS flag is ever set.** All `ogg_stream_iovecin` call sites pass
  `e_o_s = 0`.

`OggOpusParser` survives all of that only because it **decides page roles by
counting**: page 0 is assumed to be OpusHead, page 1 is discarded unread, and
pages 2+ are audio. It also mishandles a packet split across a page boundary
(a trailing lacing value of 255 is flushed as a complete packet). A synthesised
file must therefore keep OpusTags to a single page and must never split an
Opus packet across pages.

### File encryption — SOURCE-DERIVED

The audio payload is **raw ChaCha20 (RFC 7539) with NO Poly1305 tag** — not the
AEAD the control channel uses. The 32-byte key comes from RSA-unwrapping the
512-byte header's 256-byte `keyCipher`. `q5`'s authenticated helpers are never
used on this path, so the ciphertext is freely malleable, and **the same
keystream is reused for every segment** (a fresh `ChaCha7539Engine` per chunk
with the same key, same nonce and counter 0; the header's `counter` field is
never read).

`AudioDecryptor` and `AudioExporter` **disagree**: the former ignores
`header.segment` and does one continuous pass, the latter respects it. For any
file with `segment > 0` the public `AudioDecryptor` helper produces garbage
after the first segment. Do not treat it as the reference implementation.

Of the header's fourteen fields only **magic, nonce, segment and keyCipher**
have any consumer; the 512-byte offset is hard-coded and `headerSize` is
decorative.

`sdk/ble/util/OggOpusParser` is a standard Ogg page walker ("OggS" magic,
segment-table lacing, packets split on lacing values < 255) that reads
`OpusHead` for channels (`@9`), preSkip (u16le `@10`) and sampleRate (u32le
`@12`), defaulting to 48000 Hz / 1 channel / preSkip 0.

`AudioDecryptor.decryptChaCha20` requires an **exactly** 32-byte key.
`decryptRsaKey` strips both PKCS#8 and PKCS#1 PEM markers but always feeds the
bytes to `PKCS8EncodedKeySpec`, so a genuine PKCS#1 key silently fails.

---

## 9. Unknowns register

| # | Unknown | What would settle it |
|---|---|---|
| U1 | Manufacturer-data byte layout, incl. the portVersion offset | The `u4.a(ScanResult)` bytecode walk is done (R1e, `ALL.txt:80850-81100`); which branch real hardware takes (U18, merged into U1 in `docs/final-uncertainty-matrix.json`) needs one scan capture. |
| U2 | Units of `free`/`total`/`duration` in StorageRsp | A hardware capture, or a first-party app that formats them |
| U3 | Semantics of the `attribute` byte in a file-list entry | `getAttribute()` has zero call sites in the shipped SDK |
| U4 | Semantics of z2's bytes 16 and 17 | No toString name, no consumer found |
| U5 | Whether the device honours a non-zero `startSessionId` or `flag` in p2 | Live experiment; no shipped code sends either |
| U6 | The device's 0xFE12 fragment size (how it chunks the secret package) | Capture, or firmware |
| U7 | Whether the device validates the pushed RSA public key | Push a self-generated key with a valid cloud snSignature to real hardware |
| U8 | Semantics of the constant `0x02` at k3 offset 3, and of `z.h()` (stubbed to 0) | An older SDK build, or the iOS encoder |
| U9 | Device-side MTU acceptance, and whether it enforces receive-sequence monotonicity | Hardware |
| U10 | The meaning of `isForceClear` (0xFE20 vs 0xFE10) | Hardware; the iOS parameter name is the only hint |
| U11 | `c5.start` — byte offset or timestamp? | No consumer in the Android code |
| U12 | Whether the device emits file-list frames strictly in index order | Air capture; the client has no gap recovery, so it must |
| U13 | What the transfer-tail CRC covers | The SDK never computes it. The only whole-file primitive nearby is `tntGetFileCrc` = CRC-16/CCITT-FALSE(init 0xFFFF), used only for OTA |
| U14 | Whether y6's `end` is inclusive or exclusive, and whether 0 formally means EOF | Issue y6 with `end = start + N` and count delivered bytes |
| U15 | EMPTY_PACKAGE `code` values real firmware sends (U23 is a duplicate) | Client side known (§15): any code completes when no recovery is in flight, codes 0 and 1 completed live; code 1 is ignored during a recovery. The device's choice is unknown |
| U16 | s6 `status` values beyond "non-zero is an error" | Request a non-existent session, an out-of-range start, and a busy device |
| U17 | Whether real firmware sends a TAIL after the EMPTY_PACKAGE, or the EMPTY_PACKAGE alone | One clean end-to-end capture. **Corrected 2026-09-24**: the earlier note "both terminate a transfer … nothing orders them" was wrong for the client — only EMPTY_PACKAGE completes, and it must precede the TAIL (§15) |
| U18 | Which branch of `u4.a` real hardware's advertisement takes (merged into U1 in the uncertainty matrix) | A single scan capture. Only two of the three branches assign portVersion at all |
| U19 | Cloud-exported audio vs the local SDK writer: real cloud downloads carry OpusTags vendor `PALUD.AI` (riffado #160, CLOUD_OBSERVED) while `libjni_ogg.so` writes `TinnoTech123456789012` (§8) — does the cloud transcode or only re-tag? | One lawfully obtained download from an account the operator owns; `PALUD.AI` appears nowhere in the SDK, so cloud artifacts must not be treated as local-writer output |
| U20 | Which of the four closed recording shapes ({plain, PLAUD.AI-encrypted} × {Ogg, raw Opus}) real firmware emits over BLE, and whether the 512-byte header is present on the BLE path | One authentic BLE file-transfer capture; the selection chain itself is closed (§8, R6/R7) |
| U21 | The absolute host of the legacy TntAgent endpoints `/recorder/device/{checkSn,checkCustomer,saveOperation}` (`r3`/`q3`; the builder shows an empty host prefix) | Static: trace `r3.b(String)` callers; it must never be exercised (production, hard-coded query-signature secret) |
| U22 | Which capability bits (opcode 138) and which CommonSettings values (opcode 8) real firmware reports | Any real-device session log; both channels are implemented with POLICY values |

## 10. Independent corroboration: none exists for the handshake

Every third-party repository in `reference/third-party/` (`riffado`,
`plaud-api`, `plaud-toolkit`, `python-plaud-ai`, `applaud-rsteckler`,
`applaud-landoncrabtree`) implements **cloud API** access only. None contains
the GATT UUIDs, the markers, ChaCha, or any handshake. There is therefore **no
second implementation to cross-check the BLE protocol against**, and the only
non-circular evidence available is the bytecode itself. That is exactly why
`docs/evidence-digest.json` and `tests/test_evidence_conformance.py` exist.

## 11. jadx traps encountered

Recorded because each one is a place a future reader can go wrong.

| Class | jadx says | Bytecode says |
|---|---|---|
| `TntBleCommUtils` | `a(int)`/`b(int)`/`c(int)` are infinitely self-recursive | `i2l; invokevirtual a:(J)[B` — they call the *long* overload, which is the little-endian proof |
| `n7.<init>` | `(byte)(a(bArr,7) & 255)` reads like an unsigned mask | the `i2b` is what decides it: the field is **signed** |
| `u4.a(ScanResult)` | `bArr[0] ? 1 : 0`, nonsense control flow | must be walked in bytecode; jadx output is unusable here |
| `v4.getBleRequestType` | `return this.d ? w.c.b : w.c.a;` | javac inlined the constants; the symbolic names are jadx re-matching by value, and 65042 exists in **both** tables so any name it picks is arbitrary |
| `z.java:857` | `PlaudEncryptHeader.MAGIC_STRING.equals(...)` | the constant is an inlined string literal `"PLAUD.AI"`; jadx invented the owning class |
| `z$c` type-4 branch (plaintext path) | a clean if/else chain | type 4 **falls through** into the type-5 handler; the encrypted path's copy does not |
| `q.v` / several catch blocks | `Throwable th = null; … th.printStackTrace();` | would NPE; the real bytecode is a bare invoke on the caught exception |
| `q.m`'s response lambda | inlined into `q.java` | the real method `q.X(c$c, byte[])` has no counterpart in the decompiled source |

## 12. Emulator policy register

Choices the harness makes that the SDK does not fix. None of these is a claim
about the device.

| Policy | Where | What would settle it |
|---|---|---|
| DATA payload size = 32 bytes/frame | `transfer.DEFAULT_DATA_PAYLOAD_SIZE` | Hardware capture. The protocol ceiling is the u8 length field (255) and the ATT MTU. |
| No pacing: HEAD, all DATA and TAIL emitted back-to-back | `TransferSession.frames` | Hardware capture |
| A second y6 replaces the active session | `PlaudPeripheral._start_transfer` | Hardware |
| HEAD status, TAIL crc, file bytes, file table, resume fields | constructor arguments | synthetic by construction |
| `p2`'s `startSessionId` and `flag` are parsed but do not filter the table | `_serve_file_list` | U5 |
| 2BB0 advertises both NOTIFY and INDICATE; 2BB1 advertises WRITE (with response) | `PlaudPeripheral.__init__` | Q1 — needs hardware or a trace |
| The emulator declares portVersion 7 and refuses to claim ≥ 20 | `profile.PORT_VERSION` | — this is a deliberate scope boundary, not a gap |
| `0x180F` is published even though a portVersion-7 client never reads it | `PlaudPeripheral.__init__` | harmless; documented in §4.1 |
| Any handshake token is accepted (`accept_any_token=True`); `handshake_status` drives the refusal path | `PlaudPeripheral._handshake` | real hardware compares the token against account-issued material (§4.5); U7 |
| The opcode-138 capability bitmap (`feature_bitmap`, default 0) and the unsolicited `push_new_feature` emission | `PlaudPeripheral.push_new_feature` | U22 — only bit 3 has a recovered reader |
| CommonSettings values (`DEFAULT_COMMON_SETTINGS`: ENABLE_VAD=1, REC_MODE=1; unknown types read as 0) | `PlaudPeripheral._common_settings` | U22 |
| The l3 timezone byte (`l3_timezone`, default 0). The SDK caches it in `q.n` and the public facade forwards `q.n` as BOTH `bleBind(protVersion, timezone)` arguments (§14) | `PlaudPeripheral._handshake` | hardware; the facade quirk itself is proven |

---

## 13. Transport seam for real-SDK integration (R4-S1) — SOURCE-DERIVED

Call chain for one device operation (Android), verified class-by-class:

```text
template app
  -> sdk/PlaudDeviceAgent.* (facade: getState/syncTime/getStorage/...)
  -> com.plaud.sdk.proto.t3.* (agent interface; q implements it)
  -> com.plaud.sdk.proto.q.* (drivers: build request, register callbacks)
  -> com.plaud.sdk.proto.z.* (GATT glue: connectGatt, write/read/descriptor,
     requestMtu(255), discoverServices, setCharacteristicNotification)
  -> android.bluetooth.* (OS API; callbacks arrive via z$c, which directly
     extends BluetoothGattCallback and overrides the full set)
```

Transport calls are CENTRALIZED in `z`/`z$c` but there is NO replaceable
abstraction: no interface hides `android.bluetooth`, no mock/fake/test
transport exists in the 674-class AAR, and `sdk/bluetooth/BluetoothManager`
is app-side scan/sync orchestration, not a seam. iOS is stricter still:
CoreBluetooth use lives inside the precompiled `.framework` (19 symbol
matches), so there is not even bytecode to read.

Consequence: the SDK's protocol logic cannot be lifted off its transport
without an Android (resp. iOS) runtime. The only evidence-backed seam is
BELOW the OS API, at the HCI/controller level: Bumble's
`transport/android_netsim.py` attaches a Bumble device to the Android
emulator's virtual Bluetooth network, so an unmodified template app on an
AVD can talk to `plaudsim.PlaudPeripheral` with no radio and no hardware.

Falsification attempts closed: no second platform implementation exists in
the artifact (the Tinno package holds entities/utils only); callbacks are
delivered on OS binder threads the SDK does not abstract (plus direct
`android.os.Handler` use); nothing here can substitute Bumble for those.

Status: seam IDENTIFIED at the virtual-controller layer; executing the real
AAR against the emulator (build template APK with a valid partner
`userAccessToken` for `initSDK`, boot an AVD with BLE, wire netsimd) is
sequenced follow-up work, not part of this sprint. Environment already
holds JDK 17, the Android SDK, two AVDs, adb, and `netsimd`; missing pieces
are `bumble[android]` (grpcio/protobuf) in the venv and the app build.

Update 2026-09-22 (R4-S2): the transport itself is now VERIFIED bidirectional
with a generic ping/pong app (see `r4-s2/`). The remaining V3 risks are only
the template APK build and the `initSDK` partner-token requirement, not the
BLE path.

Update 2026-09-22 (R4-S3): the real template app runs against the emulator:
scan lists the peripheral (`Plaud Note Pro / SN: 8810000001`), tap Connect
drives GATT connect → MTU 517 → service discovery → 2BB0 subscribe (CCCD
indication write received by Bumble) → `first_handshake
detail=bind_token_empty` → disconnect. First blocking point is the missing
handshake token (no partner `userAccessToken` in this build), exactly the
predicted credential boundary; no GATT/transport failure precedes it. See
`r4-s3/` (evidence logs included). Flaky pre-fix runs traced to the
peripheral's rotating MAC vs the app's cached scan entry — fixed with a
static random address, a test-harness property, not a protocol claim.

Update 2026-09-22 (R5-S1): GATT-boundary audit against the same runner with
a single peripheral on netsimd. The SDK's own GATT DB shows
1910:[2BB0+CCCD, 2BB1] plus 180F:[2A19+CCCD] (1800/1801 are Bumble-substrate
generics), MTU 517, CCCD indication write 0200 received by Bumble, then
`first_handshake detail=bind_token_empty` → disconnect. No transport-,
service-, characteristic- or CCCD-level discrepancy; `PlaudPeripheral`
unchanged (2BB0 NOTIFY|INDICATE and 2BB1 WRITE remain POLICY per Q1). A
multi-peripheral contention run that failed earlier at `send_fail` was
environmental (one netsimd, three advertisers), not a GATT mismatch. See
`r5-s1/`.

Update 2026-09-22 (R5-S2): first-handshake outbound path re-derived from
javap (no live run): `q.a` checks `TextUtils.isEmpty(g)` and returns
`bind_token_empty` BEFORE any k3 construction; otherwise
`k3(z.h()=0, 0, token, portVersion)` → packHead `[01][01 00]` → 255-byte
scratch → stage iff pv>=3 → token width 32 iff pv>=9, `'0'`-padded,
`u4.b`-trimmed → `z.a({1,2}, …)` → 1910/2BB1 `writeCharacteristic`
(default with-response). j3 tail (8 raw + u8 len + name, both-null
short-circuit) confirmed. Agrees with R2/R2a throughout; no digest or
frozen-evidence change. `handshake.json` stage prose still carries the
superseded NOT-byte-exact note (stale vs its own evidence table). See
`r5-s2/`.

Update 2026-09-22 (R5-S3): j3 stage audited from javap (no live run):
`d0` is called only from the x2 branch (after checkSn, unconditionally),
gated on `r3.e()`; `j3(z.h(),1,g,pv,i,j)` with `i/j` from `t3.a` params
(template path leaves both `""`, so the shipped tail is 9 zero bytes);
waiter `{1}`; l3 status 0 → `old_protocol_ok` → `c0()` → m7 syncTime,
then battery/getState/d4/`s0$b` fan-out. No contradiction with R2/R2a;
no digest or frozen-evidence change. See `r5-s3/`.

Update 2026-09-22 (R5-S4): l3 parser re-derived from javap (no live run):
status u8@3 / pv u16@4 / tz u8@6 unguarded (7-byte minimum);
tzMin/audio/wifi/noNsAgc/ogg guarded len≥8/9/10/11/12 with Java defaults
(audioChannel=1); versionType char at len-4, version u24LE at len-3;
`n.<init>` throws `Mismatch` with no length pre-check. Status 0 →
`old_protocol_ok` (label only) → `c0()` → m7 syncTime (9 B, signed tz
pair), then battery/getState/d4/`s0$b` fan-out. Agrees with R1c/R2
throughout; no digest or frozen-evidence change. See `r5-s4/`.

Update 2026-09-22 (R5-S5): modern path audited (javap-first, lambda
dispatch via jadx, labeled): notify-ok → `z.y()` (M=1/N=-1/keys null)
→ pv≥20 → `n()` v4 chunks {65041,65042} → last reply 0xFE11 → `a0()`
w4 chunks {65042} → device 0xFE12 → dedupe/sort/concat → RSA →
J/K/L + `PLAUD.AI` self-check → post-key methods shared verbatim with
legacy (`q.a` has no pv branch) under per-frame ChaCha seal
(`[u32 seq][frame]`, same key+nonce, N-gated RX, rethrow on AEAD
failure). Same token guard gates modern too. No contradiction; nothing
frozen changed. See `r5-s5/`.

Update 2026-09-22 (R5-S6): sealed-transport semantics closed from javap:
M=1/N=-1 at init and `z.y()`; M pre-incremented per seal call (first
sealed seq 2; only other M writer is OTA-only `z.d`); seq LE32 inside
the AEAD plaintext (proven by stack accounting, not adjacency);
N accepts-then-sets with silent `N>=seq` drop and AEAD-failure leaving N
untouched; `z.a` returns queue-add, one outstanding write, error -99 on
`writeCharacteristic()==false`, no transport ACK. No contradiction;
nothing frozen changed. See `r5-s6/`.

Update 2026-09-23 (R5-S7): first sealed slice implemented test-only with
synthetic J/K/L (deterministic ASCII fixture, pv 21, no credentials, no
backend, no live handshake): `emulator/plaudsim/sealed.py` wires M
pre-increment (first sealed seq 2), N accept-and-set with silent
duplicate/stale drop, AEAD-failure propagation with N untouched, and
seq-inside-AEAD (RFC 8439 §2.8.2 vector reproduced byte-for-byte plus a
cross-library check). Sealed GetState req seq 2 → rsp seq 3 verified
in-process and over a real Bumble virtual GATT leg (replay yields no
second response); sealed dispatch lives in a test double
(`tests/sealed_support.py`), `PlaudPeripheral` still refuses pv>=20.
170 tests pass (154 + 16), all R5 verifiers pass. See `r5-s7/`. *(historical running count; §14 gives the closure total)*

Update 2026-09-23 (R5-S8): independent sealed endpoints (`SealedLink`:
M_host/N_host + M_device/N_device, first TX 2 on each side) carrying the
frozen R3 FileList and SyncFile framings unchanged inside the envelope:
sealed p2 → q2 with totals/session/size/scene/attribute verified,
sealed y6 → HEAD + type-2 DATA (offsets/session/payload) + TAIL/CRC with
payload reassembled, replay/stale drops with N pinned, ciphertext and
seq-region tamper raising InvalidTag before dispatch, plus Bumble GATT
legs for filelist/sync/replay/tamper. Sealed dispatch lives in the test
double; `PlaudPeripheral` still refuses pv>=20. 188 tests pass
(170 + 18), all R5 verifiers pass. See `r5-s8/`. *(historical running count; §14 gives the closure total)*

---

## 14. R7-S12: the official SDK's own k3 against this emulator — RUNTIME_PROVEN

**Result.** The unmodified `plaud-sdk.aar` (sha256 `041a6f88…`), running on an
API-34 AVD and attached to `emulator/plaudsim` over Bumble's `android-netsim`
transport, constructed its first_handshake (k3) frame from a **synthetic**
identifier, wrote it to 1910/2BB1, had it accepted by the emulator, and
reported `bleBind status=0`. The captured bytes equal the bytecode model
byte-for-byte. Evidence files: `r7/r7-s12-k3-capture-*.json` (every 2BB1
write, timestamped), `r7/r7-s12-logcat-*.log` (the SDK's own log lines),
`r7/r7-s12-peripheral-*.log`; driver `r7/android-app/.../debug/K3CaptureActivity.kt`
(public SDK API only) and capture peripheral `r7/k3_capture_peripheral.py`
(frozen `PlaudPeripheral` plus a write mirror). Full write-up:
`r7/r7-s12-k3-runtime-capture.md`.

**Why it is legitimate.** `recoveryConnectBleDevice(device, historicalUserId)`
derives the token by a pure local string transform (`removePrefix
"client_user_"`, drop `-`; ALL.txt:2678-2699) and, because the emulator
advertises portVersion 7 (< 20), `q.b0` goes straight to first_handshake, whose
only precondition is a non-empty token. No cloud call, key, signature, or
credential exists on this path; the device that accepted the token is ours
(POLICY). It is **not** evidence about real hardware and **not** authentication.

**The four runs.**

| run | historicalUserId | SDK-normalised token | 2BB1 k3 | outcome |
|---|---|---|---|---|
| 1 | `SYNTH-HIST-0001` | `SYNTHHIST0001` (13) | `01 01 00 02 00 00` + `"SYNTHHIST0001"` + `"000"` (22 B) — byte-exact vs `build_k3` | old_protocol_ok → sync_time → bind 0 |
| 2 | `client_user_` | `""` | **nothing written** | SDK logs `historicalUserId 为空，中止`, `bleConnectState(2)` (falsifier: the empty-token guard is the only gate) |
| 3 | `client_user_0123456789abcdef0123456789ABCDEF` | 32 chars | token **truncated** to `0123456789abcdef` (pv<9 width 16), no length warning | bind 0 |
| 4 | `SYNTH-HIST-0001`, emulator `l3_timezone=5`, opcode 8 answered | as run 1 | as run 1; **0 rejects** | `bleBind protVersion=5 tz=5` |

Every byte of k3 is forced by bytecode (`k3.enPkg`, ALL.txt:87361-87434;
`z.h()` = 0, ALL.txt:76051): `[01][01 00][02][z.h()][stage iff pv≥3][token
'0'-padded/truncated to 32 (pv≥9) or 16]`; `u4.b()` strips trailing 0x00 only,
so 0x30 padding survives. The SDK's own log named the normalised token and the
predicted "length ≠ 32" warning fired in run 1 and not in run 3.

**What else the runs proved.**

* The complete legacy connect sequence the SDK drives after k3, as written:
  syncTime (`01 04 00` + u32le unix + i8 tzH=5 + i8 tzM=30, the host's real
  IST offset — R1c's signed fields observed live), battStatus, getState, then
  two opcode-8 CommonSettings READs (§5.12). No FE10/FE20 marker was written:
  `isForceClear` is inert below portVersion 20 (predicted; observed).
* The SDK parsed **every field** of the emulator's l3 exactly as encoded:
  `old HandShakeRsp: l3{status=0, portVersion=7, timezone=5, timezoneMin=0,
  audioChannel=1, supportWifi=false, noNsAgc=false, isOggAudio=false,
  versionType=V, version=1}` (run 4) — the R5-S4 layout, round-tripped live.
* **Facade quirk, proven.** `bleBind(sn, status, protVersion, timezone)` on the
  public facade's connected transition passes `status = 0` literally and loads
  **both** `protVersion` and `timezone` from `t3.X()` = `q.n`, which the
  old-HandShakeRsp handler sets from `l3.e()` = the l3 **timezone** byte
  (offset 6), not from `l3.c()` = portVersion (ALL.txt:878-961, 44308, l3
  getters 36593+). Prediction "set l3 tz=5 ⇒ protVersion=5" held in run 4.
  The true port version is therefore not surfaced through `bleBind`.
* Opcode 8 (§5.12) was discovered here: the emulator rejected both frames as
  `unsupported_opcode` in runs 1-3 (the SDK still bound, since bind fires
  first) and answered them in run 4 with no reject.

**What it does not prove.** Nothing about real Plaud firmware; nothing about
the portVersion ≥ 20 sealed path (a cloud-issued snSignature and RSA key pair
would run *before* k3 there, §4.5); nothing beyond the connect sequence (the
driver only connects; file listing and transfer remain covered by the
in-process V1/R3/R5 tests).

**Closure totals (2026-09-23).** `pytest tests/` → **see docs/final-closure-report.md
for the exact count**; all 12 `r5-*/r6-*` verifiers ALL CHECKS PASSED;
reference integrity 33/33 pins + AAR sha256 match; `reference/**` unmodified.

## 15. R7-S13: a recording pulled through the official SDK — RUNTIME_PROVEN

Report: `r7/r7-s13-recording-pull.md`; evidence `r7/r7-s13-evidence/`
(captures, filtered logcat, peripheral logs, SHA256SUMS, two corrupted
outputs). Driver: `r7/android-app/.../debug/PullCaptureActivity.kt` (public
SDK API only, synthetic id); peripheral `r7/pull_capture_peripheral.py`.

* **V3 for the transfer path.** `getFileList()` → `bleFileList([BleFile{
  sessionId, fileSize, scene, attribute}])`; `syncFile(sid,0,0)` → HEAD, 353
  `bleData` frames, TAIL; `exportAudio(sid, dir, OPUS, 1, cb)` → DOWNLOADING →
  TRANSCODING → onComplete. Delivered bytes == served bytes (sha256
  `0f45367b…48c3d`) on the raw path (runs 1b, 3, 5, 6c) and the export path
  (runs 4b, 6b, 6d, 8, 10).
* **EMPTY_PACKAGE closes the transfer** (§5.8, corrected); TAIL alone does not;
  order EMPTY→TAIL; code 0/1 both complete. Emulator default changed
  (`TransferSession.empty_package_code = 0`, HARNESS_POLICY value; `None` =
  legacy sequence for the negative test). Pinned by
  `tests/test_r7_s13_transfer_close.py`.
* **Gap recovery observed live**: stopSync → a7 → syncFileStart(cursor) within
  35 ms (§5.8, corrected); converges when the device abandons the old stream
  (with `stream_in_task=True` a new syncFileStart/stopSync cancels the
  streaming task), otherwise a second restart and a corrupted export (run 6).
* **Configuration, not constructor defaults.** `PlaudPeripheral(...)` defaults
  to `stream_in_task=False, response_pacing_s=0.0` (inline, unpaced: in-process
  tests). Runs 6b–10 used an experimental subclass override (abort-on-restart
  and 4 ms pacing); runs 11–13 ran the shipped code path configured by
  `r7/pull_capture_peripheral.py`'s `PULLCAP_ABORT_ON_RESTART=1` /
  `PULLCAP_PACING_S=0.004` defaults; `PlaudPeripheral.for_real_sdk(...)` now
  packages exactly that (`REAL_SDK_STREAM_IN_TASK`,
  `REAL_SDK_RESPONSE_PACING_S = 0.004`; pinned by
  `test_real_sdk_preset_is_the_r7_s13_device_and_the_constructor_default_is_not`).
* **Op-queue race (HARNESS_POLICY for emulators)**: zero-latency responses
  made `BluetoothLeOperation` register its `[28,29]` bean after the HEAD had
  passed (`-98` on cancel, `-99 "Failed to send the download request"` on
  retry); ~4 ms inter-frame pacing (`response_pacing_s`) removed it and the
  bean completed `status:0` (runs 6b–10).
* **OPUS export is a byte-exact passthrough for plain-Ogg input** (no
  re-encode, OpusTags vendor untouched): U19 refined — the `TinnoTech…`
  vendor is written only when the SDK builds an Ogg from raw packets; the
  cloud's `PALUD.AI` tag comes from neither local path. `exportAudio` skips
  the download when `<outputDir>/<sessionId>.opus` already exists.
* **What it does not prove**: real firmware's frame sizes, pacing, code value
  (U15), TAIL field content, whether real devices abandon a stream on
  restart, anything above portVersion 20.

**Unknowns touched.** U15 (code values) and U17 (ordering) updated with the
client-side facts; U17's Phase-1 rationale corrected. A "U23" registered in
the first draft of this section duplicates U15 and is kept only as an alias.
