#!/usr/bin/env bash
# plaud-harness compose job: the end-to-end check that runs once the emulator
# and the mock cloud are healthy (the `job` service of docker-compose.yml; also
# run inline by scripts/local-up.sh with PYTHON pointed at the venv).
#
#   1. probe-mockcloud  GET /_mock/health                                   (Layer 4 reachable)
#   2. probe-emulator   health line ready:true                               (Layer 1 reachable)
#   3. scan-emulator    a Bumble central attaches through the emulator's bridged controller and
#                       receives a PlaudPeripheral advertisement: company 0xFFFF, portVersion 7
#   4. generate         a small synthetic set                 python -m generator batch
#   5. oracle           pipeline over that set                python -m pipeline batch --pipeline oracle
#   6. score            against the ground truth              python -m evals batch --suite oracle
#   7. cloud-roundtrip  the first meeting's device recording through the mock cloud over HTTP:
#                       identity -> bind -> presign (>= 2 parts) -> PUT parts -> complete ->
#                       download (byte-exact) -> ai/transcriptions -> poll -> hyp.json -> unbind
#                                                             docker/cloud_roundtrip.py
#   8. cloud-score      that hyp.json against the meeting     python -m evals score --suite oracle
#
# Exit status is the first failing step's (evals exits 1 on a gate failure, 2 on
# malformed input and when meetings exist but none has a hypothesis, 3 only when
# the reference root is absent or holds no meeting.json; cloud_roundtrip.py exits
# 1 on any cloud step, 2 on an unusable meeting dir). Every step is bounded: the probes by
# PROBE_TIMEOUT (docker/probe.py bounds each whole attempt, not just the wait),
# the cloud task by JOB_CLOUD_TIMEOUT plus a 10 s limit per HTTP request, and
# the generator / pipeline / evals runs are finite. Every knob is an env var;
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
JOB_CLOUD_TIMEOUT="${JOB_CLOUD_TIMEOUT:-60}"
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
"$PY" "$HERE/probe.py" scan "$EMULATOR_HCI" --timeout "$PROBE_TIMEOUT" --company-id 0xFFFF --port-version 7

step generate "n=$JOB_MEETINGS scenario=$JOB_SCENARIO seed=$JOB_SEED"
rm -rf "$OUT/meetings" "$OUT/hyp"
rm -f "$OUT/report.json" "$OUT/report.md" "$OUT/cloud-report.json" "$OUT/cloud-report.md"
"$PY" -m generator batch --n "$JOB_MEETINGS" --scenario "$JOB_SCENARIO" --seed "$JOB_SEED" --out "$OUT/meetings"

step oracle "pipeline=oracle (HARNESS SELF-TEST, not a system under test)"
"$PY" -m pipeline batch --pipeline oracle --root "$OUT/meetings" --out "$OUT/hyp/oracle" --fail-fast

step score "suite=$JOB_SUITE"
"$PY" -m evals batch --refs "$OUT/meetings" --hyps "$OUT/hyp/oracle" \
  --gates "$ROOT/evals/gates.yaml" --suite "$JOB_SUITE" \
  --report "$OUT/report.json" --md "$OUT/report.md"

MEETING_DIR="$("$PY" -c 'import pathlib, sys; m = sorted(p.parent for p in pathlib.Path(sys.argv[1]).rglob("meeting.json")); print(m[0] if m else "")' "$OUT/meetings")"
step cloud-roundtrip "meeting=$MEETING_DIR mockcloud=$MOCKCLOUD_URL (the mock's ground-truth answer: a HARNESS SELF-TEST)"
[ -n "$MEETING_DIR" ] || { echo "job: no meeting.json under $OUT/meetings" >&2; false; }
"$PY" "$HERE/cloud_roundtrip.py" --url "$MOCKCLOUD_URL" --meeting-dir "$MEETING_DIR" \
  --out "$OUT/hyp/cloud" --timeout "$JOB_CLOUD_TIMEOUT"

MEETING_ID="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["meeting_id"])' "$MEETING_DIR/meeting.json")"
step cloud-score "suite=$JOB_SUITE meeting=$MEETING_ID"
"$PY" -m evals score --ref "$MEETING_DIR" --hyp "$OUT/hyp/cloud/$MEETING_ID/hyp.json" \
  --gates "$ROOT/evals/gates.yaml" --suite "$JOB_SUITE" \
  --report "$OUT/cloud-report.json" --md "$OUT/cloud-report.md"

T1="$(now_s)"
printf 'JOB_OK elapsed_s=%s report=%s cloud_report=%s\n' "$(elapsed "$T0" "$T1")" "$OUT/report.json" "$OUT/cloud-report.json"
