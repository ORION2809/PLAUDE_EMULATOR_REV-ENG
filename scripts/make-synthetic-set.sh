#!/usr/bin/env bash
# Produce a small synthetic meeting set under build/synthetic/ (gitignored).
#
#   ./scripts/make-synthetic-set.sh                 # 3 meetings of the "default" preset, seeds 1..3
#   N=5 SCENARIO=overlap_heavy SEED=100 ./scripts/make-synthetic-set.sh
#   ./scripts/make-synthetic-set.sh build/synthetic/my-set --set noise.snr_db=10
#
# Runs fully offline with the model-free formant backend; nothing is downloaded.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build/synthetic/${SCENARIO:-default}}"
shift || true
N="${N:-3}"
SCENARIO="${SCENARIO:-default}"
SEED="${SEED:-1}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing venv python at $PY" >&2; exit 1; }
mkdir -p "$OUT"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m generator batch --n "$N" --scenario "$SCENARIO" --seed "$SEED" --out "$OUT" "$@"
echo "wrote $OUT"
