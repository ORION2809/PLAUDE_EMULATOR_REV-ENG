"""Layer 3 metrics against hand-built cases with closed-form answers.

Nothing here comes from the synthetic generator: every reference and
hypothesis is written out by hand so the expected DER, JER, WER, cpWER and
tcpWER values can be derived on paper first and asserted second.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from evals.aggregate import macro_average, micro_average  # noqa: E402
from evals.gates import evaluate, get_suite, load_gates  # noqa: E402
from evals.io import Hypothesis, Meeting, Normalizer, Segment, Word  # noqa: E402
from evals.metrics import (  # noqa: E402
    DEFAULT_DER_COLLAR,
    DEFAULT_TCP_COLLAR,
    cp_wer,
    diarization_error_rate,
    jaccard_error_rate,
    score_meeting,
    speaker_count_error,
    tcp_wer,
    to_seglst,
    word_error_rate,
)

# --------------------------------------------------------------------------- #
# The closed-form diarization case
#
#   reference   A: [0, 10]            B: [10, 20]           (duration 30 s)
#   hypothesis  x: [0, 9]  y: [10, 18]  x: [18, 20]  y: [25, 25.5]
#
#   optimal mapping x->A, y->B (overlap 9 + 8 = 17 s; the alternative gives 2 s)
#   miss       [9, 10]      = 1.0 s   (no hypothesis speech)
#   confusion  [18, 20]     = 2.0 s   (reference B, hypothesis x = A)
#   false alarm[25, 25.5]   = 0.5 s   (no reference speech)
#   total reference speech  = 20 s
#   DER (collar 0) = (1.0 + 0.5 + 2.0) / 20 = 0.175
# --------------------------------------------------------------------------- #

REF = [Segment("A", 0.0, 10.0, "one two three"), Segment("B", 10.0, 20.0, "four five")]
HYP = [
    Segment("x", 0.0, 9.0, "one two three"),
    Segment("y", 10.0, 18.0, "four five"),
    Segment("x", 18.0, 20.0, ""),
    Segment("y", 25.0, 25.5, ""),
]
DURATION = 30.0


def test_der_closed_form_with_collar_zero() -> None:
    d = diarization_error_rate(REF, HYP, duration_s=DURATION, collar=0.0)
    assert d.total == 20.0
    assert d.miss == 1.0
    assert d.false_alarm == 0.5
    assert d.confusion == 2.0
    assert d.correct == 17.0
    assert d.der == pytest.approx(0.175)
    assert d.mapping == {"x": "A", "y": "B"}


def test_der_per_speaker_breakdown_is_exact_for_the_closed_form() -> None:
    d = diarization_error_rate(REF, HYP, duration_s=DURATION, collar=0.0)
    a, b = d.per_speaker["A"], d.per_speaker["B"]
    assert (a.total, a.correct, a.miss, a.confusion, a.false_alarm) == (10.0, 9.0, 1.0, 0.0, 0.0)
    assert (b.total, b.correct, b.miss, b.confusion, b.false_alarm) == (10.0, 8.0, 0.0, 2.0, 0.5)
    assert a.mapped_to == "x" and b.mapped_to == "y"
    assert a.error_rate == pytest.approx(0.1) and b.error_rate == pytest.approx(0.25)


def test_der_collar_is_total_width_centred_on_reference_boundaries() -> None:
    # collar 0.5 => ±0.25 s around 0, 10, 10, 20:
    #   reference total 20 - 0.25 - 0.5 - 0.25 = 19.0
    #   miss [9, 9.75] = 0.75, confusion [18, 19.75] = 1.75, false alarm untouched 0.5
    d = diarization_error_rate(REF, HYP, duration_s=DURATION, collar=0.5)
    assert d.total == pytest.approx(19.0)
    assert d.miss == pytest.approx(0.75)
    assert d.confusion == pytest.approx(1.75)
    assert d.false_alarm == pytest.approx(0.5)
    assert d.correct == pytest.approx(16.5)
    assert d.der == pytest.approx(3.0 / 19.0)
    # and the default is the documented 0.25 (±0.125 s)
    assert DEFAULT_DER_COLLAR == 0.25
    d2 = diarization_error_rate(REF, HYP, duration_s=DURATION)
    assert d2.total == pytest.approx(19.5)


def test_der_uem_is_the_meeting_duration() -> None:
    # hypothesis speech after duration_s is outside the scoring region
    d = diarization_error_rate(REF, HYP, duration_s=24.0, collar=0.0)
    assert d.false_alarm == 0.0 and d.der == pytest.approx(0.15)
    # without a duration, the union of extents is used (25.5 s here) -> the false alarm counts
    d2 = diarization_error_rate(REF, HYP, duration_s=None, collar=0.0)
    assert d2.false_alarm == 0.5


# --------------------------------------------------------------------------- #
# Overlap handling
#
#   reference   A: [0, 10]   B: [8, 12]      overlap region [8, 10]
#   hypothesis  a: [0, 10]                   (B is never detected)
#
#   scoring everything (collar 0): total 14, miss 4 (B entirely) -> 4/14
#   skip_overlap: [8, 10] removed: total 8 + 2 = 10, miss 2       -> 0.2
#   overlap region only: total 4 (two tracks × 2 s), miss 2      -> 0.5
# --------------------------------------------------------------------------- #

REF_OV = [Segment("A", 0.0, 10.0, "a"), Segment("B", 8.0, 12.0, "b")]
HYP_OV = [Segment("a", 0.0, 10.0, "a")]


def test_der_scores_overlap_regions_by_default_and_can_skip_them() -> None:
    full = diarization_error_rate(REF_OV, HYP_OV, duration_s=15.0, collar=0.0)
    assert full.total == 14.0 and full.miss == 4.0 and full.der == pytest.approx(4 / 14)
    skipped = diarization_error_rate(REF_OV, HYP_OV, duration_s=15.0, collar=0.0, skip_overlap=True)
    assert skipped.total == 10.0 and skipped.miss == 2.0 and skipped.der == pytest.approx(0.2)
    assert skipped.overlap is None


def test_overlap_region_der_is_reported_separately() -> None:
    full = diarization_error_rate(REF_OV, HYP_OV, duration_s=15.0, collar=0.0)
    ov = full.overlap
    assert ov is not None
    assert ov.overlap_time == 2.0 and ov.total == 4.0 and ov.miss == 2.0 and ov.correct == 2.0
    assert ov.der == pytest.approx(0.5)
    # no overlap in the reference -> overlap block is present but empty
    none = diarization_error_rate(REF, HYP, duration_s=DURATION, collar=0.0).overlap
    assert none is not None and none.overlap_time == 0.0 and none.der is None


def test_per_speaker_breakdown_sums_to_global_components_in_overlap_with_collar() -> None:
    ref = [Segment("A", 0.0, 10.0, "x"), Segment("B", 8.0, 14.0, "y"), Segment("C", 12.0, 16.0, "z")]
    hyp = [
        Segment("p", 0.0, 9.0, "x"),
        Segment("q", 9.0, 15.0, "y"),
        Segment("r", 13.0, 17.0, "z"),
        Segment("s", 20.0, 22.0, "w"),  # an extra, unmapped hypothesis speaker
    ]
    d = diarization_error_rate(ref, hyp, duration_s=25.0, collar=0.25)
    per = d.per_speaker
    assert d.mapping == {"p": "A", "q": "B", "r": "C"}
    for name, total in (("total", d.total), ("correct", d.correct), ("miss", d.miss), ("confusion", d.confusion), ("false_alarm", d.false_alarm)):
        assert sum(getattr(b, name) for b in per.values()) == pytest.approx(total), name
    # By hand, with ±0.125 s extruded around 0, 8, 10, 12, 14, 16:
    #   [8.125, 9]   ref {A,B} hyp {p->A}     -> B missed          0.875
    #   [9, 9.875]   ref {A,B} hyp {q->B}     -> A missed          0.875
    #   [12.125, 13] ref {B,C} hyp {q->B}     -> C missed          0.875
    #   [14.125, 15] ref {C}   hyp {q->B,r->C}-> q is a false alarm 0.875 (charged to B, q's owner)
    #   [16.125, 17] ref {}    hyp {r->C}     -> r is a false alarm 0.875 (charged to C)
    #   [20, 22]     ref {}    hyp {s}        -> unmapped speaker   2.0
    expect = {
        "A": (9.5, 8.625, 0.875, 0.0, 0.0),
        "B": (5.25, 4.375, 0.875, 0.0, 0.875),
        "C": (3.5, 2.625, 0.875, 0.0, 0.875),
        "hyp:s": (0.0, 0.0, 0.0, 0.0, 2.0),
    }
    got = {k: (b.total, b.correct, b.miss, b.confusion, b.false_alarm) for k, b in per.items()}
    assert set(got) == set(expect)
    for k, vals in expect.items():
        assert got[k] == pytest.approx(vals), k
    assert (d.total, d.correct, d.miss, d.confusion, d.false_alarm) == pytest.approx((18.25, 15.625, 2.625, 0.0, 3.75))


# --------------------------------------------------------------------------- #
# JER
# --------------------------------------------------------------------------- #


def test_jer_trivial_case() -> None:
    # reference A [0, 10], hypothesis x [0, 5]: union 10, miss 5 -> 0.5
    j = jaccard_error_rate([Segment("A", 0.0, 10.0, "a")], [Segment("x", 0.0, 5.0, "a")], duration_s=10.0, collar=0.0)
    assert j.jer == pytest.approx(0.5) and j.speaker_count == 1 and j.per_speaker == {"A": pytest.approx(0.5)}
    perfect = jaccard_error_rate(REF, REF, duration_s=DURATION, collar=0.0)
    assert perfect.jer == 0.0


def test_jer_closed_form_and_per_speaker_mean_equals_global() -> None:
    # A: union [0,10]∪[18,20] = 12, fa 2 + miss 1 -> 0.25
    # B: union [10,20]∪[25,25.5] = 10.5, fa 0.5 + miss 2 -> 2.5/10.5
    j = jaccard_error_rate(REF, HYP, duration_s=DURATION, collar=0.0)
    assert j.per_speaker["A"] == pytest.approx(0.25)
    assert j.per_speaker["B"] == pytest.approx(2.5 / 10.5)
    assert j.jer == pytest.approx((0.25 + 2.5 / 10.5) / 2)
    assert j.jer == pytest.approx(sum(j.per_speaker.values()) / len(j.per_speaker))


# --------------------------------------------------------------------------- #
# WER vs cpWER: the decision-log rationale
# --------------------------------------------------------------------------- #

REF_TXT = [Segment("A", 0.0, 4.0, "hello world this is"), Segment("B", 4.0, 6.0, "good bye")]
HYP_SWAPPED = [Segment("B", 0.0, 4.0, "hello world this is"), Segment("A", 4.0, 6.0, "good bye")]
HYP_RELABELLED = [Segment("S9", 0.0, 4.0, "hello world this is"), Segment("S3", 4.0, 6.0, "good bye")]


def test_speaker_swap_makes_literal_wer_lie_but_leaves_cpwer_unchanged() -> None:
    # literal pairing: A(4 ref words) vs "good bye" -> 2 S + 2 D; B(2) vs 4 words -> 2 S + 2 I; 8 / 6
    literal = word_error_rate(REF_TXT, HYP_SWAPPED, mode="literal")
    assert literal.wer == pytest.approx(8 / 6)
    assert (literal.substitutions, literal.deletions, literal.insertions) == (4, 2, 2)
    assert literal.wer >= 1.0  # "high"
    before = cp_wer(REF_TXT, REF_TXT)
    after = cp_wer(REF_TXT, HYP_SWAPPED)
    assert before.error_rate == 0.0 and after.error_rate == 0.0
    assert after.errors == before.errors == 0 and after.length == before.length == 6
    assert set(after.assignment) == {("A", "B"), ("B", "A")}
    # arbitrary labels: literal WER is 200 % (nothing pairs), cpWER still 0
    assert word_error_rate(REF_TXT, HYP_RELABELLED, mode="literal").wer == pytest.approx(2.0)
    assert cp_wer(REF_TXT, HYP_RELABELLED).error_rate == 0.0


def test_concatenated_wer_is_speaker_agnostic() -> None:
    # time order is unchanged by the swap, so concatenation cannot see it either
    assert word_error_rate(REF_TXT, HYP_SWAPPED, mode="concat").wer == 0.0
    c = word_error_rate(REF_TXT, [Segment("z", 0.0, 6.0, "hello world this is good bye extra")], mode="concat")
    assert (c.hits, c.substitutions, c.deletions, c.insertions, c.wer) == (6, 0, 0, 1, pytest.approx(1 / 6))


def test_jiwer_counts_on_a_known_alignment() -> None:
    w = word_error_rate([Segment("A", 0, 1, "a b c d")], [Segment("A", 0, 1, "a x c d e")])
    assert (w.hits, w.substitutions, w.deletions, w.insertions) == (3, 1, 0, 1)
    assert w.wer == pytest.approx(0.5) and w.reference_words == 4 and w.hypothesis_words == 5
    assert w.per_speaker["A"]["substitutions"] == 1
    with pytest.raises(ValueError):
        word_error_rate(REF_TXT, REF_TXT, mode="nope")


def test_wer_handles_empty_sides_without_jiwer_errors() -> None:
    w = word_error_rate(REF_TXT, [], mode="literal")
    assert w.deletions == 6 and w.wer == pytest.approx(1.0)
    w2 = word_error_rate([], REF_TXT, mode="concat")
    assert w2.insertions == 6 and w2.wer is None  # no reference words -> undefined rate
    assert word_error_rate([], [], mode="concat").wer is None


def test_cpwer_counts_missed_and_false_alarm_speakers() -> None:
    empty = cp_wer(REF_TXT, [])
    assert empty.error_rate == 1.0 and empty.deletions == 6 and empty.missed_speaker == 2
    extra = cp_wer(REF_TXT, HYP_SWAPPED + [Segment("C", 6.0, 7.0, "extra")])
    assert extra.insertions == 1 and extra.falarm_speaker == 1 and extra.error_rate == pytest.approx(1 / 6)


def test_normalisation_is_applied_identically_to_both_sides() -> None:
    hyp = [Segment("A", 0.0, 4.0, "Hello, WORLD -- this is"), Segment("B", 4.0, 6.0, "good-bye!")]
    assert cp_wer(REF_TXT, hyp).error_rate == 0.0
    assert word_error_rate(REF_TXT, hyp).wer == 0.0
    assert cp_wer(REF_TXT, hyp, normalizer=Normalizer(lowercase=False)).error_rate > 0.0


# --------------------------------------------------------------------------- #
# tcpWER: time sensitivity
# --------------------------------------------------------------------------- #


def timed(spk: str, words: list[str], t0: float, shift: float = 0.0) -> Segment:
    ws = tuple(Word(w, t0 + shift + 0.5 * i, t0 + shift + 0.5 * i + 0.4) for i, w in enumerate(words))
    return Segment(spk, ws[0].start, ws[-1].end, " ".join(words), ws)


REF_TIMED = [timed("A", ["hello", "world", "this", "is", "a", "test"], 0.0), timed("B", ["good", "bye", "now"], 3.0)]


def shifted(shift: float) -> list[Segment]:
    return [timed("x", ["hello", "world", "this", "is", "a", "test"], 0.0, shift), timed("y", ["good", "bye", "now"], 3.0, shift)]


def test_tcpwer_is_zero_for_a_shift_inside_the_collar_and_total_beyond_it() -> None:
    assert DEFAULT_TCP_COLLAR == 5.0
    inside = tcp_wer(REF_TIMED, shifted(0.3), collar=5.0)
    assert inside.error_rate == 0.0 and inside.word_level_timing is True
    # +10 s with a 5 s collar: no hypothesis word can reach any reference word
    # -> every reference word deleted and every hypothesis word inserted = 18/9
    beyond = tcp_wer(REF_TIMED, shifted(10.0), collar=5.0)
    assert beyond.error_rate == pytest.approx(2.0)
    assert (beyond.substitutions, beyond.deletions, beyond.insertions, beyond.length) == (0, 9, 9, 9)
    # widen the collar past the shift and the words match again
    assert tcp_wer(REF_TIMED, shifted(10.0), collar=20.0).error_rate == 0.0
    # cpWER, being time-agnostic, never noticed
    assert cp_wer(REF_TIMED, shifted(10.0)).error_rate == 0.0


def test_tcpwer_falls_back_to_pseudo_word_timing_without_word_timings() -> None:
    no_words = [Segment(s.speaker, s.start, s.end, s.text) for s in REF_TIMED]
    r = tcp_wer(REF_TIMED, no_words, collar=5.0)
    assert r.word_level_timing is False and r.error_rate == 0.0
    seg, wl = to_seglst(no_words, "m", Normalizer(), word_level=True)
    assert wl is False and len(seg) == 2
    seg2, wl2 = to_seglst(REF_TIMED, "m", Normalizer(), word_level=True)
    assert wl2 is True and len(seg2) == 9


def test_to_seglst_drops_segments_with_no_scorable_words() -> None:
    seg, _ = to_seglst([Segment("A", 0, 1, "..."), Segment("B", 1, 2, "ok")], "m", Normalizer(), word_level=False)
    assert [s["speaker"] for s in seg.segments] == ["B"]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def meeting_from(segments: list[Segment], meeting_id: str = "m", duration: float = DURATION, speakers=None) -> Meeting:
    ids = speakers or sorted({s.speaker for s in segments})
    return Meeting(meeting_id, 16000, duration, 1, [{"id": i} for i in ids], sorted(segments, key=lambda s: s.start))


def test_oracle_scores_zero_everywhere() -> None:
    m = meeting_from(REF_TIMED, duration=10.0)
    rep = score_meeting(m, Hypothesis("m", "oracle", list(REF_TIMED)))
    flat = rep.flat()
    for key in ("der.der", "jer.jer", "wer_literal.wer", "wer_concat.wer", "cpwer.error_rate", "tcpwer.error_rate"):
        assert flat[key] == 0.0, key
    assert flat["speaker_count.error"] == 0 and rep.der.overlap is not None
    assert rep.settings["uem"] == [0.0, 10.0] and rep.settings["der_collar"] == DEFAULT_DER_COLLAR
    md = rep.to_markdown()
    assert "| cpWER | 0.0000 |" in md and "| DER | 0.0000 |" in md


def test_empty_hypothesis_is_total_failure() -> None:
    m = meeting_from(REF, speakers=["A", "B", "ghost"])
    rep = score_meeting(m, Hypothesis("m", "silent", []))
    assert rep.der.der == 1.0 and rep.der.miss == rep.der.total
    assert rep.jer.jer == 1.0
    assert rep.cpwer.error_rate == 1.0 and rep.cpwer.missed_speaker == 2
    assert rep.tcpwer.error_rate == 1.0
    assert rep.speaker_count.reference_declared == 3 and rep.speaker_count.reference_active == 2
    assert rep.speaker_count.error == -2 and rep.speaker_count.abs_error == 2


def test_score_meeting_rejects_a_hypothesis_for_another_meeting() -> None:
    with pytest.raises(ValueError, match="does not match"):
        score_meeting(meeting_from(REF), Hypothesis("other", "s", list(HYP)))
    rep = score_meeting(meeting_from(REF), Hypothesis("other", "s", list(HYP)), check_meeting_id=False)
    # default collar 0.25 (±0.125 s around 0, 10, 10, 20): total 19.5,
    # miss [9, 9.875] 0.875, confusion [18, 19.875] 1.875, false alarm 0.5
    assert (rep.der.total, rep.der.miss, rep.der.confusion, rep.der.false_alarm) == pytest.approx((19.5, 0.875, 1.875, 0.5))
    assert rep.der.der == pytest.approx((0.875 + 1.875 + 0.5) / 19.5)


def test_speaker_count_error_uses_active_reference_speakers() -> None:
    m = meeting_from(REF, speakers=["A", "B", "C"])
    r = speaker_count_error(m, Hypothesis("m", "s", [Segment("p", 0, 1, "x"), Segment("q", 1, 2, "y"), Segment("r", 2, 3, "z")]))
    assert (r.reference_declared, r.reference_active, r.hypothesis, r.error, r.abs_error) == (3, 2, 3, 1, 1)


def test_macro_and_micro_averages_differ_by_construction() -> None:
    # meeting 1: the closed form (20 s reference, 3.5 s error); meeting 2: perfect, 10 s reference
    m1 = meeting_from(REF, "m1")
    r1 = score_meeting(m1, Hypothesis("m1", "s", list(HYP)), der_collar=0.0)
    m2 = meeting_from([Segment("A", 0.0, 10.0, "one two")], "m2", duration=10.0)
    r2 = score_meeting(m2, Hypothesis("m2", "s", [Segment("z", 0.0, 10.0, "one two")]), der_collar=0.0)
    macro, micro = macro_average([r1, r2]), micro_average([r1, r2])
    assert macro["n"] == 2 and micro["n"] == 2
    assert macro["der.der"] == pytest.approx(0.175 / 2)
    assert micro["der.der"] == pytest.approx(3.5 / 30.0)
    assert micro["der.total"] == 30.0 and micro["cpwer.length"] == 7
    assert macro["cpwer.error_rate"] == 0.0 and micro["cpwer.error_rate"] == 0.0
    assert micro["jer.jer"] == pytest.approx((0.25 + 2.5 / 10.5 + 0.0) / 3)
    assert macro["counts"]["der.overlap.der"] == 0  # None on both meetings -> excluded
    assert macro_average([]) == {"n": 0} and micro_average([]) == {"n": 0}


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-25): label clashes, empty references, overlap mapping,
# same-speaker overlap, collar validation
# --------------------------------------------------------------------------- #


def assert_breakdown_sums_to_global(d) -> None:
    for name in ("total", "correct", "miss", "confusion", "false_alarm"):
        assert sum(getattr(b, name) for b in d.per_speaker.values()) == pytest.approx(getattr(d, name)), name


def test_breakdown_is_exact_when_hypothesis_labels_reuse_reference_names() -> None:
    """EV-1.  The generator writes spk<N> and energy_vad writes spk<N>, so a
    hypothesis label equal to a reference label is the normal case, not an
    edge case.  An *unmapped* hypothesis spk0 must not count as correct for
    the reference spk0."""
    ref = [Segment("spk0", 0.0, 10.0, "a"), Segment("spk1", 10.0, 20.0, "b")]
    hyp = [Segment("spk0", 0.0, 2.0, "a"), Segment("spk1", 2.0, 10.0, "a"), Segment("spk2", 10.0, 20.0, "b")]
    d = diarization_error_rate(ref, hyp, duration_s=20.0, collar=0.0)
    assert (d.total, d.correct, d.confusion, d.miss, d.false_alarm) == (20.0, 18.0, 2.0, 0.0, 0.0)
    assert d.mapping == {"spk1": "spk0", "spk2": "spk1"}
    got = {k: (b.total, b.correct, b.miss, b.confusion, b.false_alarm, b.mapped_to) for k, b in d.per_speaker.items()}
    assert got == {
        "spk0": (10.0, 8.0, 0.0, 2.0, 0.0, "spk1"),  # [0, 2] is hypothesis spk0 = unmapped -> confusion
        "spk1": (10.0, 10.0, 0.0, 0.0, 0.0, "spk2"),
        "hyp:spk0": (0.0, 0.0, 0.0, 0.0, 0.0, None),
    }
    assert_breakdown_sums_to_global(d)


def test_breakdown_charges_an_unmapped_clashing_label_its_own_false_alarm() -> None:
    ref = [Segment("A", 0.0, 10.0, "x"), Segment("B", 10.0, 20.0, "y")]
    hyp = [Segment("X", 0.0, 10.0, "x"), Segment("Y", 10.0, 20.0, "y"), Segment("A", 25.0, 27.0, "")]
    d = diarization_error_rate(ref, hyp, duration_s=30.0, collar=0.0)
    assert d.false_alarm == 2.0 and d.mapping == {"X": "A", "Y": "B"}
    assert d.per_speaker["A"].false_alarm == 0.0 and d.per_speaker["hyp:A"].false_alarm == 2.0
    assert_breakdown_sums_to_global(d)
    # a reference speaker literally named "hyp:z" never shares a key with the unmapped hypothesis "z"
    d2 = diarization_error_rate(
        [Segment("hyp:z", 0.0, 10.0, "x")], [Segment("q", 0.0, 10.0, "x"), Segment("z", 12.0, 13.0, "")],
        duration_s=20.0, collar=0.0,
    )
    assert set(d2.per_speaker) == {"hyp:z", "hyp:z#2"}
    assert d2.per_speaker["hyp:z"].mapped_to == "q" and d2.per_speaker["hyp:z#2"].false_alarm == 1.0


@pytest.mark.parametrize("collar", [0.0, 0.25, 0.5])
def test_breakdown_attributes_clashing_labels_correctly_past_26_speakers(collar: float) -> None:
    """30 reference speakers spk0..spk29 (past pyannote's 'Z' -> 'AA' label
    rollover); the hypothesis names them spk1..spk30 (off by one, as two
    independent spk<N> numberings would) and adds a short, unmapped spk0 inside
    reference spk5's turn.  That spk0's false alarm belongs to hyp:spk0, not to
    the reference spk0."""
    ref = [Segment(f"spk{i}", 2.0 * i, 2.0 * i + 1.5, "w") for i in range(30)]
    hyp = [Segment(f"spk{i + 1}", 2.0 * i + 0.1, 2.0 * i + 1.6, "w") for i in range(30)]
    hyp.append(Segment("spk0", 10.5, 11.0, ""))  # inside ref spk5 [10, 11.5], away from every boundary
    d = diarization_error_rate(ref, hyp, duration_s=64.0, collar=collar)
    assert d.mapping == {f"spk{i + 1}": f"spk{i}" for i in range(30)}
    assert d.per_speaker["hyp:spk0"].false_alarm == pytest.approx(0.5)
    assert d.per_speaker["spk0"].mapped_to == "spk1"
    # every mapped hypothesis overruns its reference turn by 0.1 s (inside the collar once collar/2 >= 0.1)
    boundary_fa = max(0.0, 0.1 - collar / 2)
    for i in range(30):
        assert d.per_speaker[f"spk{i}"].false_alarm == pytest.approx(boundary_fa), i
    assert_breakdown_sums_to_global(d)


@pytest.mark.parametrize(
    "segments",
    [[], [Segment("A", 1.0, 1.2, "yes")]],
    ids=["no-segments", "all-speech-inside-the-collar"],
)
def test_meeting_without_scorable_reference_speech_gives_undefined_rates_not_a_crash(segments) -> None:
    """EV-2.  pyannote's JER divides by the reference speaker count; with no
    scorable reference speaker that was a ZeroDivisionError."""
    m = Meeting("m", 16000, 10.0, 1, [{"id": "A"}], list(segments))
    for hyp_segments in ([], list(segments), [Segment("x", 2.0, 3.0, "noise")]):
        rep = score_meeting(m, Hypothesis("m", "s", hyp_segments))
        assert rep.jer.jer is None and rep.jer.speaker_count == 0 and rep.jer.per_speaker == {}
        assert rep.der.der is None and rep.der.total == 0.0
    assert rep.der.false_alarm == 1.0  # the hypothesis' speech is still counted
    gate = evaluate(get_suite(load_gates(), "synthetic-clean"), rep.flat(), target="m")
    assert not gate.passed and {"der.der", "jer.jer"} <= set(gate.failed_metrics)  # fail closed


def test_jer_with_every_reference_second_removed_by_skip_overlap_is_undefined() -> None:
    both = [Segment("A", 0.0, 5.0, "a"), Segment("B", 0.0, 5.0, "b")]
    j = jaccard_error_rate(both, [Segment("x", 0.0, 5.0, "a")], duration_s=5.0, collar=0.0, skip_overlap=True)
    assert j.jer is None and j.speaker_count == 0


def test_overlap_der_is_scored_under_the_global_mapping() -> None:
    """EV-3.  Reference A [0,10] B [8,20]; the hypothesis gives the second voice
    in [8,10] to its own cluster w.  Globally w is unmapped, so [8,10] holds
    2 s of confusion; a mapping re-optimised on the overlap alone (w -> A)
    used to hide it."""
    ref = [Segment("A", 0.0, 10.0, "a"), Segment("B", 8.0, 20.0, "b")]
    hyp = [Segment("x", 0.0, 8.0, "a"), Segment("y", 8.0, 20.0, "b"), Segment("w", 8.0, 10.0, "a")]
    d = diarization_error_rate(ref, hyp, duration_s=20.0, collar=0.0)
    assert d.mapping == {"x": "A", "y": "B"} and d.confusion == 2.0
    assert d.per_speaker["A"].confusion == 2.0
    ov = d.overlap
    assert (ov.overlap_time, ov.total, ov.correct, ov.confusion, ov.miss, ov.false_alarm) == (2.0, 4.0, 2.0, 2.0, 0.0, 0.0)
    assert ov.der == pytest.approx(0.5)


@pytest.mark.parametrize("collar", [0.0, 0.25])
def test_overlap_components_are_a_restriction_of_the_global_components(collar: float) -> None:
    ref = [Segment("A", 0.0, 10.0, "x"), Segment("B", 8.0, 14.0, "y"), Segment("C", 12.0, 16.0, "z")]
    hyp = [Segment("C", 0.0, 9.0, "x"), Segment("A", 9.0, 15.0, "y"), Segment("r", 8.5, 17.0, "z"), Segment("B", 20.0, 22.0, "w")]
    d = diarization_error_rate(ref, hyp, duration_s=25.0, collar=collar)
    ov = d.overlap
    for name in ("total", "correct", "miss", "confusion", "false_alarm"):
        assert getattr(ov, name) <= getattr(d, name) + 1e-9, name
    assert ov.total == pytest.approx(2 * (4.0 - (2 * collar if collar else 0.0)))  # two tracks over [8,10] and [12,14]


def test_overlapping_segments_of_one_speaker_count_once() -> None:
    """EV-4.  Two overlapping hypothesis segments with the same label are one
    speaker talking, not two (DER now agrees with JER, which already used the
    per-label union)."""
    ref = [Segment("A", 0.0, 10.0, "one two three four")]
    hyp = [Segment("s", 0.0, 6.0, "one two"), Segment("s", 5.0, 10.0, "three four")]
    d = diarization_error_rate(ref, hyp, duration_s=10.0, collar=0.0)
    assert (d.der, d.false_alarm, d.correct) == (0.0, 0.0, 10.0)
    assert jaccard_error_rate(ref, hyp, duration_s=10.0, collar=0.0).jer == 0.0
    # two *different* hypothesis speakers overlapping are still two tracks
    two = diarization_error_rate(ref, [Segment("s", 0.0, 10.0, "a"), Segment("t", 5.0, 10.0, "b")], duration_s=10.0, collar=0.0)
    assert two.false_alarm == 5.0
    # the reference follows the same rule: A [0,6] + A [5,10] is 10 s of A, and not an overlap region
    r2 = diarization_error_rate([Segment("A", 0.0, 6.0, "x"), Segment("A", 5.0, 10.0, "y")], [Segment("s", 0.0, 10.0, "z")], duration_s=10.0, collar=0.0)
    assert (r2.total, r2.der, r2.overlap.overlap_time) == (10.0, 0.0, 0.0)
    # touching segments are left alone (their boundary keeps its collar)
    touch = diarization_error_rate([Segment("A", 0.0, 5.0, "x"), Segment("A", 5.0, 10.0, "y")], [Segment("s", 0.0, 10.0, "z")], duration_s=10.0, collar=0.5)
    assert touch.total == pytest.approx(10.0 - 0.25 - 0.5 - 0.25)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_negative_or_non_finite_collars_are_rejected(bad: float) -> None:
    """EV-5(e).  pyannote treats a negative collar as 0 (``if collar > 0.``)
    while the report would record the negative value."""
    m = meeting_from(REF)
    with pytest.raises(ValueError, match="der_collar"):
        score_meeting(m, Hypothesis("m", "s", list(HYP)), der_collar=bad)
    with pytest.raises(ValueError, match="tcp_collar"):
        score_meeting(m, Hypothesis("m", "s", list(HYP)), tcp_collar=bad)
    with pytest.raises(ValueError, match="collar"):
        diarization_error_rate(REF, HYP, duration_s=DURATION, collar=bad)
    with pytest.raises(ValueError, match="collar"):
        tcp_wer(REF_TIMED, shifted(0.0), collar=bad)


def _tie_case() -> tuple[list[Segment], list[Segment]]:
    """Hypothesis h02 and h10 tie for reference R (1 s each); ten other pairs
    match one-to-one.  pyannote renames the hypothesis 0, 1, ... and sorts labels
    as strings (0, 1, 10, 11, 2, ...), which reorders the co-occurrence matrix
    and so decides the tie (pyannote/metrics/diarization.py:161-168)."""
    others = [i for i in range(12) if i not in (2, 10)]
    ref = [Segment("R", 0.0, 2.0, "r")] + [Segment(f"s{i:02d}", 10.0 + 2 * i, 11.0 + 2 * i, "x") for i in others]
    hyp = [Segment("h02", 0.0, 1.0, "r"), Segment("h10", 1.0, 2.0, "r"), Segment("h10", 4.0, 8.0, "")]
    hyp += [Segment(f"h{i:02d}", 10.0 + 2 * i, 11.0 + 2 * i, "x") for i in others]
    return ref, hyp


def test_reported_mapping_is_the_one_pyannote_scored_with_even_on_ties() -> None:
    """The DER mapping (and every mapped_to) must be pyannote's, captured here
    from inside DiarizationErrorRate.compute_components."""
    from pyannote.core.utils.generators import int_generator, string_generator
    from pyannote.metrics.diarization import DiarizationErrorRate

    from evals.metrics import to_annotation, to_uem

    class Spy(DiarizationErrorRate):
        def optimal_mapping(self, reference, hypothesis, uem=None):
            self.seen = super().optimal_mapping(reference, hypothesis, uem)
            return self.seen

    ref, hyp = _tie_case()
    r0, h0 = to_annotation(ref, "m"), to_annotation(hyp, "m")
    uem = to_uem(40.0, "m", r0, h0)
    spy = Spy(collar=0.0)
    spy(r0, h0, uem=uem)
    R, H = spy.uemify(r0, h0, uem=uem)
    r_names, h_names = dict(zip(string_generator(), R.labels())), dict(zip(int_generator(), H.labels()))
    used = {h_names[h]: r_names[r] for h, r in spy.seen.items()}

    d = diarization_error_rate(ref, hyp, duration_s=40.0, collar=0.0)
    assert used["h10"] == "R"  # the tie went to h10 inside pyannote
    assert d.mapping == used
    assert d.per_speaker["R"].mapped_to == "h10" and "hyp:h02" in d.per_speaker
    assert_breakdown_sums_to_global(d)


def test_per_speaker_jer_uses_pyannotes_mapping_so_its_mean_is_the_library_jer() -> None:
    """Tied candidates can have different JER (h10 also talks for 4 s elsewhere):
    the per-speaker mirror must pick pyannote's, or its mean drifts from the library's."""
    ref, hyp = _tie_case()
    j = jaccard_error_rate(ref, hyp, duration_s=40.0, collar=0.0)
    assert j.jer == pytest.approx(sum(j.per_speaker.values()) / len(j.per_speaker))
    assert j.per_speaker["R"] == pytest.approx(5 / 6)  # h10: miss 1 + false alarm 4 over a 6 s union
