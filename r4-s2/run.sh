#!/usr/bin/env bash
# R4-S2 reproduce script (EXPERIMENTAL, disposable).
# Proves: Android runtime <-> netsim <-> Bumble BLE transport (ping/pong).
# Prerequisites (verified on the original machine 2026-09-22):
#   Android SDK  + build-tools 34 + JDK 17 Temurin + emulator + an API-34 AVD
#   with hw.bluetooth=yes, and a Python with grpcio, protobuf, bumble.
#
# Machine-specific locations come from the environment (nothing is hard-coded):
#   ANDROID_HOME  Android SDK root (falls back to ANDROID_SDK_ROOT, then
#                 ~/Library/Android/sdk, Android Studio's macOS default)
#   JAVA_HOME     JDK 17 (on macOS falls back to `/usr/libexec/java_home -v 17`)
#   ADB, EMU      override the adb / emulator binaries (default: inside ANDROID_HOME)
#   AVD           AVD name (default mivi_test_34, the name used for R4-S2)
#   PY            Python with bumble + grpcio (default: the repo's .venv if it
#                 exists, else /tmp/r4s2-venv as in the original R4-S2 run)
#   NETSIM_DIR    directory holding the emulator's netsim.ini (default: searched
#                 under /var/folders, where the macOS emulator writes it)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
if [ -z "${JAVA_HOME:-}" ] && [ -x /usr/libexec/java_home ]; then
  JAVA_HOME="$(/usr/libexec/java_home -v 17 2>/dev/null || true)"
fi
: "${JAVA_HOME:?set JAVA_HOME to a JDK 17 home}"
export ANDROID_HOME JAVA_HOME
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"
EMU="${EMU:-$ANDROID_HOME/emulator/emulator}"
AVD="${AVD:-mivi_test_34}"
if [ -z "${PY:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY=/tmp/r4s2-venv/bin/python; fi
fi
for tool in "$ADB" "$EMU" "$PY"; do
  [ -x "$tool" ] || { echo "not executable: $tool (set ANDROID_HOME, ADB, EMU or PY)" >&2; exit 1; }
done
APK="$HERE/android-app/app/build/outputs/apk/debug/app-debug.apk"

echo "=== 1. boot the AVD (Bluetooth via netsim packet streamer) ==="
"$EMU" -avd "$AVD" -no-window -no-audio -no-snapshot &
echo "=== 2. wait for boot (poll) ==="
"$ADB" wait-for-device
for _ in $(seq 1 40); do
  [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && break
  sleep 10
done
"$ADB" shell getprop sys.boot_completed | tr -d '\r'
echo "=== 3. start the Bumble peripheral (attaches to netsimd via .ini) ==="
export TMPDIR
TMPDIR="${NETSIM_DIR:-$(dirname "$(find /var/folders -maxdepth 4 -name netsim.ini 2>/dev/null | head -1)")}"
[ -f "$TMPDIR/netsim.ini" ] || { echo "no netsim.ini yet; is the emulator up?"; exit 1; }
nohup "$PY" "$HERE/bumble_pingpong.py" > /tmp/r4s2-bumble.log 2>&1 &
sleep 8
grep -q PINGPONG_PERIPHERAL_READY /tmp/r4s2-bumble.log
echo "=== 4. build + install + launch the test app ==="
(cd "$HERE/android-app" && ./gradlew assembleDebug --console=plain -q)
"$ADB" install -r "$APK"
"$ADB" shell pm grant com.example.pingpong android.permission.BLUETOOTH_SCAN || true
"$ADB" shell pm grant com.example.pingpong android.permission.BLUETOOTH_CONNECT || true
"$ADB" logcat -c
"$ADB" shell am start -n com.example.pingpong/.MainActivity
echo "=== 5. collect evidence (60s) ==="
sleep 45
echo "--- Android side ---"
"$ADB" logcat -d | grep -E "PingPong" | head -25
echo "--- Bumble side ---"
grep -E "PINGPONG_WRITE_RX|PINGPONG_NOTIF_TX" /tmp/r4s2-bumble.log | head -5
