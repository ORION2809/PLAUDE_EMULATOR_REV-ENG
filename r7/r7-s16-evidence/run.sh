#!/bin/bash
# R7-S16: the real SDK's Wi-Fi transfer with the pen's WebSocket entering the phone through its Wi-Fi
# interface (wlan0), not through `adb forward`.  Written 30 Sep 2026.  NOT YET RUN.
#
# WHAT THIS IS, AND WHAT IT IS NOT.  The Wi-Fi here is the EMULATOR'S OWN simulated Wi-Fi, which
# Google calls "the previous networking model" (`-feature -WiFiPacketStream`,
# developer.android.com/studio/run/emulator-networking-interconnect): the guest's wlan0
# (mac80211_hwsim over virtio-wifi) exchanges 802.11 frames with an access point that runs inside the
# emulator process (HostapdController, WPA2-PSK/CCMP), and the AP's data frames go to a QEMU
# user-mode network (slirp) netdev named `virtio-wifi`.  It is NOT netsim's shared Wi-Fi medium
# (R7-S15's).  Bluetooth still goes through netsim (a separate feature, BluetoothEmulation).
#
# WHY NOT NETSIM.  netsimd 37.1.11 has no way for host traffic to enter its Wi-Fi network
# (android.googlesource.com/platform/tools/netsim, branch main, read 30 Sep 2026):
#   * no port forwarding: no flag (rust/daemon/src/args.rs), no config field (proto/netsim/config.proto,
#     SlirpOptions fields 1-18), slirp_run uses only http_proxy and host_dns
#     (rust/daemon/src/wifi/libslirp.rs, "TODO: Convert ProtoSlirpOptions"), and the slirp command enum
#     has no host forward (rust/libslirp-rs/src/libslirp.rs:132-138);
#   * `--wifi-tap` is `todo!()` (rust/daemon/src/wireless/wifi_manager.rs:46-47);
#   * a second station on PLAUD0001 would replace the phone's key: netsim's AP driver keeps ONE pairwise
#     key (platform/external/wpa_supplicant_8, branch emu-master-dev,
#     src/drivers/driver_virtio_wifi.c:24-31; set_key overwrites it, :446-454) and hostapd-rs encrypts and
#     decrypts every unicast frame with it (rust/hostapd-rs/src/hostapd.rs:280-392).
#   A netsim-faithful run needs a netsimd patched with a slirp host forward; it cannot be built here.
#
# HOW THE PEN GETS IN (platform/external/qemu, branch emu-master-dev):
#   * `-wifi-user-mode-options <opts>` becomes `-netdev user,id=virtio-wifi,<opts>` INSIDE the qemu
#     process (android-qemu2-glue/main.cpp:3104-3110; the default is dhcpstart=10.0.2.16).  Options used:
#     dhcpstart=10.0.2.16,restrict=on,hostfwd=tcp:127.0.0.1:18081-10.0.2.16:8081
#   * the options are validated against an allowlist that includes dhcpstart, restrict and hostfwd
#     (android/emu/cmdline/src/android/cmdline-option.cpp:433-461).  On any refusal main.cpp replaces the
#     WHOLE string with the default, so restrict is off for the entire boot; the monitor fallback
#     (`hostfwd_add virtio-wifi tcp:127.0.0.1:18081-10.0.2.16:8081`) can restore the forward but not
#     restrict, so after a refusal a transfer runs only with REQUIRE_RESTRICT=0.
#   * the pen (r7/wifi_capture_device.py, UNCHANGED) dials ws://127.0.0.1:18081; QEMU accepts on the host
#     and opens a connection to 10.0.2.16:8081 on the Wi-Fi netdev.  libslirp gives a host-loopback source
#     the gateway address, so the phone should see 10.0.2.2 (libslirp src/socket.c sotranslate_accept).
#   * restrict=on isolates the guest on that network; DHCP and host forwards still work
#     (libslirp src/udp.c, the bootp branch precedes the restricted check; src/tcp_input.c:383-391; pings
#     to the gateway are answered by libslirp itself, src/ip_icmp.c:206-210).
#   * the network gets the pen's SSID and passphrase from the console command `wifi add PLAUD0001 10000001`
#     (android/android-emu/android/console.cpp:994-1005), which the WifiConfigurable feature gates
#     (console.cpp:181-184; off in emulator/lib/advancedFeatures.ini).
#
# SIDE EFFECT ON THE SDK INSTALL, AND ITS UNDO.  `wifi add` makes HostapdController::setSsid rewrite
# the file hostapd started from, which on the first boot is the SHARED template
# $ANDROID_SDK/emulator/lib/hostapd.conf (HostapdController.cpp:89-93, 135, 157-176, 221-227).  Every
# later boot without WiFiPacketStream would then start a WPA2 "PLAUD0001" AP.  So this script refuses to
# start unless the template has its pristine sha256 (HOSTAPD_TEMPLATE_SHA256), copies it into the output
# directory, and restores it after the emulator has stopped (hostapd-template.txt records what happened).
# The BSSID seen in the scan (00:13:10:95:fe:0b, written by buildHostapdConfig, HostapdController.cpp:60)
# therefore shows that `wifi add` took effect; the template's BSSID, 00:13:10:85:fe:01, equals netsim's, so
# the BSSID does NOT tell the emulator's AP from netsim's: E2 does that.
#
# EVIDENCE (README.md, E1-E10): the Wi-Fi netdev as qemu built it, from the `-verbose` "QEMU options list"
# argv lines in avd.log (qemu-core-args.txt; `ps` only shows the requested launcher options,
# qemu-args.txt) and, with MONITOR=1, the monitor's `info network`/`info usernet`; avd.log without
# "Successfully initialized netsim WiFi"; the host port owned by QEMU and no `adb forward` on it; PLAUD0001
# with BSSID 00:13:10:95:fe:0b; the SDK's "Device connected: 10.0.2.2"; the guest's /proc/net/tcp{,6} row
# 10.0.2.16:8081 <-> 10.0.2.2 ESTABLISHED (hex 1002000A:1F91 <-> 0202000A:xxxx 01, or its ::ffff: form in
# tcp6); a `filter-dump` pcap of netdev virtio-wifi checked by check_wifi_pcap.py against the pen's capture,
# with the file re-hashed; the Ethernet netdev's pcap (emulator `-tcpdump` covers only `mynet`,
# main.cpp:3123-3126) with no port-8081 packet; Wi-Fi counters and the egress record (TCP, UDP and ICMP
# echo); and a FWD=0 control run in which the pen cannot connect although the SDK's server listened.
#
# NO `adb forward` IS CREATED ANYWHERE IN THIS SCRIPT.  The QEMU monitor (MONITOR=1, the default) listens
# without authentication on 127.0.0.1:$MONITOR_PORT until this script stops the emulator (the EXIT trap
# stops it by `emu kill`, then by signalling this run's own emulator PIDs): anyone on this Mac can control
# the VM through it meanwhile.  MONITOR=0 disables it; restrict and the forward are still read from
# avd.log, but the fallback and the `info usernet` evidence are lost.
# NETSIMD: only processes named exactly `netsimd` are touched.  A netsimd that is already running makes
# the script refuse (KILL_NETSIMD=1 stops it first); at exit only the netsimd that served this run is
# stopped.
#
# Usage:
#   r7/r7-s16-evidence/run.sh <out-dir> probe            # boot, check the six unknowns, no SDK run
#   r7/r7-s16-evidence/run.sh <out-dir> probe transfer   # probe, then the SDK transfer, same boot
#   FWD=0 r7/r7-s16-evidence/run.sh <out-dir> control    # control boot without the host forward
# Environment:
#   ANDROID_SDK (required)  AVD (mivi_test_34)  APK (debug app)  PY (.venv python)  HOST_PORT (18081)
#   FWD (1)  HOSTFWD_VIA=options|monitor (options)  MONITOR (1)  MONITOR_PORT (45454)
#   REQUIRE_RESTRICT (1: refuse transfer/e2e probe unless restrict=on is in the Wi-Fi netdev)
#   PROBE_E2E (1: in probe, join PLAUD0001 from the shell, listen on 8081 in the guest, dial it from the host)
#   WINDOW_S (150)  FEATURE_FLAGS (default "-feature -WiFiPacketStream,WifiConfigurable"; set it empty for none)
#   FORCE_MEMORY (0)  MIN_AVAIL_MB (3072)  MAX_SWAP_MB (4096)  HEAVY_JOBS (regex of jobs that must not run)
#   KILL_NETSIMD (0)  HOSTAPD_TEMPLATE_SHA256 (sha256 of the pristine emulator/lib/hostapd.conf, 37.1.11)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK=${ANDROID_SDK:?set ANDROID_SDK to the Android SDK folder (emulator/, platform-tools/)}
ADB="$SDK/platform-tools/adb"
adbe() { "$ADB" -s emulator-5554 "$@"; }
usage() { sed -n '/^# Usage:/,/^set -u/p' "${BASH_SOURCE[0]}" | sed '$d'; }
[ $# -ge 1 ] || { usage; exit 2; }
OUT=$1; shift; MODES=${*:-probe}

AVD=${AVD:-mivi_test_34}
APK=${APK:-$ROOT/r7/android-app/app/build/outputs/apk/debug/app-debug.apk}
PY=${PY:-$ROOT/.venv/bin/python}
CHECK="$ROOT/r7/r7-s16-evidence/check_wifi_pcap.py"
PEN="$ROOT/r7/wifi_capture_device.py"
HP=${HOST_PORT:-18081}; GUEST=10.0.2.16; GW=10.0.2.2; SPORT=8081
SSID=PLAUD0001; PASS=10000001
AP_BSSID=00:13:10:95:fe:0b; TEMPLATE_BSSID=00:13:10:85:fe:01
FWD=${FWD:-1}; HOSTFWD_VIA=${HOSTFWD_VIA:-options}; MONITOR=${MONITOR:-1}; MONPORT=${MONITOR_PORT:-45454}
REQUIRE_RESTRICT=${REQUIRE_RESTRICT:-1}; PROBE_E2E=${PROBE_E2E:-1}; WINDOW_S=${WINDOW_S:-150}
FEATURE_FLAGS=${FEATURE_FLAGS--feature -WiFiPacketStream,WifiConfigurable}   # no colon: empty means none
FORCE_MEMORY=${FORCE_MEMORY:-0}; MIN_AVAIL_MB=${MIN_AVAIL_MB:-3072}; MAX_SWAP_MB=${MAX_SWAP_MB:-4096}
HEAVY_JOBS=${HEAVY_JOBS:-faster_whisper|sherpa-onnx|sherpa_onnx|pipeline batch|scripts/run-v5.sh|research/nvidia/|colima|limactl}
KILL_NETSIMD=${KILL_NETSIMD:-0}
HOSTAPD_TEMPLATE="$SDK/emulator/lib/hostapd.conf"
HOSTAPD_TEMPLATE_SHA256=${HOSTAPD_TEMPLATE_SHA256:-ec59f9e548f8abac42bfa45f466646e57d4e61231a6f2256d3a9952086a9ffd6}
export ANDROID_SDK_ROOT=$SDK ANDROID_HOME=$SDK PYTHONDONTWRITEBYTECODE=1

refuse() { echo "REFUSED: $*"; exit 1; }
sha_of() { shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'; }
listener_of() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 {print $1 " pid=" $2; exit}'; }
netsimd_pids() { pgrep -x netsimd 2>/dev/null | tr '\n' ' '; }   # the process NAME, not any argv mention

# ---------------------------------------------------------------- argument checks (nothing started yet)
for m in $MODES; do
  case $m in probe|transfer|control) ;; *) echo "unknown mode: $m (probe, transfer, control)"; exit 2 ;; esac
