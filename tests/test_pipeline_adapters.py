"""The optional adapters' own logic, against stand-in modules (no models).

faster-whisper, pyannote.audio, whisperx, torch and speechbrain are NOT
installed here and no model may be downloaded, so these tests install small
stand-ins into ``sys.modules`` (monkeypatched, so they vanish after each
test) shaped after each library's documented public API.  What they prove:
the adapters normalise both pyannote.audio 3.x (an Annotation) and 4.x (an
output object with ``.speaker_diarization``) outputs, fall back between the
``token=``/``use_auth_token=`` spellings, forward ``vad_filter``, keep words
whisperx could not align, and that the embedding-cluster diarizer's
plumbing works with any embedder.  What they do NOT prove: that the real
libraries behave like the stand-ins -- that needs the packages and models
(docs/pipeline.md section 8).
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))

from pyannote.core import Annotation, Segment

from pipeline import PipelineConfig, get_pipeline, registered
from pipeline.base import ParamError
from pipeline import adapters
from pipeline.adapters import EmbeddingClusterDiarizer, pyannote_annotation, repair_word_times, whisperx_segments
from pipeline.synthetic import render_layout

SR = 16000
LAYOUT = [(0.5, 2.5, "A"), (3.0, 5.0, "B"), (5.5, 7.0, "A"), (7.5, 8.5, "B")]


# --- stand-ins -------------------------------------------------------------------------------------


class _Tensor:
    def __init__(self, a):
        self.a = np.asarray(a)

    def unsqueeze(self, i):
        return _Tensor(np.expand_dims(self.a, i))

    def squeeze(self, i):
        return _Tensor(np.squeeze(self.a, axis=i))

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _torch_module():
    t = types.ModuleType("torch")
    t.from_numpy = lambda a: _Tensor(a)
    t.tensor = lambda x: _Tensor(np.asarray(x))
    t.device = lambda name: name
    t.no_grad = _NoGrad
    return t


def _annotation(turns):
    a = Annotation()
    for s, e, lab in turns:
        a[Segment(s, e)] = lab
    return a


@dataclass
class DiarizeOutput:  # pyannote.audio 4.x shape (README: output.speaker_diarization)
    speaker_diarization: Annotation
    exclusive_speaker_diarization: Annotation


def _pyannote_module(major: int, calls: dict, overlap=None, exclusive=None):
    overlap = overlap or [(0.0, 1.0, "SPEAKER_00"), (1.2, 2.0, "SPEAKER_01")]
    exclusive = exclusive or overlap

    if major >= 4:

        class Pipeline:
            @classmethod
            def from_pretrained(cls, checkpoint, *, revision=None, token=None, cache_dir=None):
                calls["from_pretrained"] = {"checkpoint": checkpoint, "token": token}
                return cls()

            def to(self, device):
                calls["device"] = device
                return self

            def __call__(self, file, num_speakers=None, min_speakers=None, max_speakers=None):
                calls["call"] = {"keys": sorted(file), "num_speakers": num_speakers}
                return DiarizeOutput(_annotation(overlap), _annotation(exclusive))

    else:

        class Pipeline:  # 3.x: use_auth_token, returns the Annotation itself
            @classmethod
            def from_pretrained(cls, checkpoint_path, hparams_file=None, use_auth_token=None, cache_dir=None):
                calls["from_pretrained"] = {"checkpoint": checkpoint_path, "use_auth_token": use_auth_token}
                return cls()

            def __call__(self, file, num_speakers=None, min_speakers=None, max_speakers=None):
                calls["call"] = {"keys": sorted(file), "num_speakers": num_speakers}
                return _annotation(overlap)

    m = types.ModuleType("pyannote.audio")
    m.Pipeline = Pipeline
    return m


def _faster_whisper_module(calls: dict):
    @dataclass
    class Word:
        start: float
        end: float
        word: str
        probability: float = 0.9

    @dataclass
    class Seg:
        start: float
        end: float
        text: str
        words: list
        temperature: float = 0.0

    class WhisperModel:
        def __init__(self, model_size_or_path, device="auto", compute_type="default"):
            calls["init"] = {"model": model_size_or_path, "device": device, "compute_type": compute_type}

        def transcribe(self, audio, beam_size=5, word_timestamps=False, language=None, vad_filter=False,
                       temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
            calls["transcribe"] = {"beam_size": beam_size, "word_timestamps": word_timestamps,
                                   "language": language, "vad_filter": vad_filter, "dtype": str(audio.dtype),
                                   "temperature": temperature}
            segs = [Seg(0.5, 2.0, " Hello, World!", [Word(0.5, 1.0, " Hello,"), Word(1.1, 2.0, " World!")]),
                    Seg(3.0, 4.5, " Yes.", [Word(3.0, 4.5, " Yes.")], 0.4)]
            return iter(segs), {"language": "en"}

    m = types.ModuleType("faster_whisper")
    m.WhisperModel = WhisperModel
    return m


def _whisperx_module(calls: dict, *, token_kwarg: str = "use_auth_token"):
    class _M:
        def transcribe(self, audio, batch_size=16):
            return {"language": "en", "segments": [{"start": 0.0, "end": 2.0, "text": "we shipped 2024 units"}]}

    def align(segments, model, metadata, audio, device, return_char_alignments=False):
        # whisperx leaves a token with no characters in the align model's dictionary unaligned
        return {"segments": [
            {"start": 0.0, "end": 2.0, "text": "we shipped 2024 units", "words": [
                {"word": "we", "start": 0.0, "end": 0.3}, {"word": "shipped", "start": 0.35, "end": 0.9},
                {"word": "2024"}, {"word": "units", "start": 1.5, "end": 2.0}]},
            {"start": 3.0, "end": 4.0, "text": "3.5 %", "words": [{"word": "3.5"}, {"word": "%"}]},
        ]}

    def assign_word_speakers(diarize_df, result):
        for s in result["segments"]:
            s["speaker"] = "SPEAKER_00" if s["start"] < 2.5 else "SPEAKER_01"
            for w in s["words"]:
                if "start" in w:
                    w["speaker"] = s["speaker"]
        return result

    class DiarizationPipeline:
        def __init__(self, model_name=None, device="cpu", **kw):
            if set(kw) != {token_kwarg}:
                raise TypeError(f"unexpected keyword arguments {sorted(kw)}")
            calls["diarization_kwarg"] = token_kwarg

        def __call__(self, audio, min_speakers=None, max_speakers=None):
            calls["diarize"] = {"min_speakers": min_speakers, "max_speakers": max_speakers}
            return None

    m = types.ModuleType("whisperx")
    m.load_model = lambda name, device, compute_type="int8": _M()
    m.load_align_model = lambda language_code, device: (object(), {})
    m.align = align
    m.assign_word_speakers = assign_word_speakers
    diar = types.ModuleType("whisperx.diarize")
    diar.DiarizationPipeline = DiarizationPipeline  # >= 3.3 location only
    return m, diar


def _band_embed(clips, sr):
    """A stand-in 'speaker embedding': log energy in 12 bands (enough to tell the
    pulse-train voices apart, which differ in pass band)."""
    edges = np.linspace(100, 4000, 13)
    out = []
    for c in clips:
        spec = np.abs(np.fft.rfft(np.asarray(c, float), n=4096)) ** 2
        f = np.fft.rfftfreq(4096, 1.0 / sr)
        out.append([np.log(spec[(f >= a) & (f < b)].sum() + 1e-9) for a, b in zip(edges[:-1], edges[1:])])
    v = np.asarray(out)
    return v - v.mean(axis=1, keepdims=True)  # cosine-friendly: centre each vector


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "x.wav"
    sf.write(str(p), render_layout(LAYOUT), SR, subtype="PCM_16")
    return p


# --- pyannote.audio ----------------------------------------------------------------------------------


def test_pyannote_annotation_normalises_3x_and_4x_outputs():
    ann = _annotation([(0.0, 1.0, "A")])
    excl = _annotation([(0.0, 0.5, "A")])
    assert pyannote_annotation(ann) is ann
    out = DiarizeOutput(ann, excl)
    assert pyannote_annotation(out) is ann
    assert pyannote_annotation(out, exclusive=True) is excl
    with pytest.raises(Exception, match="unsupported"):
        pyannote_annotation(object())


def test_pyannote_4x_output_object_is_read_not_crashed_on(monkeypatch, wav):
    """Review finding PIPE-05: with pyannote.audio 4.x the adapter called
    .itertracks on a DiarizeOutput and raised AttributeError."""
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "pyannote.audio", _pyannote_module(4, calls))
    monkeypatch.setattr(adapters, "_major_version", lambda dist: 4)
    monkeypatch.setenv("HF_TEST_TOKEN", "not-a-real-token")
    assert registered()["pyannote-audio"].available()
    pipe = get_pipeline("pyannote-audio", PipelineConfig(params={"token_env": "HF_TEST_TOKEN", "num_speakers": 2}))
    hyp = pipe.run(wav)
    assert [(s["speaker"], s["start"], s["end"]) for s in hyp.segments] == [("SPEAKER_00", 0.0, 1.0), ("SPEAKER_01", 1.2, 2.0)]
    assert calls["from_pretrained"] == {"checkpoint": "pyannote/speaker-diarization-community-1", "token": "not-a-real-token"}
    assert calls["call"] == {"keys": ["sample_rate", "waveform"], "num_speakers": 2}


def test_pyannote_3x_annotation_and_use_auth_token_still_work(monkeypatch, wav):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "pyannote.audio", _pyannote_module(3, calls))
    monkeypatch.setattr(adapters, "_major_version", lambda dist: 3)
    hyp = get_pipeline("pyannote-audio").run(wav)
    assert len(hyp.segments) == 2
    assert calls["from_pretrained"]["checkpoint"] == "pyannote/speaker-diarization-3.1"
    assert "use_auth_token" in calls["from_pretrained"]


# --- faster-whisper ----------------------------------------------------------------------------------


def test_faster_whisper_transcriber_emits_the_contract(monkeypatch, wav, tmp_path):
    """Review finding PIPE-10: no test exercised FasterWhisperTranscriber.
    The model is a local CTranslate2 directory: since 2026-09-25 a library
    size name such as "tiny" is resolved from the Hugging Face cache only
    (tests/test_pipeline_models.py covers that path)."""
    calls: dict = {}
    model_dir = tmp_path / "fw-ct2-model"
    model_dir.mkdir()
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_whisper_module(calls))
    pipe = get_pipeline("faster-whisper", PipelineConfig(params={"model": str(model_dir), "language": "en", "num_speakers": 2}))
    hyp = pipe.run(wav)
    assert calls["init"] == {"model": str(model_dir), "device": "cpu", "compute_type": "int8"}
    assert calls["transcribe"]["word_timestamps"] is True and calls["transcribe"]["dtype"] == "float32"
    assert calls["transcribe"]["vad_filter"] is False
    words = [w["w"] for s in hyp.segments for w in s["words"]]
    assert words == ["hello", "world", "yes"]
    assert all(s["text"] == s["text"].lower() for s in hyp.segments)
    assert hyp.extra["components"] == {"transcriber": "faster-whisper", "diarizer": "energy-vad-cluster"}
    # an unknown key is refused, a diarizer key is accepted
    with pytest.raises(Exception, match="does not accept"):
        get_pipeline("faster-whisper", PipelineConfig(params={"diarization_model": "x"}))
    assert get_pipeline("faster-whisper", PipelineConfig(params={"chunk_s": 1.0, "model": str(model_dir)}))


def test_faster_whisper_seed_and_temperature_are_forwarded_and_recorded(monkeypatch, wav, tmp_path):
    """ASR repeatability (docs/pipeline.md §2): the transcriber passes the
    temperature schedule through, records the seed, and counts the segments
    each temperature produced; the stand-in's second segment needed the
    sampled fallback (temperature 0.4)."""
    from pipeline.adapters import DEFAULT_FW_SEED, FW_LIBRARY_TEMPERATURES, _faster_whisper_from_params

    calls: dict = {}
    model_dir = tmp_path / "fw-ct2-model"
    model_dir.mkdir()
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_whisper_module(calls))
    asr = _faster_whisper_from_params({"model": str(model_dir)})
    assert asr.settings["seed"] == DEFAULT_FW_SEED == 0
    assert asr.settings["temperature"] == list(FW_LIBRARY_TEMPERATURES)
    asr.transcribe(np.zeros(16000 * 5, dtype=np.float32), 16000)
    assert calls["transcribe"]["temperature"] == list(FW_LIBRARY_TEMPERATURES)
    assert asr.last_info["temperatures"] == {"0.0": 1, "0.4": 1}
    assert asr.last_info["fallback_segments"] == 1

    greedy = _faster_whisper_from_params({"model": str(model_dir), "temperature": 0, "seed": None})
    greedy.transcribe(np.zeros(16000 * 5, dtype=np.float32), 16000)
    assert calls["transcribe"]["temperature"] == 0.0
    assert greedy.settings["seed"] is None and greedy.settings["seed_applied"] is False

    for bad in ({"seed": -1}, {"seed": 1.5}, {"seed": True}, {"seed": 2**32}, {"temperature": -0.1},
                {"temperature": []}, {"temperature": [0.0, "x"]}, {"temperature": float("nan")}):
        with pytest.raises(ParamError):
            _faster_whisper_from_params({"model": str(model_dir), **bad})
    assert registered()["whisper-sherpa"].params >= {"seed", "temperature"}


def test_faster_whisper_pyannote_forwards_vad_filter_and_uses_the_exclusive_timeline(monkeypatch, wav, tmp_path):
    """Review finding PIPE-05 (3): the combined factory dropped vad_filter.  Words
    are attributed on pyannote 4.x's exclusive (one-speaker-at-a-time) timeline."""
    calls: dict = {}
    pcalls: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_whisper_module(calls))
    overlap = [(0.0, 5.0, "SPEAKER_00"), (0.4, 5.0, "SPEAKER_01")]  # overlap-aware: both talk
    exclusive = [(0.0, 2.5, "SPEAKER_00"), (2.5, 5.0, "SPEAKER_01")]
    monkeypatch.setitem(sys.modules, "pyannote.audio", _pyannote_module(4, pcalls, overlap, exclusive))
    model_dir = tmp_path / "fw-ct2-model"  # a local model: the default small.en is never downloaded implicitly
    model_dir.mkdir()
    hyp = get_pipeline("faster-whisper+pyannote", PipelineConfig(params={"vad_filter": True, "model": str(model_dir)})).run(wav)
    assert calls["transcribe"]["vad_filter"] is True
    assert [(s["speaker"], s["text"]) for s in hyp.segments] == [("SPEAKER_00", "hello world"), ("SPEAKER_01", "yes")]


