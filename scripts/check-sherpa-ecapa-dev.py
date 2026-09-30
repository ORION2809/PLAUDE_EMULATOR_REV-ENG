#!/usr/bin/env python3
"""Dev-split evidence for whisper-sherpa-ecapa (docs/pipeline.md §11.10).

Question: on the 18 AMI dev meetings, does clustering the ECAPA embeddings of
``embedding-cluster`` into the number of speakers that calibrated sherpa-onnx
finds (``cluster_threshold`` 1.15) beat either part alone?  No model runs:

* the ECAPA embeddings are the dev cache of scripts/calibrate-embedding-cluster.py
  (build/v5-calib/embedding-cluster/pre/, not published; regenerate with that
  script), and the replay of the clustering is the one it validated against real
  runs;
* sherpa's speaker count per meeting is read from its 1.15 turns
  (build/v5-calib/hyp/1.15/, from scripts/calibrate-sherpa.sh).

Two measures, both without a speaker-count hint:

1. DER/JER of the turns (``python -m evals batch``, collar 0.25);
2. a cpWER proxy: the dev REFERENCE words assigned to the turns with every
   tie-break, exactly as the tie-break stage of calibrate-sherpa.sh does for
   sherpa's turns (no ASR errors, so it isolates attribution).

The same measures for ECAPA alone (build/v5-calib/embedding-cluster/hyp/0.55/)
and sherpa alone (build/v5-calib/hyp/1.15/) are computed alongside, so the
comparison is like for like.  Writes build/v5-calib/whisper-sherpa-ecapa/.
The test split is never read.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.io import Hypothesis, Segment, Word, load_meeting  # noqa: E402
from evals.metrics import score_meeting  # noqa: E402
from pipeline.adapters import EmbeddingClusterDiarizer  # noqa: E402
from pipeline.base import DEFAULT_TIE_BREAK, TIE_BREAKS, assign_speakers  # noqa: E402

DEV = ROOT / "data/corpora/ami/dev"
CALIB = ROOT / "build/v5-calib"
OUT = CALIB / "whisper-sherpa-ecapa"
SHERPA = CALIB / "hyp" / "1.15"
ECAPA_PRE = CALIB / "embedding-cluster" / "pre"
ECAPA_CAL = CALIB / "embedding-cluster" / "hyp" / "0.55"

_spec = importlib.util.spec_from_file_location("calibrate_embedding_cluster", ROOT / "scripts/calibrate-embedding-cluster.py")
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)


def hybrid_hypotheses() -> dict[str, dict]:
    """ECAPA turns cut into sherpa's count; returns per-meeting counts."""
    _, c = EmbeddingClusterDiarizer.params_from({})
    counts = {}
    for f in sorted(ECAPA_PRE.glob("*.npz")):
        mid = f.stem
        z = np.load(f)
        cells = [tuple(map(float, r)) for r in z["cells"]]
        k = len({s["speaker"] for s in json.loads((SHERPA / mid / "hyp.json").read_text())["segments"]})
        turns = cal.turns_for(cells, z["emb"], c, cal.EMBEDDING_DISTANCE_THRESHOLD, k)
        d = OUT / "hyp" / mid
        d.mkdir(parents=True, exist_ok=True)
        doc = cal.hyp_doc(mid, turns, cal.EMBEDDING_DISTANCE_THRESHOLD, k,
                          "scripts/check-sherpa-ecapa-dev.py: ECAPA clustered into calibrated sherpa's speaker count")
        doc["system"] = "sherpa-count+embedding-cluster:replayed"
        (d / "hyp.json").write_text(json.dumps(doc) + "\n")
        ref = len({s["speaker"] for s in json.loads((DEV / mid / "meeting.json").read_text())["segments"]})
        counts[mid] = {"sherpa_count": k, "reference_speakers": ref, "hypothesis_speakers": len({t[2] for t in turns})}
    return counts


