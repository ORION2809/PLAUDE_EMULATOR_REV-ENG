# R5-S6: modern sealed-frame sequence + transport semantics (READ-ONLY)

Verdict: **MODERN TRANSPORT SEMANTICS AUDITED.**

`verify_modern_transport.py` (ALL CHECKS PASSED) parses javap text,
with ordering asserted by bytecode offset. One correction to R5-S5
wording is folded in (see §7). Nothing implemented, nothing frozen
touched.

## 1. TX sequence state machine (M, process-global int)

- Storage/initial: static-init **M=1**; re-asserted by `z.y()` per
  set_data_notify success (with J/K/L=null, G.clear, H/I=0, N=-1).
  `e(int)`/`d(int)` are bare test setters.
- Gate per `z.a` call: pv≥20 **and** J/K/L all non-null → seal branch;
  else cleartext fallback to the same 1910/2BB1 write.
- Increment: `M=M+1` **before** sealing, once per encrypt-branch call
  (i.e. per logical frame handed to `z.a`; the y-queue stores sealed
  bytes, so a transport retry can never re-seal or double-increment).
  No overflow guard — int wraps silently (practically unreachable).
- Layout: `new byte[len+4]`; LE32 seq via `a(J)[B` at [0:4];
  frame at [4:]; the array ref rides the stack into
  `q5.d(buf, J, K, L)`.
- First sealed frame after `z.y()` carries **seq 2** (conditional on no
  intervening sealed call — markers pre-key always take cleartext).
- Marker (FE10/FE20/FE12) and legacy frames never consume seq: the only
  M writers are static-init, the setter, `z.y()`, the `z.a` seal branch,
  and OTA-only `z.d`. Resets happen **only** via `z.y()` (per
  connect/handshake-restart through `b0`); disconnect clears the queue
  but not M/N; secret install touches neither.

## 2. RX sequence state machine (N)

Init −1 (static + `z.y()`). Per 2BB0 notify with keys set: `q5.b`
open → decrypted `len≥4` → `seq=l2i(u32@0)` (signed narrow) →
**drop silently iff `N≥seq`** (equal and lower rejected; goto method
end, no response) → else `N=seq`, strip 4, recurse into plaintext
dispatch. R5-S5's "drop iff N≥seq" stands but was incomplete: the
**update must be stated** — accept means `N:=seq`.
Consequences: the first accepted device seq may be 0 (N=−1); a device
counter ≥0x80000000 narrows negative and every later frame drops;
seq 0/1 have no special treatment; AEAD throw precedes the seq read,
so failures leave N untouched.

## 3. AEAD contract — sequence IS authenticated (proven)

`q5.d(data,key,nonce,aad)`: key≠32 → error; nonce≠12 → error;
`updateAAD(aad)` iff aad non-empty; `doFinal(plaintext)`. The seq
prefix is inside the sealed plaintext (not a header beside it), under
the same AAD=L — adjacency was not assumed; the stack accounting
(`dup`-kept array ref → arg0) proves it. `q5.b` mirrors with a
ciphertext≥16 gate. Algorithms: RSA/ECB/PKCS1Padding (PKCS8 privkey),
ChaCha20-Poly1305, 12-B IvParameterSpec, Conscrypt fallback.

## 4. Write/ACK semantics (SDK vs device)

SDK transport: `z.a` returns the **queue-add** boolean; one GATT write
outstanding (pump on write-callback); default with-response (no
`setWriteType` in `z`); `writeCharacteristic()==false` → error −99 to
the live request (pump still returns true; no transport retry).
Application ACK = the ordinary (sealed) response frame matched by
waiter opcode — no separate transport ACK exists (no ACK primitive in
`z`). Replies are required only by each request's own callback
registration, never by the transport before the next write.

## 5. Chronology (P=plaintext, S=sealed, Q=sequence-bearing)

connect → notify-ok → `z.y()` → FE10/FE20 chunks (P, 2BB1) →
FE11 (P, 2BB0) → w4 chunks (P, 2BB1) → FE12 chunks (P, 2BB0) →
J/K/L install → k3 (S+Q, seq 2) → sealed l3 → … (S+Q, seq 3, 4, …).
No pre-sealing frame can alter M/N (single update sites, both
post-key branches).

## 6/7. Reconciliation; remaining unknowns

Confirms ledger §4.3, digest, and R5-S5 throughout; refines "drop iff
N≥seq" with the explicit update rule. No contradictions. Changed:
`r5-s6/` + PROJECT.md row + ledger note. Tests 154 → 154.
Unknowns (device-dependent): device-side seq start/width, ACK timing,
secret-blob sizing; unreachable int-overflow behavior.