# --- whisperx ----------------------------------------------------------------------------------------


def test_repair_word_times_interpolates_between_aligned_neighbours():
    ws, n = repair_word_times(
        [{"word": "a", "start": 0.0, "end": 1.0, "speaker": "S1"}, {"word": "2024"}, {"word": "b", "start": 2.0, "end": 3.0}],
        0.0, 3.0, "S0",
    )
    assert n == 1 and (ws[1]["start"], ws[1]["end"], ws[1]["speaker"]) == (1.0, 2.0, "S1")
    ws, n = repair_word_times([{"word": "x"}, {"word": "y"}], 4.0, 5.0, "S0")
    assert n == 2 and [(w["start"], w["end"]) for w in ws] == [(4.0, 4.5), (4.5, 5.0)]
    ws, n = repair_word_times([{"word": "a", "start": 2.0, "end": 3.0}, {"word": "z"}], 0.0, 2.5, None)
    assert n == 1 and ws[1]["start"] == ws[1]["end"] == 3.0  # never runs backwards


def test_whisperx_keeps_words_it_could_not_align(monkeypatch, wav):
    """Review finding PIPE-07: 'we shipped 2024 units' became 'we shipped units',
    and a segment with no aligned word fell back to its whole text."""
    calls: dict = {}
    wx, diar = _whisperx_module(calls)
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setitem(sys.modules, "whisperx.diarize", diar)
    hyp = get_pipeline("whisperx", PipelineConfig(params={"num_speakers": 2})).run(wav)
    assert [s["text"] for s in hyp.segments] == ["we shipped 2024 units", "3.5 %"]
    w2024 = hyp.segments[0]["words"][2]
    assert w2024["w"] == "2024" and (w2024["start"], w2024["end"]) == (0.9, 1.5)
    assert [(w["w"], w["start"], w["end"]) for w in hyp.segments[1]["words"]] == [("3.5", 3.0, 3.5), ("%", 3.5, 4.0)]
    assert hyp.segments[1]["speaker"] == "SPEAKER_01"
    assert hyp.extra["whisperx"]["interpolated_words"] == 3
    assert calls["diarize"] == {"min_speakers": 2, "max_speakers": 2}
    assert calls["diarization_kwarg"] == "use_auth_token"


