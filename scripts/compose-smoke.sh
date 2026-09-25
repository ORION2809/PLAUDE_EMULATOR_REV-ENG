#!/usr/bin/env bash
# V6 smoke: time `docker compose up -d --wait` against the 60 s target, then run the
# one-shot job and propagate its exit code.
#
#   ./scripts/compose-smoke.sh                 # build (untimed), up --wait (timed), run job, down -v
#   KEEP_UP=1 ./scripts/compose-smoke.sh       # leave the system running afterwards
#   SKIP_BUILD=1 ./scripts/compose-smoke.sh    # images already built
#   COMPOSE_TARGET_S=60                         # the target (seconds)
#
# Exit codes: 0 pass; 1 the bring-up exceeded the target, `up --wait` failed or the job
# failed; 2 Docker (or the compose v2 plugin, or the daemon) is not available here.
#
# HARNESS_POLICY: the image build is excluded from the timed window (V6 is about bringing
# the system live, and the first build of numpy/scipy/pyroomacoustics/av dominates by
# minutes); `docker compose build` is timed and printed separately so nothing is hidden.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_S="${COMPOSE_TARGET_S:-60}"
PROJECT="${COMPOSE_PROJECT_NAME:-plaud-harness}"

now_s() { python3 -c 'import time; print(f"{time.time():.3f}")' 2>/dev/null || date +%s; }
diff_s() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }

if ! command -v docker >/dev/null 2>&1; then
  echo "compose-smoke: docker is not installed or not on PATH; V6 cannot be executed on this machine." >&2
  echo "compose-smoke: the same topology runs without Docker via ./scripts/local-up.sh" >&2
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "compose-smoke: 'docker compose' (the v2 plugin) is not available." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "compose-smoke: the Docker daemon is not reachable (is it running?)." >&2
  exit 2
fi

COMPOSE=(docker compose -f "$ROOT/docker-compose.yml" -p "$PROJECT")

cleanup() {
  if [ "${KEEP_UP:-0}" = "1" ]; then
    echo "compose-smoke: KEEP_UP=1, leaving project '$PROJECT' running"
  else
    "${COMPOSE[@]}" --profile check down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

"${COMPOSE[@]}" --profile check config -q
echo "compose-smoke: compose file valid; project=$PROJECT target_s=$TARGET_S"

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  tb0="$(now_s)"
  "${COMPOSE[@]}" --profile check build
  tb1="$(now_s)"
  echo "compose-smoke: build_s=$(diff_s "$tb0" "$tb1") (not part of the V6 window)"
fi

# Start from nothing so the measurement is a cold bring-up of the system.
"${COMPOSE[@]}" --profile check down -v --remove-orphans >/dev/null 2>&1 || true

t0="$(now_s)"
up_rc=0
"${COMPOSE[@]}" up -d --wait --wait-timeout "$((TARGET_S * 2))" || up_rc=$?
t1="$(now_s)"
elapsed="$(diff_s "$t0" "$t1")"
echo "compose-smoke: up_wait_s=$elapsed target_s=$TARGET_S up_rc=$up_rc"
"${COMPOSE[@]}" ps -a || true

if [ "$up_rc" -ne 0 ]; then
  echo "compose-smoke: FAIL docker compose up -d --wait exited $up_rc" >&2
  "${COMPOSE[@]}" logs --no-color --tail 60 || true
  exit 1
fi

tj0="$(now_s)"
job_rc=0
"${COMPOSE[@]}" run --rm job || job_rc=$?
tj1="$(now_s)"
echo "compose-smoke: job_s=$(diff_s "$tj0" "$tj1") job_rc=$job_rc"
if [ "$job_rc" -ne 0 ]; then
  echo "compose-smoke: FAIL job exited $job_rc" >&2
  exit 1
fi

if ! awk -v e="$elapsed" -v t="$TARGET_S" 'BEGIN { exit !(e <= t) }'; then
  echo "compose-smoke: FAIL system live in ${elapsed}s, exceeds the ${TARGET_S}s target" >&2
  exit 1
fi
echo "compose-smoke: PASS system live in ${elapsed}s (target ${TARGET_S}s); job exit 0"
