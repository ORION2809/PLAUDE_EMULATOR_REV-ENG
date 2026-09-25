# R7-S13: a recording pulled through the genuine SDK (RUNTIME, synthetic-only)

Verdict: **V3 ACHIEVED for the file-transfer path.** The unmodified official
Android SDK (`plaud-sdk.aar`, sha256 `041a6f88…aedce`) connected to our Bumble
emulator over `android-netsim`, listed the served recording, pulled it over
BLE through both public paths — the raw collector (`syncFile` →
`bleData`/`bleDataComplete`) and the product path (`exportAudio(…, OPUS)`) —
and handed the application bytes that hash identically to the file the
emulator served. Its own gap recovery was exercised three times (offsets 3200, 8000,
3200 again on the shipped code) and converged to the same byte-exact file. Along the way the runs
**falsified one frozen protocol claim and refined two others** (§4).

This is **not** evidence about real hardware: the device is ours, the token is
synthetic, `accept_any_token` is HARNESS_POLICY. It is evidence about what the
*client* requires of a device, which is exactly what a device emulator must
get right.

## 1. Topology and driver

```
API-34 AVD (mivi_test_34) ── netsimd ── bumble android-netsim ── r7/pull_capture_peripheral.py
  com.plaud.template (r7/android-app)                              PullCapturingPeripheral(PlaudPeripheral, pv 7)
    debug/PullCaptureActivity.kt                                     serves tests/fixtures/r6s2_16k_mono.ogg
      recoveryConnectBleDevice(dev, "SYNTH-HIST-0001")               (11 271 B, sha256 0f45367b…48c3d, plain Ogg/Opus)
      bleBind(0) → getFileList() → bleFileList                        session 1700000000, scene 2, attribute 1
      mode=raw    : syncFile(sid,0,0) → bleData(offset,bytes)…        mirrors every 2BB1 write → capture-<run>.json
      mode=export : exportAudio(sid, dir, OPUS, 1, cb)                knobs: EMPTY_CODE/POS, DROP_OFFSET, TAIL_CRC,
      both        : raw then export                                            ABORT_ON_RESTART, PACING_S
```

Per run: `am force-stop` + `pm clear` + re-grant BLE permissions, fresh
peripheral process, `logcat -c`, `am start … --es mode <m>`, 65 s, `logcat -d`,
`run-as` pull of the output files. Scripts and every log are in
`r7/r7-s13-evidence/` (`batch1.sh`, `batch2.sh`, `capture-*.json`,
`logcat-*.filtered.log`, `peripheral-*.log`, `SHA256SUMS`).

## 2. Runs