done
case " $MODES " in *" transfer "*) [ "$FWD" = 1 ] || { echo "transfer needs FWD=1"; exit 2; } ;; esac
case " $MODES " in *" control "*) [ "$FWD" = 0 ] || { echo "control needs FWD=0 (its own boot)"; exit 2; } ;; esac
case $HOSTFWD_VIA in options|monitor) ;; *) echo "HOSTFWD_VIA must be options or monitor"; exit 2 ;; esac
[ "$HOSTFWD_VIA" = monitor ] && [ "$MONITOR" != 1 ] && { echo "HOSTFWD_VIA=monitor needs MONITOR=1"; exit 2; }
NEED_APP=0; case " $MODES " in *" transfer "*|*" control "*) NEED_APP=1 ;; esac
for f in "$PY" "$CHECK" "$PEN" "$SDK/emulator/emulator" "$ADB"; do [ -e "$f" ] || { echo "missing: $f"; exit 2; }; done
[ "$NEED_APP" = 0 ] || [ -f "$APK" ] || { echo "missing APK $APK (build r7/android-app: ./gradlew :app:assembleDebug)"; exit 2; }

mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd) || exit 2
[ -e "$OUT/avd.log" ] && { echo "$OUT already holds a run (avd.log); use a new directory"; exit 2; }

