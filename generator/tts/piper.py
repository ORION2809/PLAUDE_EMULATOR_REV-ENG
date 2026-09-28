"""Piper backend: intelligible multi-speaker speech whose word boundaries come
from the voice's own duration alignment (import-guarded; never downloads).

Requires the `piper` package (piper-tts >= 1.3; 1.8.0 is installed and
vendored at reference/upstream/piper) with onnxruntime, and a voice under
`data/voices/piper/`: `<stem>.onnx` + `<stem>.onnx.json`, plus, preferably,
`<stem>.aligned.onnx` and its provenance sidecar `<stem>.aligned.source.json`,
both written by `python -m generator.tts.piper align <stem>.onnx` (piper's own
`add_alignment_output` + `onnx.save`, as `piper.patch_voice_with_alignment`
does, then the sha256 of the source and of the result). The aligned copy
makes the model also output the per-phoneme-id durations. It is used only
while the sidecar's hashes match BOTH files; with no copy, no sidecar or a
stale one, the model is patched in memory with the same function, which
needs the `onnx` package (piper-tts's `[alignment]` extra; plain
`pip install piper-tts` does not bring it). The constructor decides this
per voice file without loading it (`default_load_plan`) and raises
BackendUnavailable when no voice can be loaded, so `available_backends()`,
`python -m generator backends` and the tests' gate report it before any
render. A model that yields no alignment output is refused: this backend
never estimates timings.

How a word gets its samples (docs/generator.md section 6):
1. Phonemize with piper's own espeak-ng phonemizer. "context" mode
   phonemizes the whole utterance (plus a sentence-final "." for prosody,
   HARNESS_POLICY) and splits it at the " " word-separator phonemes. espeak
   sometimes fuses words ("for the" -> "fɚðə"); when the groups do not match
   the tokens one to one (count, or a group not starting with its token's
   first phoneme), the utterance falls back to "per-token" mode, where each
   token is phonemized on its own and the separators are inserted here.
   Either way every phoneme belongs to exactly one known token.
2. Build the phoneme-id sequence exactly as `piper.phoneme_ids.phonemes_to_ids`
   does (BOS PAD (phoneme PAD)* EOS), remembering which token owns each id.
   A phoneme owns the PAD that follows it (piper's PhonemeAlignment
   convention); BOS, the separators, the final "." and EOS own no token.
3. `PiperVoice.phoneme_ids_to_audio(ids, SynthesisConfig, include_alignments=True)`
   returns the audio and the samples each id was rendered with
   (ceil'd VITS durations x hop length 256). The backend checks that the
   counts cover the audio exactly (sum == len); a word spans the samples of
   its ids. These are facts of the model's hard monotonic alignment, which
   is what "exact" means here; the decoder's receptive field still lets a
   phoneme's energy spill a few ms across a frame boundary.
4. Resample the whole utterance to 16 kHz (scipy resample_poly, 320/441 for
   22.05 kHz, zero-phase), map every boundary x with round-half-up(x*320/441)
   (at most 0.5 sample = 31.25 us from the model-rate boundary), and keep
   [first word start, last word end): the lead-in (BOS) and the tail ("." and
   EOS) are cut, so words[0].start == 0 and words[-1].end == len(pcm). Both
   are quiet but not silent, and a word-final stop's release can fall in the
   tail frames and is cut with them (measured in docs/generator.md 6.2).
   4 ms half-cosine edge fades and a -8 dBFS peak (both HARNESS_POLICY, as in
   the formant backend). Inter-word gaps are the model's separator audio,
   not digital silence.

Determinism (measured, docs/generator.md): with both noise scales pinned to
0.0 (default) the same ids, speaker and thread count give byte-identical audio
in one session, across sessions and across processes. The bytes DO depend on
onnxruntime's intra-op thread count (float reduction order; <= 2.9e-5
full-scale difference, durations unchanged over 40 utterances), so the
backend pins it (DEFAULT_THREADS = 1, HARNESS_POLICY). Other CPU types and
onnxruntime versions are not verified. `deterministic=False` uses the voice's
own noise scales (0.333/0.333): the output then changes on every call (even
its length), while each call's word timings stay exact for that call. The
seed cannot reach piper's sampling (it happens inside the ONNX graph) and is
ignored in both modes.

Voices: a multi-speaker model offers one voice per speaker id, named
"<stem>:<reader>" with the reader id from the config's speaker_id_map.
Auto-assignment (HARNESS_POLICY) draws from a fixed 16-voice palette of ONE
model (see LIBRITTS_R_MEDIUM_PALETTE and `palette_indices`): the pinned
`voice_model` if given, else DEFAULT_VOICE_MODEL when present, else the first
model by name with a measured PALETTES entry. Other voice files in the
directory therefore cannot change the automatic choice while the default
voice is present. A model without a measured palette gets 16 evenly spaced
ids and a PaletteFallbackWarning (the low/high alternation is then lost).

`python -m generator.tts.piper align <stem>.onnx` (re)makes the aligned copy
and sidecar; `python -m generator.tts.piper check` prints each voice's load
plan.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path
from typing import Any, Callable

import numpy as np

from generator._evidence import ROOT
from generator.contract import SAMPLE_RATE
from generator.tts.base import BackendUnavailable, SynthResult, WordTiming

VOICES_DIR = ROOT / "data" / "voices" / "piper"
ALIGNED_SUFFIX = ".aligned.onnx"
#: `<stem>.aligned.source.json`: which source the aligned copy was made from.
#: Not `<stem>.aligned.onnx.json`, which piper would take for a voice config.
SIDECAR_SUFFIX = ".aligned.source.json"
SIDECAR_SCHEMA = "plaud-harness/piper-aligned-source/1"
#: HARNESS_POLICY: the model auto-assignment draws from unless one is pinned.
DEFAULT_VOICE_MODEL = "en_US-libritts_r-medium"


class PaletteFallbackWarning(UserWarning):
    """Auto-assignment fell back to evenly spaced ids of an unmeasured model."""


class AlignedCopyIgnoredWarning(UserWarning):
    """A present `<stem>.aligned.onnx` was not verified by its sidecar and is
    not used; the model is patched in memory instead."""


#: piper/const.py sentinels: BOS "^", EOS "$", PAD "_"; " " separates words.
BOS, EOS, PAD = "^", "$", "_"
WORD_SEPARATOR = " "
#: Terminators piper's espeak phonemizer appends to a clause (phonemize_espeak.py).
PUNCTUATION = frozenset(".,;:!?")

#: HARNESS_POLICY: every utterance is phonemized as one declarative sentence.
SENTENCE_FINAL = "."
#: HARNESS_POLICY: peak level and edge fades, as in the formant backend.
PEAK_DBFS = -8.0
EDGE_FADE_S = 0.004
#: HARNESS_POLICY: onnxruntime intra-op threads. The bytes depend on it.
DEFAULT_THREADS = 1

_TOKENS = re.compile(r"[a-z]+(?: [a-z]+)*")


@dataclass(frozen=True)
class PaletteVoice:
    speaker_id: int
    reader: str  # key in the voice config's speaker_id_map (LibriTTS-R reader id)
    f0_hz: float  # median f0 measured on PALETTE_F0_TEXT (see docs/generator.md)
    pitch: str  # "low" (< 135 Hz) or "high" (> 200 Hz)


#: HARNESS_POLICY: 16 libritts_r speakers, interleaved low/high pitch, each
#: group spread across the id range. Derivation (docs/generator.md section 6):
#: 64 candidates sid = round(k*903/63), k = 0..63, rendered PALETTE_F0_TEXT with
#: noise 0; median autocorrelation f0; low = f0 < 135 Hz (27 candidates),
#: high = f0 > 200 Hz (14); from each group, sorted by id, the entries at
#: round(j*(len-1)/7), j = 0..7; then L0 H0 L1 H1 ... L7 H7.
LIBRITTS_R_MEDIUM_PALETTE: tuple[PaletteVoice, ...] = (
    PaletteVoice(43, "7314", 96.3, "low"),
    PaletteVoice(14, "5712", 218.3, "high"),
    PaletteVoice(201, "1165", 128.6, "low"),
    PaletteVoice(57, "2592", 237.1, "high"),
    PaletteVoice(330, "3945", 117.3, "low"),
    PaletteVoice(115, "5622", 222.7, "high"),
    PaletteVoice(459, "8222", 114.8, "low"),
    PaletteVoice(229, "7754", 204.2, "high"),
    PaletteVoice(602, "7069", 128.9, "low"),
    PaletteVoice(301, "1678", 237.1, "high"),
    PaletteVoice(659, "8879", 118.5, "low"),
    PaletteVoice(444, "3119", 216.2, "high"),
    PaletteVoice(788, "957", 116.1, "low"),
    PaletteVoice(588, "6924", 218.3, "high"),
    PaletteVoice(889, "2674", 128.2, "low"),
    PaletteVoice(731, "1425", 239.7, "high"),
)
PALETTES: dict[str, tuple[PaletteVoice, ...]] = {"en_US-libritts_r-medium": LIBRITTS_R_MEDIUM_PALETTE}
PALETTE_F0_TEXT = (
    "we should review the budget and the release plan with the whole team before the meeting on monday morning."
)


_HASHES: dict[tuple[str, int, int, int, int], str] = {}


def file_sha256(path: Path) -> str:
    """sha256 of a file (symlinks followed), cached within the process per
    (real path, inode, size, mtime, ctime): any rewrite or replacement of the
    file changes the key, so a cached value is never reused for new bytes."""
    path = Path(path)
    st = path.stat()
    key = (str(path.resolve()), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if key not in _HASHES:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        _HASHES[key] = h.hexdigest()
    return _HASHES[key]


def _importable(name: str) -> bool:
    """Is a top-level package importable, without importing it? A module
    set to None in sys.modules counts as absent (as `import` treats it)."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def map_index(x: int, up: int, down: int) -> int:
    """Model-rate sample index -> output index: round half up of x*up/down,
    in integer arithmetic (monotonic; |error| <= 0.5 output sample)."""
    return (2 * int(x) * up + down) // (2 * down)