## 8. Complete modern session timeline (plaintext/sealed per frame)

Mechanically supported order (R5-S5 call graph + this sprint's TX/RX/GATT
tracing; `q.a`/`d0`/`c0` are the identical methods as legacy — no pv
branch in `q.a` itself, sealing happens one layer down in `z.a`):

```text
GATT connect → requestMtu(255) → onMtuChanged → discoverServices
→ set_notify ok (both battery paths; pv>=5 skips 180F, pv 7 takes
  "new battery service")
→ set_data_notify ok → z.y() [G.clear, H/I=0, J/K/L=null, M=1, N=-1]
→ pv>=20 ? q.n() : q.a()
  q.n(): v4 chunks, marker 0xFE10 (0xFE20 iff forceClear), P, 2BB1,
         waiters {65041,65042}, chunking (len+99)/100 x <=100 B
  ← last-chunk reply 0xFE11, P, 2BB0, waiter w$a.a
  → q.a0(): w4 chunks, marker 0xFE12, RSA pubkey PEM TEXT, P, 2BB1,
            waiters {65042}
  ← device 0xFE12 chunks, P, 2BB0 → dedupe/sort-by-index&255/concat
    frame[4:] once size==count → q5.a RSA decrypt → P[0:32]=J key,
    P[32:44]=K nonce, P[44:56]=L AAD, P[56:] self-check q5.b=="PLAUD.AI"
→ J/K/L install (secret path touches only I/H/G/J/K/L, never M/N)
→ k3 first_handshake, S+Q seq 2, 1910/2BB1, waiters {1,2}
  (same empty-token guard as legacy gates modern too)
→ sealed l3 (status 0 → old_protocol_ok → c0())
→ sealed x2 GetSSN → d0() iff r3.e() → sealed j3 (waiter {1})
→ sealed l3 → c0() → m7 syncTime (waiter {4})
→ sealed battery (op 9) → getState (op 3) → d4 (op 16) → s0$b fan-out
→ file list (op 26) → syncFile (op 28) → type-2 DATA → tail (op 29)
```

Every pre-key frame is plaintext and sequence-free (markers pre-key take
the cleartext branch; M/N untouched). Every post-key frame is sealed
S+Q with monotonically increasing M (host) / N-gated RX (device).
No pre-sealing frame can alter M/N (single update sites, both post-key
branches). Markers never consume sequence: the only M writers are
static-init, e(int) setter, z.y(), the z.a seal branch, and OTA-only
z.d.

## 9. Three layers (credential / mechanics / device)

Layer A — credential issuance (genuinely needs Plaud backend, absent
here): partner access token (JWT sub), snSignature (…/sdk/sn-sign),
RSA keypair (…/sdk/gen-key returns private key; phone never calls
KeyPairGenerator), encrypted secret plaintext P, handshake token value,
j3 e/f live values, secret-blob sizing.

Layer B — protocol mechanics (reconstructed, credential-free): FE10 /
FE20 / FE11 / FE12 framing + chunking + reassembly, RSA-decrypt call
shape, J/K/L split + PLAUD.AI self-check, ChaCha20-Poly1305 envelope
with seq-inside-AEAD, M/N state machines, k3/j3/l3/x2 layouts, GATT
1910/2BB1/2BB0 + CCCD + with-response + queue + waiters, file-list /
transfer / resume / stop / delete layouts.

Layer C — device behavior (emulator must reproduce): l3 status +
capability fields, x2 SSN (must equal advertised serial), syncTime /
battery / getState / getStorage answers, file-list paging with
echo-stamp + stride-by-pv + accumulation gates, syncFile HEAD / type-2
DATA / TAIL + resume-from-cursor + stop→restart, delete, unsolicited
battery push. All of Layer C on the legacy (pv 7, cleartext) path is
NOT blocked by authentication — see gap matrix.

## 10. Synthetic/local testability (no backend needed)

Yes for the sealed machinery itself: with synthetic J/K/L (32/12/12
random bytes), M=1/N=-1 reset, and synthetic handshake/device/file
payloads, the emulator can internally exercise wrap/unwrap
(`handshake.wrap_envelope` round-trips AES-GCM-independent ChaCha
vectors), ReplayWindow accept/drop, and the full post-handshake
command → file-list → transfer → synthetic-bytes chain on the
cleartext legacy path today (V1 test does exactly this). What synthetic
material CANNOT do: produce a credential the real SDK will accept
(token, snSignature, RSA-signed secret) — so a real-AAR sealed session
against the emulator remains blocked at `bind_token_empty` (R4-S3/R5-S1
boundary), while an emulator-internal sealed-session self-test is
unblocked. Required for the latter: a sealed-mode peripheral harness
(synthetic J/K/L install + M/N wiring into profile.py) — scoped as the
next slice below, not built here.

