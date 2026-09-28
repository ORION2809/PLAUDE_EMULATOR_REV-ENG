# R7-S12: genuine SDK k3 captured over netsim (RUNTIME, synthetic-only)

Verdict: **k3 CONSTRUCTION IS SDK_PROVEN + BYTECODE_PROVEN + RUNTIME_PROVEN +
EMULATOR_INTEGRATION_PROVEN.** The unmodified official Android SDK built its
first_handshake frame from a synthetic identifier, wrote it to 1910/2BB1 of our
emulator, the emulator accepted it, and the SDK reported `bleBind status=0`.
Captured bytes match the bytecode model byte-for-byte, in padding (run 1) and
truncation (run 3); an empty identifier writes nothing (run 2, falsifier).

This is **not** authentication and **not** evidence about real hardware. The
device that accepted the token is ours; accepting any token is HARNESS_POLICY.

## Topology

```
API-34 AVD (mivi_test_34, arm64, google_apis)          netsimd :50922
  com.plaud.template (r7/android-app, our copy)            │
    K3CaptureActivity ──► sdk.PlaudDeviceAgent ──► Android BT stack ──► netsim
                                                                           │
  r7/k3_capture_peripheral.py  ◄── bumble android-netsim (host mode) ◄──────┘
    CapturingPeripheral(PlaudPeripheral, portVersion 7, F0:1A:2B:3C:4D:5E)
      mirrors every 2BB1 write → r7/r7-s12-k3-capture-<run>.json
      answers l3(status 0, tz=<policy>) so the handshake completes
```

* SDK binary: `reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar`,
  sha256 `041a6f8814d350dbc8bcd4a515487136529bb8bb4368fc88c9b36c9a386aedce`
  (the copy bundled at `r7/android-app/app/libs/plaud-sdk.aar` is the same file).
* Nothing under `reference/**` was modified. The template app copy in `r7/`
  gained one debug Activity (public SDK API only) and a manifest entry.
* Synthetic init token: a well-formed but fake JWT, unused by the recovery path.

## Why the recovery entry point is cloud-free (bytecode)

> **Correction, 25 September 2026.** The recovery *entry point* is cloud-free, as
> shown below. The driver around it was not: its `initSDK` call passed a synthetic
> token, and `NiceBuildSdk.initSdk` answers a non-blank token by POSTing
> `partner/sdk/gen-key` to `platform-jp.plaud.ai`. This report's logs were filtered
> to K3CAP/PenBleSDK lines and do not show SDK network activity, but the later
> R7-S13/S14 logs show one such request per app start (19 of those 22 logs record a 401;
> in R7-S13 runs 11–13 a failed DNS lookup stopped the request before it reached the
> server), so these four runs very likely sent it too. The drivers now pass a blank token
> (`r7/r7-s14-wifi-real-sdk.md` D7; offline check in
> `r7/r7-s14-evidence/blank-token-offline-check/`).

`PlaudDeviceAgent.recoveryConnectBleDevice(BleDevice, String)` (ALL.txt:2653):

1. `startsWith("client_user_")` → `removePrefix` (2678-2688)
2. `replace("-", "")` (2691-2699)
3. empty → `Log.e("recoveryConnect: historicalUserId 为空，中止")`, `bleConnectState(2)`, return (2703-2718)
4. `length != 32` → warning "底层会补零/截断" (lower layer pads/truncates) (2719-2732)
5. `t3.a(device, token, "", "", isForceClear=true)` (2758-2761) — **no**
   `resolveHandshakeToken`, no network call (contrast `connectBleDevice`, 2620-2631)

`q.a()` (first_handshake, 39357): `TextUtils.isEmpty(g)` → `bind_token_empty`;
else `new k3(z.h(), 0, g, portVersion)` → `enPkg()` → 2BB1. With an advertised
portVersion < 20, `q.b0` reaches `q.a()` without the RSA pre-handshake (ledger
§4.6). `isForceClear` only selects the 0xFE20 marker, which is consumed by the
pre-handshake — inert here (predicted, and no marker frame was observed).

## k3 wire layout (k3.enPkg, ALL.txt:87361-87434)

```
[0]     01          protocolType (packHead)
[1..2]  01 00       u16le opcode 1
[3]     02          constant (ledger U8: no reader found)
[4]     00          z.h() — hard 0 in this build (ALL.txt:76051-76054); unconditional
[5]     00          stage 0 = k3; written iff portVersion >= 3
[6..]   token       ASCII, '0'(0x30)-padded / truncated to 32 (pv>=9) else 16
```

`u4.b()` trims trailing **0x00** only, so 0x30 padding is preserved. Model:
`emulator/plaudsim/handshake.py::build_k3` / `build_k3_from_historical_id`.

## Runs

| run | id passed | SDK log `handshakeToken=` | k3 on 2BB1 (hex) | stages | bind |
|---|---|---|---|---|---|
| 1 | `SYNTH-HIST-0001` | `SYNTHHIST0001` (+ length≠32 warning) | `01010002000053594e54484849535430303031303030` (22 B) | start→gatt_connect→set_notify→set_data_notify(new_battery_service_port_7)→first_handshake→**old_protocol_ok**→sync_time | `status=0 protVersion=0 tz=0` |
| 2 | `client_user_` | — (abort) | **none** | none; `bleConnectState(2)` | — |
| 3 | `client_user_0123456789abcdef0123456789ABCDEF` | `0123456789abcdef0123456789ABCDEF` (no warning) | `01010002000030313233343536373839616263646566` (22 B; truncated to 16) | as run 1 | `status=0 protVersion=0 tz=0` |
| 4 | `SYNTH-HIST-0001` (emulator: opcode 8 answered, `l3_timezone=5`) | `SYNTHHIST0001` | as run 1 | as run 1 | `status=0 protVersion=5 tz=5` |