def palette_indices(n: int, seed: int, size: int) -> list[int]:
    """n distinct palette positions for n speakers (HARNESS_POLICY).

    Start at seed % size and step by the largest ODD number <= size // n:
    the picks spread over the palette, never repeat ((n-1)*step < size), and
    with an even-sized interleaved palette consecutive speakers alternate
    between the low and high pitch groups."""
    if n < 1:
        raise ValueError("n must be >= 1")
    if n > size:
        raise ValueError(f"only {size} palette voices for {n} speakers")
    q = size // n
    step = max(1, q if q % 2 == 1 else q - 1)
    offset = int(seed) % size
    return [(offset + i * step) % size for i in range(n)]


def median_f0(audio: np.ndarray, rate: int) -> tuple[float, int]:
    """(median f0 in Hz, voiced frames): the estimator behind the palette.

    40 ms frames, 10 ms hop; frames quieter than peak - 30 dB are skipped;
    normalised, unbiased autocorrelation; the highest peak for 60-400 Hz;
    voiced when that peak exceeds 0.6. Lag-quantised, so values repeat."""
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    frame, hop = int(0.04 * rate), int(0.01 * rate)
    lo, hi = int(rate / 400), int(rate / 60)
    floor = float(np.max(np.abs(x))) * 10.0 ** (-30.0 / 20.0) if len(x) else 0.0
    unbias = 1.0 - np.arange(frame) / frame
    f0s = []
    for s in range(0, len(x) - frame, hop):
        fr = x[s : s + frame] - x[s : s + frame].mean()
        if np.sqrt(np.mean(fr**2)) < floor:
            continue
        ac = np.correlate(fr, fr, "full")[frame - 1 :]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0] / unbias
        i = int(np.argmax(ac[lo:hi]))
        if ac[lo + i] > 0.6:
            f0s.append(rate / (lo + i))
    return (float(np.median(f0s)) if f0s else float("nan")), len(f0s)


