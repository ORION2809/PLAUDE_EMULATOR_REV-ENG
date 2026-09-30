#!/usr/bin/env python3
"""Turn on-device speechbench results into harness hypotheses and score them.

research/nvidia/android/bench.sh leaves one JSON per (clip, run) in
build/nvidia/device/<model>-<serial>/.  For the clips that are whole harness
meetings (AMI IS1009a, NOTSOFAR MTG_32045 far-field) this builds the
speaker-attributed hypotheses the phone produced:

* ``phone-nemo35-tags``      Nemotron 3.5 words with the runtime's own speaker tags
* ``phone-nemo35+nemo-diar`` Nemotron 3.5 words + Nemotron 3 Diarization turns (harness rule)
* ``phone-nemo-en-rc13+nemo-diar``  the English model at the 1.12 s right context + the same turns
* ``phone-whisper+sherpa``   whisper.cpp small.en words + sherpa-onnx turns (harness rule):
                              the current stack's on-device form

and scores each against the meeting's reference (``python -m evals score``), so the
phone's accuracy can be set beside the Mac runs of the same models.

Usage: device_to_hyp.py build/nvidia/device/<model>-<serial>
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.base import Hypothesis, assign_speakers, assignment_rule, make_segment, sort_segments  # noqa: E402
from pipeline.formats import write_hypothesis_files  # noqa: E402
from pipeline.nemo_speech import words_to_segments  # noqa: E402

CLIPS = {"ami-IS1009a": ROOT / "data/corpora/ami/test/IS1009a",
         "nsf-sc-MTG_32045": ROOT / "data/corpora/notsofar/sc/MTG_32045"}


def load(p: Path) -> dict | None:
    return json.loads(p.read_text()) if p.is_file() and p.stat().st_size else None


def cli_words(doc: dict) -> list[dict]:
    return [{"word": w["w"], "start": w["start"], "end": w["end"], **({"speaker": w["speaker"]} if "speaker" in w else {})}
            for w in doc["words"]]


def turns_of(doc: dict) -> list[tuple[float, float, str]]:
    return [(float(t["start"]), float(t["end"]), f"spk{t['speaker']}") for t in doc["turns"]]


def composed(mid: str, system: str, words_doc: dict, turns_doc: dict, duration: float) -> Hypothesis:
    asr = words_to_segments(cli_words(words_doc), 0.5, duration)
    stats: dict = {}
    segs = assign_speakers(asr, turns_of(turns_doc), stats=stats)
    return Hypothesis(meeting_id=mid, system=system, segments=segs,
                      extra={"device_runs": [words_doc["engine"], turns_doc["engine"]],
                             "timing": {"words": words_doc["timing"], "turns": turns_doc["timing"]},
                             "memory": {"words": words_doc["memory"], "turns": turns_doc["memory"]},
                             "assignment": {"rule": assignment_rule(), **stats},
                             "diarization": {"turns": [list(t) for t in turns_of(turns_doc)]}}).validate()


def tagged(mid: str, system: str, doc: dict, duration: float) -> Hypothesis:
    toks = []
    for w in cli_words(doc):
        for s in words_to_segments([w], 0.5, duration):
            toks += [dict(t, speaker=f"spk{w.get('speaker', 0)}") for t in s["words"]]
    segs, run = [], []
    for t in toks:
        if run and (t["speaker"] != run[-1]["speaker"] or t["start"] - run[-1]["end"] > 0.5):
            segs.append(run)
            run = []
        run.append(t)
    if run:
        segs.append(run)
    out = [make_segment(r[0]["speaker"], r[0]["start"], max(max(w["end"] for w in r), r[0]["start"] + 0.01),
                        " ".join(w["w"] for w in r), [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in r]) for r in segs]
    return Hypothesis(meeting_id=mid, system=system, segments=sort_segments(out),
                      extra={"device_runs": [doc["engine"]], "timing": doc["timing"], "memory": doc["memory"],
                             "assignment": {"rule": "NeMo-Speech.cpp word speaker tags"}}).validate()


def main() -> int:
    dev = Path(sys.argv[1])
    out_root = dev / "hyp"
    table = []
    for clip, mdir in CLIPS.items():
        meeting = json.loads((mdir / "meeting.json").read_text())
        mid, dur = meeting["meeting_id"], float(meeting["duration_s"])
        r = {k: load(dev / f"{clip}.{k}.json") for k in ("sb-nemo35-tags", "sb-nemo35", "sb-nemo-en-rc13", "sb-nemo-diar",
                                                         "sb-whisper", "sb-sherpa-diar", "sb-nemo35-rc13", "sb-nemo-en-rc13-tags")}
        hyps = {}
        if r["sb-nemo35-tags"]:
            hyps["phone-nemo35-tags"] = tagged(mid, "phone-nemo35-tags", r["sb-nemo35-tags"], dur)
        if r["sb-nemo-en-rc13-tags"]:
            hyps["phone-nemo-en-rc13-tags"] = tagged(mid, "phone-nemo-en-rc13-tags", r["sb-nemo-en-rc13-tags"], dur)
        for w, name in (("sb-nemo35", "phone-nemo35+nemo-diar"), ("sb-nemo-en-rc13", "phone-nemo-en-rc13+nemo-diar"),
                        ("sb-nemo35-rc13", "phone-nemo35-rc13+nemo-diar")):
            if r[w] and r["sb-nemo-diar"]:
                hyps[name] = composed(mid, name, r[w], r["sb-nemo-diar"], dur)
        if r["sb-whisper"] and r["sb-sherpa-diar"]:
            hyps["phone-whisper+sherpa"] = composed(mid, "phone-whisper+sherpa", r["sb-whisper"], r["sb-sherpa-diar"], dur)
        for name, h in hyps.items():
            path = out_root / clip / name / mid / "hyp.json"
            write_hypothesis_files(h, path)
            rep = path.parent / "report.json"
            subprocess.run([sys.executable, "-m", "evals", "score", "--ref", str(mdir), "--hyp", str(path), "--report", str(rep),
                            "--quiet"], cwd=ROOT, capture_output=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
            m = json.loads(rep.read_text())
            flat = m.get("report") or m
            def g(*keys):
                d = flat
                for k in keys:
                    d = (d or {}).get(k) if isinstance(d, dict) else None
                return d
            table.append({"clip": clip, "system": name, "cpwer": g("cpwer", "error_rate"), "tcpwer": g("tcpwer", "error_rate"),
                          "wer": g("wer_concat", "wer"), "der": g("der", "der")})
    (dev / "scores.json").write_text(json.dumps(table, indent=2) + "\n")
    for t in table:
        print(f"{t['clip']:20s} {t['system']:32s} cpWER {t['cpwer']}  tcpWER {t['tcpwer']}  WER {t['wer']}  DER {t['der']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