# the emulator command, built before anything is started (a bad FEATURE_FLAGS value cannot abort later)
FEATURE_ARR=(); read -r -a FEATURE_ARR <<< "$FEATURE_FLAGS"
WIFI_OPTS="dhcpstart=$GUEST,restrict=on"
[ "$FWD" = 1 ] && [ "$HOSTFWD_VIA" = options ] && WIFI_OPTS="$WIFI_OPTS,hostfwd=tcp:127.0.0.1:$HP-$GUEST:$SPORT"
# -verbose (= -debug-init, cmdline-option.cpp:100-102) makes qemu print the argv it built, the only place
# the Wi-Fi netdev string can be read without the monitor
EMU_ARGS=(-avd "$AVD" -no-window -no-audio -no-snapshot -no-boot-anim -verbose
  ${FEATURE_ARR[@]+"${FEATURE_ARR[@]}"}
  -wifi-user-mode-options "$WIFI_OPTS" -dns-server 127.0.0.1 -http-proxy 127.0.0.1:9
  -tcpdump "$OUT/eth0-mynet.pcap" -netsim-args "--pcap")
QEMU_ARGS=(-object "filter-dump,id=wifidump,netdev=virtio-wifi,file=$OUT/wifi-virtio-8023.pcap")
[ "$MONITOR" = 1 ] && QEMU_ARGS+=(-monitor "tcp:127.0.0.1:$MONPORT,server,nowait")

exec > >(tee -a "$OUT/run.log") 2>&1
echo "R7-S16 run.sh $(date '+%Y-%m-%d %H:%M:%S %Z') modes=[$MODES] FWD=$FWD HOSTFWD_VIA=$HOSTFWD_VIA MONITOR=$MONITOR"

# ---------------------------------------------------------------- refusals (nothing started yet)
if "$ADB" devices | grep -q '^emulator-' || pgrep -f 'qemu-system-aarch64' > /dev/null; then
  refuse "an emulator is already running; stop it first"
fi
for port in 5554 5555; do   # console and adb ports of emulator-5554: another holder would move us to 5556
  [ -z "$(listener_of "$port")" ] || refuse "port $port is taken ($(listener_of "$port")); the emulator must be emulator-5554"
done
PRE_NETSIMD=$(netsimd_pids)
if [ -n "$PRE_NETSIMD" ] && [ "$KILL_NETSIMD" != 1 ]; then
  refuse "a netsimd is already running (pids $PRE_NETSIMD) with no emulator; stop it, or set KILL_NETSIMD=1 (netsimd reads its flags only when it starts)"
fi
memory_gate() {
  local swap_used_mb pagesize free inactive spec avail_mb heavy
  swap_used_mb=$(sysctl -n vm.swapusage | awk '{for (i = 1; i <= NF; i++) if ($i == "used") { v = $(i + 2);
      u = substr(v, length(v)); n = substr(v, 1, length(v) - 1) + 0; if (u == "G") n *= 1024; else if (u == "K") n /= 1024;
      printf "%d", n } }')
  pagesize=$(vm_stat | awk '/page size of/ {print $8}')
  read -r free inactive spec <<< "$(vm_stat | awk -F: '/^Pages free/ {gsub(/[ .]/, "", $2); f = $2}
      /^Pages inactive/ {gsub(/[ .]/, "", $2); i = $2} /^Pages speculative/ {gsub(/[ .]/, "", $2); s = $2}
      END {print f + 0, i + 0, s + 0}')"
  avail_mb=$(( (free + inactive + spec) * ${pagesize:-16384} / 1048576 ))
  heavy=$(pgrep -fl "$HEAVY_JOBS" 2>/dev/null | head -5)
  { echo "time=$(date +%T) swap_used_mb=${swap_used_mb:-?} max_swap_mb=$MAX_SWAP_MB"
    echo "available_mb(free+inactive+speculative)=$avail_mb min_avail_mb=$MIN_AVAIL_MB"
    echo "heavy_jobs=[${heavy//$'\n'/; }]"; sysctl -n vm.swapusage; } | tee "$OUT/memory-gate.txt"
  local why=""
  [ "${swap_used_mb:-0}" -gt "$MAX_SWAP_MB" ] && why="$why swap used ${swap_used_mb} MB > $MAX_SWAP_MB MB;"
  [ "$avail_mb" -lt "$MIN_AVAIL_MB" ] && why="$why available ${avail_mb} MB < $MIN_AVAIL_MB MB;"
  [ -n "$heavy" ] && why="$why heavy jobs running;"
  if [ -n "$why" ]; then
    if [ "$FORCE_MEMORY" = 1 ]; then echo "MEMORY GATE OVERRIDDEN (FORCE_MEMORY=1):$why" | tee -a "$OUT/memory-gate.txt"
    else refuse "memory gate:$why (FORCE_MEMORY=1 overrides)"; fi
  fi
}
memory_gate
[ -z "$(listener_of "$HP")" ] || refuse "host port $HP already has a listener: $(listener_of "$HP")"
[ "$MONITOR" != 1 ] || [ -z "$(listener_of "$MONPORT")" ] || refuse "monitor port $MONPORT is taken: $(listener_of "$MONPORT")"
"$ADB" forward --list > "$OUT/adb-forward-before.txt" 2>&1
grep -q "tcp:$HP" "$OUT/adb-forward-before.txt" && refuse "an adb forward uses tcp:$HP; remove it (it would carry the pen instead)"
[ -f "$HOSTAPD_TEMPLATE" ] || refuse "missing $HOSTAPD_TEMPLATE"
HOSTAPD_SHA_START=$(sha_of "$HOSTAPD_TEMPLATE")
[ "$HOSTAPD_SHA_START" = "$HOSTAPD_TEMPLATE_SHA256" ] || refuse "$HOSTAPD_TEMPLATE has sha256 $HOSTAPD_SHA_START, not the pristine $HOSTAPD_TEMPLATE_SHA256: an earlier run may have been killed before restoring it. Restore it from that run's hostapd.conf.orig, or set HOSTAPD_TEMPLATE_SHA256 once you know this content is the original"
cp -p "$HOSTAPD_TEMPLATE" "$OUT/hostapd.conf.orig" || refuse "cannot copy $HOSTAPD_TEMPLATE"