def check_text(text: str) -> list[str]:
    """The generator contract: lowercase letter tokens, single spaces."""
    if not isinstance(text, str) or not _TOKENS.fullmatch(text):
        raise ValueError(f"text must be lowercase letter tokens separated by single spaces: {text!r}")
    return text.split(" ")


def build_phoneme_ids(
    words: list[list[str]], id_map: dict[str, list[int]], final: str | None = SENTENCE_FINAL
) -> tuple[list[int], list[int], list[str]]:
    """Phoneme ids laid out exactly as piper's phonemes_to_ids lays out
    w0 + [" "] + w1 + ... + [final], with the owning word of every id
    (-1: BOS, separator, final punctuation, EOS). Phonemes missing from the
    map are dropped, as piper drops them, and returned."""
    for sym in (BOS, PAD, EOS, WORD_SEPARATOR):
        if sym not in id_map:
            raise ValueError(f"voice phoneme_id_map lacks {sym!r}")
    pad = list(id_map[PAD])
    ids: list[int] = list(id_map[BOS]) + pad
    owners: list[int] = [-1] * len(ids)
    dropped: list[str] = []
    for k, phonemes in enumerate(words):
        if k:
            sep = list(id_map[WORD_SEPARATOR]) + pad
            ids += sep
            owners += [-1] * len(sep)
        before = len(ids)
        for p in phonemes:
            if p not in id_map:
                dropped.append(p)
                continue
            chunk = list(id_map[p]) + pad
            ids += chunk
            owners += [k] * len(chunk)
        if len(ids) == before:
            raise ValueError(f"word {k} has no phoneme this voice knows: {phonemes!r}")
    if final and final in id_map:
        chunk = list(id_map[final]) + pad
        ids += chunk
        owners += [-1] * len(chunk)
    ids += list(id_map[EOS])
    owners += [-1] * len(id_map[EOS])
    return ids, owners, dropped


def _split_words(phonemes: list[str]) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    for p in phonemes:
        if p == WORD_SEPARATOR:
            groups.append([])
        else:
            groups[-1].append(p)
    return [g for g in groups if g]


#: espeak's stress and length marks: skipped when comparing first phonemes.
_MARKS = frozenset("ˈˌːˑ")


def _first_phoneme(phonemes: list[str]) -> str:
    return next((p for p in phonemes if p not in _MARKS), "")


def _strip_edges(phonemes: list[str]) -> list[str]:
    """Drop trailing clause punctuation and outer separators."""
    out = list(phonemes)
    while out and (out[-1] in PUNCTUATION or out[-1] == WORD_SEPARATOR):
        out.pop()
    while out and out[0] == WORD_SEPARATOR:
        out.pop(0)
    return out


