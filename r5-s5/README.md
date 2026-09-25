# R5-S5: modern (pv ≥ 20) handshake audit (READ-ONLY)

Verdict: **MODERN HANDSHAKE AUDITED — no contradiction with R2/R2a.**

`verify_modern.py` (ALL CHECKS PASSED) parses javap text; the three
`[jadx]` checks cover invokedynamic-lambda bodies javap cannot see and
are labeled as such. Nothing implemented, nothing frozen touched.

## 1/2. Call graph and state machine

```text
set_data_notify ok (both battery paths)
→ z.y()  [G.clear, H/I=0, J/K/L=null, M=1, N=-1]
→ pv>=20 ? q.n() : q.a()
q.n(): serial empty -> s0$d.f | snSignature null -> sn_signature_empty
  → v4 chunks {65041,65042} (fire-and-forget)
  → last-chunk reply 0xFE11 -> q.a0()  ([jadx]; else -> q.a())
q.a0(): pubkey null -> s0$d.f | w4 chunks {65042} -> last reply -> q.a()
device 0xFE12 chunks -> z$c accumulate -> RSA -> J/K/L -> self-check
  -> dispatch 65042
q.a()/d0()/c0()... IDENTICAL methods as legacy (no pv branch in q.a)
  → z.a seals every frame; z$c unseals every notify
```

So the ledger's formula is confirmed with one refinement: modern is
legacy handshake **plus** the v4/w4 pre-handshake **plus** the per-frame
seal — the post-key methods are shared verbatim, not reimplemented.

## 3/4. Frame tables

Outbound (all via the shared `z.a` → service 1910 / char 2BB1):

| Frame | Marker/op | Chunks | Waiters |
|---|---|---|---|
| v4 sn-signature | 0xFE10 (0xFE20 iff forceClear) | `(len+99)/100` × ≤100 B, count-then-index | {65041, 65042} |
| w4 RSA pubkey (PEM text) | 0xFE12 | same chunking | {65042} |
| k3/j3/control | proto 1, opcodes as legacy | single write | {1,2}/{1}/… |

Inbound: 0xFE11 → waiter `w$a.a`; 0xFE12 → dedupe + sort-by-`index&255`
+ concat `frame[4:]` once `size==count` (signed-count wedge preserved);
then everything through the seal.

## 5/6. Encryption boundary and key provenance (javap `z$c.txt`, `q5.txt`)

- Establish: `q5.a(userRSAPrivateKey_PEM, blob)` (RSA/ECB/PKCS1Padding,
  PKCS8) → null/`<56` aborts → `J=[0:32]` key, `K=[32:44]` nonce,
  `L=[44:56]` AAD → self-check `q5.b(rest,J,K,L)` == `"PLAUD.AI"`.
- Steady state TX: `M+1`, wire `[u32le seq][frame]` sealed by `q5.d`
  (ChaCha20-Poly1305, 12-B `IvParameterSpec`, `updateAAD`, Conscrypt
  fallback); first post-key frame carries seq 2.
- Steady state RX: `q5.b` open → `len≥4` → `seq=l2i(u32@0)`, drop iff
  `N≥seq` → strip 4 → recurse into plaintext dispatch. AEAD failure →
  `RuntimeException` rethrown, never a silent drop.
- Same key AND nonce reused every frame both directions (do not
  per-frame-derive).

## 7/8. Credential-dependent fields; R2 comparison

Blocked (cloud-issued, absent here): snSignature, RSA keypair (phone
never calls `KeyPairGenerator`), handshake token (same `q.a` guard
gates modern too), secret plaintext. Confirmed vs R2/R2a: marker
literals/order, chunk arithmetic, split bounds, magic, seq start,
static counters, rethrow rule, `z.y()` reset values. No contradictions.

## 10–13. Files, tests, unknowns

Added `r5-s5/verify_modern.py` + this README; PROJECT.md row; ledger
note. Tests 154 → 154. Unknowns: device chunk-ACK timing, secret blob
size (fits RSA-2048 in practice — INFERRED), live credential values;
lambda dispatch details rest on jadx, not javap.
