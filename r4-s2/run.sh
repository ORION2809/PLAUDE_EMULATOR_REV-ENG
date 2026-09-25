#!/usr/bin/env bash
# R4-S2 reproduce script (EXPERIMENTAL, disposable).
# Proves: Android runtime <-> netsim <-> Bumble BLE transport (ping/pong).
# Prerequisites (all present on this machine, verified 2026-09-22):
#   Android SDK  + build-tools 34 + JDK 17 Temurin + emulator + AVD mivi_test_34
#   /tmp/r4s2-venv with grpcio, protobuf, bumble (editable or installed)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ADB=/Users/mivi/mivi-toolchain/android-sdk/platform-tools/adb
EMU=/Users/mivi/.local/bin/emulator
VENV=/tmp/r4s2-venv/bin/python
APK="$HERE/android-app/app/build/outputs/apk/debug/app-debug.apk"

echo "=== 1. boot the AVD (Bluetooth via netsim packet streamer) ==="
$EMU -avd mivi_test_34 -no-window -no-audio -no-snapshot &
echo "=== 2. wait for boot (poll) ==="
$ADB wait-for-device
for _ in $(seq 1 40); do
  [ "$($ADB shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && break
  sleep 10
done
$ADB shell getprop sys.boot_completed | tr -d '\r'
echo "=== 3. start the Bumble peripheral (attaches to netsimd via .ini) ==="
export TMPDIR
TMPDIR=$(dirname "$(find /var/folders -maxdepth 4 -name netsim.ini 2>/dev/null | head -1)")
[ -f "$TMPDIR/netsim.ini" ] || { echo "no netsim.ini yet; is the emulator up?"; exit 1; }
nohup $VENV "$HERE/bumble_pingpong.py" > /tmp/r4s2-bumble.log 2>&1 &
sleep 8
grep -q PINGPONG_PERIPHERAL_READY /tmp/r4s2-bumble.log
echo "=== 4. build + install + launch the test app ==="
export ANDROID_HOME=/Users/mivi/mivi-toolchain/android-sdk
export JAVA_HOME=/Users/mivi/.local/opt/jdk-17.0.20.1+1/Contents/Home
(cd "$HERE/android-app" && ./gradlew assembleDebug --console=plain -q)
$ADB install -r "$APK"
$ADB shell pm grant com.example.pingpong android.permission.BLUETOOTH_SCAN || true
$ADB shell pm grant com.example.pingpong android.permission.BLUETOOTH_CONNECT || true
$ADB logcat -c
$ADB shell am start -n com.example.pingpong/.MainActivity
echo "=== 5. collect evidence (60s) ==="
sleep 45
echo "--- Android side ---"
$ADB logcat -d | grep -E "PingPong" | head -25
echo "--- Bumble side ---"
grep -E "PINGPONG_WRITE_RX|PINGPONG_NOTIF_TX" /tmp/r4s2-bumble.log | head -5