@dataclass
class PiperRender:
    """One utterance at the model rate, before resampling. Everything the
    16 kHz word timings are derived from, kept so tests can recompute them."""

    text: str
    tokens: list[str]
    voice: str
    speaker_id: int | None
    mode: str  # "context" | "per-token"
    word_phonemes: list[list[str]]
    phoneme_ids: list[int]
    owners: list[int]  # word index per id, -1 for BOS / separator / final / EOS
    id_samples: np.ndarray  # int64, samples per id (model rate)
    audio: np.ndarray  # float32, raw model output (model rate)
    model_rate: int
    dropped_phonemes: list[str] = field(default_factory=list)
    noise_scale: float | None = None
    noise_w_scale: float | None = None

    @property
    def boundaries(self) -> np.ndarray:
        """Cumulative sample offset before each id (len(ids) + 1 entries)."""
        return np.concatenate([[0], np.cumsum(self.id_samples)]).astype(np.int64)

    def word_spans(self) -> list[tuple[int, int]]:
        """(start, end) of each word at the model rate."""
        cum = self.boundaries
        owners = np.asarray(self.owners)
        spans = []
        for k in range(len(self.tokens)):
            idx = np.flatnonzero(owners == k)
            if len(idx) == 0 or idx[-1] - idx[0] + 1 != len(idx):
                raise AssertionError(f"word {k} does not own one contiguous run of ids")
            spans.append((int(cum[idx[0]]), int(cum[idx[-1] + 1])))
        return spans


@dataclass
class _Model:
    stem: str
    onnx_path: Path
    aligned_path: Path | None
    config_path: Path
    config: dict[str, Any]

    @property
    def num_speakers(self) -> int:
        return int(self.config.get("num_speakers", 1))

    @property
    def sample_rate(self) -> int:
        return int(self.config["audio"]["sample_rate"])

    @property
    def readers(self) -> list[str]:
        """Speaker names ordered by speaker id."""
        sid_map = self.config.get("speaker_id_map") or {}
        return [r for r, _ in sorted(sid_map.items(), key=lambda kv: kv[1])]

    def voice_names(self) -> list[str]:
        if self.num_speakers > 1:
            return [f"{self.stem}:{r}" for r in self.readers]
        return [self.stem]

    @property
    def sidecar_path(self) -> Path:
        return self.onnx_path.with_name(self.stem + SIDECAR_SUFFIX)

    def measured_palette(self) -> tuple[PaletteVoice, ...] | None:
        """The PALETTES entry for this stem, if every entry's reader still
        maps to the recorded speaker id in this config; else None."""
        entries = PALETTES.get(self.stem)
        sid_map = self.config.get("speaker_id_map") or {}
        if entries and all(sid_map.get(p.reader) == p.speaker_id for p in entries):
            return entries
        return None


def discover_models(voices_dir: Path) -> list[_Model]:
    models = []
    if not voices_dir.is_dir():
        return models
    for path in sorted(voices_dir.glob("*.onnx")):
        if path.name.endswith(ALIGNED_SUFFIX):
            continue
        config_path = path.with_name(path.name + ".json")
        if not config_path.is_file():
            continue
        aligned = path.with_name(path.name[: -len(".onnx")] + ALIGNED_SUFFIX)
        models.append(
            _Model(
                stem=path.name[: -len(".onnx")],
                onnx_path=path,
                aligned_path=aligned if aligned.is_file() else None,
                config_path=config_path,
                config=json.loads(config_path.read_text(encoding="utf-8")),
            )
        )
    return models


def check_aligned(model: _Model) -> tuple[bool, str]:
    """Is `<stem>.aligned.onnx` verifiably THIS `<stem>.onnx`, patched?
    Its sidecar (written by `write_aligned_copy`) must record the current
    sha256 of both files. Returns (True, "verified") or (False, why not).
    The config (`.onnx.json`) is not part of the check: the aligned graph
    does not depend on it, and it is always read from the `.onnx.json`."""
    aligned = model.aligned_path
    if aligned is None:
        return False, f"no {model.stem}{ALIGNED_SUFFIX}"
    side = model.sidecar_path
    if not side.is_file():
        return False, (
            f"{aligned.name} has no provenance sidecar {side.name} "
            f"(`python -m generator.tts.piper align {model.onnx_path.name}` writes both)"
        )
    try:
        record = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"{side.name} is unreadable: {exc}"
    if not isinstance(record, dict) or record.get("schema") != SIDECAR_SCHEMA:
        return False, f"{side.name} is not a {SIDECAR_SCHEMA} record"
    source_sha = file_sha256(model.onnx_path)
    if record.get("source_sha256") != source_sha:
        return False, (
            f"{aligned.name} is stale: it was made from a {model.onnx_path.name} with sha256 "
            f"{record.get('source_sha256')}, and that file now has sha256 {source_sha}"
        )
    if record.get("aligned_sha256") != file_sha256(aligned):
        return False, f"{aligned.name} does not match the sha256 its sidecar {side.name} records"
    return True, "verified"