# ---------------------------------------------------------------- start (from here on the EXIT trap cleans up)
PP=""; BG_PIDS=""; PROBE_NET_ID=""; EMU_PID=""; QPID=""; EMU_STOPPED=0; OUR_NETSIMD=""
find_qpid() {  # this AVD's qemu process: its NAME must be qemu-system-*, not just its argv
  local p
  for p in $(pgrep -f "qemu-system-aarch64.*-avd $AVD" 2>/dev/null); do
    case "$(ps -o comm= -p "$p" 2>/dev/null)" in *qemu-system*) echo "$p"; return 0 ;; esac
  done
}
emu_alive() { local p; for p in $(printf '%s\n' $EMU_PID $QPID | sort -u); do kill -0 "$p" 2>/dev/null && echo "$p"; done; }
stop_emulator() {  # `emu kill`, then TERM, then KILL, to this run's own emulator PIDs only
  local i p
  [ -n "$QPID" ] || QPID=$(find_qpid)
  if [ -z "$(emu_alive)" ]; then EMU_STOPPED=1; return 0; fi
  adbe emu kill > /dev/null 2>&1
  for i in $(seq 1 30); do [ -z "$(emu_alive)" ] && break; sleep 1; done
  if [ -n "$(emu_alive)" ]; then
    echo "emulator still running 30 s after 'emu kill': TERM to $(emu_alive | tr '\n' ' ')"
    for p in $(emu_alive); do kill "$p" 2>/dev/null; done
    for i in $(seq 1 15); do [ -z "$(emu_alive)" ] && break; sleep 1; done
  fi
  if [ -n "$(emu_alive)" ]; then
    echo "emulator still running after TERM: KILL to $(emu_alive | tr '\n' ' ')"
    for p in $(emu_alive); do kill -9 "$p" 2>/dev/null; done; sleep 2
  fi
  if [ -z "$(emu_alive)" ]; then EMU_STOPPED=1; return 0; fi
  echo "WARNING: emulator pids $(emu_alive | tr '\n' ' ') are still running"; return 1
}
stop_our_netsimd() {  # netsimd processes that appeared after this run started (the one serving our emulator)
  local p q pre
  for p in $(netsimd_pids); do
    pre=0; for q in $PRE_NETSIMD; do [ "$p" = "$q" ] && pre=1; done
    [ "$pre" = 0 ] && kill "$p" 2>/dev/null && echo "stopped netsimd pid $p"
  done
}
restore_hostapd() {  # put the shared hostapd template back (`wifi add` rewrites it)
  local now; now=$(sha_of "$HOSTAPD_TEMPLATE")
  { echo "$(date +%T) template=$HOSTAPD_TEMPLATE"; echo "sha256_at_start=$HOSTAPD_SHA_START"; echo "sha256_now=$now"
    if [ "$now" = "$HOSTAPD_SHA_START" ]; then echo "unchanged"
    else
      cp -p "$HOSTAPD_TEMPLATE" "$OUT/hostapd.conf.as-left-by-emulator" 2>/dev/null
      cp -p "$OUT/hostapd.conf.orig" "$HOSTAPD_TEMPLATE" && echo "restored from hostapd.conf.orig"
      echo "sha256_after_restore=$(sha_of "$HOSTAPD_TEMPLATE")"
    fi; } >> "$OUT/hostapd-template.txt" 2>&1
  [ "$(sha_of "$HOSTAPD_TEMPLATE")" = "$HOSTAPD_SHA_START" ] || echo "WARNING: $HOSTAPD_TEMPLATE is NOT restored (see hostapd-template.txt)"
}
cleanup() {
  [ -n "$PP" ] && kill "$PP" 2>/dev/null
  for p in $BG_PIDS; do kill "$p" 2>/dev/null; done
  if [ -n "$PROBE_NET_ID" ] && [ -n "$(emu_alive)" ]; then adbe shell cmd wifi forget-network "$PROBE_NET_ID" > /dev/null 2>&1; fi
  [ "$EMU_STOPPED" = 1 ] || stop_emulator
  restore_hostapd
  stop_our_netsimd
  "$ADB" forward --list > "$OUT/adb-forward-at-exit.txt" 2>&1
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ -n "$PRE_NETSIMD" ]; then   # KILL_NETSIMD=1: stop the stray netsimd, by PID and name only
  for p in $PRE_NETSIMD; do kill "$p" 2>/dev/null && echo "stopped pre-existing netsimd pid $p"; done; sleep 2
  PRE_NETSIMD=$(netsimd_pids)
  [ -z "$PRE_NETSIMD" ] || refuse "netsimd pids $PRE_NETSIMD did not stop"
fi
printf '%q ' "$SDK/emulator/emulator" "${EMU_ARGS[@]}" -qemu "${QEMU_ARGS[@]}" > "$OUT/emulator-command.txt"; echo >> "$OUT/emulator-command.txt"
echo "starting: $(cat "$OUT/emulator-command.txt")"
nohup "$SDK/emulator/emulator" "${EMU_ARGS[@]}" -qemu "${QEMU_ARGS[@]}" > "$OUT/avd.log" 2>&1 &
EMU_PID=$!

