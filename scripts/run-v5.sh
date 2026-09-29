#!/usr/bin/env bash
# V5-style measurement (docs/v5-results.md): run the real composed pipeline
# `whisper-sherpa` (faster-whisper small.en + sherpa-onnx diarization) with and
# without the num_speakers hint, the model-free `energy-vad-cluster` baseline and
# the `oracle` self-test on
#   ami           converted AMI test-split meetings at full length: by default the 4 of the
#                 25 Sep subset (data/corpora/ami/meetings); V5_AMI_ROOT/V5_AMI_MEETINGS select
#                 another pinned set, e.g. the whole 16-meeting test split (docs/v5-results.md)
#   piper         the 4-meeting synthetic Piper set (build/synthetic/piper, mix.wav)
#   piper-device  the same Piper meetings, device-shaped Ogg/Opus (audio key `device`)
# then score everything with `python -m evals batch`, apply the gate suites and
# write a summary.  Everything lands under build/v5/ (git-ignored):
#
#   build/v5/hyp/<dataset>/<system>/<meeting_id>/hyp.{json,rttm,stm}   (+ derived <system>-turns, -wordruns)
#   build/v5/logs/<dataset>/<system>/<meeting_id>.log     stdout/stderr + /usr/bin/time -l + load average
#   build/v5/reports/<dataset>/<system>[.<variant>].{json,md,txt}   evals batch reports
#   build/v5/gates/<suite>.<dataset>.{json,txt}                      gate command output
#   build/v5/summary.{json,md}                                       the tables docs/v5-results.md quotes
#
# Idempotent: an input that is present (and verifies) is not downloaded again, a
# hypothesis that exists is not recomputed (FORCE=1 recomputes), scoring is
# recomputed for every system (or only the V5_SCORE_ONLY ones, keeping the other
# reports), and the summary is always recomputed (deterministic).
#
# Environment:
#   PYTHON=...            interpreter (default .venv/bin/python)
#   V5_OUT=DIR            output root (default build/v5)
#   V5_STAGES=a,b         subset of inputs,infer,derive,score,gates,summary (default: all)
#   V5_DATASETS=a,b       subset of ami,piper,piper-device (default: all)
#   FORCE=1               recompute hypotheses that already exist
#   V5_NO_DOWNLOAD=1      never download; a missing input is an error (exit 3)
#   V5_NO_WAIT=1          do not wait for other heavy processes (one-inference-at-a-time guard)
#   V5_WAIT_MAX=SECONDS   give up waiting after this long (default 7200; exit 4)
#   V5_AMI_ROOT=DIR       converted AMI meetings (default data/corpora/ami/meetings); must hold
#                         exactly V5_AMI_MEETINGS, because scoring walks the whole directory
#   V5_AMI_MEETINGS="..." AMI meeting ids (default: the 4-meeting subset); each needs a pin below
#   V5_ASR_CACHE=DIR      share one transcript per meeting across the whisper-sherpa systems
#                         (--param asr_cache, pipeline/adapters.py); ASR does not depend on the
#                         diarizer, so the hint / no-hint / calibrated runs differ only in diarization
#   V5_EXTRA_SYSTEMS="name:pipeline:hint:k=v,k=v ..."   more systems, run on every dataset after
#                         the standard ones (e.g. a calibrated cluster_threshold)
#   V5_AMI_GATES="suite:system:on ..."   gate suites for the ami dataset (default: the 25 Sep
#                         regression suite on the hinted run)
#   V5_ONLY_EXTRA=1       the infer stage runs only V5_EXTRA_SYSTEMS (a second instance can then
#                         run beside the main one without both computing the same hypotheses)
#   V5_REASSIGN="name:source:tie_break ..."   derived systems: the source whisper-sherpa system's
#                         words re-assigned to its own turns with another tie-break, from the ASR
#                         cache, no model run (pipeline/adapters.py reassign_hypothesis); the
#                         derive stage first checks that the source's own tie-break reproduces it
#   V5_SCORE_ONLY="name ..."   the score stage scores only the systems with exactly these names
#                         (e.g. systems added after a full run); every other report is kept as is
#
# Exit codes: 0 ok (every gate passed); 1 a gate failed; 2 an inference run failed;
# 3 a local-only input is missing and could not be provided; 4 gave up waiting.
#
# HARNESS_POLICY throughout: which systems, datasets and variants are run, the
# per-meeting hint (= the reference's active speaker count), the output layout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
OUT="${V5_OUT:-$ROOT/build/v5}"
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
STAGES=",${V5_STAGES:-inputs,infer,derive,score,gates,summary},"
DATASETS=",${V5_DATASETS:-ami,piper,piper-device},"
FORCE="${FORCE:-0}"

