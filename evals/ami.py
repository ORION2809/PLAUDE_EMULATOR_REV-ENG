"""AMI Meeting Corpus (NXT manual annotations) -> harness meeting directories.

The V5 reference side: turns the AMI manual annotations (NITE XML Toolkit
format, CC BY 4.0) plus a Mix-Headset WAV into the Layer 2 -> Layer 3
contract, ``plaud-harness/meeting/1``::

    <out>/<MEETING_ID>/meeting.json   one segment per speech turn, words from words/
    <out>/<MEETING_ID>/ref.rttm       written from meeting.json (evals.io)
    <out>/<MEETING_ID>/ref.stm        written from meeting.json (evals.io)
    <out>/<MEETING_ID>/mix.wav        read-only hard link to (or copy of) <MEETING_ID>.Mix-Headset.wav

Why here and not under scripts/: docs/evals.md makes ``evals/io.py`` the only
writer of the contract, so the converter builds :class:`evals.io.Meeting`
objects and writes them with ``evals.io.write_meeting`` and
``write_reference_files``; a module inside the package imports that without
path tricks, is importable by the tests, and runs as ``python -m evals.ami``.
Its own imports are the standard library and ``evals.io`` (importing the
``evals`` package also loads ``evals.metrics``, which the self-score uses).

NXT inputs read (paths relative to the annotations root)::

    corpusResources/meetings.xml          <meeting observation=ID><speaker nxt_agent= global_name= channel= role=/>
    words/<ID>.<AGENT>.words.xml          <w starttime= endtime= [punc] [trunc]>text</w>, <vocalsound/>,
                                          <disfmarker/>, <gap/>, <transformerror/>, ...

The segments layer (segments/<ID>.<AGENT>.segments.xml) is not read: an NXT
``<segment>`` runs over vocal sounds and pauses between its words, so its
span is not speech time (docs/evals.md "AMI" has the numbers).

Conventions.  Where a published AMI setup fixes the choice it is cited;
every other choice is HARNESS_POLICY.  Sources (read 2026-09-25, each file
verified by its git blob SHA against the commit named):

* BUT = BUTSpeechFIT/AMI-diarization-setup, commit 2509d893 (README.md and
  ``only_words/rttms/*``).  README: "All words are considered as speech and
  included in the references"; "adjacent speech segments (words) of the same
  speaker are merged not to create false break points.  Consecutive speech
  segments from the same speaker separated by pauses (silence) are not
  merged in any case"; it prefers ``only_words`` to
  ``word_and_vocalsounds``.
* lhotse = lhotse-speech/lhotse, commit 1f22c586 (``lhotse/recipes/ami.py``
  ``parse_ami_annotations``, ``lhotse/recipes/utils.py`` ``normalize_text_ami``).

Speech time (what ref.rttm holds):

* Speaker id = the participant's ``global_name`` from meetings.xml (lhotse:
  ``global_spk_id[local_id] = speaker.attrib["global_name"]``; the BUT
  RTTMs label speakers the same way).  Every participant listed for the
  meeting is declared, including one with no words.
* A speaker's speech time is the union of the intervals of all its timed
  ``<w>`` elements, punctuation and ``..`` included (BUT: "all words").
  Intervals that overlap or touch (next start <= running end) merge into one
  turn; any gap, however short, starts a new turn (BUT).  Vocal and
  non-vocal sounds, disfluency markers, gaps and transform errors are not
  ``<w>`` and are neither speech time nor text (BUT ``only_words``).
* The running end of a turn is the max of the merged ends (HARNESS_POLICY).
  BUT's published RTTMs are reproduced exactly by replacing the running end
  with each merged word's end instead.  The two rules can differ only when a
  merged word ends before the running end, and they do in 2 of the 170
  meetings BUT covers (EN2006a, TS3009c, both in its train split;
  docs/evals.md "AMI").
* A zero-length turn (an isolated zero-length ``<w>``) is dropped.
* One contract segment per turn.  Segments of one speaker therefore never
  overlap or touch.  Different speakers' turns overlap as annotated.

Text (what ref.stm and the words hold):

* Scorable words = ``<w>`` elements without ``punc="true"`` whose text
  normalises to at least one token (lhotse keeps only ``<w>``; its Kaldi
  normaliser removes punctuation).  Each goes to the turn that contains its
  interval, in document order.  A turn may have no scorable word (``..``
  or punctuation with a duration); it keeps its speech time and has empty
  text.
* A scorable word with no speech time is dropped from the text and counted
  (HARNESS_POLICY): an untimed word, an isolated zero-length word, or one
  starting at or after the end of the audio.
* Truncated words (``trunc="true"``) are kept as written, e.g. ``th``
  (lhotse keeps ``word.text``).  ``Policy(truncated="drop")`` removes them
  from the text only; their time stays speech (HARNESS_POLICY option).
* Text: every run of characters other than word characters and ``'`` becomes
  a space (Kaldi-style: lhotse ``normalize_text_ami(..., "kaldi")`` does
  ``re.sub(r"[^A-Z0-9']+", " ", text)``).  Then ``evals.io.DEFAULT_NORMALIZER``
  runs, the same normaliser the scorer applies to both sides.  So ``T_V_s``
  becomes ``t v s``, ``Mm-hmm`` becomes ``mm hmm`` and ``now.I'm`` becomes
  ``now i'm``.  Unlike the Kaldi normaliser, ``MM-HMM``/``UH-HUH``/``OK`` are
  not kept as single tokens: the evals normaliser splits every hyphen, on the
  hypothesis too (HARNESS_POLICY).  The stored text is a fixed point of the
  normaliser, so scoring with or without normalisation gives the same tokens.
* A word that normalises to several tokens has its interval split equally,
  the rule ``evals.io.normalized_words`` applies to hypotheses.

Timing repairs (HARNESS_POLICY, each counted in ``generator.scenario.counts``):
end < start becomes end = start; a ``<w>`` starting at or after the end of
the audio is dropped and an end past it is clipped; within a turn, a word
starting before the previous word is raised to that start (the contract
wants words sorted).

* Times are rounded to 6 decimals, the precision evals.io writes RTTM/STM
  with.
* ``duration_s``, ``sample_rate`` and ``channels`` come from the WAV header
  (frames / rate), not from meetings.xml's ``duration`` attribute.  The
  attribute is recorded, and for ES2004a it is 1186 s against 1049.35 s of
  audio.
* ``mix.wav`` is a hard link to the source WAV (no extra disk).  If the
  filesystem refuses a link, it is a copy.  It is never a symlink, because
  ``pipeline.meeting.resolve_audio`` refuses an audio path that resolves
  outside the meeting directory.  Either way its write bits are removed.
  A hard link shares the source's inode, so that also makes the raw corpus
  WAV read-only: a writer that reopens mix.wav in place fails instead of
  rewriting the corpus (HARNESS_POLICY).  The source is checked against a
  SHA256SUMS file when one lists it, and ``stats`` re-hashes every mix.wav
  against the sha256 recorded in meeting.json.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import traceback
import wave
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from evals.io import (
    DEFAULT_NORMALIZER,
    ContractError,
    Meeting,
    Segment,
    Word,
    load_meeting,
    load_rttm,
    load_stm,
    rttm_lines,
    sorted_segments,
    write_meeting,
    write_reference_files,
)

CONVERTER_NAME = "evals.ami"
#: Bump when a convention changes the output for the same inputs.
#: 2: speech turns from the union of <w> intervals (was: one per NXT segment).
CONVERTER_VERSION = "2"

NITE_NS = "http://nite.sourceforge.net/"
_NITE_ID = f"{{{NITE_NS}}}id"

#: HARNESS_POLICY: the members of the manual-annotations zip the converter
#: reads, plus the release notes and licence kept beside them for provenance.
ANNOTATION_MEMBERS: tuple[str, ...] = (
    "words/",
    "corpusResources/meetings.xml",
    "00README_MANUAL.txt",
    "LICENCE.txt",
    "MANIFEST_MANUAL.txt",
)
EXTRACT_MANIFEST = "EXTRACTED_FROM.json"

#: Files the converter owns inside a meeting directory.  A rewrite removes
#: them first; any other non-hidden entry makes the rewrite refuse
#: (HARNESS_POLICY, the generator's rule in docs/generator.md section 2).
OWNED_FILES: tuple[str, ...] = ("meeting.json", "ref.rttm", "ref.stm", "mix.wav")

#: HARNESS_POLICY: default local layout (docs/evals.md "AMI"), resolved
#: against the repository root so the CLI works from any directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = REPO_ROOT / "data/corpora/ami/raw/ami_public_manual_1.6.2.zip"
DEFAULT_ANNOTATIONS = REPO_ROOT / "data/corpora/ami/raw/annotations"
DEFAULT_AUDIO_DIR = REPO_ROOT / "data/corpora/ami/raw/audio"
DEFAULT_CHECKSUMS = REPO_ROOT / "data/corpora/ami/raw/SHA256SUMS.local"
DEFAULT_OUT = REPO_ROOT / "data/corpora/ami/meetings"
#: The BUT setup's only_words RTTMs, laid out as in its repository
#: (``only_words/rttms/{train,dev,test}/<ID>.rttm``); ``crosscheck`` reads them.
DEFAULT_BUT_RTTMS = REPO_ROOT / "data/corpora/ami/but_setup/only_words/rttms"
MIX_HEADSET_SUFFIX = ".Mix-Headset.wav"
#: Stand-in audio length for building a meeting whose WAV is not here
#: (``crosscheck``, the whole-release test): longer than any AMI meeting.
STAND_IN_FRAMES = 16000 * 4 * 3600

_TIME_DECIMALS = 6
_SPLIT_NON_WORD = re.compile(r"[^\w']+")

CITATIONS: dict[str, str] = {
    "but_setup": "BUTSpeechFIT/AMI-diarization-setup commit 2509d8933721023fab4def2618aabd5c28eb82e9 "
    "README.md (read 2026-09-25): 'All words are considered as speech'; adjacent words of a speaker merged, "
    "turns separated by pauses never merged; only_words preferred over word_and_vocalsounds",
    "lhotse": "lhotse-speech/lhotse commit 1f22c586044c95047269989f31a6b474b6af3c7d, lhotse/recipes/ami.py "
    "parse_ami_annotations and lhotse/recipes/utils.py normalize_text_ami (read 2026-09-25)",
}

#: Machine-readable record of the conventions above, written into
#: meeting.json ``generator.scenario.conventions``.
CONVENTIONS: dict[str, str] = {
    "speaker_id": "meetings.xml global_name (lhotse; BUT RTTM labels)",
    "speech_time": "per speaker, union of the intervals of all timed <w> (punctuation and '..' included); "
    "merge when next start <= running end, split at any gap (BUT only_words); zero-length turns dropped",
    "running_end": "max of merged ends (HARNESS_POLICY; BUT replaces it with each merged word's end)",
    "segments": "one per speech turn; the segments layer is not read",
    "not_speech_not_text": "vocalsound, nonvocalsound, disfmarker, gap, transformerror and any non-<w> element",
    "scorable": "<w> without punc=true whose text normalises to >= 1 token, placed in the turn containing it; "
    "one without speech time (untimed, isolated zero-length, past the audio end) is dropped (HARNESS_POLICY)",
    "truncated": "see policy.truncated (keep: lhotse); drop removes text only, speech time stays",
    "text": "re.sub(r\"[^\\w']+\", ' ') (Kaldi-style, lhotse normalize_text_ami) then evals.io.DEFAULT_NORMALIZER; "
    "no MM-HMM/UH-HUH/OK exceptions (HARNESS_POLICY)",
    "multi_token_words": "interval split equally (evals.io.normalized_words rule)",
    "timing_repairs": "end<start -> end=start; <w> at/after the audio end dropped, ends clipped; a word starting "
    "before the previous word of its turn raised to that start (HARNESS_POLICY)",
    "time_rounding": "6 decimals (evals.io write precision)",
    "duration": "WAV frames / sample rate",
    "mix_wav": "hard link to <ID>.Mix-Headset.wav, copy if linking fails; never a symlink; write bits removed "
    "(on the shared inode for a hard link)",
}


class AmiError(ValueError):
    """The annotations or audio cannot be converted.  The message names the file."""


# --------------------------------------------------------------------------- #
# NXT readers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Participant:
    agent: str
    global_name: str
    channel: int | None
    role: str | None

    def to_speaker(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.global_name, "nxt_agent": self.agent}
        if self.channel is not None:
            d["channel"] = self.channel
        if self.role is not None:
            d["role"] = self.role
        return d


@dataclass(frozen=True)
class NxtToken:
    """One child element of a words file, in document order."""

    id: str
    kind: str  # element tag: "w", "vocalsound", "disfmarker", "gap", ...
    text: str
    start: float | None
    end: float | None
    punc: bool = False
    trunc: bool = False


def _parse_xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except FileNotFoundError:
        raise AmiError(f"{path}: file not found") from None
    except ET.ParseError as exc:
        raise AmiError(f"{path}: not well-formed XML ({exc})") from None


def _float_attr(el: ET.Element, name: str, where: str) -> float | None:
    raw = el.get(name)
    if raw is None:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise AmiError(f"{where}: attribute {name}={raw!r} is not a number") from None
    return v if math.isfinite(v) else None


def list_meetings(meetings_xml: Path) -> list[str]:
    """Every ``observation`` in corpusResources/meetings.xml, in file order."""
    return [m.get("observation", "") for m in _parse_xml(meetings_xml) if m.get("observation")]


def read_participants(meetings_xml: Path, meeting_id: str) -> list[Participant]:
    """The participants meetings.xml lists for ``meeting_id``, sorted by NXT agent letter."""
    for m in _parse_xml(meetings_xml):
        if m.get("observation") != meeting_id:
            continue
        out: list[Participant] = []
        for spk in m:
            if spk.tag != "speaker":
                continue
            agent, name = spk.get("nxt_agent"), spk.get("global_name")
            if not agent or not name:
                raise AmiError(f"{meetings_xml}: {meeting_id} has a speaker without nxt_agent/global_name")
            ch = spk.get("channel")
            out.append(Participant(agent, name, int(ch) if ch is not None and ch.isdigit() else None, spk.get("role")))
        if not out:
            raise AmiError(f"{meetings_xml}: meeting {meeting_id} lists no speakers")
        agents = [p.agent for p in out]
        names = [p.global_name for p in out]
        if len(set(agents)) != len(agents) or len(set(names)) != len(names):
            raise AmiError(f"{meetings_xml}: meeting {meeting_id} repeats an nxt_agent or global_name: {out}")
        return sorted(out, key=lambda p: p.agent)
    raise AmiError(f"{meetings_xml}: no meeting with observation={meeting_id!r}")


def read_words(path: Path) -> list[NxtToken]:
    """All child elements of a words file in document order (words and non-words)."""
    out: list[NxtToken] = []
    seen: set[str] = set()
    for el in _parse_xml(path):
        nid = el.get(_NITE_ID)
        if not nid:
            raise AmiError(f"{path}: <{el.tag}> without nite:id")
        if nid in seen:
            raise AmiError(f"{path}: duplicate nite:id {nid}")
        seen.add(nid)
        where = f"{path}#{nid}"
        out.append(
            NxtToken(
                id=nid,
                kind=el.tag,
                text=el.text or "",
                start=_float_attr(el, "starttime", where),
                end=_float_attr(el, "endtime", where),
                punc=el.get("punc") == "true",
                trunc=el.get("trunc") == "true",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def word_tokens(text: str) -> list[str]:
    """Scoring tokens of one ``<w>`` text: Kaldi-style split, then the evals normaliser."""
    return DEFAULT_NORMALIZER.tokens(_SPLIT_NON_WORD.sub(" ", text))


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Policy:
    """The switchable conventions.  Everything else is fixed (module docstring)."""

    truncated: str = "keep"  # "keep" (lhotse) | "drop" (HARNESS_POLICY option)

    def __post_init__(self) -> None:
        if self.truncated not in ("keep", "drop"):
            raise ValueError(f"Policy.truncated must be 'keep' or 'drop', got {self.truncated!r}")

    def to_dict(self) -> dict[str, str]:
        return {"truncated": self.truncated}


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    channels: int
    frames: int

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate


STAND_IN_AUDIO = AudioInfo(16000, 1, STAND_IN_FRAMES)


def read_wav_info(path: Path) -> AudioInfo:
    """Header facts of a PCM WAV (standard library ``wave``)."""
    try:
        with wave.open(str(path), "rb") as w:
            info = AudioInfo(w.getframerate(), w.getnchannels(), w.getnframes())
    except FileNotFoundError:
        raise AmiError(f"{path}: audio file not found") from None
    except (wave.Error, EOFError) as exc:
        raise AmiError(f"{path}: not a PCM WAV file ({exc})") from None
    if info.sample_rate <= 0 or info.frames <= 0:
        raise AmiError(f"{path}: empty audio ({info})")
    return info


def _r(x: float) -> float:
    return round(x, _TIME_DECIMALS)


def _union(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted union of closed intervals; intervals that overlap or touch merge
    (next start <= running end), and the running end is the max of the ends."""
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _w_spans(tokens: Sequence[NxtToken], duration_s: float, counts: Counter) -> list[tuple[float, float] | None]:
    """The repaired interval of each ``<w>`` token (None: not a word, or no speech time)."""
    spans: list[tuple[float, float] | None] = []
    for tok in tokens:
        if tok.kind != "w":
            spans.append(None)
            continue
        counts["w_elements"] += 1
        if tok.start is None or tok.end is None:
            counts["w_untimed"] += 1
            spans.append(None)
            continue
        start, end = tok.start, tok.end
        if end < start:
            end = start
            counts["w_negative_duration_clamped"] += 1
        if start >= duration_s:
            counts["w_after_audio_end_dropped"] += 1
            spans.append(None)
            continue
        if end > duration_s:
            end = duration_s
            counts["w_ends_clipped_to_audio"] += 1
        spans.append((start, end))
    return spans


