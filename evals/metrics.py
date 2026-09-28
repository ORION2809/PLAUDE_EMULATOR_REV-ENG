"""Metrics for Layer 3: DER/JER (pyannote.metrics), WER (jiwer), cpWER/tcpWER (meeteval).

Every number reported here is produced by one of those three libraries, or
is a decomposition of one of their numbers that this module derives and the
tests prove sums back to the library's figure.  What the harness *chooses*
(collars, UEM, which words feed which metric) is labelled HARNESS_POLICY.

Collar conventions (see docs/evals.md):

* ``der_collar`` follows pyannote.metrics: it is the TOTAL width of the
  no-score zone centred on every reference boundary.  ``0.25`` therefore
  ignores ±0.125 s around each boundary.  NIST md-eval's ``-c 0.25`` (±0.25 s)
  is ``der_collar=0.5`` here.
* ``tcp_collar`` follows meeteval: a hypothesis word may only be matched to a
  reference word whose interval overlaps the hypothesis word's interval
  extended by ``tcp_collar`` seconds on each side.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Hashable, Sequence

from pyannote.core import Annotation, Segment as PSegment, Timeline
from pyannote.core.utils.generators import int_generator, string_generator
from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate
from pyannote.metrics.identification import IdentificationErrorRate

from evals.io import (
    DEFAULT_NORMALIZER,
    Hypothesis,
    Meeting,
    Normalizer,
    Segment,
    normalized_words,
    sorted_segments,
)

#: HARNESS_POLICY: default DER/JER collar (pyannote semantics: total width,
#: i.e. ±0.125 s around each reference boundary).
DEFAULT_DER_COLLAR = 0.25
#: HARNESS_POLICY: default tcpWER collar, 5 s -- the value meeteval's
#: preprocess warning recommends ("You may want to set the collar to 5
#: seconds", meeteval/wer/preprocess.py:398).  meeteval itself has no default:
#: its CLI declares ``--collar`` required (meeteval/wer/__main__.py:612-616)
#: and ``tcp_word_error_rate`` takes ``collar`` as a required keyword
#: (meeteval/wer/wer/time_constrained.py:665-669).
DEFAULT_TCP_COLLAR = 5.0

# meeteval logs a warning when tcpWER runs with collar 0; that is a deliberate
# configuration here (tests probe it), so keep the log quiet.
logging.getLogger("preprocess").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #


def _check_collar(name: str, value: Any) -> None:
    """Collars must be finite and >= 0.

    pyannote extrudes nothing for a collar <= 0 (``if collar > 0.``,
    pyannote/metrics/utils.py:74) while the report would record the negative
    value; meeteval's CLI refuses x < 0 (meeteval/wer/__main__.py:544-553).
    """
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")


def to_annotation(segments: Sequence[Segment], uri: str) -> Annotation:
    """Contract segments -> pyannote Annotation for DER/JER.

    HARNESS_POLICY: one track per *speaker activity interval*.  Segments of the
    same speaker that strictly overlap are merged first, so a speaker is never
    counted as talking twice at once (pyannote counts tracks, not speakers;
    two overlapping ``s`` segments would otherwise be scored as false alarm,
    or as double reference time).  Touching segments (end == next start) are
    kept apart, so a reference turn boundary keeps its collar.  Overlap between
    *different* speakers is untouched: two speakers talking for 2 s is 4 s.
    JER already used the per-speaker union (``label_timeline``), so DER and JER
    now read the same speaker activity.
    """
    by_speaker: dict[str, list[list[float]]] = {}
    for s in sorted_segments(segments):
        intervals = by_speaker.setdefault(s.speaker, [])
        if intervals and s.start < intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], s.end)
        else:
            intervals.append([s.start, s.end])
    ann = Annotation(uri=uri)
    track = 0
    for speaker, intervals in by_speaker.items():
        for start, end in intervals:
            ann[PSegment(start, end), track] = speaker
            track += 1
    return ann


def to_uem(duration_s: float | None, uri: str, *annotations: Annotation) -> Timeline:
    """Scoring region.

    HARNESS_POLICY: when the meeting duration is known the UEM is
    ``[0, duration_s]`` — hypothesis speech outside the audio is not scored.
    Without a duration, the union of reference and hypothesis extents is used:
    what pyannote would fall back to *with a warning*
    (pyannote/metrics/utils.py:200-202), made explicit here.
    """
    if duration_s is not None:
        return Timeline([PSegment(0.0, float(duration_s))], uri=uri)
    extent: PSegment | None = None
    for ann in annotations:
        tl = ann.get_timeline(copy=False)
        if len(tl) == 0:
            continue
        extent = tl.extent() if extent is None else (extent | tl.extent())
    return Timeline([extent] if extent else [], uri=uri)


def to_seglst(
    segments: Sequence[Segment],
    session_id: str,
    normalizer: Normalizer,
    *,
    word_level: bool,
) -> tuple[Any, bool]:
    """Contract segments -> meeteval SegLST.

    Returns ``(seglst, is_word_level)``.  With ``word_level`` and word timings
    on *every* non-empty segment, one SegLST entry per normalised word is
    emitted with its own timing; otherwise one entry per segment with the
    normalised text.  Segments whose normalised text is empty are dropped
    (they carry no scorable words).  HARNESS_POLICY.
    """
    from meeteval.io.seglst import SegLST

    ordered = sorted_segments(segments)
    can_word_level = word_level and all(s.words for s in ordered if normalizer(s.text))
    entries: list[dict[str, Any]] = []
    if can_word_level:
        for s in ordered:
            for w in normalized_words(s, normalizer):
                entries.append(
                    {
                        "session_id": session_id,
                        "speaker": s.speaker,
                        "start_time": w.start,
                        "end_time": w.end,
                        "words": w.w,
                    }
                )
        return SegLST(entries), True
    for s in ordered:
        text = normalizer(s.text)
        if not text:
            continue
        entries.append(
            {
                "session_id": session_id,
                "speaker": s.speaker,
                "start_time": s.start,
                "end_time": s.end,
                "words": text,
            }
        )
    return SegLST(entries), False


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #


def _rate(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


@dataclass
class SpeakerBreakdown:
    """Per-speaker share of the DER components (seconds).

    Keys are reference speakers (after the optimal hypothesis->reference
    mapping) or ``hyp:<label>`` for hypothesis speakers left unmapped.
    ``false_alarm`` on a reference speaker is the false alarm of the
    hypothesis speaker mapped onto it.  Sums over all keys equal the global
    components exactly (tested), including in overlap regions, where a
    sub-segment's miss/confusion is shared equally among its unmatched
    reference speakers and its false alarm among its unmatched hypothesis
    speakers (HARNESS_POLICY — the libraries do not define this split).
    """

    total: float = 0.0
    correct: float = 0.0
    miss: float = 0.0
    confusion: float = 0.0
    false_alarm: float = 0.0
    mapped_to: str | None = None

    @property
    def error_rate(self) -> float | None:
        return _rate(self.miss + self.confusion + self.false_alarm, self.total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "miss": self.miss,
            "confusion": self.confusion,
            "false_alarm": self.false_alarm,
            "error_rate": self.error_rate,
            "mapped_to": self.mapped_to,
        }


@dataclass
class OverlapResult:
    """DER components restricted to the reference's overlap regions.

    Scored under the meeting's *global* optimal mapping (the one DER and the
    per-speaker breakdown use) with the same collar, so every second counted
    here is counted identically in the global components.
    """

    overlap_time: float
    total: float
    miss: float
    false_alarm: float
    confusion: float
    correct: float

    @property
    def der(self) -> float | None:
        return _rate(self.miss + self.false_alarm + self.confusion, self.total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlap_time": self.overlap_time,
            "total": self.total,
            "miss": self.miss,
            "false_alarm": self.false_alarm,
            "confusion": self.confusion,
            "correct": self.correct,
            "der": self.der,
        }


@dataclass
class DiarizationResult:
    """pyannote.metrics DiarizationErrorRate output plus derived breakdowns."""

    der: float | None
    total: float
    correct: float
    miss: float
    false_alarm: float
    confusion: float
    collar: float
    skip_overlap: bool
    mapping: dict[str, str]
    per_speaker: dict[str, SpeakerBreakdown]
    overlap: OverlapResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "der": self.der,
            "total": self.total,
            "correct": self.correct,
            "miss": self.miss,
            "false_alarm": self.false_alarm,
            "confusion": self.confusion,
            "collar": self.collar,
            "skip_overlap": self.skip_overlap,
            "mapping": dict(self.mapping),
            "per_speaker": {k: v.to_dict() for k, v in self.per_speaker.items()},
            "overlap": self.overlap.to_dict() if self.overlap else None,
        }


@dataclass
class JerResult:
    jer: float | None
    speaker_count: int
    speaker_error: float
    per_speaker: dict[str, float]
    collar: float
    skip_overlap: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "jer": self.jer,
            "speaker_count": self.speaker_count,
            "speaker_error": self.speaker_error,
            "per_speaker": dict(self.per_speaker),
            "collar": self.collar,
            "skip_overlap": self.skip_overlap,
        }


@dataclass
class WerResult:
    """jiwer word error rate with counts.  ``mode`` says how words were paired."""

    mode: str
    wer: float | None
    hits: int
    substitutions: int
    deletions: int
    insertions: int
    reference_words: int
    hypothesis_words: int
    per_speaker: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "wer": self.wer,
            "errors": self.errors,
            "hits": self.hits,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "reference_words": self.reference_words,
            "hypothesis_words": self.hypothesis_words,
            "per_speaker": self.per_speaker,
        }


@dataclass
class CpWerResult:
    """meeteval CPErrorRate (cpWER when ``collar`` is None, tcpWER otherwise).

    ``refused`` is set, and every count is ``None``, when meeteval would not
    score the meeting (:data:`MEETEVAL_SPEAKER_LIMIT`); the meeting's other
    metrics are still reported (HARNESS_POLICY).
    """

    error_rate: float | None
    errors: int | None
    length: int | None
    substitutions: int | None
    deletions: int | None
    insertions: int | None
    missed_speaker: int | None
    falarm_speaker: int | None
    scored_speaker: int | None
    assignment: list[tuple[str | None, str | None]]
    collar: float | None
    word_level_timing: bool | None
    refused: str | None = None

    @classmethod
    def not_scored(cls, reason: str, collar: float | None, word_level: bool | None) -> "CpWerResult":
        return cls(None, None, None, None, None, None, None, None, None, [], collar, word_level, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_rate": self.error_rate,
            "errors": self.errors,
            "length": self.length,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "missed_speaker": self.missed_speaker,
            "falarm_speaker": self.falarm_speaker,
            "scored_speaker": self.scored_speaker,
            "assignment": [list(pair) for pair in self.assignment],
            "collar": self.collar,
            "word_level_timing": self.word_level_timing,
            "refused": self.refused,
        }


@dataclass
class SpeakerCountResult:
    reference_declared: int
    reference_active: int
    hypothesis: int
    error: int
    abs_error: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_declared": self.reference_declared,
            "reference_active": self.reference_active,
            "hypothesis": self.hypothesis,
            "error": self.error,
            "abs_error": self.abs_error,
        }


@dataclass
class MeetingReport:
    """Everything the harness knows about one hypothesis for one meeting."""

    meeting_id: str
    system: str
    der: DiarizationResult
    jer: JerResult
    wer_literal: WerResult
    wer_concat: WerResult
    cpwer: CpWerResult
    tcpwer: CpWerResult
    speaker_count: SpeakerCountResult
    settings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "system": self.system,
            "der": self.der.to_dict(),
            "jer": self.jer.to_dict(),
            "wer_literal": self.wer_literal.to_dict(),
            "wer_concat": self.wer_concat.to_dict(),
            "cpwer": self.cpwer.to_dict(),
            "tcpwer": self.tcpwer.to_dict(),
            "speaker_count": self.speaker_count.to_dict(),
            "settings": self.settings,
        }

    def flat(self) -> dict[str, float | int | None]:
        """Dotted metric paths, the vocabulary gates and aggregation use."""
        d = self.der
        ov = d.overlap
        return {
            "der.der": d.der,
            "der.total": d.total,
            "der.miss": d.miss,
            "der.false_alarm": d.false_alarm,
            "der.confusion": d.confusion,
            "der.correct": d.correct,
            "der.miss_rate": _rate(d.miss, d.total),
            "der.false_alarm_rate": _rate(d.false_alarm, d.total),
            "der.confusion_rate": _rate(d.confusion, d.total),
            "der.overlap.der": ov.der if ov else None,
            "der.overlap.total": ov.total if ov else 0.0,
            "der.overlap.overlap_time": ov.overlap_time if ov else 0.0,
            "jer.jer": self.jer.jer,
            "jer.speaker_count": self.jer.speaker_count,
            "jer.speaker_error": self.jer.speaker_error,
            "wer_literal.wer": self.wer_literal.wer,
            "wer_literal.errors": self.wer_literal.errors,
            "wer_literal.reference_words": self.wer_literal.reference_words,
            "wer_concat.wer": self.wer_concat.wer,
            "wer_concat.errors": self.wer_concat.errors,
            "wer_concat.reference_words": self.wer_concat.reference_words,
            "cpwer.error_rate": self.cpwer.error_rate,
            "cpwer.errors": self.cpwer.errors,
            "cpwer.length": self.cpwer.length,
            "cpwer.missed_speaker": self.cpwer.missed_speaker,
            "cpwer.falarm_speaker": self.cpwer.falarm_speaker,
            "tcpwer.error_rate": self.tcpwer.error_rate,
            "tcpwer.errors": self.tcpwer.errors,
            "tcpwer.length": self.tcpwer.length,
            "speaker_count.error": self.speaker_count.error,
            "speaker_count.abs_error": self.speaker_count.abs_error,
            "speaker_count.reference_active": self.speaker_count.reference_active,
            "speaker_count.hypothesis": self.speaker_count.hypothesis,
        }

    #: The metrics a human wants to see first (also the markdown table order).
    HEADLINE: tuple[tuple[str, str], ...] = (
        ("cpWER", "cpwer.error_rate"),
        ("tcpWER", "tcpwer.error_rate"),
        ("DER", "der.der"),
        ("JER", "jer.jer"),
        ("WER (label-literal)", "wer_literal.wer"),
        ("WER (concatenated)", "wer_concat.wer"),
        ("overlap DER", "der.overlap.der"),
        ("speaker-count error", "speaker_count.error"),
    )

    def to_markdown(self) -> str:
        flat = self.flat()
        lines = [
            f"# {self.meeting_id} — {self.system}",
            "",
            "| metric | value |",
            "|---|---|",
        ]
        for label, key in self.HEADLINE:
            lines.append(f"| {label} | {format_value(flat[key])} |")
        d = self.der
        lines += [
            "",
            f"DER components (s): miss {d.miss:.3f}, false alarm {d.false_alarm:.3f}, "
            f"confusion {d.confusion:.3f}, correct {d.correct:.3f}, total {d.total:.3f} "
            f"(collar {d.collar}, skip_overlap {d.skip_overlap})",
            "",
            "| speaker | total s | correct s | miss s | confusion s | false alarm s | error rate | mapped hyp |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for spk, b in d.per_speaker.items():
            lines.append(
                f"| {spk} | {b.total:.3f} | {b.correct:.3f} | {b.miss:.3f} | {b.confusion:.3f} | "
                f"{b.false_alarm:.3f} | {format_value(b.error_rate)} | {b.mapped_to or '-'} |"
            )
        c, t = self.cpwer, self.tcpwer
        cp_line = (
            f"cpWER: not scored ({c.refused})" if c.refused else
            f"cpWER: {c.errors}/{c.length} (S {c.substitutions}, D {c.deletions}, I {c.insertions}; "
            f"missed speakers {c.missed_speaker}, false-alarm speakers {c.falarm_speaker}); "
            f"assignment {c.assignment}"
        )
        tcp_line = (
            f"tcpWER: not scored ({t.refused})" if t.refused else
            f"tcpWER (collar {t.collar} s, word-level timing {t.word_level_timing}): "
            f"{t.errors}/{t.length} (S {t.substitutions}, D {t.deletions}, I {t.insertions})"
        )
        lines += [
            "",
            cp_line,
            tcp_line,
            f"WER label-literal: {self.wer_literal.errors}/{self.wer_literal.reference_words} "
            f"(S {self.wer_literal.substitutions}, D {self.wer_literal.deletions}, I {self.wer_literal.insertions})",
            "",
        ]
        return "\n".join(lines)


def format_value(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


# --------------------------------------------------------------------------- #
# Diarization metrics
# --------------------------------------------------------------------------- #


def _overlap_regions(reference: Annotation) -> Timeline:
    """Regions where two reference tracks overlap (mirrors pyannote's extrude)."""
    regions: list[PSegment] = []
    for (s1, t1), (s2, t2) in reference.co_iter(reference):
        if s1 == s2 and t1 == t2:
            continue
        regions.append(s1 & s2)
    return Timeline(regions).support()


def _pyannote_mapping(metric: Any, R: Annotation, H: Annotation, *, ref_to_hyp: bool = False) -> dict[Hashable, Hashable]:
    """The Hungarian mapping exactly as pyannote computes it inside
    ``compute_components``: the uemified reference renamed 'A', 'B', ... and
    the hypothesis 0, 1, ... in ``labels()`` order, then mapped
    (pyannote/metrics/diarization.py:161-173 for DER, :409-416 for JER).
    Renaming changes the order of the co-occurrence matrix once there are more
    than 26 reference or 10 hypothesis labels, which decides ties; mirroring it
    makes the reported mapping the one pyannote scored with.

    Returns ``{hyp: ref}`` (DER) or, with ``ref_to_hyp``, ``{ref: hyp}`` (JER),
    in the original labels.
    """
    r_new = dict(zip(R.labels(), string_generator()))
    h_new = dict(zip(H.labels(), int_generator()))
    r_old = {v: k for k, v in r_new.items()}
    h_old = {v: k for k, v in h_new.items()}
    Rr, Hr = R.rename_labels(mapping=r_new), H.rename_labels(mapping=h_new)
    if ref_to_hyp:
        return {r_old[r]: h_old[h] for r, h in metric.optimal_mapping(Hr, Rr).items()}
    return {h_old[h]: r_old[r] for h, r in metric.optimal_mapping(Rr, Hr).items()}


#: Namespaces that keep reference and hypothesis labels apart once the
#: hypothesis is renamed with the optimal mapping.  pyannote renames both sides
#: into disjoint alphabets for the same reason (pyannote/metrics/diarization.py:161-165);
#: without it an *unmapped* hypothesis label equal to a reference label (the
#: generator and energy_vad both emit spk<N>) would be scored as correct.
_REF_NS = "R:"
_HYP_NS = "H:"


def _namespaced(
    reference: Annotation, hypothesis: Annotation, mapping: dict[Hashable, Hashable]
) -> tuple[Annotation, Annotation]:
    """Reference labels -> ``R:<ref>``; a hypothesis label -> ``R:<its mapped ref>``
    when mapped, else ``H:<hyp>``.  Scoring these with pyannote's
    ``IdentificationErrorRate`` reproduces ``DiarizationErrorRate`` exactly."""
    ref_ns = reference.rename_labels(mapping={r: _REF_NS + str(r) for r in reference.labels()})
    hyp_ns = hypothesis.rename_labels(
        mapping={h: (_REF_NS + str(mapping[h])) if h in mapping else (_HYP_NS + str(h)) for h in hypothesis.labels()}
    )
    return ref_ns, hyp_ns


def _speaker_breakdown(
    metric: DiarizationErrorRate,
    reference: Annotation,
    hypothesis: Annotation,
    ref_ns: Annotation,
    hyp_ns: Annotation,
    uem: Timeline,
    mapping: dict[Hashable, Hashable],
) -> dict[str, SpeakerBreakdown]:
    """Exact per-speaker split of pyannote's DER components (see SpeakerBreakdown).

    Works on the namespaced annotations, so an unmapped hypothesis label can
    never match (or be charged as) a reference speaker of the same name.
    """
    R, H, common = metric.uemify(
        ref_ns,
        hyp_ns,
        uem=uem,
        collar=metric.collar,
        skip_overlap=metric.skip_overlap,
        returns_timeline=True,
    )
    inverse = {ref_label: hyp_label for hyp_label, ref_label in mapping.items()}
    buckets: dict[str, SpeakerBreakdown] = {}
    for label in reference.labels():
        buckets[_REF_NS + str(label)] = SpeakerBreakdown(mapped_to=str(inverse[label]) if label in inverse else None)
    for label in hypothesis.labels():
        if label not in mapping:
            buckets[_HYP_NS + str(label)] = SpeakerBreakdown(mapped_to=None)

    for seg in common:
        d = seg.duration
        r = Counter(R.get_labels(seg, unique=False))
        h = Counter(H.get_labels(seg, unique=False))
        n_ref, n_hyp = sum(r.values()), sum(h.values())
        matched = r & h  # multiset intersection; only R:<x> labels can match
        n_correct = sum(matched.values())
        miss_count = max(0, n_ref - n_hyp)
        fa_count = max(0, n_hyp - n_ref)
        conf_count = min(n_ref, n_hyp) - n_correct
        unmatched_ref = r - matched
        unmatched_hyp = h - matched
        u_r, u_h = sum(unmatched_ref.values()), sum(unmatched_hyp.values())
        for label, n in r.items():
            buckets[label].total += d * n
        for label, n in matched.items():
            buckets[label].correct += d * n
        for label, n in unmatched_ref.items():
            b = buckets[label]
            b.miss += d * n * miss_count / u_r
            b.confusion += d * n * conf_count / u_r
        for label, n in unmatched_hyp.items():
            # R:<x> = a mapped hypothesis speaker, charged to the reference speaker it maps to
            buckets.setdefault(label, SpeakerBreakdown()).false_alarm += d * n * fa_count / u_h

    # Display keys: the reference speaker's own name, or hyp:<label> for an
    # unmapped hypothesis speaker (made unique if a reference speaker is
    # literally called "hyp:<label>").
    out: dict[str, SpeakerBreakdown] = {}
    for key, b in buckets.items():
        if key.startswith(_REF_NS):
            out[key[len(_REF_NS):]] = b
    for key, b in buckets.items():
        if key.startswith(_HYP_NS):
            base = name = "hyp:" + key[len(_HYP_NS):]
            n = 2
            while name in out:
                name = f"{base}#{n}"
                n += 1
            out[name] = b
    return out


def diarization_error_rate(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    *,
    uri: str = "meeting",
    duration_s: float | None = None,
    collar: float = DEFAULT_DER_COLLAR,
    skip_overlap: bool = False,
    with_overlap: bool = True,
) -> DiarizationResult:
    """DER via ``pyannote.metrics.diarization.DiarizationErrorRate``.

    ``der = (miss + false_alarm + confusion) / total`` where ``total`` is the
    reference speech time inside the scoring region after collar extrusion
    (``None`` when that is 0: no scorable reference speech).
    """
    _check_collar("collar", collar)
    reference = to_annotation(ref_segments, uri)
    hypothesis = to_annotation(hyp_segments, uri)
    uem = to_uem(duration_s, uri, reference, hypothesis)
    metric = DiarizationErrorRate(collar=collar, skip_overlap=skip_overlap)
    detail = metric(reference, hypothesis, uem=uem, detailed=True)

    # The mapping pyannote used, on the uemified annotations (uemify may
    # change the optimal assignment), reproduced step for step.
    R, H = metric.uemify(reference, hypothesis, uem=uem, collar=collar, skip_overlap=skip_overlap)
    mapping = _pyannote_mapping(metric, R, H)
    ref_ns, hyp_ns = _namespaced(reference, hypothesis, mapping)
    per_speaker = _speaker_breakdown(metric, reference, hypothesis, ref_ns, hyp_ns, uem, mapping)

    overlap: OverlapResult | None = None
    if with_overlap and not skip_overlap:
        regions = _overlap_regions(reference)
        overlap_uem = regions.crop(uem, mode="intersection")
        if overlap_uem.duration() > 0:
            # Under the GLOBAL mapping: identification error of the namespaced
            # annotations over the overlap regions.  A fresh DiarizationErrorRate
            # here would re-optimise the mapping on the overlap alone and could
            # report 0 where the global score charges confusion.
            ov_metric = IdentificationErrorRate(collar=collar, skip_overlap=False)
            ov = ov_metric(ref_ns, hyp_ns, uem=overlap_uem, detailed=True)
            overlap = OverlapResult(
                overlap_time=float(overlap_uem.duration()),
                total=ov["total"],
                miss=ov["missed detection"],
                false_alarm=ov["false alarm"],
                confusion=ov["confusion"],
                correct=ov["correct"],
            )
        else:
            overlap = OverlapResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    total = detail["total"]
    return DiarizationResult(
        der=_rate(detail["missed detection"] + detail["false alarm"] + detail["confusion"], total),
        total=total,
        correct=detail["correct"],
        miss=detail["missed detection"],
        false_alarm=detail["false alarm"],
        confusion=detail["confusion"],
        collar=collar,
        skip_overlap=skip_overlap,
        mapping={str(k): str(v) for k, v in mapping.items()},
        per_speaker=per_speaker,
        overlap=overlap,
    )


def jaccard_error_rate(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    *,
    uri: str = "meeting",
    duration_s: float | None = None,
    collar: float = DEFAULT_DER_COLLAR,
    skip_overlap: bool = False,
) -> JerResult:
    """JER via ``pyannote.metrics.diarization.JaccardErrorRate``.

    Per reference speaker, ``1 - |R ∩ H| / |R ∪ H|`` against its optimally
    mapped hypothesis speaker (1.0 when unmapped); JER is the mean.  With no
    reference speaker left after the UEM, collar and overlap removal, JER is
    undefined: ``jer`` is ``None`` (pyannote itself would divide by zero,
    pyannote/metrics/diarization.py:459).
    """
    _check_collar("collar", collar)
    reference = to_annotation(ref_segments, uri)
    hypothesis = to_annotation(hyp_segments, uri)
    uem = to_uem(duration_s, uri, reference, hypothesis)
    metric = JaccardErrorRate(collar=collar, skip_overlap=skip_overlap)
    R, H = metric.uemify(reference, hypothesis, uem=uem, collar=collar, skip_overlap=skip_overlap)
    if not R.labels():
        return JerResult(jer=None, speaker_count=0, speaker_error=0.0, per_speaker={}, collar=collar, skip_overlap=skip_overlap)
    detail = metric(reference, hypothesis, uem=uem, detailed=True)

    # Per-speaker mirror of pyannote's compute_components loop, same mapping.
    mapping = _pyannote_mapping(metric, R, H, ref_to_hyp=True)  # ref label -> hyp label
    per_speaker: dict[str, float] = {}
    for ref_speaker in R.labels():
        hyp_speaker = mapping.get(ref_speaker)
        if hyp_speaker is None:
            per_speaker[str(ref_speaker)] = 1.0
            continue
        r = R.label_timeline(ref_speaker)
        h = H.label_timeline(hyp_speaker)
        union = r.union(h).support().duration()
        fa = h.duration() - h.crop(r).duration()
        miss = r.duration() - r.crop(h).duration()
        per_speaker[str(ref_speaker)] = (fa + miss) / union if union > 0 else 0.0

    count = int(detail["speaker count"])
    return JerResult(
        jer=_rate(detail["speaker error"], count),
        speaker_count=count,
        speaker_error=detail["speaker error"],
        per_speaker=per_speaker,
        collar=collar,
        skip_overlap=skip_overlap,
    )


# --------------------------------------------------------------------------- #
# ASR metrics
# --------------------------------------------------------------------------- #


def _jiwer_counts(ref_tokens: list[str], hyp_tokens: list[str]) -> dict[str, int]:
    """jiwer.process_words counts, with the empty cases jiwer refuses handled."""
    if not ref_tokens and not hyp_tokens:
        return {"hits": 0, "substitutions": 0, "deletions": 0, "insertions": 0}
    if not ref_tokens:
        return {"hits": 0, "substitutions": 0, "deletions": 0, "insertions": len(hyp_tokens)}
    if not hyp_tokens:
        return {"hits": 0, "substitutions": 0, "deletions": len(ref_tokens), "insertions": 0}
    import jiwer

    out = jiwer.process_words(" ".join(ref_tokens), " ".join(hyp_tokens))
    return {
        "hits": int(out.hits),
        "substitutions": int(out.substitutions),
        "deletions": int(out.deletions),
        "insertions": int(out.insertions),
    }


def word_error_rate(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    *,
    normalizer: Normalizer = DEFAULT_NORMALIZER,
    mode: str = "literal",
) -> WerResult:
    """Plain WER via ``jiwer.process_words`` (Levenshtein on words).

    ``mode="literal"``: words are paired by *speaker label identity* — the
    reference speaker ``spk0`` is scored against the hypothesis speaker named
    ``spk0``.  This is the metric the decision log calls "plain WER" and it
    lies when labels are swapped or arbitrary; it is reported so the lie is
    visible next to cpWER.

    ``mode="concat"``: speaker-agnostic — all reference words in segment start
    order against all hypothesis words in segment start order.  Overlapping
    segments interleave by start time (a known limitation of concatenation).
    """
    if mode not in ("literal", "concat"):
        raise ValueError(f"unknown WER mode {mode!r}")
    ref_sorted = sorted_segments(ref_segments)
    hyp_sorted = sorted_segments(hyp_segments)
    per_speaker: dict[str, dict[str, int]] = {}
    if mode == "concat":
        pairs = [("*", ref_sorted, hyp_sorted)]
    else:
        labels: list[str] = []
        for s in ref_sorted + hyp_sorted:
            if s.speaker not in labels:
                labels.append(s.speaker)
        pairs = [
            (lab, [s for s in ref_sorted if s.speaker == lab], [s for s in hyp_sorted if s.speaker == lab])
            for lab in labels
        ]
    totals = Counter()
    n_ref = n_hyp = 0
    for label, rs, hs in pairs:
        r_tok = [t for s in rs for t in normalizer.tokens(s.text)]
        h_tok = [t for s in hs for t in normalizer.tokens(s.text)]
        counts = _jiwer_counts(r_tok, h_tok)
        counts["reference_words"] = len(r_tok)
        counts["hypothesis_words"] = len(h_tok)
        per_speaker[label] = counts
        totals.update({k: counts[k] for k in ("hits", "substitutions", "deletions", "insertions")})
        n_ref += len(r_tok)
        n_hyp += len(h_tok)
    errors = totals["substitutions"] + totals["deletions"] + totals["insertions"]
    return WerResult(
        mode=mode,
        wer=_rate(errors, n_ref),
        hits=totals["hits"],
        substitutions=totals["substitutions"],
        deletions=totals["deletions"],
        insertions=totals["insertions"],
        reference_words=n_ref,
        hypothesis_words=n_hyp,
        per_speaker=per_speaker if mode == "literal" else {},
    )


def _cp_result(res: Any, collar: float | None, word_level: bool | None) -> CpWerResult:
    return CpWerResult(
        error_rate=None if res.length == 0 else float(res.error_rate),
        errors=int(res.errors),
        length=int(res.length),
        substitutions=int(res.substitutions),
        deletions=int(res.deletions),
        insertions=int(res.insertions),
        missed_speaker=int(res.missed_speaker),
        falarm_speaker=int(res.falarm_speaker),
        scored_speaker=int(res.scored_speaker),
        assignment=[(None if a is None else str(a), None if b is None else str(b)) for a, b in res.assignment],
        collar=collar,
        word_level_timing=word_level,
    )


#: meeteval 0.4.3 raises ``RuntimeError("Are you sure?...")`` from
#: ``_minimum_permutation_word_error_rate`` (used by cpWER and tcpWER) when
#: either side has more than this many speakers.  It is a sanity check, not a
#: computational limit (the assignment is a linear-sum problem).  The harness
#: keeps meeteval's refusal: the meeting's cpWER/tcpWER are recorded as not
#: scored, with the reason, and its DER/JER/WER are still reported.  A gate on
#: an aggregate fails closed when any meeting was not scored (evals/gates.py).
MEETEVAL_SPEAKER_LIMIT = 20


def _speaker_limit_reason(ref: Any, hyp: Any) -> str | None:
    n_ref = len({e["speaker"] for e in ref})
    n_hyp = len({e["speaker"] for e in hyp})
    if max(n_ref, n_hyp) <= MEETEVAL_SPEAKER_LIMIT:
        return None
    return (f"meeteval refuses more than {MEETEVAL_SPEAKER_LIMIT} speakers "
            f"(reference {n_ref}, hypothesis {n_hyp})")


def _refusal(exc: RuntimeError, ref: Any, hyp: Any) -> str:
    """The recorded reason for a meeteval refusal; anything else re-raises."""
    reason = _speaker_limit_reason(ref, hyp)
    if reason is None or not str(exc).startswith("Are you sure?"):
        raise exc
    return reason


def cp_wer(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    *,
    session_id: str = "meeting",
    normalizer: Normalizer = DEFAULT_NORMALIZER,
) -> CpWerResult:
    """cpWER via ``meeteval.wer.wer.cp.cp_word_error_rate``.

    Concatenated minimum-permutation WER: each speaker's words are
    concatenated, the reference->hypothesis speaker assignment that minimises
    the total error is found, and the pooled error count is divided by the
    reference word count.  Speaker labels are opaque — a swapped hypothesis
    scores identically (the decision-log rationale, asserted in tests).
    """
    from meeteval.wer.wer.cp import cp_word_error_rate

    ref, _ = to_seglst(ref_segments, session_id, normalizer, word_level=False)
    hyp, _ = to_seglst(hyp_segments, session_id, normalizer, word_level=False)
    try:
        return _cp_result(cp_word_error_rate(ref, hyp), None, None)
    except RuntimeError as exc:
        return CpWerResult.not_scored(_refusal(exc, ref, hyp), None, None)


def tcp_wer(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    *,
    session_id: str = "meeting",
    normalizer: Normalizer = DEFAULT_NORMALIZER,
    collar: float = DEFAULT_TCP_COLLAR,
) -> CpWerResult:
    """tcpWER via ``meeteval.wer.wer.time_constrained.tcp_word_error_rate``.

    Like cpWER, but a hypothesis word can only match a reference word whose
    interval overlaps the hypothesis word's interval widened by ``collar`` on
    each side.  HARNESS_POLICY: when a side carries word timings on every
    non-empty segment they are used as-is (``pseudo_word_level_timing='none'``);
    otherwise meeteval's defaults apply (reference ``character_based``,
    hypothesis ``character_based_points``).
    """
    from meeteval.wer.wer.time_constrained import tcp_word_error_rate

    _check_collar("collar", collar)
    ref, ref_wl = to_seglst(ref_segments, session_id, normalizer, word_level=True)
    hyp, hyp_wl = to_seglst(hyp_segments, session_id, normalizer, word_level=True)
    try:
        res = tcp_word_error_rate(
            ref,
            hyp,
            collar=collar,
            reference_pseudo_word_level_timing="none" if ref_wl else "character_based",
            hypothesis_pseudo_word_level_timing="none" if hyp_wl else "character_based_points",
        )
    except RuntimeError as exc:
        return CpWerResult.not_scored(_refusal(exc, ref, hyp), collar, ref_wl and hyp_wl)
    return _cp_result(res, collar, ref_wl and hyp_wl)


def speaker_count_error(meeting: Meeting, hyp: Hypothesis) -> SpeakerCountResult:
    """Hypothesis speaker count minus *active* reference speaker count."""
    active = len(meeting.active_speakers)
    n_hyp = len(hyp.speaker_ids)
    return SpeakerCountResult(
        reference_declared=len(meeting.speaker_ids),
        reference_active=active,
        hypothesis=n_hyp,
        error=n_hyp - active,
        abs_error=abs(n_hyp - active),
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def score_meeting(
    meeting: Meeting,
    hyp: Hypothesis,
    *,
    normalizer: Normalizer = DEFAULT_NORMALIZER,
    der_collar: float = DEFAULT_DER_COLLAR,
    skip_overlap: bool = False,
    tcp_collar: float = DEFAULT_TCP_COLLAR,
    check_meeting_id: bool = True,
) -> MeetingReport:
    """Score one hypothesis against one meeting with every metric."""
    _check_collar("der_collar", der_collar)
    _check_collar("tcp_collar", tcp_collar)
    if check_meeting_id and hyp.meeting_id != meeting.meeting_id:
        raise ValueError(
            f"hypothesis meeting_id {hyp.meeting_id!r} does not match reference {meeting.meeting_id!r}"
        )
    uri = meeting.meeting_id
    common = dict(uri=uri, duration_s=meeting.duration_s, collar=der_collar, skip_overlap=skip_overlap)
    return MeetingReport(
        meeting_id=meeting.meeting_id,
        system=hyp.system,
        der=diarization_error_rate(meeting.segments, hyp.segments, **common),
        jer=jaccard_error_rate(meeting.segments, hyp.segments, **common),
        wer_literal=word_error_rate(meeting.segments, hyp.segments, normalizer=normalizer, mode="literal"),
        wer_concat=word_error_rate(meeting.segments, hyp.segments, normalizer=normalizer, mode="concat"),
        cpwer=cp_wer(meeting.segments, hyp.segments, session_id=uri, normalizer=normalizer),
        tcpwer=tcp_wer(meeting.segments, hyp.segments, session_id=uri, normalizer=normalizer, collar=tcp_collar),
        speaker_count=speaker_count_error(meeting, hyp),
        settings={
            "der_collar": der_collar,
            "der_collar_semantics": "pyannote: total width centred on each reference boundary",
            "skip_overlap": skip_overlap,
            "tcp_collar": tcp_collar,
            "uem": [0.0, meeting.duration_s],
            "normalizer": normalizer.to_dict(),
        },
    )


def is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