AMI_ROOT="${V5_AMI_ROOT:-$ROOT/data/corpora/ami/meetings}"
case "$AMI_ROOT" in /*) ;; *) AMI_ROOT="$ROOT/$AMI_ROOT" ;; esac
AMI_RAW="$ROOT/data/corpora/ami/raw"
PIPER_ROOT="$ROOT/build/synthetic/piper"
VOICE_DIR="$ROOT/data/voices/piper"
VOICE=en_US-libritts_r-medium

# Pinned inputs (sha256 computed locally on 2026-09-25 for the first four meetings and
# on 2026-09-28 for the other twelve of the test split; sizes equal the servers'
# Content-Length on those days).  AMI: CC BY 4.0.  Voice: see docs/generator.md §6.2.
AMI_BASE="https://groups.inf.ed.ac.uk/ami"
AMI_ZIP=ami_public_manual_1.6.2.zip
AMI_ZIP_SHA=b56e5babb2496b8795deeeda7e71178d7fbc9963f94276cf2a3f4b56ebbc9f9d
AMI_MEETINGS="${V5_AMI_MEETINGS:-EN2002a ES2004a IS1009a TS3003a}"
AMI_TEST_SPLIT="EN2002a EN2002b EN2002c EN2002d ES2004a ES2004b ES2004c ES2004d IS1009a IS1009b IS1009c IS1009d TS3003a TS3003b TS3003c TS3003d"
ami_wav_sha() {
  case "$1" in
    EN2002a) echo a15017d0d3c871866971ee178052a0ce3a7e44d6fbf89fc23dcab326511b65f5 ;;
    ES2004a) echo 3e2560b19bee6952c7c7ce041b0f1ea8a7ea9468044c4eea79d2a2c67e24ab0f ;;
    IS1009a) echo 6eb5a0ede0d9e72794f976ce7bea5b78133eae969f99b4c5418b43c2468d25b1 ;;
    TS3003a) echo b6ce59ff735c234afdd94b13744aa26f6785af746170543fd03b33160e920a6d ;;
    IS1009b) echo 07c2891ae6ad7c507b2a4f15b2dcd0a2343491df9e4e6a58f9db0d0b2c5e1382 ;;
    IS1009c) echo f317910070db0f2649315decbb34686d648bbe9dc3f7576710184f4d572bdbcc ;;
    IS1009d) echo 470440114ee2ed076cfc77c53136ad5d3af500d8a5a6ca36755e83534f5ca4ea ;;
    ES2004b) echo ad0cf07c42b1694ccf7bea8f1a37348d9cfab194787abc7d050b29ba56e365c8 ;;
    ES2004c) echo 47dc9bd2e3b1d174842091ae9e1fe765218f0c87658b17d6469c404e03ec2ac5 ;;
    ES2004d) echo 3cfce3297ebb7faeb53c02727eea5e9f2c76e9ed4b62db3b03d73e66836e91f3 ;;
    TS3003b) echo 0c94f3a09ab747caa7714efe8852f5ff37d36cf272b75709344991df1aa266ca ;;
    TS3003c) echo b9f455870e8cb0ba765523a1767cc366775ab7e5e3eabf37d9c91289e5a1a70a ;;
    TS3003d) echo ece4a41c37c1092c737d1dfe313afb18248364625991f745fed4b372c3c67eb3 ;;
    EN2002b) echo 50e2c8e4737850c4bf3cc24e7087c755e1c733cd91822106afd0df485ba64893 ;;
    EN2002c) echo a057708b3adb553f709fcde97d2af8e0455076bc3f88c250975227433d6e0270 ;;
    EN2002d) echo 6aa99d9d71cb695ea699d6be277f78eff68e847c93560031486eba9f18164ebe ;;
    *) echo "unpinned" ;;
  esac
}
VOICE_REV=c10ece1aade47bb51c153c893d14e5bf8e5b7117
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/$VOICE_REV/en/en_US/libritts_r/medium"
voice_sha() {
  case "$1" in
    "$VOICE.onnx") echo 10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195 ;;
    "$VOICE.onnx.json") echo b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3 ;;
    MODEL_CARD) echo 0ccde6927e5bb4d743f4ea39618a9387ba18cca3351220a8a9cfdbc68b30fcb9 ;;
  esac
}

want_stage() { [[ "$STAGES" == *",$1,"* ]]; }
want_data() { [[ "$DATASETS" == *",$1,"* ]]; }
say() { printf '[run-v5] %s\n' "$*"; }
sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------------- inputs ----

fetch_verified() {  # url dest sha256 -- download only when absent or wrong; never with a token
  local url="$1" dest="$2" sha="$3"
  if [[ -f "$dest" ]] && [[ "$(sha256 "$dest")" == "$sha" ]]; then
    say "present, sha256 ok: ${dest#$ROOT/}"
    return 0
  fi
  if [[ "${V5_NO_DOWNLOAD:-0}" == 1 ]]; then
    echo "error: ${dest#$ROOT/} missing or wrong and V5_NO_DOWNLOAD=1" >&2
    exit 3
  fi
  if [[ -f "$dest" ]]; then
    echo "error: ${dest#$ROOT/} exists with sha256 $(sha256 "$dest"), expected $sha; move it away first" >&2
    exit 3
  fi
  mkdir -p "$(dirname "$dest")"
  say "download $url"
  curl -fL --retry 3 -o "$dest.part" "$url"
  local got
  got="$(sha256 "$dest.part")"
  if [[ "$got" != "$sha" ]]; then
    rm -f "$dest.part"
    echo "error: $url: sha256 $got, expected $sha" >&2
    exit 3
  fi
  mv "$dest.part" "$dest"
}

inputs_models() {
  if "$PY" -m pipeline models --verify >/dev/null 2>&1; then
    say "model weights present and verified (python -m pipeline models --verify)"
  elif [[ "${V5_NO_DOWNLOAD:-0}" == 1 ]]; then
    echo "error: model weights missing and V5_NO_DOWNLOAD=1 (python -m pipeline fetch-models)" >&2
    exit 3
  else
    say "fetching pinned model weights (python -m pipeline fetch-models; keeps verified files)"
    "$PY" -m pipeline fetch-models
  fi
}

inputs_ami() {
  local m sums="$OUT/inputs/ami.SHA256SUMS"
  mkdir -p "$OUT/inputs"
  : >"$sums"
  for m in $AMI_MEETINGS; do
    if [[ "$(ami_wav_sha "$m")" == unpinned ]]; then
      echo "error: AMI meeting $m has no sha256 pin in scripts/run-v5.sh (ami_wav_sha)" >&2
      exit 3
    fi
    echo "$(ami_wav_sha "$m")  $m.Mix-Headset.wav" >>"$sums"
  done
  local need=0
  for m in $AMI_MEETINGS; do [[ -f "$AMI_ROOT/$m/meeting.json" ]] || need=1; done
  if [[ $need == 0 ]]; then
    for m in $AMI_MEETINGS; do
      if [[ "$(sha256 "$AMI_ROOT/$m/mix.wav")" != "$(ami_wav_sha "$m")" ]]; then
        echo "error: ${AMI_ROOT#$ROOT/}/$m/mix.wav does not match its pinned sha256" >&2
        exit 3
      fi
    done
    say "AMI meetings present, mix.wav sha256 verified: $AMI_MEETINGS"
    check_ami_root
    return 0
  fi
  fetch_verified "$AMI_BASE/AMICorpusAnnotations/$AMI_ZIP" "$AMI_RAW/$AMI_ZIP" "$AMI_ZIP_SHA"
  [[ -d "$AMI_RAW/annotations/words" ]] || "$PY" -m evals.ami extract
  for m in $AMI_MEETINGS; do
    [[ -f "$AMI_ROOT/$m/meeting.json" ]] && continue
    fetch_verified "$AMI_BASE/AMICorpusMirror/amicorpus/$m/audio/$m.Mix-Headset.wav" \
      "$AMI_RAW/audio/$m.Mix-Headset.wav" "$(ami_wav_sha "$m")"
    "$PY" -m evals.ami convert --meeting "$m" --checksums "$sums" --out "$AMI_ROOT"
  done
  check_ami_root
}

# Scoring walks the whole reference root, so it must hold exactly the selected meetings.
check_ami_root() {
  local present want
  present="$(find "$AMI_ROOT" -mindepth 2 -maxdepth 2 -name meeting.json -exec dirname {} \; 2>/dev/null | xargs -n1 basename | sort | tr '\n' ' ')"
  want="$(echo $AMI_MEETINGS | tr ' ' '\n' | sort | tr '\n' ' ')"
  if [[ "$present" != "$want" ]]; then
    echo "error: ${AMI_ROOT#$ROOT/} holds [${present% }], V5_AMI_MEETINGS is [${want% }]" >&2
    exit 3
  fi
}

inputs_voice() {
  local f
  for f in "$VOICE.onnx" "$VOICE.onnx.json" MODEL_CARD; do
    fetch_verified "$VOICE_BASE/$f" "$VOICE_DIR/$f" "$(voice_sha "$f")"
  done
  if [[ ! -f "$VOICE_DIR/$VOICE.aligned.source.json" ]]; then
    say "writing the aligned voice copy (python -m generator.tts.piper align; needs the onnx package)"
    "$PY" -m generator.tts.piper align "$VOICE_DIR/$VOICE.onnx" || \
      say "align failed; the backend patches the voice in memory when onnx is importable (docs/generator.md §6.2)"
  fi
}

make_piper_meeting() {  # dir seed extra --set args...: the exact commands of docs/generator.md §6.3
  local dir="$1" seed="$2"
  shift 2
  if [[ -f "$PIPER_ROOT/$dir/meeting.json" ]]; then
    say "Piper meeting present: ${PIPER_ROOT#$ROOT/}/$dir"
    return 0
  fi
  wait_quiet
  "$PY" -m generator make --scenario piper_meeting --seed "$seed" "$@" --out "$PIPER_ROOT/$dir"
}

inputs_piper() {
  inputs_voice
  make_piper_meeting synth-piper-2spk-s0101 101 --set name=piper-2spk --set duration_s=120
  make_piper_meeting synth-piper-2spk-overlap-s0102 102 --set name=piper-2spk-overlap --set duration_s=120 \
    --set turn_taking.overlap_ratio=0.15
  make_piper_meeting synth-piper-3spk-reverb-noise-s0103 103 --set name=piper-3spk-reverb-noise --set n_speakers=3 \
    --set duration_s=135 --set "room.dims_m=[7,6,3]" --set room.rt60_s=0.7 --set room.max_order=40 \
    --set noise.kind=white --set noise.snr_db=15
  make_piper_meeting synth-piper-4spk-s0104 104 --set name=piper-4spk --set n_speakers=4 --set duration_s=150 \
    --set turn_taking.model=random
}

# ---------------------------------------------------------------- guard -----

# One heavy inference process at a time (the lead's rule): wait while another
# model run, generation or pytest session is active.  The pattern extends the
# lead's `faster_whisper|sherpa|pipeline batch`.
HEAVY_RE='faster_whisper|sherpa|pipeline (run|batch)|generator (make|batch)|pytest'
wait_quiet() {
  [[ "${V5_NO_WAIT:-0}" == 1 ]] && return 0
  local waited=0 pids
  while :; do
    pids="$(pgrep -f "$HEAVY_RE" | grep -vx "$$" || true)"
    [[ -z "$pids" ]] && return 0
    if [[ $waited == 0 ]]; then
      say "waiting: another heavy process is running:"
      ps -o pid=,command= -p "$(echo "$pids" | paste -sd, -)" | cut -c1-160 || true
    fi
    if (( waited >= ${V5_WAIT_MAX:-7200} )); then
      echo "error: other heavy processes still running after ${waited}s" >&2
      exit 4
    fi
    sleep 30
    waited=$((waited + 30))
  done
}

# ---------------------------------------------------------------- infer -----

dataset_root() {
  case "$1" in
    ami) echo "$AMI_ROOT" ;;
    piper | piper-device) echo "$PIPER_ROOT" ;;
  esac
}
dataset_key() { [[ "$1" == piper-device ]] && echo device || echo mix; }

# TSV per meeting: dir, meeting_id, active reference speakers, audio path (pipeline.meeting.resolve_audio)
list_meetings() {
  "$PY" - "$1" "$2" <<'EOF'
import sys
from pathlib import Path
from evals.io import load_meeting
from pipeline.meeting import read_meeting, resolve_audio
root, key = Path(sys.argv[1]), sys.argv[2]
for md in sorted(p for p in root.iterdir() if (p / "meeting.json").is_file()):
    m = load_meeting(md)
    audio = resolve_audio(md, read_meeting(md), key)
    print(f"{md}\t{m.meeting_id}\t{len(m.active_speakers)}\t{audio}")
EOF
}

RUN_FAILURES=0
run_one() {  # dataset system pipeline hint(0|1) meeting_dir meeting_id n_active audio [k=v,k=v]
  local ds="$1" sys="$2" pipe="$3" hint="$4" md="$5" mid="$6" n="$7" audio="$8" extra="${9:-}"
  local dest="$OUT/hyp/$ds/$sys/$mid" log="$OUT/logs/$ds/$sys/$mid.log"
  if [[ -f "$dest/hyp.json" && "$FORCE" != 1 ]]; then
    say "exists, skipped: ${dest#$ROOT/}/hyp.json"
    return 0
  fi
  local params=() kv
  [[ "$hint" == 1 ]] && params=(--param "num_speakers=$n")
  if [[ -n "$extra" ]]; then
    for kv in ${extra//,/ }; do params+=(--param "$kv"); done
  fi
  if [[ "$pipe" == whisper-sherpa && -n "${V5_ASR_CACHE:-}" ]]; then
    params+=(--param "asr_cache=$V5_ASR_CACHE")
  fi
  wait_quiet
  mkdir -p "$(dirname "$log")" "$OUT/hyp/$ds/$sys"
  rm -rf "$dest.partial"
  {
    echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ) $ds $sys $mid pipeline=$pipe hint=$hint n_active=$n params=${params[*]:-}"
    echo "# audio $audio"
    echo "# load average at start: $(sysctl -n vm.loadavg)"
    echo "# swap at start: $(sysctl -n vm.swapusage)"
  } >"$log"
  say "$ds / $sys / $mid (${params[*]:-no hint})"
  local rc=0
  /usr/bin/time -l "$PY" -m pipeline run --pipeline "$pipe" --audio "$audio" --meeting-dir "$md" \
    --out "$dest.partial/hyp.json" ${params[@]+"${params[@]}"} >>"$log" 2>&1 || rc=$?
  echo "# load average at end: $(sysctl -n vm.loadavg)" >>"$log"
  echo "# exit $rc" >>"$log"
  if [[ $rc != 0 ]]; then
    say "FAILED ($rc): see ${log#$ROOT/}"
    RUN_FAILURES=$((RUN_FAILURES + 1))
    return 0
  fi
  rm -rf "$dest"
  mv "$dest.partial" "$dest"
}

infer_dataset() {  # dataset system:pipeline:hint ...
  local ds="$1"
  shift
  local root key rows
  root="$(dataset_root "$ds")"
  key="$(dataset_key "$ds")"
  rows="$(list_meetings "$root" "$key")"
  local spec sys pipe hint extra md mid n audio
  for spec in "$@"; do
    IFS=: read -r sys pipe hint extra <<<"$spec"
    while IFS=$'\t' read -r md mid n audio; do
      run_one "$ds" "$sys" "$pipe" "$hint" "$md" "$mid" "$n" "$audio" "$extra"
    done <<<"$rows"
  done
}

# Order: cheap systems first, then the model runs, one process at a time.
SYSTEMS_FULL=(oracle:oracle:0 energy-vad-cluster:energy-vad-cluster:0 energy-vad-cluster-hint:energy-vad-cluster:1
  whisper-sherpa:whisper-sherpa:0 whisper-sherpa-hint:whisper-sherpa:1)
SYSTEMS_DEVICE=(whisper-sherpa:whisper-sherpa:0 whisper-sherpa-hint:whisper-sherpa:1)
EXTRA_SPECS=(${V5_EXTRA_SYSTEMS:-})
EXTRA_NAMES=()
WS_EXTRA_NAMES=()     # extra systems built on whisper-sherpa (derived views, text metrics)
for spec in ${EXTRA_SPECS[@]+"${EXTRA_SPECS[@]}"}; do
  IFS=: read -r _n _p _h _x <<<"$spec"
  EXTRA_NAMES+=("$_n")
  [[ "$_p" == whisper-sherpa ]] && WS_EXTRA_NAMES+=("$_n")
done
WS_SYSTEMS=(whisper-sherpa whisper-sherpa-hint ${WS_EXTRA_NAMES[@]+"${WS_EXTRA_NAMES[@]}"})
REASSIGN_SPECS=(${V5_REASSIGN:-})
REASSIGN_NAMES=()
for spec in ${REASSIGN_SPECS[@]+"${REASSIGN_SPECS[@]}"}; do REASSIGN_NAMES+=("${spec%%:*}"); done

# ---------------------------------------------------------------- derive ----

# Two diarization-only views of each whisper-sherpa hypothesis (no inference):
#   <sys>-turns     the raw sherpa-onnx turns (hyp.extra.diarization.turns), so DER/JER
#                   can be read without the word-run segmentation (docs/pipeline.md §11.7);
#   <sys>-wordruns  the hypothesis's own word-run segments with the text removed.  DER/JER
#                   ignore text, so these equal the full hypothesis's DER/JER; they exist
#                   because evals cannot score cpWER for a hypothesis with > 20 speakers
#                   (until 28 Sep it then dropped the whole meeting; see score_lifted).
#                   Empty text keeps such a hypothesis out of meeteval entirely.
derive_turns() {
  "$PY" - "$OUT/hyp" "${WS_SYSTEMS[@]}" <<'EOF'
import hashlib, json, sys
from pathlib import Path
from evals.io import Segment, write_rttm, write_stm
hyp_root = Path(sys.argv[1])
for src_sys in sys.argv[2:]:
    for src in sorted(hyp_root.glob(f"*/{src_sys}/*/hyp.json")):
        d = json.loads(src.read_text())
        turns = d.get("extra", {}).get("diarization", {}).get("turns")
        if turns is None:
            raise SystemExit(f"{src}: no extra.diarization.turns")
        segs = [Segment(str(s), float(a), float(b), "") for a, b, s in turns if float(b) > float(a)]
        out_dir = src.parent.parent.parent / f"{src_sys}-turns" / src.parent.name
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "plaud-harness/hypothesis/1",
            "meeting_id": d["meeting_id"],
            "system": f"{d['system']}:diarization-turns",
            "segments": [s.to_dict() for s in segs],
            "extra": {"derived_from": str(src), "derived_from_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                      "note": "raw sherpa-onnx turns from extra.diarization.turns; no text (DER/JER only)"},
        }
        (out_dir / "hyp.json").write_text(json.dumps(doc, indent=2) + "\n")
        ordered = sorted(segs, key=lambda s: (s.start, s.end, s.speaker))
        write_rttm(ordered, d["meeting_id"], out_dir / "hyp.rttm")
        write_stm(ordered, d["meeting_id"], out_dir / "hyp.stm")
        print(f"[run-v5] derived {out_dir.relative_to(hyp_root.parent)} ({len(segs)} turns)")
        runs = [Segment(str(g["speaker"]), float(g["start"]), float(g["end"]), "") for g in d["segments"]]
        out_dir = src.parent.parent.parent / f"{src_sys}-wordruns" / src.parent.name
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = {**doc, "system": f"{d['system']}:word-runs-without-text", "segments": [s.to_dict() for s in runs],
               "extra": {**doc["extra"], "note": "the hypothesis's word-run segments, text removed (DER/JER only)"}}
        (out_dir / "hyp.json").write_text(json.dumps(doc, indent=2) + "\n")
        ordered = sorted(runs, key=lambda s: (s.start, s.end, s.speaker))
        write_rttm(ordered, d["meeting_id"], out_dir / "hyp.rttm")
        write_stm(ordered, d["meeting_id"], out_dir / "hyp.stm")
        print(f"[run-v5] derived {out_dir.relative_to(hyp_root.parent)} ({len(runs)} word runs)")
EOF
}

# Re-assigned views (V5_REASSIGN): no model run; see pipeline/adapters.py reassign_hypothesis.
reassign_systems() {
  "$PY" - "$OUT/hyp" "$V5_ASR_CACHE" "${REASSIGN_SPECS[@]}" <<'EOF'
import json, sys
from pathlib import Path
from pipeline.adapters import reassign_hypothesis
from evals.io import Segment, write_rttm, write_stm
hyp_root, cache, specs = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
for spec in specs:
    name, source, tb = spec.split(":")
    for ds_dir in sorted(p for p in hyp_root.iterdir() if (p / source).is_dir()):
        for src in sorted((ds_dir / source).glob("*/hyp.json")):
            doc = json.loads(src.read_text())
            own = (doc.get("extra", {}).get("assignment") or {}).get("tie_break") or "floor"
            if reassign_hypothesis(doc, cache, own)["segments"] != doc["segments"]:
                raise SystemExit(f"{src}: re-assigning with its own tie-break ({own}) does not reproduce it")
            new = reassign_hypothesis(doc, cache, tb, system=f"{doc['system']}:reassigned:{tb}")
            out_dir = ds_dir / name / src.parent.name
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "hyp.json").write_text(json.dumps(new, indent=2) + "\n")
            segs = sorted((Segment(str(s["speaker"]), float(s["start"]), float(s["end"]), s.get("text", "")) for s in new["segments"]),
                          key=lambda s: (s.start, s.end, s.speaker))
            write_rttm(segs, new["meeting_id"], out_dir / "hyp.rttm")
            write_stm(segs, new["meeting_id"], out_dir / "hyp.stm")
        print(f"[run-v5] reassigned {ds_dir.name}/{source} -> {name} (tie_break {tb}); self-check passed")