def test_whisperx_diarization_pipeline_token_rename_is_guarded(monkeypatch, wav):
    """Review finding PIPE-05 (2): DiarizationPipeline(use_auth_token=...) had no
    fallback for a token= rename."""
    calls: dict = {}
    wx, diar = _whisperx_module(calls, token_kwarg="token")
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setitem(sys.modules, "whisperx.diarize", diar)
    get_pipeline("whisperx").run(wav)
    assert calls["diarization_kwarg"] == "token"


def test_whisperx_segments_without_any_words_keep_their_text():
    segs, n = whisperx_segments({"segments": [{"start": 0.0, "end": 1.0, "text": "Hi.", "speaker": "S", "words": []}]})
    assert n == 0 and [(s["speaker"], s["text"]) for s in segs] == [("S", "hi")]


# --- embedding-cluster ---------------------------------------------------------------------------------


def test_embedding_cluster_diarizer_with_a_standin_embedder():
    x = render_layout(LAYOUT)
    d = EmbeddingClusterDiarizer(_band_embed)
    for hint in (None, 2):
        turns = d.diarize(x, SR, num_speakers=hint)
        labels = [t[2] for t in turns]
        assert len(labels) == 4 and labels[0] == labels[2] and labels[1] == labels[3] and labels[0] != labels[1], (hint, turns)
    assert d.last_info["count"]["k"] == 2


