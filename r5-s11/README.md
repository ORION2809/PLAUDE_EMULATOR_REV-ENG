# R5-S11: uncertainty isolation + capture-ready traces (synthetic only)

Verdict: **MODERN UNCERTAINTY ISOLATED AND TRACE-READY.**

`verify_uncertainty_trace.py` (ALL CHECKS PASSED) +
`tests/test_r5_s11_trace.py` (9 passed). Full suite 222 → 231, all
verifiers green. No proven fact altered; no unknown upgraded.

## 1-6. Audit counts (33 behaviors, `r5-s11/uncertainty_matrix.json`)

15 SDK_PROVEN · 3 EMULATOR_INTEGRATION_PROVEN · 4 INFERRED ·
5 HARNESS_POLICY · 6 UNKNOWN. Load-bearing rows: FE11 marker/ solicitation
(SDK_PROVEN) vs FE11 payload bytes (UNKNOWN); FE20 host marker rule
(SDK_PROVEN) vs device-side effect (UNKNOWN); host M=1 first-seq-2
(SDK_PROVEN) vs device TX start (UNKNOWN); sealed layouts (SDK_PROVEN)
vs sealed carriage (INTEGRATION); replay/tamper end-to-end
(INTEGRATION). No UNKNOWN row claims safe emulation as plain true.

## 7-8. Isolated policy (`ModernProfile`)

`fe20_clear_policy` (`index_zero` default, `never` supported, anything
else raises instead of guessing), `fe11_chunk` (default empty),
`device_tx_start` (default 1, device side only -- host M=1 stays fixed
as proven). Defaults reproduce R5-S9 byte-for-byte; all prior tests
pass unmodified. SDK-proven behavior is deliberately not configurable.

## 9-12. Trace schema, capture, replay, comparison

`tests/protocol_trace.py`: `TraceEvent(ts/direction/phase/
characteristic/raw/parsed/result/classification)` with raw authoritative
(never mutated; keyless re-parse marked `keyed` when opened with keys so
replay never downgrades stored knowledge); `Trace` JSON export (refuses
non-`SYNTHETIC_EMULATOR_TRACE` on import); `compare_traces` with kinds
BYTE_DIFFERENCE (length/marker/content detail), ORDERING_MISMATCH,
TIMING_DIFFERENCE (explicit tolerance), PARSER_UNKNOWN (context on real
divergence only -- silent on identical traces, never a mismatch verdict).
The full R5-S9 scenario emits PREKEY/RSA/SEALED/FILESYNC events; golden
replay preserves order/bytes/phases/directions; unknown bytes
(`\x99…`) survive export/import untouched.

## 13-17. Diffs / files / integrity / unknowns

No diffs on identical traces; tamper → BYTE_DIFFERENCE; clock shift →
TIMING_DIFFERENCE (suppressed by tolerance); truncation →
ORDERING_MISMATCH. Changed: `tests/sealed_support.py` (profile +
capture-only trace hooks), `tests/protocol_trace.py`,
`tests/test_r5_s11_trace.py`, `r5-s11/` (matrix, verifier, README),
one PROJECT.md row. `reference/**` clean; frozen records untouched;
R5-S9 semantics unchanged (defaults + full suite green). No
contradictions. Remaining unknowns: the 6 UNKNOWN matrix rows --
resolvable only by a legitimate device trace, which this harness can
now ingest via the same schema.