def speaker_segments(
    speaker: str,
    tokens: Sequence[NxtToken],
    duration_s: float,
    policy: Policy,
    counts: Counter,
) -> list[Segment]:
    """One speaker's contract segments: its speech turns, each with the scorable words inside it."""
    spans = _w_spans(tokens, duration_s, counts)
    merged = _union(sp for sp in spans if sp is not None)
    turns = [(a, b) for a, b in merged if b > a]
    if len(merged) > len(turns):
        counts["zero_length_turns_dropped"] += len(merged) - len(turns)
    starts = [a for a, _ in turns]
    words: list[list[Word]] = [[] for _ in turns]
    prev_start = [-math.inf] * len(turns)

    for tok, span in zip(tokens, spans):
        if tok.kind != "w":
            counts[f"dropped_{tok.kind}"] += 1  # neither speech time nor text
            continue
        if tok.punc:
            counts["punctuation_not_text"] += 1
            continue
        toks = word_tokens(tok.text)
        if not toks:
            counts["dropped_empty_after_normalisation"] += 1
            continue
        i = bisect.bisect_right(starts, span[0]) - 1 if span is not None else -1
        if span is None or i < 0 or span[1] > turns[i][1]:
            counts["words_without_speech_time_dropped"] += 1
            continue
        if tok.trunc:
            if policy.truncated == "drop":
                counts["dropped_truncated"] += 1
                continue
            counts["kept_truncated"] += 1
        start, end = span
        if start < prev_start[i]:
            start = prev_start[i]
            end = max(end, start)
            counts["nonmonotonic_words_clamped"] += 1
        prev_start[i] = start
        if len(toks) > 1:
            counts["words_split_into_several_tokens"] += 1
        step = (end - start) / len(toks)
        for k, t in enumerate(toks):
            words[i].append(
                Word(t, _r(start + k * step), _r(start + (k + 1) * step) if k < len(toks) - 1 else _r(end))
            )

    out: list[Segment] = []
    for (a, b), ws in zip(turns, words):
        if not ws:
            counts["turns_without_text"] += 1
        out.append(Segment(speaker, _r(a), _r(b), " ".join(w.w for w in ws), tuple(ws)))
    return out