def test_embedding_cluster_registry_entry_with_standin_speechbrain(monkeypatch, wav):
    calls: dict = {}

    class EncoderClassifier:
        @classmethod
        def from_hparams(cls, source, savedir=None, run_opts=None):
            calls["from_hparams"] = {"source": source, "savedir": savedir, "run_opts": run_opts}
            return cls()

        def encode_batch(self, wavs, wav_lens=None):
            batch = wavs.a
            lens = np.asarray(wav_lens.a)
            clips = [row[: max(1, int(round(l * batch.shape[1])))] for row, l in zip(batch, lens)]
            return _Tensor(_band_embed(clips, SR)[:, None, :])

    sb = types.ModuleType("speechbrain")
    inf = types.ModuleType("speechbrain.inference")
    spk = types.ModuleType("speechbrain.inference.speaker")
    spk.EncoderClassifier = EncoderClassifier
    monkeypatch.setitem(sys.modules, "torch", _torch_module())
    monkeypatch.setitem(sys.modules, "speechbrain", sb)
    monkeypatch.setitem(sys.modules, "speechbrain.inference", inf)
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", spk)
    hyp = get_pipeline("embedding-cluster", PipelineConfig(params={"batch_windows": 5})).run(wav)
    assert calls["from_hparams"]["source"] == "speechbrain/spkrec-ecapa-voxceleb"
    assert len(hyp.speakers) == 2 and all(s["text"] == "" for s in hyp.segments)
    with pytest.raises(Exception, match="does not accept"):
        get_pipeline("embedding-cluster", PipelineConfig(params={"f0_weight": 1.0}))