| run | peripheral | driver | result |
|---|---|---|---|
| 1 / 1b | HEAD · 353 DATA · TAIL(0xBEEF) | both | `bleSyncFileHead`, 353 `bleData`, `bleSyncFileTail(status=48879)`; **no `bleDataComplete`**; raw bytes reassembled by the driver on a 3 s grace: **byte-exact**. Export: download reached 99 %, never completed; SDK op-queue reported `-98` on the pending `[28,29]` request when the export re-issued it. |
| 2 | same, TAIL field = 0 | both | identical to 1b → **the TAIL u16 is not a completion gate** |
| 3 | + `EMPTY_PACKAGE(code 0)` before TAIL | both | **`bleDataComplete` fired** (raw byte-exact). Export completed but its file was 14 311 B = fixture[0:3040] ‖ fixture[0:11271]: the SDK's temp file kept a partial first attempt (the -98/-99 fumble caused by the driver starting export while the raw request was still pending) and appended the retry. Kept as `export-run3-both-concatenated-14311.opus`. |
| 4 | same | export (output file left from run 3) | SDK logged "本地文件已存在，直接导出" (local file exists, export directly) — **`exportAudio` skips the download when `<outputDir>/<sessionId>.opus` exists**. Led to `pm clear` per run. |
| 4b | EMPTY(0) before TAIL | export, clean | `DOWNLOADING → TRANSCODING → onComplete`; output **11 271 B, sha256 identical to the served file** → OPUS export of plain-Ogg input is a **byte-exact passthrough** (no re-encode, OpusTags vendor untouched). |
| 5 | EMPTY(0) before TAIL | raw + `stopSyncFile()` | `bleDataComplete` on EMPTY; app's `stopSyncFile()` → wire opcode 29 → device `a7` → SDK cancels the pending `[28,29]` bean with `-98` → `bleSyncFileStop`. The public raw session is closed by the app, not by the TAIL. |
| 6 | EMPTY(0), DATA@3200 dropped, no pacing | export | SDK logged `start resend syncFileStart(…,lastPosition:3200)`; the emulator kept draining the old stream, whose EMPTY arrived during recovery → `---isPacketLossStopSync---` → second restart; `-98/-99` retries; output 12 871 B = fixture[0:4800] ‖ fixture[3200:11271] (kept as `export-run6-gap-corrupted-12871.opus`). Diagnosis: emulator artefact (old stream not abandoned) exposing an SDK writer weakness. |
| 7 | EMPTY(0) **after** TAIL | export | never completes (65 s) |
| 8 | EMPTY(**1**) before TAIL | export | completes, byte-exact |
| 9 | no EMPTY (control) | export | never completes |
| 6b | EMPTY(0), drop@3200, **abort-on-restart + 4 ms pacing** | export | gap at ~+1.2 s → `start resend` at +1.720 s → **stopSync (29) at +1.720 s** → `a7` → `syncFileStart(start=3200)` at +1.755 s → 254 frames → EMPTY → complete; output **byte-exact** |
| 6c | same | raw + stop | `bleDataComplete`, byte-exact; two stopSyncs on the wire (SDK's own on the gap, then the app's) |
| 6d | drop@8000 | export | resend from 8000 at +3.99 s; byte-exact |
| 10 | clean, abort+pacing | export | byte-exact; op-queue `[28,29]` callback `status:0` (see §4.4) |
| 11 | **shipped `PlaudPeripheral` defaults** (EMPTY code 0, `stream_in_task`, 4 ms pacing), no experimental override | export | byte-exact; `[28,29]` `status:0`; no retries |
| 12 | shipped defaults | raw + stop | `bleDataComplete`, byte-exact; app's `stopSyncFile()` acked |
| 13 | shipped defaults, drop@3200 | export | one stopSync, one restart from 3200, byte-exact |

Every "byte-exact" above is `0f45367bd1eae540a51f2741e3150ffc705aace3054bfdaa4e134b71cbb48c3d`
(`SHA256SUMS`).

## 3. What the genuine client does (RUNTIME_PROVEN)

Connect sequence as in R7-S12 (`start → gatt_connect → set_notify →
set_data_notify(new_battery_service_port_7) → first_handshake → sync_time →
first_handshake/old_protocol_ok → bleConnectState(1) → bleBind(0, protVersion=5,
tz=5)`; the facade's `protVersion`/`timezone` quirk reproduced). Then, from the
app's calls:

1. `getFileList()` → `p2` (opcode 26) with the request stamp echoed → one
   `q2` page → `bleFileList([BleFile{sessionId, fileSize, scene, attribute,
   stability=0, isMusic=false}])`.
2. `syncFile(sid, 0, 0)` / `exportAudio` → `y6` (opcode 28) `[01 1C 00][sid][start=0][end=0]`.
3. HEAD `s6` → `bleSyncFileHead(sid, 0)`; DATA type-2 frames consumed in
   cursor order → `bleData(sid, offset, bytes)` (32-byte payloads here);
   TAIL `t6` → `bleSyncFileTail(sid, <u16 field>)`.
4. **Completion is the EMPTY_PACKAGE frame** `[02][sid][FF FF FF FF][00][code]`
   received while the cursor has advanced: `q$a.a` → `v3.finish(code)` →
   `bleDataComplete()` / export "下载完成" (ALL.txt:69024-69108; jadx
   `q.java:257-266`). It must precede the TAIL (run 7). Code 0 and 1 both
   complete on the normal path (runs 4b, 8).
5. On an offset gap: `start resend syncFileStart(sessionId, lastPosition=cursor)`
   → **immediate `stopSync` (29)** → wait for `a7` (30) → `y6(sid, cursor, 0)`;
   the device must serve from `cursor`; the client accepts only frames whose
   offset equals its cursor (frames of the abandoned stream are dropped).
   Elapsed gap→restart ≈ 35 ms after detection (runs 6b/6c/6d).
6. An EMPTY_PACKAGE with code ≠ 1 arriving while a recovery is in flight
   triggers `---isPacketLossStopSync---` and another restart (run 6).
7. `exportAudio(OPUS)`: `DOWNLOADING` (progress = download bytes) → on EMPTY
   `TRANSCODING` → `onComplete(<outputDir>/<sid>.opus)`; for plain-Ogg input
   the output **is** the device bytes. If the output file already exists the
   download is skipped.
8. The raw `syncFile` session stays open until the app calls `stopSyncFile()`;
   the op-queue's pending `[28,29]` request is then cancelled with `-98`.

## 4. Corrections to the frozen ledger (RUNTIME beats bytecode reading)

### 4.1 The transfer is closed by EMPTY_PACKAGE, not by TAIL — **falsified §5.8 sequence**
`docs/protocol-ledger.md` §5.8 documents HEAD · DATA… · TAIL and mentions
EMPTY_PACKAGE only as a frame shape. The emulator (`transfer.TransferSession.frames`)
emitted exactly that and the real client never completed (runs 1, 2, 9).
Sequence a device must send: **HEAD · DATA… · EMPTY_PACKAGE(code) · TAIL**.
Position after TAIL does not work (run 7). Class: RUNTIME_PROVEN (this build).

### 4.2 stopSync is sent synchronously on a gap — **corrects §5.8 wording**
§5.8 says the stopSync wire write is "reached only through the 5 000 ms stall
timeout". At runtime the gap branch (`this.o.k(null, …)` = `q.k`, the stopSync
write) put opcode 29 on the wire 0–35 ms after `start resend …` and re-issued
`syncFileStart` from the cursor once the `a7` ack arrived (runs 6b/6c/6d,
`capture-run6b….json`: 28@0 +0.000 s, 29 +1.720 s, 28@3200 +1.755 s). The 5 s
runnable is the *stall* path, not the gap path.

