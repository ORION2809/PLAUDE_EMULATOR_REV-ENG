#!/bin/zsh
ROOT=/Users/mivi/Desktop/plaud-harness
ADB=/Users/mivi/mivi-toolchain/android-sdk/platform-tools/adb
S=/private/tmp/claude-502/-Users-mivi-Desktop-plaud-harness/d6d49373-6f77-4f77-b814-2e74aefe7ea2/scratchpad
run() {  # name mode extra-am-args... (env already exported by caller)
  name=$1; mode=$2; shift 2
  $ADB -s emulator-5554 shell am force-stop com.plaud.template
  # full app-data reset so the SDK's own download cache cannot leak between runs
  $ADB -s emulator-5554 shell pm clear com.plaud.template > /dev/null
  for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION; do $ADB -s emulator-5554 shell pm grant com.plaud.template android.permission.$p; done
  pkill -f pull_capture_peripheral.py; sleep 1
  K3CAP_BUMBLE_LOG=INFO PYTHONDONTWRITEBYTECODE=1 PULLCAP_CAPTURE=$S/pull-capture-$name.json nohup $ROOT/.venv/bin/python $ROOT/r7/pull_capture_peripheral.py > $S/pull-peripheral-$name.log 2>&1 &
  sleep 7
  $ADB -s emulator-5554 logcat -c
  $ADB -s emulator-5554 shell am start -n com.plaud.template/.debug.PullCaptureActivity --es id "SYNTH-HIST-0001" --es mode $mode "$@" > /dev/null
  sleep 65
  $ADB -s emulator-5554 logcat -d > $S/pull-logcat-$name.log
  echo "##### $name (mode=$mode env: EMPTY_CODE=$PULLCAP_EMPTY_CODE POS=$PULLCAP_EMPTY_POS DROP=$PULLCAP_DROP_OFFSET TAIL=$PULLCAP_TAIL_CRC) #####"
  grep -E "PULLCAP" $S/pull-logcat-$name.log | sed 's/^.*PULLCAP_/PULLCAP_/' | grep -v "PULLCAP_DATA \|PULLCAP_STAGE\|PULLCAP_EXPORT_PROGRESS\|PULLCAP_SCAN\|PULLCAP_CONNECT\|PULLCAP_GETFILELIST\|PULLCAP_FILELIST\|PULLCAP_START"
  echo "--- sdk ---"
  grep -E "callBackRequest:\[28|removeResponseBean:\[28|errorCode|EMPTY_PACKAGE|start resend|isPacketLoss|lastPosition|TimeoutSync|下载完成|exportAudio" $S/pull-logcat-$name.log | grep -v PULLCAP | cut -c1-190
  echo "--- peripheral ---"
  grep -E "PULLCAP_(SYNC_START|SERVE|STOP|STREAM)" $S/pull-peripheral-$name.log | sed 's/^.*PULLCAP_/PULLCAP_/'
  $ADB -s emulator-5554 shell run-as com.plaud.template ls -la files/pullcap-export/ 2>/dev/null | grep opus
  $ADB -s emulator-5554 shell run-as com.plaud.template cat files/pullcap-export/1700000000.opus > $S/pullcap-export-$name.opus 2>/dev/null
  $ADB -s emulator-5554 shell run-as com.plaud.template cat files/pullcap-raw-1700000000.bin > $S/pullcap-raw-$name.bin 2>/dev/null
  echo "--- sdk cache after run ---"
  $ADB -s emulator-5554 shell run-as com.plaud.template find . -name "*1700000000*" 2>/dev/null
}
unset PULLCAP_EMPTY_CODE PULLCAP_EMPTY_POS PULLCAP_DROP_OFFSET PULLCAP_TAIL_CRC PULLCAP_NO_EMPTY
export PULLCAP_ABORT_ON_RESTART=1
run run11-shipped-default-export export
run run12-shipped-default-raw raw --ez stop true
export PULLCAP_DROP_OFFSET=3200
run run13-shipped-default-gap-export export
unset PULLCAP_DROP_OFFSET
echo "##### ALL RUNS DONE #####"