@dataclass
class BuildResult:
    meeting: Meeting
    counts: dict[str, int]


def build_meeting(
    annotations: Path,
    meeting_id: str,
    audio: AudioInfo,
    *,
    policy: Policy = Policy(),
    audio_meta: dict[str, Any] | None = None,
) -> BuildResult:
    """Build (but do not write) the contract meeting for ``meeting_id``."""
    annotations = Path(annotations)
    meetings_xml = annotations / "corpusResources" / "meetings.xml"
    participants = read_participants(meetings_xml, meeting_id)
    meeting_el = next(m for m in _parse_xml(meetings_xml) if m.get("observation") == meeting_id)
    duration_s = audio.duration_s
    counts: Counter = Counter()
    segments: list[Segment] = []

    for p in participants:
        words_path = annotations / "words" / f"{meeting_id}.{p.agent}.words.xml"
        if not words_path.exists():
            counts["participants_without_words_file"] += 1
            continue
        own = speaker_segments(p.global_name, read_words(words_path), duration_s, policy, counts)
        for prev, nxt in zip(own, own[1:]):  # invariant: one speaker's turns never overlap or touch
            if not prev.end < nxt.start:
                raise AssertionError(f"{meeting_id}: {p.global_name} turns {prev} and {nxt} overlap or touch")
        segments += own

    if not segments:
        raise AmiError(
            f"{annotations}: meeting {meeting_id} yields no speech turn "
            "(no timed <w> in the words files of its participants?); an empty reference cannot be scored"
        )
    segments = sorted_segments(segments)
    counts["segments"] = len(segments)
    counts["words"] = sum(len(s.words) for s in segments)
    for s in segments:
        if DEFAULT_NORMALIZER(s.text) != s.text:  # the stored text must be a normaliser fixed point
            raise AssertionError(f"{meeting_id}: text is not normalised: {s.text!r}")

    readme = annotations / "00README_MANUAL.txt"
    release = readme.read_text(encoding="latin-1").splitlines()[0].strip() if readme.exists() else None
    extracted = annotations / EXTRACT_MANIFEST
    scenario: dict[str, Any] = {
        "source": "AMI Meeting Corpus, NXT manual annotations",
        "licence": "CC BY 4.0 (annotations and audio); see LICENCE.txt beside the annotations",
        "attribution": "AMI Consortium; Carletta et al. 2006, 'The AMI meeting corpus: a pre-announcement'",
        "annotations_release": release,
        "annotations_extracted_from": json.loads(extracted.read_text()) if extracted.exists() else None,
        "meeting_type": meeting_el.get("type"),
        "meetings_xml_duration_s": _float_attr(meeting_el, "duration", str(meetings_xml)),
        "policy": policy.to_dict(),
        "conventions": CONVENTIONS,
        "citations": CITATIONS,
        "counts": dict(sorted(counts.items())),
    }
    meeting = Meeting(
        meeting_id=meeting_id,
        sample_rate=audio.sample_rate,
        duration_s=duration_s,
        channels=audio.channels,
        speakers=[p.to_speaker() for p in participants],
        segments=segments,
        audio={"mix_wav": "mix.wav", "stems": {}, "device": {}, **({"mix_source": audio_meta} if audio_meta else {})},
        generator={
            "name": CONVERTER_NAME,
            "version": CONVERTER_VERSION,
            "timing_exact": False,
            "scenario": scenario,
        },
    )
    return BuildResult(meeting, dict(counts))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_checksums(path: Path) -> dict[str, str]:
    """``sha256sum``-style lines (``<hex>  <path>`` or ``<hex> *<path>``) -> {file name: hex}.

    Keyed by file name because SHA256SUMS.local lists repository-relative paths.
    A name listed twice with different digests is refused.
    """
    out: dict[str, str] = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise AmiError(f"{path}:{n}: not a '<sha256>  <path>' line: {line!r}")
        name = Path(parts[1].strip().lstrip("*")).name
        digest = parts[0].lower()
        if out.get(name, digest) != digest:
            raise AmiError(f"{path}: {name} is listed with two different digests")
        out[name] = digest
    return out


