#!/bin/bash
set -u
ROOT=<repo>
SDK=<toolchain>/android-sdk
adbe() { "$SDK/platform-tools/adb" -s emulator-5554 "$@"; }
S=<scratchpad>
OUT=$S/r7-blank-token; mkdir -p $OUT
export ANDROID_SDK_ROOT=$SDK ANDROID_HOME=$SDK
nohup $SDK/emulator/emulator -avd mivi_test_34 -no-window -no-audio -no-snapshot -no-boot-anim > $OUT/avd.log 2>&1 &
adbe wait-for-device
booted=0; for i in $(seq 1 60); do [ "$(adbe shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { booted=1; break; }; sleep 5; done
echo "booted=$booted"; [ "$booted" = 1 ] || { echo "BOOT FAILED"; exit 1; }
adbe install -r $ROOT/r7/android-app/app/build/outputs/apk/debug/app-debug.apk | tail -1
adbe shell pm clear com.plaud.template > /dev/null
for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do adbe shell pm grant com.plaud.template android.permission.$p; done
adbe shell svc wifi disable; adbe shell svc data disable
sleep 3
echo "internet check (expect failure):"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -2; adbe shell "cat /proc/net/route" | head -3
adbe shell svc bluetooth enable; sleep 2
K3CAP_BUMBLE_LOG=INFO PYTHONDONTWRITEBYTECODE=1 PULLCAP_CAPTURE=$OUT/capture.json nohup $ROOT/.venv/bin/python $ROOT/r7/pull_capture_peripheral.py > $OUT/peripheral.log 2>&1 &
PP=$!
for i in $(seq 1 30); do grep -q PULLCAP_PERIPHERAL_READY $OUT/peripheral.log && break; sleep 1; done
adbe logcat -c
adbe shell am start -n com.plaud.template/.debug.PullCaptureActivity --es id "SYNTH-HIST-0001" --es mode export > /dev/null
sleep 65
adbe logcat -d > $OUT/logcat.log
echo "== result =="
grep -E "PULLCAP_(BIND|EXPORT_DONE|EXPORT_ERROR)" $OUT/logcat.log | sed 's/^.*PULLCAP_/PULLCAP_/'
echo "gen-key lines: $(grep -c -i 'gen-key' $OUT/logcat.log)"
echo "RSA-fetch lines: $(grep -c '正在获取 RSA' $OUT/logcat.log)"
echo "any https URL lines: $(grep -c 'URL: https://' $OUT/logcat.log)"
grep -i "partner\|clearPartner\|token" $OUT/logcat.log | grep -v PULLCAP | cut -c1-180 | head -8
kill $PP 2>/dev/null
adbe emu kill > /dev/null 2>&1; sleep 3; pkill -f netsimd; echo "ALL DONE"
