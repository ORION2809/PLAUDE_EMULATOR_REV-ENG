#!/usr/bin/env bash
# plaud-harness compose job: the end-to-end check that runs once the emulator
# and the mock cloud are healthy (the `job` service of docker-compose.yml; also
# run inline by scripts/local-up.sh with PYTHON pointed at the venv).
#
#   1. probe   mockcloud  GET /_mock/health                       (Layer 4 reachable)
#   2. probe   emulator   health line ready:true                   (Layer 1 reachable)
#   3. scan    a Bumble central attaches through the emulator's bridged controller
#              and receives the PlaudPeripheral advertisement (portVersion 7)
#   4. generate a small synthetic set        python -m generator batch
#   5. oracle   pipeline over that set        python -m pipeline batch --pipeline oracle
#   6. score    against the ground truth      python -m evals batch --suite oracle
#
# Exit status is the first failing step's (evals exits 1 on a gate failure,
# 2 on malformed input, 3 when nothing was scored). Every knob is an env var;
# the defaults are the compose service names (HARNESS_POLICY).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HARNESS_ROOT:-$(cd "$HERE/.." && pwd)}"
PY="${PYTHON:-python}"
OUT="${JOB_OUT:-$ROOT/build/compose/job}"
MOCKCLOUD_URL="${MOCKCLOUD_URL:-http://mockcloud:8787}"
EMULATOR_HEALTH="${EMULATOR_HEALTH:-emulator:9001}"
EMULATOR_HCI="${EMULATOR_HCI:-tcp-client:emulator:9000}"
JOB_MEETINGS="${JOB_MEETINGS:-2}"
JOB_SCENARIO="${JOB_SCENARIO:-smoke}"
JOB_SEED="${JOB_SEED:-1}"
JOB_SUITE="${JOB_SUITE:-oracle}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-30}"

export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT:$ROOT/emulator${PYTHONPATH:+:$PYTHONPATH}"

now_s() { "$PY" -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() { "$PY" -c 'import sys; print(f"{float(sys.argv[2]) - float(sys.argv[1]):.2f}")' "$1" "$2"; }

STEP="init"
step() { STEP="$1"; shift; printf 'JOB_STEP %s %s\n' "$STEP" "$*"; }
on_err() { local rc=$?; printf 'JOB_FAIL step=%s rc=%s\n' "$STEP" "$rc" >&2; exit "$rc"; }
trap on_err ERR

T0="$(now_s)"
mkdir -p "$OUT"
printf 'JOB_START out=%s mockcloud=%s emulator=%s hci=%s meetings=%s scenario=%s seed=%s suite=%s\n' \
  "$OUT" "$MOCKCLOUD_URL" "$EMULATOR_HEALTH" "$EMULATOR_HCI" "$JOB_MEETINGS" "$JOB_SCENARIO" "$JOB_SEED" "$JOB_SUITE"

step probe-mockcloud "$MOCKCLOUD_URL"
"$PY" "$HERE/probe.py" mockcloud "$MOCKCLOUD_URL" --timeout "$PROBE_TIMEOUT"

step probe-emulator "$EMULATOR_HEALTH"
"$PY" "$HERE/probe.py" emulator "$EMULATOR_HEALTH" --timeout "$PROBE_TIMEOUT"

step scan-emulator "$EMULATOR_HCI"
"$PY" "$HERE/probe.py" scan "$EMULATOR_HCI" --timeout "$PROBE_TIMEOUT"

step generate "n=$JOB_MEETINGS scenario=$JOB_SCENARIO seed=$JOB_SEED"
rm -rf "$OUT/meetings" "$OUT/hyp"
"$PY" -m generator batch --n "$JOB_MEETINGS" --scenario "$JOB_SCENARIO" --seed "$JOB_SEED" --out "$OUT/meetings"

step oracle "pipeline=oracle (HARNESS SELF-TEST, not a system under test)"
"$PY" -m pipeline batch --pipeline oracle --root "$OUT/meetings" --out "$OUT/hyp/oracle" --fail-fast

step score "suite=$JOB_SUITE"
"$PY" -m evals batch --refs "$OUT/meetings" --hyps "$OUT/hyp/oracle" \
  --gates "$ROOT/evals/gates.yaml" --suite "$JOB_SUITE" \
  --report "$OUT/report.json" --md "$OUT/report.md"

T1="$(now_s)"
printf 'JOB_OK elapsed_s=%s report=%s\n' "$(elapsed "$T0" "$T1")" "$OUT/report.json"
