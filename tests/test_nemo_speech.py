"""pipeline/nemo_speech.py against a stand-in ``nemo-speech`` executable.

The stand-in answers the four CLI calls the adapters make (``--version``,
``pull``, ``transcribe --json [--diarize]``, ``diarize --format rttm``) with
fixed outputs in NeMo-Speech.cpp 0.1.0's formats and logs its argv, so the
adapters' parsing, provenance, caching and flags are tested without models.
The real runtime is exercised by docs/nvidia-speech.md's runs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pipeline import nemo_speech as ns
from pipeline.base import PipelineConfig, PipelineUnavailable, get_pipeline, registered

FAKE = r'''#!{python}
import json, os, sys
from pathlib import Path
d = Path(os.environ["FAKE_NEMO_DIR"])
with open(d / "argv.log", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("nemo-speech 0.1.0-fake")
elif a[:1] == ["pull"]:
    name = a[1]
    rev = d / "models" / name / "0123abcd"
    rev.mkdir(parents=True, exist_ok=True)
    g = rev / (name + ".q8_0.gguf")
    g.write_bytes(b"GGUF" + name.encode())
    print("[model] ready: " + str(g))
    print("nvidia/" + name + "\t" + ("diarization" if "diar" in name else "asr") + "\t" + str(g))
elif a[:1] == ["transcribe"]:
    words = [
        {{"word": "Hello,", "start": 0.10, "end": 0.30, "speaker": 1}},
        {{"word": "world.", "start": 0.40, "end": 0.60, "speaker": 1}},
        {{"word": "(uh)", "start": 0.62, "end": 0.64, "speaker": 1}},
        {{"word": "Yes", "start": 1.50, "end": 1.70, "speaker": 2}},
        {{"word": "it's-fine", "start": 1.80, "end": 2.20, "speaker": 2}},
        {{"word": "late", "start": 2.90, "end": 3.50, "speaker": 2}},
    ]
    if "--diar-model" not in a:
        for w in words:
            del w["speaker"]
    print(json.dumps({{"file": a[1], "text": "x", "languages": ["en-US"], "words": words}}))
elif a[:1] == ["diarize"]:
    out = Path(a[a.index("--output") + 1])
    out.write_text("SPEAKER audio 1 0.000 0.700 <NA> <NA> speaker_1 <NA> <NA>\n"
                   "SPEAKER audio 1 1.400 1.600 <NA> <NA> speaker_2 <NA> <NA>\n"
                   "SPEAKER audio 1 2.000 0.000 <NA> <NA> speaker_2 <NA> <NA>\n")
else:
    sys.exit(2)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    b = d / "nemo-speech"
    b.write_text(FAKE.format(python=sys.executable))
    b.chmod(0o755)
    monkeypatch.setenv("NEMO_SPEECH_BIN", str(b))
    monkeypatch.setenv("FAKE_NEMO_DIR", str(d))
    monkeypatch.setattr(ns, "_RUNTIME_CACHE", {})
    monkeypatch.setattr(ns, "_MODEL_CACHE", {})
    return d


def argv(d: Path) -> list[list[str]]:
    return [json.loads(line) for line in (d / "argv.log").read_text().splitlines()]


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    t = np.arange(int(3.2 * 16000)) / 16000.0
    sf.write(str(p), (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 16000, subtype="PCM_16")
    return p


def test_words_to_segments_normalises_splits_on_pauses_and_clips():
    words = [{"word": "Hello,", "start": 0.1, "end": 0.3}, {"word": "big-world", "start": 0.4, "end": 0.8},
             {"word": "...", "start": 0.9, "end": 1.0}, {"word": "Late", "start": 1.5, "end": 9.0}]
    segs = ns.words_to_segments(words, gap_s=0.5, duration_s=2.0)
    assert [s["text"] for s in segs] == ["hello big-world", "late"]
    assert segs[0]["words"][1] == {"w": "big-world", "start": 0.4, "end": 0.8}
    assert segs[1]["end"] == 2.0  # clipped to the audio
    assert ns.words_to_segments([], 0.5, 1.0) == []


def test_parse_rttm_keeps_positive_turns_sorted():
    rttm = ("SPEAKER a 1 2.0 1.0 <NA> <NA> s2 <NA> <NA>\n"
            "SPEAKER a 1 0.5 0.0 <NA> <NA> s1 <NA> <NA>\n"
            "SPEAKER a 1 0.0 1.5 <NA> <NA> s1 <NA> <NA>\n# comment\n")
    assert ns.parse_rttm(rttm) == [(0.0, 1.5, "s1"), (2.0, 3.0, "s2")]


def test_transcriber_parses_words_records_provenance_and_caches(fake, tmp_path):
    t = ns.NemoSpeechTranscriber(cache_dir=tmp_path / "cache")
    pcm = np.zeros(int(3.2 * 16000), dtype=np.float32)
    segs = t.transcribe(pcm, 16000)
    assert [s["text"] for s in segs] == ["hello world uh", "yes it's-fine", "late"]
    assert t.model_provenance["repo"] == "nvidia/nemotron-3.5" and len(t.model_provenance["sha256"]) == 64
    assert t.runtime["version"] == "nemo-speech 0.1.0-fake" and "nemo-speech" in t.runtime["files"]
    assert t.last_info["cache"] == {"hit": False, "key": t.cache_key(pcm)}
    assert t.last_info["cli"]["wall_s"] > 0 and t.last_info["n_words"] == 6
    call = argv(fake)[-1]
    assert call[0] == "transcribe" and "--json" in call and call[call.index("--language") + 1] == "en-US"
    assert "--stream" not in call and "--diar-model" not in call
    n_calls = len(argv(fake))
    assert t.transcribe(pcm, 16000) == segs and t.last_info["cache"]["hit"] is True
    assert len(argv(fake)) == n_calls  # the hit ran no CLI


def test_transcriber_stream_flag_and_16k_only(fake):
    t = ns.NemoSpeechTranscriber(stream=True, language=None)
    t.transcribe(np.zeros(16000, dtype=np.float32), 16000)
    call = argv(fake)[-1]
    assert "--stream" in call and "--language" not in call
    assert "--asr.streaming.rnnt_right_context" not in call and "right_context" not in t.settings
    with pytest.raises(PipelineUnavailable):
        t.transcribe(np.zeros(8000, dtype=np.float32), 8000)


def test_right_context_reaches_the_cli_and_the_cache_key(fake):
    pcm = np.zeros(16000, dtype=np.float32)
    t13 = ns.NemoSpeechTranscriber(right_context=13)
    t13.transcribe(pcm, 16000)
    call = argv(fake)[-1]
    assert call[call.index("--asr.streaming.rnnt_right_context") + 1] == "13"
    assert t13.cache_key(pcm) != ns.NemoSpeechTranscriber().cache_key(pcm)
    hyp_params = registered()["nemotron-asr"].params
    assert "right_context" in hyp_params and "diar_model" not in hyp_params


def test_diarizer_turns_flags_and_unhonourable_hint(fake):
    d = ns.NemoSpeechDiarizer(preset="v3-offline", post={"onset": 0.6, "min_duration_on": 0.2})
    turns = d.diarize(np.zeros(16000 * 3, dtype=np.float32), 16000, num_speakers=3)
    assert turns == [(0.0, 0.7, "speaker_1"), (1.4, 3.0, "speaker_2")]
    assert d.last_info["num_speakers_hint"] == 3 and d.last_info["n_speakers"] == 2
    assert d.last_info["hint_honoured"] is False and d.last_info["n_turns"] == 2
    call = argv(fake)[-1]
    assert call[call.index("--preset") + 1] == "v3-offline"
    assert call[call.index("--onset") + 1] == "0.6" and call[call.index("--min-duration-on") + 1] == "0.2"
    assert d.last_info["command"][:3] == ["nemo-speech", "diarize", "<audio.wav>"]
    assert "<audio.rttm>" in d.last_info["command"] and "--force" in d.last_info["command"]
    assert d.models["diarization"]["repo"] == "nvidia/nemotron-diar"


def test_nemotron_pipeline_assigns_words_to_sortformer_turns(fake, wav):
    hyp = get_pipeline("nemotron").run(wav)
    assert [(s["speaker"], s["text"]) for s in hyp.segments] == [
        ("speaker_1", "hello world uh"), ("speaker_2", "yes it's-fine"), ("speaker_2", "late")]
    e = hyp.extra
    assert set(e["models"]) == {"asr", "diarization"}
    assert e["components"] == {"transcriber": "nemo-speech-asr", "diarizer": "nemo-speech-diarization"}
    assert e["diarization"]["turns"] == [[0.0, 0.7, "speaker_1"], [1.4, 3.0, "speaker_2"]]
    assert "rtf" in e["timing"]


def test_nemotron_tagged_uses_the_runtimes_word_speakers(fake, wav):
    hyp = get_pipeline("nemotron-tagged", PipelineConfig(params={"diar_preset": "v3-offline"})).run(wav)
    assert [(s["speaker"], s["text"]) for s in hyp.segments] == [
        ("spk1", "hello world uh"), ("spk2", "yes it's-fine"), ("spk2", "late")]  # 0.7 s pause splits
    call = argv(fake)[-1]
    assert call[0] == "transcribe" and "--diar-model" in call and call[call.index("--diar-preset") + 1] == "v3-offline"
    assert hyp.extra["diarization"]["n_speakers"] == 2 and "turns" not in hyp.extra["diarization"]
    assert hyp.extra["models"]["diarization"]["repo"] == "nvidia/nemotron-diar"


def test_unavailable_without_the_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("NEMO_SPEECH_BIN", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("nemo-speech-diarization", "nemotron", "nemotron-tagged"):
        assert "nemo-speech CLI not found" in (registered()[name].reason() or "")
    with pytest.raises(PipelineUnavailable):
        ns.NemoSpeechDiarizer()


def test_registered_params():
    r = registered()
    assert {"asr_model", "diar_model", "diar_preset", "stream", "asr_cache"} <= r["nemotron"].params
    assert "asr_cache" not in r["nemotron-tagged"].params
    assert {"diar_model", "onset", "min_duration_off"} <= r["nemo-speech-diarization"].params
    assert "cpu_threads" in r["faster-whisper+nemotron-diar"].params


def test_run_measured_reports_peak_memory():
    out, res = ns.run_measured([sys.executable, "-c", "x = bytearray(30_000_000); print('ok')"])
    assert out.returncode == 0 and out.stdout.strip() == "ok" and res["wall_s"] > 0
    if Path("/usr/bin/time").exists() and os.uname().sysname == "Darwin":
        assert res["max_rss_bytes"] > 30_000_000
