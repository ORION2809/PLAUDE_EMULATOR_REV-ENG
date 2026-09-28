"""Piper backend: real speech, word boundaries from the voice's own duration
alignment, and the V4 ground-truth round trip on a Piper meeting.

Two kinds of tests:
* stub tests (always run, CI included): the alignment arithmetic, the
  boundary mapping, the phonemization fallback, the voice palette and the
  generator wiring, driven through a fake `piper` module and loader;
* real-voice tests (local only): the installed piper-tts 1.8.0 with
  data/voices/piper/en_US-libritts_r-medium{.onnx,.onnx.json} and, preferably,
  the verified aligned copy (.aligned.onnx + .aligned.source.json). They skip
  with the reason prefix "piper voice unavailable:" when the package or the
  voice is absent (the CI case), or when the voice is present but cannot be
  loaded here (e.g. no aligned copy and no `onnx` to patch it in memory): the
  fixture constructs the backend AND loads the voice inside its try.
"""

from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import generator.tts.piper as piper_mod
from generator.contract import SAMPLE_RATE, parse_rttm, parse_stm
from generator.tts.base import BackendUnavailable
from generator.tts.piper import (
    LIBRITTS_R_MEDIUM_PALETTE,
    PALETTE_F0_TEXT,
    SIDECAR_SCHEMA,
    AlignedCopyIgnoredWarning,
    PaletteFallbackWarning,
    PaletteVoice,
    PiperBackend,
    build_phoneme_ids,
    check_aligned,
    check_text,
    default_load_plan,
    discover_models,
    map_index,
    median_f0,
    palette_indices,
    render_to_result,
    write_aligned_copy,
)

SKIP_PREFIX = "piper voice unavailable:"
VOICE_STEM = "en_US-libritts_r-medium"
#: sha256 of the voice this harness measured (docs/generator.md section 6).
ORIGINAL_SHA256 = "10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195"
ALIGNED_SHA256 = "10356488f1ae6019943f3d92034338bdfb43066c400d4186e101741293e742a0"
CONFIG_SHA256 = "b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3"
#: espeak-ng fuses these pairs ("fɚðə", "wɪððə"), forcing per-token mode.
MERGING_TEXT = "we start the review for the budget with the team"
CONTEXT_TEXT = "the budget review starts tomorrow morning"


# --- pure functions ------------------------------------------------------------------


def test_map_index_is_round_half_up_monotonic_and_within_half_a_sample() -> None:
    for up, down in ((320, 441), (2, 3), (16, 11), (1, 1)):
        prev = -1
        for x in range(0, 3 * down * 7 + 5):
            exact = Fraction(x * up, down)
            y = map_index(x, up, down)
            assert y == int(exact + Fraction(1, 2)), (up, down, x)
            assert abs(Fraction(y) - exact) <= Fraction(1, 2)
            assert y >= prev
            prev = y


def test_palette_indices_are_distinct_spread_and_alternate_parity() -> None:
    for size in (16, 10, 4):
        for n in range(1, size + 1):
            for seed in range(0, 3 * size):
                idx = palette_indices(n, seed, size)
                assert idx == palette_indices(n, seed, size)
                assert len(idx) == n and len(set(idx)) == n
                assert idx[0] == seed % size
                # consecutive speakers come from different pitch groups (even/odd slots)
                assert all((a - b) % 2 == 1 for a, b in zip(idx, idx[1:])), (size, n, seed, idx)
    # spread: two speakers sit 7 of 16 slots apart, four speakers 3 apart
    assert palette_indices(2, 0, 16) == [0, 7] and palette_indices(4, 5, 16) == [5, 8, 11, 14]
    with pytest.raises(ValueError):
        palette_indices(17, 0, 16)


def test_build_phoneme_ids_uses_pipers_layout_and_tags_owners() -> None:
    id_map = {"_": [0], "^": [1], "$": [2], " ": [3], ".": [10], "a": [20], "b": [21], "c": [22]}
    ids, owners, dropped = build_phoneme_ids([["a", "b"], ["c", "?"]], id_map)
    # piper.phoneme_ids.phonemes_to_ids(["a","b"," ","c","."]) -> BOS PAD (p PAD)* EOS
    assert ids == [1, 0, 20, 0, 21, 0, 3, 0, 22, 0, 10, 0, 2]
    assert owners == [-1, -1, 0, 0, 0, 0, -1, -1, 1, 1, -1, -1, -1]
    assert dropped == ["?"]
    with pytest.raises(ValueError, match="no phoneme"):
        build_phoneme_ids([["a"], ["?"]], id_map)


def test_check_text_enforces_the_generator_contract() -> None:
    assert check_text("hello there") == ["hello", "there"]
    for bad in ("", "Hello there", "two  spaces", " lead", "trail ", "don't", "tab\tsep"):
        with pytest.raises(ValueError):
            check_text(bad)


# --- a fake piper (CI): the backend's arithmetic without a model -----------------------

RATE = 22050


@dataclass
class _Cfg:
    speaker_id: Optional[int] = None
    length_scale: Optional[float] = None
    noise_scale: Optional[float] = None
    noise_w_scale: Optional[float] = None
    normalize_audio: bool = True
    volume: float = 1.0