# --- the real package, once installed ---------------------------------------------------------------


def test_real_faster_whisper_on_a_generator_clip(tmp_path):
    """Runs against a real model only when faster-whisper is installed AND
    ``PLAUD_HARNESS_FW_MODEL`` names a model (a size such as ``tiny`` or a local
    CTranslate2 directory), so CI never downloads weights implicitly.  Without
    the package it checks the registry says so (no skip: tests/conftest.py's
    strict mode refuses undocumented skips).  The generator's formant voices
    are not intelligible, so this asserts the contract, not the words; a
    non-empty-words assertion belongs with an intelligible TTS voice."""
    import os

    from pipeline import PipelineUnavailable

    if not registered()["faster-whisper"].available():
        with pytest.raises(PipelineUnavailable, match="faster_whisper"):
            get_pipeline("faster-whisper")
        return
    model = os.environ.get("PLAUD_HARNESS_FW_MODEL")
    if not model:  # pragma: no cover - only once the package is installed
        pytest.skip("faster-whisper is importable here; set PLAUD_HARNESS_FW_MODEL to run it against a real model")
    from generator.testing import fixture_meeting

    d, m = fixture_meeting(tmp_path, preset="smoke", seed=3)
    # Setting the variable is the opt-in: a size name may be downloaded (unpinned, no token).
    params = {"model": model, "language": "en", "allow_download": not Path(model).is_dir()}
    hyp = get_pipeline("faster-whisper", PipelineConfig(params=params)).run(d / m["audio"]["mix_wav"], d)
    hyp.validate()
    assert hyp.meeting_id == m["meeting_id"]
    for s in hyp.segments:
        assert s["text"] == s["text"].lower()
        for w in s.get("words", []):
            assert w["w"] and w["w"] == w["w"].lower() and 0.0 <= w["start"] <= w["end"] <= m["duration_s"] + 0.5