def default_load_plan(model: _Model) -> tuple[str | None, str]:
    """How `default_loader` will get an alignment-emitting session for this
    model, decided without loading anything: ("aligned-file", note),
    ("in-memory-patch", why the aligned copy is not used) or (None, why the
    model cannot be loaded here)."""
    if not _importable("onnxruntime"):
        return None, "the onnxruntime package is not installed"
    ok, why = check_aligned(model)
    if ok:
        return "aligned-file", why
    if _importable("onnx"):
        return "in-memory-patch", why
    return None, (
        f"{why}; patching {model.onnx_path.name} in memory needs the onnx package "
        "(piper-tts[alignment]; not a dependency of piper-tts itself), which is not installed"
    )


def write_aligned_copy(onnx_path: str | Path) -> dict[str, Any]:
    """(Re)make `<stem>.aligned.onnx` beside `<stem>.onnx` exactly as
    `python -m piper.patch_voice_with_alignment` does (onnx.load,
    add_alignment_output, onnx.save), then write the sidecar
    `<stem>.aligned.source.json` that `check_aligned` verifies. Both files
    are replaced atomically; a crash between them leaves a sidecar that no
    longer matches, so the copy is then not used."""
    import onnx
    from piper.patch_voice_with_alignment import add_alignment_output

    src = Path(onnx_path)
    if not src.name.endswith(".onnx") or src.name.endswith(ALIGNED_SUFFIX):
        raise ValueError(f"expected a <stem>.onnx voice, not {src.name}")
    if not src.is_file():
        raise FileNotFoundError(src)
    stem = src.name[: -len(".onnx")]
    out = src.with_name(stem + ALIGNED_SUFFIX)
    side = src.with_name(stem + SIDECAR_SUFFIX)
    source_sha = file_sha256(src)
    proto = onnx.load(str(src))
    output_name = add_alignment_output(proto)
    tmp = out.with_name(out.name + ".partial")
    onnx.save(proto, str(tmp))
    del proto
    os.replace(tmp, out)
    if file_sha256(src) != source_sha:
        raise RuntimeError(f"{src} changed while it was being patched; run again")
    record = {
        "schema": SIDECAR_SCHEMA,
        "source": src.name,
        "source_sha256": source_sha,
        "source_bytes": src.stat().st_size,
        "aligned": out.name,
        "aligned_sha256": file_sha256(out),
        "aligned_bytes": out.stat().st_size,
        "alignment_output": output_name,
        "tool": "piper.patch_voice_with_alignment.add_alignment_output + onnx.save",
        "piper_tts": _dist_version("piper-tts"),
        "onnx": _dist_version("onnx"),
    }
    tmp_side = side.with_name(side.name + ".partial")
    tmp_side.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_side, side)
    return record


def default_loader(model: _Model, threads: int):
    """A PiperVoice whose session also outputs the per-id durations: the
    verified aligned copy, else the model patched in memory."""
    kind, why = default_load_plan(model)
    if kind is None:
        raise BackendUnavailable(f"piper backend: {model.stem}: {why}")
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.config import PiperConfig

    if kind == "aligned-file":
        source: str | bytes = str(model.aligned_path)
    else:
        if model.aligned_path is not None:
            warnings.warn(
                f"piper backend: not using {model.aligned_path.name}: {why}; "
                f"patching {model.onnx_path.name} in memory instead",
                AlignedCopyIgnoredWarning,
                stacklevel=2,
            )
        try:
            import onnx
            from piper.patch_voice_with_alignment import add_alignment_output
        except ImportError as exc:
            raise BackendUnavailable(f"piper backend: {model.stem}: cannot import onnx to patch it: {exc}") from exc
        try:
            proto = onnx.load(str(model.onnx_path))
        except Exception as exc:  # protobuf DecodeError and friends
            raise RuntimeError(f"piper backend: {model.onnx_path} is not a readable ONNX model: {exc}") from exc
        try:
            add_alignment_output(proto)
        except ValueError as exc:
            if "already marked as output" not in str(exc):
                raise RuntimeError(f"piper backend: cannot add the alignment output to {model.onnx_path.name}: {exc}") from exc
        source = proto.SerializeToString()
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(threads)
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(source, sess_options=options, providers=["CPUExecutionProvider"])
    if len(session.get_outputs()) < 2:
        raise BackendUnavailable(f"piper backend: {model.stem} exposes no phoneme alignment output")
    return PiperVoice(session=session, config=PiperConfig.from_dict(model.config))


