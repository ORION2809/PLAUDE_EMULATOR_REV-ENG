# R5-S1: PlaudPeripheral vs the real SDK at the GATT boundary (EXPERIMENTAL)

Verdict: **SDK GATT BOUNDARY MATCHED — no emulator change required.**

## Topology (same as R4-S3, re-verified)

```text
real template APK (stock AAR, empty partner token)
→ android.bluetooth.*
→ AVD mivi_test_34 (API 34, hw.bluetooth=yes) virtual controller
→ netsimd (localhost:59080)
→ Bumble android-netsim transport
→ frozen plaudsim.PlaudPeripheral (portVersion 7, synthetic state)
```

Peripheral runner: `r4-s3/plaud_netsim_peripheral.py` reused UNCHANGED
(static random MAC `F0:1A:2B:3C:4D:5E`, legacy flags+manufacturer-data
advertising — see classifications below). No `emulator/plaudsim/**` change.

## Reproduce

Same as `r4-s3/README.md` (same APK, no rebuild this sprint):
boot AVD → attach runner with the emulator's TMPDIR → install APK →
grant BLUETOOTH_SCAN/CONNECT → launch → Get Started → Connect on
`Plaud Note Pro / SN: 8810000001` → collect `adb logcat` + Bumble DEBUG log.
Evidence files here: `logcat-sdk-sequence.log` (174 lines, rendering spam
stripped), `bumble-att-sequence.log` (68 lines, ANSI stripped).

## 2. Exact real-SDK sequence observed (clean run, PID 12048, 2026-09-22)

Scan lists `Plaud Note Pro / SN: 8810000001` → tap Connect →
`RSA key pair not ready within 10s` + `Cannot sign device SN - user access
token not set` + `resolveHandshakeToken: no token available` →
`bleConnectStage start` → `validCustomer Pass` → `gatt_connect` →
`BluetoothGatt: connect()` → `btStatusChange: CONNECTING` →
`onConnectionStateChange CONNECTED` → `configureMTU mtu: 255` →
`onConfigureMTU mtu=517 status=0` → `discoverServices` →
`onSearchComplete Status=0` → `onServicesDiscovered status: 0` →
`btStatusChange: CONNECTED` → `set_notify` → `New Battery Service` →
`set_data_notify detail=new_battery_service_port_7` →
`setCharacteristicNotification(00002bb0…) enable: true` →
`DescriptorWrite` queued → `-callBackRequest:null status:0` →
`first_handshake` → `first_handshake detail=bind_token_empty` →
`bleConnectFail-UUID_IS_EMPTY{errCode=-3}` → disconnect
(`cancelOpen`, `clientDisconnect`, Bumble `HCI_DISCONNECTION_COMPLETE reason=19`).

Android GATT DB for this link (BtGatt.GattService, status 0) — the new
evidence beyond R4-S3:

- 1800: 2A00, 2A01 (generic GAP — Bumble substrate, not the peripheral)
- 1801: 2A05+CCCD, 2B29, 2B2A (generic GATT — Bumble substrate)
- **1910 (id 14): 2BB0 (id 16) + 2902 (id 17), 2BB1 (id 19)**
- 180F (id 20): 2A19 (id 22) + 2902 (id 23)

## 3. Exact PlaudPeripheral sequence produced

`R4S3_PERIPHERAL_READY port_version=7` → `LE CONNECTION [0x0200]` as
PERIPHERAL → 23 ATT req/resp (discovery: READ_BY_TYPE / READ_BY_GROUP_TYPE /
FIND_INFORMATION) → `Connection Parameters Update` + `ATT MTU Update 517` →
`ATT_WRITE_REQUEST handle 0x0011 value 0200` → `ATT_WRITE_RESPONSE` →
`Subscription update handle=0x0010: 0200`, `CCCDs: {16: b'\x02\x00'}` →
`DISCONNECTION reason=19` (SDK-initiated, after `bind_token_empty`).
No k3 write ever arrives — the SDK stops before writing, as designed
without a token. No handshake/GetState/GetStorage/syncTime/file-transfer
logic executed (out of scope, not implemented here).

## 1/4/5. Audit: before/after, discrepancies, classification