def test_real_faster_whisper_seeded_sampling_repeats_across_processes(tmp_path):
    """ASR repeatability (docs/pipeline.md §2).  Forces the sampled path
    (``temperature=[0.8]`` samples every window) and runs the transcriber in
    two fresh processes with the same seed: the words must be identical.
    Same opt-in as the test above: a real model only when faster-whisper is
    installed and ``PLAUD_HARNESS_FW_MODEL`` names one."""
    import json
    import os
    import subprocess

    if not registered()["faster-whisper"].available():
        return  # covered by test_real_faster_whisper_on_a_generator_clip's registry check
    model = os.environ.get("PLAUD_HARNESS_FW_MODEL")
    if not model:  # pragma: no cover - only once the package is installed
        pytest.skip("faster-whisper is importable here; set PLAUD_HARNESS_FW_MODEL to run it against a real model")
    from generator.testing import fixture_meeting

    d, m = fixture_meeting(tmp_path, preset="smoke", seed=3)
    wav = d / m["audio"]["mix_wav"]
    code = (
        "import json, sys, soundfile as sf\n"
        "from pipeline.adapters import _faster_whisper_from_params\n"
        "p = json.loads(sys.argv[1]); x, sr = sf.read(sys.argv[2], dtype='float32')\n"
        "t = _faster_whisper_from_params(p)\n"
        "segs = t.transcribe(x if x.ndim == 1 else x.mean(axis=1), sr)\n"
        "print(json.dumps({'words': [w['w'] for s in segs for w in s['words']], 'info': t.last_info}))\n"
    )
    params = {"model": model, "language": "en", "temperature": [0.8], "seed": 0,
              "allow_download": not Path(model).is_dir()}
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def once() -> dict:
        r = subprocess.run([sys.executable, "-c", code, json.dumps(params), str(wav)], cwd=root, env=env,
                           capture_output=True, text=True, timeout=600, check=True)
        return json.loads(r.stdout.strip().splitlines()[-1])

    a, b = once(), once()
    assert a["info"]["temperatures"] and set(a["info"]["temperatures"]) == {"0.8"}  # every segment sampled
    assert a["words"] == b["words"]


def test_asr_cache_returns_the_stored_transcript_for_the_same_audio_and_settings(monkeypatch, tmp_path):
    """``asr_cache`` (scripts/run-v5.sh): the first transcribe decodes and
    stores; the same samples and settings hit the entry without decoding;
    different samples or settings miss.  A corrupted entry is refused."""
    from pipeline.adapters import ASR_CACHE_SCHEMA, _faster_whisper_from_params

    calls: dict = {}
    model_dir = tmp_path / "fw-ct2-model"
    model_dir.mkdir()
    cache = tmp_path / "asr-cache"
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_whisper_module(calls))
    x = np.linspace(-0.1, 0.1, 16000 * 5, dtype=np.float32)
    a = _faster_whisper_from_params({"model": str(model_dir), "asr_cache": str(cache)})
    first = a.transcribe(x, 16000)
    assert a.last_info["cache"]["hit"] is False and "transcribe" in calls
    entry = cache / f"{a.last_info['cache']['key']}.json"
    assert json.loads(entry.read_text())["schema"] == ASR_CACHE_SCHEMA
    calls.clear()
    b = _faster_whisper_from_params({"model": str(model_dir), "asr_cache": str(cache)})
    assert b.transcribe(x, 16000) == first and b.last_info["cache"]["hit"] is True
    assert "transcribe" not in calls  # no decode on a hit
    assert b.last_info["temperatures"] == a.last_info["temperatures"]
    # other samples, or other decoding settings, are other entries
    b.transcribe(x[:-1], 16000)
    assert b.last_info["cache"]["hit"] is False
    c = _faster_whisper_from_params({"model": str(model_dir), "asr_cache": str(cache), "beam_size": 1})
    c.transcribe(x, 16000)
    assert c.last_info["cache"]["hit"] is False
    # a tampered entry is refused, not silently used
    doc = json.loads(entry.read_text())
    doc["key"] = "0" * 64
    entry.write_text(json.dumps(doc))
    with pytest.raises(Exception, match="not an asr_cache entry"):
        _faster_whisper_from_params({"model": str(model_dir), "asr_cache": str(cache)}).transcribe(x, 16000)
    for bad in ("", 3):
        with pytest.raises(ParamError):
            _faster_whisper_from_params({"model": str(model_dir), "asr_cache": bad})
    (tmp_path / "file").write_text("x")
    with pytest.raises(ParamError, match="not a directory"):
        _faster_whisper_from_params({"model": str(model_dir), "asr_cache": str(tmp_path / "file")})