boot_failed() { echo "BOOT FAILED: $*"; tail -30 "$OUT/avd.log"
  echo "BOOT FAILED: $* (if the -qemu arguments are the cause, retry with MONITOR=0: the netdev is still read from avd.log)" > "$OUT/boot-failed.txt"
  exit 1; }
for i in $(seq 1 60); do
  "$ADB" devices | grep -q '^emulator-5554' && break
  kill -0 "$EMU_PID" 2>/dev/null || boot_failed "the emulator exited"
  sleep 2
done
"$ADB" devices | grep -q '^emulator-5554' || boot_failed "no emulator-5554 after 120 s"
booted=0; for i in $(seq 1 120); do
  [ "$(adbe shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { booted=1; break; }
  kill -0 "$EMU_PID" 2>/dev/null || boot_failed "the emulator exited during boot"
  sleep 5
done
echo "booted=$booted"; [ "$booted" = 1 ] || boot_failed "sys.boot_completed never became 1"

# ---------------------------------------------------------------- what actually started (E1, E2, E3)
QPID=$(find_qpid)
OUR_NETSIMD=$(netsimd_pids)
ps -ww -o args= -p "${QPID:-$EMU_PID}" > "$OUT/qemu-args.txt" 2>&1        # the REQUESTED launcher options only
grep -a -E 'QEMU options list|argv\[[0-9]+\] = |Concatenated QEMU options' "$OUT/avd.log" > "$OUT/qemu-core-args.txt"
NETDEV=$(grep -a -o 'user,id=virtio-wifi[^" ]*' "$OUT/avd.log" | head -1)   # the netdev qemu BUILT
HOSTFWD_IN_ARGS=no; case $NETDEV in *hostfwd=*) HOSTFWD_IN_ARGS=yes ;; esac
RESTRICT_ON=no; case $NETDEV in *restrict=on*) RESTRICT_ON=yes ;; esac
NETDEV_SOURCE=avd.log
[ -n "$NETDEV" ] || { HOSTFWD_IN_ARGS=unknown; RESTRICT_ON=unknown; NETDEV_SOURCE=none; }
for p in $OUR_NETSIMD; do ps -ww -o args= -p "$p"; done > "$OUT/netsimd-args.txt" 2>/dev/null
grep -a -i -E 'netsim wifi|WiFiPacketStream|WifiConfigurable|overridden|packet streamer|hostapd|virtio.wifi|user mode networking|filter-dump' \
  "$OUT/avd.log" > "$OUT/avd-wifi-lines.txt"
NETSIM_WIFI=no; grep -a -q 'Successfully initialized netsim WiFi' "$OUT/avd.log" && NETSIM_WIFI=yes
VALIDATOR_MSG=$(grep -a -i 'user mode networking option' "$OUT/avd.log" | head -3 | tr '\n' ' ')
INI="${TMPDIR:-/tmp}/netsim.ini"; WEBPORT=$(awk -F= '/^web.port/ {print $2}' "$INI" 2>/dev/null | tr -d ' \r')
if [ -n "$WEBPORT" ]; then curl -s -m 5 "http://127.0.0.1:$WEBPORT/v1/devices" > "$OUT/netsim-devices.json" 2>&1; fi
NETSIM_WIFI_CHIPS=$(grep -o '"WIFI"' "$OUT/netsim-devices.json" 2>/dev/null | wc -l | tr -d ' ')

monitor_cmd() {  # send one HMP command to the QEMU monitor; prints banner + reply
  "$PY" - "$MONPORT" "$1" <<'PYEOF'
import socket, sys, time
port, cmd = int(sys.argv[1]), sys.argv[2]
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    time.sleep(0.5)
    s.settimeout(1.0)
    data = b""
    try:
        data += s.recv(65536)
    except socket.timeout:
        pass
    s.sendall(cmd.encode() + b"\r\n")
    end = time.time() + 2.0
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    s.close()
    sys.stdout.write(data.decode("utf-8", "replace").replace("\r", ""))
except OSError as exc:
    print(f"MONITOR ERROR: {exc!r}")
PYEOF
}
if [ "$MONITOR" = 1 ]; then
  { echo "== info network"; monitor_cmd "info network"; echo; echo "== info usernet"; monitor_cmd "info usernet"; } \
    > "$OUT/qemu-monitor-info.txt" 2>&1
  # `info network` shows the netdev's live settings ("virtio-wifi: index=...,type=user,...,restrict=on")
  MON_LINE=$(grep -a -E 'virtio-wifi:.*type=user' "$OUT/qemu-monitor-info.txt" | head -1)
  if [ "$RESTRICT_ON" = unknown ] && [ -n "$MON_LINE" ]; then
    case $MON_LINE in *restrict=on*) RESTRICT_ON=yes ;; *) RESTRICT_ON=no ;; esac; NETDEV_SOURCE=monitor
  fi
fi

HOSTFWD_MODE=none
LISTENER=$(listener_of "$HP")
case $LISTENER in *"pid=$QPID"|*"pid=$EMU_PID") HOSTFWD_MODE=options ;; esac
if [ "$FWD" = 1 ] && [ "$HOSTFWD_MODE" = none ] && [ "$MONITOR" = 1 ]; then
  echo "host forward not active after boot (HOSTFWD_VIA=$HOSTFWD_VIA; validator: ${VALIDATOR_MSG:-no message}); adding it through the QEMU monitor"
  [ "$RESTRICT_ON" = yes ] || echo "NOTE: restrict=on is not in effect for this boot; the monitor cannot add it (a transfer needs REQUIRE_RESTRICT=0)"
  monitor_cmd "hostfwd_add virtio-wifi tcp:127.0.0.1:$HP-$GUEST:$SPORT" > "$OUT/qemu-monitor-hostfwd_add.txt" 2>&1
  sleep 1
  LISTENER=$(listener_of "$HP")
  case $LISTENER in *"pid=$QPID"|*"pid=$EMU_PID") HOSTFWD_MODE=monitor ;; esac
  { echo "== info usernet (after hostfwd_add)"; monitor_cmd "info usernet"; } >> "$OUT/qemu-monitor-info.txt" 2>&1
