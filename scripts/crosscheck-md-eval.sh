#!/usr/bin/env bash
# Cross-check the harness's DER (pyannote.metrics, evals/metrics.py) against NIST
# md-eval-22.pl on the hypotheses of a V5 run (review finding EV-4).
#
#   ./scripts/crosscheck-md-eval.sh [V5_OUT] [REFS_ROOT]
#       V5_OUT     default build/v5            (reads hyp/ami/<system>/<meeting>/hyp.rttm and
#                                               reports/ami/<system>[.collar0].json)
#       REFS_ROOT  default data/corpora/ami/meetings
#
# md-eval-22.pl is fetched from nryant/dscore at a pinned commit and sha256-checked into
# data/tools/ (git-ignored); it is never committed.  The harness's DER collar is the TOTAL
# width centred on each reference boundary (evals: 0.25 = +-0.125 s), md-eval's -c is per
# side, so the default report is compared with `-c 0.125` and the collar0 report with
# `-c 0`.  Overlapped speech is scored by both (md-eval's default; evals skip_overlap=False).
# Writes <V5_OUT>/md-eval-crosscheck.json and prints how many comparisons differ by more
# than 1e-4 (md-eval prints DER in percent to 2 decimals); exit 1 only when nothing could be
# compared or md-eval produced no score.  On the 25 Sep V5 subset (28 Sep): 62 of 64 agree;
# the two that differ are the heavily over-clustered un-hinted sherpa turns at collar 0.25
# (EN2002a 0.0028, TS3003a 0.0003), where the speaker mappings can diverge.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build/v5}"; REFS="${2:-$ROOT/data/corpora/ami/meetings}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
DSCORE_COMMIT=e02f949ac6592279300a2c33d03daf9e0c12fd27
MDEVAL_SHA=872aa955cbc3d57e6d4fd6fe2e699791739c1cb716cc89105bb4717415b9400d
TOOL="$ROOT/data/tools/md-eval-22.pl"
if [[ ! -f "$TOOL" ]] || [[ "$(shasum -a 256 "$TOOL" | cut -d' ' -f1)" != "$MDEVAL_SHA" ]]; then
  mkdir -p "$(dirname "$TOOL")"
  curl -fsSL -o "$TOOL.part" "https://raw.githubusercontent.com/nryant/dscore/$DSCORE_COMMIT/scorelib/md-eval-22.pl"
  [[ "$(shasum -a 256 "$TOOL.part" | cut -d' ' -f1)" == "$MDEVAL_SHA" ]] || { echo "md-eval-22.pl sha256 mismatch" >&2; exit 3; }
  mv "$TOOL.part" "$TOOL"
fi
command -v perl >/dev/null || { echo "perl is required" >&2; exit 3; }
"$PY" - "$OUT" "$REFS" "$TOOL" <<'PYEOF'
import json, re, subprocess, sys, tempfile
from pathlib import Path
out, refs, tool = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
rows, tmp = [], Path(tempfile.mkdtemp())
for rep in sorted((out / "reports" / "ami").glob("*.json")):
    name = rep.name[:-5]
    sysname, _, variant = name.partition(".")
    if variant not in ("", "collar0") or not (out / "hyp" / "ami" / sysname).is_dir():
        continue
    side = 0.125 if variant == "" else 0.0
    for m in json.loads(rep.read_text())["meetings"]:
        mid, der = m["meeting_id"], m["der"]["der"]
        hyp = out / "hyp" / "ami" / sysname / mid / "hyp.rttm"
        if der is None or not hyp.is_file():
            continue
        meeting = json.loads((refs / mid / "meeting.json").read_text())
        uem = tmp / f"{mid}.uem"
        uem.write_text(f"{mid} 1 0.000 {meeting['duration_s']:.3f}\n")
        res = subprocess.run(["perl", tool, "-r", str(refs / mid / "ref.rttm"), "-s", str(hyp), "-u", str(uem),
                              "-c", str(side)], capture_output=True, text=True)
        mm = re.search(r"OVERALL SPEAKER DIARIZATION ERROR = ([\d.]+) percent", res.stdout)
        md = float(mm.group(1)) / 100 if mm else None
        rows.append({"system": sysname, "collar_total_s": 2 * side, "meeting": mid, "evals_der": der, "md_eval_der": md,
                     "abs_diff": None if md is None else round(abs(md - der), 6)})
bad = [r for r in rows if r["md_eval_der"] is None or r["abs_diff"] > 1e-4]
doc = {"schema": "plaud-harness/md-eval-crosscheck/1", "tool": "md-eval-22.pl (nryant/dscore@e02f949)",
       "note": "md-eval prints DER in percent with 2 decimals, so agreement is to 1e-4",
       "comparisons": len(rows), "differ": len(bad), "max_abs_diff": max((r["abs_diff"] or 0) for r in rows) if rows else None,
       "rows": rows}
(out / "md-eval-crosscheck.json").write_text(json.dumps(doc, indent=1) + "\n")
print(f"md-eval cross-check: {len(rows)} comparisons, {len(bad)} differ by > 1e-4, max |diff| {doc['max_abs_diff']}")
for r in bad:
    print(f"  differs: {r['system']} {r['meeting']} collar {r['collar_total_s']}: evals {r['evals_der']:.4f}, md-eval {r['md_eval_der']}")
sys.exit(1 if not rows or any(r["md_eval_der"] is None for r in rows) else 0)
PYEOF