class _FakeVoice:
    """Letters are phonemes; espeak's word fusion is imitated for "for the"."""

    def __init__(self, alignments: str = "ok") -> None:
        self.alignments = alignments
        self.calls: list[tuple[list[int], _Cfg]] = []

    def phonemize(self, text: str) -> list[list[str]]:
        final = text.endswith(".")
        words = text.rstrip(".").split()
        out: list[str] = []
        for i, w in enumerate(words):
            fused = i > 0 and words[i - 1] == "for" and w == "the"
            if i and not fused:
                out.append(" ")
            if w == "budget" and len(words) > 1:  # a context split: "bud get"
                out.extend(["b", "u", "d", " ", "g", "e", "t"])
                continue
            out.extend(list(w))
        return [out + (["."] if final else [])]

    def phoneme_ids_to_audio(self, ids, syn_config=None, include_alignments=False):
        self.calls.append((list(ids), syn_config))
        # 1-3 frames of 256 samples per id, fixed by the id and the speaker
        sid = syn_config.speaker_id or 0
        samples = np.array([256 * (1 + (i * 7 + sid + k) % 3) for k, i in enumerate(ids)], dtype=np.int64)
        n = int(samples.sum())
        audio = (0.5 * np.sin(np.arange(n) * 0.07)).astype(np.float32)
        if self.alignments == "none":
            return audio, None
        if self.alignments == "short":
            return audio[:-1], samples
        return audio, samples


def _write_fake_model(d: Path, stem: str = "xx_fake-medium", n_speakers: int = 4) -> Path:
    import json

    id_map = {"_": [0], "^": [1], "$": [2], " ": [3], ".": [10]}
    id_map.update({chr(c): [20 + c - ord("a")] for c in range(ord("a"), ord("z") + 1)})
    config = {
        "audio": {"sample_rate": RATE},
        "num_speakers": n_speakers,
        "speaker_id_map": {f"r{i}": i for i in range(n_speakers)},
        "phoneme_id_map": id_map,
        "espeak": {"voice": "en-us"},
        "phoneme_type": "espeak",
        "num_symbols": 64,
    }
    (d / f"{stem}.onnx").write_bytes(b"not a real model")
    (d / f"{stem}.onnx.json").write_text(json.dumps(config))
    return d / f"{stem}.onnx"


def _fake_env(monkeypatch, tmp_path: Path, *, legacy: bool = False, n_speakers: int = 4) -> Path:
    module = types.ModuleType("piper")
    module.PiperVoice = object
    if not legacy:
        module.SynthesisConfig = _Cfg
    monkeypatch.setitem(sys.modules, "piper", module)
    d = tmp_path / "voices"
    d.mkdir(parents=True)
    _write_fake_model(d, n_speakers=n_speakers)
    return d


def _packages(monkeypatch, *, onnx: bool, onnxruntime: bool = True) -> None:
    """Make `onnx` / `onnxruntime` look installed or absent to the load plan
    (a ModuleType or None in sys.modules), whatever this machine has."""
    for name, present in (("onnx", onnx), ("onnxruntime", onnxruntime)):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name) if present else None)


def _fake_onnx(monkeypatch) -> None:
    """A stand-in for onnx + piper's patch tool: load/save bytes and append
    one output name, so write_aligned_copy runs without the real packages."""

    class Proto:
        def __init__(self, data: bytes) -> None:
            self.data, self.outputs = data, ["output"]

        def SerializeToString(self) -> bytes:
            return self.data + b"|outputs=" + ",".join(self.outputs).encode()

    onnx = types.ModuleType("onnx")
    onnx.load = lambda path: Proto(Path(path).read_bytes())
    onnx.save = lambda proto, path: Path(path).write_bytes(proto.SerializeToString())
    patch = types.ModuleType("piper.patch_voice_with_alignment")

    def add_alignment_output(proto, tensor_name=None) -> str:
        if "/Ceil_output_0" in proto.outputs:
            raise ValueError("Tensor is already marked as output: /Ceil_output_0")
        proto.outputs.append("/Ceil_output_0")
        return "/Ceil_output_0"

    patch.add_alignment_output = add_alignment_output
    monkeypatch.setitem(sys.modules, "onnx", onnx)
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    monkeypatch.setitem(sys.modules, "piper.patch_voice_with_alignment", patch)


def _fake_palette(monkeypatch, stem: str, n_speakers: int) -> list[str]:
    """Register a 'measured' 16-voice palette for a fake model."""
    sids = [int(round(j * (n_speakers - 1) / 15)) for j in range(16)]
    entries = tuple(PaletteVoice(s, f"r{s}", 100.0 + s, "low" if k % 2 == 0 else "high") for k, s in enumerate(sids))
    monkeypatch.setitem(piper_mod.PALETTES, stem, entries)
    return [f"{stem}:r{s}" for s in sids]


def _fake_backend(monkeypatch, tmp_path: Path, **kw) -> tuple[PiperBackend, _FakeVoice]:
    voice = _FakeVoice(kw.pop("alignments", "ok"))
    d = _fake_env(monkeypatch, tmp_path, n_speakers=kw.pop("n_speakers", 4))
    return PiperBackend(voices_dir=d, loader=lambda model, threads: voice, **kw), voice


def _check_tiling(r, res) -> None:
    """The invariants every render must satisfy, recomputed from the render."""
    n = len(r.audio)
    assert int(r.id_samples.sum()) == n
    spans = r.word_spans()
    owned = sum(int(s) for s, o in zip(r.id_samples, r.owners) if o >= 0)
    unowned = sum(int(s) for s, o in zip(r.id_samples, r.owners) if o < 0)
    assert sum(e - s for s, e in spans) == owned and owned + unowned == n
    up, down = res.notes["resample"]
    assert Fraction(up, down) == Fraction(SAMPLE_RATE, r.model_rate)
    a16 = map_index(spans[0][0], up, down)
    assert res.words[0].start_sample == 0 and res.words[-1].end_sample == len(res.pcm)
    assert len(res.pcm) == map_index(spans[-1][1], up, down) - a16
    gaps = 0
    for w, (s, e) in zip(res.words, spans):
        assert (w.start_sample, w.end_sample) == (map_index(s, up, down) - a16, map_index(e, up, down) - a16)
        assert w.end_sample > w.start_sample
    for w0, w1, (_, e0), (s1, _) in zip(res.words, res.words[1:], spans, spans[1:]):
        gap = w1.start_sample - w0.end_sample
        assert gap == map_index(s1, up, down) - map_index(e0, up, down) and gap > 0
        gaps += gap
    assert sum(w.end_sample - w.start_sample for w in res.words) + gaps == len(res.pcm)
    assert [w.word for w in res.words] == r.tokens and res.timing_exact