fi
lsof -nP -iTCP:"$HP" -sTCP:LISTEN > "$OUT/hostport-listener.txt" 2>&1
"$ADB" forward --list > "$OUT/adb-forward-after-boot.txt" 2>&1
HOSTFWD_ACTIVE=no; [ "$HOSTFWD_MODE" != none ] && HOSTFWD_ACTIVE=yes
{ echo "emulator_pid=$EMU_PID qemu_pid=${QPID:-?} netsimd_pids=${OUR_NETSIMD:-none}"
  echo "wifi_netdev=$NETDEV (source: $NETDEV_SOURCE)"; echo "hostfwd_in_wifi_netdev=$HOSTFWD_IN_ARGS"
  echo "restrict_on=$RESTRICT_ON"; echo "validator_messages=$VALIDATOR_MSG"; echo "hostfwd_mode=$HOSTFWD_MODE"
  echo "host_port_listener=${LISTENER:-none}"; echo "netsim_wifi_initialised=$NETSIM_WIFI"
  echo "netsim_wifi_chips=${NETSIM_WIFI_CHIPS:-unknown}"
  echo "adb_forwards_on_host_port=$(grep -c "tcp:$HP" "$OUT/adb-forward-after-boot.txt" | tr -d ' ')"
} | tee "$OUT/boot-state.txt"

adbe shell cmd wifi status > "$OUT/wifi-status-boot.txt" 2>&1   # before `wifi add` the AP is the template's

# ---------------------------------------------------------------- the pen's network (E4)
adbe emu wifi add "$SSID" "$PASS" > "$OUT/wifi-add.txt" 2>&1
WIFI_ADD=$(tr -d '\r' < "$OUT/wifi-add.txt" | grep -v '^$' | head -1)
echo "wifi add: ${WIFI_ADD:-<no reply>}"
if [ "$NEED_APP" = 1 ]; then adbe install -r "$APK" | tail -1; fi
adbe shell svc data disable; adbe shell svc wifi enable; adbe shell svc bluetooth enable; sleep 8
SCAN_BSSID=""
for i in 1 2 3 4 5 6; do
  adbe shell cmd wifi start-scan > /dev/null 2>&1; sleep 5
  adbe shell cmd wifi list-scan-results > "$OUT/wifi-scan.txt" 2>&1
  SCAN_BSSID=$(awk -v s="$SSID" '$0 ~ s {print $1; exit}' "$OUT/wifi-scan.txt")
  [ -n "$SCAN_BSSID" ] && break
done
echo "scan: ${SCAN_BSSID:-no} $SSID (written by wifi add: $AP_BSSID; template, like netsim: $TEMPLATE_BSSID)"
echo "internet check from the shell (expect failure):"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -1 | tee "$OUT/ping.txt"

prep() { adbe shell am force-stop com.plaud.template; adbe shell pm clear com.plaud.template > /dev/null
  for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION NEARBY_WIFI_DEVICES; do
    adbe shell pm grant com.plaud.template android.permission.$p 2>/dev/null; done; }
