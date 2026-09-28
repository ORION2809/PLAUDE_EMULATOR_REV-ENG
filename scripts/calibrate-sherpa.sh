#!/usr/bin/env bash
# Calibrate sherpa-onnx's clustering threshold (the `cluster_threshold` --param
# of sherpa-onnx-diarization and whisper-sherpa) on the AMI DEVELOPMENT split.
# The test split is never used for tuning (docs/v5-results.md).
#
#   ./scripts/calibrate-sherpa.sh                       # all stages
#   CALIB_STAGES=score,select ./scripts/calibrate-sherpa.sh
#
# sherpa-onnx's segmentation and speaker embeddings do not depend on the
# threshold, and they are almost all of its cost; so each dev meeting is run
# through them ONCE (`python -m pipeline.sherpa_sweep precompute`, cached as
# .npz), and every threshold of CALIB_THRESHOLDS is replayed from the cache
# (`sweep`: sherpa-onnx's own FastClustering, then a line-by-line port of its
# label reconstruction; pipeline/sherpa_sweep.py).  The replay was checked
# bit-identical to real sherpa-onnx runs on IS1008a at five thresholds, and
# the `confirm` stage repeats that check at the selected threshold on
# CALIB_CONFIRM meetings.  Each threshold is scored without a speaker-count
# hint with `python -m evals batch` (DER collar 0.25, as V5); the lowest macro
# DER wins (ties: the smaller macro |speaker-count error|, then the lower
# threshold).  HARNESS_POLICY: the grid, the criterion.
#
# A last stage compares the word-assignment tie-breaks (pipeline/base.py
# TIE_BREAKS) on the same dev meetings: the REFERENCE words, stripped of their
# speakers, are assigned to the selected threshold's diarization turns with
# each rule and scored with cpWER.  It isolates the tie-break (ASR errors do
# not enter) and costs no inference; it is a proxy, because Whisper's words
# and timings differ from the reference's.  The lowest macro cpWER wins
# (ties: the current default, floor).
#
# Outputs (CALIB_OUT, default build/v5-calib):
#   pre/<meeting>.npz (cached stages), logs/precompute/<meeting>.log
#   hyp/<threshold>/<meeting>/hyp.json (replayed), confirm/<meeting>/hyp.json (real sherpa-onnx)
#   reports/<threshold>.{json,md,txt}, selected.json, summary.md
#   tiebreak.json, tiebreak.md
#
# Environment:
#   CALIB_THRESHOLDS="..."  grid (default below)
#   CALIB_JOBS=N            parallel inference processes (default 1)
#   CALIB_ROOT=DIR          converted dev meetings (default data/corpora/ami/dev)
#   CALIB_STAGES=a,b        subset of inputs,precompute,sweep,score,select,confirm,tiebreak
#   CALIB_CONFIRM="..."     dev meetings re-run with real sherpa-onnx at the selected threshold
#                           (default: IS1008a TS3004a)
#   V5_NO_DOWNLOAD=1        never download (a missing input is an error, exit 3)
#
# Exit codes: 0 ok; 2 an inference run failed; 3 an input is missing or wrong.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
OUT="${CALIB_OUT:-$ROOT/build/v5-calib}"
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
DEV_ROOT="${CALIB_ROOT:-$ROOT/data/corpora/ami/dev}"
case "$DEV_ROOT" in /*) ;; *) DEV_ROOT="$ROOT/$DEV_ROOT" ;; esac
THRESHOLDS="${CALIB_THRESHOLDS:-$(awk 'BEGIN{for(t=0.80;t<=1.4001;t+=0.025) printf "%.3f ", t}')}"
JOBS="${CALIB_JOBS:-1}"
STAGES=",${CALIB_STAGES:-inputs,precompute,sweep,score,select,confirm,tiebreak},"
CONFIRM="${CALIB_CONFIRM:-IS1008a TS3004a}"
want_stage() { [[ "$STAGES" == *",$1,"* ]]; }
say() { printf '[calibrate] %s\n' "$*"; }
sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

# AMI dev split (BUT setup lists, data/corpora/ami/but_setup/lists/dev.meetings.txt).
# Mix-Headset WAVs, CC BY 4.0; sha256 computed locally on 2026-09-28, sizes equal
# the server's Content-Length on that day.
AMI_BASE="https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus"
DEV_MEETINGS="IS1008a IS1008b IS1008c IS1008d ES2011a ES2011b ES2011c ES2011d TS3004a TS3004b TS3004c TS3004d IB4001 IB4002 IB4003 IB4004 IB4010 IB4011"
dev_sha() {
  case "$1" in
    IS1008a) echo 75ae8796ca2177fa0c77055de2fe2f95325b412dc7152a9b32c76c9884a3d382 ;;
    IS1008b) echo 0d992a210480c6e9652b317ed7f66261e80a1aa86f7e5c3dffd10e81b8e30de2 ;;
    IS1008c) echo 1965c56162efb29953d21a5e7f5659f7f78a41e8923b7c399d3b4bbf9ae4b0a9 ;;
    IS1008d) echo 22bc4c5ef7c033427426e84b168f71d162a42ed287ae4781aa2f6c06b67a6177 ;;
    ES2011a) echo 130bc4218891a5b4cdbedcd716c94cd062f6ea13b2c19b77fe9d82e4023e1c78 ;;
    ES2011b) echo 63459ac9811903fe49f79982e9155b457425fa62c5f1f47d4047f512cd348c83 ;;
    ES2011c) echo 8c2d75e76817ab770ccab71e48e1d41e62729992e6f928bf2a6a8a4bd381e111 ;;
    ES2011d) echo 8f4f6d823d691037c04a9f7f0bb6ffa3db512d85aa69245144a55d55cfe4f102 ;;
    TS3004a) echo ca3bc00d6c85451918290b0a3dd137e322ad8b063fd26f71fe3bdaa42ad66719 ;;
    TS3004b) echo 3c8e8e9f4b24cc9a8c2fa5932cbecebcf9d14b6b415ac48d6229547fc014c9b2 ;;
    TS3004c) echo 4fea0b90a24164511a902f9bc148322ee9dea236195690af081bc0033db640ea ;;
    TS3004d) echo d89950bb537e05939994c06083447bd820a86a64cb32aec4d691e7096f974cdf ;;
    IB4001) echo 11e395326e9d8922801c736822a91477aa8bb2a65ce353585e3b19724e294c5d ;;
    IB4002) echo a97ec6a4118347b5a18dc5ec98df2412f1de8581a2b0542d8c82ba6b4d56aec4 ;;
    IB4003) echo a28e876a8dc3ed076ce38a167d8de4cc1d3ff8c0b5142c57d4c194ece7752fc2 ;;
    IB4004) echo 106be8125d20fe35ca483d48a782866f1b99c9aaa1a1a51cbf94c50933540b18 ;;
    IB4010) echo 996c2f77648eb34adc9221a08d1cf4a4d7a4c1daddbe2b25cd9bff4e6495b3e0 ;;
    IB4011) echo fa1e9060e7314dd703d919a493145b5d20a1b9e8a23f6903f362fbbaccc842d5 ;;
    *) echo unpinned ;;
  esac
}

inputs() {
  local m f sums="$OUT/inputs/ami-dev.SHA256SUMS"
  mkdir -p "$OUT/inputs" "$ROOT/data/corpora/ami/raw/audio"
  : >"$sums"
  for m in $DEV_MEETINGS; do
    [[ "$(dev_sha "$m")" != unpinned ]] || { echo "error: dev meeting $m has no pin" >&2; exit 3; }
    echo "$(dev_sha "$m")  $m.Mix-Headset.wav" >>"$sums"
  done
  [[ -d "$ROOT/data/corpora/ami/raw/annotations/words" ]] || { echo "error: AMI annotations not extracted (./scripts/run-v5.sh inputs stage, or python -m evals.ami extract)" >&2; exit 3; }
  for m in $DEV_MEETINGS; do
    f="$ROOT/data/corpora/ami/raw/audio/$m.Mix-Headset.wav"
    if [[ ! -f "$f" ]]; then
      [[ "${V5_NO_DOWNLOAD:-0}" != 1 ]] || { echo "error: $f missing and V5_NO_DOWNLOAD=1" >&2; exit 3; }
      say "download $m"
      curl -fL --retry 3 -o "$f.part" "$AMI_BASE/$m/audio/$m.Mix-Headset.wav"
      mv "$f.part" "$f"
    fi
    [[ "$(sha256 "$f")" == "$(dev_sha "$m")" ]] || { echo "error: $f does not match its pin" >&2; exit 3; }
    [[ -f "$DEV_ROOT/$m/meeting.json" ]] || "$PY" -m evals.ami convert --meeting "$m" --checksums "$sums" --out "$DEV_ROOT"
  done
  local present want
  present="$(find "$DEV_ROOT" -mindepth 2 -maxdepth 2 -name meeting.json -exec dirname {} \; | xargs -n1 basename | sort | tr '\n' ' ')"
  want="$(echo $DEV_MEETINGS | tr ' ' '\n' | sort | tr '\n' ' ')"
  [[ "$present" == "$want" ]] || { echo "error: ${DEV_ROOT#$ROOT/} holds [${present% }], expected the 18 dev meetings" >&2; exit 3; }
  say "dev meetings present and verified: 18"
}

precompute_one() {  # meeting
  local m="$1" dest="$OUT/pre/$1.npz" log="$OUT/logs/precompute/$1.log"
  [[ -f "$dest" ]] && return 0
  mkdir -p "$(dirname "$log")"
  echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ) precompute $m; load: $(sysctl -n vm.loadavg 2>/dev/null || cat /proc/loadavg)" >"$log"
  if /usr/bin/time -l "$PY" -m pipeline.sherpa_sweep precompute --meeting-dir "$DEV_ROOT/$m" --out "$dest" >>"$log" 2>&1; then
    say "precomputed $m"
  else
    say "FAILED precompute $m (see ${log#$ROOT/})"
    return 1
  fi
}
export -f precompute_one say
export OUT DEV_ROOT PY ROOT

precompute() {
  say "precompute: 18 meetings, $JOBS at a time"
  printf '%s\n' $DEV_MEETINGS | xargs -P "$JOBS" -L 1 bash -c 'precompute_one "$0"' || { echo "error: precompute failures" >&2; exit 2; }
}

sweep() {
  rm -rf "$OUT/hyp"
  "$PY" -m pipeline.sherpa_sweep sweep --pre-dir "$OUT/pre" --refs "$DEV_ROOT" --thresholds "$THRESHOLDS" --out "$OUT/hyp"
}

score() {
  local t
  mkdir -p "$OUT/reports"
  rm -rf "$OUT/reports"
  mkdir -p "$OUT/reports"
  for t in $("$PY" -c 'import sys; print(" ".join(f"{float(x):g}" for x in sys.argv[1:]))' $THRESHOLDS); do
    local rc=0
    "$PY" -m evals batch --refs "$DEV_ROOT" --hyps "$OUT/hyp/$t" --report "$OUT/reports/$t.json" \
      --md "$OUT/reports/$t.md" >"$OUT/reports/$t.txt" 2>&1 || rc=$?
    echo "# exit $rc" >>"$OUT/reports/$t.txt"
    say "scored threshold=$t: exit $rc"
  done
}

select_threshold() {
  "$PY" - "$OUT" "$THRESHOLDS" <<'PYEOF'
import json, sys
from pathlib import Path
out, grid = Path(sys.argv[1]), [f"{float(x):g}" for x in sys.argv[2].split()]
rows = []
for t in grid:
    r = json.loads((out / "reports" / f"{t}.json").read_text())
    m = r["macro"]
    hyp_spk = [x["speaker_count"]["hypothesis"] for x in r["meetings"]]
    rows.append({"threshold": float(t), "n": m["n"], "der": m["der.der"], "jer": m["jer.jer"],
                 "miss": m["der.miss_rate"], "false_alarm": m["der.false_alarm_rate"], "confusion": m["der.confusion_rate"],
                 "speaker_count_error": m["speaker_count.error"], "speaker_count_abs_error": m["speaker_count.abs_error"],
                 "hyp_speakers": hyp_spk, "errors": len(r["errors"]), "missing": len(r["missing"])})
bad = [x for x in rows if x["n"] != 18 or x["errors"] or x["missing"]]
if bad:
    sys.exit(f"incomplete scoring: {bad}")
best = min(rows, key=lambda x: (x["der"], x["speaker_count_abs_error"], x["threshold"]))
sel = {"schema": "plaud-harness/sherpa-calibration/1", "cluster_threshold": best["threshold"],
       "criterion": "lowest macro DER (collar 0.25) over the 18 AMI dev meetings, no speaker-count hint; "
                    "ties: smaller macro |speaker-count error|, then the lower threshold (HARNESS_POLICY)",
       "grid": [x["threshold"] for x in rows], "rows": rows}
(out / "selected.json").write_text(json.dumps(sel, indent=2) + "\n")
f = lambda v, n=4: "–" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))
L = ["# sherpa-onnx cluster_threshold on the AMI dev split", "",
     "| threshold | DER | JER | miss | false alarm | confusion | spk err | abs spk err | hyp speakers per meeting |",
     "|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
for x in rows:
    L.append(f"| {x['threshold']:.2f}{' **selected**' if x is best else ''} | {f(x['der'])} | {f(x['jer'])} | {f(x['miss'])} | "
             f"{f(x['false_alarm'])} | {f(x['confusion'])} | {f(x['speaker_count_error'], 2)} | {f(x['speaker_count_abs_error'], 2)} | "
             f"{' '.join(map(str, x['hyp_speakers']))} |")
L += ["", f"Selected: cluster_threshold = {best['threshold']} ({sel['criterion']})."]
(out / "summary.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
PYEOF
}

tiebreak() {
  "$PY" - "$OUT" "$DEV_ROOT" <<'PYEOF'
import json, sys
from pathlib import Path
from evals.io import Hypothesis, Segment, Word, load_meeting
from evals.metrics import score_meeting
from pipeline.base import DEFAULT_TIE_BREAK, TIE_BREAKS, assign_speakers
out, dev = Path(sys.argv[1]), Path(sys.argv[2])
sel = json.loads((out / "selected.json").read_text())
t = sel["cluster_threshold"]
tdir = out / "hyp" / f"{t:g}"
rows = {tb: {"cpwer": [], "der": [], "ties": 0, "words": 0, "errors": 0, "length": 0} for tb in TIE_BREAKS}
per_meeting = {}
for mdir in sorted(p for p in dev.iterdir() if (p / "meeting.json").is_file()):
    meeting = load_meeting(mdir)
    hyp = json.loads((tdir / mdir.name / "hyp.json").read_text())
    turns = [tuple(x) for x in hyp["extra"]["diarization"]["turns"]]
    asr = [{"start": s.start, "end": s.end, "text": s.text,
            "words": [{"w": w.w, "start": w.start, "end": w.end} for w in (s.words or [])]} for s in meeting.segments]
    asr.sort(key=lambda s: (s["start"], s["end"]))
    per_meeting[meeting.meeting_id] = {}
    for tb in TIE_BREAKS:
        stats = {}
        segs = assign_speakers(asr, turns, stats=stats, tie_break=tb)
        h = Hypothesis(meeting.meeting_id, f"reference-words+sherpa@{t}:{tb}",
                       [Segment(s["speaker"], s["start"], s["end"], s["text"],
                                tuple(Word(w["w"], w["start"], w["end"]) for w in s.get("words") or [])) for s in segs])
        r = score_meeting(meeting, h)
        row = rows[tb]
        row["cpwer"].append(r.cpwer.error_rate); row["der"].append(r.der.der)
        row["ties"] += stats["tie"]; row["words"] += stats["words"]
        row["errors"] += r.cpwer.errors or 0; row["length"] += r.cpwer.length or 0
        per_meeting[meeting.meeting_id][tb] = {"cpwer": r.cpwer.error_rate, "der": r.der.der, "ties": stats["tie"], "words": stats["words"]}
summary = {tb: {"macro_cpwer": sum(v["cpwer"]) / len(v["cpwer"]), "micro_cpwer": v["errors"] / v["length"],
                "macro_der": sum(v["der"]) / len(v["der"]), "tie_words": v["ties"], "words": v["words"]} for tb, v in rows.items()}
best = min(TIE_BREAKS, key=lambda tb: (round(summary[tb]["macro_cpwer"], 12), tb != DEFAULT_TIE_BREAK))
doc = {"schema": "plaud-harness/tiebreak-calibration/1", "cluster_threshold": t, "selected": best,
       "criterion": "lowest macro cpWER of the dev REFERENCE words assigned to the selected threshold's sherpa turns "
                    "(a proxy: no ASR errors); ties: the default (floor) (HARNESS_POLICY)",
       "summary": summary, "per_meeting": per_meeting}
(out / "tiebreak.json").write_text(json.dumps(doc, indent=2) + "\n")
L = [f"# Word-assignment tie-break on the AMI dev split (turns at cluster_threshold {t})", "",
     "| tie-break | macro cpWER | micro cpWER | macro DER | words decided by the tie-break | words |", "|---|---:|---:|---:|---:|---:|"]
for tb in TIE_BREAKS:
    x = summary[tb]
    L.append(f"| {tb}{' **selected**' if tb == best else ''} | {x['macro_cpwer']:.4f} | {x['micro_cpwer']:.4f} | {x['macro_der']:.4f} | {x['tie_words']} | {x['words']} |")
L += ["", f"Selected: {best} ({doc['criterion']})."]
(out / "tiebreak.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
PYEOF
}

say "output root: ${OUT#$ROOT/}; grid: $THRESHOLDS; jobs: $JOBS"
confirm() {
  "$PY" - "$OUT" "$DEV_ROOT" $CONFIRM <<'PYEOF'
import json, subprocess, sys
from pathlib import Path
out, dev, meetings = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
sel = json.loads((out / "selected.json").read_text())
t = sel["cluster_threshold"]
rows = {}
for m in meetings:
    dest = out / "confirm" / m / "hyp.json"
    if not dest.is_file():
        subprocess.run([sys.executable, "-m", "pipeline", "run", "--pipeline", "sherpa-onnx-diarization", "--audio",
                        str(dev / m / "mix.wav"), "--meeting-dir", str(dev / m), "--param", f"cluster_threshold={t}",
                        "--out", str(dest)], check=True, capture_output=True)
    real = json.loads(dest.read_text())["extra"]["diarization"]["turns"]
    replay = json.loads((out / "hyp" / f"{t:g}" / m / "hyp.json").read_text())["extra"]["diarization"]["turns"]
    rows[m] = {"identical": real == replay, "turns_real": len(real), "turns_replayed": len(replay)}
sel["confirmed_against_real_sherpa"] = rows
(out / "selected.json").write_text(json.dumps(sel, indent=2) + "\n")
print(json.dumps(rows))
if not all(r["identical"] for r in rows.values()):
    sys.exit("the replayed turns differ from real sherpa-onnx at the selected threshold")
PYEOF
}

want_stage inputs && inputs
want_stage precompute && precompute
want_stage sweep && sweep
want_stage score && score
want_stage select && select_threshold
want_stage confirm && confirm
want_stage tiebreak && tiebreak
