#!/bin/bash
# The accuracy runs of the NVIDIA comparison (docs/nvidia-speech.md), as two lanes
# that can run side by side on the 8 GB M1:
#
#   research/nvidia/queue.sh gpu [job ...]   Metal/MPS jobs, one at a time (NVIDIA models,
#                                            whisper.cpp, pyannote on MPS)
#   research/nvidia/queue.sh cpu [job ...]   CPU jobs (faster-whisper + sherpa-onnx)
#   research/nvidia/queue.sh list
#
# Every job is scripts/run-v5.sh's infer stage with V5_ONLY_EXTRA: a hypothesis that
# exists is skipped, so a lane can be stopped and restarted.  Timing from these runs
# is measured under contention; speed is measured separately (research/nvidia/speed.sh).
# NOTSOFAR sets use run-v5's "ami" dataset slot (it runs any root of harness meetings);
# their hypotheses land under build/nvidia/notsofar-<variant>/hyp/ami/.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
TC=${TOOLCHAIN:-$HOME/mivi-toolchain}
export PYTHONDONTWRITEBYTECODE=1
export NEMO_SPEECH_BIN=${NEMO_SPEECH_BIN:-$TC/NeMo-Speech.cpp/build/metal-asr/bin/nemo-speech}
export NEMO_SPEECH_MODEL_DIR=${NEMO_SPEECH_MODEL_DIR:-$TC/nemo-models}
export WHISPER_CPP_BIN=${WHISPER_CPP_BIN:-$TC/whisper.cpp/build/macos-metal/bin/whisper-cli}
export WHISPER_CPP_MODEL=${WHISPER_CPP_MODEL:-$TC/whisper-models/ggml-small.en-q8_0.bin}
export PYANNOTE_METRICS_ENABLED=false

AMI_IDS="EN2002a EN2002b EN2002c EN2002d ES2004a ES2004b ES2004c ES2004d IS1009a IS1009b IS1009c IS1009d TS3003a TS3003b TS3003c TS3003d"
nsf_ids() { ls "data/corpora/notsofar/$1" 2>/dev/null | grep '^MTG_' | tr '\n' ' '; }

# set  -> V5_OUT, reference root, meeting ids, whisper ASR cache
set_env() {
  case "$1" in
    test) OUTD=build/nvidia/test; REFS=data/corpora/ami/test; IDS=$AMI_IDS; WCACHE=$ROOT/build/v5-test/asr-cache ;;
    nsf-sc) OUTD=build/nvidia/notsofar-sc; REFS=data/corpora/notsofar/sc; IDS=$(nsf_ids sc); WCACHE=$ROOT/build/nvidia/notsofar-sc/asr-cache-whisper ;;
    nsf-ct) OUTD=build/nvidia/notsofar-ctmix; REFS=data/corpora/notsofar/ctmix; IDS=$(nsf_ids ctmix); WCACHE=$ROOT/build/nvidia/notsofar-ctmix/asr-cache-whisper ;;
    *) echo "unknown set $1" >&2; exit 2 ;;
  esac
}