def der(name: str, hyps: Path) -> dict:
    rep = OUT / "reports" / f"{name}.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.unlink(missing_ok=True)
    p = subprocess.run([sys.executable, "-m", "evals", "batch", "--refs", str(DEV), "--hyps", str(hyps), "--report", str(rep),
                        "--quiet"], cwd=ROOT, capture_output=True, text=True)
    r = json.loads(rep.read_text()) if rep.is_file() else None
    if p.returncode != 0 or r is None or r["errors"] or r["missing"] or r["macro"].get("n") != 18:
        raise SystemExit(f"{name}: evals exit {p.returncode}, report incomplete\n{p.stderr[-2000:]}")
    m = r["macro"]
    return {k: m[k] for k in ("der.der", "jer.jer", "der.confusion_rate", "der.miss_rate", "speaker_count.error",
                              "speaker_count.abs_error")}


def word_proxy(hyps: Path) -> dict:
    out = {}
    for tb in TIE_BREAKS:
        rates, errors, length = [], 0, 0
        for mdir in sorted(p for p in DEV.iterdir() if (p / "meeting.json").is_file()):
            meeting = load_meeting(mdir)
            turns = [tuple(x) for x in json.loads((hyps / mdir.name / "hyp.json").read_text())["extra"]["diarization"]["turns"]]
            asr = sorted(({"start": s.start, "end": s.end, "text": s.text,
                           "words": [{"w": w.w, "start": w.start, "end": w.end} for w in (s.words or [])]}
                          for s in meeting.segments), key=lambda s: (s["start"], s["end"]))
            segs = assign_speakers(asr, turns, stats={}, tie_break=tb)
            h = Hypothesis(meeting.meeting_id, f"reference-words:{tb}",
                           [Segment(s["speaker"], s["start"], s["end"], s["text"],
                                    tuple(Word(w["w"], w["start"], w["end"]) for w in s.get("words") or [])) for s in segs])
            r = score_meeting(meeting, h)
            rates.append(r.cpwer.error_rate)
            errors += r.cpwer.errors or 0
            length += r.cpwer.length or 0
        out[tb] = {"macro_cpwer": sum(rates) / len(rates), "micro_cpwer": errors / length}
    return out


def main() -> int:
    counts = hybrid_hypotheses()
    systems = {"sherpa-count+ecapa": OUT / "hyp", "ecapa-alone-0.55": ECAPA_CAL, "sherpa-alone-1.15": SHERPA}
    rows = {name: {"der": der(name, path), "reference_words_cpwer": word_proxy(path)} for name, path in systems.items()}
    best_tb = min(TIE_BREAKS, key=lambda tb: (round(rows["sherpa-count+ecapa"]["reference_words_cpwer"][tb]["macro_cpwer"], 4),
                                             tb != DEFAULT_TIE_BREAK))
    doc = {"schema": "plaud-harness/sherpa-ecapa-dev-check/1", "split": "AMI dev (18 meetings), no speaker-count hint",
           "systems": rows, "counts": counts, "tie_break": best_tb,
           "tie_break_rule": "lowest macro cpWER of the reference-word proxy at 4 decimals; ties: the default (floor)"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dev-check.json").write_text(json.dumps(doc, indent=2) + "\n")
    L = ["# whisper-sherpa-ecapa on the AMI dev split (no hint)", "",
         "| turns | macro DER | JER | confusion | miss | speaker-count error | reference-word cpWER (floor / latest_start) |",
         "|---|---:|---:|---:|---:|---:|---|"]
    for name, r in rows.items():
        d, w = r["der"], r["reference_words_cpwer"]
        L.append(f"| {name} | {d['der.der']:.4f} | {d['jer.jer']:.4f} | {d['der.confusion_rate']:.4f} | {d['der.miss_rate']:.4f} | "
                 f"{d['speaker_count.error']:+.2f} | {w['floor']['macro_cpwer']:.4f} / {w['latest_start']['macro_cpwer']:.4f} |")
    L += ["", f"Tie-break for sherpa-count+ecapa: {best_tb} ({doc['tie_break_rule']}).", ""]
    (OUT / "dev-check.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
