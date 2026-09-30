"""Model-backed pipeline: faster-whisper + sherpa-onnx (``whisper-sherpa``).

Two kinds of test live here.

* Model-free (run everywhere, CI included): the word-to-speaker assignment
  rule (``base.SpeakerIndex`` / ``assign_speakers``), the output
  normalisers (``whisper_words``, ``sherpa_turns``), the sherpa-onnx and
  faster-whisper adapters' plumbing against stand-in modules shaped after
  the installed releases' APIs, the model manifest and its sha256-checked
  fetcher (with an in-memory opener, no network), and the registry/CLI
  wiring.  Every hypothesis they build is re-parsed by the evals layer's own
  contract parser.
* Real-model (``test_real_*``): the installed faster-whisper 1.2.1 and
  sherpa-onnx 1.13.8 on a 20 s excerpt of AMI ES2004a (Mix-Headset,
  CC BY 4.0), scored against the AMI manual word annotations.  They need the
  pinned weights (``python -m pipeline fetch-models``) and the local AMI
  files.  When the PACKAGES are absent (CI) the test checks the registry
  says so instead of skipping (tests/conftest.py strict mode); when the
  packages are present but the weights or AMI files are not, they skip with
  one of the reasons in SKIP_* below.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import types
import warnings
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.io import hypothesis_from_dict  # noqa: E402
from pipeline import PipelineConfig, PipelineUnavailable, get_pipeline, registered  # noqa: E402
from pipeline import model_store  # noqa: E402
from pipeline.adapters import FasterWhisperTranscriber, resolve_fw_model, whisper_words  # noqa: E402
from pipeline.base import ASSIGNMENT_RULE, MIN_SEGMENT_S, Hypothesis, ParamError, SpeakerIndex, assign_speakers  # noqa: E402
from pipeline.formats import write_hypothesis_files  # noqa: E402
from pipeline.whisper_sherpa import (  # noqa: E402
    EMBEDDING_ASSET,
    SEGMENTATION_ASSET,
    ModelComposedPipeline,
    SherpaOnnxDiarizer,
    sherpa_turns,
)

SR = 16000

#: Skip-reason prefixes used here (documented gates, tests/conftest.py).
SKIP_WEIGHTS = "model weights not fetched: "  # from model_store.missing_reason
SKIP_AMI = "AMI data not present: "

AMI_RAW = ROOT / "data" / "corpora" / "ami" / "raw"
AMI_ZIP = AMI_RAW / "ami_public_manual_1.6.2.zip"
AMI_WAV = AMI_RAW / "audio" / "ES2004a.Mix-Headset.wav"
#: HARNESS_POLICY: the real-model excerpt.  ES2004a 780-800 s: two talkers
#: (AMI D then A, 57 + 27 reference words, 1 word of C).
CLIP_MEETING, CLIP_T0, CLIP_T1 = "ES2004a", 780.0, 800.0


def _hyp_roundtrip(hyp: Hypothesis) -> None:
    """The evals layer's own parser must accept the hypothesis."""
    hypothesis_from_dict(json.loads(json.dumps(hyp.to_dict())))


# --- word-to-speaker assignment (no models) ------------------------------------------------------


def test_word_takes_the_speaker_with_the_largest_total_overlap():
    # A's two turns overlap the word 0.2 + 0.3 = 0.5 s; B's single turn 0.35 s.
    # A per-TURN maximum would pick B; the rule sums per speaker.
    turns = [(0.5, 1.2, "A"), (1.3, 2.0, "A"), (1.1, 1.45, "B")]
    idx = SpeakerIndex(turns)
    assert idx.speaker_for(1.0, 1.6) == ("A", "overlap")
    assert idx.speaker_for(1.12, 1.44) == ("B", "overlap")  # B 0.32 vs A 0.08 + 0.14


def test_a_tie_goes_to_the_speaker_whose_overlapping_turn_started_first():
    turns = [(2.0, 3.0, "late"), (0.0, 2.0, "early")]  # unsorted on purpose
    assert SpeakerIndex(turns).speaker_for(1.5, 2.5) == ("early", "tie")
    # A word wholly inside two overlapping turns: Y holds the floor (started at 5 s),
    # although X appeared first in the meeting.
    idx = SpeakerIndex([(0.0, 1.0, "X"), (5.0, 10.0, "Y"), (6.0, 9.0, "X")])
    assert idx.speaker_for(7.0, 8.0) == ("Y", "tie")


def test_each_tie_break_rule_and_the_default():
    """assignment_tie_break (docs/pipeline.md §11.8): floor (default),
    latest_start, first_seen, previous_word."""
    from pipeline.base import DEFAULT_TIE_BREAK, TIE_BREAKS, tie_break_param

    turns = [(0.0, 1.0, "X"), (5.0, 10.0, "Y"), (6.0, 9.0, "X"), (6.5, 8.5, "Z")]
    word = (7.0, 8.0)  # inside Y, X and Z: a three-way tie
    got = {tb: SpeakerIndex(turns, tie_break=tb).speaker_for(*word)[0] for tb in TIE_BREAKS}
    assert got == {"floor": "Y", "latest_start": "Z", "first_seen": "X", "previous_word": "Y"}
    assert SpeakerIndex(turns, tie_break="previous_word").speaker_for(*word, previous="Z") == ("Z", "tie")
    assert SpeakerIndex(turns, tie_break="previous_word").speaker_for(*word, previous="Q") == ("Y", "tie")
    # a clear winner is never a tie, whatever the rule
    assert {SpeakerIndex(turns, tie_break=tb).speaker_for(5.1, 5.9) for tb in TIE_BREAKS} == {("Y", "overlap")}
    assert DEFAULT_TIE_BREAK == "floor" and tie_break_param(None) == "floor"
    with pytest.raises(ParamError):
        tie_break_param("coin_flip")


def test_recorded_rule_text_names_the_tie_break_used():
    """hyp.extra["assignment"]["rule"] describes the tie-break that ran; floor
    keeps the 2026-09-25 text byte for byte (earlier hypotheses carry it)."""
    from pipeline.base import TIE_BREAKS, assignment_rule

    assert assignment_rule() == assignment_rule("floor") == ASSIGNMENT_RULE
    assert "earliest-starting overlapping turn" in ASSIGNMENT_RULE
    texts = {tb: assignment_rule(tb) for tb in TIE_BREAKS}
    assert len(set(texts.values())) == len(TIE_BREAKS)
    assert "latest-starting overlapping turn" in texts["latest_start"]
    assert "appears first" in texts["first_seen"] and "previous word" in texts["previous_word"]
    with pytest.raises(ParamError):
        assignment_rule("coin_flip")


def test_previous_word_tie_break_keeps_a_run_together():
    turns = [(0.0, 4.0, "A"), (2.0, 4.0, "B")]
    seg = {"start": 1.0, "end": 3.5, "text": "one two three", "words": [
        {"w": "one", "start": 1.0, "end": 1.5}, {"w": "two", "start": 2.2, "end": 2.6}, {"w": "three", "start": 3.0, "end": 3.4}]}
    stats: dict = {}
    for tb, want in (("previous_word", ["A"]), ("latest_start", ["A", "B"])):
        out = assign_speakers([seg], turns, stats=stats, tie_break=tb)
        assert [s["speaker"] for s in out] == want, tb
        assert stats["tie"] == 2 and stats["overlap"] == 1


