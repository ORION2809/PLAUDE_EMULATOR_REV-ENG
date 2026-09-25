# R5-S4: l3 response parser and post-j3 transition (READ-ONLY)

Verdict: **L3 AND POST-HANDSHAKE PATH AUDITED — agrees with R1/R2 throughout.**

`verify_l3.py` (ALL CHECKS PASSED) parses `build/evidence/javap` text.
No response fabricated, nothing implemented, nothing frozen touched.

## 1. Call chain (j3 waiter {1} → syncTime)

```text
2BB0 → waiter {1} → q.a(byte[]): u16@1==1 → new l3 → "old HandShakeRsp:"
→ status==0 → cache → old_protocol_ok → F=0 → c0() → sync_time stage
→ m() → waiter {4} → m7 → 1910/2BB1 → (n7 → bleBind path, per R1c/R2)
```

## 2/3. l3 layout (all MECHANICALLY RECOVERED from `l3.txt`)

Base `n.<init>` checks u16@1==1 else throws `Mismatch` — **no length
pre-check**, so <3-byte frames die in the reader. Java defaults first:
`timezoneMin=0, audioChannel=1, supportWifi/noNsAgc/isOggAudio=false,
versionType="V", version=0`.

| Off | Field | Width | Guard | Name source |
|---|---|---|---|---|
| 3 | status | u8 (`a`) | none | toString[0] + `d()` |
| 4..5 | portVersion | u16 (`b`) | none | toString[1] + `c()` |
| 6 | timezone | u8 (`a`) | none | toString[2] + `e()` |
| 7 | timezoneMin | u8 | len≥8 | toString[3] + `f()` |
| 8 | audioChannel | u8 | len≥9 | toString[4] + `b()` |
| 9 | supportWifi | u8==1 | len≥10 | toString[5] + `k()` |
| 10 | noNsAgc | u8==1 | len≥11 | toString[6] + `i()` |
| 11 | isOggAudio | u8==1 | len≥12 | toString[7] + `j()` |
| len-4 | versionType | char (`(b&255)→char`) | inside len≥12 | toString[8] + `h()` |
| len-3.. | version | **u24LE** (`b0\|b1≪8\|b2≪16`, `&255` masks) | inside len≥12 | toString[9] + `g()` |

Minimum viable frame: **7 bytes** (unguarded prefix); each guard
`if_icmplt 287` falls through to the end, keeping defaults. The nested
`len>=4` check is dead code (unreachable unless len≥12). Truncated
frames are therefore legal and meaningful — never an error.

## 5/6. Branches and `old_protocol_ok`

- status≠0 → `status_` + `s0$d.a(status)` fail. x2/l3 ctor throws (incl.
  `Mismatch`) → `s0$d.f` (exception targets 266/736). Unknown type →
  `callback_type_error_` + `s0$d.f`. byte0≠1 → **silent drop**.
- status==0 → caches (`q.m/n/o/p/q/r/s`, BleDevice audio/noNsAgc/ogg/
  version setters, `p7.l = audioChannel*80`) → `old_protocol_ok` — a
  success **stage label**, occurring exactly once; it assigns no state
  beyond the callback.
- Exact syncTime trigger: **`c0()`** (`syncTimeCount/attempt_` +
  `sync_time` stage → `m(c$b,c$c)` → waiter `{4}` → `m7`).

## 7/8. syncTime request + fan-out

`m7`: `currentTimeMillis/1000` (ldiv truncation) +
`TimeZone.getDefault().getOffset/60000` split by `idiv`/`irem 60`
(**both signed**, truncate-toward-zero) → `packHead` + u32 stamp + u8
tzH + u8 tzM = 9 bytes, proto 1 opcode 4. Post-l3 tail order: `c0()`
syncTime → `e()` battery (`K()`, opcode 9; `q.d()` confirms the old
`0x180F` branch when `H()` false) → getState `y2` (op 3) → `d4`
(op 16) → `s0$b` app fan-out.

## 9. R1/R2 comparison — independent confirmation, no conflicts

LE u24 version, guard ladder 8/9/10/11/12, status@3, `audioChannel`
default 1, end-read version block, opcode-4 9-byte syncTime with signed
tz pair — every prior claim re-derived here from raw javap. Emulator
`encode_l3` satisfies the recovered layout byte-for-byte (checked in
the verifier). No contradiction; no digest/fixture change.

## 10–13. Files, tests, unknowns

Changed: `r5-s4/` (README + verifier) + PROJECT.md row + ledger note.
Tests 154 → 154. Unknowns: live capability values (device-supplied);
`H()` gate internals (pv≥5 shape only); `s0$b` fan-out opcode;
`n7`-after-`m7` stands as R1c/R2.