EOF
}

# ---------------------------------------------------------------- score -----

score_one() {  # dataset system variant [evals args...]
  local ds="$1" sys="$2" variant="$3"
  shift 3
  local hyps="$OUT/hyp/$ds/$sys" base="$OUT/reports/$ds/$sys"
  [[ -d "$hyps" ]] || return 0
  [[ -n "$variant" ]] && base="$base.$variant"
  mkdir -p "$(dirname "$base")"
  local rc=0
  "$PY" -m evals batch --refs "$(dataset_root "$ds")" --hyps "$hyps" --report "$base.json" --md "$base.md" \
    "$@" >"$base.txt" 2>&1 || rc=$?
  echo "# exit $rc" >>"$base.txt"
  say "scored $ds/$sys${variant:+ ($variant)}: exit $rc -> ${base#$ROOT/}.{json,md,txt}"
}

# meeteval 0.4.3 refuses cpWER/tcpWER when either side has more than 20 speakers
# (meeteval/wer/wer/cp.py `_minimum_permutation_word_error_rate`: "Are you sure?").
# `python -m evals batch` then keeps the meeting's DER/JER/WER and marks cpWER/tcpWER
# not scored (`not_scored` in the report) -- that is the primary result and it is
# kept.  (Before 2026-09-28 evals recorded the whole meeting under `errors`.)  As a
# SUPPLEMENTARY, labelled
# measurement (HARNESS_POLICY), the same `evals.cli.main batch` path is re-run
# in-process with only that guard removed from meeteval's source (the guard block
# is cut out textually and the function re-compiled in meeteval's own module
# namespace; the step fails if the guard text is not found).  The docstring of
# cp_word_error_rate says the Hungarian implementation "works for large numbers of speakers".
score_lifted() {  # dataset system variant [evals args...] -> reports/<ds>/<sys>.lifted[.<variant>].*
  local ds="$1" sys="$2" variant="$3"
  shift 3
  local hyps="$OUT/hyp/$ds/$sys" base="$OUT/reports/$ds/$sys.lifted"
  [[ -d "$hyps" ]] || return 0
  [[ -n "$variant" ]] && base="$base.$variant"
  local rc=0
  "$PY" - "$(dataset_root "$ds")" "$hyps" "$base" "$@" >"$base.txt" 2>&1 <<'EOF' || rc=$?
import inspect, sys
import meeteval.wer.wer.cp as cp
src = inspect.getsource(cp._minimum_permutation_word_error_rate)
start = src.index("    if max(len(hypothesis), len(reference)) > 20:")
end = src.index("    assignment, distance, _ = _minimum_permutation_assignment(")
assert "Are you sure?" in src[start:end] and src[start:end].count("\n    if ") == 0, src[start:end]
ns: dict = {}
exec(compile(src[:start] + src[end:], f"{cp.__file__} (20-speaker guard lifted)", "exec"), cp.__dict__, ns)
cp._minimum_permutation_word_error_rate = ns["_minimum_permutation_word_error_rate"]
print("meeteval cpWER 20-speaker guard lifted in-process (supplementary scoring, docs/v5-results.md)")
from evals.cli import main
refs, hyps, base, *extra = sys.argv[1:]
sys.exit(main(["batch", "--refs", refs, "--hyps", hyps, "--report", base + ".json", "--md", base + ".md", *extra]))
EOF
  echo "# exit $rc" >>"$base.txt"
  say "scored $ds/$sys with the meeteval speaker guard lifted${variant:+ ($variant)}: exit $rc -> ${base#$ROOT/}.{json,md,txt}"
}