`Before` = frozen `PlaudPeripheral` + r4-s3 runner. `After` = identical —
the audit found **no GATT/transport/service mismatch before
`first_handshake`**, so nothing was changed.

| Item | Observed | Classification |
|---|---|---|
| advertisement (flags + manufacturer data only, 29 B; frozen `advertise()` with name + UUID16 + mfg is 42 B and netsimd-rejected) | SDK lists device; empty-ScanFilter + software manufacturer-data filter | HARNESS POLICY (transport limit) + SOURCE-BACKED (ledger 6.2) + RUNTIME-OBSERVED |
| device identity/name (no GAP name on air; SDK shows `Plaud Note Pro`, `SN: 8810000001`) | synthesised from serial prefix 881 | SOURCE-BACKED (u4 string table) + RUNTIME-OBSERVED |
| static MAC `F0:1A:2B:3C:4D:5E` | SDK `connect()` targets it; `bleConnectStage ... mac=F0:1A…` | HARNESS POLICY + RUNTIME-OBSERVED (rotating MAC + cached scan entry = silent no-GATT failure, fixed in R4-S3) |
| service 1910 | in Android GATT DB (id 14) | SOURCE-BACKED (w$d; evidence-digest UUID set is mechanical) + RUNTIME-OBSERVED |
| 2BB0 + CCCD | id 16 + 2902 id 17; subscribe OK; indication write 0200 accepted | roles SOURCE-BACKED; property mask (NOTIFY\|INDICATE) HARNESS POLICY — exact bitmask is UNKNOWN (Q1); SDK choosing indication when both offered is RUNTIME-OBSERVED |
| 2BB1 | discovered (id 19); no write attempted yet (blocked earlier) | role SOURCE-BACKED; write-type behaviour UNKNOWN beyond discovery — no claim |
| CCCD 2902, notify-vs-indicate | write `0200` = indication; `_respond` prefers indication when set | SOURCE-BACKED (`z.a` selects from discovered properties) + RUNTIME-OBSERVED |
| service discovery | 23 ATT exchanges, `onSearchComplete 0` | RUNTIME-OBSERVED |
| MTU 255 → 517, discovery only after `onMtuChanged` | exact ledger 1.2 path | SOURCE-BACKED + RUNTIME-OBSERVED |
| lifecycle/callback order (`set_notify` → new-battery-service skip of 0x180F → `set_data_notify` → `first_handshake`) | matches ledger 4.1 branches (pv 7: ≥5 new-battery path, <20 no pre-handshake) | SOURCE-BACKED + RUNTIME-OBSERVED |
| `first_handshake detail=bind_token_empty` → `UUID_IS_EMPTY(-3)` → disconnect | the predicted credential boundary, no preceding GATT failure | SOURCE-BACKED (`q.a` empty-token guard) + RUNTIME-OBSERVED |
| disconnect (reason 19, remote-terminated) | clean, both sides log it | RUNTIME-OBSERVED |
| generic services 1800/1801 in DB | ignored by SDK | harness-substrate fact (Bumble stack), not a peripheral claim |

## Adversarial test (honest flake, then clean pass)

An earlier run this sprint with **three** peripherals on one netsimd
(2× plaud + pingpong) diverged earlier: `set_data_notify detail=send_fail`
→ `HANDSHAKE_CMD_SEND_FAIL(-4)`, Bumble showing discovery READs but **no**
ATT_WRITE. Timestamps show the same central MAC opening connections on two
peripherals within milliseconds — stack contention, not a peripheral bug.
Killing the extras and re-running with a **single** peripheral reproduces
the R4-S3 boundary exactly (`bind_token_empty`, CCCD 0200 received).
Lesson recorded, not a code change: one peripheral per netsimd for SDK runs.

## 8/9/10. Files, tests, frozen evidence

- Changed: `r5-s1/` only (this README + 2 evidence logs) + milestone rows in
  `PROJECT.md` / ledger §13. No `emulator/**`, `tests/**`, fixtures,
  `reference/**`, or frozen R1–R4 evidence touched.
- Tests: 154 passed before; re-run after (no code changes).
- `reference/**` verified clean; Bumble untouched.
