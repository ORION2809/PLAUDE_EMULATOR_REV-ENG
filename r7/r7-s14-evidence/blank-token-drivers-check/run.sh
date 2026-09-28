#!/bin/bash
# Blank-token, offline re-runs of the R7-S12 (K3) and R7-S14 (Wi-Fi) drivers, 2026-09-28.
set -u
ROOT=<repo>
SDK=<toolchain>/android-sdk
adbe() { "$SDK/platform-tools/adb" -s emulator-5554 "$@"; }
OUT=$1; mkdir -p "$OUT"
export ANDROID_SDK_ROOT=$SDK ANDROID_HOME=$SDK PYTHONDONTWRITEBYTECODE=1
nohup $SDK/emulator/emulator -avd mivi_test_34 -no-window -no-audio -no-snapshot -no-boot-anim > $OUT/avd.log 2>&1 &
adbe wait-for-device
booted=0; for i in $(seq 1 90); do [ "$(adbe shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { booted=1; break; }; sleep 5; done
echo "booted=$booted"; [ "$booted" = 1 ] || { echo "BOOT FAILED"; exit 1; }
adbe install -r $ROOT/r7/android-app/app/build/outputs/apk/debug/app-debug.apk | tail -1
adbe shell svc wifi disable; adbe shell svc data disable; sleep 3
echo "internet check (expect failure):"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -1
adbe shell svc bluetooth enable; sleep 2
prep() { adbe shell am force-stop com.plaud.template; adbe shell pm clear com.plaud.template > /dev/null
  for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do adbe shell pm grant com.plaud.template android.permission.$p; done; }
count() { echo "$1: gen-key lines $(grep -c -i 'gen-key' $2); https URL lines $(grep -c 'URL: https://' $2); RSA-fetch lines $(grep -c '正在获取 RSA' $2)"; }

# --- K3 (R7-S12) --------------------------------------------------------------
prep
nohup $ROOT/.venv/bin/python $ROOT/r7/k3_capture_peripheral.py android-netsim $OUT/k3-capture.json > $OUT/k3-peripheral.log 2>&1 &
PP=$!; for i in $(seq 1 30); do grep -q K3CAP_PERIPHERAL_READY $OUT/k3-peripheral.log && break; sleep 1; done
adbe logcat -c
adbe shell am start -n com.plaud.template/.debug.K3CaptureActivity --es id "SYNTH-HIST-0001" > /dev/null
sleep 40
adbe logcat -d > $OUT/k3-logcat.log
kill $PP 2>/dev/null; sleep 2
echo "== K3 =="; grep -E "K3CAP_(BIND|STAGE|RECOVERY|INIT)" $OUT/k3-logcat.log | sed 's/^.*K3CAP_/K3CAP_/' | head -8
count K3 $OUT/k3-logcat.log

# --- Wi-Fi (R7-S14), mode=open then mode=transfer -------------------------------
for mode in open transfer; do
  prep
  WIFICAP_CAPTURE=$OUT/wifi-$mode-capture.json nohup $ROOT/.venv/bin/python $ROOT/r7/wifi_capture_device.py > $OUT/wifi-$mode-peripheral.log 2>&1 &
  PP=$!; for i in $(seq 1 30); do grep -q WIFICAP_PERIPHERAL_READY $OUT/wifi-$mode-peripheral.log && break; sleep 1; done
  adbe forward tcp:18081 tcp:8081 > /dev/null
  adbe logcat -c
  adbe shell am start -n com.plaud.template/.debug.WifiCaptureActivity --es id "SYNTH-HIST-0001" --es mode $mode > /dev/null
  sleep 75
  adbe logcat -d > $OUT/wifi-$mode-logcat.log
  kill $PP 2>/dev/null; adbe forward --remove tcp:18081 > /dev/null 2>&1; sleep 2
  echo "== Wi-Fi mode=$mode =="; grep -E "WIFICAP_(BIND|BLE_WIFI_OPEN|CB_ERROR|END|EXPORT|CB_STATE)" $OUT/wifi-$mode-logcat.log | sed 's/^.*WIFICAP_/WIFICAP_/' | head -10
  count "Wi-Fi $mode" $OUT/wifi-$mode-logcat.log
done
adbe emu kill > /dev/null 2>&1; sleep 3; pkill -f netsimd; echo "ALL DONE"