def _prepare_dir(out_dir: Path) -> None:
    """Remove the converter's own files; refuse a directory holding anything else."""
    if out_dir.exists():
        if not out_dir.is_dir():
            raise FileExistsError(f"{out_dir} exists and is not a directory")
        foreign = sorted(p.name for p in out_dir.iterdir() if not p.name.startswith(".") and p.name not in OWNED_FILES)
        if foreign:
            raise FileExistsError(
                f"{out_dir} holds entries the AMI converter did not write: {foreign}; "
                "remove them or convert into a fresh directory"
            )
        for name in OWNED_FILES:
            p = out_dir / name
            if p.is_symlink() or p.exists():
                p.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)


def _remove_write_bits(path: Path) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _place_audio(src: Path, dst: Path, link: str) -> str:
    """Hard link (or copy) ``src`` to ``dst`` and remove the write bits.

    For a hard link the mode belongs to the shared inode, so the source WAV
    becomes read-only too: that is the point (module docstring, mix.wav).
    """
    placed = "copy"
    if link == "hardlink":
        try:
            os.link(src, dst)
            placed = "hardlink"
        except OSError:
            pass
    elif link != "copy":
        raise ValueError(f"link must be 'hardlink' or 'copy', got {link!r}")
    if placed == "copy":
        shutil.copyfile(src, dst)
    _remove_write_bits(dst)
    return placed


