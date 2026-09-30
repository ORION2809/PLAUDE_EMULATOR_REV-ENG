#!/bin/bash
# R7-S15: the real SDK's Wi-Fi transfer with a joinable "PLAUD0001" network (2026-09-29).
#
# R7-S14 stopped at the join: the AVD's simulated Wi-Fi (netsim) only offered
# "AndroidWifi".  netsimd can instead offer a network with a chosen SSID and
# passphrase (`--wifi <ssid> <password>`); the SDK asks Android to join
# PLAUD0001 with passphrase 10000001 (bytecode, r7/r7-s14-wifi-real-sdk.md).
# Egress guard.  Run 1 used netsim's `--debug-no-network`; it also stopped the
# network's DHCP server, so the phone associated and completed the WPA2
# handshake but never got an address, and the SDK gave up at 30 s.  From run 2
# DHCP stays on and egress is cut another way: the emulated DNS server is
# 127.0.0.1 on the host (nothing listens, so no name resolves) and every TCP
# connection goes through a dead proxy (127.0.0.1:9).  The shell probes below
# (ping, nc) run while the phone is on PLAUD0001, but that network is private to
# the app that requested it: the shell has no route, so they fail with "Network
# is unreachable" before the DNS server or the proxy is used.  They show that the
# phone's default network is cut, not the two guards; testing those needs a probe
# bound to the requested network.  The SDK's only cloud contact seen so far,
# gen-key, needs a hostname and a non-blank token; the drivers pass a blank one.
# The pen's Wi-Fi side is our emulator on the host, reached through
# `adb forward tcp:18081 tcp:8081` exactly as in R7-S14.
#
# Android shows a "connect to this device" dialog for an app's network request;
# R7-S14 never approved it.  approve_dialog() below taps its button through
# uiautomator, as a user would.
#
# Usage: r7/r7-s15-evidence/run.sh <out-dir> [modes...]   (modes: transfer, open; default transfer)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK=${ANDROID_SDK:?set ANDROID_SDK to the Android SDK folder (emulator/, platform-tools/)}
adbe() { "$SDK/platform-tools/adb" -s emulator-5554 "$@"; }
OUT=$1; shift; MODES=${*:-transfer}; mkdir -p "$OUT"
export ANDROID_SDK_ROOT=$SDK ANDROID_HOME=$SDK PYTHONDONTWRITEBYTECODE=1
NETSIM_ARGS=${NETSIM_ARGS:-"--wifi PLAUD0001 10000001 --pcap"}

if "$SDK/platform-tools/adb" devices | grep -q '^emulator-'; then
  echo "an emulator is already running; stop it first (this script restarts netsimd, which all emulators share)"; exit 1
fi
pkill -f netsimd 2>/dev/null; sleep 2   # netsimd reads its flags only when it starts
PP=""
cleanup() { [ -n "$PP" ] && kill "$PP" 2>/dev/null; adbe forward --remove tcp:18081 > /dev/null 2>&1
  adbe emu kill > /dev/null 2>&1; sleep 3; pkill -f netsimd 2>/dev/null; }
trap cleanup EXIT
trap 'exit 130' INT TERM
nohup $SDK/emulator/emulator -avd mivi_test_34 -no-window -no-audio -no-snapshot -no-boot-anim \
  -dns-server 127.0.0.1 -http-proxy 127.0.0.1:9 -netsim-args "$NETSIM_ARGS" > "$OUT/avd.log" 2>&1 &
