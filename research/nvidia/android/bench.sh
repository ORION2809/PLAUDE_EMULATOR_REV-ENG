#!/bin/bash
# On-device benchmark of both local stacks (docs/nvidia-speech.md), over adb.
#
#   research/nvidia/android/bench.sh <serial> push     copy runtime, models and clips to /data/local/tmp/sb
#   research/nvidia/android/bench.sh <serial> run      run every (engine, clip) pair, one process each
#   research/nvidia/android/bench.sh <serial> pull     copy results to build/nvidia/device/<model>-<serial>/
#   research/nvidia/android/bench.sh <serial> clean    remove /data/local/tmp/sb from the device
#
# Every run is `toybox time -v speechbench <engine> <clip> ...` (or a vendor CLI) in its
# own process, so "Max RSS" is that engine's peak alone; CPU backend, 4 threads (NeMo-
# Speech.cpp hard-codes 4; whisper.cpp and sherpa-onnx are set to 4).  Before each run
# the script waits COOLDOWN seconds and records battery temperature and CPU max
# frequencies (throttling), in <result>.env.
set -euo pipefail
SERIAL=${1:?serial}; CMD=${2:?push|run|pull|clean}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TC=${TOOLCHAIN:-$HOME/mivi-toolchain}
ADB=${ADB:-$TC/android-sdk/platform-tools/adb}
adbp() { "$ADB" -s "$SERIAL" "$@"; }
NATIVE=${SB_OUT:-$ROOT/build/nvidia/speechbench}/android
D=/data/local/tmp/sb
COOLDOWN=${COOLDOWN:-30}
NM=$TC/nemo-models/nvidia
MODELS=(  # device name  <-  local file
  "nemotron-3.5.gguf=$(ls $NM/nemotron-3.5-asr-streaming-0.6b/*/*.gguf 2>/dev/null | head -1)"
  "nemotron-en.gguf=$(ls $NM/nemotron-speech-streaming-en-0.6b/*/*.gguf 2>/dev/null | head -1)"
  "nemotron-diar.gguf=$(ls $NM/Nemotron-3-Diarization/*/*.gguf 2>/dev/null | head -1)"
  "whisper-small.en-q8_0.bin=$TC/whisper-models/ggml-small.en-q8_0.bin"
  "segmentation-3.0.onnx=$ROOT/data/models/sherpa-onnx/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
  "campplus.onnx=$ROOT/data/models/sherpa-onnx/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)
# clip name  <-  harness meeting dir (mix.wav, 16 kHz mono PCM16) or WAV
CLIPS=(
  "ami-EN2002a-300s-60s.wav=$ROOT/build/nvidia/device-clips/ami-EN2002a-300s-60s.wav"
  "ami-IS1009a.wav=$ROOT/data/corpora/ami/test/IS1009a/mix.wav"
  "nsf-sc-MTG_32045.wav=$ROOT/data/corpora/notsofar/sc/MTG_32045/mix.wav"
)
# name | runner | engine/args (clip substituted for CLIP, models under $D/models)
PLAN=(
  "sb-nemo35-tags|sb|nemo-asr CLIP model=$D/models/nemotron-3.5.gguf diar_model=$D/models/nemotron-diar.gguf"
  "sb-nemo35|sb|nemo-asr CLIP model=$D/models/nemotron-3.5.gguf"
  "sb-nemo-en-rc13|sb|nemo-asr CLIP model=$D/models/nemotron-en.gguf right_context=13"
  "sb-nemo35-rc13|sb|nemo-asr CLIP model=$D/models/nemotron-3.5.gguf right_context=13"
  "sb-nemo-en|sb|nemo-asr CLIP model=$D/models/nemotron-en.gguf"
  "sb-nemo-en-rc13-tags|sb|nemo-asr CLIP model=$D/models/nemotron-en.gguf right_context=13 diar_model=$D/models/nemotron-diar.gguf"
  "sb-nemo-diar|sb|nemo-diar CLIP diar_model=$D/models/nemotron-diar.gguf"
  "sb-whisper|sb|whisper CLIP model=$D/models/whisper-small.en-q8_0.bin threads=4"
  "sb-sherpa-diar|sb|sherpa-diar CLIP segmentation=$D/models/segmentation-3.0.onnx embedding=$D/models/campplus.onnx threshold=1.15"
  "cli-nemo-speech|nemo|transcribe CLIP --model $D/models/nemotron-3.5.gguf --language en-US --json --device cpu"
  "cli-whisper|whisper|-m $D/models/whisper-small.en-q8_0.bin -f CLIP -l en -t 4 -bs 5 -ojf -of OUTBASE -np"
)

case "$CMD" in
  push)
    [[ -x "$NATIVE/speechbench" ]] || { echo "run build-native.sh android first" >&2; exit 2; }
    mkdir -p "$ROOT/build/nvidia/device-clips"
    c="$ROOT/build/nvidia/device-clips/ami-EN2002a-300s-60s.wav"
    [[ -f "$c" ]] || PYTHONDONTWRITEBYTECODE=1 "$ROOT/.venv/bin/python" -c "
