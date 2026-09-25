# R4-S3: real Plaud Android SDK against the Bumble emulator (EXPERIMENTAL)

Result: **the real SDK reaches the handshake-token boundary through the
verified BLE transport** (see Evidence below). No further.

## What runs here

- `plaud_netsim_peripheral.py` — frozen `plaudsim.PlaudPeripheral`
  (portVersion 7, synthetic state) attached to the netsim transport.
  Only addition vs virtual-link tests: a legacy-compatible advertising
  payload (netsimd rejects extended advertising) and a static random MAC
  (restarts must not rotate the address the app cached).
- Template APK: built from `reference/plaud-org/plaud-sdk-public/android`
  with stock `gradlew` (Gradle 8.9, AGP 8.5.2, JDK Temurin 17). No source
  changes; empty partner token (no `local.properties`).
- `logcat-sdk-sequence.log` — filtered SDK connect sequence from a run.

## Reproduce

```bash
# 1. boot AVD (Bluetooth via netsim packet streamer), API 34
emulator -avd mivi_test_34 -no-window -no-audio -no-snapshot &
adb wait-for-device
# 2. attach Bumble peripheral (reads netsim port from $TMPDIR/netsim.ini)
TMPDIR=<same-tmp-as-emulator> python plaud_netsim_peripheral.py
# 3. build + install template (first build downloads Gradle/AGP deps)
export ANDROID_HOME=<sdk> JAVA_HOME=<temurin17>
cd reference/plaud-org/plaud-sdk-public/android && ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant com.plaud.template android.permission.BLUETOOTH_SCAN
adb shell pm grant com.plaud.template android.permission.BLUETOOTH_CONNECT
# 4. launch, tap Get Started, tap Connect on "Plaud Note Pro / SN: 8810000001"
# 5. adb logcat | grep -E "PenBleSDK|PlaudDeviceAgent|bleConnectStage|BluetoothGatt"
```

## Evidence (2026-09-22)

Scan → `Plaud Note Pro / SN: 8810000001` listed (manufacturer data parsed,
product name resolved) → tap Connect → RSA/SN/token warnings (no user
access token) → `BluetoothGatt: connect()` → CONNECTED → `configureMTU
mtu: 255` → negotiated **517** → `discoverServices` → `onSearchComplete
status: 0` → `btStatusChange: CONNECTED` → `New Battery Service`
(portVersion 7 ≥ 5 path) → `setCharacteristicNotification(2BB0)` +
CCCD write `0200` (indication; Bumble logged `CCCDs: {16: b'\x02\x00'}`)
→ `first_handshake detail=bind_token_empty` →
`bleConnectFail-UUID_IS_EMPTY{errCode=-3}` → disconnect.

First blocking point: **missing handshake token** (account credential).
No GATT/command failure precedes it; flaky earlier runs traced to the
peripheral's rotating MAC vs the app's cached scan entry (fixed with a
static address), not to transport.