has_errors() {  # report.json -> true when evals recorded per-meeting errors, or meeteval refused
  # a meeting's cpWER/tcpWER (> 20 speakers; since 28 Sep evals keeps DER/JER/WER and records the refusal)
  [[ -f "$1" ]] && "$PY" -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["errors"] or any(m["cpwer"].get("refused") or m["tcpwer"].get("refused") for m in r["meetings"]) else 1)' "$1"
}

in_score_only() {  # system -> true unless V5_SCORE_ONLY is set and does not name it
  [[ -z "${V5_SCORE_ONLY:-}" || " ${V5_SCORE_ONLY} " == *" $1 "* ]]
}

score_dataset() {
  local ds="$1" sys ws derived=()
  for ws in "${WS_SYSTEMS[@]}"; do derived+=("$ws-wordruns" "$ws-turns"); done
  for sys in oracle energy-vad-cluster energy-vad-cluster-hint "${WS_SYSTEMS[@]}" \
    ${EXTRA_NAMES[@]+"${EXTRA_NAMES[@]}"} ${REASSIGN_NAMES[@]+"${REASSIGN_NAMES[@]}"} "${derived[@]}"; do
    [[ -d "$OUT/hyp/$ds/$sys" ]] || continue
    in_score_only "$sys" || continue
    score_one "$ds" "$sys" ""                       # evals defaults: DER collar 0.25 (±0.125 s), tcpWER collar 5 s
    score_one "$ds" "$sys" collar0 --der-collar 0   # no DER collar, overlap scored
    rm -f "$OUT/reports/$ds/$sys".lifted*
    if has_errors "$OUT/reports/$ds/$sys.json"; then
      score_lifted "$ds" "$sys" ""
      score_lifted "$ds" "$sys" collar0 --der-collar 0
    elif [[ " ${WS_SYSTEMS[*]} " == *" $sys "* ]]; then
      # control: where the guard does not fire, the lifted path must equal the standard one
      score_lifted "$ds" "$sys" control
    fi
  done
  for sys in "${WS_SYSTEMS[@]}" ${REASSIGN_NAMES[@]+"${REASSIGN_NAMES[@]}"}; do
    # an if, not an && list: a false test must not become the function's status under set -e
    if [[ -d "$OUT/hyp/$ds/$sys" ]] && in_score_only "$sys"; then
      score_one "$ds" "$sys" norm --numbers-to-words --expand-contractions
    fi
  done
}

