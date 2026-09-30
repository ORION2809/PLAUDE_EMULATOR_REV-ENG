#!/usr/bin/env python3
"""mivi-speech (Apple engine) results -> harness hypotheses (docs/nvidia-speech.md).

For each ``<results>/<meeting_id>.json`` written by ``mivi-speech transcribe --out``:

* ``<out>/<system>/<meeting_id>/hyp.json``: the product's speaker-attributed transcript
  (words already carry the speaker the Swift engine assigned with the harness rule),
  one segment per run of one speaker's words split at pauses > 0.5 s;
* ``<out>/<system>-turns/<meeting_id>/hyp.json``: the diarizer's turns alone (DER/JER).

It also checks that the Swift assignment equals ``pipeline.base.assign_speakers`` on the
same words and turns (the rule must not drift between the two languages) and fails when
they disagree.

Usage: to_hyp.py <results dir> <out hyp root> <system name>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline.base import Hypothesis, SpeakerIndex, make_segment, normalize_text, sort_segments  # noqa: E402
from pipeline.formats import write_hypothesis_files  # noqa: E402


def segments_from_words(words: list[dict], gap: float, duration: float) -> list:
    toks = []
    for w in words:
        parts = normalize_text(str(w["w"])).split()
        a = min(max(0.0, float(w["start"])), duration)
        b = min(max(a, float(w["end"])), duration)
        step = (b - a) / len(parts) if parts else 0.0
        for i, t in enumerate(parts):
            toks.append({"w": t, "start": a + i * step, "end": a + (i + 1) * step if i + 1 < len(parts) else b,
                         "speaker": f"spk{int(w.get('speaker', 0))}"})
    segs, run = [], []
    for t in toks:
        if run and (t["speaker"] != run[-1]["speaker"] or t["start"] - run[-1]["end"] > gap):
            segs.append(run)
            run = []
        run.append(t)
    if run:
        segs.append(run)
    return sort_segments([make_segment(r[0]["speaker"], r[0]["start"], max(max(x["end"] for x in r), r[0]["start"] + 0.01),
                                       " ".join(x["w"] for x in r), [{"w": x["w"], "start": x["start"], "end": x["end"]} for x in r])
                          for r in segs])


def main() -> int:
    results, out, system = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    mismatches, n = 0, 0
    for f in sorted(results.glob("*.json")):
        mid = f.stem
        doc = json.loads(f.read_text())
        dur = float(doc["timing"]["audio_s"])
        words, turns = doc["words"], doc["turns"]
        index = SpeakerIndex([(t["start"], t["end"], str(t["speaker"])) for t in turns], fallback="0")
        for w in words:
            spk, _ = index.speaker_for(float(w["start"]), float(w["end"]))
            if str(w.get("speaker", 0)) != spk:
                mismatches += 1
        extra = {"engine": doc["engine"], "settings": doc["settings"], "timing": doc["timing"], "memory": doc["memory"],
                 "source": str(f), "diarization": {"turns": [[t["start"], t["end"], f"spk{t['speaker']}"] for t in turns]}}
        h = Hypothesis(meeting_id=mid, system=system, segments=segments_from_words(words, 0.5, dur), extra=extra).validate()
        write_hypothesis_files(h, out / system / mid / "hyp.json")
        tsegs = sort_segments([make_segment(f"spk{t['speaker']}", t["start"], t["end"], "") for t in turns if t["end"] > t["start"]])
        write_hypothesis_files(Hypothesis(meeting_id=mid, system=f"{system}-turns", segments=tsegs,
                                          extra={"source": str(f), "note": "diarizer turns only (DER/JER)"}).validate(),
                               out / f"{system}-turns" / mid / "hyp.json")
        n += 1
    print(f"[to_hyp] {system}: {n} meetings; Swift vs Python assignment mismatches: {mismatches}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