approve_dialog() {  # tap the positive button of Android's network-request dialog when it appears (as R7-S15)
  local log=$1 end=$((SECONDS + 60))
  while [ $SECONDS -lt $end ]; do
    adbe shell uiautomator dump /sdcard/ui.xml > /dev/null 2>&1
    local xml; xml=$(adbe shell cat /sdcard/ui.xml 2>/dev/null)
    if echo "$xml" | grep -q -i "$SSID"; then
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
proc_poll() {  # $1 file, $2 seconds: the guest's port-8081 socket rows, once a second
  local end=$((SECONDS + $2))
  while [ $SECONDS -lt $end ]; do
    { echo "# $(date +%T)"; adbe shell cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -i ':1F91 '; } >> "$1"
    sleep 1
  done
}
guest_listeners() {  # LISTEN rows (state 0A) whose LOCAL port is the SDK's (0x1F91), in the guest
  adbe shell cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | tr -d '\r' | awk '$4 == "0A" && substr($2, length($2) - 4) == ":1F91"'
}
json_field() { "$PY" -c 'import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d.get(k, {}) if isinstance(d, dict) else {}
print(d if d != {} else "?")' "$1" "$2" 2>/dev/null || echo "?"; }

# ---------------------------------------------------------------- probe: the six unknowns
run_probe() {
  local P="$OUT/probe"; mkdir -p "$P"
  echo "== probe"
  ls -l "$OUT/wifi-virtio-8023.pcap" "$OUT/eth0-mynet.pcap" > "$P/pcaps-after-boot.txt" 2>&1
  "$PY" "$CHECK" --wifi-pcap "$OUT/wifi-virtio-8023.pcap" --summary --report "$P/wifi-pcap-summary-boot.json" \
    > "$P/wifi-pcap-summary-boot.txt" 2>&1
  local e2e="skipped" host_got="" guest_got="" peer="?" left
  if [ "$PROBE_E2E" != 1 ]; then e2e="skipped (PROBE_E2E=$PROBE_E2E)"
  elif [ "$HOSTFWD_ACTIVE" != yes ]; then e2e="skipped (no host forward)"
  elif [ "$RESTRICT_ON" != yes ] && [ "$REQUIRE_RESTRICT" = 1 ]; then e2e="skipped (restrict=on not in the Wi-Fi netdev; REQUIRE_RESTRICT=0 overrides)"
  else
    adbe shell cmd wifi connect-network "$SSID" wpa2 "$PASS" > "$P/connect-network.txt" 2>&1
    PROBE_NET_ID=$(adbe shell cmd wifi list-networks 2>/dev/null | tr -d '\r' | awk -v s="$SSID" '$0 ~ s && $1 ~ /^[0-9]+$/ {print $1; exit}')
    for i in $(seq 1 30); do adbe shell cmd wifi status 2>/dev/null | grep -q "connected to \"$SSID\"" && break; sleep 1; done
    adbe shell cmd wifi status > "$P/wifi-status-joined.txt" 2>&1
    adbe shell ip addr show wlan0 > "$P/ip-addr-wlan0.txt" 2>&1
    # `exec` makes $! the adb client itself (a function would leave it running after `kill`)
    ( exec "$ADB" -s emulator-5554 shell "(echo PROBE-FROM-GUEST; sleep 10) | toybox nc -l -p $SPORT" \
        > "$P/guest-nc-output.txt" 2>&1 ) &
    local gnc=$!; BG_PIDS="$BG_PIDS $gnc"
    for i in $(seq 1 10); do [ -n "$(guest_listeners)" ] && break; sleep 1; done   # nc really listening
    { echo "# $(date +%T) listening"; adbe shell cat /proc/net/tcp /proc/net/tcp6 | grep -i ':1F91 '; } > "$P/proc-net-tcp.txt"
    "$PY" - "$HP" > "$P/host-dial.json" 2>&1 <<'PYEOF' &
import json, socket, sys, time
port = int(sys.argv[1])
out = {"target": f"127.0.0.1:{port}"}
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    out["local"] = "%s:%d" % s.getsockname()[:2]
    s.sendall(b"PROBE-FROM-HOST\n")
    s.settimeout(1.0)
    data, end = b"", time.time() + 6
    while time.time() < end and b"\n" not in data:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    out["received"] = data.decode("utf-8", "replace")
    time.sleep(4)              # hold the connection while the guest's socket table is read
    s.close()
except OSError as exc:
    out["error"] = repr(exc)
print(json.dumps(out))
PYEOF
    local hd=$!; sleep 3
    { echo "# $(date +%T) connected"; adbe shell cat /proc/net/tcp /proc/net/tcp6 | grep -i ':1F91 '; } >> "$P/proc-net-tcp.txt"
    wait "$hd"
    for i in $(seq 1 15); do kill -0 "$gnc" 2>/dev/null || break; sleep 1; done
    # stop the guest's nc whether or not a connection reached it; the bracket keeps the pattern from
    # matching the `sh -c` that runs pkill
    adbe shell "pkill -f 'nc -l -p [${SPORT:0:1}]${SPORT:1}'" > /dev/null 2>&1; kill "$gnc" 2>/dev/null
    sleep 1; left=$(guest_listeners)
    echo "${left:-no LISTEN row on :1F91}" > "$P/guest-listeners-after-probe.txt"
    { date +%T
      echo "ping 8.8.8.8:"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -2
      echo "ping example.com (needs DNS):"; adbe shell ping -c 1 -W 3 example.com 2>&1 | tail -2
      echo "tcp 1.1.1.1:443:"; adbe shell "toybox nc -w 3 1.1.1.1 443 < /dev/null && echo TCP-OPEN || echo TCP-FAILED" 2>&1 | tail -1
      echo "tcp $GW:$HP (the host through the gateway):"; adbe shell "toybox nc -w 3 $GW $HP < /dev/null && echo TCP-OPEN || echo TCP-FAILED" 2>&1 | tail -1
    } > "$P/egress-joined.txt" 2>&1
    adbe shell cmd wifi forget-network "$PROBE_NET_ID" > "$P/forget-network.txt" 2>&1 && PROBE_NET_ID=""
    sleep 3; adbe shell cmd wifi status > "$P/wifi-status-after-forget.txt" 2>&1
    host_got=$(json_field "$P/host-dial.json" received)
    guest_got=$(tr -d '\r' < "$P/guest-nc-output.txt" | head -1)
    "$PY" "$CHECK" --wifi-pcap "$OUT/wifi-virtio-8023.pcap" --proc-net-tcp "$P/proc-net-tcp.txt" \
      --report "$P/peer-check.json" > /dev/null 2>&1
    peer=$(json_field "$P/peer-check.json" checks.proc_net_tcp_peer.status)
    e2e="done"
  fi
  "$PY" "$CHECK" --wifi-pcap "$OUT/wifi-virtio-8023.pcap" --eth-pcap "$OUT/eth0-mynet.pcap" --summary \
    --report "$P/wifi-pcap-summary-end.json" > "$P/wifi-pcap-summary-end.txt" 2>&1
  local pkts; pkts=$(json_field "$P/wifi-pcap-summary-end.json" wifi_pcap.packets)
  { echo "R7-S16 probe results ($(date '+%Y-%m-%d %H:%M:%S')); evidence files are in this directory and in .."
    echo "U1 option validator (allowlist has dhcpstart, restrict, hostfwd): wifi netdev=[$NETDEV] (from $NETDEV_SOURCE)" \
         "hostfwd=$HOSTFWD_IN_ARGS restrict_on=$RESTRICT_ON messages=[${VALIDATOR_MSG:-none}]" \
         "host_forward=$HOSTFWD_MODE (listener: ${LISTENER:-none})"
    echo "U2 -feature syntax ($FEATURE_FLAGS): netsim_wifi_initialised=$NETSIM_WIFI (want no)," \
         "netsim_wifi_chips=${NETSIM_WIFI_CHIPS:-unknown} (want 0), wifi_add_reply=[${WIFI_ADD:-none}] (want OK)"
    echo "U3 wifi add on API 34: reply=[${WIFI_ADD:-none}] scan_bssid=[${SCAN_BSSID:-none}] (want $AP_BSSID;" \
         "$TEMPLATE_BSSID would mean the template AP) ../wifi-scan.txt, ../hostapd-template.txt after exit"
    echo "U4 filter-dump on virtio-wifi: packets=$pkts (want > 0) ../wifi-virtio-8023.pcap, wifi-pcap-summary-end.json"
    echo "U5 host forward with -http-proxy set: e2e=$e2e host_received=[$host_got] (want PROBE-FROM-GUEST)" \
         "guest_received=[$guest_got] (want PROBE-FROM-HOST); guest listener after the probe: guest-listeners-after-probe.txt"
    echo "U6 libslirp: guest sees the dial from $GW: proc_net_tcp_peer=$peer (want pass);" \
         "restricted egress: egress-joined.txt (want every probe to fail), egress (TCP, UDP, ICMP) in wifi-pcap-summary-end.json"
  } | tee "$P/probe-results.txt"
}

# ---------------------------------------------------------------- transfer / control: the real SDK
CHECKS_TODO=""
run_sdk() {
  local mode=$1 left
  echo "== $mode"
  if [ "$mode" = transfer ]; then
    [ "$HOSTFWD_ACTIVE" = yes ] || { echo "SKIPPED $mode: no host forward (see boot-state.txt)"; return 1; }
    [ "$RESTRICT_ON" = yes ] || [ "$REQUIRE_RESTRICT" = 0 ] || { echo "SKIPPED $mode: restrict=on not in effect (restrict_on=$RESTRICT_ON; REQUIRE_RESTRICT=0 overrides)"; return 1; }
  else
    [ "$HOSTFWD_ACTIVE" = no ] || { echo "SKIPPED $mode: a host forward is active, not a control"; return 1; }
    [ -z "$(listener_of "$HP")" ] || { echo "SKIPPED $mode: something listens on $HP: $(listener_of "$HP")"; return 1; }
  fi
  [ -n "$SCAN_BSSID" ] || { echo "SKIPPED $mode: $SSID not visible to the phone"; return 1; }
  left=$(guest_listeners)
  [ -z "$left" ] || { echo "SKIPPED $mode: something already listens on $SPORT in the guest: $left"; return 1; }
  prep
  WIFICAP_CAPTURE=$OUT/wifi-$mode-capture.json WIFICAP_HOST_PORT=$HP nohup "$PY" "$PEN" > "$OUT/wifi-$mode-peripheral.log" 2>&1 &
  PP=$!; for i in $(seq 1 30); do grep -q WIFICAP_PERIPHERAL_READY "$OUT/wifi-$mode-peripheral.log" && break; sleep 1; done
  grep -q WIFICAP_PERIPHERAL_READY "$OUT/wifi-$mode-peripheral.log" || { echo "pen did not start"; kill "$PP"; PP=""; return 1; }
  adbe shell cmd wifi status > "$OUT/wifi-$mode-status-before.txt" 2>&1
  "$ADB" forward --list > "$OUT/wifi-$mode-adb-forward.txt" 2>&1
  adbe logcat -c
  adbe shell am start -n com.plaud.template/.debug.WifiCaptureActivity --es id "SYNTH-HIST-0001" --es mode transfer > /dev/null
  approve_dialog "$OUT/wifi-$mode-dialog.log" & BG_PIDS="$BG_PIDS $!"
  proc_poll "$OUT/wifi-$mode-proc-net-tcp.txt" "$WINDOW_S" & BG_PIDS="$BG_PIDS $!"
  ( sleep 25   # while the phone should be on PLAUD0001 (the app's network; the shell has no route to it)
    { date +%T; adbe shell cmd wifi status 2>&1 | head -5; adbe shell ip addr show wlan0 2>&1
      echo "ping IP:"; adbe shell ping -c 1 -W 3 8.8.8.8 2>&1 | tail -2
      echo "tcp 1.1.1.1:443:"; adbe shell "toybox nc -w 3 1.1.1.1 443 < /dev/null && echo TCP-OPEN || echo TCP-FAILED" 2>&1 | tail -1
    } > "$OUT/wifi-$mode-egress.txt" 2>&1 ) & BG_PIDS="$BG_PIDS $!"
  sleep "$WINDOW_S"
  adbe logcat -d > "$OUT/wifi-$mode-logcat.log"
  adbe shell cmd wifi status > "$OUT/wifi-$mode-status-after.txt" 2>&1
  kill "$PP" 2>/dev/null; PP=""; sleep 2
  "$ADB" forward --list >> "$OUT/wifi-$mode-adb-forward.txt" 2>&1
  echo "== Wi-Fi mode=$mode"
  grep -a -E "WIFICAP_(BIND|BLE_WIFI_OPEN|CB_|END|EXPORT|DONE|ERROR)" "$OUT/wifi-$mode-logcat.log" | sed 's/^.*WIFICAP_/WIFICAP_/' | head -24
  grep -a -E "Starting WebSocket server|Device connected:|Handshake timeout" "$OUT/wifi-$mode-logcat.log" | head -5
  echo "gen-key lines: $(grep -a -c -i 'gen-key' "$OUT/wifi-$mode-logcat.log")"
  CHECKS_TODO="$CHECKS_TODO $mode"
  return 0
}

SKIPPED=0
for mode in $MODES; do
  case $mode in probe) run_probe ;; transfer|control) run_sdk "$mode" || SKIPPED=1 ;; esac