# ---------------------------------------------------------------- gates -----

GATE_FAILURES=0
gate_one() {  # suite dataset system gate-on -> gates/<suite>.<dataset>[.<system>].{json,txt}
  local suite="$1" ds="$2" sys="$3" on="$4" out="$OUT/gates/$1.$2"
  # the 25 Sep layout names a gate by suite and dataset; other systems add their name
  [[ "$sys" == whisper-sherpa-hint || "$sys" == oracle ]] || out="$out.$sys"
  [[ -d "$OUT/hyp/$ds/$sys" ]] || { say "no hypotheses for $ds/$sys; gate $suite not run"; return 0; }
  mkdir -p "$OUT/gates"
  local cmd=("$PY" -m evals batch --refs "$(dataset_root "$ds")" --hyps "$OUT/hyp/$ds/$sys"
    --gates "$ROOT/evals/gates.yaml" --suite "$suite" --gate-on "$on" --report "$out.json")
  local rc=0
  {
    echo "\$ python -m evals batch --refs ${cmd[5]#$ROOT/} --hyps ${cmd[7]#$ROOT/} --gates evals/gates.yaml --suite $suite --gate-on $on --report ${out#$ROOT/}.json"
    "${cmd[@]}" 2>&1 || rc=$?
    echo "exit $rc"
  } >"$out.txt"
  cat "$out.txt"
  [[ $rc == 0 ]] || GATE_FAILURES=$((GATE_FAILURES + 1))
}