@dataclass
class ConversionResult:
    meeting_dir: Path
    meeting: Meeting
    counts: dict[str, int]
    audio_placed_as: str


def convert_meeting(
    annotations: Path,
    meeting_id: str,
    audio_path: Path,
    out_root: Path,
    *,
    policy: Policy = Policy(),
    link: str = "hardlink",
    checksums: Path | None = None,
) -> ConversionResult:
    """Write ``<out_root>/<meeting_id>/`` and read it back through evals.io.

    With ``checksums`` (a sha256sum-style file), a listed WAV whose digest
    differs is refused before anything is written.
    """
    audio_path = Path(audio_path)
    info = read_wav_info(audio_path)
    digest = _sha256(audio_path)
    verified_by = None
    if checksums is not None:
        want = read_checksums(checksums).get(audio_path.name)
        if want is not None:
            if want != digest:
                raise AmiError(f"{audio_path}: sha256 {digest} does not match {checksums} ({want})")
            verified_by = Path(checksums).name
    audio_meta = {
        "file": audio_path.name,
        "sha256": digest,
        "sha256_verified_by": verified_by,
        "frames": info.frames,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
    }
    built = build_meeting(Path(annotations), meeting_id, info, policy=policy, audio_meta=audio_meta)
    out_dir = Path(out_root) / meeting_id
    _prepare_dir(out_dir)
    placed = _place_audio(audio_path, out_dir / "mix.wav", link)
    built.meeting.audio["mix_source"]["placed_as"] = placed
    write_meeting(built.meeting, out_dir)
    write_reference_files(built.meeting, out_dir)
    check_meeting_dir(out_dir)
    return ConversionResult(out_dir, load_meeting(out_dir), built.counts, placed)


# --------------------------------------------------------------------------- #
# Checks and statistics
# --------------------------------------------------------------------------- #


