# R5-S8: independent sealed endpoints carrying FileList + SyncFile

Verdict: **INDEPENDENT SEALED FILE SYNC VERIFIED (in-process + Bumble GATT).**

`verify_sealed_filesync.py` (ALL CHECKS PASSED, 0 skipped) plus
`tests/test_r5_s8_sealed_filesync.py` (18 passed). Full suite 170 → 188,
all previous verifiers green. No contradiction, no frozen change, no
credential, no handshake.

## 1. What changed (and what did not)

- `emulator/plaudsim/sealed.py` (+`SealedLink`, `sealed_filelist_roundtrip`,
  `sealed_sync_roundtrip`): two independent `SealedSession` endpoints
  sharing only the synthetic J/K/L. R3 builders/parsers and the R5-S7
  seal/open primitive are reused byte-for-byte; no framing was
  reinterpreted.
- `tests/sealed_support.py`: `SealedTestPeripheral` now dispatches sealed
  GetState (op 3), FileList (op 26 via frozen `FileTable`) and SyncFile
  (op 28 via frozen `TransferSession`), sealing each response frame with
  the device session. Constructor signature is backward compatible
  (R5-S7 loopback tests pass unchanged).
- R5-S7's shared-loopback `sealed_getstate_roundtrip` is retained for
  compatibility; `SealedLink` supersedes it for multi-frame exchanges.

## 2. Counter behavior (the point of the sprint)

Host first TX = 2, device first TX = 2 (not 3); second TX = 3 per side;
RX counters advance independently (filelist leg leaves N_device = 2 and
N_host = 2; a further host request moves only N_device). Resets are
independent. R5-S7 properties (pre-increment, auth-inside, drop rules,
AEAD propagation, retry identity, reset) re-verified per endpoint.

## 3. Application proof

FileList: sealed p2 → device decrypt → frozen table paging → sealed q2;
host accumulator completes with totals/session/file-size/scene/attribute
all matching the deterministic fixture. SyncFile: sealed y6 → frozen
`TransferSession` → sealed HEAD + 3 type-2 DATA (offsets 0/32/64,
session gating intact) + TAIL; payload reassembles to the synthetic
96 bytes; CRC passes through. Sealing is outer transport: p2 12 B, q2
op 26, y6 15 B layouts are asserted unchanged inside the envelope.

## 4. Replay / tamper

Duplicate and stale sealed requests drop silently with N pinned and no
second response, in-process and over Bumble. Ciphertext flips (including
the seq-covering region) raise InvalidTag before any dispatcher runs
(peripheral `requests` stays empty, N stays −1). `open()` takes the wire
alone, so a sequence cannot be presented outside the authenticated input.

## 5. Bumble

Sealed FileList, sealed SyncFile (5 frames), replay-drop and
tamper-no-dispatch legs all run over the real `TwoDevices` virtual link.

## 6. Evidence discipline

Mechanically recovered (R5-S6): seal shape, M/N rules, drop semantics.
Inherited frozen (R3): all application layouts. Implemented: endpoint
pairing, sealed dispatch. Harness policy: synthetic J/K/L, pv 21,
loopback/independence test topology. Still unknown / NOT claimed: real
device J/K/L, real device first-TX value, ACK timing, and whether real
hardware accepts synthetic sealed traffic.
