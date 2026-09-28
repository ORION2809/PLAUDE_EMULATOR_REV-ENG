#!/bin/zsh
# R7-S14 one run: fresh app state, fresh capture-device process, adb forward,
# the driver, read-only probes during the SDK's join window, then collection.
#   run.sh <name> <mode> [extra `am start` args...]
#   env: GRANT_NEARBY=1 also grants NEARBY_WIFI_DEVICES (control run)
#        WAIT_S (default 75) seconds to let the driver finish
# Archival like r7/r7-s13-evidence/batch*.sh: paths are this machine's.
# Nothing here taps, approves or configures any Wi-Fi network.
ROOT=/Users/mivi/Desktop/plaud-harness
ADB=/Users/mivi/mivi-toolchain/android-sdk/platform-tools/adb
EV=$ROOT/r7/r7-s14-evidence
PORT=18081
name=$1; mode=$2; shift 2
A="$ADB -s emulator-5554"
eval $A shell am force-stop com.plaud.template
eval $A shell pm clear com.plaud.template > /dev/null
for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do eval $A shell pm grant com.plaud.template android.permission.$p; done
[[ "$GRANT_NEARBY" == 1 ]] && eval $A shell pm grant com.plaud.template android.permission.NEARBY_WIFI_DEVICES
eval $A shell dumpsys package com.plaud.template | grep -E 'NEARBY_WIFI_DEVICES|CHANGE_NETWORK_STATE|ACCESS_FINE_LOCATION: granted' > $EV/perms-$name.txt
pkill -f wifi_capture_device.py; sleep 1
eval $A forward --remove tcp:$PORT 2>/dev/null
eval $A forward tcp:$PORT tcp:8081
eval $A forward --list > $EV/forward-$name.txt
K3CAP_BUMBLE_LOG=INFO PYTHONDONTWRITEBYTECODE=1 WIFICAP_HOST_PORT=$PORT WIFICAP_CAPTURE=$EV/capture-$name.json \
  nohup $ROOT/.venv/bin/python $ROOT/r7/wifi_capture_device.py > $EV/device-$name.log 2>&1 &
sleep 7
eval $A logcat -G 16M > /dev/null
eval $A logcat -c
eval $A logcat -v threadtime > $EV/.logcat-$name.full 2>/dev/null &
LOGPID=$!
t0=$(date +%s)
eval $A shell am start -n com.plaud.template/.debug.WifiCaptureActivity --es id "SYNTH-HIST-0001" --es mode $mode "$@" > /dev/null
probe() {  # read-only snapshots: is anything listening on 8081, what is on top, what does Wi-Fi see
  tag=$1
  {
    echo "### probe $tag at +$(( $(date +%s) - t0 ))s"
    echo "--- /proc/net/tcp{,6} rows with local port 1F91 (8081) ---"
    eval $A shell "cat /proc/net/tcp /proc/net/tcp6" | awk 'NR==1 || $2 ~ /:1F91$/'
    echo "--- top activity ---"
    eval $A shell dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity:' | head -3
    echo "--- connectivity requests from the app (specifier) ---"
    eval $A shell dumpsys connectivity | grep -i -E 'WifiNetworkSpecifier|SSID' | head -8
    echo "--- wifi status ---"
    eval $A shell cmd wifi status | head -6
    echo "--- scan results (SSIDs visible to the AVD) ---"
    eval $A shell cmd wifi list-scan-results | head -8
  } >> $EV/probes-$name.txt 2>&1
  eval $A shell screencap -p /sdcard/wificap-$tag.png
  eval $A pull /sdcard/wificap-$tag.png $EV/screen-$name-$tag.png > /dev/null 2>&1
}
: > $EV/probes-$name.txt
sleep 20; probe a
sleep 12; probe b
sleep $(( ${WAIT_S:-75} - 32 ))
kill $LOGPID 2>/dev/null; sleep 1
# app process lines (by pid) + system lines about the app's specifier request / the join dialog
APPPIDS=$(grep -E ' WIFICAP ' $EV/.logcat-$name.full | awk '{print $3}' | sort -u | tr '\n' '|' | sed 's/|$//')
awk -v pids="^($APPPIDS)$" '$3 ~ pids' $EV/.logcat-$name.full | grep -v -E 'onCharacteristicChanged|OpenGLRenderer|EGL_emulation|HWUI|Choreographer' > $EV/logcat-$name.filtered.log
grep -E 'WifiNetworkSpecifier|NetworkRequestDialog|com\.plaud\.template' $EV/.logcat-$name.full | grep -v -E 'CoreBackPreview|WindowManagerShell|Transition' > $EV/logcat-$name.system.log
rm -f $EV/.logcat-$name.full
pkill -INT -f wifi_capture_device.py; sleep 2; pkill -f wifi_capture_device.py
eval $A shell run-as com.plaud.template find files -type f 2>/dev/null > $EV/appfiles-$name.txt
echo "##### $name (mode=$mode) #####"
grep -o 'WIFICAP_.*' $EV/logcat-$name.filtered.log | grep -v -E 'WIFICAP_STAGE|WIFICAP_SCAN_STARTED' | cut -c1-220
echo "--- device ---"
grep -o 'WIFICAP_[A-Z_]*.*' $EV/device-$name.log | cut -c1-200 | uniq -c | head -40
