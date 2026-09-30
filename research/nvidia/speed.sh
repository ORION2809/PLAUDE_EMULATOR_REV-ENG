#!/bin/bash
# Quiet speed and memory runs on this Mac (docs/nvidia-speech.md "Speed"): the same three
# clips as research/nvidia/android/bench.sh, one engine per process, CPU (4 threads) and
# GPU (Metal / MPS), nothing else running.  Results: build/nvidia/speed/mac/<clip>.<run>.{json,time}
#
#   research/nvidia/speed.sh            refuses to start while inference jobs run
#   FORCE=1 research/nvidia/speed.sh    re-run existing results
#
# speechbench (the C++ path the Android app uses) measures NVIDIA's runtime and whisper.cpp;
# the Python harness measures faster-whisper, sherpa-onnx and pyannote (their timing fields).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
TC=${TOOLCHAIN:-$HOME/mivi-toolchain}
OUT=build/nvidia/speed/mac
SB=build/nvidia/speechbench/macos/speechbench
NM=$TC/nemo-models/nvidia
N35=$(ls $NM/nemotron-3.5-asr-streaming-0.6b/*/*.gguf); NEN=$(ls $NM/nemotron-speech-streaming-en-0.6b/*/*.gguf)
ND=$(ls $NM/Nemotron-3-Diarization/*/*.gguf); WM=$TC/whisper-models/ggml-small.en-q8_0.bin
COOLDOWN=${COOLDOWN:-10}
export PYTHONDONTWRITEBYTECODE=1 PYANNOTE_METRICS_ENABLED=false
mkdir -p "$OUT"
if [[ "${FORCE_BUSY:-0}" != 1 ]] && pgrep -f 'pipeline run|nemo-speech|whisper-cli|scripts/run-v5.sh' > /dev/null; then
  echo "inference is running; speed runs need a quiet machine (FORCE_BUSY=1 overrides)" >&2; exit 3
fi
CLIPS=(
  "ami-EN2002a-300s-60s=build/nvidia/device-clips/ami-EN2002a-300s-60s.wav"
  "ami-IS1009a=data/corpora/ami/test/IS1009a/mix.wav"
  "nsf-sc-MTG_32045=data/corpora/notsofar/sc/MTG_32045/mix.wav"
)
SBRUNS=(
  "sb-nemo35-cpu|nemo-asr model=$N35 gpu=-1"
  "sb-nemo35-rc13-cpu|nemo-asr model=$N35 gpu=-1 right_context=13"
  "sb-nemo-en-rc13-cpu|nemo-asr model=$NEN gpu=-1 right_context=13"
  "sb-nemo-diar-cpu|nemo-diar diar_model=$ND gpu=-1"
  "sb-whisper-cpu|whisper model=$WM gpu=0 threads=4"
  "sb-nemo35-metal|nemo-asr model=$N35 gpu=0"
  "sb-nemo-en-rc13-metal|nemo-asr model=$NEN gpu=0 right_context=13"
  "sb-nemo35-tags-metal|nemo-asr model=$N35 diar_model=$ND gpu=0"
  "sb-nemo-diar-metal|nemo-diar diar_model=$ND gpu=0"
  "sb-whisper-metal|whisper model=$WM gpu=1 threads=4"
)
PYRUNS=(  # harness pipelines: name|python|pipeline|params
  "py-faster-whisper-cpu|.venv/bin/python|faster-whisper|language=en,cpu_threads=4"
  "py-sherpa-diar-cpu|.venv/bin/python|sherpa-onnx-diarization|cluster_threshold=1.15"
  "py-pyannote-mps|.venv-pyannote/bin/python|pyannote-audio|device=mps"
  "py-pyannote-cpu|.venv-pyannote/bin/python|pyannote-audio|device=cpu"
)
for c in "${CLIPS[@]}"; do cn="${c%%=*}"; wav="${c#*=}"
  for r in "${SBRUNS[@]}"; do name="${r%%|*}"; args="${r#*|}"; o="$OUT/$cn.$name"
    [[ -s "$o.json" && "${FORCE:-0}" != 1 ]] && continue
    sleep "$COOLDOWN"; echo "[speed] $(date +%T) $cn $name"
    engine="${args%% *}"; opts="${args#* }"
    /usr/bin/time -l "$SB" "$engine" "$wav" $opts --out "$o.json" 2> "$o.time" || echo "  failed: $(tail -2 "$o.time")"
  done
  for r in "${PYRUNS[@]}"; do IFS='|' read -r name py pipe params <<<"$r"; o="$OUT/$cn.$name"
    [[ -s "$o.json" && "${FORCE:-0}" != 1 ]] && continue
    [[ "$name" == py-pyannote-cpu && "$cn" == ami-IS1009a ]] && continue   # ~15 min; the 60 s and 6 min clips suffice
    if [[ "$name" == py-pyannote-* && -z "${HF_TOKEN:-}" ]]; then
      [[ -f "${HF_TOKEN_FILE:-}" ]] && export HF_TOKEN="$(tr -d '\n' < "$HF_TOKEN_FILE")" || { echo "  $name: no HF token, skipped"; continue; }
    fi
    sleep "$COOLDOWN"; echo "[speed] $(date +%T) $cn $name"
    p=(); for kv in ${params//,/ }; do p+=(--param "$kv"); done
    /usr/bin/time -l "$py" -m pipeline run --pipeline "$pipe" --audio "$wav" --out "$o.hyp/hyp.json" "${p[@]}" > /dev/null 2> "$o.time" \
      && cp "$o.hyp/hyp.json" "$o.json" || echo "  failed: $(tail -2 "$o.time")"
  done
done
sysctl -n machdep.cpu.brand_string hw.memsize hw.ncpu > "$OUT/machine.txt"; sw_vers >> "$OUT/machine.txt"
echo "[speed] done -> $OUT"