def test_word_outside_every_turn_takes_the_nearest_turn_by_gap():
    idx = SpeakerIndex([(0.0, 1.0, "A"), (3.0, 4.0, "B")])
    assert idx.speaker_for(1.2, 1.5) == ("A", "nearest")  # gap 0.2 vs 1.5
    assert idx.speaker_for(2.6, 2.8) == ("B", "nearest")  # gap 1.6 vs 0.2


def test_zero_length_word_inside_a_long_turn_stays_with_that_turn():
    # The old rule measured the word midpoint to the nearest turn BOUNDARY, so a
    # point in the middle of A (5 s from A's edges) went to B (0.5 s away).
    idx = SpeakerIndex([(0.0, 10.0, "A"), (5.5, 6.0, "B")])
    assert idx.speaker_for(5.0, 5.0) == ("A", "nearest")


def test_no_turns_means_the_fallback_speaker():
    assert SpeakerIndex([], fallback="spk0").speaker_for(0.0, 1.0) == ("spk0", "fallback")


def test_assign_speakers_builds_contract_segments_from_word_runs():
    asr = [
        {"start": 0.0, "end": 4.0, "text": "x", "words": [
            {"w": " Hello,", "start": 0.0, "end": 0.4},
            {"w": " world.", "start": 0.5, "end": 0.9},
            {"w": " ...", "start": 0.9, "end": 1.0},           # punctuation only: dropped
            {"w": " New York", "start": 2.0, "end": 2.4},      # two tokens: interval halved
            {"w": " Yes", "start": 3.0, "end": 3.0},           # zero length, alone in its run
        ]},
        {"start": 5.0, "end": 6.0, "text": " Okay.", "words": []},  # no word timings
        {"start": 7.0, "end": 7.5, "text": " ?!", "words": []},     # nothing left: dropped
    ]
    turns = [(0.0, 1.5, "spk0"), (1.8, 2.6, "spk1"), (2.9, 3.1, "spk0"), (4.8, 6.2, "spk1")]
    stats: dict = {}
    segs = assign_speakers(asr, turns, stats=stats)
    assert [(s["speaker"], s["text"]) for s in segs] == [
        ("spk0", "hello world"), ("spk1", "new york"), ("spk0", "yes"), ("spk1", "okay")
    ]
    assert segs[1]["words"] == [{"w": "new", "start": 2.0, "end": 2.2}, {"w": "york", "start": 2.2, "end": 2.4}]
    assert segs[2]["start"] == 3.0 and segs[2]["end"] == pytest.approx(3.0 + MIN_SEGMENT_S)
    assert (segs[0]["start"], segs[0]["end"]) == (0.0, 0.9)
    assert "words" not in segs[3]
    assert stats == {"words": 5, "overlap": 4, "tie": 0, "nearest": 1, "fallback": 0, "segments_without_words": 1}
    _hyp_roundtrip(Hypothesis("m1", "t", segs).validate())


def test_assign_speakers_sorts_words_and_spans_the_latest_end():
    asr = [{"start": 0.0, "end": 2.0, "text": "", "words": [
        {"w": "b", "start": 0.6, "end": 1.4}, {"w": "a", "start": 0.5, "end": 0.7}]}]
    seg = assign_speakers(asr, [(0.0, 2.0, "S")])[0]
    assert [w["w"] for w in seg["words"]] == ["a", "b"] and seg["text"] == "a b"
    assert (seg["start"], seg["end"]) == (0.5, 1.4)


def test_speaker_index_handles_an_hour_of_words_quickly():
    import time

    rng = np.random.default_rng(0)
    edges = np.sort(rng.uniform(0, 3600, 3000))
    turns = [(float(a), float(b), f"s{i % 5}") for i, (a, b) in enumerate(zip(edges[:-1], edges[1:]))]
    words = [{"w": "w", "start": float(t), "end": float(t) + 0.3} for t in np.sort(rng.uniform(0, 3599, 10000))]
    t0 = time.perf_counter()
    segs = assign_speakers([{"start": 0.0, "end": 3600.0, "text": "", "words": words}], turns)
    assert sum(len(s["words"]) for s in segs) == 10000
    assert time.perf_counter() - t0 < 10.0  # measured 0.121, 0.127, 0.170 s on the M1 (2026-09-25)


# --- output normalisers (no models) ----------------------------------------------------------------


@dataclass
class _Word:  # faster_whisper.transcribe.Word (1.2.1): start, end, word, probability
    start: float
    end: float
    word: str
    probability: float = 0.9


@dataclass
class _Seg:  # sherpa_onnx OfflineSpeakerDiarizationSegment: start, end, speaker
    start: float
    end: float
    speaker: int


def test_whisper_words_normalise_split_and_clip():
    ws = whisper_words([_Word(-0.05, 0.3, " So"), _Word(0.3, 0.3, " U.S."), _Word(0.4, 0.5, " ..."),
                        _Word(9.8, 10.4, " end."), _Word(1.0, 0.9, " back")], duration_s=10.0)
    assert [w["w"] for w in ws] == ["so", "u.s", "end", "back"]
    assert ws[0]["start"] == 0.0 and ws[2]["end"] == 10.0
    assert all(w["end"] >= w["start"] for w in ws)


def test_sherpa_turns_relabel_by_first_appearance_and_drop_empty_turns():
    turns = sherpa_turns([_Seg(3.0, 4.0, 0), _Seg(0.5, 2.0, 7), _Seg(2.0, 2.0, 1), _Seg(1.5, 3.5, 0)])
    assert turns == [(0.5, 2.0, "spk0"), (1.5, 3.5, "spk1"), (3.0, 4.0, "spk1")]


def test_sherpa_turns_are_clipped_to_the_audio_duration():
    # Review 2026-09-25: on 0.5 s of audio sherpa returned (0.031, 0.6385, 0);
    # the padded last window must not put speech after the end of the file.
    raw = [_Seg(0.031, 0.6385, 0), _Seg(0.55, 0.9, 1), _Seg(-0.01, 0.2, 2)]
    assert sherpa_turns(raw, duration_s=0.5) == [(0.0, 0.2, "spk0"), (0.031, 0.5, "spk1")]
    assert sherpa_turns(raw)[-1] == (0.55, 0.9, "spk2")  # no duration: nothing clipped at the end


# --- adapters against stand-in modules (no models) -------------------------------------------------


