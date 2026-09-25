# R5-S7: first modern sealed end-to-end slice (SYNTHETIC, test-only)

Verdict: **MODERN SEALED GETSTATE ROUND-TRIP VERIFIED, over Bumble GATT.**

`verify_modern_sealed_roundtrip.py` (ALL CHECKS PASSED, 0 skipped) plus
`tests/test_r5_s7_sealed.py` (16 passed). Full suite 154 → 170, no legacy
regressions. Nothing credentialed, no backend, no live handshake.

## 1. Recovered protocol vs synthetic test mode vs live authentication

**Recovered (SDK does this; R5-S5/R5-S6, ledger §4.3):** ChaCha20-Poly1305
over `[u32le seq][frame]` with fixed key J / nonce K / AAD L; M reset 1,
pre-increment per seal (first sealed seq 2); N reset −1, accept-and-set,
silent drop iff N ≥ seq; AEAD failure propagates with N untouched; the
same nonce every frame both directions; GetState layout opcode 3.

**Synthetic (this sprint chooses this; the SDK does not fix it):** the
J/K/L values (`SYNTHETIC-J-KEY-0123456789ABCDEF` / `SYN-TEST-NON` /
`SYN-TEST-AAD`, deterministic ASCII, lengths 32/12/12), the declared
port version 21, and the loopback sharing of one `SealedSession` between
test client and test peripheral — so the request seals at seq 2 and the
response at seq 3. On a real link host and device hold independent
counters and the device-side start is UNKNOWN (R5-S6); the sharing is a
documented loopback simplification, not a protocol claim.

**Live (still needs legitimate Plaud material):** partner token,
snSignature, RSA keypair, FE10/FE20/FE11/FE12 exchange, secret P. The
synthetic fixture represents channel state *after* secret establishment.

## 2. Files

- `emulator/plaudsim/sealed.py` (new): `seal_raw`/`open_raw` (q5.d/q5.b
  shapes incl. key/nonce/ciphertext gates), `SealedSession` (M/N +
  `seal_calls`), `sealed_getstate_roundtrip`, synthetic fixture. No
  bumble import; legacy modules untouched.
- `tests/sealed_support.py` (new): `SealedTestPeripheral` test double —
  real Bumble ATT/GATT (1910/2BB0/2BB1), sealed dispatch for GetState
  only, silent drop on duplicate/stale, log-and-ignore on AEAD failure.
- `tests/test_r5_s7_sealed.py` (new, 16 tests): fixture, RFC 8439 §2.8.2
  vector, cross-library (pycryptodome, importorskip) vector, first-seq-2,
  2→3, retry identity, dup/stale/forward, AEAD+N, short-skip,
  auth-prefix, reset, in-process round-trip, 2 Bumble GATT tests.
- `r5-s7/verify_modern_sealed_roundtrip.py` (new): 20 standalone checks
  incl. the Bumble GATT leg (SKIP only if bumble unimportable).
- PROJECT.md row + ledger §13 note (one line each).

## 3. Crypto proof (non-circular)

`seal_raw` reproduces the published RFC 8439 §2.8.2 AEAD vector
byte-for-byte (ciphertext+tag) and a second library agrees where
installed. This pins the key/nonce/AAD/plaintext roles independently of
our bytecode reading. The sequence-is-inside proof: `open()` takes the
wire alone (no external seq parameter exists), the same frame seals
differently at seq 2 vs 3, and any ciphertext flip raises InvalidTag.

## 4. Bumble integration

Exercised, not stubbed: central writes the sealed request to 2BB1
with-response over a real `TwoDevices` virtual link; the peripheral
decrypts, RX-validates, dispatches opcode 3, seals the GetState response
and notifies on 2BB0; the client decrypts and RX-validates. A replayed
write over the same link yields no second response (`drops == 1`).

## 5. Remaining gaps (not this slice)

Sealed dispatch lives in the test double, not `PlaudPeripheral` (which
still refuses pv ≥ 20 — unchanged, correct); only GetState is wired
(no file list / transfer / audio in sealed mode); no FE/RSA/handshake;
independent host/device counters not modelled (shared loopback instead).
Suggested next slice: sealed file-list + syncFile with synthetic bytes
on the same harness.
