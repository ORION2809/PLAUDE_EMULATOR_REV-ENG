# R5-S10: FE11 solicitation + handshake timing audit (read-only)

Verdict: **FE11/TIMING MECHANICALLY AUDITED (javap throughout).**

`verify_fe11_timing.py` (ALL CHECKS PASSED) + one focused device-side
test (`test_no_fe11_until_all_v4_chunks_received`). Zero emulator
changes: the R5-S9 synthetic peripheral is already consistent with every
newly recovered fact. `reference/**` and all frozen records untouched.

## 1-4. FE11 structure, dispatch, validation, timing

- Marker literal 65041 solicited from the device; symbolic SDK name
  `STICK_PREHANDSHAKE_CNF` (from the unknown-marker log string).
  MECHANICALLY PROVEN (javap v4/q/z$c).
- Payload: NO SDK read of any byte past the u16le@0 marker exists --
  count/index/payload bytes are UNKNOWN-UNCONSTRAINED (nobody reads
  them). Our FE11 shape (count 1/index 0/empty) stays HARNESS POLICY.
- Origin: device, only meaningful after the final v4 chunk (see below).
  No dynamic value evidenced anywhere on the FE11 path.
- Dispatch key: single int 65041 via `z.e(z,I)` → one `s$b` call, then
  return; missing waiter logs `File Sync Callback is Null`.
  MECHANICALLY PROVEN (javap z.txt 1144-1189, z$c 583-625).
- Validation: the s$b lambda checks ONLY `b(bArr2,0)==65041`, and only
  for the last-chunk registration (`i3==length-1`); 65041 → `a0()`,
  anything else → `a()` (skips RSA entirely). Non-last registrations
  log and ignore. a0()'s own waiter checks NOTHING and goes to `a()`.
  MECHANICALLY PROVEN (javap q synthetic bodies + [jadx] cross-read).
- Completion: v4 chunks are queued fire-and-forget (no inter-chunk
  wait); FE11 is solicitation, not ACK (no success/failure callback, no
  retry at this layer). FE11 CAN arrive before `q.n()` returns (async
  notify vs sync queue loop). SOURCE-DERIVED.
- Duplicate FE11: no dedup evidenced -- the retained last-chunk waiter
  fires again, re-running `a0()` (w4 retransmit). SOURCE-DERIVED.
- Previous-waiter clearing: no explicit removal call; instead the
  newest-match-wins lookup EVICTS older duplicate entries as a side
  effect (FE11 lookup keeps one n() waiter, FE12 lookup keeps a0()'s).
  MECHANICALLY PROVEN.
- Timing bounds: transport per-write 1000 ms (-98/-99); global handshake
  Handler 10 s default / 30 s, `TIME_OUT(-2)` via `q$h what==1`,
  re-armed by `a(w)` from send callbacks, cleared on getSsn/sync-time
  success. No `postDelayed` near the pre-key path. FE12 is sent
  immediately inside `a0()`; post-check `a()` immediately inside the
  FE12 callback. MECHANICALLY PROVEN.

## 5-7. FE20 clearing + state machine

- The host reassembly accumulator G is touched ONLY in `z.y()`
  (connection lifecycle) -- never in `n()/a0()/v4`, never for FE20.
  FE20 receipt handling lives in firmware: NO code in evidence.
  Host-side sending rule (h flag → 65056 vs 65040, no buffer reset)
  MECHANICALLY PROVEN; device-side effect UNKNOWN; R5-S9's index-0
  clear rule stays HARNESS POLICY + INFERRED (compatible, not proven).
- `h=true` ⟺ recovery entry point (`recoveryConnectBleDevice` vs
  `connectBleDevice`, template DeviceManager.kt:1111-1116).
  SOURCE-DERIVED.
- State table (SDK behavior): z.y-reset → `n()` sends v4+waiter{41,42}×N
  → FE11(65041) on last-reg → `a0()` sends w4+waiter{42}×M → FE12
  accumulate/dedupe/sort → RSA (`userRSAPrivateKey` null → logged
  return; short → `abort handshake`; self-check mismatch → SILENT
  return) → dispatch 65042 → `a()` (k3). Any stall → global TIME_OUT.
  Emulator/test-harness rows: unchanged from R5-S9 (consistent).

## 8-13. Changes / tests / integrity / unknowns

- Changes: NONE to emulator/product code (preferred outcome per §7).
- Tests: +1 focused (`no FE11 before final chunk`, device-side mirror
  of the last-registration gate); full suite + all verifiers green.
- Integrity: `reference/**` clean; frozen records untouched; no
  contradiction found (R5-S9 wording stands; this sprint upgrades
  none of its policies to facts).
- Remaining unknowns: FE11 live payload bytes (unread by SDK --
  unresolvable from this evidence by construction), device-side FE20
  effect, live chunk-ACK timing, real J/K/L values.