def test_stub_render_tiles_the_audio_and_maps_boundaries_exactly(monkeypatch, tmp_path: Path) -> None:
    backend, voice = _fake_backend(monkeypatch, tmp_path)
    assert backend.voices() == [f"xx_fake-medium:r{i}" for i in range(4)]
    r = backend.render("hello there team", "xx_fake-medium:r2")
    assert r.mode == "context" and r.speaker_id == 2
    ids, cfg = voice.calls[-1]
    assert ids == r.phoneme_ids and cfg.speaker_id == 2 and cfg.noise_scale == 0.0 and cfg.noise_w_scale == 0.0
    res = render_to_result(r)
    _check_tiling(r, res)
    assert res.notes["timing_method"] == "piper-duration-alignment" and res.notes["phonemization"] == "context"
    assert res.pcm.dtype == np.int16 and 0.39 * 32767 < np.max(np.abs(res.pcm)) <= 0.4 * 32767
    again = backend.synth("hello there team", "xx_fake-medium:r2", seed=99)
    assert np.array_equal(again.pcm, res.pcm) and again.words == res.words


def test_stub_word_fusion_falls_back_to_per_token_phonemization(monkeypatch, tmp_path: Path) -> None:
    backend, _ = _fake_backend(monkeypatch, tmp_path)
    r = backend.render("wait for the team", "xx_fake-medium:r0")
    assert r.mode == "per-token"
    assert r.word_phonemes == [list("wait"), list("for"), list("the"), list("team")]
    _check_tiling(r, render_to_result(r))
    ctx, _ = _fake_backend(monkeypatch, tmp_path / "b", context_phonemes=False)
    assert ctx.render("hello there", "xx_fake-medium:r0").mode == "per-token"
    # a fusion ("for the") plus a split ("bud get") keeps the group count at 3
    # but shifts the groups; the first-phoneme check must catch it
    shifted = backend.render("for the budget", "xx_fake-medium:r0")
    assert shifted.mode == "per-token" and shifted.word_phonemes[2] == list("budget")
    assert backend.render("the budget", "xx_fake-medium:r0").mode == "per-token"  # split alone: count differs


def test_stub_refuses_missing_or_inconsistent_alignments(monkeypatch, tmp_path: Path) -> None:
    b1, _ = _fake_backend(monkeypatch, tmp_path / "a", alignments="none")
    with pytest.raises(RuntimeError, match="no phoneme alignment"):
        b1.synth("hello there", "xx_fake-medium:r0", 1)
    b2, _ = _fake_backend(monkeypatch, tmp_path / "b", alignments="short")
    with pytest.raises(AssertionError, match="does not tile"):
        b2.synth("hello there", "xx_fake-medium:r0", 1)


def test_stub_voice_resolution_and_bad_input(monkeypatch, tmp_path: Path) -> None:
    backend, _ = _fake_backend(monkeypatch, tmp_path)
    for bad_voice in ("xx_fake-medium", "xx_fake-medium:r9", "nope:r0"):
        with pytest.raises(KeyError):
            backend.synth("hello", bad_voice, 1)
    for bad_text in ("", "Hello there", "two  spaces"):
        with pytest.raises(ValueError):
            backend.synth(bad_text, "xx_fake-medium:r0", 1)


def test_stub_nondeterministic_mode_uses_the_voice_noise_scales(monkeypatch, tmp_path: Path) -> None:
    backend, voice = _fake_backend(monkeypatch, tmp_path, deterministic=False)
    res = backend.synth("hello there", "xx_fake-medium:r1", 1)
    cfg = voice.calls[-1][1]
    assert cfg.noise_scale is None and cfg.noise_w_scale is None  # piper then uses the config's scales
    assert res.notes["deterministic_by_config"] is False and res.timing_exact


def test_stub_backend_unavailable_reasons(monkeypatch, tmp_path: Path) -> None:
    _fake_env(monkeypatch, tmp_path / "legacy", legacy=True)
    with pytest.raises(BackendUnavailable, match="SynthesisConfig"):
        PiperBackend(voices_dir=tmp_path / "legacy" / "voices")
    _fake_env(monkeypatch, tmp_path / "current")  # a current API, but no voice in `empty`
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackendUnavailable, match="never downloads"):
        PiperBackend(voices_dir=empty)
    monkeypatch.setitem(sys.modules, "piper", None)  # import piper -> ImportError
    with pytest.raises(BackendUnavailable, match="not installed"):
        PiperBackend(voices_dir=tmp_path / "legacy" / "voices")


def test_stub_assign_voices_is_deterministic_distinct_and_extends_past_the_palette(monkeypatch, tmp_path: Path) -> None:
    backend, _ = _fake_backend(monkeypatch, tmp_path, n_speakers=40)
    # no measured palette for this model: 16 evenly spaced ids, and a warning
    with pytest.warns(PaletteFallbackWarning, match="xx_fake-medium has no measured palette"):
        pal = backend.palette()
    assert len(pal) == 16 and pal[0] == "xx_fake-medium:r0" and pal[-1] == "xx_fake-medium:r39"
    with pytest.warns(PaletteFallbackWarning):
        for n in (1, 2, 3, 4, 16, 20, 40):
            v = backend.assign_voices(n, 7)
            assert v == backend.assign_voices(n, 7) and len(v) == n and len(set(v)) == n
        with pytest.raises(ValueError):
            backend.assign_voices(41, 7)