Byte-for-byte comparison (`tests/test_r7_s12_k3_runtime.py`): run 1 and run 3
captures equal `build_k3_from_historical_id(id, 7)` exactly, and the emulator's
`parse_handshake_request` reads them back as `token="SYNTHHIST0001000"` /
`"0123456789abcdef"`, `stage=0`, `agent_value=0`.

## The complete genuine connect sequence (all runs with a bind)

```
t+0.000  op 1   01010002000053594e54484849535430303031303030   k3 (22 B)
t+0.090  op 4   0104004bb3b36a051e                             syncTime: u32le unix, i8 tzH=5, i8 tzM=30 (IST)
t+0.165  op 9   010900                                         battStatus
t+0.230  op 3   010300                                         getState
t+0.307  op 8   010800010f00                                   CommonSettings READ ENABLE_VAD
t+0.340  op 8   010800011100                                   CommonSettings READ REC_MODE
```

Opcode 8 was **unknown to the emulator** before this run (rejected as
`unsupported_opcode` in runs 1-3; the SDK bound anyway because `bleBind` fires
on the CONNECTED transition, before the reads). It is now implemented and
answered (run 4: zero rejects). Full decode in ledger §5.12.

## Two further facts the runs settled

* **l3 round-trip.** The SDK's own log in run 4:
  `old HandShakeRsp: l3{status=0, portVersion=7, timezone=5, timezoneMin=0,
  audioChannel=1, supportWifi=false, noNsAgc=false, isOggAudio=false,
  versionType=V, version=1}` — every field of `encode_l3` read back by the real
  parser (R5-S4 layout, now RUNTIME_PROVEN).
* **`bleBind` facade quirk.** `protVersion=0 tz=0` in runs 1/3 was not an
  emulator gap: `btStatusChange` (ALL.txt:878-961) passes `status=0` literally
  and loads both remaining arguments from `t3.X()` = `q.n`, which the
  old-HandShakeRsp handler (44308) sets from `l3.e()` = the l3 **timezone**
  byte, not `l3.c()` = portVersion. Prediction "l3 tz=5 ⇒ protVersion=5" held.

## Reproduce

```bash
# toolchain used: JDK 17 (<jdk17>), Android SDK (<android-sdk>), AVD mivi_test_34,
# gradle 8.2 (cached); replace <jdk17> / <android-sdk> with your own install paths
export JAVA_HOME=<jdk17>/Contents/Home ANDROID_HOME=<android-sdk>   # macOS JDK bundle layout
( cd r7/android-app && ./gradlew :app:assembleDebug --offline )      # local.properties: synthetic token only
emulator -avd mivi_test_34 -no-window -no-audio -no-snapshot -no-boot-anim &
adb -s emulator-5554 wait-for-device
adb -s emulator-5554 install -r r7/android-app/app/build/outputs/apk/debug/app-debug.apk
for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do adb -s emulator-5554 shell pm grant com.plaud.template android.permission.$p; done
adb -s emulator-5554 shell svc bluetooth enable
.venv/bin/pip install grpcio protobuf                                  # netsim transport deps
export TMPDIR=$(dirname "$(find /var/folders -maxdepth 4 -name netsim.ini | head -1)") K3CAP_BUMBLE_LOG=DEBUG
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python r7/k3_capture_peripheral.py android-netsim r7/k3-capture.json &
adb -s emulator-5554 logcat -c
adb -s emulator-5554 shell am start -n com.plaud.template/.debug.K3CaptureActivity --es id "SYNTH-HIST-0001"
sleep 25; adb -s emulator-5554 logcat -d | grep -E "K3CAP|PlaudDeviceAgent|PenBleSDK"; cat r7/k3-capture.json
```

Pitfalls met and fixed: bumble's `setup_basic_logging("WARNING")` silences the
peripheral's INFO "ready" line (looked like a hang); zsh does not word-split
`$ADB` with embedded flags; `adb` must target `emulator-5554` explicitly when a
physical phone is also attached (the physical phone was never used).

## Files

```
r7/k3_capture_peripheral.py                 capture peripheral (frozen PlaudPeripheral + write mirror)
r7/android-app/app/src/debug/java/com/plaud/template/debug/K3CaptureActivity.kt   (src/main until 28 Sep 2026)
r7/r7-s12-k3-capture-run1.json              run 1 writes (also r7/k3-capture.json = last run)
r7/r7-s12-k3-capture-run3-truncate.json     run 3 writes
r7/r7-s12-k3-capture-run4-settings.json     run 4 writes
r7/r7-s12-logcat-run{1,2-negative,3-truncate,4-settings}.log
r7/r7-s12-peripheral-run{1,2-negative,3-truncate,4-settings}.log
tests/test_r7_s12_k3_runtime.py             golden replay (8 tests)
tests/test_r7_s12_common_settings.py        opcode 8 (19 tests)
```