class PiperBackend:
    name = "piper"

    def __init__(
        self,
        voices_dir: Path | None = None,
        deterministic: bool = True,
        threads: int = DEFAULT_THREADS,
        context_phonemes: bool = True,
        loader: Callable[[_Model, int], Any] | None = None,
        voice_model: str | None = None,
    ) -> None:
        try:
            import piper  # type: ignore
        except Exception as exc:  # ImportError or a broken install
            raise BackendUnavailable(
                "piper backend: the `piper` package is not installed (pip install piper-tts); "
                "not installed by the harness on purpose"
            ) from exc
        if not hasattr(piper, "SynthesisConfig") or not hasattr(piper, "PiperVoice"):
            raise BackendUnavailable(
                "piper backend: this piper has no SynthesisConfig/PiperVoice API "
                "(piper-tts >= 1.3 required; the vendored reference is 1.8.0)"
            )
        self._config_cls = piper.SynthesisConfig
        self._dir = Path(voices_dir or VOICES_DIR)
        models = discover_models(self._dir)
        if not models:
            raise BackendUnavailable(
                f"piper backend: no <voice>.onnx + <voice>.onnx.json under {self._dir}; "
                "the harness never downloads voices"
            )
        # A voice file the default loader cannot open (no verified aligned
        # copy and no onnx to patch it, or no onnxruntime) is not offered;
        # when that leaves nothing, the backend is unavailable NOW, not at the
        # first render. A custom loader is trusted to load what it is given.
        self._unavailable: dict[str, str] = {}
        if loader is None:
            for m in models:
                kind, why = default_load_plan(m)
                if kind is None:
                    self._unavailable[m.stem] = why
            if len(self._unavailable) == len(models):
                raise BackendUnavailable(
                    f"piper backend: no voice under {self._dir} can be loaded with alignments: "
                    + "; ".join(f"{s}: {w}" for s, w in self._unavailable.items())
                )
        self._models = [m for m in models if m.stem not in self._unavailable]
        self._deterministic = bool(deterministic)
        self._threads = int(threads)
        self._context = bool(context_phonemes)
        self._loader = loader or default_loader
        self._custom_loader = loader is not None
        self._voice_model = voice_model
        if voice_model is not None:
            self._model(voice_model)  # KeyError / BackendUnavailable now, not later
        self._loaded: dict[str, Any] = {}
        self._sources: dict[str, str] = {}
        self._isolated: dict[tuple[str, str], list[str]] = {}

    # -- voices ----------------------------------------------------------------
    @property
    def unavailable(self) -> dict[str, str]:
        """Voice files present but not loadable here -> why."""
        return dict(self._unavailable)

    def voices(self) -> list[str]:
        return [v for m in self._models for v in m.voice_names()]

    def _model(self, stem: str) -> _Model:
        for m in self._models:
            if m.stem == stem:
                return m
        if stem in self._unavailable:
            raise BackendUnavailable(f"piper backend: {stem}: {self._unavailable[stem]}")
        raise KeyError(f"unknown piper voice model {stem!r}; have {[m.stem for m in self._models]}")

    def resolve(self, voice: str) -> tuple[_Model, int | None]:
        stem, _, reader = voice.partition(":")
        try:
            m = self._model(stem)
        except KeyError:
            raise KeyError(f"unknown piper voice {voice!r}") from None
        if m.num_speakers > 1:
            sid_map = m.config.get("speaker_id_map") or {}
            if reader not in sid_map:
                raise KeyError(f"unknown piper voice {voice!r}: {stem} has no speaker {reader!r}")
            return m, int(sid_map[reader])
        if reader:
            raise KeyError(f"unknown piper voice {voice!r}: {stem} is a single-speaker voice")
        return m, None

    def default_voice_model(self) -> str | None:
        """The model auto-assignment draws from when none is pinned
        (HARNESS_POLICY): DEFAULT_VOICE_MODEL when it is offered; else the
        first model by name with a measured palette; else the first
        multi-speaker model (evenly spaced ids, with a warning); None when
        every model is single-speaker."""
        stems = [m.stem for m in self._models]
        if DEFAULT_VOICE_MODEL in stems:
            return DEFAULT_VOICE_MODEL
        for m in self._models:
            if m.measured_palette():
                return m.stem
        multi = [m.stem for m in self._models if m.num_speakers > 1]
        return multi[0] if multi else None

    def _auto_model(self, model: str | None) -> _Model | None:
        stem = model or self._voice_model or self.default_voice_model()
        return None if stem is None else self._model(stem)

    def palette(self, model: str | None = None) -> list[str]:
        """The auto-assignment palette of `model` (default: the pinned
        voice_model, else `default_voice_model()`): its measured PALETTES
        entry, else 16 ids evenly spaced over its speakers with a
        PaletteFallbackWarning. A single-speaker model is its own one-voice
        palette; with only single-speaker models, every model stem."""
        m = self._auto_model(model)
        if m is None:
            return [x.stem for x in self._models]
        if m.num_speakers <= 1:
            return [m.stem]
        entries = m.measured_palette()
        if entries:
            return [f"{m.stem}:{p.reader}" for p in entries]
        warnings.warn(
            f"piper backend: {m.stem} has no measured palette in PALETTES; auto-assignment uses 16 evenly "
            "spaced speaker ids, without the low/high pitch alternation (docs/generator.md section 6.2)",
            PaletteFallbackWarning,
            stacklevel=2,
        )
        readers = m.readers
        k = min(16, len(readers))
        ids = sorted({int(round(j * (len(readers) - 1) / max(1, k - 1))) for j in range(k)})
        return [f"{m.stem}:{readers[i]}" for i in ids]

    def assign_voices(self, n_speakers: int, seed: int, model: str | None = None) -> list[str]:
        """Deterministic, distinct voices for n speakers (HARNESS_POLICY):
        `palette_indices` over `palette(model)`; beyond the palette, further
        ids evenly spaced over the rest of the same model's speakers."""
        pal = self.palette(model)
        if n_speakers <= len(pal):
            return [pal[i] for i in palette_indices(n_speakers, seed, len(pal))]
        m = self._auto_model(model)
        pool = m.voice_names() if m is not None else self.voices()
        rest = [v for v in pool if v not in pal]
        need = n_speakers - len(pal)
        if need > len(rest):
            raise ValueError(
                f"piper offers {len(pal) + len(rest)} voices, fewer than n_speakers={n_speakers}; "
                "set speakers.voices explicitly to repeat one"
            )
        # spacing >= 1, so the rounded positions strictly increase: distinct
        extra = [rest[int(round(j * (len(rest) - 1) / (need - 1)))] for j in range(need)] if need > 1 else [rest[0]]
        start = int(seed) % len(pal)
        return pal[start:] + pal[:start] + extra

    # -- rendering ---------------------------------------------------------------
    def _load(self, model: _Model):
        if model.stem not in self._loaded:
            if self._custom_loader:
                source = "custom-loader"
            else:
                source = default_load_plan(model)[0] or "unavailable"
            self._loaded[model.stem] = self._loader(model, self._threads)
            self._sources[model.stem] = source
        return self._loaded[model.stem]

    def model_notes(self, model: _Model) -> dict[str, Any]:
        """Identity of the voice files behind a render (for the turn notes)."""
        return {
            "voice_model": model.stem,
            "voice_sha256": file_sha256(model.onnx_path),
            "config_sha256": file_sha256(model.config_path),
            "model_source": self._sources.get(model.stem),
        }

    def _isolated_phonemes(self, voice_obj, model: _Model, token: str) -> list[str]:
        key = (model.stem, token)
        if key not in self._isolated:
            self._isolated[key] = _strip_edges([p for s in voice_obj.phonemize(token) for p in s])
        return self._isolated[key]

    def _phonemize(self, voice_obj, model: _Model, tokens: list[str]) -> tuple[list[list[str]], str]:
        isolated = [self._isolated_phonemes(voice_obj, model, t) for t in tokens]
        for t, ph in zip(tokens, isolated):
            if not ph:
                raise ValueError(f"piper's phonemizer gives no phonemes for {t!r}")
        if self._context and all(len(_split_words(ph)) == 1 for ph in isolated):
            sentences = voice_obj.phonemize(" ".join(tokens) + SENTENCE_FINAL)
            if len(sentences) == 1:
                flat = _strip_edges(list(sentences[0]))
                groups = _split_words(flat)
                # one group per token, and each group starts like its token
                # alone: a fusion plus a split elsewhere would keep the count
                # but shift the groups, and the first phonemes then disagree
                if (
                    len(groups) == len(tokens)
                    and not any(p in PUNCTUATION for g in groups for p in g)
                    and all(_first_phoneme(g) == _first_phoneme(i) for g, i in zip(groups, isolated))
                ):
                    return groups, "context"
        return [list(ph) for ph in isolated], "per-token"

    def render(self, text: str, voice: str) -> PiperRender:
        tokens = check_text(text)
        model, sid = self.resolve(voice)
        voice_obj = self._load(model)
        words, mode = self._phonemize(voice_obj, model, tokens)
        ids, owners, dropped = build_phoneme_ids(words, model.config["phoneme_id_map"])
        if self._deterministic:
            cfg = self._config_cls(speaker_id=sid, noise_scale=0.0, noise_w_scale=0.0)
        else:
            cfg = self._config_cls(speaker_id=sid)
        out = voice_obj.phoneme_ids_to_audio(ids, cfg, include_alignments=True)
        if not isinstance(out, tuple) or out[1] is None:
            raise RuntimeError(f"piper voice {model.stem} returned no phoneme alignment; it cannot give exact timings")
        audio = np.asarray(out[0], dtype=np.float32).reshape(-1)
        samples = np.asarray(out[1], dtype=np.int64).reshape(-1)
        if len(samples) != len(ids) or np.any(samples < 0) or int(samples.sum()) != len(audio):
            raise AssertionError(
                f"piper alignment does not tile the audio: {len(samples)} counts for {len(ids)} ids, "
                f"sum {int(samples.sum())} vs {len(audio)} samples"
            )
        return PiperRender(
            text=text,
            tokens=tokens,
            voice=voice,
            speaker_id=sid,
            mode=mode,
            word_phonemes=words,
            phoneme_ids=ids,
            owners=owners,
            id_samples=samples,
            audio=audio,
            model_rate=model.sample_rate,
            dropped_phonemes=dropped,
            noise_scale=cfg.noise_scale,
            noise_w_scale=cfg.noise_w_scale,
        )

    def synth(self, text: str, voice: str, seed: int) -> SynthResult:
        r = self.render(text, voice)
        res = render_to_result(r, self.name, self._deterministic, self._threads)
        res.notes.update(self.model_notes(self.resolve(voice)[0]))
        return res