def check_meeting_dir(meeting_dir: Path) -> Meeting:
    """evals' loaders accept the directory, ref.rttm/ref.stm agree with
    meeting.json, and mix.wav matches its header facts and is read-only."""
    meeting_dir = Path(meeting_dir)
    m = load_meeting(meeting_dir)
    rttm = load_rttm(meeting_dir / "ref.rttm", meeting_id=m.meeting_id)
    stm = load_stm(meeting_dir / "ref.stm", meeting_id=m.meeting_id)
    if len(rttm) != len(m.segments) or len(stm) != len(m.segments):
        raise ContractError(
            f"{meeting_dir}: {len(m.segments)} segments but {len(rttm)} RTTM and {len(stm)} STM lines"
        )
    for i, (s, r, t) in enumerate(zip(m.segments, rttm, stm)):
        if (s.speaker, s.text) != (t.speaker, t.text) or r.speaker != s.speaker:
            raise ContractError(f"{meeting_dir}: segment {i} differs between meeting.json, ref.rttm and ref.stm")
        if max(abs(s.start - r.start), abs(s.end - r.end), abs(s.start - t.start), abs(s.end - t.end)) > 1e-6:
            raise ContractError(f"{meeting_dir}: segment {i} times differ between meeting.json and ref files")
    mix = meeting_dir / str(m.audio.get("mix_wav", "mix.wav"))
    info = read_wav_info(mix)
    if abs(info.duration_s - m.duration_s) > 1e-9 or info.sample_rate != m.sample_rate:
        raise ContractError(f"{meeting_dir}: mix.wav ({info}) does not match meeting.json duration/rate")
    if stat.S_IMODE(os.stat(mix).st_mode) & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ContractError(f"{mix}: must be read-only (it can share its inode with the raw corpus WAV)")
    return m


def verify_mix_audio(meeting_dir: Path) -> str:
    """Re-hash mix.wav against the sha256 meeting.json recorded at conversion.

    Catches a write through any hard link to the corpus WAV.  Returns the digest.
    """
    meeting_dir = Path(meeting_dir)
    m = load_meeting(meeting_dir)
    want = (m.audio.get("mix_source") or {}).get("sha256")
    if not want:
        raise ContractError(f"{meeting_dir}: meeting.json records no mix_source.sha256")
    got = _sha256(meeting_dir / str(m.audio.get("mix_wav", "mix.wav")))
    if got != want:
        raise ContractError(f"{meeting_dir}: mix.wav sha256 {got} differs from the converted {want}")
    return got


def meeting_stats(meeting: Meeting) -> dict[str, Any]:
    """Speech, overlap and word statistics of a reference.

    ``overlap_fraction`` = time with >= 2 speakers active / time with >= 1
    active, on the per-speaker union of segments (the generator's
    "overlapped / union of speech" definition, docs/generator.md section 1).
    """
    per_speaker = {
        spk: _union((s.start, s.end) for s in meeting.segments if s.speaker == spk) for spk in meeting.speaker_ids
    }
    events: list[tuple[float, int]] = []
    for ivs in per_speaker.values():
        for a, b in ivs:
            events += [(a, 1), (b, -1)]
    events.sort(key=lambda e: (e[0], e[1]))
    active, last, speech, overlap = 0, 0.0, 0.0, 0.0
    for t, d in events:
        if active >= 1:
            speech += t - last
        if active >= 2:
            overlap += t - last
        active += d
        last = t
    per_speaker_time = {spk: sum(b - a for a, b in ivs) for spk, ivs in per_speaker.items()}
    return {
        "meeting_id": meeting.meeting_id,
        "duration_s": meeting.duration_s,
        "speakers_declared": len(meeting.speaker_ids),
        "speakers_active": len(meeting.active_speakers),
        "segments": len(meeting.segments),
        "segments_without_text": sum(1 for s in meeting.segments if not s.text),
        "words": sum(len(s.words) for s in meeting.segments),
        "words_per_speaker": {
            spk: sum(len(s.words) for s in meeting.segments if s.speaker == spk) for spk in meeting.speaker_ids
        },
        "speaker_time_s": sum(per_speaker_time.values()),
        "speaker_time_per_speaker_s": per_speaker_time,
        "speech_union_s": speech,
        "speech_fraction_of_duration": speech / meeting.duration_s,
        "overlap_s": overlap,
        "overlap_fraction": (overlap / speech) if speech > 0 else None,
    }


def rttm_stats(meeting_dir: Path) -> dict[str, Any]:
    m = load_meeting(meeting_dir)
    segs = load_rttm(Path(meeting_dir) / "ref.rttm", meeting_id=m.meeting_id)
    return {
        "lines": len(segs),
        "speakers": len({s.speaker for s in segs}),
        "total_duration_s": sum(s.end - s.start for s in segs),
    }


def self_score(meeting_dir: Path) -> dict[str, Any]:
    """Reference scored against itself: from meeting.json, and from ref.rttm + ref.stm."""
    from evals.io import Hypothesis
    from evals.metrics import score_meeting

    meeting_dir = Path(meeting_dir)
    m = load_meeting(meeting_dir)
    out: dict[str, Any] = {}
    rttm = load_rttm(meeting_dir / "ref.rttm", meeting_id=m.meeting_id)
    stm = load_stm(meeting_dir / "ref.stm", meeting_id=m.meeting_id)
    from_files = [Segment(t.speaker, r.start, r.end, t.text) for r, t in zip(rttm, stm)]
    for label, segs in (("meeting_json", list(m.segments)), ("ref_rttm_stm", from_files)):
        rep = score_meeting(m, Hypothesis(m.meeting_id, f"self:{label}", segs))
        out[label] = {
            "der": rep.der.der,
            "jer": rep.jer.jer,
            "cpwer": rep.cpwer.error_rate,
            "tcpwer": rep.tcpwer.error_rate,
            "speaker_count_error": rep.speaker_count.error,
        }
    return out


