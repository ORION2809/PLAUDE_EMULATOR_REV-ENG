# V2 fault-injection matrix (BLE file transfer)

**Claim tested:** the emulator "survives a fault-injection suite without
corrupting a transfer". Every cell below is a real Bumble `TwoDevices` GATT
session: `plaudsim.faults.FaultyPeripheral` serves a file while an injector
damages its outbound frames, and a receiver model derived from the Android
SDK's bytecode (`tests/fault_support.py`) drives the transfer the way the SDK
would. The invariant asserted for every cell is `outcome_of`: **the
reassembled bytes equal the served file byte-for-byte, or the client model
reports a detected failure -- never a silently corrupted file.**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_v2_receiver_model.py tests/test_v2_fault_matrix.py -q -p no:cacheprovider --timeout=120`
(48 model tests + 61 matrix cells; the cells write `build/v2-fault-matrix.json`,
from which the table below was rendered).

## 1. What "complete" means -- the R7-S13 correction

The genuine Android SDK, run on an AVD against this emulator
(`r7/r7-s13-recording-pull.md`), **completes a transfer only on the
EMPTY_PACKAGE sentinel** `[02][sid][FF FF FF FF][00][code]` sent after the
last DATA frame and before the TAIL:

| runtime fact | runs | model rule |
|---|---|---|
| HEAD . DATA . TAIL never completes (request stays pending; export stuck at 99 %; op-queue later reports -98) | 1, 1b, 2, 9 | R9: the TAIL fires `bleSyncFileTail` and nothing else; R10: no timer is armed |
| HEAD . DATA . EMPTY(code) . TAIL completes (`bleDataComplete` / export `onComplete`) | 3, 4b, 8, 10, 6b-6d | R2: `v3.finish(code)` at ALL.txt:69100-69108 |
| EMPTY after TAIL never completes | 7 | R12: the TAIL (non-sticky opcode 29) unregistered the receiver; the sentinel finds "File Sync Callback is Null" (ALL.txt:72342) |
| codes 0 and 1 both complete on the normal path | 4b, 8 | R2: the code is inspected only on the while-recovering branch |
| on a gap: `stopSync` (29) immediately, `syncFileStart(cursor)` ~35 ms later after the a7 | 6b, 6c, 6d | R6: the `fileSyncLossPkgStop` runnable writes z6 synchronously; the 5 s runnable is the stall path (R10) |
| EMPTY with code != 1 while a recovery is in flight -> `---isPacketLossStopSync---` -> second restart | 6 | R2 while-H branch (ALL.txt:69041-69099) |
| device keeps draining the abandoned stream -> second restart -> **corrupted export** (fixture[0:4800] ‖ fixture[3200:11271]) | 6 | not reproducible at q$a level (P8) -> reported as `corruption_risk` |
| device aborts the old stream on the new `syncFileStart` -> exactly one restart, byte-exact | 6b, 6c, 6d | the paced `stream_in_task=True` device of every gap cell |

Consequences for the model (`tests/fault_support.py`):

* `TransferResult.complete` is set **only** by the `finish` action (R2). A
  TAIL sets `tail_seen`/`tail_crc` and nothing else.
* A TAIL with all the bytes but no sentinel leaves the request pending: q$a
  arms no timer (R10), so the driver's idle timeout (P2) reports
  `stalled_after_tail`. That is the SDK-level "never completes" of runs 1/2/9;
  the app-level -98 that eventually followed on the AVD is not modelled.
* After `finish` the receiver is still registered (nothing in R2 unregisters
  it), so the driver keeps dispatching until the TAIL callback (P7).
* The size check (P4) moved from the TAIL to the finish: a lost last frame
  yields `finish_short`, and the app-level resume (P5) starts from the cursor.
* Emission modes: gap cells run the base peripheral's task streaming
  (`stream_in_task=True, response_pacing_s=0.004`) so a z6 or a new y6 aborts
  the stale stream and the recovery converges with one restart (runs 6b/6d).
  The atomic mode (whole stream emitted inside the y6 write handler, nothing
  can be abandoned) is kept for cells without recovery and, for a gap, is
  run as exactly one cell -- `old_stream_not_abandoned` -- whose expectation
  is the run-6 behaviour, not a recovery.

## 2. The matrix

Outcome vocabulary: `recovered` = byte-exact through the SDK's own paths
(finish fired, no app help); `recovered_by_app_resume` = byte-exact only after
the app-level size check + resume (P4/P5); `detected` = the model reported a
failure and the bytes are a prefix of the served file; `corruption_risk` =
the trace carries the run-6 signature (more than one restart, one of them
caused by a frame of the stream the client had abandoned): the receiver
model's bytes are exact but the genuine SDK's export layer corrupted its
output under this trace, so the matrix refuses to call it recovered.
"observed" lists: outcome, bytes, whether `finish` fired, whether the tail
callback fired, the failure label, SDK restarts and z6 stop-syncs.

| cell | injected fault | expected | observed | evidence class | citation |
|---|---|---|---|---|---|
| `clean_default_sequence[atomic]` | none (HEAD . DATA x32 . EMPTY(0) . TAIL) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | RUNTIME_PROVEN + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (runs 3/4b/10); q$a rules R2/R9 (ALL.txt:68962-69395) |
| `no_empty_package[atomic]` | `empty_package_code=None`: HEAD . DATA . TAIL, no sentinel | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | RUNTIME_PROVEN + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (runs 1/1b/2/9); q$a rules R9/R10 (ALL.txt:68962-69395); policy P2 |
| `empty_after_tail[atomic]` | sentinel moved after the TAIL (`EMPTY_PACKAGE_AFTER` on TAIL) | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | RUNTIME_PROVEN + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (run 7); q$a rules R12 (ALL.txt:68962-69395); policy P2 |
| `empty_code_1[atomic]` | `empty_package_code=1` | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[1] | RUNTIME_PROVEN + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (run 8); q$a rules R2 (ALL.txt:68962-69395) |
| `empty_code_7[atomic]` | `empty_package_code=7` | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[7] | BYTECODE_PROVEN + UNKNOWN | q$a rules R2 (ALL.txt:68962-69395); U15 |
| `empty_package_duplicated[atomic]` | DUPLICATE of the sentinel | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0, 0] | BYTECODE_PROVEN | q$a rules R2/R12 (ALL.txt:68962-69395) |
| `empty_package_wrong_session[atomic]` | WRONG_SESSION on the sentinel | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R1 (ALL.txt:68962-69395); policy P2 |
| `empty_package_truncated[atomic]` | TRUNCATE sentinel to 10 B (no code byte) | detected | detected, bytes exact, no finish, no tail cb, `undecodable`, restarts 0, z6 0 | HARNESS_POLICY + UNKNOWN | q$a rules R13 (ALL.txt:68962-69395) |
| `tail_missing[atomic]` | DROP TAIL | recovered | recovered, bytes exact, finish, no tail cb, restarts 0, z6 0, finish[0] | RUNTIME + BYTECODE_PROVEN + UNKNOWN | r7/r7-s13-recording-pull.md (runs 3/4b/8); q$a rules R2 (ALL.txt:68962-69395) |
| `tail_crc_corrupted[atomic]` | CORRUPT TAIL byte 7 (crc) ^ 0xFF | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | RUNTIME + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (runs 1b/2); q$a rules R13 (ALL.txt:68962-69395) |
| `head_missing[atomic]` | DROP HEAD | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R3/R2 (ALL.txt:68962-69395) |
| `head_status_nonzero[atomic]` | HEAD status := 1 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R8 (ALL.txt:68962-69395) |
| `battery_push_interleaved[atomic]` | opcode-9 battery push after DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R12 (ALL.txt:68962-69395) |
| `data_duplicate[atomic]` | DUPLICATE DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R5 (ALL.txt:68962-69395) |
| `clean_default_sequence[paced]` | none (HEAD . DATA x32 . EMPTY(0) . TAIL) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | RUNTIME_PROVEN + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (runs 3/4b/10); q$a rules R2/R9 (ALL.txt:68962-69395) |
| `no_empty_package[paced]` | `empty_package_code=None`: HEAD . DATA . TAIL, no sentinel | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | RUNTIME_PROVEN + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (runs 1/1b/2/9); q$a rules R9/R10 (ALL.txt:68962-69395); policy P2 |
| `empty_after_tail[paced]` | sentinel moved after the TAIL (`EMPTY_PACKAGE_AFTER` on TAIL) | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | RUNTIME_PROVEN + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (run 7); q$a rules R12 (ALL.txt:68962-69395); policy P2 |
| `empty_code_1[paced]` | `empty_package_code=1` | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[1] | RUNTIME_PROVEN + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (run 8); q$a rules R2 (ALL.txt:68962-69395) |
| `empty_code_7[paced]` | `empty_package_code=7` | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[7] | BYTECODE_PROVEN + UNKNOWN | q$a rules R2 (ALL.txt:68962-69395); U15 |
| `empty_package_duplicated[paced]` | DUPLICATE of the sentinel | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0, 0] | BYTECODE_PROVEN | q$a rules R2/R12 (ALL.txt:68962-69395) |
| `empty_package_wrong_session[paced]` | WRONG_SESSION on the sentinel | detected | detected, bytes exact, no finish, tail cb, `stalled_after_tail`, restarts 0, z6 0 | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R1 (ALL.txt:68962-69395); policy P2 |
| `empty_package_truncated[paced]` | TRUNCATE sentinel to 10 B (no code byte) | detected | detected, bytes exact, no finish, no tail cb, `undecodable`, restarts 0, z6 0 | HARNESS_POLICY + UNKNOWN | q$a rules R13 (ALL.txt:68962-69395) |
| `tail_missing[paced]` | DROP TAIL | recovered | recovered, bytes exact, finish, no tail cb, restarts 0, z6 0, finish[0] | RUNTIME + BYTECODE_PROVEN + UNKNOWN | r7/r7-s13-recording-pull.md (runs 3/4b/8); q$a rules R2 (ALL.txt:68962-69395) |
| `tail_crc_corrupted[paced]` | CORRUPT TAIL byte 7 (crc) ^ 0xFF | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | RUNTIME + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (runs 1b/2); q$a rules R13 (ALL.txt:68962-69395) |
| `head_missing[paced]` | DROP HEAD | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R3/R2 (ALL.txt:68962-69395) |
| `head_status_nonzero[paced]` | HEAD status := 1 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R8 (ALL.txt:68962-69395) |
| `battery_push_interleaved[paced]` | opcode-9 battery push after DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R12 (ALL.txt:68962-69395) |
| `data_duplicate[paced]` | DUPLICATE DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R5 (ALL.txt:68962-69395) |
| `data_drop_first[paced]` | DROP DATA@0 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | RUNTIME + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (runs 6b/6d); q$a rules R6/R11 (ALL.txt:68962-69395) |
| `data_drop_middle[paced]` | DROP DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | RUNTIME + BYTECODE_PROVEN | r7/r7-s13-recording-pull.md (run 6b); q$a rules R6/R11 (ALL.txt:68962-69395) |
| `data_drop_run_of_3[paced]` | DROP DATA@512,544,576 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN | q$a rules R6/R11 (ALL.txt:68962-69395) |
| `data_reordered[paced]` | REORDER DATA@320 with DATA@352 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN | q$a rules R6/R3/R11 (ALL.txt:68962-69395) |
| `data_truncated_partial[paced]` | TRUNCATE DATA@512 to 15 B (5 payload bytes) | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN | q$a rules R7/R6 (ALL.txt:68962-69395) |
| `data_truncated_header_only[paced]` | TRUNCATE DATA@512 to 10 B (header only) | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN | q$a rules R7/R6 (ALL.txt:68962-69395) |
| `data_wrong_session_once[paced]` | WRONG_SESSION on DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN | q$a rules R1/R6 (ALL.txt:68962-69395) |
| `old_stream_not_abandoned[atomic]` | DROP DATA@512 on a device that keeps serving the stale stream | corruption_risk | corruption_risk, bytes exact, finish, tail cb, restarts 2, z6 1, finish[0] | RUNTIME + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (run 6); q$a rules R2/R9/R12 (ALL.txt:68962-69395); policy P8 |
| `old_stream_not_abandoned[paced]` | DROP DATA@512 on a device that keeps serving the stale stream | corruption_risk or detected | detected, bytes prefix, no finish, no tail cb, `restart_cap_exceeded`, restarts 9, z6 9 | RUNTIME + HARNESS_POLICY | r7/r7-s13-recording-pull.md (run 6 vs 6b); policy P3/P2/P8 |
| `data_drop_last[atomic]` | DROP DATA@992 (last frame) | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, app resumes 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R2 (ALL.txt:68962-69395); policy P4/P5 |
| `data_drop_last[paced]` | DROP DATA@992 (last frame) | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, app resumes 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R2 (ALL.txt:68962-69395); policy P4/P5 |
| `data_truncated_below_header[atomic]` | TRUNCATE DATA@512 to 7 B | detected | detected, bytes prefix, no finish, no tail cb, `undecodable`, restarts 0, z6 0 | HARNESS_POLICY + UNKNOWN | q$a rules R13 (ALL.txt:68962-69395) |
| `stop_ack_lost_timer_restart[paced]` | DROP DATA@512 + DROP a7 (opcode 30) | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R6/R10/R11 (ALL.txt:68962-69395); policy P1 |
| `stop_sync_mid_transfer_then_restart[paced]` | app stopSyncFile at cursor >= 256, then syncFile from cursor | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, app resumes 1, finish[0] | RUNTIME + BYTECODE_PROVEN + HARNESS_POLICY | r7/r7-s13-recording-pull.md (run 5) |
| `link_disconnect_then_resume[paced]` | device disconnects after DATA@512; reconnect + resume | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, app resumes 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | policy P5; ALL.txt:39892 |
| `file_list_interrupted_mid_page` | device silent from file-list page 1 of 3 | detected | detected | BYTECODE_PROVEN + HARNESS_POLICY | policy P2; ledger S5.6 |
| `file_list_page_dropped` | DROP file-list page 1 of 3 | detected | detected | BYTECODE_PROVEN + HARNESS_POLICY | policy P2; ledger S5.6 |
| `mtu_23_fitted[atomic]` | ATT MTU 23, frames fitted (10 B payload) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R3/R7/R2 (ALL.txt:68962-69395); ledger S12 |
| `mtu_185_fitted[atomic]` | ATT MTU 185, frames fitted (172 B payload) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R3/R7/R2 (ALL.txt:68962-69395); ledger S12 |
| `mtu_247_fitted[atomic]` | ATT MTU 247, frames fitted (234 B payload) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R3/R7/R2 (ALL.txt:68962-69395); ledger S12 |
| `mtu_517_fitted[atomic]` | ATT MTU 517, frames fitted (255 B payload) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R3/R7/R2 (ALL.txt:68962-69395); ledger S12 |
| `mtu_23_unsized_frames[paced]` | ATT MTU 23 with 32 B frames (link truncates to 20 B), 96 B file | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, app resumes 2, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R7/R6/R2/R11 (ALL.txt:68962-69395); policy P4/P5 |
| `cccd_notify_with_gap[paced]` | CCCD notify + DROP DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | ledger S12 |
| `cccd_indicate_with_gap[paced]` | CCCD indicate + DROP DATA@512 | recovered | recovered, bytes exact, finish, tail cb, restarts 1, z6 1, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | ledger S12 |
| `delete_file_being_synced[paced]` | w6 delete of the session at cursor >= 256 | detected | detected, bytes prefix, no finish, no tail cb, `stalled`, restarts 0, z6 0 | HARNESS_POLICY + UNKNOWN | policy P2 |
| `zero_length_file[atomic]` | 0-byte file (HEAD . EMPTY . TAIL) | detected | detected, bytes exact, no finish, no tail cb, `restart_cap_exceeded`, restarts 9, z6 0 | BYTECODE_PROVEN + HARNESS_POLICY + UNKNOWN | q$a rules R2/R9 (ALL.txt:68962-69395); policy P3 |
| `file_one_frame[atomic]` | 32-byte file | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R3/R2/R9 (ALL.txt:68962-69395) |
| `file_4096_frames[atomic]` | 131 072-byte file (4096 frames) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN | q$a rules R3/R2/R9 (ALL.txt:68962-69395) |
| `data_wrong_session_persistent[atomic]` | WRONG_SESSION on every DATA and the sentinel | detected | detected, bytes empty, no finish, no tail cb, `restart_cap_exceeded`, restarts 9, z6 0 | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R1/R9 (ALL.txt:68962-69395); policy P3 |
| `back_to_back_two_files[paced]` | sync A (DROP DATA@512) then sync B (777 B) | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | BYTECODE_PROVEN + HARNESS_POLICY | q$a rules R1/R12 (ALL.txt:68962-69395) |
| `second_connection_control_traffic[paced]` | second central sends getState mid-stream | recovered | recovered, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | HARNESS_POLICY |  |
| `second_connection_competing_sync[paced]` | second central issues its own syncFile mid-stream | recovered_by_app_resume | recovered_by_app_resume, bytes exact, finish, tail cb, restarts 0, z6 0, finish[0] | HARNESS_POLICY | policy P2/P5; ledger S12 |
| `data_payload_bitflip[atomic]` | CORRUPT DATA@512 payload byte ^ 0x80 (not a cell) | undetectable | undetectable, bytes corrupted | BYTECODE_PROVEN |  |

`data_payload_bitflip` is deliberately outside the invariant: q$a has no
payload integrity check and the TAIL crc is never verified (R13), so a flipped
byte is accepted; on real BLE the link-layer CRC rejects the PDU, on Bumble's
virtual link nothing does.

## 3. Receiver-model provenance

The model is `com.plaud.sdk.proto.q$a`, the inner class the SDK creates per
`syncFile` request (javap: `build/evidence/javap/ALL.txt:68831-69574`; jadx:
`build/evidence/jadx-out/sources/com/plaud/sdk/proto/q.java:88-330`). Field
names are taken directly from the bytecode: `a` cursor, `b` resend pending,
`c` handed over to a restart, `d`/`e` the 5 000 ms stall timer, `g` start,
`i` sessionId, `n` the `v3` sink (`receiveVoiceData`, `finish`). Shared state:
`q.H` (recovery in flight; read `q.j` ALL.txt:45087-45091, written `q.d`
:45113-45119), `q.m` (portVersion, :45093-45097), and the response registry
`z.t` (newest-first lookup with eviction, ALL.txt:75489-75535).

| rule | what | where |
|---|---|---|
| R1 | session id u32@1 must equal `i` (portVersion >= 7) else "---sessionId miss match---", frame dropped | ALL.txt:68975-69001 |
| R2 | offset 0xFFFFFFFF = EMPTY_PACKAGE, code u8@i+5; H clear: ignored when cursor == start or b, else **`v3.finish(code)`**; H set: ignored when c or code == 1, else c = true, timer off, restart ("---isPacketLossStopSync---") | ALL.txt:69024-69108; q.java:247-290 |
| R3 | offset == cursor -> accept | :69026, :69120-69122 |
| R4 | offset != cursor and H -> arm/extend the timer, return | :69123-69128 |
| R5 | offset < cursor -> silent drop | :69129-69131 |
| R6 | first gap: b = true, `fileSyncLossPkgStop`: `q.k` writes z6 (H = false first, ALL.txt:46662-46689), H = true, timer armed; a7 callback: c = true, restart from the cursor | :69132-69182, :69396-69441 |
| R7 | accept: b = false, len u8@i+4 clamped to the frame end, cursor += len, `receiveVoiceData(payload, offset)` | :69196-69230 |
| R8 | HEAD: parse s6; failed when parse throws or status > 0 (`z.c(29)`); callback iff !b or failed | :69238-69289 |
| R9 | TAIL: !H and cursor != start -> `z.c(29)` + tail callback (**not** completion); otherwise c = true, timer off, restart | :69295-69395 |
| R10 | stall timer: 5 000 ms in 10 ms ticks; on expiry with H set and !c -> restart; armed only by R4/R6 | :68906-68950, :69491-69557 |
| R11 | restart = `q.a(JJJZ..)`: H = false, register {28, 29}, write y6(sessionId, cursor, end), new q$a with a = g = cursor | ALL.txt:39870-40135 |
| R12 | dispatch: type 2 -> registration for opcode 29 ("File Sync Callback is Null" when none); sticky opcodes {11,19,26,28,35,50,52,61,143}; every other response opcode (29, 30) removes its registration before the callback | ALL.txt:72280-72360; z.java:619-745 |
| R13 | TAIL crc forwarded, never compared; `TntBleCommUtils.readInt` is native -> under-length frames reported as `undecodable`, not guessed | ledger S5.8 |

Runtime cross-checks of the model (all `r7/r7-s13-recording-pull.md`): the
completion branch (R2, runs 3/4b/8), the non-completion of TAIL-only streams
(R9/R10, runs 1/1b/2/9), the after-TAIL sentinel (R12, run 7), the immediate
z6 and the ~35 ms restart (R6, `capture-run6b-gap-export-abort.json`: 28@0,
29 at +1.720 s, 28@3200 at +1.755 s), the while-recovering restart (R2 while-H,
run 6), and the crc indifference (R13, runs 1b/2). `tests/test_r7_s13_transfer_close.py`
pins the archived logs and captures; `tests/test_v2_receiver_model.py` pins
each rule to an analytic answer, and the sequence tests there drive the
default, legacy and after-TAIL sequences through the R12 dispatcher.

## 4. HARNESS_POLICY (chosen by the harness, not device claims)

Model side (`tests/fault_support.py`):

| id | policy | value in the matrix |
|---|---|---|
| P1 | `stall_timeout`: q$a's constant is 5.0 s; scaled for speed, mechanism unchanged | 0.4 s (0.3 s in `stop_ack_lost_timer_restart`, which asserts the expiry fired) |
| P2 | `idle_timeout`: q$a has no quiescence timer; the driver reports `stalled` / `stalled_after_tail` after this much silence | 0.6 s (1.0 s in the timer cell, 5.0 s for the 4096-frame file) |
| P3 | `max_restarts`: q$a restarts without bound; the driver caps and reports `restart_cap_exceeded` | 8 (16 for `mtu_23_unsized_frames`) |
| P4 | `expected_size`: compare the cursor with the file-list `fileSize` at finish -> `finish_short`; the SDK does not, the app: UNKNOWN | on for every cell |
| P5 | app-level resume after `finish_short` / `link_lost` / `app_stopped`: re-issue `syncFile(sid, cursor, 0)` | at most 3 resumes |
| P6 | the sink refuses an offset that is not its length (by R3 impossible; a violation is a model bug) | -- |
| P7 | post-finish drain for the TAIL callback, up to `idle_timeout`; recovery actions after finish are recorded (`post_finish`), not executed | -- |
| P8 | not modelled: the export writer and the op-queue's -98/-99 request retries (where run 6's corruption happened) | -> `corruption_risk` |
| -- | a new `syncFile` call starts from an empty inbox (the SDK would drop late frames of the old registration, R12) | -- |

Device side (`emulator/plaudsim/faults.py`, `profile.py`, `transfer.py`):

| policy | value |
|---|---|
| EMPTY_PACKAGE code | 0 (`DEFAULT_EMPTY_PACKAGE_CODE`); real firmware's value UNKNOWN; cells also run 1 and 7 |
| sentinel position | after the last DATA frame, before the TAIL (the only position that completes) |
| DATA payload size | 32 B (`DEFAULT_DATA_PAYLOAD_SIZE`); `fit_payload_to_mtu` sizes to `att_mtu - 3 - 10` in the MTU cells |
| inter-frame pacing (`PACE`) | 4 ms, the value under which the genuine SDK converged on the AVD (R7-S13 S4.4); `response_pacing_s` of the base peripheral |
| task streaming | `stream_in_task=True` for every paced cell: a z6 or a new y6 aborts the running stream (`PlaudPeripheral._abort_stream`) |
| `abandon_stream_on_restart` / `cancel_stream_on_stop` | True by default (runs 6b-10); False reproduces the run-6 device |
| `cancel_stream_on_delete` | a w6 delete of the streaming session aborts the stream (real device UNKNOWN) |
| single transfer slot | a second y6 (from any central) replaces the active session |
| CCCD | 2BB0 advertises NOTIFY and INDICATE; the emulator answers in whichever mode the central selected |
| HEAD status, TAIL crc, file table, file bytes | synthetic values (status 0, crc 0xBEEF, one 1024 B random file; 777 B second file; 96 B and 131 072 B variants) |
| injector `once` semantics | a fault fires once per matching key so "the client recovers" is separable from "the device is permanently broken" |
| atomic vs paced | atomic = the base peripheral's inline emission (the whole stream before the write response); for a gap it is the run-6 device and is run only as `old_stream_not_abandoned` |

## 5. What the matrix cannot test without hardware

* Real firmware's frame sizes, pacing, EMPTY_PACKAGE code value, TAIL field
  content, and whether a real device abandons a stream on a new
  `syncFileStart` or a `stopSync` -- every device-side behaviour here is a
  policy chosen so the genuine client converges.
* Whether a real device ever emits a HEAD status other than 0, ever omits a
  HEAD or a TAIL, or answers a delete during a sync; the model's reaction to
  those frames is bytecode, the device's emission of them is invented.
* The SDK's export layer and op-queue (P8): the run-6 corruption lives there,
  and only an AVD/phone run against a device that keeps draining a stale
  stream reproduces it. The matrix reports the signature, not the corruption.
* The app-level timeout for a transfer that never completes (the -98 seen on
  the AVD) and the app's reaction to `bleSyncFileHead` status != 0, a missing
  `bleSyncFileTail`, or a double `bleDataComplete`.
* Link-layer effects: BLE CRC rejection of corrupted PDUs (the bit-flip row),
  connection-interval pacing, supervision timeouts and real reconnection
  latency; Bumble's virtual link has none of them.
* The sealed channel (portVersion >= 20): the emulator speaks the cleartext
  branch only, so every cell here is a sub-20 device.
* `TntBleCommUtils.readInt` on under-length frames (native): reported as
  `undecodable`, never reproduced.

## 6. Files

* `emulator/plaudsim/faults.py` -- `Kind` (HEAD/DATA/EMPTY/TAIL/FILE_LIST/OTHER),
  `FaultKind`, `Fault`, `FaultInjector`, `FaultyPeripheral`.
* `tests/fault_support.py` -- `SdkHost`, `SdkReceiver`, `SdkTransferDriver`,
  `TransferResult`, `outcome_of`, `run6_signature`, link helpers.
* `tests/test_v2_receiver_model.py` -- 48 pure tests of the rules and the injector.
* `tests/test_v2_fault_matrix.py` -- the 61 cells; writes `build/v2-fault-matrix.json`.
* `tests/test_r7_s13_transfer_close.py` -- the runtime evidence and the emulator's
  default sequence (owned by the R7-S13 track; the model tests there pass).