## 11. Emulator gap matrix (ledger vs implementation vs proof)

Recovered = ledger DIRECT/SOURCE-DERIVED section; Implemented = wired
into `profile.py` dispatch (helpers alone do not count); Verified =
test/verifier name; Blocked = needs cloud credential.

| Capability | Recovered | Implemented | Verified | Blocked |
|---|---|---|---|---|
| GATT 1910/180F, 2BB0, 2BB1, CCCD | yes §1.1 | yes `profile.PlaudPeripheral` | `test_gatt_uuids_match_the_sdk_constants`, V1 session | no |
| Battery op 9 + push | yes §5.4 | yes `PlaudBatteryState`+`push_battery` | `test_battery_status_opcode_9_round_trip`, `test_battery_can_be_pushed_unsolicited` | no |
| GetState op 3 | yes §5.1 | yes | `test_get_state_exchange`, z2 conformance | no |
| GetStorage op 6 | yes §5.3 | yes | `test_get_storage_exchange`, `test_free_and_total_are_not_swapped` | no |
| SyncTime op 4 | yes §5.2 | yes (BOUND advance) | `test_sync_time_exchange`, signed-tz tests | no |
| FE10 / FE20 | yes §2 | helper only (`handshake.pack_marker_frame`, unwired; `_on_command_write` rejects non-type-1) | helper tests only | yes (needs snSignature) |
| FE11 / FE12 | yes §2 | no (never emitted/answered) | helper tests only | yes (pv>=20 RSA path) |
| RSA secret + J/K/L | yes §4.3 | slice-only (`split_secret_package`), no decrypt | slice conformance only | yes (`userRSAPrivateKey` cloud-issued) |
| Sealed transport + seq M/N | yes §4.3 | no (`wrap_envelope_plaintext` only; `PORT_VERSION=7`, refuse ≥20; `ReplayWindow` unwired) | helper tests only | yes for sealed; no for legacy (no seq cleartext) |
| GetSSN x2 / j3 / l3 | yes §5.5 | yes (`_get_ssn`, `_handshake`+`encode_l3`; j3 tail parsed, no separate d0 driver) | `test_x2_*`, `test_k3_*`, `test_l3_*`, l3 guard/LE tests | no (accept_any_token POLICY) |
| File list op 26 | yes §5.6 | yes (`FileTable.frames`, echo-stamp, stride 8/9/10) | accumulator/pages/offset-11/stride/scene-attr tests | no |
| Transfer start/DATA/tail/resume/stop/delete | yes §5.7–5.10 | yes (`TransferSession`, type-2 both branches, EMPTY_PACKAGE, CRC-opaque) | full-sequence/resume/gap-recovery/offset tests | no |
| Audio packet path (Opus 16k/20ms/80B, Ogg quirks, 512B PLAUD.AI header, raw ChaCha) | yes §8 SOURCE-DERIVED | no (only `SECRET_MAGIC` const; file bytes opaque) | none | no for synthetic DATA; yes for authentic encrypted audio |

POLICY (not device claims): `PORT_VERSION=7` / refuse ≥20
(`profile.py:78`); `accept_any_token=True` default; DATA 32 B/frame;
no pacing; second y6 replaces session.

## 12. Minimum next implementation slice (R5-S7 candidate)

Sealed-mode self-test harness WITHOUT touching the legacy peripheral
or any credential path: synthetic J/K/L install + M pre-increment
wiring + N accept-and-set wiring + wrap/unwrap round-trip through
`profile.py` on a test-only pv≥20 peripheral subclass, asserting
seq-2-first, N>=seq drop, AEAD-failure RuntimeException, and one
sealed getState round-trip with synthetic bytes. Independently
testable, no backend, no GATT change, no legacy regression surface.
Full sealed file-transfer and audio come after, not in the same slice.

## 13. Integrity

- Project tests: 154 passed (`PYTHONPATH=emulator:reference/upstream/bumble
  python3.11 -m pytest tests/ -q`).
- Verifiers: r5-s2, r5-s3, r5-s4, r5-s5, r5-s6 ALL CHECKS PASSED
  (python3.11; system python3.9 cannot import bumble — environmental,
  not a failure).
- `reference/**` clean (plaud-sdk-public 0, bumble 0 modified).
- Changed: `r5-s6/` only (+ PROJECT.md row + ledger §13 note, pre-existing).
  No `emulator/`, `tests/`, fixtures, frozen R1–R4 evidence touched.
- Bumble suite: not run (vendored substrate, untouched; transport already
  proven by R4-S2/R5-S1 live runs).