def _sherpa_standin(calls: dict, segments: list[_Seg], sample_rate: int = SR) -> types.ModuleType:
    """Shaped after sherpa-onnx 1.13.8's Python API (docs/pipeline.md §11)."""
    m = types.ModuleType("sherpa_onnx")

    class _Cfg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class OfflineSpeakerDiarizationConfig(_Cfg):
        def validate(self):
            calls.setdefault("validated", 0)
            calls["validated"] += 1
            return True

    class OfflineSpeakerDiarization:
        def __init__(self, config):
            calls["init"] = config
            self.sample_rate = sample_rate

        def set_config(self, config):
            calls.setdefault("set_config", []).append(config.clustering.num_clusters)

        def process(self, samples, callback=None):
            calls.setdefault("process", []).append((str(samples.dtype), samples.flags["C_CONTIGUOUS"], len(samples)))
            return types.SimpleNamespace(sort_by_start_time=lambda: list(segments))

    for name in ("OfflineSpeakerSegmentationModelConfig", "OfflineSpeakerSegmentationPyannoteModelConfig",
                 "SpeakerEmbeddingExtractorConfig", "FastClusteringConfig"):
        setattr(m, name, type(name, (_Cfg,), {}))
    m.OfflineSpeakerDiarizationConfig = OfflineSpeakerDiarizationConfig
    m.OfflineSpeakerDiarization = OfflineSpeakerDiarization
    return m


def _fw_standin(calls: dict, words: list[_Word]) -> types.ModuleType:
    """Shaped after faster-whisper 1.2.1: WhisperModel(path, device, compute_type, cpu_threads, ...)
    and transcribe(...) -> (lazy segment generator, TranscriptionInfo)."""
    m = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model_size_or_path, device="auto", device_index=0, compute_type="default", cpu_threads=0, **kw):
            calls["init"] = {"model": model_size_or_path, "device": device, "compute_type": compute_type, "cpu_threads": cpu_threads}

        def transcribe(self, audio, language=None, beam_size=5, word_timestamps=False, vad_filter=False,
                       temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0), **kw):
            calls["transcribe"] = {"language": language, "beam_size": beam_size, "word_timestamps": word_timestamps,
                                   "vad_filter": vad_filter, "dtype": str(audio.dtype), "temperature": temperature}
            seg = types.SimpleNamespace(start=words[0].start, end=words[-1].end, text=" ".join(w.word for w in words), words=words,
                                        temperature=0.0)
            info = types.SimpleNamespace(language="en", language_probability=1.0, duration=len(audio) / SR, duration_after_vad=len(audio) / SR)
            return (s for s in [seg]), info

    m.WhisperModel = WhisperModel
    return m


@pytest.fixture
def fake_models(tmp_path):
    seg, emb, asr = tmp_path / "seg.onnx", tmp_path / "emb.onnx", tmp_path / "ct2"
    seg.write_bytes(b"x")
    emb.write_bytes(b"y")
    asr.mkdir()
    return seg, emb, asr


def test_sherpa_diarizer_plumbing_hint_on_and_off(monkeypatch, fake_models):
    seg, emb, _ = fake_models
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _sherpa_standin(calls, [_Seg(0.0, 1.0, 3), _Seg(1.0, 2.0, 1)]))
    d = SherpaOnnxDiarizer(seg, emb, cluster_threshold=0.7, num_threads=2)
    cfg = calls["init"]
    assert cfg.segmentation.pyannote.model == str(seg) and cfg.embedding.model == str(emb)
    assert cfg.clustering.num_clusters == -1 and cfg.clustering.threshold == 0.7
    assert cfg.segmentation.num_threads == 2 and (cfg.min_duration_on, cfg.min_duration_off) == (0.3, 0.5)
    x = np.zeros(SR * 2, dtype=np.float64)[::1]
    assert d.diarize(x, SR) == [(0.0, 1.0, "spk0"), (1.0, 2.0, "spk1")]
    assert "set_config" not in calls  # the constructor's config already has num_clusters=-1
    d.diarize(x, SR, num_speakers=2)
    d.diarize(x, SR, num_speakers=2)
    d.diarize(x, SR)
    assert calls["set_config"] == [2, -1]  # changed only when the hint changes
    assert calls["process"][0] == ("float32", True, SR * 2)
    assert d.last_info == {"num_speakers_hint": None, "n_speakers": 2, "hint_honoured": None, "n_turns": 2}
    with pytest.raises(PipelineUnavailable, match="expects 16000 Hz"):
        d.diarize(x, 8000)
    assert d.diarize(np.zeros(0, dtype=np.float32), SR) == []


def test_sherpa_diarizer_records_and_warns_when_the_hint_is_not_honoured(monkeypatch, fake_models):
    # sherpa lowers an unreachable cluster count silently (review 2026-09-25:
    # hint 6 -> 3 speakers, hint 12 -> 4 on the 20 s AMI clip); the stand-in
    # always returns 2 speakers, and turns past the end of the audio.
    seg, emb, _ = fake_models
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _sherpa_standin({}, [_Seg(0.0, 1.0, 0), _Seg(0.8, 2.6, 1)]))
    d = SherpaOnnxDiarizer(seg, emb)
    x = np.zeros(SR * 2, dtype=np.float32)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an honoured hint must not warn
        assert d.diarize(x, SR, num_speakers=2)[-1] == (0.8, 2.0, "spk1")  # clipped to the 2 s of audio
    assert d.last_info["hint_honoured"] is True
    with pytest.warns(RuntimeWarning, match="returned 2 speaker.*num_speakers=3.*not honoured"):
        d.diarize(x, SR, num_speakers=3)
    assert d.last_info == {"num_speakers_hint": 3, "n_speakers": 2, "hint_honoured": False, "n_turns": 2}


def test_sherpa_diarizer_reports_missing_weights(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _sherpa_standin({}, []))
    monkeypatch.setenv(model_store.MODELS_DIR_ENV, str(tmp_path / "empty"))
    with pytest.raises(PipelineUnavailable, match=f"^{SKIP_WEIGHTS}{SEGMENTATION_ASSET}.*fetch-models"):
        SherpaOnnxDiarizer()
    with pytest.raises(PipelineUnavailable, match="model file not found"):
        SherpaOnnxDiarizer(tmp_path / "nope.onnx", tmp_path / "nope2.onnx")


