#!/usr/bin/env bash
# The docker-compose.yml topology without Docker: the venv runs emulator/serve.py and
# `python -m mockcloud` in the background, waits for the same two healthchecks compose
# uses, runs docker/job.sh against them, prints the measured bring-up time and tears down.
#
#   ./scripts/local-up.sh
#   EMULATOR_HCI_PORT=19000 EMULATOR_HEALTH_PORT=19001 MOCKCLOUD_PORT=18787 ./scripts/local-up.sh
#   LOCAL_UP_OUT=/tmp/x ./scripts/local-up.sh        # logs + job output elsewhere (default build/compose/local)
#   LOCAL_UP_TARGET_S=60                              # the target the "live" time is compared with
#   MOCKCLOUD_CHUNK_SIZE=20000                        # the mock's multipart ChunkSize (as in docker-compose.yml)
#   JOB_* / PROBE_TIMEOUT                             # passed through to docker/job.sh
#
# Prints:  LOCAL_UP_LIVE_S=<s>   both services healthy (the V6 clock)
#          LOCAL_UP_TOTAL_S=<s>  including the job (also on a job failure)
# Exit codes: 0 pass; 1 a port was already taken, a service failed or died, the job failed, or
# the live time exceeded the target; 2 the venv is missing. On SIGINT/SIGTERM the services and
# the job's whole process tree are stopped at once (the job runs in the background and the
# script waits on it, so the trap is not deferred until a foreground command finishes).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "local-up: missing venv python at $PY (see requirements/all.txt)" >&2; exit 2; }

EMULATOR_HCI_PORT="${EMULATOR_HCI_PORT:-9000}"
EMULATOR_HEALTH_PORT="${EMULATOR_HEALTH_PORT:-9001}"
MOCKCLOUD_PORT="${MOCKCLOUD_PORT:-8787}"
MOCKCLOUD_CHUNK_SIZE="${MOCKCLOUD_CHUNK_SIZE:-20000}"
OUT="${LOCAL_UP_OUT:-$ROOT/build/compose/local}"
TARGET_S="${LOCAL_UP_TARGET_S:-60}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-30}"
LOGS="$OUT/logs"
mkdir -p "$LOGS"

export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT:$ROOT/emulator${PYTHONPATH:+:$PYTHONPATH}"

now_s() { "$PY" -c 'import time; print(f"{time.time():.3f}")'; }
diff_s() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
fail() { echo "local-up: FAIL $*" >&2; exit 1; }

# Every descendant of a pid (portable: ps on macOS and Linux, no pgrep needed).
tree_pids() {
  local pid="$1" child
  echo "$pid"
  for child in $(ps -A -o pid= -o ppid= 2>/dev/null | awk -v p="$pid" '$2 == p { print $1 }'); do
    tree_pids "$child"
  done
}
# Freeze the tree (nothing in it can fork or reparent), list it again, then TERM + CONT it.
kill_tree() {
  local pids
  pids="$(tree_pids "$1")"
  # shellcheck disable=SC2086
  kill -STOP $pids 2>/dev/null || true
  pids="$(tree_pids "$1")"
  # shellcheck disable=SC2086
  { kill -TERM $pids; kill -CONT $pids; } 2>/dev/null || true
}

SVC_NAMES=()
SVC_PIDS=()
FG_PID=""   # the job (or a probe) the script is currently waiting on
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  [ -n "$FG_PID" ] && kill_tree "$FG_PID"
  local pid
  for pid in "${SVC_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill_tree "$pid"
  done
  for pid in "$FG_PID" "${SVC_PIDS[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
  done
  echo "local-up: torn down (logs in $LOGS)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# Run a command in the background and wait for it, so INT/TERM reach the trap at once.
run() {
  local rc=0
  "$@" &
  FG_PID=$!
  wait "$FG_PID" || rc=$?
  FG_PID=""
  return "$rc"
}

# A port someone else holds would answer our probes for them (a leftover local-up, another
# mockcloud, the compose stack published with docker/compose.host-ports.yml). Refuse it.
port_in_use() {
  "$PY" - "$1" <<'EOF'
import socket, sys
port = int(sys.argv[1])
try:
    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
        sys.exit(0)          # something accepts connections there
except OSError:
    pass
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT leftovers
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(0)              # bound by a listener we could not reach
finally:
    s.close()
sys.exit(1)
EOF
}
for port in "$EMULATOR_HCI_PORT" "$EMULATOR_HEALTH_PORT" "$MOCKCLOUD_PORT"; do
  if port_in_use "$port"; then
    fail "port $port is already in use on 127.0.0.1 (a leftover local-up, another mockcloud or a published compose stack?); stop it or choose other ports (EMULATOR_HCI_PORT / EMULATOR_HEALTH_PORT / MOCKCLOUD_PORT)"
  fi
