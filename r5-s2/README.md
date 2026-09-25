# R5-S2: first-handshake outbound construction audit (READ-ONLY)

Verdict: **HANDSHAKE CONSTRUCTION AUDITED — agrees with R2/R2a throughout.**

No token fabricated, nothing sent, no frozen file modified. All claims
below are asserted by `verify_first_handshake.py` (ALL CHECKS PASSED),
which parses `build/evidence/javap` text directly. Runtime anchors cite
frozen `r5-s1/logcat-sdk-sequence.log` (no live re-run needed).

## 1. Exact call chain into first_handshake

```text
template DeviceManager.connect
→ PlaudDeviceAgent.connectBleDevice(BleDevice, deviceToken)
→ NiceBuildSdk.resolveHandshakeToken [reads PartnerApiManager.getUserAccessToken,
   JWT sub-parse; logs 'no token available' when blank — r5-s1 line 10]
→ t3.a(device, token, ..)
→ q.g := token  (q.B() is the getter)
→ q.b0() → advertised portVersion < 20 → q.a()  [stage 'first_handshake']
```

## 2/3. Guard location and what (not) gets built

`q.a()` bytecode offsets: `getfield g` → `TextUtils.isEmpty` → if empty:
stage callback `bind_token_empty` + `s0$d.h` + `handshake fail UUID_IS_EMPTY`
+ return — all **before** `new k3` (offset 80). On the empty path **no
packet/frame is constructed**; the runtime log shows exactly this
(`first_handshake detail=bind_token_empty`, r5-s1 lines 123–125).

Non-empty path: log `handshake start` → waiter array `{1,2}` →
`new k3(z.h(), 0, g, BleDevice.getPortVersion())` → `k3.enPkg()` →
`z.a(int[]{1,2}, bytes, s$a, s$b)`.

## 3/4. Frame construction and GATT write path

`k3.enPkg`: `l.packHead()` → `[01][01 00]` (proto via u8 writer `a`,
opcode via u16 writer `b`; widths proven in pure-Java `TntBleCommUtils`
bodies: a=8 b=16 c=24 d=32 e=64) → 255-byte scratch → u8 `0x02` →
u8 agent (`z.h()`, stubbed `iconst_0`) → u8 stage **iff** `d>=3` →
token width 32 **iff** `d>=9` else 16, right-padded `'0'` (48),
truncated via `substring` → `ByteBuffer.wrap.put(token.getBytes())` →
`u4.b` trailing-zero trim. (`k3` has an inner `isEmpty` branch too, but
`q.a`'s guard makes it unreachable on this path.)

`j3` (second handshake, `q.d0` passes stage constant 1): k3 base + `e`
as **8 raw bytes** (zero-padded/truncated, no length prefix) + u8 length
(`c()` 1-byte allocator) + `f` bytes, merged via `byteMergeAll`;
bare-k3-bytes short-circuit needs **both** `e,f` null, and the shipped
wrappers pass `""`, so the tail is appended in practice.

`z.a(int[],byte[],..)`: portVersion<20 (or keys unset) takes the
cleartext branch → `a(w$d.a=1910, w$d.c=2BB1, …)` → `y`-queue →
`getService(1910).getCharacteristic(2BB1)` → `setValue` → log
`CharacteristicWrite` → `BluetoothGatt.writeCharacteristic`, with **no**
`setWriteType` anywhere in `z` (default = with-response). `false` return
→ `HandShakeReq:one--sendFail` + `send_fail` (the R5-S1 contention-run
signature — environmental, not construction).

## 5/6. Field classification

| Field | Class |
|---|---|
| header `[01][01 00]` | MECHANICALLY RECOVERED |
| `0x02` const @3 | RECOVERED present; semantics UNKNOWN (no reader) |
| agent @4 (`z.h()=0`) | RECOVERED stub; semantics UNKNOWN |
| stage @5, iff pv>=3 (0=k3, 1=j3) | RECOVERED (branch + driver literals) |
| token bytes | layout RECOVERED; value CREDENTIAL-DEPENDENT |
| portVersion `d` | RECOVERED as layout-only selector (never emitted) |
| j3 `e` (8 raw) / `f`+u8 len | layout RECOVERED; live values UNKNOWN/session-supplied |
| dest 1910/2BB1, with-response; waiters {1,2} | MECHANICALLY RECOVERED |
| `getBytes()` charset | INFERRED benign (tokens are hex-ASCII in practice) |

## 7/8. Comparison with R2/R2a — agreement, one stale prose

Independently confirmed against ledger §5.5: header, const, agent stub,
stage rule + driver literals, 16/32 widths, `'0'` pad, truncation,
255+trim, j3 tail shape + both-null rule + always-appended, write target
+ default type, waiters, guard-first. Digest `k3.enpkg_structure` /
`j3` entries and `test_k3_request_structure_matches_sdk_bytecode` agree.

Stale prose (docs-level only, frozen file NOT modified):
`docs/fixtures/handshake.json` stage `firstHandshake` still says
"k3 … NOT byte-exact … NOT implemented", contradicting its own
`k3_j3_evidence_table` BYTE-EXACT closure. Stronger evidence: this
audit + digest + conformance test. No wire-level contradiction found.

## 9/10/11. Digest, files, tests

- Evidence-digest changes: **none** — construction is already covered
  mechanically; driver-guard facts live in the local verifier instead of
  widening the pinned schema.
- Changed: `r5-s2/` (this README + verifier) + PROJECT.md row + ledger
  §13 note. No `reference/`, `emulator/`, fixture, or implementation
  change.
- Tests: 154 before → re-run after (no product code touched).

## 12. Remaining unknowns

Token alphabet/issuance/validity; device-side acceptance (accept-any is
POLICY); `0x02`/`z.h()` semantics; live `e/f` values; non-ASCII
`getBytes` edge. Response side (l3/x2) stands as R2.