# job = set:system-spec (run-v5 V5_EXTRA_SYSTEMS syntax name:pipeline:hint:k=v,...);
# NCACHE / WCPPCACHE are substituted per set.
GPU_JOBS=(
  "test:nemotron-en-rc13:nemotron-asr:0:asr_model=nemotron-en,right_context=13,asr_cache=NCACHE"
  "nsf-sc:nemotron:nemotron:0:asr_cache=NCACHE"
  "nsf-sc:nemotron-en-rc13:nemotron-asr:0:asr_model=nemotron-en,right_context=13,asr_cache=NCACHE"
  "nsf-sc:whisper-cpp:whisper-cpp:0:asr_cache=WCPPCACHE"
  "nsf-sc:pyannote-audio:pyannote-audio:0:device=mps"
  "nsf-sc:whisper-pyannote:faster-whisper+pyannote:0:language=en,cpu_threads=4,diarization_device=mps"
  "test:nemotron35-rc13:nemotron-asr:0:right_context=13,asr_cache=NCACHE"
  "test:whisper-cpp:whisper-cpp:0:asr_cache=WCPPCACHE"
  "test:nemo-diar-v3offline:nemo-speech-diarization:0:diar_preset=v3-offline"
  "test:sortformer-v2:nemo-speech-diarization:0:diar_model=nvidia/diar_streaming_sortformer_4spk-v2"
  "nsf-ct:nemotron:nemotron:0:asr_cache=NCACHE"
  "nsf-ct:nemotron-en-rc13:nemotron-asr:0:asr_model=nemotron-en,right_context=13,asr_cache=NCACHE"
  "nsf-ct:whisper-cpp:whisper-cpp:0:asr_cache=WCPPCACHE"
  "nsf-ct:pyannote-audio:pyannote-audio:0:device=mps"
  "nsf-ct:whisper-pyannote:faster-whisper+pyannote:0:language=en,cpu_threads=4,diarization_device=mps"
  "test:parakeet-tdt:nemotron-asr:0:asr_model=parakeet-tdt,asr_cache=NCACHE"
  "nsf-sc:nemotron-tagged:nemotron-tagged:0"
  "nsf-sc:nemotron35-rc13:nemotron-asr:0:right_context=13,asr_cache=NCACHE"
  "nsf-sc:sortformer-v2:nemo-speech-diarization:0:diar_model=nvidia/diar_streaming_sortformer_4spk-v2"
  "test:nemotron-tagged:nemotron-tagged:0"
)
CPU_JOBS=(
  "nsf-sc:whisper-sherpa-cal:whisper-sherpa:0:cluster_threshold=1.15,assignment_tie_break=latest_start"
  "nsf-ct:whisper-sherpa-cal:whisper-sherpa:0:cluster_threshold=1.15,assignment_tie_break=latest_start"
)

run_job() {
  local job="$1" set spec name pipe py=".venv/bin/python"
  set="${job%%:*}"; spec="${job#*:}"; name="${spec%%:*}"; pipe="$(echo "$spec" | cut -d: -f2)"
  set_env "$set"
  spec="${spec//NCACHE/$ROOT/$OUTD/asr-cache-nemo}"
  spec="${spec//WCPPCACHE/$ROOT/$OUTD/asr-cache-wcpp}"
  if [[ "$pipe" == pyannote-audio || "$pipe" == faster-whisper+pyannote ]]; then
    py=".venv-pyannote/bin/python"
    local tok="${HF_TOKEN_FILE:-}"
    [[ -n "${HF_TOKEN:-}" || -f "$tok" ]] || { echo "[queue] $job: needs HF_TOKEN or HF_TOKEN_FILE; skipped" >&2; return 0; }
    [[ -n "${HF_TOKEN:-}" ]] || export HF_TOKEN="$(tr -d '\n' < "$tok")"
  fi
  mkdir -p "$OUTD"
  echo "[queue] $(date '+%F %T') start $set $name" | tee -a "$OUTD/queue.log"
  PYTHON="$ROOT/$py" V5_OUT="$OUTD" V5_AMI_ROOT="$REFS" V5_AMI_MEETINGS="$IDS" V5_DATASETS=ami V5_STAGES=infer \
    V5_ONLY_EXTRA=1 V5_NO_WAIT=1 V5_ASR_CACHE="$WCACHE" V5_EXTRA_SYSTEMS="$spec" \
    ./scripts/run-v5.sh >> "$OUTD/infer-$name.log" 2>&1
  echo "[queue] $(date '+%F %T') end $set $name exit $?" | tee -a "$OUTD/queue.log"
}

lane="${1:-list}"; shift || true
case "$lane" in
  gpu) jobs=("${GPU_JOBS[@]}") ;;
  cpu) jobs=("${CPU_JOBS[@]}") ;;
  list) printf 'gpu %s\n' "${GPU_JOBS[@]}"; printf 'cpu %s\n' "${CPU_JOBS[@]}"; exit 0 ;;
  *) echo "usage: $0 gpu|cpu|list [set:name ...]" >&2; exit 2 ;;
esac
for job in "${jobs[@]}"; do
  if [[ $# -gt 0 ]]; then
    want=0; for sel in "$@"; do [[ "$job" == "$sel":* ]] && want=1; done
    [[ $want == 1 ]] || continue
  fi
  run_job "$job"
done