# --------------------------------------------------------------------------- #
# Cross-check against a published RTTM reference (the BUT setup)
# --------------------------------------------------------------------------- #

Turn = tuple[str, int, int]  # (speaker, start ms, end ms)


def _ms(x: Decimal) -> int:
    return int(x.quantize(Decimal("0.001")) * 1000)


def rttm_turns(text: str, *, source: str = "<rttm>") -> list[Turn]:
    """``SPEAKER`` lines -> sorted (speaker, start ms, end ms), summed in decimal.

    Millisecond resolution is exact here: every AMI word time has at most 4
    decimals and the BUT RTTMs print at most 3.
    """
    out: list[Turn] = []
    for n, line in enumerate(text.splitlines(), 1):
        f = line.split()
        if not f:
            continue
        if len(f) < 8 or f[0] != "SPEAKER":
            raise AmiError(f"{source}:{n}: not an RTTM SPEAKER line: {line!r}")
        start = Decimal(f[3])
        out.append((f[7], _ms(start), _ms(start + Decimal(f[4]))))
    return sorted(out)


def meeting_turns(meeting: Meeting) -> list[Turn]:
    return sorted((s.speaker, _ms(Decimal(repr(s.start))), _ms(Decimal(repr(s.end)))) for s in meeting.segments)


@dataclass
class TurnDiff:
    meeting_id: str
    ours: int
    theirs: int
    only_ours: list[Turn] = field(default_factory=list)
    only_theirs: list[Turn] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not self.only_ours and not self.only_theirs


def compare_turns(meeting_id: str, ours: Sequence[Turn], theirs: Sequence[Turn]) -> TurnDiff:
    a, b = Counter(ours), Counter(theirs)
    return TurnDiff(
        meeting_id, len(ours), len(theirs), sorted((a - b).elements()), sorted((b - a).elements())
    )


def crosscheck_rttms(annotations: Path, rttm_root: Path) -> list[TurnDiff]:
    """Build every meeting that has ``<ID>.rttm`` anywhere under ``rttm_root``
    (stand-in duration, no audio needed) and compare its turns with that file."""
    files = sorted(Path(rttm_root).rglob("*.rttm"), key=lambda p: p.stem)
    names = [p.stem for p in files]
    if len(set(names)) != len(names):
        raise AmiError(f"{rttm_root}: a meeting id appears in two RTTM files")
    out = []
    for path in files:
        built = build_meeting(annotations, path.stem, STAND_IN_AUDIO)
        theirs = rttm_turns(path.read_text(encoding="utf-8"), source=str(path))
        out.append(compare_turns(path.stem, meeting_turns(built.meeting), theirs))
    return out


def release_digest(annotations: Path, *, validate: bool = False) -> dict[str, Any]:
    """sha256 of every meeting's RTTM lines in meetings.xml order (stand-in duration).

    A regression pin for the whole release that needs only the annotations.
    ``validate`` also runs each meeting through ``evals.io.meeting_from_dict``.
    """
    from evals.io import meeting_from_dict

    h = hashlib.sha256()
    segments = words = 0
    ids = list_meetings(Path(annotations) / "corpusResources" / "meetings.xml")
    for mid in ids:
        m = build_meeting(annotations, mid, STAND_IN_AUDIO).meeting
        if validate:
            meeting_from_dict(m.to_dict(), where=mid)
        h.update(("\n".join(rttm_lines(m.segments, mid)) + "\n").encode())
        segments += len(m.segments)
        words += sum(len(s.words) for s in m.segments)
    return {"meetings": len(ids), "segments": segments, "words": words, "rttm_sha256": h.hexdigest()}


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def _wanted(name: str) -> bool:
    return any(name == m or (m.endswith("/") and name.startswith(m)) for m in ANNOTATION_MEMBERS)