### 4.3 TAIL u16 field — refined Q2 / §5.8
Still never verified as a CRC (0xBEEF and 0 behave identically, runs 1b/2),
and it is surfaced to the app as `bleSyncFileTail(sessionId, status)` on
Android — the iOS name `crc` and the Android name `status` are the same u16.

### 4.4 The op-queue race — a device-fidelity requirement (HARNESS_POLICY)
With zero-latency responses the tinnotech `BluetoothLeOperation` registered
the `[28,29]` response bean *after* the HEAD notification had already been
delivered, so the request never completed and was later reported `-98`
(runs 1–5) or produced `-99 "Failed to send the download request"` retries
(runs 3, 6). With ~4 ms inter-frame pacing (runs 6b–10) the bean completed
with `status:0` and no retries occurred. A device emulator should not answer
faster than the phone's GATT write callback; `PlaudPeripheral` now documents
this as policy (see §6).

### 4.5 OPUS export is passthrough for Ogg input — refines U19
The local SDK does not re-encode or re-tag a plain Ogg/Opus recording on
`AudioExportFormat.OPUS`; the `TinnoTech123456789012` vendor string is
written only when the SDK *builds* an Ogg from raw packets. The cloud's
`PALUD.AI` tag therefore cannot come from this path either.

## 5. What this does not prove

Real firmware's frame sizes, pacing, EMPTY_PACKAGE code values, TAIL field
content, whether the device abandons a stream on a new `syncFileStart`, and
everything above `portVersion 20` (sealed link) remain UNKNOWN. The emulator's
choices — 32-byte payloads, code 0, 4 ms pacing, abort-on-restart — are
HARNESS_POLICY chosen so the genuine client converges; they are not device
claims.

## 6. Consequences applied to the harness (confirmed live by runs 11–13)

* `emulator/plaudsim/transfer.py`: `TransferSession.frames()` now emits
  `EMPTY_PACKAGE(code 0)` before the TAIL by default (`empty_package_code`
  parameter, `None` restores the pre-R7-S13 sequence for the negative test).
* `emulator/plaudsim/profile.py`: transfers stream from a cancellable task with
  `response_pacing_s` (default 0.004) and are aborted by a new `syncFileStart`
  or `stopSync` (`abort_stream_on_restart=True`).
* Tests: `tests/test_r7_s13_transfer_close.py` pins the sequence, the ordering
  rule, the abort behaviour and the negative control.
* Ledger §5.8/§14 and `docs/reconstruction-log.md` updated; PROJECT.md 4b V3
  moved to achieved-for-transfer; U19 refined.