done

services_alive() {
  local i
  for i in "${!SVC_PIDS[@]}"; do
    kill -0 "${SVC_PIDS[$i]}" 2>/dev/null \
      || fail "${SVC_NAMES[$i]} (pid ${SVC_PIDS[$i]}) exited; see $LOGS/${SVC_NAMES[$i]}.log"
  done
}

T0="$(now_s)"
echo "local-up: starting emulator (hci tcp-server:127.0.0.1:$EMULATOR_HCI_PORT, health 127.0.0.1:$EMULATOR_HEALTH_PORT) and mockcloud (127.0.0.1:$MOCKCLOUD_PORT, chunk $MOCKCLOUD_CHUNK_SIZE)"

PLAUD_HCI_TRANSPORT="tcp-server:127.0.0.1:$EMULATOR_HCI_PORT" \
PLAUD_HCI_MODE=bridge \
PLAUD_HEALTH_HOST=127.0.0.1 \
PLAUD_HEALTH_PORT="$EMULATOR_HEALTH_PORT" \
  "$PY" "$ROOT/emulator/serve.py" >"$LOGS/emulator.log" 2>&1 &
SVC_NAMES+=(emulator); SVC_PIDS+=($!)
EMULATOR_PID=$!

"$PY" -m mockcloud --host 127.0.0.1 --port "$MOCKCLOUD_PORT" --local-file-root "$ROOT/build" \
  --chunk-size "$MOCKCLOUD_CHUNK_SIZE" >"$LOGS/mockcloud.log" 2>&1 &
SVC_NAMES+=(mockcloud); SVC_PIDS+=($!)

# The same predicates as the compose healthcheck, then: are the answers really ours?
run "$PY" "$ROOT/docker/probe.py" emulator "127.0.0.1:$EMULATOR_HEALTH_PORT" --timeout "$PROBE_TIMEOUT" \
  || { services_alive; fail "the emulator never reported ready (see $LOGS/emulator.log)"; }
run "$PY" "$ROOT/docker/probe.py" mockcloud "http://127.0.0.1:$MOCKCLOUD_PORT" --timeout "$PROBE_TIMEOUT" \
  || { services_alive; fail "the mock cloud never answered /_mock/health (see $LOGS/mockcloud.log)"; }
services_alive
HEALTH_PID="$("$PY" -c 'import json, socket, sys; s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2); print(json.loads(s.makefile("rb").readline()).get("pid"))' "$EMULATOR_HEALTH_PORT")"
[ "$HEALTH_PID" = "$EMULATOR_PID" ] || fail "the emulator health port answers for pid $HEALTH_PID, not ours ($EMULATOR_PID)"
T_LIVE="$(now_s)"
LIVE_S="$(diff_s "$T0" "$T_LIVE")"
echo "LOCAL_UP_LIVE_S=$LIVE_S"

set +e
(
  PYTHON="$PY" HARNESS_ROOT="$ROOT" JOB_OUT="$OUT/job" \
  MOCKCLOUD_URL="http://127.0.0.1:$MOCKCLOUD_PORT" \
  EMULATOR_HEALTH="127.0.0.1:$EMULATOR_HEALTH_PORT" \
  EMULATOR_HCI="tcp-client:127.0.0.1:$EMULATOR_HCI_PORT" \
  PROBE_TIMEOUT="$PROBE_TIMEOUT" \
    bash "$ROOT/docker/job.sh" 2>&1 | tee "$LOGS/job.log"
  exit "${PIPESTATUS[0]}"
) &
FG_PID=$!
wait "$FG_PID"
JOB_RC=$?
FG_PID=""
set -e

T_END="$(now_s)"
TOTAL_S="$(diff_s "$T0" "$T_END")"
echo "LOCAL_UP_TOTAL_S=$TOTAL_S"

[ "$JOB_RC" -eq 0 ] || fail "job exited $JOB_RC (see $LOGS/job.log)"
services_alive
if ! awk -v e="$LIVE_S" -v t="$TARGET_S" 'BEGIN { exit !(e <= t) }'; then
  fail "live in ${LIVE_S}s, exceeds the ${TARGET_S}s target"
fi
echo "local-up: PASS live in ${LIVE_S}s (target ${TARGET_S}s), total ${TOTAL_S}s including the job"