def test_stub_palette_prefers_the_measured_model_whatever_else_is_in_the_directory(monkeypatch, tmp_path: Path) -> None:
    """Reviewer's case: another multi-speaker voice that sorts first must not
    change the automatic choice. The measured model wins over filename order;
    the default model wins over both; an unmeasured palette always warns."""
    import warnings

    backend, _ = _fake_backend(monkeypatch, tmp_path, n_speakers=40)
    measured = _fake_palette(monkeypatch, "xx_fake-medium", 40)
    d = tmp_path / "voices"
    _write_fake_model(d, "aa_other-medium", n_speakers=109)  # sorts before xx_fake
    voice = _FakeVoice()
    both = PiperBackend(voices_dir=d, loader=lambda model, threads: voice)
    assert [m.stem for m in both._models] == ["aa_other-medium", "xx_fake-medium"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no fallback warning on the measured path
        assert both.default_voice_model() == "xx_fake-medium"
        assert both.palette() == measured == backend.palette()
        for n, seed in ((2, 101), (2, 102), (3, 103), (4, 104)):
            assert both.assign_voices(n, seed) == backend.assign_voices(n, seed)
        assert both.assign_voices(2, 5, model="xx_fake-medium") == both.assign_voices(2, 5)
    # a pinned or default model without a measured palette: even spacing + warning
    with pytest.warns(PaletteFallbackWarning, match="aa_other-medium"):
        assert both.palette(model="aa_other-medium")[0] == "aa_other-medium:r0"
    pinned = PiperBackend(voices_dir=d, loader=lambda model, threads: voice, voice_model="aa_other-medium")
    with pytest.warns(PaletteFallbackWarning):
        assert all(v.startswith("aa_other-medium:") for v in pinned.assign_voices(4, 1))
    monkeypatch.setattr(piper_mod, "DEFAULT_VOICE_MODEL", "aa_other-medium")
    assert both.default_voice_model() == "aa_other-medium"
    with pytest.raises(KeyError, match="unknown piper voice model"):
        PiperBackend(voices_dir=d, loader=lambda model, threads: voice, voice_model="zz_absent-medium")


def test_stub_backend_drives_generate_meeting_with_exact_word_timings(monkeypatch, tmp_path: Path) -> None:
    """The generator wiring without a model: voices from the backend's own
    assign_voices, words inside their turns, timing_exact propagated."""
    from generator.export import build_segments
    from generator.meeting import generate_meeting, pick_voices
    from generator.scenario import load_scenario

    backend, _ = _fake_backend(monkeypatch, tmp_path, n_speakers=40)
    _fake_palette(monkeypatch, "xx_fake-medium", 40)
    sc = load_scenario("piper_smoke", {"seed": 3, "duration_s": 12.0})
    meeting = generate_meeting(sc, tts=backend)
    assert meeting.voices == backend.assign_voices(2, 3) == pick_voices(backend, 2, 3)
    notes = meeting.turns[0].notes
    assert notes["voice_model"] == "xx_fake-medium" and notes["model_source"] == "custom-loader"
    assert notes["voice_sha256"] == piper_mod.file_sha256(tmp_path / "voices" / "xx_fake-medium.onnx")
    assert meeting.tts_backend == "piper" and all(t.timing_exact for t in meeting.turns)
    for t in meeting.turns:
        assert t.words[0].start_sample == t.start_sample
        assert t.words[-1].end_sample == t.start_sample + len(t.pcm) <= t.end_sample
    assert all(seg["words"] for seg in build_segments(meeting))


def test_stub_meeting_json_exports_the_voice_files_timing_method_and_attribution(monkeypatch, tmp_path: Path) -> None:
    """docs/generator.md §8 (was open): a Piper meeting.json now says how its
    word boundaries were made and which voice files made the audio; the
    formant meeting.json is unchanged (no ``generator.tts`` key)."""
    from generator.export import build_tts_metadata, write_meeting
    from generator.meeting import generate_meeting
    from generator.scenario import load_scenario

    backend, _ = _fake_backend(monkeypatch, tmp_path, n_speakers=40)
    _fake_palette(monkeypatch, "xx_fake-medium", 40)
    meeting = generate_meeting(load_scenario("piper_smoke", {"seed": 3, "duration_s": 12.0}), tts=backend)
    m = write_meeting(meeting, tmp_path / "out")
    tts = m["generator"]["tts"]
    assert tts == build_tts_metadata(meeting)
    assert tts["timing_method"] == ["piper-duration-alignment"]
    assert tts["voice_files"] == [{"voice_model": "xx_fake-medium", "model_source": "custom-loader",
                                   "voice_sha256": piper_mod.file_sha256(tmp_path / "voices" / "xx_fake-medium.onnx"),
                                   "config_sha256": piper_mod.file_sha256(tmp_path / "voices" / "xx_fake-medium.onnx.json")}]
    assert tts["attribution"] == []  # only listed voices carry one
    assert "not acoustic onsets" in m["provenance"]["ground_truth"]
    assert "LibriTTS-R" in piper_mod.VOICE_ATTRIBUTIONS["en_US-libritts_r-medium"]
    assert "CC BY 4.0" in piper_mod.VOICE_ATTRIBUTIONS["en_US-libritts_r-medium"]

    formant = generate_meeting(load_scenario("smoke", {"seed": 3, "duration_s": 12.0}))
    assert build_tts_metadata(formant) is None
    fm = write_meeting(formant, tmp_path / "formant")
    assert "tts" not in fm["generator"] and fm["provenance"]["ground_truth"].startswith("segments, words and activity.npy")


def test_stub_voice_as_downloaded_without_onnx_is_unavailable_at_construction(monkeypatch, tmp_path: Path, capsys) -> None:
    """Reviewer's case: <stem>.onnx + .onnx.json exactly as downloaded, no
    aligned copy, and no `onnx` (plain `pip install piper-tts`). The default
    loader could not open it, so the constructor, available_backends() and
    `generator backends` must all say so before any render."""
    from generator.cli import main
    from generator.tts import available_backends

    d = _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(piper_mod, "VOICES_DIR", d)
    _packages(monkeypatch, onnx=False)
    with pytest.raises(BackendUnavailable, match="needs the onnx package") as exc:
        PiperBackend(voices_dir=d)
    reason = str(exc.value)
    assert "no xx_fake-medium.aligned.onnx" in reason and "not installed" in reason
    assert available_backends()["piper"] == reason
    assert main(["backends"]) == 0 and f"piper    {reason}" in capsys.readouterr().out
    _packages(monkeypatch, onnx=True, onnxruntime=False)
    with pytest.raises(BackendUnavailable, match="onnxruntime package is not installed"):
        PiperBackend(voices_dir=d)
    _packages(monkeypatch, onnx=True)
    assert default_load_plan(discover_models(d)[0]) == ("in-memory-patch", "no xx_fake-medium.aligned.onnx")
    assert PiperBackend(voices_dir=d).voices() == [f"xx_fake-medium:r{i}" for i in range(4)]
    assert available_backends()["piper"] == "available"


def test_stub_an_unloadable_voice_file_is_not_offered_beside_a_loadable_one(monkeypatch, tmp_path: Path) -> None:
    _fake_onnx(monkeypatch)
    d = _fake_env(monkeypatch, tmp_path)
    write_aligned_copy(_write_fake_model(d, "aa_other-medium", n_speakers=3))
    _packages(monkeypatch, onnx=False)  # xx_fake has no aligned copy: not loadable now
    backend = PiperBackend(voices_dir=d)
    assert backend.voices() == [f"aa_other-medium:r{i}" for i in range(3)]
    assert list(backend.unavailable) == ["xx_fake-medium"]
    with pytest.raises(BackendUnavailable, match="needs the onnx package"):
        backend.resolve("xx_fake-medium:r0")
    with pytest.raises(KeyError):
        backend.resolve("zz_absent-medium:r0")


def test_stub_aligned_copy_is_used_only_while_its_sidecar_matches(monkeypatch, tmp_path: Path) -> None:
    """Reviewer's case: a stale <stem>.aligned.onnx (the .onnx beside it
    changed) must not be used silently."""
    import json

    _fake_onnx(monkeypatch)
    d = _fake_env(monkeypatch, tmp_path)
    src = d / "xx_fake-medium.onnx"
    record = write_aligned_copy(src)
    aligned, side = d / "xx_fake-medium.aligned.onnx", d / "xx_fake-medium.aligned.source.json"
    assert json.loads(side.read_text()) == record and record["schema"] == SIDECAR_SCHEMA
    assert record["source_sha256"] == piper_mod.file_sha256(src) and record["alignment_output"] == "/Ceil_output_0"
    assert aligned.read_bytes() == b"not a real model|outputs=output,/Ceil_output_0"
    assert not list(d.glob("*.partial"))

    def plan():
        return default_load_plan(discover_models(d)[0])

    assert check_aligned(discover_models(d)[0]) == (True, "verified") and plan() == ("aligned-file", "verified")
    src.write_bytes(b"an updated voice file")  # the source changes; the copy is now stale
    ok, why = check_aligned(discover_models(d)[0])
    assert not ok and "stale" in why and record["source_sha256"] in why
    assert plan()[0] == "in-memory-patch"
    _packages(monkeypatch, onnx=False)
    with pytest.raises(BackendUnavailable, match="stale"):
        PiperBackend(voices_dir=d)
    _fake_onnx(monkeypatch)
    write_aligned_copy(src)  # re-made from the new source: verified again
    assert plan() == ("aligned-file", "verified")
    aligned.write_bytes(b"edited after the sidecar was written")
    assert "does not match" in check_aligned(discover_models(d)[0])[1]
    write_aligned_copy(src)
    side.write_text(json.dumps({"schema": "something else"}))
    assert "is not a plaud-harness" in check_aligned(discover_models(d)[0])[1]
    side.unlink()
    assert "no provenance sidecar" in check_aligned(discover_models(d)[0])[1]
    with pytest.raises(ValueError, match="expected a <stem>.onnx"):
        write_aligned_copy(aligned)


# --- the real voice (local only) --------------------------------------------------------


def _loaded_backend(**kw) -> PiperBackend:
    """The real backend with the voice LOADED, or a skip with the gate's
    prefix: a voice that is present but cannot be loaded here (no verified
    aligned copy and no onnx, no onnxruntime) is a missing input, not a
    failure. A voice file that is corrupt still fails (not BackendUnavailable)."""
    try:
        backend = PiperBackend(**kw)
        model, _ = backend.resolve(f"{VOICE_STEM}:{LIBRITTS_R_MEDIUM_PALETTE[0].reader}")
        backend._load(model)
    except (BackendUnavailable, KeyError) as exc:
        pytest.skip(f"{SKIP_PREFIX} {exc}")
    return backend


@pytest.fixture(scope="module")
def piper_backend() -> PiperBackend:
    return _loaded_backend()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def test_voice_files_are_the_measured_ones_and_the_render_says_which(piper_backend: PiperBackend) -> None:
    import json

    model, _ = piper_backend.resolve(piper_backend.palette()[0])
    assert _sha256(model.onnx_path) == ORIGINAL_SHA256, "not the voice docs/generator.md measured"
    assert _sha256(model.config_path) == CONFIG_SHA256
    ok, why = check_aligned(model)
    if model.sidecar_path.is_file():  # made by `python -m generator.tts.piper align`
        record = json.loads(model.sidecar_path.read_text())
        assert ok, why
        assert (record["source_sha256"], record["aligned_sha256"]) == (ORIGINAL_SHA256, ALIGNED_SHA256)
        assert _sha256(model.aligned_path) == ALIGNED_SHA256
    else:
        assert not ok  # an unverified copy is never used
    voice = piper_backend._load(model)
    assert [o.name for o in voice.session.get_outputs()] == ["output", "/Ceil_output_0"]
    notes = piper_backend.synth(CONTEXT_TEXT, piper_backend.palette()[0], 0).notes
    assert notes["model_source"] == ("aligned-file" if ok else "in-memory-patch")
    assert (notes["voice_model"], notes["voice_sha256"], notes["config_sha256"]) == (VOICE_STEM, ORIGINAL_SHA256, CONFIG_SHA256)


def test_aligned_model_is_the_original_plus_one_alignment_output(piper_backend: PiperBackend) -> None:
    model, _ = piper_backend.resolve(piper_backend.palette()[0])
    if model.aligned_path is None:
        pytest.skip(f"{SKIP_PREFIX} no {VOICE_STEM}.aligned.onnx (the backend then patches in memory)")
    try:
        import onnx
        from piper.patch_voice_with_alignment import add_alignment_output
    except ImportError as exc:
        pytest.skip(f"{SKIP_PREFIX} the onnx package is needed to re-derive {VOICE_STEM}.aligned.onnx: {exc}")

    original = onnx.load(str(model.onnx_path))
    assert add_alignment_output(original) == "/Ceil_output_0"
    expected = hashlib.sha256(original.SerializeToString()).hexdigest()
    del original
    aligned = onnx.load(str(model.aligned_path))
    assert hashlib.sha256(aligned.SerializeToString()).hexdigest() == expected


@pytest.mark.parametrize("text, expected_mode", [(CONTEXT_TEXT, "context"), (MERGING_TEXT, "per-token")])
def test_real_word_boundaries_tile_the_rendered_audio_exactly(piper_backend: PiperBackend, text: str, expected_mode: str) -> None:
    from piper.phoneme_ids import phonemes_to_ids

    for voice in piper_backend.palette()[:4]:
        r = piper_backend.render(text, voice)
        assert r.mode == expected_mode and r.model_rate == 22050 and not r.dropped_phonemes
        flat = [p for k, w in enumerate(r.word_phonemes) for p in ([" "] if k else []) + w] + ["."]
        model, _ = piper_backend.resolve(voice)
        assert r.phoneme_ids == phonemes_to_ids(flat, model.config["phoneme_id_map"])
        assert np.all(r.id_samples % 256 == 0) and np.all(r.id_samples >= 256)  # whole VITS frames
        _check_tiling(r, render_to_result(r))


def test_real_render_equals_pipers_own_synthesize(piper_backend: PiperBackend) -> None:
    """Same ids, same audio and same per-phoneme alignment as piper's public
    synthesize(); the backend adds only the word bookkeeping."""
    from piper import SynthesisConfig

    voice = piper_backend.palette()[1]
    r = piper_backend.render(CONTEXT_TEXT, voice)
    model, sid = piper_backend.resolve(voice)
    cfg = SynthesisConfig(speaker_id=sid, noise_scale=0.0, noise_w_scale=0.0)
    (chunk,) = list(piper_backend._load(model).synthesize(CONTEXT_TEXT + ".", cfg, include_alignments=True))
    assert chunk.phoneme_ids == r.phoneme_ids
    assert np.array_equal(chunk.phoneme_id_samples, r.id_samples)
    normalised = np.clip(r.audio / np.max(np.abs(r.audio)), -1.0, 1.0).astype(np.float32)
    assert np.array_equal(chunk.audio_float_array, normalised)
    ends = np.cumsum([a.num_samples for a in chunk.phoneme_alignments])
    starts = ends - np.array([a.num_samples for a in chunk.phoneme_alignments])
    seps = [i for i, a in enumerate(chunk.phoneme_alignments) if a.phoneme in (" ", ".")]
    first = [1] + [i + 1 for i in seps[:-1]]
    last = [i - 1 for i in seps]
    assert [(int(starts[a]), int(ends[b])) for a, b in zip(first, last)] == r.word_spans()


def test_real_cut_lead_and_tail_are_quiet(piper_backend: PiperBackend) -> None:
    """The BOS lead-in and the "."/EOS tail that the backend cuts are quiet
    relative to the kept utterance (sample minima 17.5 and 18.7 dB over 120
    renders, docs/generator.md section 6.2; not bounds, and a word-final stop
    release can fall in the tail and is cut). 12 dB here."""
    from generator.text import TextSource

    def db(x: np.ndarray) -> float:
        return 10 * np.log10(np.mean(np.square(x.astype(np.float64))) + 1e-20)

    ts = TextSource(11)
    for voice in piper_backend.palette():
        r = piper_backend.render(ts.utterance(6), voice)
        (a, _), (_, b) = r.word_spans()[0], r.word_spans()[-1]
        kept = db(r.audio[a:b])
        assert kept - db(r.audio[:a]) > 12.0 and kept - db(r.audio[b:]) > 12.0, voice


def test_real_synthesis_is_byte_deterministic_and_voice_dependent(piper_backend: PiperBackend) -> None:
    v0, v1 = piper_backend.palette()[:2]
    a = piper_backend.synth(CONTEXT_TEXT, v0, seed=1)
    b = piper_backend.synth(CONTEXT_TEXT, v0, seed=2)  # the seed cannot reach piper's sampling
    fresh = PiperBackend().synth(CONTEXT_TEXT, v0, seed=1)  # new onnxruntime session
    assert np.array_equal(a.pcm, b.pcm) and a.words == b.words
    assert np.array_equal(a.pcm, fresh.pcm) and a.words == fresh.words
    other = piper_backend.synth(CONTEXT_TEXT, v1, seed=1)
    n = min(len(a.pcm), len(other.pcm))
    assert not np.array_equal(a.pcm[:n], other.pcm[:n])


def test_real_noise_scales_make_output_nondeterministic_but_still_tiled() -> None:
    """Falsifier for the determinism claim: with the voice's own noise scales
    (0.333) two renders differ, and each is still exactly tiled."""
    noisy = _loaded_backend(deterministic=False)
    voice = f"{VOICE_STEM}:{LIBRITTS_R_MEDIUM_PALETTE[0].reader}"
    r1, r2 = noisy.render(CONTEXT_TEXT, voice), noisy.render(CONTEXT_TEXT, voice)
    assert r1.noise_scale is None and r1.phoneme_ids == r2.phoneme_ids
    assert len(r1.audio) != len(r2.audio) or not np.array_equal(r1.audio, r2.audio)
    for r in (r1, r2):
        _check_tiling(r, render_to_result(r))


def test_real_palette_matches_the_voice_and_its_measured_pitch(piper_backend: PiperBackend) -> None:
    """Palette entries exist in the voice config with the recorded speaker id,
    and re-measuring each voice's median f0 reproduces the recorded value and
    pitch group; auto-assignment therefore mixes low and high voices."""
    from piper import SynthesisConfig

    model, _ = piper_backend.resolve(piper_backend.palette()[0])
    sid_map = model.config["speaker_id_map"]
    assert piper_backend.palette() == [f"{VOICE_STEM}:{p.reader}" for p in LIBRITTS_R_MEDIUM_PALETTE]
    voice = piper_backend._load(model)
    ids = voice.phonemes_to_ids(voice.phonemize(PALETTE_F0_TEXT)[0])
    for p in LIBRITTS_R_MEDIUM_PALETTE:
        assert sid_map[p.reader] == p.speaker_id
        audio, _ = voice.phoneme_ids_to_audio(ids, SynthesisConfig(speaker_id=p.speaker_id, noise_scale=0.0, noise_w_scale=0.0), True)
        f0, voiced = median_f0(audio, model.sample_rate)
        assert voiced > 100 and abs(f0 - p.f0_hz) < 0.1, (p, f0)
        assert (f0 < 135.0) if p.pitch == "low" else (f0 > 200.0)
    pitch = {f"{VOICE_STEM}:{p.reader}": p.pitch for p in LIBRITTS_R_MEDIUM_PALETTE}
    for seed in range(16):
        pair = piper_backend.assign_voices(2, seed)
        assert {pitch[v] for v in pair} == {"low", "high"}
        four = piper_backend.assign_voices(4, seed)
        assert [pitch[v] for v in four].count("low") == 2 and len(set(four)) == 4


def _voice_dir_with(tmp: Path, model, names=("onnx", "onnx.json", "aligned.onnx", "aligned.source.json")) -> Path:
    tmp.mkdir(parents=True)
    for suffix in names:
        src = model.onnx_path.with_name(f"{VOICE_STEM}.{suffix}")
        if src.exists():
            (tmp / src.name).symlink_to(src)
    return tmp


#: The readers docs/generator.md section 6.3 lists for the four set meetings.
SET_VOICES = {(2, 101): ["5622", "957"], (2, 102): ["8222", "6924"], (3, 103): ["7754", "957", "5712"], (4, 104): ["7069", "3119", "2674", "5712"]}


def test_real_other_voice_files_do_not_change_the_automatic_voices(piper_backend: PiperBackend, tmp_path: Path) -> None:
    """Reviewer's v_two case: a multi-speaker vctk placeholder that sorts
    before libritts_r sits in the voice directory. The documented set's
    voices (section 6.3) must stay the same, without a fallback warning."""
    import json
    import warnings

    model, _ = piper_backend.resolve(piper_backend.palette()[0])
    d = _voice_dir_with(tmp_path / "v_two", model)
    config = json.loads(model.config_path.read_text())
    config["num_speakers"] = 109
    config["speaker_id_map"] = {f"p{225 + i}": i for i in range(109)}
    (d / "en_GB-vctk-medium.onnx").write_bytes(b"placeholder, never loaded")
    (d / "en_GB-vctk-medium.onnx.json").write_text(json.dumps(config))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        two = PiperBackend(voices_dir=d)
        for (n, seed), readers in SET_VOICES.items():
            expected = [f"{VOICE_STEM}:{r}" for r in readers]
            assert two.assign_voices(n, seed) == piper_backend.assign_voices(n, seed) == expected
        assert two.palette() == piper_backend.palette()
    if "en_GB-vctk-medium" not in two.unavailable:  # offered when onnx could patch it in memory
        with pytest.warns(PaletteFallbackWarning):
            assert two.palette(model="en_GB-vctk-medium")[:3] == ["en_GB-vctk-medium:p225", "en_GB-vctk-medium:p232", "en_GB-vctk-medium:p239"]


def test_real_stale_aligned_copy_is_not_used(piper_backend: PiperBackend, tmp_path: Path) -> None:
    """Reviewer's v_stale case: the .onnx beside a verified-looking aligned
    copy is not the model it was made from. Before the fix this rendered
    23406 samples with timing_exact True from the stale copy."""
    model, _ = piper_backend.resolve(piper_backend.palette()[0])
    if not check_aligned(model)[0]:
        pytest.skip(f"{SKIP_PREFIX} no verified {VOICE_STEM}.aligned.onnx to make stale")
    d = _voice_dir_with(tmp_path / "v_stale", model, ("onnx.json", "aligned.onnx", "aligned.source.json"))
    (d / f"{VOICE_STEM}.onnx").write_bytes(b"not the model at all")
    stale = discover_models(d)[0]
    ok, why = check_aligned(stale)
    assert not ok and "stale" in why and ORIGINAL_SHA256 in why
    if default_load_plan(stale)[0] is None:  # no onnx here: unavailable, and says why
        with pytest.raises(BackendUnavailable, match="stale"):
            PiperBackend(voices_dir=d)
        return
    backend = PiperBackend(voices_dir=d)
    with pytest.warns(AlignedCopyIgnoredWarning, match="stale"), pytest.raises(RuntimeError, match="not a readable ONNX model"):
        backend.synth(CONTEXT_TEXT, backend.palette()[0], 0)


# --- end to end: a two-speaker Piper meeting keeps V4 (DER exactly 0) ------------------


@pytest.fixture(scope="module")
def piper_meeting(piper_backend: PiperBackend, tmp_path_factory) -> tuple[Path, dict]:
    from generator.testing import fixture_meeting

    return fixture_meeting(tmp_path_factory.mktemp("piper"), "piper_smoke", 7)


def test_piper_meeting_ground_truth_round_trips_at_der_zero(piper_meeting, piper_backend: PiperBackend) -> None:
    import warnings

    from pyannote.core import Annotation, Segment, Timeline
    from pyannote.database.util import load_rttm
    from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

    from generator.cli import main
    from generator.export import load_activity_mask
    from generator.turns import mask_to_segments

    warnings.filterwarnings("ignore", message=".*'uem' was approximated.*")
    d, m = piper_meeting
    assert main(["validate", str(d)]) == 0
    assert m["generator"]["tts_backend"] == "piper" and m["generator"]["timing_exact"] is True
    tts = m["generator"]["tts"]
    assert tts["timing_method"] == ["piper-duration-alignment"]
    assert [v["voice_model"] for v in tts["voice_files"]] == [VOICE_STEM]
    assert tts["attribution"] == [piper_mod.VOICE_ATTRIBUTIONS[VOICE_STEM]]
    assert 20.0 <= m["duration_s"] <= 30.0 and len(m["speakers"]) == 2
    assert [s["voice"] for s in m["speakers"]] == piper_backend.assign_voices(2, 7)
    assert m["turn_taking"]["overlap_ratio_realised"] > 0.0, "the smoke preset must overlap"
    segs = m["segments"]
    rttm = parse_rttm((d / "ref.rttm").read_text())
    stm = parse_stm((d / "ref.stm").read_text())
    assert [(r["speaker"], r["start"], r["end"]) for r in rttm] == [(s["speaker"], s["start"], s["end"]) for s in segs]
    assert [(r["speaker"], r["start"], r["end"], r["text"]) for r in stm] == [(s["speaker"], s["start"], s["end"], s["text"]) for s in segs]

    def annotation(items) -> Annotation:
        ann = Annotation(uri=m["meeting_id"])
        for spk, a, b in items:
            ann[Segment(a, b), len(ann)] = spk
        return ann

    ref = load_rttm(str(d / "ref.rttm"))[m["meeting_id"]]
    uem = Timeline([Segment(0.0, m["duration_s"])], uri=m["meeting_id"])
    mask = load_activity_mask(d)
    from_mask = annotation([(f"spk{s}", a / SAMPLE_RATE, b / SAMPLE_RATE) for s, a, b in mask_to_segments(mask)])
    der, jer = DiarizationErrorRate(), JaccardErrorRate()
    assert der(ref, ref, uem=uem) == 0.0 and jer(ref, ref, uem=uem) == 0.0
    assert der(ref, from_mask, uem=uem) == 0.0 and jer(ref, from_mask, uem=uem) == 0.0
    shifted = annotation([(s["speaker"], s["start"] + 0.5, s["end"] + 0.5) for s in segs])
    assert der(ref, shifted, uem=uem) > 0.05


def test_piper_meeting_words_and_stems_are_the_backend_output(piper_meeting, piper_backend: PiperBackend) -> None:
    """Every turn's stem samples are exactly a fresh render of its text, and
    its meeting.json word times are exactly that render's word samples."""
    from generator.export import read_wav

    d, m = piper_meeting
    voices = {s["id"]: s["voice"] for s in m["speakers"]}
    stems = {sid: read_wav(d / rel)[0][0] for sid, rel in m["audio"]["stems"].items()}
    for seg in m["segments"]:
        start = int(round(seg["start"] * SAMPLE_RATE))
        assert start / SAMPLE_RATE == seg["start"]
        res = piper_backend.synth(seg["text"], voices[seg["speaker"]], seed=0)
        assert np.array_equal(stems[seg["speaker"]][start : start + len(res.pcm)], res.pcm)
        got = [(w["w"], int(round(w["start"] * SAMPLE_RATE)), int(round(w["end"] * SAMPLE_RATE))) for w in seg["words"]]
        assert got == [(w.word, start + w.start_sample, start + w.end_sample) for w in res.words]
        for w in seg["words"]:
            assert w["start"] == int(round(w["start"] * SAMPLE_RATE)) / SAMPLE_RATE
        end = int(round(seg["end"] * SAMPLE_RATE))
        assert start + len(res.pcm) <= end < start + len(res.pcm) + 125  # grid padding only
        assert not np.any(stems[seg["speaker"]][start + len(res.pcm) : end])
