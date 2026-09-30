#!/bin/bash
# Accuracy and speed of the Apple engine (mivi-speech) on the harness's meetings
# (docs/nvidia-speech.md "Apple"): one process per meeting, then harness scoring.
#
#   research/nvidia/apple/run_eval.sh <set> <config-name> [mivi-speech flags...]
#     set: test (AMI test, 16 meetings) | nsf-sc | nsf-ct (NOTSOFAR-1 eval-full-only)
#   e.g. run_eval.sh test ane-fast128 --asr-compute ane --diar fast128 --diar-compute ane
#
# Results: build/nvidia/apple/<set>/<config>/{results/<mid>.json, logs/<mid>.log, hyp/, report*.json}
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SET=${1:?set}; CFG=${2:?config name}; shift 2
BIN="$ROOT/research/nvidia/apple/MiviSpeech/.build/release/mivi-speech"
MODELS=${MIVI_MODELS:-$HOME/mivi-toolchain/fluid-models}
case "$SET" in
  test) REFS=$ROOT/data/corpora/ami/test ;;
  nsf-sc) REFS=$ROOT/data/corpora/notsofar/sc ;;
  nsf-ct) REFS=$ROOT/data/corpora/notsofar/ctmix ;;
  *) echo "unknown set $SET" >&2; exit 2 ;;
esac
OUT=$ROOT/build/nvidia/apple/$SET/$CFG
mkdir -p "$OUT/results" "$OUT/logs" "$MODELS"
export PYTHONDONTWRITEBYTECODE=1
for md in "$REFS"/*/; do
  mid=$(basename "$md"); r="$OUT/results/$mid.json"
  [[ -s "$r" && "${FORCE:-0}" != 1 ]] && continue
  echo "[apple] $(date +%T) $SET $CFG $mid"
  /usr/bin/time -l "$BIN" transcribe "$md/mix.wav" --models "$MODELS" --out "$r.part" --quiet "$@" > "$OUT/logs/$mid.log" 2>&1 \
    && mv "$r.part" "$r" || { echo "  failed: $(tail -3 "$OUT/logs/$mid.log")"; rm -f "$r.part"; }
done
"$ROOT/.venv/bin/python" "$ROOT/research/nvidia/apple/to_hyp.py" "$OUT/results" "$OUT/hyp" "apple-$CFG" || echo "[apple] assignment parity check FAILED"
for sys in "apple-$CFG" "apple-$CFG-turns"; do
  "$ROOT/.venv/bin/python" -m evals batch --refs "$REFS" --hyps "$OUT/hyp/$sys" --report "$OUT/report.$sys.json" --quiet
done
"$ROOT/.venv/bin/python" - "$OUT" "$CFG" <<'EOF'
import json, sys, glob
out, cfg = sys.argv[1], sys.argv[2]
for s in (f"apple-{cfg}", f"apple-{cfg}-turns"):
    m = json.load(open(f"{out}/report.{s}.json"))["macro"]
    print(f"{s:36s} n={m['n']} cpWER {m.get('cpwer.error_rate')} WER {m.get('wer_concat.wer')} DER {m.get('der.der'):.4f} "
          f"miss {m.get('der.miss_rate'):.4f} conf {m.get('der.confusion_rate'):.4f}")
rt = [json.load(open(f)) for f in glob.glob(f"{out}/results/*.json")]
aud = sum(r["timing"]["audio_s"] for r in rt); proc = sum(r["timing"]["process_s"] for r in rt)
print(f"RTF (pooled) {proc / aud:.4f} over {aud / 3600:.2f} h; asr {sum(r['timing']['asr_s'] for r in rt) / aud:.4f}, "
      f"diar {sum(r['timing']['diar_s'] for r in rt) / aud:.4f}; max footprint "
      f"{max(r['memory']['peak_footprint_bytes'] for r in rt) / 2**20:.0f} MB")
EOF
