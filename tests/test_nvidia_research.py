"""research/nvidia scripts on small synthetic inputs (docs/nvidia-speech.md)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "research/nvidia" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hyp(mid: str, system: str, segments: list, extra: dict) -> dict:
    return {"schema": "plaud-harness/hypothesis/1", "meeting_id": mid, "system": system, "segments": segments, "extra": extra}


def test_recombine_puts_one_systems_words_on_another_systems_turns(tmp_path):
    rc = _load("recombine")
    from pipeline.adapters import ASR_CACHE_SCHEMA

    cache = tmp_path / "cache"
    cache.mkdir()
    words = [{"start": 0.0, "end": 2.0, "text": "hello there friend",
              "words": [{"w": "hello", "start": 0.0, "end": 0.5}, {"w": "there", "start": 0.6, "end": 1.0},
                        {"w": "friend", "start": 1.5, "end": 2.0}]}]
    (cache / "k1.json").write_text(json.dumps({"schema": ASR_CACHE_SCHEMA, "key": "k1", "segments": words}))
    wh = tmp_path / "w/m1/hyp.json"
    wh.parent.mkdir(parents=True)
    wh.write_text(json.dumps(_hyp("m1", "asr-sys", [{"speaker": "x", "start": 0, "end": 2, "text": "hello there friend"}],
                                  {"asr": {"cache": {"key": "k1"}}, "components": {"transcriber": "T"}})))
    th = tmp_path / "t/m1/hyp.json"
    th.parent.mkdir(parents=True)
    th.write_text(json.dumps(_hyp("m1", "diar-sys", [], {"diarization": {"turns": [[0.0, 1.1, "A"], [1.2, 2.5, "B"]]},
                                                         "components": {"diarizer": "D"}})))
    h = rc.recombine(wh, cache, th, "asr+diar", "floor")
    assert [(s["speaker"], s["text"]) for s in h.segments] == [("A", "hello there"), ("B", "friend")]
    assert h.extra["recombined"]["words"]["asr_cache_key"] == "k1"
    assert h.extra["components"] == {"transcriber": "T", "diarizer": "D"}
    wh.write_text(json.dumps(_hyp("m1", "asr-sys", [], {})))
    with pytest.raises(SystemExit):
        rc.recombine(wh, cache, th, "asr+diar", "floor")  # no cache key recorded


def test_forced_alignment_labels_map_to_harness_speakers(tmp_path):
    sp = _load("score_nvidia_protocol")
    md = tmp_path / "ES0000a"
    md.mkdir()
    (md / "meeting.json").write_text(json.dumps({"meeting_id": "ES0000a", "speakers": [
        {"id": "FEE001", "nxt_agent": "A"}, {"id": "MEE002", "nxt_agent": "B"}]}))
    fa = tmp_path / "fa"
    fa.mkdir()
    (fa / "ES0000a.rttm").write_text("SPEAKER ES0000a 1 0.50 1.00 <NA> <NA> ES0000a.B <NA> <NA>\n"
                                     "SPEAKER ES0000a 1 2.00 0.25 <NA> <NA> ES0000a.A <NA> <NA>\n")
    mid, turns = sp.fa_reference(fa, md)
    assert mid == "ES0000a" and turns == [(0.5, 1.5, "MEE002"), (2.0, 2.25, "FEE001")]


def test_notsofar_reference_segments_from_word_timing():
    pn = _load("prepare_notsofar")
    gt = [{"speaker_id": "Ron", "word_timing": [["we", 1.0, 1.2], ["Should", 1.2, 1.5]], "text": "We should <ST/>"},
          {"speaker_id": "Sarah", "word_timing": [], "text": "<ST/>"},
          {"speaker_id": "Sarah", "word_timing": [["late", 9.5, 11.0]], "text": "late"}]
    segs, speakers, counts = pn.reference_segments(gt, duration=10.0)
    assert speakers == ["Ron", "Sarah"] and counts == {"gt_segments": 3, "segments_without_timed_words": 1}
    assert segs[0] == {"speaker": "Ron", "start": 1.0, "end": 1.5, "text": "we should",
                       "words": [{"w": "we", "start": 1.0, "end": 1.2}, {"w": "should", "start": 1.2, "end": 1.5}]}
    assert segs[1]["end"] == 10.0  # clipped to the audio


def test_device_tagged_transcript_splits_on_speaker_and_pause():
    dh = _load("device_to_hyp")
    doc = {"engine": "nemo-asr+tags", "timing": {}, "memory": {}, "words": [
        {"w": "Hi,", "start": 0.0, "end": 0.2, "speaker": 1}, {"w": "all", "start": 0.3, "end": 0.5, "speaker": 1},
        {"w": "yes", "start": 0.6, "end": 0.8, "speaker": 2}, {"w": "later", "start": 2.0, "end": 2.2, "speaker": 2}]}
    h = dh.tagged("m", "phone", doc, 5.0)
    assert [(s["speaker"], s["text"]) for s in h.segments] == [("spk1", "hi all"), ("spk2", "yes"), ("spk2", "later")]


def test_evaluate_derives_turns_views(tmp_path):
    ev = _load("evaluate")
    src = tmp_path / "sys"
    (src / "m1").mkdir(parents=True)
    (src / "m1/hyp.json").write_text(json.dumps(_hyp("m1", "sys", [], {"diarization": {"turns": [[1.0, 2.0, "B"], [0.0, 1.0, "A"], [3.0, 3.0, "C"]]}})))
    assert ev.derive_turns(src, tmp_path / "sys-turns")
    d = json.loads((tmp_path / "sys-turns/m1/hyp.json").read_text())
    assert [(s["speaker"], s["start"]) for s in d["segments"]] == [("A", 0.0), ("B", 1.0)]  # sorted, empty turn dropped
    (src / "m1/hyp.json").write_text(json.dumps(_hyp("m1", "sys", [], {})))
    assert ev.derive_turns(src, tmp_path / "x") is False