adbe wait-for-device
booted=0; for i in $(seq 1 120); do [ "$(adbe shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { booted=1; break; }; sleep 5; done
echo "booted=$booted"; [ "$booted" = 1 ] || { echo "BOOT FAILED"; exit 1; }
ps -o args= -p "$(pgrep -f netsimd | head -1)" > "$OUT/netsimd-args.txt" 2>/dev/null
echo "netsimd: $(cat "$OUT/netsimd-args.txt")"
adbe install -r "$ROOT/r7/android-app/app/build/outputs/apk/debug/app-debug.apk" | tail -1
adbe shell svc data disable; adbe shell svc wifi enable; adbe shell svc bluetooth enable; sleep 8
adbe shell cmd wifi list-scan-results > "$OUT/wifi-scan.txt" 2>&1
echo "scan: $(grep -c PLAUD0001 "$OUT/wifi-scan.txt") PLAUD0001 line(s)"
echo "internet check (expect failure):"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -1 | tee "$OUT/ping.txt"

prep() { adbe shell am force-stop com.plaud.template; adbe shell pm clear com.plaud.template > /dev/null
  for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION NEARBY_WIFI_DEVICES; do
    adbe shell pm grant com.plaud.template android.permission.$p 2>/dev/null; done; }
approve_dialog() {  # tap the positive button of Android's network-request dialog when it appears
  local log=$1 end=$((SECONDS + 60))
  while [ $SECONDS -lt $end ]; do
    adbe shell uiautomator dump /sdcard/ui.xml > /dev/null 2>&1
    local xml; xml=$(adbe shell cat /sdcard/ui.xml 2>/dev/null)
    if echo "$xml" | grep -q -i 'PLAUD0001'; then
      local b; b=$(echo "$xml" | tr '>' '\n' | grep -i -E 'text="(Connect|Allow|OK)"' | grep -o 'bounds="\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]"' | head -1)
      if [ -n "$b" ]; then
        local x1 y1 x2 y2; read -r x1 y1 x2 y2 <<< "$(echo "$b" | grep -o '[0-9]*' | tr '\n' ' ')"
        adbe shell input tap $(( (x1 + x2) / 2 )) $(( (y1 + y2) / 2 ))
        echo "$(date +%T) dialog approved at $(( (x1 + x2) / 2 )),$(( (y1 + y2) / 2 ))" | tee -a "$log"
        echo "$xml" > "${log%.log}-dialog.xml"
        return 0
      fi
    fi
    sleep 1
  done
  echo "$(date +%T) no dialog seen in 60 s" | tee -a "$log"; return 1
}

for mode in $MODES; do
  prep
  WIFICAP_CAPTURE=$OUT/wifi-$mode-capture.json nohup "$ROOT/.venv/bin/python" "$ROOT/r7/wifi_capture_device.py" > "$OUT/wifi-$mode-peripheral.log" 2>&1 &
  PP=$!; for i in $(seq 1 30); do grep -q WIFICAP_PERIPHERAL_READY "$OUT/wifi-$mode-peripheral.log" && break; sleep 1; done
  adbe forward tcp:18081 tcp:8081 > /dev/null
  adbe logcat -c
  adbe shell am start -n com.plaud.template/.debug.WifiCaptureActivity --es id "SYNTH-HIST-0001" --es mode "$mode" > /dev/null
  approve_dialog "$OUT/wifi-$mode-dialog.log" &
  ( sleep 25   # egress checks while the phone should be on PLAUD0001
    { date +%T; adbe shell cmd wifi status 2>&1 | head -5
      echo "ping IP:"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -2
      echo "ping name:"; adbe shell ping -c 1 -W 3 platform-jp.plaud.ai 2>&1 | tail -2
      echo "tcp 1.1.1.1:443:"; adbe shell "toybox nc -w 3 1.1.1.1 443 < /dev/null && echo TCP-OPEN || echo TCP-FAILED" 2>&1 | tail -1
    } > "$OUT/wifi-$mode-egress.txt" 2>&1 ) &
  sleep 150
  adbe logcat -d > "$OUT/wifi-$mode-logcat.log"
  adbe shell cmd wifi status > "$OUT/wifi-$mode-wifi-status.txt" 2>&1
  kill $PP 2>/dev/null; adbe forward --remove tcp:18081 > /dev/null 2>&1; sleep 2
  echo "== Wi-Fi mode=$mode =="
  grep -a -E "WIFICAP_(BIND|BLE_WIFI_OPEN|CB_|END|EXPORT|DONE|ERROR)" "$OUT/wifi-$mode-logcat.log" | sed 's/^.*WIFICAP_/WIFICAP_/' | head -20
  echo "gen-key lines: $(grep -a -c -i 'gen-key' "$OUT/wifi-$mode-logcat.log")"
done
PP=""; echo "ALL DONE"   # the EXIT trap stops the emulator and netsimd