# ---------------------------------------------------------------- summary ---

summarise() {
  "$PY" - "$ROOT" "$OUT" "$AMI_ROOT" "${WS_SYSTEMS[*]}" "${EXTRA_NAMES[*]:-} ${REASSIGN_NAMES[*]:-}" <<'EOF'
import hashlib, json, platform, re, subprocess, sys, importlib.metadata as md
from pathlib import Path
from pipeline.meeting import read_meeting, resolve_audio
root, out, ami_root = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
ws_systems, extra = sys.argv[4].split(), sys.argv[5].split()
DATASETS = {"ami": ami_root, "piper": root / "build/synthetic/piper",
            "piper-device": root / "build/synthetic/piper"}
AUDIO_KEY = {"ami": "mix", "piper": "mix", "piper-device": "device"}
SYSTEMS = ["oracle", "energy-vad-cluster", "energy-vad-cluster-hint", *ws_systems,
           *[s for s in extra if s not in ws_systems],
           *[f"{s}-wordruns" for s in ws_systems], *[f"{s}-turns" for s in ws_systems]]
VARIANTS = ["collar0", "norm", "lifted", "lifted.collar0", "lifted.control"]
TEXT_SYSTEMS = {"oracle", *ws_systems, *[s for s in extra if not s.startswith("energy") and not s.startswith("embedding")]}
KEYS = ["der.der", "der.miss_rate", "der.false_alarm_rate", "der.confusion_rate", "jer.jer",
        "cpwer.error_rate", "tcpwer.error_rate", "wer_concat.wer", "wer_literal.wer",
        "speaker_count.error", "speaker_count.abs_error", "speaker_count.hypothesis", "der.total",
        "cpwer.length", "cpwer.errors", "tcpwer.errors", "wer_concat.errors", "wer_concat.reference_words"]

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def rel(p):
    """Repository-relative when inside the repository (V5_OUT may point elsewhere)."""
    p = Path(p)
    return str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
def sh(*a):
    try: return subprocess.run(a, capture_output=True, text=True, check=False).stdout.strip()
    except OSError: return ""
def ver(n):
    try: return md.version(n)
    except md.PackageNotFoundError: return None

def flat_of(m):
    """MeetingReport.flat() paths from a report's nested per-meeting dict."""
    out = {}
    for k in KEYS:
        head, _, tail = k.partition(".")
        if head == "der" and tail.endswith("_rate"):
            d = m["der"]; comp = tail[: -len("_rate")]
            out[k] = (d[comp] / d["total"]) if d["total"] else None
        else:
            out[k] = m.get(head, {}).get(tail)
    return out

def report(ds, sys_, variant=""):
    p = out / "reports" / ds / (sys_ + (f".{variant}" if variant else "") + ".json")
    if not p.is_file(): return None
    r = json.loads(p.read_text())
    return {"file": rel(p),
            "per_meeting": {m["meeting_id"]: flat_of(m) for m in r["meetings"]},
            "macro": {k: r["macro"].get(k) for k in KEYS} if r["meetings"] else None,
            "micro": {k: r["micro"].get(k) for k in KEYS} if r["meetings"] else None,
            "macro_counts": {k: r["macro"].get("counts", {}).get(k) for k in KEYS} if r["meetings"] else None,
            "errors": [{"meeting_dir": e["meeting_dir"], "error": e["error"].splitlines()[0] + " " +
                        (e["error"].splitlines()[1] if len(e["error"].splitlines()) > 1 else "")} for e in r["errors"]],
            "missing": r["missing"],
            "refused": sorted(m["meeting_id"] for m in r["meetings"]
                              if m["cpwer"].get("refused") or m["tcpwer"].get("refused"))}

def parse_log(p):
    if not p.is_file(): return {}
    t = p.read_text()
    r = {}
    m = re.search(r"(\d+)\s+maximum resident set size", t); r["max_rss_bytes"] = int(m.group(1)) if m else None
    m = re.search(r"([\d.]+) real", t); r["wall_s"] = float(m.group(1)) if m else None
    m = re.search(r"# (\S+Z) ", t); r["started_utc"] = m.group(1) if m else None
    for tag in ("start", "end"):
        m = re.search(rf"load average at {tag}: \{{ ([\d.]+) ([\d.]+) ([\d.]+) \}}", t)
        r[f"load_{tag}"] = [float(x) for x in m.groups()] if m else None
    r["warnings"] = sorted({l.strip() for l in t.splitlines() if "Warning" in l})
    return r

env = {
    "date_utc": sh("date", "-u", "+%Y-%m-%dT%H:%M:%SZ"),
    "machine": {"cpu": sh("sysctl", "-n", "machdep.cpu.brand_string"), "ncpu": sh("sysctl", "-n", "hw.ncpu"),
                "memsize_bytes": sh("sysctl", "-n", "hw.memsize"), "os": platform.platform()},
    "python": platform.python_version(),
    "packages": {n: ver(n) for n in ("faster-whisper", "ctranslate2", "sherpa-onnx", "onnxruntime", "numpy",
                                      "pyannote.metrics", "pyannote.core", "meeteval", "jiwer", "piper-tts")},
    "git_head": sh("git", "-C", str(root), "rev-parse", "HEAD"),
    "git_dirty_paths": len([l for l in sh("git", "-C", str(root), "status", "--porcelain").splitlines() if l]),
    "code_sha256": {str(p.relative_to(root)): sha(p) for p in sorted(
        [*root.glob("pipeline/*.py"), *root.glob("evals/*.py"), root / "evals/gates.yaml", root / "scripts/run-v5.sh"])},
}

summary = {"schema": "plaud-harness/v5-summary/3", "environment": env, "datasets": {}}
for ds, refs in DATASETS.items():
    if not (out / "reports" / ds).is_dir(): continue
    dsum = {"refs": rel(refs), "audio_key": AUDIO_KEY[ds], "meetings": {}, "systems": {}}
    for sys_ in SYSTEMS:
        std = report(ds, sys_)
        if std is None: continue
        entry = {**std, "variants": {v: r for v in VARIANTS if (r := report(ds, sys_, v)) is not None}, "runs": {}}
        for hp in sorted((out / "hyp" / ds / sys_).glob("*/hyp.json")):
            h = json.loads(hp.read_text()); ex = h.get("extra", {}); dz = ex.get("diarization", {})
            entry["runs"][h["meeting_id"]] = {
                "timing": ex.get("timing"), "n_speakers": dz.get("n_speakers"), "hint": dz.get("num_speakers_hint"),
                "hint_honoured": dz.get("hint_honoured"), "n_turns": dz.get("n_turns"),
                "n_hyp_segments": len(h["segments"]), "n_hyp_speakers": len({s["speaker"] for s in h["segments"]}),
                "n_hyp_words": sum(len(s.get("words") or []) for s in h["segments"]),
                "asr_cache_hit": (ex.get("asr", {}).get("cache") or {}).get("hit"),
                "process": parse_log(out / "logs" / ds / sys_ / f"{h['meeting_id']}.log")}
        ctl = entry["variants"].get("lifted.control")
        if ctl is not None:
            entry["lifted_control_equals_standard"] = ctl["per_meeting"] == std["per_meeting"]
        dsum["systems"][sys_] = entry
    for mdir in sorted(p for p in refs.iterdir() if (p / "meeting.json").is_file()):
        meeting = read_meeting(mdir); mid = meeting["meeting_id"]
        if not any(mid in e["runs"] for e in dsum["systems"].values()): continue
        segs = meeting["segments"]; audio_path = resolve_audio(mdir, meeting, AUDIO_KEY[ds])
        dsum["meetings"][mid] = {
            "duration_s": meeting["duration_s"], "speakers_declared": len(meeting["speakers"]),
            "speakers_active": len({s["speaker"] for s in segs}), "segments": len(segs),
            "words": sum(len(s.get("words") or []) for s in segs),
            "audio": rel(audio_path), "audio_bytes": audio_path.stat().st_size,
            "audio_sha256": sha(audio_path)}
    for hp in sorted((out / "hyp" / ds / "whisper-sherpa").glob("*/hyp.json")):
        dsum["models"] = json.loads(hp.read_text()).get("extra", {}).get("models"); break
    det = {}
    def words_of(p):
        return sorted(((w["w"], w["start"], w["end"]) for s in json.loads(p.read_text())["segments"]
                       for w in s.get("words") or []), key=lambda x: (x[1], x[2], x[0]))
    for mid in dsum["meetings"]:
        paths = [out / "hyp" / ds / s / mid / "hyp.json" for s in ws_systems]
        paths = [p for p in paths if p.is_file()]
        if len(paths) >= 2:
            ws = [words_of(p) for p in paths]
            det[mid] = {"systems": [p.parent.parent.name for p in paths], "words": [len(w) for w in ws],
                        "identical": all(w == ws[0] for w in ws[1:])}
    dsum["asr_words_identical_across_hint_runs"] = det
    # control: DER/JER of the text-free word runs equal those of the full hypothesis wherever both were scored
    wr = {}
    for base in ws_systems:
        full, runs = dsum["systems"].get(base), dsum["systems"].get(base + "-wordruns")
        if not full or not runs: continue
        scored = dict(full["per_meeting"]); lifted = full["variants"].get("lifted")
        if lifted: scored.update({m: r for m, r in lifted["per_meeting"].items() if m not in scored})
        both = [m for m in scored if m in runs["per_meeting"]]
        wr[base] = {"meetings_compared": len(both), "der_jer_equal": all(
            scored[m]["der.der"] == runs["per_meeting"][m]["der.der"] and scored[m]["jer.jer"] == runs["per_meeting"][m]["jer.jer"] for m in both)}
    dsum["wordruns_der_jer_equal_full_hypothesis"] = wr
    summary["datasets"][ds] = dsum
(out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

def f(x, nd=4):
    if x is None: return "–"
    if isinstance(x, float): return f"{x:.{nd}f}"
    return str(x)
L = [f"# V5 summary ({env['date_utc']})", "",
     f"git HEAD {env['git_head']} + {env['git_dirty_paths']} uncommitted paths; generated by scripts/run-v5.sh", ""]
for ds, dsum in summary["datasets"].items():
    L += [f"## {ds} ({dsum['refs']}, audio key `{dsum['audio_key']}`)", "",
          "| meeting | duration s | active spk | ref words | audio | audio sha256 |", "|---|---|---|---|---|---|"]
    for mid, i in dsum["meetings"].items():
        L.append(f"| {mid} | {f(i['duration_s'], 3)} | {i['speakers_active']} | {i['words']} | `{i['audio']}` | `{i['audio_sha256']}` |")
    L += ["", "| system | report | agg | DER | miss | FA | conf | JER | cpWER | tcpWER | WER concat | spk err | errors | cpWER refused |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sys_, e in dsum["systems"].items():
        txt = sys_ in TEXT_SYSTEMS
        for vname, rep in [("standard", e), *e["variants"].items()]:
            for agg in ("macro", "micro"):
                a = rep[agg]
                if a is None:
                    L.append(f"| {sys_} | {vname} | {agg} | " + " | ".join(["–"] * 9) + f" | {len(rep['errors'])} | {len(rep['refused'])} |"); continue
                L.append(f"| {sys_} | {vname} | {agg} | {f(a['der.der'])} | {f(a['der.miss_rate'])} | {f(a['der.false_alarm_rate'])} | "
                         f"{f(a['der.confusion_rate'])} | {f(a['jer.jer'])} | {f(a['cpwer.error_rate']) if txt else 'n/a'} | "
                         f"{f(a['tcpwer.error_rate']) if txt else 'n/a'} | {f(a['wer_concat.wer']) if txt else 'n/a'} | "
                         f"{f(a['speaker_count.error'], 2)} | {len(rep['errors'])} | {len(rep['refused'])} |")
    L += ["", "| system | meeting | report | DER | JER | cpWER | tcpWER | WER concat | hyp spk | hint | honoured | RTF | wall s | max RSS MB | load start/end (1 min) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sys_, e in dsum["systems"].items():
        txt = sys_ in TEXT_SYSTEMS
        for mid, run in e["runs"].items():
            src, r = ("standard", e["per_meeting"].get(mid))
            if r is None and "lifted" in e["variants"]:
                src, r = "lifted", e["variants"]["lifted"]["per_meeting"].get(mid)
            r = r or {k: None for k in KEYS}
            t = run.get("timing") or {}; p = run.get("process") or {}; rss = p.get("max_rss_bytes")
            ls, le = p.get("load_start"), p.get("load_end")
            L.append(f"| {sys_} | {mid} | {src} | {f(r['der.der'])} | {f(r['jer.jer'])} | {f(r['cpwer.error_rate']) if txt else 'n/a'} | "
                     f"{f(r['tcpwer.error_rate']) if txt else 'n/a'} | {f(r['wer_concat.wer']) if txt else 'n/a'} | {run['n_hyp_speakers']} | "
                     f"{f(run['hint'])} | {f(run['hint_honoured'])} | {f(t.get('rtf'), 3)} | {f(p.get('wall_s'), 2)} | "
                     f"{f(rss / 1e6 if rss else None, 0)} | {f(ls[0] if ls else None, 2)} / {f(le[0] if le else None, 2)} |")
    L += ["", f"ASR words identical across the whisper-sherpa systems: {json.dumps(dsum['asr_words_identical_across_hint_runs'])}"]
    ctl = {s: e.get("lifted_control_equals_standard") for s, e in dsum["systems"].items() if "lifted_control_equals_standard" in e}
    L += [f"Lifted-guard control equals the standard evals path: {json.dumps(ctl)}",
          f"Text-free word runs give the full hypothesis's DER/JER: {json.dumps(dsum['wordruns_der_jer_equal_full_hypothesis'])}", ""]
(out / "summary.md").write_text("\n".join(L) + "\n")
print(f"[run-v5] summary -> {out / 'summary.md'} and {out / 'summary.json'}")
EOF
}

# ---------------------------------------------------------------- main ------

say "output root: ${OUT#$ROOT/}"
if want_stage inputs; then
  if want_data ami || want_data piper || want_data piper-device; then inputs_models; fi
  if want_data ami; then inputs_ami; fi
  if want_data piper || want_data piper-device; then inputs_piper; fi
fi
if want_stage infer; then
  if want_data ami && [[ "${V5_ONLY_EXTRA:-0}" == 1 ]]; then
    infer_dataset ami ${EXTRA_SPECS[@]+"${EXTRA_SPECS[@]}"}
  elif want_data ami; then infer_dataset ami "${SYSTEMS_FULL[@]}" ${EXTRA_SPECS[@]+"${EXTRA_SPECS[@]}"}; fi
  if want_data piper; then infer_dataset piper "${SYSTEMS_FULL[@]}" ${EXTRA_SPECS[@]+"${EXTRA_SPECS[@]}"}; fi
  if want_data piper-device; then infer_dataset piper-device "${SYSTEMS_DEVICE[@]}"; fi
fi
if want_stage derive; then
  derive_turns
  if [[ ${#REASSIGN_SPECS[@]} -gt 0 ]]; then
    [[ -n "${V5_ASR_CACHE:-}" ]] || { echo "error: V5_REASSIGN needs V5_ASR_CACHE" >&2; exit 3; }
    reassign_systems
  fi
fi
if want_stage score; then
  for ds in ami piper piper-device; do want_data "$ds" && score_dataset "$ds"; done
fi
if want_stage gates; then
  want_data ami && gate_one oracle ami oracle each
  want_data piper && gate_one oracle piper oracle each
  # the 25 Sep regression suites gate the hinted runs (num_speakers = the reference's
  # active speaker count); V5_AMI_GATES picks others, e.g. ami-headset on the full test split
  if want_data ami; then
    for g in ${V5_AMI_GATES:-ami-subset-whisper-sherpa:whisper-sherpa-hint:macro}; do
      IFS=: read -r _suite _sys _on <<<"$g"
      gate_one "$_suite" ami "$_sys" "$_on"
    done
  fi
  want_data piper && gate_one synthetic-piper-whisper-sherpa piper whisper-sherpa-hint macro
fi
if want_stage summary; then summarise; fi

say "hypotheses: ${OUT#$ROOT/}/hyp/<dataset>/<system>/<meeting_id>/hyp.json"
say "logs:       ${OUT#$ROOT/}/logs/<dataset>/<system>/<meeting_id>.log"
say "reports:    ${OUT#$ROOT/}/reports/<dataset>/<system>[.collar0|.norm|.lifted*].{json,md,txt}"
say "gates:      ${OUT#$ROOT/}/gates/<suite>.<dataset>.{json,txt}"
say "summary:    ${OUT#$ROOT/}/summary.{md,json}"
if [[ $RUN_FAILURES != 0 ]]; then
  echo "error: $RUN_FAILURES inference run(s) failed" >&2
  exit 2
fi
if [[ $GATE_FAILURES != 0 ]]; then
  echo "error: $GATE_FAILURES gate suite(s) failed" >&2
  exit 1
fi