def test_faster_whisper_adapter_uses_the_installed_api_shape(monkeypatch, fake_models):
    _, _, asr_dir = fake_models
    calls: dict = {}
    words = [_Word(0.0, 0.4, " So"), _Word(0.4, 0.9, " that's"), _Word(0.9, 0.9, " it.")]
    monkeypatch.setitem(sys.modules, "faster_whisper", _fw_standin(calls, words))
    t = FasterWhisperTranscriber(model=str(asr_dir), language="en", cpu_threads=4, allow_library_names=False)
    segs = t.transcribe(np.zeros(SR, dtype=np.float64), SR)
    assert calls["init"] == {"model": str(asr_dir), "device": "cpu", "compute_type": "int8", "cpu_threads": 4}
    assert calls["transcribe"] == {"language": "en", "beam_size": 5, "word_timestamps": True, "vad_filter": False, "dtype": "float32",
                                   "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]}
    assert segs[0]["text"] == "so that's it" and [w["w"] for w in segs[0]["words"]] == ["so", "that's", "it"]
    assert t.last_info == {"language": "en", "language_probability": 1.0, "duration": 1.0, "duration_after_vad": 1.0,
                           "n_segments": 1, "n_words": 3, "temperatures": {"0.0": 1}, "fallback_segments": 0}
    with pytest.raises(ParamError, match="neither a pinned asset"):
        FasterWhisperTranscriber(model="tiny", allow_library_names=False)


def test_composed_model_pipeline_with_standins_emits_the_contract(monkeypatch, fake_models, tmp_path):
    seg, emb, asr_dir = fake_models
    words = [_Word(0.1, 0.5, " Hello"), _Word(0.5, 0.9, " there."), _Word(1.2, 1.6, " Hi."), _Word(1.6, 1.6, " Bye")]
    monkeypatch.setitem(sys.modules, "faster_whisper", _fw_standin({}, words))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _sherpa_standin({}, [_Seg(0.0, 1.0, 5), _Seg(1.1, 1.7, 2)]))
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.zeros(2 * SR, dtype=np.float32), SR, subtype="PCM_16")
    pipe = ModelComposedPipeline(
        FasterWhisperTranscriber(model=str(asr_dir), allow_library_names=False),
        SherpaOnnxDiarizer(seg, emb), PipelineConfig(name="whisper-sherpa", params={"num_speakers": 2}), name="whisper-sherpa")
    hyp = pipe.run(wav)
    assert [(s["speaker"], s["text"]) for s in hyp.segments] == [("spk0", "hello there"), ("spk1", "hi bye")]
    e = hyp.extra
    assert e["components"] == {"transcriber": "faster-whisper", "diarizer": "sherpa-onnx"}
    assert e["diarization"]["turns"] == [[0.0, 1.0, "spk0"], [1.1, 1.7, "spk1"]]
    assert e["diarization"]["num_speakers_hint"] == 2 and e["assignment"]["words"] == 4
    assert e["diarization"]["hint_honoured"] is True and e["assignment"]["rule"] == ASSIGNMENT_RULE
    assert set(e["timing"]) == {"model_load_s", "diarization_s", "asr_s", "assign_s", "audio_s", "rtf"}
    # explicit (unpinned) model paths: the bytes actually loaded are hashed
    assert e["models"]["segmentation"] == {"path": str(seg), "pinned": False,
                                           "files": {"seg.onnx": {"sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 1}}}
    assert e["models"]["asr"] == {"path": str(asr_dir), "pinned": False, "files": {}}
    _hyp_roundtrip(hyp)
    paths = write_hypothesis_files(hyp, tmp_path / "out" / "hyp.json")
    assert paths["rttm"].read_text().splitlines()[0].startswith("SPEAKER clip 1 0.100 0.800 <NA> <NA> spk0")


# --- manifest and fetcher (no network) ---------------------------------------------------------------


def test_manifest_pins_every_file_with_https_source_sha256_and_licence():
    assert set(model_store.ASSETS) >= {"faster-whisper-small.en", SEGMENTATION_ASSET, EMBEDDING_ASSET}
    for a in model_store.ASSETS.values():
        assert a.licence and a.licence_source and a.upstream and a.files
        for f in a.files:
            assert re.fullmatch(r"[0-9a-f]{64}", f.sha256) and f.size > 0
            src = f.url or (a.archive.url if a.archive else "")
            assert src.startswith(("https://huggingface.co/", "https://github.com/k2-fsa/sherpa-onnx/releases/download/"))
            assert (f.url is None) != (f.archive_member is None)
        if a.revision:  # Hugging Face files are pinned to a commit, never a branch
            assert all(a.revision in f.url for f in a.files if f.url)
    assert model_store.ASSETS["faster-whisper-small.en"].size < 600e6  # fits the 8 GB machine budget


def _fake_asset(monkeypatch, tmp_path, payloads: dict[str, bytes], archive: bytes | None = None, members=()):
    files = []
    for rel, data in payloads.items():
        import hashlib

        member = f"pkg/{rel}" if rel in members else None
        files.append(model_store.ModelFile(rel, hashlib.sha256(data).hexdigest(), len(data),
                                           url=None if member else f"https://example.invalid/{rel}", archive_member=member))
    arc = None
    if archive is not None:
        import hashlib

        arc = model_store.Archive("https://example.invalid/pkg.tar.bz2", hashlib.sha256(archive).hexdigest(), len(archive))
    asset = model_store.ModelAsset(key="fake", kind="speaker-embedding", subdir="fake", entry=list(payloads)[0],
                                   files=tuple(files), licence="MIT", licence_source="test", upstream="test", archive=arc)
    monkeypatch.setitem(model_store.ASSETS, "fake", asset)
    monkeypatch.setenv(model_store.MODELS_DIR_ENV, str(tmp_path / "models"))
    return asset


def _opener(served: dict[str, bytes], seen: list):
    def op(url):
        seen.append(url)
        return io.BytesIO(served[url])

    return op


def test_fetch_downloads_verifies_and_keeps_verified_files(monkeypatch, tmp_path):
    a = _fake_asset(monkeypatch, tmp_path, {"m.onnx": b"weights" * 100})
    assert model_store.missing_reason("fake").startswith(SKIP_WEIGHTS + "fake")
    seen: list = []
    path = model_store.fetch("fake", opener=_opener({"https://example.invalid/m.onnx": b"weights" * 100}, seen), log=lambda m: None)
    assert path.read_bytes() == b"weights" * 100 and model_store.missing_reason("fake") is None
    assert model_store.verify("fake") == []
    model_store.fetch("fake", opener=_opener({}, seen), log=lambda m: None)  # already verified: no request
    assert seen == ["https://example.invalid/m.onnx"]
    assert a.describe()["files"]["m.onnx"]["bytes"] == 700


def test_fetch_refuses_a_sha256_mismatch_and_leaves_nothing_behind(monkeypatch, tmp_path):
    a = _fake_asset(monkeypatch, tmp_path, {"m.onnx": b"good"})
    with pytest.raises(ValueError, match="pinned"):
        model_store.fetch("fake", opener=_opener({"https://example.invalid/m.onnx": b"evil"}, []), log=lambda m: None)
    assert not a.directory.exists() or not any(a.directory.iterdir())
    a.directory.mkdir(parents=True, exist_ok=True)
    (a.directory / "m.onnx").write_bytes(b"bad!")  # right size, wrong bytes
    assert model_store.missing_reason("fake") is None  # the cheap check is size only...
    assert "sha256" in model_store.verify("fake")[0]  # ...verify hashes


def test_fetch_extracts_only_named_archive_members(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in (("pkg/model.onnx", b"seg" * 50), ("pkg/LICENSE", b"MIT"), ("../evil", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    arc = buf.getvalue()
    a = _fake_asset(monkeypatch, tmp_path, {"model.onnx": b"seg" * 50, "LICENSE": b"MIT"}, archive=arc,
                    members=("model.onnx", "LICENSE"))
    model_store.fetch("fake", opener=_opener({"https://example.invalid/pkg.tar.bz2": arc}, []), log=lambda m: None)
    assert sorted(p.name for p in a.directory.iterdir()) == ["LICENSE", "model.onnx"]
    assert not (tmp_path / "models" / "evil").exists() and not (tmp_path / "evil").exists()


def test_cli_models_and_fetch_models_usage(monkeypatch, tmp_path):
    env = {"PYTHONDONTWRITEBYTECODE": "1", model_store.MODELS_DIR_ENV: str(tmp_path / "none"), "PATH": "/usr/bin:/bin"}

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "pipeline", *args], cwd=ROOT, env=env, capture_output=True, text=True)

    r = cli("models", "--json")
    assert r.returncode == 0, r.stderr  # listing is not a check
    rows = {x["key"]: x for x in json.loads(r.stdout)["assets"]}
    assert not rows[SEGMENTATION_ASSET]["present"] and rows[SEGMENTATION_ASSET]["reason"].startswith(SKIP_WEIGHTS)
    # --verify against an empty models dir must FAIL (review 2026-09-25: it exited 0)
    r = cli("models", "--verify")
    assert r.returncode == 1 and r.stdout.count("MISSING") == len(model_store.ASSETS), r.stdout
    r = cli("models", "--verify", "--json", SEGMENTATION_ASSET)
    assert r.returncode == 1
    assert [(x["key"], x["verified"]) for x in json.loads(r.stdout)["assets"]] == [(SEGMENTATION_ASSET, False)]
    assert cli("models", "no-such-asset").returncode == 2
    r = cli("fetch-models", "no-such-asset")
    assert r.returncode == 2 and "unknown model asset" in r.stderr


def test_cli_models_verify_passes_only_when_every_requested_file_matches(monkeypatch, tmp_path):
    # in-process (the fake asset is monkeypatched into this interpreter's manifest)
    from pipeline.cli import main

    a = _fake_asset(monkeypatch, tmp_path, {"m.onnx": b"weights"})
    a.directory.mkdir(parents=True)
    (a.directory / "m.onnx").write_bytes(b"weights")
    assert main(["models", "--verify", "fake"]) == 0
    (a.directory / "m.onnx").write_bytes(b"WEIGHTS")  # same size, other bytes
    assert main(["models", "--verify", "fake"]) == 1


# --- load-time hashing: provenance is what was loaded -------------------------------------------------


def _replace_file(path: Path, data: bytes) -> None:
    """Put other bytes at ``path`` through a new inode, as a copy or a re-download would."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def test_require_records_computed_digests_and_refuses_same_size_tampering(monkeypatch, tmp_path):
    a = _fake_asset(monkeypatch, tmp_path, {"m.onnx": b"weights" * 10})
    with pytest.raises(PipelineUnavailable, match=f"^{SKIP_WEIGHTS}fake"):
        model_store.require("fake")
    a.directory.mkdir(parents=True)
    (a.directory / "m.onnx").write_bytes(b"weights" * 10)
    path, prov = model_store.require("fake")
    pin = hashlib.sha256(b"weights" * 10).hexdigest()
    assert path == a.directory / "m.onnx" and prov["pinned"] is True and prov["sha256_matches_pin"] is True
    assert prov["files"]["m.onnx"]["sha256"] == pin == prov["files"]["m.onnx"]["pinned_sha256"]
    # Review 2026-09-25: a same-size file with other contents passed the size check
    # and was REPORTED with the pinned sha256.  Now it is refused at load time.
    _replace_file(a.directory / "m.onnx", b"WEIGHTS" * 10)
    assert model_store.missing_reason("fake") is None  # the cheap availability check is size only
    with pytest.raises(PipelineUnavailable, match=re.escape(model_store.MISMATCH_PREFIX) + r"fake .*fetch-models fake"):
        model_store.require("fake")
    # the manifest itself only states pins
    assert set(a.describe()["files"]["m.onnx"]) == {"pinned_sha256", "bytes", "url"}


def test_require_follows_symlinks_and_hashes_the_target(monkeypatch, tmp_path):
    a = _fake_asset(monkeypatch, tmp_path, {"m.onnx": b"good-bytes"})
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "real.onnx").write_bytes(b"evil-bytes")  # same size
    a.directory.mkdir(parents=True)
    (a.directory / "m.onnx").symlink_to(tmp_path / "elsewhere" / "real.onnx")
    with pytest.raises(PipelineUnavailable, match=re.escape(model_store.MISMATCH_PREFIX)):
        model_store.require("fake")


def test_file_sha256_is_memoised_by_inode_size_and_mtime(monkeypatch, tmp_path):
    p = tmp_path / "w.bin"
    p.write_bytes(b"abc")
    hashed: list = []
    real = model_store.sha256_file
    monkeypatch.setattr(model_store, "sha256_file", lambda q: hashed.append(q) or real(q))
    assert model_store.file_sha256(p) == model_store.file_sha256(p) == hashlib.sha256(b"abc").hexdigest()
    assert len(hashed) == 1
    _replace_file(p, b"abd")
    assert model_store.file_sha256(p) == hashlib.sha256(b"abd").hexdigest() and len(hashed) == 2


def _pin_assets(monkeypatch, tmp_path, contents: dict[str, bytes]) -> dict[str, Path]:
    """Replace the real manifest entries by tiny ones with the same keys and kinds."""
    monkeypatch.setenv(model_store.MODELS_DIR_ENV, str(tmp_path / "models"))
    paths = {}
    for key, data in contents.items():
        real = model_store.ASSETS[key]
        name = "model.bin" if real.kind == "asr" else "model.onnx"
        asset = model_store.ModelAsset(
            key=key, kind=real.kind, subdir=f"t/{key}", entry="." if real.kind == "asr" else name,
            files=(model_store.ModelFile(name, hashlib.sha256(data).hexdigest(), len(data), url=f"https://example.invalid/{key}"),),
            licence="MIT", licence_source="test", upstream="test")
        monkeypatch.setitem(model_store.ASSETS, key, asset)
        asset.directory.mkdir(parents=True)
        (asset.directory / name).write_bytes(data)
        paths[key] = asset.directory / name
    return paths


def test_model_constructors_refuse_tampered_pinned_weights(monkeypatch, tmp_path):
    paths = _pin_assets(monkeypatch, tmp_path, {SEGMENTATION_ASSET: b"seg", EMBEDDING_ASSET: b"emb",
                                                "faster-whisper-small.en": b"asr"})
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _sherpa_standin({}, []))
    fw_calls: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", _fw_standin(fw_calls, [_Word(0.0, 0.5, " hi")]))
    d = SherpaOnnxDiarizer()
    assert d.models["segmentation"]["files"]["model.onnx"]["sha256"] == hashlib.sha256(b"seg").hexdigest()
    t = FasterWhisperTranscriber(model="small.en", allow_library_names=False)
    assert fw_calls["init"]["model"] == str(paths["faster-whisper-small.en"].parent)
    assert t.model_provenance["key"] == "faster-whisper-small.en" and t.model_provenance["sha256_matches_pin"]
    _replace_file(paths[SEGMENTATION_ASSET], b"SEG")
    _replace_file(paths["faster-whisper-small.en"], b"ASR")
    with pytest.raises(PipelineUnavailable, match=re.escape(model_store.MISMATCH_PREFIX) + SEGMENTATION_ASSET):
        SherpaOnnxDiarizer()
    fw_calls.clear()
    for allow in (False, True):
        with pytest.raises(PipelineUnavailable, match=re.escape(model_store.MISMATCH_PREFIX) + "faster-whisper-small.en"):
            FasterWhisperTranscriber(model="small.en", allow_library_names=allow)
    assert "init" not in fw_calls  # refused before the library saw the file


# --- faster-whisper model names never download implicitly ---------------------------------------------


def _fw_utils_standin(monkeypatch, tmp_path, calls: list) -> None:
    """faster_whisper + faster_whisper.utils shaped after 1.2.1: available_models(),
    download_model(size_or_id, output_dir=None, local_files_only=False, cache_dir=None,
    revision=None, use_auth_token=None) -> snapshot directory."""
    snap = tmp_path / "hf" / "snapshots" / "abc123"
    fw = _fw_standin({}, [_Word(0.0, 0.5, " hi")])
    utils = types.ModuleType("faster_whisper.utils")

    def download_model(size_or_id, output_dir=None, local_files_only=False, cache_dir=None, revision=None, use_auth_token=None):
        calls.append({"name": size_or_id, "local_files_only": local_files_only, "use_auth_token": use_auth_token})
        if local_files_only:
            raise FileNotFoundError("not in the cache (stand-in for huggingface_hub LocalEntryNotFoundError)")
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "model.bin").write_bytes(b"tiny-weights")
        return str(snap)

    utils.available_models = lambda: ["tiny", "tiny.en", "small.en"]
    utils.download_model = download_model
    fw.utils = utils
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)


def test_library_model_names_resolve_from_the_cache_only_unless_allow_download(monkeypatch, tmp_path):
    calls: list = []
    _fw_utils_standin(monkeypatch, tmp_path, calls)
    monkeypatch.setenv(model_store.MODELS_DIR_ENV, str(tmp_path / "empty-models"))
    with pytest.raises(PipelineUnavailable, match="not in the local Hugging Face cache.*allow_download=true"):
        resolve_fw_model("tiny")
    path, prov = resolve_fw_model("tiny", allow_download=True)
    # both calls: no stored Hugging Face token is sent (use_auth_token=False -> token=False)
    assert calls == [{"name": "tiny", "local_files_only": True, "use_auth_token": False},
                     {"name": "tiny", "local_files_only": False, "use_auth_token": False}]
    assert Path(path).name == "abc123" and prov["pinned"] is False and prov["allow_download"] is True
    assert prov["files"]["model.bin"]["sha256"] == hashlib.sha256(b"tiny-weights").hexdigest()
    with pytest.raises(ParamError, match="not a pinned asset"):
        resolve_fw_model("no-such-size")
    with pytest.raises(ParamError, match="neither a pinned asset"):
        resolve_fw_model("tiny", allow_library_names=False, allow_download=True)
    # Review 2026-09-25: the unfetched pinned alias fell through to the library's
    # unpinned download ('small.en', None).  It is now unavailable, whatever is allowed.
    calls.clear()
    for kw in ({}, {"allow_download": True}):
        with pytest.raises(PipelineUnavailable, match=f"^{SKIP_WEIGHTS}faster-whisper-small.en"):
            resolve_fw_model("small.en", **kw)
    assert calls == []


def test_legacy_faster_whisper_entry_is_local_only_and_records_provenance(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", _fw_standin(calls, [_Word(0.1, 0.5, " Hello"), _Word(0.6, 0.9, " there.")]))
    monkeypatch.setenv(model_store.MODELS_DIR_ENV, str(tmp_path / "empty-models"))
    with pytest.raises(PipelineUnavailable, match=f"^{SKIP_WEIGHTS}faster-whisper-small.en.*fetch-models"):
        get_pipeline("faster-whisper")  # default small.en, not fetched: refused, nothing downloaded
    assert "init" not in calls
    with pytest.raises(ParamError, match="allow_download must be true or false"):
        get_pipeline("faster-whisper", PipelineConfig(params={"allow_download": "yes"}))
    reg = registered()  # whisper-sherpa never takes library names, so it has no allow_download
    assert "allow_download" in reg["faster-whisper"].params and "allow_download" in reg["faster-whisper+pyannote"].params
    assert "allow_download" not in reg["whisper-sherpa"].params
    model_dir = tmp_path / "ct2"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"local")
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.zeros(SR, dtype=np.float32), SR, subtype="PCM_16")
    hyp = get_pipeline("faster-whisper", PipelineConfig(params={"model": str(model_dir)})).run(wav)
    asr = hyp.extra["models"]["asr"]
    assert asr["pinned"] is False and asr["files"]["model.bin"]["sha256"] == hashlib.sha256(b"local").hexdigest()
    assert hyp.extra["assignment"]["rule"] == ASSIGNMENT_RULE and "model_load_s" in hyp.extra["timing"]
    _hyp_roundtrip(hyp)


def test_library_model_name_lookup_touches_no_network_without_allow_download(tmp_path):
    """The installed faster-whisper/huggingface_hub, in a subprocess with an empty
    Hugging Face cache and HF_HUB_OFFLINE=1: resolving "tiny" must fail with the
    cache message and leave the cache empty.  Without faster-whisper (CI) the
    resolver reports it not importable; both are PipelineUnavailable, no skip."""
    cache = tmp_path / "hf-home"
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HF_HOME": str(cache),
           "HF_HUB_CACHE": str(cache / "hub"), "HF_HUB_OFFLINE": "1", model_store.MODELS_DIR_ENV: str(tmp_path / "m")}
    code = ("from pipeline.adapters import resolve_fw_model\n"
            "from pipeline.base import PipelineUnavailable\n"
            "try:\n    resolve_fw_model('tiny')\nexcept PipelineUnavailable as e:\n    print('UNAVAILABLE', e)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.startswith("UNAVAILABLE"), (r.stdout, r.stderr)
    assert "not in the local Hugging Face cache" in r.stdout or "not importable" in r.stdout, r.stdout
    assert not any(p.is_file() for p in cache.rglob("*")) if cache.exists() else True


# --- registry ----------------------------------------------------------------------------------------


def test_registry_declares_the_model_pipelines():
    reg = registered()
    for name in ("whisper-sherpa", "sherpa-onnx-diarization"):
        e = reg[name]
        assert e.is_system_under_test and e.params is not None
        assert {"num_speakers", "cluster_threshold", "segmentation_model", "embedding_model"} <= e.params
        reason = e.reason()
        if reason is not None:
            assert "not importable" in reason or reason.startswith(SKIP_WEIGHTS), reason
    assert {"model", "cpu_threads", "language"} <= reg["whisper-sherpa"].params
    assert "model" not in reg["sherpa-onnx-diarization"].params


def test_whisper_sherpa_ecapa_is_declared_with_distinct_ecapa_params():
    e = registered()["whisper-sherpa-ecapa"]
    assert e.is_system_under_test
    assert {"num_speakers", "cluster_threshold", "assignment_tie_break", "asr_cache", "ecapa_model", "batch_windows"} <= e.params
    assert "embedding_model" in e.params  # sherpa's CAM++ file, as in whisper-sherpa; ECAPA's is ecapa_model
    reason = e.reason()
    if reason is not None:
        assert "not importable" in reason or reason.startswith(SKIP_WEIGHTS), reason


class _FakeDiarizer:
    def __init__(self, turns, models=None):
        self.turns, self.calls = turns, []
        self.models, self.settings, self.last_info = models or {}, {"fake": True}, {"info": 1}

    def diarize(self, pcm, sample_rate, num_speakers=None):
        self.calls.append(num_speakers)
        return list(self.turns)


def test_sherpa_counted_embedding_diarizer_takes_the_count_from_sherpa_and_the_turns_from_ecapa():
    """whisper-sherpa-ecapa (docs/pipeline.md §11.10): sherpa-onnx gives the
    speaker count, the ECAPA clusterer the turns; a hint skips sherpa."""
    from pipeline.whisper_sherpa import SherpaCountedEmbeddingDiarizer

    counter = _FakeDiarizer([(0.0, 1.0, "A"), (1.0, 2.0, "B"), (2.0, 3.0, "A"), (3.0, 4.0, "C")],
                            {"segmentation": "seg", "embedding": "campp"})
    ecapa = _FakeDiarizer([(0.0, 2.0, "spk0"), (2.0, 4.0, "spk1")], {"embedding": "ecapa"})
    d = SherpaCountedEmbeddingDiarizer(counter, ecapa)
    pcm = np.zeros(16000, dtype=np.float32)
    assert d.diarize(pcm, 16000) == [(0.0, 2.0, "spk0"), (2.0, 4.0, "spk1")]
    assert counter.calls == [None] and ecapa.calls == [3]
    assert d.last_info["speaker_count"]["source"] == "sherpa-onnx" and d.last_info["speaker_count"]["speakers"] == 3
    assert d.models == {"count_segmentation": "seg", "count_embedding": "campp", "turns_embedding": "ecapa"}
    assert d.last_info["num_speakers_hint"] is None and d.last_info["hint_honoured"] is None
    assert d.last_info["n_speakers"] == 2 and d.last_info["n_turns"] == 2  # every key run-v5.sh reads
    d.diarize(pcm, 16000, num_speakers=4)  # the hint: sherpa is not run
    assert counter.calls == [None] and ecapa.calls == [3, 4]
    assert d.last_info["speaker_count"] == {"source": "hint", "speakers": 4}
    assert d.last_info["num_speakers_hint"] == 4 and d.last_info["hint_honoured"] is False  # the fake gives 2
    silent = SherpaCountedEmbeddingDiarizer(_FakeDiarizer([]), ecapa)
    assert silent.diarize(pcm, 16000) == [] and ecapa.calls == [3, 4]  # no speech: no clustering
    assert silent.last_info["embedding"] == {} and silent.last_info["n_speakers"] == 0
    assert silent.last_info["n_turns"] == 0


# --- the real models ---------------------------------------------------------------------------------


def _need_real(*assets: str, packages: tuple[str, ...] = ("faster_whisper", "sherpa_onnx"), entry: str = "whisper-sherpa") -> bool:
    """False (after checking the registry says so) when a package is absent,
    as in CI; a documented skip when the packages are present but the pinned
    weights or the AMI files are not; True when the real test can run."""
    from pipeline.adapters import _missing_any

    if _missing_any(*packages):
        reason = registered()[entry].reason()
        assert reason is not None and "not importable" in reason
        with pytest.raises(PipelineUnavailable, match="not importable"):
            get_pipeline(entry)
        return False
    for key in assets:
        reason = model_store.missing_reason(key)
        if reason:
            pytest.skip(reason)
    for p in (AMI_ZIP, AMI_WAV):
        if not p.is_file():
            pytest.skip(f"{SKIP_AMI}{p.relative_to(ROOT)} (local-only, CC BY 4.0; see docs/pipeline.md §11)")
    return True


def _ami_words(meeting: str, t0: float, t1: float) -> list[tuple[str, float, float, str]]:
    """AMI manual word annotations (NXT words.xml) inside [t0, t1], times
    relative to t0: (speaker letter, start, end, word).  Punctuation and
    non-word elements (vocal sounds, gaps) are left out."""
    out = []
    with zipfile.ZipFile(AMI_ZIP) as z:
        for name in z.namelist():
            m = re.fullmatch(rf"words/{meeting}\.([A-Z])\.words\.xml", name)
            if not m:
                continue
            for el in ET.fromstring(z.read(name)):
                if el.tag != "w" or el.get("punc") == "true" or not (el.text or "").strip():
                    continue
                s, e = float(el.get("starttime", "nan")), float(el.get("endtime", "nan"))
                if s >= t0 and e <= t1:
                    out.append((m.group(1), s - t0, e - t0, el.text.strip()))
    return sorted(out, key=lambda t: t[1])


def _norm_tokens(text: str) -> list[str]:
    """Scoring normalisation for the sanity WER only: lowercase, AMI's
    spelled acronyms ('L_C_D_') joined, hyphens split, other punctuation removed."""
    text = re.sub(r"\b((?:[A-Za-z]_)+)", lambda m: m.group(1).replace("_", ""), text).lower().replace("-", " ")
    return [t for t in re.sub(r"[^a-z0-9' ]", " ", text).split() if t]


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not AMI_WAV.is_file():
        return None
    x, sr = sf.read(str(AMI_WAV), start=int(CLIP_T0 * SR), stop=int(CLIP_T1 * SR), dtype="float32")
    assert sr == SR
    p = tmp_path_factory.mktemp("ami") / f"{CLIP_MEETING}_{int(CLIP_T0)}_{int(CLIP_T1)}.wav"
    sf.write(str(p), x, SR, subtype="PCM_16")
    return p, x


@pytest.mark.timeout(600)
def test_real_faster_whisper_transcribes_ami_speech(clip):
    if not _need_real("faster-whisper-small.en", packages=("faster_whisper",), entry="faster-whisper"):
        return
    import jiwer

    _, x = clip
    t = FasterWhisperTranscriber(model="small.en", language="en", cpu_threads=4, allow_library_names=False)
    segs = t.transcribe(x, SR)
    words = [w for s in segs for w in s["words"]]
    assert t.last_info["language"] == "en" and len(words) >= 40
    for w in words:
        assert re.fullmatch(r"\S+", w["w"]) and w["w"] == w["w"].lower()
        assert 0.0 <= w["start"] <= w["end"] <= CLIP_T1 - CLIP_T0
    ref = _norm_tokens(" ".join(w for *_, w in _ami_words(CLIP_MEETING, CLIP_T0, CLIP_T1)))
    hyp = _norm_tokens(" ".join(w["w"] for w in words))
    wer = jiwer.wer(" ".join(ref), " ".join(hyp))
    # Measured 2026-09-25: WER 0.128 on this excerpt (86 reference tokens after
    # this normalisation; 0 substitutions, 8 deletions, 3 insertions).  AMI keeps
    # disfluencies and partial words ("b buttons", "mm-hmm") that Whisper drops,
    # so deletions dominate.  The bound is loose on purpose.
    assert wer < 0.35, (wer, " ".join(hyp))


@pytest.mark.timeout(600)
def test_real_sherpa_diarizer_separates_the_two_ami_talkers(clip):
    if not _need_real(SEGMENTATION_ASSET, EMBEDDING_ASSET, packages=("sherpa_onnx",), entry="sherpa-onnx-diarization"):
        return
    _, x = clip
    d = SherpaOnnxDiarizer()
    ref = [(spk, s, e) for spk, s, e, _ in _ami_words(CLIP_MEETING, CLIP_T0, CLIP_T1) if spk in ("A", "D")]
    for hint in (2, None):
        turns = d.diarize(x, SR, num_speakers=hint)
        labels = {t[2] for t in turns}
        assert turns == sorted(turns) and all(0.0 <= a < b <= CLIP_T1 - CLIP_T0 + 1e-6 for a, b, _ in turns)
        if hint is not None:
            assert len(labels) == hint
        else:
            assert len(labels) >= 2
        idx = SpeakerIndex(turns)
        majority = {}
        for who in ("A", "D"):
            got = [idx.speaker_for(s, e)[0] for spk, s, e in ref if spk == who]
            lab = max(set(got), key=got.count)
            majority[who] = (lab, got.count(lab) / len(got))
        # The two AMI talkers land on different hypothesis speakers.  Measured
        # 2026-09-25 (fractions of each talker's reference words on its majority
        # label): hint 2 -> A 24/27, D 54/57; no hint -> 3 labels, A split
        # 14/10/3 (0.52), D 54/57.  The unaided split of A is the uncalibrated
        # cluster_threshold over-counting (docs/pipeline.md §11), so purity is
        # asserted for D always and for A only with the hint.
        assert majority["A"][0] != majority["D"][0], (hint, majority)
        assert majority["D"][1] >= 0.8, (hint, majority)
        if hint is not None:
            assert majority["A"][1] >= 0.8, (hint, majority)
    # Turns never end after the audio (sherpa pads its last window; review 2026-09-25
    # measured a turn ending at 0.639 s on 0.5 s of this clip before the clip rule).
    assert all(b <= 0.5 for _, b, _ in d.diarize(x[: SR // 2], SR))
    # An unreachable hint is lowered by sherpa; that is recorded and warned about.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        turns = d.diarize(x, SR, num_speakers=12)
    n = len({t[2] for t in turns})
    assert n <= 12 and d.last_info["n_speakers"] == n and d.last_info["hint_honoured"] is (n == 12)
    assert any("not honoured" in str(w.message) for w in caught) is (n != 12)


@pytest.mark.timeout(900)
def test_real_whisper_sherpa_pipeline_writes_a_scorable_hypothesis(clip, tmp_path):
    if not _need_real("faster-whisper-small.en", SEGMENTATION_ASSET, EMBEDDING_ASSET):
        return
    from evals.io import load_hypothesis

    wav, _ = clip
    for params in ({"num_speakers": 2}, {}):
        hyp = get_pipeline("whisper-sherpa", PipelineConfig(params=params)).run(wav)
        paths = write_hypothesis_files(hyp, tmp_path / f"hint{params.get('num_speakers', 0)}" / "hyp.json")
        parsed = load_hypothesis(paths["json"])  # the evals layer's contract parser
        assert parsed.meeting_id == wav.stem and len(parsed.segments) >= 2
        assert all(s.words for s in parsed.segments)  # per-word timings everywhere
        e = hyp.extra
        assert e["diarization"]["num_speakers_hint"] == params.get("num_speakers")
        if params:
            assert len(hyp.speakers) <= 2 and e["diarization"]["n_speakers"] == 2
        assert e["models"]["asr"]["key"] == "faster-whisper-small.en" and e["models"]["asr"]["sha256_matches_pin"]
        # the digests recorded are computed from the loaded files (equal to the pins)
        assert e["models"]["asr"]["files"]["model.bin"]["sha256"].startswith("62b2a45b")
        assert e["models"]["segmentation"]["files"]["model.onnx"]["sha256"].startswith("220ad67c")
        assert e["diarization"]["hint_honoured"] is (True if params else None)
        assert e["timing"]["audio_s"] == pytest.approx(CLIP_T1 - CLIP_T0)
        assert e["timing"]["rtf"] > 0
        assert paths["rttm"].read_text().startswith(f"SPEAKER {wav.stem} 1 ")


# --- the threshold-sweep replay of sherpa-onnx (pipeline/sherpa_sweep.py) -----------------


def test_topk_index_follows_libcxx_partial_sort_on_ties():
    """sherpa-onnx's TopkIndex is std::partial_sort with vec[a] > vec[b],
    which is not stable; on macOS (libc++) a tie at the boundary goes where
    the heap puts it, not to the lower index."""
    from pipeline.sherpa_sweep import topk_index

    # make_heap([0, 1]) sifts index 1 up on the tie (libc++ __sift_down moves unless the
    # child compares strictly lower), so index 1 is the heap top and index 2 replaces it
    assert sorted(topk_index(np.array([1, 1, 2]), 2)) == [0, 2]
    assert sorted(topk_index(np.array([1, 1, 1]), 1)) == [0]
    assert sorted(topk_index(np.array([0, 3, 3, 1, 3]), 2)) == [1, 2]
    assert topk_index(np.array([5]), 0) == [] and sorted(topk_index(np.array([2, 1]), 5)) == [0, 1]
    for seed in range(20):
        v = np.random.default_rng(seed).integers(0, 4, size=7)
        for k in range(8):
            got = topk_index(v, k)
            assert len(got) == min(k, 7) and len(set(got)) == len(got)
            if 0 < k < 7:  # a valid top-k set: nothing outside beats anything inside
                assert min(v[got]) >= max(v[[i for i in range(7) if i not in got]])


@pytest.mark.timeout(900)
def test_real_sherpa_replay_equals_sherpa_onnx_at_several_thresholds(clip):
    """The calibration replays sherpa-onnx from cached embeddings; its turns
    must be exactly sherpa-onnx's (checked on the full IS1008a dev meeting at
    five thresholds, build/v5-calib/validation/)."""
    if not _need_real(SEGMENTATION_ASSET, EMBEDDING_ASSET, packages=("sherpa_onnx", "onnxruntime"), entry="sherpa-onnx-diarization"):
        return
    import types

    from pipeline.sherpa_sweep import SherpaStages
    from pipeline.whisper_sherpa import sherpa_turns

    _, x = clip
    d = SherpaOnnxDiarizer()
    st = SherpaStages(d.segmentation_path, d.embedding_path, num_threads=d.settings["num_threads"])
    pre = st.precompute(x)
    for t in (0.5, 0.9, 1.1):
        real = SherpaOnnxDiarizer(cluster_threshold=t).diarize(x, SR)
        raw = st.turns_for(pre, t)
        replay = sherpa_turns([types.SimpleNamespace(start=a, end=b, speaker=s) for a, b, s in raw], x.size / SR)
        assert replay == real, t