def render_to_result(r: PiperRender, backend: str = "piper", deterministic: bool = True, threads: int | None = None) -> SynthResult:
    """Resample a render to 16 kHz, cut it to [first word start, last word
    end) and map every word boundary with `map_index`."""
    from scipy.signal import resample_poly

    g = gcd(r.model_rate, SAMPLE_RATE)
    up, down = SAMPLE_RATE // g, r.model_rate // g
    spans = r.word_spans()
    x = r.audio.astype(np.float64)
    y = resample_poly(x, up, down) if (up, down) != (1, 1) else x.copy()
    a16, b16 = map_index(spans[0][0], up, down), map_index(spans[-1][1], up, down)
    if not (0 <= a16 < b16 <= len(y)):
        raise AssertionError(f"mapped utterance [{a16}, {b16}) outside the resampled audio ({len(y)})")
    seg = y[a16:b16].copy()
    n_fade = min(int(round(EDGE_FADE_S * SAMPLE_RATE)), len(seg) // 2)
    if n_fade > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(n_fade) + 0.5) / n_fade)
        seg[:n_fade] *= ramp
        seg[-n_fade:] *= ramp[::-1]
    peak = float(np.max(np.abs(seg)))
    if peak > 0:
        seg *= (10.0 ** (PEAK_DBFS / 20.0)) / peak
    pcm = np.clip(np.round(seg * 32767.0), -32768, 32767).astype(np.int16)
    words = [
        WordTiming(tok, map_index(s, up, down) - a16, map_index(e, up, down) - a16)
        for tok, (s, e) in zip(r.tokens, spans)
    ]
    return SynthResult(
        pcm=pcm,
        words=words,
        backend=backend,
        voice=r.voice,
        timing_exact=True,
        notes={
            "timing_method": "piper-duration-alignment",
            "phonemization": r.mode,
            "speaker_id": r.speaker_id,
            "model_rate": r.model_rate,
            "resample": [up, down],
            "model_n_samples": int(len(r.audio)),
            "model_word_spans": [list(s) for s in spans],
            "model_cut": [spans[0][0], spans[-1][1]],
            "boundary_rounding": "round-half-up(x*up/down); <= 0.5 sample at 16 kHz",
            "dropped_phonemes": list(r.dropped_phonemes),
            "noise_scale": r.noise_scale,
            "noise_w_scale": r.noise_w_scale,
            "deterministic_by_config": deterministic,
            "onnxruntime_threads": threads,
            "seed_ignored": True,
        },
    )