done

# ---------------------------------------------------------------- stop the emulator, restore, then judge
for p in $BG_PIDS; do kill "$p" 2>/dev/null; done; BG_PIDS=""
[ -n "$PROBE_NET_ID" ] && adbe shell cmd wifi forget-network "$PROBE_NET_ID" > /dev/null 2>&1 && PROBE_NET_ID=""
stop_emulator || echo "the pcaps may still be growing: the checks below may see a truncated last record"
restore_hostapd
stop_our_netsimd
RC=$SKIPPED
[ "$SKIPPED" = 1 ] && echo "a requested SDK mode was skipped (see run.log)" | tee -a "$OUT/summary.txt"
for mode in $CHECKS_TODO; do
  args=(--wifi-pcap "$OUT/wifi-virtio-8023.pcap" --eth-pcap "$OUT/eth0-mynet.pcap"
        --capture-json "$OUT/wifi-$mode-capture.json" --proc-net-tcp "$OUT/wifi-$mode-proc-net-tcp.txt"
        --logcat "$OUT/wifi-$mode-logcat.log" --wifi-status "$OUT/wifi-$mode-egress.txt" --ssid "$SSID"
        --report "$OUT/wifi-$mode-pcap-check.json")
  [ "$mode" = control ] && args+=(--expect-none)
  [ "$RESTRICT_ON" = yes ] && args+=(--expect-no-egress)
  "$PY" "$CHECK" "${args[@]}"; rc=$?
  echo "check_wifi_pcap $mode: exit $rc (0 pass, 1 fail, 2 input/internal error, 3 inconclusive)" | tee -a "$OUT/summary.txt"
  [ "$rc" -gt "$RC" ] && RC=$rc
done
cat "$OUT/boot-state.txt" >> "$OUT/summary.txt"
echo "ALL DONE (exit $RC)"   # the EXIT trap records adb forward --list
exit "$RC"