def extract_annotations(zip_path: Path, dest: Path) -> list[Path]:
    """Extract the members the converter reads (ANNOTATION_MEMBERS) into ``dest``.

    Member names are checked: an absolute path or a ``..`` component is
    refused (AmiError) before anything is written.  A provenance manifest
    (zip name, sha256, member count) is written as ``EXTRACTED_FROM.json``.
    """
    zip_path, dest = Path(zip_path), Path(dest)
    with zipfile.ZipFile(zip_path) as z:
        members = [i for i in z.infolist() if not i.is_dir() and _wanted(i.filename)]
        for info in members:
            parts = Path(info.filename).parts
            if Path(info.filename).is_absolute() or ".." in parts or "\\" in info.filename:
                raise AmiError(f"{zip_path}: refusing unsafe member name {info.filename!r}")
        if not members:
            raise AmiError(f"{zip_path}: none of {list(ANNOTATION_MEMBERS)} found")
        written: list[Path] = []
        for info in members:
            target = dest / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(target)
    (dest / EXTRACT_MANIFEST).write_text(
        json.dumps(
            {"zip": zip_path.name, "sha256": _sha256(zip_path), "members": len(written)}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _audio_for(audio_dir: Path, meeting_id: str) -> Path:
    return Path(audio_dir) / f"{meeting_id}{MIX_HEADSET_SUFFIX}"


def _cmd_extract(args: argparse.Namespace) -> int:
    written = extract_annotations(args.zip, args.out)
    print(f"extracted {len(written)} files from {args.zip} into {args.out}")
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    ids = args.meeting or sorted(
        p.name[: -len(MIX_HEADSET_SUFFIX)] for p in Path(args.audio_dir).glob(f"*{MIX_HEADSET_SUFFIX}")
    )
    if not ids:
        print(f"no *{MIX_HEADSET_SUFFIX} under {args.audio_dir} and no --meeting given", file=sys.stderr)
        return 2
    if args.checksums is not None and not Path(args.checksums).is_file():
        raise AmiError(f"--checksums {args.checksums}: file not found")
    checksums = args.checksums if args.checksums is not None else (
        DEFAULT_CHECKSUMS if DEFAULT_CHECKSUMS.is_file() else None
    )
    policy = Policy(truncated=args.truncated)
    for mid in ids:
        res = convert_meeting(
            args.annotations, mid, _audio_for(args.audio_dir, mid), args.out,
            policy=policy, link=args.link, checksums=checksums,
        )
        st = meeting_stats(res.meeting)
        verified = res.meeting.audio["mix_source"]["sha256_verified_by"]
        print(
            f"{mid}: {st['duration_s']:.3f} s, {st['speakers_active']}/{st['speakers_declared']} speakers, "
            f"{st['segments']} segments, {st['words']} words, overlap {st['overlap_fraction']:.4f}, "
            f"mix.wav {res.audio_placed_as} (read-only), sha256 "
            f"{'verified by ' + verified if verified else 'not listed in a checksum file'} -> {res.meeting_dir}"
        )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    root = Path(args.meetings)
    dirs = sorted(p.parent for p in root.glob("*/meeting.json"))
    if args.meeting:
        dirs = [d for d in dirs if d.name in set(args.meeting)]
    if not dirs:
        print(f"no meeting.json under {root}", file=sys.stderr)
        return 3
    rows = []
    for d in dirs:
        m = check_meeting_dir(d)
        row = {**meeting_stats(m), "rttm": rttm_stats(d), "counts": m.generator.get("scenario", {}).get("counts", {})}
        row["mix_sha256"] = verify_mix_audio(d)
        if not args.no_score:
            row["self_score"] = self_score(d)
        rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print("meeting  duration_s  spk(act/decl)  segments(no text)  words  speaker_time_s  speech_union_s"
              "  overlap_frac  rttm(lines/spk/total_s)  self DER / cpWER  mix.wav sha256")
        for r in rows:
            ss = r.get("self_score", {}).get("meeting_json", {})
            print(
                f"{r['meeting_id']}  {r['duration_s']:.4f}  {r['speakers_active']}/{r['speakers_declared']}  "
                f"{r['segments']}({r['segments_without_text']})  {r['words']}  {r['speaker_time_s']:.3f}  "
                f"{r['speech_union_s']:.3f}  {r['overlap_fraction']:.6f}  {r['rttm']['lines']}/"
                f"{r['rttm']['speakers']}/{r['rttm']['total_duration_s']:.3f}  {ss.get('der')} / {ss.get('cpwer')}"
                f"  {r['mix_sha256'][:12]}… ok"
            )
    return 0


def _fmt_turn(t: Turn) -> str:
    return f"{t[0]} {t[1] / 1000:.3f}-{t[2] / 1000:.3f}"


def _cmd_crosscheck(args: argparse.Namespace) -> int:
    if not Path(args.rttms).is_dir():
        print(f"no RTTM directory {args.rttms} (docs/evals.md 'AMI' says how to fetch the BUT setup)", file=sys.stderr)
        return 3
    diffs = crosscheck_rttms(args.annotations, args.rttms)
    if not diffs:
        print(f"no *.rttm under {args.rttms}", file=sys.stderr)
        return 3
    bad = [d for d in diffs if not d.identical]
    for d in bad:
        print(f"{d.meeting_id}: ours {d.ours} turns, theirs {d.theirs}")
        for t in d.only_ours:
            print(f"  only ours:   {_fmt_turn(t)}")
        for t in d.only_theirs:
            print(f"  only theirs: {_fmt_turn(t)}")
    print(f"meetings compared: {len(diffs)}, identical: {len(diffs) - len(bad)}, differing: {len(bad)}")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals.ami", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="extract the annotation members the converter needs from the zip")
    e.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    e.add_argument("--out", type=Path, default=DEFAULT_ANNOTATIONS)
    e.set_defaults(func=_cmd_extract)
    c = sub.add_parser("convert", help="write contract meeting directories")
    c.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    c.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    c.add_argument("--out", type=Path, default=DEFAULT_OUT)
    c.add_argument("--meeting", action="append", help="meeting id (repeatable); default: every <ID>.Mix-Headset.wav")
    c.add_argument("--truncated", choices=("keep", "drop"), default="keep")
    c.add_argument("--link", choices=("hardlink", "copy"), default="hardlink")
    c.add_argument("--checksums", type=Path, default=None,
                   help=f"sha256sum-style file; default {DEFAULT_CHECKSUMS.relative_to(REPO_ROOT)} when present")
    c.set_defaults(func=_cmd_convert)
    s = sub.add_parser("stats", help="check converted meetings and print duration/speakers/words/overlap/self-score")
    s.add_argument("--meetings", type=Path, default=DEFAULT_OUT)
    s.add_argument("--meeting", action="append")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-score", action="store_true", help="skip the DER/cpWER self-score")
    s.set_defaults(func=_cmd_stats)
    x = sub.add_parser("crosscheck", help="compare the speech turns with published RTTMs (exit 1 if any differ)")
    x.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    x.add_argument("--rttms", type=Path, default=DEFAULT_BUT_RTTMS)
    x.set_defaults(func=_cmd_crosscheck)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Exit codes (the evals CLI's): 0 done, 2 input error or any unexpected
    error (traceback on stderr), 3 nothing found (``stats``, ``crosscheck``);
    ``crosscheck`` exits 1 when a meeting's turns differ."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (AmiError, ContractError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception:  # a crash must not look like success or like a skip
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
