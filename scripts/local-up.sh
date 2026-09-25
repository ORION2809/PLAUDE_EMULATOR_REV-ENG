#!/usr/bin/env bash
# The docker-compose.yml topology without Docker: the venv runs emulator/serve.py and
# `python -m mockcloud` in the background, waits for the same two healthchecks compose
# uses, runs docker/job.sh inline, prints the measured bring-up time and tears down.
#
#   ./scripts/local-up.sh
#   EMULATOR_HCI_PORT=19000 EMULATOR_HEALTH_PORT=19001 MOCKCLOUD_PORT=18787 ./scripts/local-up.sh
#   LOCAL_UP_OUT=/tmp/x ./scripts/local-up.sh        # logs + job output elsewhere (default build/compose/local)
#   LOCAL_UP_TARGET_S=60                              # the target the "live" time is compared with
#
# Prints:  LOCAL_UP_LIVE_S=<s>   both services healthy (the V6 clock)
#          LOCAL_UP_TOTAL_S=<s>  including the job
# Exit codes: 0 pass; 1 a service or the job failed, or the live time exceeded the target;
# 2 the venv is missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "local-up: missing venv python at $PY (see requirements/all.txt)" >&2; exit 2; }

EMULATOR_HCI_PORT="${EMULATOR_HCI_PORT:-9000}"
EMULATOR_HEALTH_PORT="${EMULATOR_HEALTH_PORT:-9001}"
MOCKCLOUD_PORT="${MOCKCLOUD_PORT:-8787}"
OUT="${LOCAL_UP_OUT:-$ROOT/build/compose/local}"
TARGET_S="${LOCAL_UP_TARGET_S:-60}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-30}"
LOGS="$OUT/logs"
mkdir -p "$LOGS"

export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT:$ROOT/emulator${PYTHONPATH:+:$PYTHONPATH}"

now_s() { "$PY" -c 'import time; print(f"{time.time():.3f}")'; }
diff_s() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }

PIDS=()
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
  done
  echo "local-up: torn down (logs in $LOGS)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

T0="$(now_s)"
echo "local-up: starting emulator (hci tcp-server:127.0.0.1:$EMULATOR_HCI_PORT, health 127.0.0.1:$EMULATOR_HEALTH_PORT) and mockcloud (127.0.0.1:$MOCKCLOUD_PORT)"

PLAUD_HCI_TRANSPORT="tcp-server:127.0.0.1:$EMULATOR_HCI_PORT" \
PLAUD_HCI_MODE=bridge \
PLAUD_HEALTH_HOST=127.0.0.1 \
PLAUD_HEALTH_PORT="$EMULATOR_HEALTH_PORT" \
  "$PY" "$ROOT/emulator/serve.py" >"$LOGS/emulator.log" 2>&1 &
PIDS+=($!)

"$PY" -m mockcloud --host 127.0.0.1 --port "$MOCKCLOUD_PORT" --local-file-root "$ROOT/build" \
  >"$LOGS/mockcloud.log" 2>&1 &
PIDS+=($!)

# The same predicates as the compose healthchecks.
"$PY" "$ROOT/docker/probe.py" emulator "127.0.0.1:$EMULATOR_HEALTH_PORT" --timeout "$PROBE_TIMEOUT"
"$PY" "$ROOT/docker/probe.py" mockcloud "http://127.0.0.1:$MOCKCLOUD_PORT" --timeout "$PROBE_TIMEOUT"
T_LIVE="$(now_s)"
LIVE_S="$(diff_s "$T0" "$T_LIVE")"
echo "LOCAL_UP_LIVE_S=$LIVE_S"

PYTHON="$PY" HARNESS_ROOT="$ROOT" JOB_OUT="$OUT/job" \
MOCKCLOUD_URL="http://127.0.0.1:$MOCKCLOUD_PORT" \
EMULATOR_HEALTH="127.0.0.1:$EMULATOR_HEALTH_PORT" \
EMULATOR_HCI="tcp-client:127.0.0.1:$EMULATOR_HCI_PORT" \
PROBE_TIMEOUT="$PROBE_TIMEOUT" \
  bash "$ROOT/docker/job.sh" 2>&1 | tee "$LOGS/job.log"
JOB_RC="${PIPESTATUS[0]}"

T_END="$(now_s)"
TOTAL_S="$(diff_s "$T0" "$T_END")"
echo "LOCAL_UP_TOTAL_S=$TOTAL_S"

if [ "$JOB_RC" -ne 0 ]; then
  echo "local-up: FAIL job exited $JOB_RC" >&2
  exit 1
fi
if ! awk -v e="$LIVE_S" -v t="$TARGET_S" 'BEGIN { exit !(e <= t) }'; then
  echo "local-up: FAIL live in ${LIVE_S}s, exceeds the ${TARGET_S}s target" >&2
  exit 1
fi
echo "local-up: PASS live in ${LIVE_S}s (target ${TARGET_S}s), total ${TOTAL_S}s including the job"
