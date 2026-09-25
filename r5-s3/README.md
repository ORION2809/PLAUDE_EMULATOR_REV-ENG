# R5-S3: second-handshake (j3) mechanical audit (READ-ONLY)

Verdict: **J3 STRUCTURE AUDITED — no contradiction with R2/R2a.**

`verify_j3.py` (ALL CHECKS PASSED) parses `build/evidence/javap` text;
frame construction itself stays owned by `r5-s2/verify_first_handshake.py`.
No credential used, none needed: every byte below is credential-independent
except the token value carried over from k3.

## 1–2. d0 trigger and call chain

`q.d0()` has exactly one caller: the **type-2 (x2) branch** of the shared
`q.a(byte[])` dispatcher — parse x2 → log `getSsnRsp ssn:` → `b(ssn)`
(checkSn) → `d0()`, sequential and unconditional. Entry: `twoHandshake`
log → proceeds **iff** `r3.a().e()` (singleton flags `f||h`, semantics
UNKNOWN); false takes a silent return (no send, no fail callback).

```text
2BB0 notify → z$c → waiter {1,2} s$b → q.a(byte[]): u16@1==2 → x2 → d0()
→ waiter {1} → z.a → 1910/2BB1 → l3 → status==0 → c0() → syncTime
```

## 3–9. j3 construction (all MECHANICALLY RECOVERED)

`new j3(z.h()=0, 1, g, portVersion, i, j)` → `enPkg()` = k3 base
(`[01][01 00]`, proto 1, opcode 1, const `0x02`, agent 0, stage **1**,
token padded/truncated as R5-S2) + tail:

| Order | Field | Width | Source |
|---|---|---|---|
| +0..+7 | `e` bytes | 8 raw, zero-padded/truncated, **no** length prefix | `bipush 8` scratch + capped copy loop |
| +8 | `len(f)` | u8 (`c(I)` 1-byte allocator, proven) | `TntBleCommUtils.c` |
| +9.. | `f` bytes | exact, unpadded | `getBytes()` |

Both-null short-circuit: `e==null && f==null` → bare k3 bytes (stage 1
still emitted when pv≥3). Either non-null → full tail merge.

## 10–13. e/f provenance

`t3.a(device,p2,p3,p4,…)` stores `g=p2, i=p3, j=p4` (putfield order
verified) → `j3.e=i, f=j`. `e`'s value is the `deviceToken` argument of
`PlaudDeviceAgent.connectBleDevice` (Kotlin signature); the shipped
template calls the single-arg overload whose `$default` forces `""`, and
`f` is always `""` from this caller. So in practice the tail is 9 zero
bytes — but that is caller behavior (RUNTIME/template OBSERVED), not a
wire rule. `e/f` meaning to the device: UNKNOWN (fields unnamed in
bytecode; `devToken`/`userName` labels are dataflow-derived, and no
device-side consumer is in evidence).

## 14–16. Encoding, padding, encryption

Strings via platform-default `getBytes()` (benign: values are ASCII in
practice — INFERRED). `e` truncated/padded to 8; `f` exact with u8
length; `f=""` → single `0x00`. `q.d0` itself never encrypts: `z.a`
takes the cleartext branch whenever pv<20 (or keys unset) and the
ChaCha branch otherwise — so j3 is **cleartext on the legacy path**,
sealed on pv≥20, same call.

## 17–20. Write target, response, success/failure

Write: same `z.a` path as k3 → service 1910 / char 2BB1, default
with-response; `false` → `two--sendFail` + `send_fail` + `s0$d.g`.
Response re-enters the shared dispatcher, waiter `{1}`: `l3.d()` status
at the fixed offset — nonzero → `status_` + `s0$d.a(status)`; zero →
cache capability fields (`m,n,o,p,q,r,s`, BleDevice setters,
`p7.l = audioChannel*80`) → `old_protocol_ok` → `c0()` → `m()` builds
**m7 (opcode 4) = syncTime**, then battery `K()` (opcode 9), getState
`y2` (op 3), `d4` (op 16), `s0$b` fan-out. Other failure paths:
unknown type → `callback_type_error_` + `s0$d.f`; x2-parse exception →
`GetSsnRsp:fail` + `s0$d.f`; checkSn Fail → `s0$d.d` logged but `d0`
still runs (sequential calls).

## Comparison / files / tests / unknowns

Confirms ledger §5.5 + fixture evidence table + digest `j3` entry +
`test_k3_request_structure_matches_sdk_bytecode` direction; no
wire-level contradiction (the R5-S2 fixture-prose staleness stands
reported, file untouched). Digest unchanged (driver facts live in the
local verifier). Changed: `r5-s3/` only + PROJECT.md row + ledger note.
Tests 154 → 154. Unknowns: token value, `e` semantics to the device,
`r3.e()` flag meanings, `s0$b` fan-out opcode.
