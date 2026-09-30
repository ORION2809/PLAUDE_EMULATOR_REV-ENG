"""pipeline/whisper_cpp.py against a stand-in ``whisper-cli`` that writes
whisper.cpp 1.9.4's ``-ojf`` JSON (docs/nvidia-speech.md)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from pipeline import whisper_cpp as wc
from pipeline.base import PipelineUnavailable, get_pipeline, registered

FAKE = r'''#!{python}
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_WCPP_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
def tok(text, a, b):
    return {{"text": text, "offsets": {{"from": a, "to": b}}, "id": 1, "p": 0.9, "t_dtw": -1}}
doc = {{"transcription": [
    {{"offsets": {{"from": 0, "to": 1500}}, "text": " Hello, it's me.", "tokens": [
        tok("[_BEG_]", 0, 0), tok(" Hello", 100, 400), tok(",", 400, 450), tok(" it", 500, 600),
        tok("'s", 600, 700), tok(" me", 800, 1000), tok(".", 1000, 1100), tok("[_TT_75]", 1500, 1500)]}},
    {{"offsets": {{"from": 2000, "to": 9000}}, "text": " ...", "tokens": [tok(" ...", 2000, 2500)]}},
    {{"offsets": {{"from": 2600, "to": 9000}}, "text": " Late", "tokens": [tok(" Late", 2600, 9000)]}}]}}
out = a[a.index("-of") + 1] + ".json"
open(out, "w").write(json.dumps(doc))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    b = tmp_path / "whisper-cli"
    b.write_text(FAKE.format(python=sys.executable))
    b.chmod(0o755)
    model = tmp_path / "ggml-small.en-q8_0.bin"
    model.write_bytes(b"ggml")
    log = tmp_path / "argv.log"
    monkeypatch.setenv("WHISPER_CPP_BIN", str(b))
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model))
    monkeypatch.setenv("FAKE_WCPP_LOG", str(log))
    return log


def test_tokens_to_words_merges_subword_tokens_and_skips_specials():
    toks = [{"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
            {"text": " Don", "offsets": {"from": 100, "to": 200}}, {"text": "'t", "offsets": {"from": 200, "to": 300}},
            {"text": " stop", "offsets": {"from": 350, "to": 600}}, {"text": "!", "offsets": {"from": 600, "to": 650}},
            {"text": " --", "offsets": {"from": 700, "to": 800}}]
    assert wc.tokens_to_words(toks, 10.0) == [{"w": "don't", "start": 0.1, "end": 0.3}, {"w": "stop", "start": 0.35, "end": 0.65}]
    assert wc.tokens_to_words(toks, 0.5)[-1] == {"w": "stop", "start": 0.35, "end": 0.5}  # clipped


def test_transcriber_segments_words_cache_and_command(fake, tmp_path):
    t = wc.WhisperCppTranscriber(cache_dir=tmp_path / "cache")
    pcm = np.zeros(16000 * 5, dtype=np.float32)
    segs = t.transcribe(pcm, 16000)
    assert [s["text"] for s in segs] == ["hello it's me", "late"]
    assert segs[1]["end"] == 5.0 and segs[0]["words"][1] == {"w": "it's", "start": 0.5, "end": 0.7}
    call = json.loads(fake.read_text().splitlines()[-1])
    assert call[call.index("-t") + 1] == "4" and call[call.index("-bs") + 1] == "5" and "-ojf" in call and "-ng" not in call
    assert t.model_provenance["bytes"] == 4 and len(t.model_provenance["sha256"]) == 64
    assert t.last_info["command"][:5] == ["whisper-cli", "-m", "<model>", "-f", "<audio.wav>"] and "<out>" in t.last_info["command"]
    n = len(fake.read_text().splitlines())
    assert t.transcribe(pcm, 16000) == segs and t.last_info["cache"]["hit"] is True
    assert len(fake.read_text().splitlines()) == n


def test_whisper_cpp_pipeline_and_no_gpu(fake, tmp_path):
    import soundfile as sf

    wav = tmp_path / "a.wav"
    sf.write(str(wav), np.zeros(16000 * 3, dtype=np.float32), 16000, subtype="PCM_16")
    from pipeline.base import PipelineConfig

    hyp = get_pipeline("whisper-cpp", PipelineConfig(params={"no_gpu": "true"})).run(wav)
    assert [s["text"] for s in hyp.segments] == ["hello it's me", "late"]
    assert "-ng" in json.loads(fake.read_text().splitlines()[-1])
    assert hyp.extra["models"]["asr"]["source"] == "ggml-small.en-q8_0.bin"


def test_unavailable_without_binary_or_model(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_CPP_BIN", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert "whisper-cli not found" in (registered()["whisper-cpp"].reason() or "")
    with pytest.raises(PipelineUnavailable):
        wc.WhisperCppTranscriber()


def test_whisper_cpp_sherpa_declares_both_parts():
    p = registered()["whisper-cpp+sherpa"].params
    assert {"wcpp_model", "cluster_threshold", "assignment_tie_break", "asr_cache"} <= p
