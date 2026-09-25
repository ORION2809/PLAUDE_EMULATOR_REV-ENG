# R5-S9: synthetic modern handshake-to-filesync integration

Verdict: **MODERN SYNTHETIC HANDSHAKE-TO-FILESYNC INTEGRATED (Bumble GATT).**

`verify_modern_integration.py` (ALL CHECKS PASSED) +
`tests/test_r5_s9_modern_integration.py` (7 passed). Full suite 214 → 221,
all prior verifiers green. SYNTHETIC-ONLY throughout; proves integration
of recovered mechanics, nothing about real-device compatibility.

## 1. Architecture

`tests/sealed_support.py`: `ModernTestPeripheral(SealedTestPeripheral)`
adds a pre-key marker phase (v4 accumulate → FE11; w4 accumulate →
synthetic RSA-encrypt → FE12 → install fresh `SealedSession(J,K,L)`)
then reuses the frozen sealed dispatch via a refactored
`_dispatch_sealed` (no fork of `PlaudPeripheral`, which still refuses
pv≥20; no legacy change). Host driver (v4/w4 chunking, FE12 collect,
RSA decrypt, self-check) lives in the test module + verifier.
Deterministic synthetic material: 247-B snSignature (3 chunks),
runtime RSA-2048 keypair, P = SYNTHETIC_J/K/L + sealed `PLAUD.AI`
self-check region, 256-B ciphertext (100/100/56 FE12 chunks).

## 2-13. Results

FE10: marker/count/index/≤100 B + exact reassembly verified device-side.
FE20: clears the held-chunk buffer at transfer start (index 0); a stale
1-of-3 FE10 partial does not pollute the FE20 batch. (Boundary note:
per-frame clearing would break the SDK's own multi-chunk FE20 sends, so
index-0 clearing is INFERRED; device internals UNKNOWN.) FE11: literal +
waiter-formation verified; its count/index/payload bytes are harness
policy, labeled as such. FE12: dedupe/sort/concat, RSA/ECB/PKCS1Padding
decrypt, J/K/L split, self-check — all pass; duplicates skipped.
Sealed entry: M=1/N=-1 both sides; GetState req seq 2 → rsp seq 2
(independent counters, explicitly asserted). FileList/SyncFile: sealed
inners are byte-identical frozen R3 (`table.frames`, `transfer.frames`);
96-B file reassembled; CRC/opcodes/session/offsets unchanged. Replay:
future-accepted then stale+dropped, duplicates dropped, N pinned.
Tamper: ciphertext and seq-region flips → `aead_failure`, no dispatch,
N pinned. Bumble: every leg above crosses real 2BB1 writes / 2BB0
notifies (MTU 255); no transport fake-outs.

## 14-19. Regression / integrity / classification

221 tests green; R5-S2…R6-S2 verifiers green; `reference/**` clean (0).
MECHANICALLY PROVEN FROM SDK: framing, chunking, reassembly, RSA/J-K-L
shapes, seal semantics. EMULATOR-INTEGRATION PROVEN: the end-to-end
chain above. SYNTHETIC-ONLY: all key/credential/token material, FE11
payload shape, index-0 clear rule. UNKNOWN: real-device acceptance,
timing, FE11 live bytes, 512-B zeros question (untouched). Remaining
real-device gaps: everything requiring Plaud-issued credentials —
unchanged by this sprint by design.