import soundfile as sf; x, sr = sf.read('$ROOT/data/corpora/ami/test/EN2002a/mix.wav', start=16000*300, frames=16000*60, dtype='int16')
sf.write('$c', x, sr, subtype='PCM_16')"
    adbp shell "mkdir -p $D/lib $D/models $D/clips $D/results $D/whisper-cli"
    adbp push "$NATIVE/speechbench" "$NATIVE/nemo-speech" "$D/" > /dev/null
    adbp push "$NATIVE"/lib/. "$D/lib/" > /dev/null
    adbp push "$NATIVE"/whisper-cli/. "$D/whisper-cli/" > /dev/null
    adbp shell "chmod 755 $D/speechbench $D/nemo-speech $D/whisper-cli/whisper-cli"
    for m in "${MODELS[@]}"; do n="${m%%=*}"; f="${m#*=}"
      if [[ "$(adbp shell "stat -c %s $D/models/$n 2>/dev/null" | tr -d '\r')" != "$(stat -f %z "$f")" ]]; then adbp push "$f" "$D/models/$n" > /dev/null; fi
      echo "model $n $(stat -f %z "$f") bytes"; done
    for c in "${CLIPS[@]}"; do adbp push "${c#*=}" "$D/clips/${c%%=*}" > /dev/null; echo "clip ${c%%=*}"; done
    adbp shell "cd $D && sha256sum speechbench nemo-speech lib/* whisper-cli/* models/* clips/*" > "$ROOT/build/nvidia/device-clips/pushed.SHA256SUMS.$SERIAL"
    ;;
  run)
    only=${ONLY:-}   # e.g. ONLY="sb-nemo35-tags sb-whisper" or clip filter CLIPS_ONLY
    for c in "${CLIPS[@]}"; do cn="${c%%=*}"
      [[ -n "${CLIPS_ONLY:-}" && " $CLIPS_ONLY " != *" $cn "* ]] && continue
      for p in "${PLAN[@]}"; do IFS='|' read -r name runner args <<<"$p"
        [[ -n "$only" && " $only " != *" $name "* ]] && continue
        out="$D/results/${cn%.wav}.$name"
        if adbp shell "test -s $out.json -o -s $out.time" && [[ "${FORCE:-0}" != 1 ]]; then echo "exists: $out"; continue; fi
        sleep "$COOLDOWN"
        a="${args//CLIP/$D/clips/$cn}"; a="${a//OUTBASE/$out}"
        adbp shell "{ date +%s; dumpsys battery | grep temperature; cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq | tr '\n' ' '; } > $out.env"
        case "$runner" in
          sb) cmd="LD_LIBRARY_PATH=$D/lib toybox time -v $D/speechbench $a --out $out.json" ;;
          nemo) cmd="LD_LIBRARY_PATH=$D/lib NEMO_SPEECH_MODEL_DIR=$D/models toybox time -v $D/nemo-speech $a > $out.json" ;;
          whisper) cmd="LD_LIBRARY_PATH=$D/whisper-cli toybox time -v $D/whisper-cli/whisper-cli $a" ;;
        esac
        echo "[bench] $(date '+%T') $cn $name"
        adbp shell "cd $D && ( $cmd ) 2> $out.time; echo exit=\$? >> $out.time" || true
        adbp shell "{ date +%s; dumpsys battery | grep temperature; cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq | tr '\n' ' '; } >> $out.env"
        adbp shell "grep -E 'Real time|Max RSS|exit=' $out.time" | tr -d '\r' | tr '\n' ' '; echo
      done
    done
    ;;
  pull)
    model="$(adbp shell getprop ro.product.model | tr -d '\r')"
    dest="$ROOT/build/nvidia/device/$model-$SERIAL"
    mkdir -p "$dest"
    adbp pull "$D/results/." "$dest/" > /dev/null
    { for p in ro.product.manufacturer ro.product.model ro.soc.manufacturer ro.soc.model ro.board.platform \
               ro.build.version.release ro.build.version.sdk ro.build.fingerprint; do
        echo "$p=$(adbp shell getprop $p | tr -d '\r')"; done
      adbp shell "grep -E 'CPU part|Features' /proc/cpuinfo | sort | uniq -c; grep MemTotal /proc/meminfo"
    } > "$dest/device.txt"
    cp "$ROOT/build/nvidia/device-clips/pushed.SHA256SUMS.$SERIAL" "$dest/pushed.SHA256SUMS" 2>/dev/null || true
    echo "pulled to $dest"; ls "$dest" | wc -l
    ;;
  clean) adbp shell "rm -rf $D" ;;
  *) echo "unknown command $CMD" >&2; exit 2 ;;
esac