__all__ = [
    "ALIGNED_SUFFIX",
    "AlignedCopyIgnoredWarning",
    "DEFAULT_THREADS",
    "DEFAULT_VOICE_MODEL",
    "LIBRITTS_R_MEDIUM_PALETTE",
    "PALETTES",
    "PALETTE_F0_TEXT",
    "PaletteFallbackWarning",
    "PaletteVoice",
    "PiperBackend",
    "PiperRender",
    "SIDECAR_SCHEMA",
    "SIDECAR_SUFFIX",
    "VOICES_DIR",
    "build_phoneme_ids",
    "check_aligned",
    "check_text",
    "default_load_plan",
    "default_loader",
    "discover_models",
    "file_sha256",
    "map_index",
    "median_f0",
    "palette_indices",
    "render_to_result",
    "write_aligned_copy",
]


def main(argv: list[str] | None = None) -> int:
    """python -m generator.tts.piper {align,check}"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m generator.tts.piper", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("align", help="(re)make <stem>.aligned.onnx and its sidecar beside <stem>.onnx (needs onnx)")
    al.add_argument("models", nargs="+", help="<stem>.onnx voice files")
    ch = sub.add_parser("check", help="print each voice's load plan (aligned-file / in-memory-patch / unavailable)")
    ch.add_argument("--voices-dir", default=None, help=f"default {VOICES_DIR}")
    args = parser.parse_args(argv)
    if args.cmd == "align":
        for path in args.models:
            print(json.dumps(write_aligned_copy(path), indent=2, sort_keys=True))
        return 0
    voices_dir = Path(args.voices_dir) if args.voices_dir else VOICES_DIR
    models = discover_models(voices_dir)
    if not models:
        print(f"no <voice>.onnx + <voice>.onnx.json under {voices_dir}")
        return 1
    worst = 0
    for m in models:
        kind, why = default_load_plan(m)
        print(f"{m.stem}: {kind or 'UNAVAILABLE'} ({why})")
        worst = worst if kind else 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
