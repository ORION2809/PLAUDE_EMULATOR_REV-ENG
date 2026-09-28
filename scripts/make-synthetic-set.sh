#!/usr/bin/env bash
# Produce a small synthetic meeting set under build/synthetic/ (gitignored).
#
#   ./scripts/make-synthetic-set.sh                 # 3 meetings of the "default" preset, seeds 1..3
#   N=5 SCENARIO=overlap_heavy SEED=100 ./scripts/make-synthetic-set.sh
#   ./scripts/make-synthetic-set.sh build/synthetic/my-set --set noise.snr_db=10
#
# A relative output directory is relative to the CALLER's working directory
# (it is resolved before the script changes to the repository root).
# PYTHON overrides the interpreter (default: the repository's .venv).
# Runs fully offline with the model-free formant backend; nothing is downloaded.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build/synthetic/${SCENARIO:-default}}"
shift || true
N="${N:-3}"
SCENARIO="${SCENARIO:-default}"
SEED="${SEED:-1}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
case "$PY" in */*) ;; *) PY="$(command -v "$PY" || echo "$PY")" ;; esac
[ -x "$PY" ] || { echo "missing python at $PY (set PYTHON or create $ROOT/.venv)" >&2; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m generator batch --n "$N" --scenario "$SCENARIO" --seed "$SEED" --out "$OUT" "$@"
echo "wrote $OUT"
