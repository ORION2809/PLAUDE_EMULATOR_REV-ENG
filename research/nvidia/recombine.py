#!/usr/bin/env python3
"""Cross every transcript with every diarizer's turns, without running a model.

A composed system here is "ASR words + diarization turns + the harness rule
that gives each word the speaker with the largest overlap"
(``pipeline.base.assign_speakers``, deterministic).  Each hypothesis records
the transcript it used (``extra.asr.cache.key`` into an ASR cache in the
``plaud-harness/asr-cache/1`` schema) and the turns (``extra.diarization.turns``),
so the word source of one system can be combined with the turn source of
another exactly as the pipeline would have combined them.  This produces the
ASR x diarizer matrix of docs/nvidia-speech.md (e.g. Nemotron words on
pyannote turns, Whisper words on Nemotron turns) from saved runs.

Usage: recombine.py --words <hyp root>/<system> --asr-cache DIR
                    --turns <hyp root>/<system> --out <hyp root>/<new system>
                    [--tie-break floor] [--system NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.adapters import ASR_CACHE_SCHEMA  # noqa: E402
from pipeline.base import Hypothesis, assign_speakers, assignment_rule, tie_break_param  # noqa: E402
from pipeline.formats import write_hypothesis_files  # noqa: E402


def recombine(words_hyp: Path, asr_cache: Path, turns_hyp: Path, system: str, tie_break: str) -> Hypothesis:
    w = json.loads(words_hyp.read_text())
    t = json.loads(turns_hyp.read_text())
    if w["meeting_id"] != t["meeting_id"]:
        raise SystemExit(f"meeting mismatch: {words_hyp} vs {turns_hyp}")
    key = (((w.get("extra") or {}).get("asr") or {}).get("cache") or {}).get("key")
    if not key:
        raise SystemExit(f"{words_hyp}: no extra.asr.cache.key (run with --param asr_cache)")
    entry = json.loads((asr_cache / f"{key}.json").read_text())
    if entry.get("schema") != ASR_CACHE_SCHEMA or entry.get("key") != key:
        raise SystemExit(f"{asr_cache}/{key}.json does not match")
    turns = [(float(a), float(b), str(s)) for a, b, s in ((t.get("extra") or {}).get("diarization") or {}).get("turns") or []]
    if not turns and not t.get("segments"):
        raise SystemExit(f"{turns_hyp}: no extra.diarization.turns")
    tb = tie_break_param(tie_break)
    stats: dict = {}
    segments = assign_speakers(entry["segments"], turns, stats=stats, tie_break=tb)
    stats["tie_break"] = tb
    we, te = w.get("extra") or {}, t.get("extra") or {}
    extra = {
        "recombined": {
            "words": {"system": w.get("system"), "path": str(words_hyp), "sha256": hashlib.sha256(words_hyp.read_bytes()).hexdigest(),
                      "asr_cache_key": key, "transcriber": (we.get("components") or {}).get("transcriber")},
            "turns": {"system": t.get("system"), "path": str(turns_hyp), "sha256": hashlib.sha256(turns_hyp.read_bytes()).hexdigest(),
                      "diarizer": (te.get("components") or {}).get("diarizer")},
            "tool": "research/nvidia/recombine.py (no model run)"},
        "components": {"transcriber": (we.get("components") or {}).get("transcriber"),
                       "diarizer": (te.get("components") or {}).get("diarizer")},
        "models": {"asr": (we.get("models") or {}).get("asr"),
                   **{k: v for k, v in (te.get("models") or {}).items() if k != "asr"}},
        "asr": we.get("asr"),
        "diarization": {**(te.get("diarization") or {}), "turns": [[a, b, s] for a, b, s in turns]},
        "assignment": {"rule": assignment_rule(tb), **stats},
        "timing": {"asr_s": (we.get("timing") or {}).get("asr_s"), "diarization_s": (te.get("timing") or {}).get("diarization_s"),
                   "audio_s": (we.get("timing") or {}).get("audio_s")},
    }
    return Hypothesis(meeting_id=w["meeting_id"], system=system, segments=segments, extra=extra).validate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--words", required=True, help="<hyp root>/<system> whose transcript is used")
    ap.add_argument("--asr-cache", required=True)
    ap.add_argument("--turns", required=True, help="<hyp root>/<system> whose extra.diarization.turns are used")
    ap.add_argument("--out", required=True, help="<hyp root>/<new system>")
    ap.add_argument("--tie-break", default="floor")
    ap.add_argument("--system", default=None)
    args = ap.parse_args()
    words, turns, out = Path(args.words), Path(args.turns), Path(args.out)
    system = args.system or out.name
    n = 0
    for wh in sorted(words.glob("*/hyp.json")):
        th = turns / wh.parent.name / "hyp.json"
        if not th.is_file():
            raise SystemExit(f"no turns for {wh.parent.name} in {turns}")
        hyp = recombine(wh, Path(args.asr_cache), th, system, args.tie_break)
        write_hypothesis_files(hyp, out / wh.parent.name / "hyp.json")
        n += 1
    print(f"[recombine] {system}: {n} meetings -> {out}")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
