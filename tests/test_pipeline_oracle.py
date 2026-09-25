"""oracle / perturbed-oracle: ground truth in, controlled damage out.

Neither pipeline is a system under test.  These tests pin (1) that the
oracle reproduces meeting.json exactly, (2) that zero rates are the
identity, (3) that a seed fully determines the damage, (4) analytic
counts at rate 1.0, (5) monotone counts with rate, and (6) that the
public metric libraries (pyannote.metrics DER, meeteval cpWER) move the
way the damage says they should -- including the one case where they must
NOT move (a consistent relabelling is invisible to DER/cpWER).
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipeline import PipelineConfig, PipelineError, get_pipeline
from pipeline.meeting import reference_segments
from pipeline.oracle import (
    OraclePipeline,
    PerturbationConfig,
    PerturbedOraclePipeline,
    perturb_segments,
)
from pipeline.synthetic import synthetic_meeting

LAYOUT = [
    (0.5, 2.5, "spk0"),
    (3.0, 5.0, "spk1"),
    (5.5, 7.0, "spk0"),
    (7.5, 8.5, "spk1"),
    (9.0, 10.0, "spk0"),
    (10.5, 12.0, "spk1"),
]


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[dict, Path]:
    return synthetic_meeting(tmp_path_factory.mktemp("m"), meeting_id="orc1", layout=LAYOUT, words_per_segment=5)


def _big_meeting(tmp_path_factory, n_segments: int = 40, words: int = 5) -> tuple[dict, Path]:
    layout = [(i * 1.0, i * 1.0 + 0.8, f"spk{i % 3}") for i in range(n_segments)]
    return synthetic_meeting(tmp_path_factory.mktemp("big"), meeting_id="big", layout=layout, words_per_segment=words)


# --- oracle --------------------------------------------------------------------


def test_oracle_returns_ground_truth_exactly(meeting):
    m, d = meeting
    hyp = OraclePipeline().run(d / "mix.wav", d)
    assert hyp.meeting_id == "orc1"
    assert hyp.system == "oracle"
    assert hyp.segments == reference_segments(m)
    assert hyp.segments == m["segments"]  # the synthetic writer emits contract-shaped segments
    assert OraclePipeline.is_system_under_test is False
    assert hyp.extra["harness_self_test"] is True
    assert hyp.to_dict()["schema"] == "plaud-harness/hypothesis/1"


def test_oracle_finds_meeting_json_from_audio_path_alone(meeting):
    m, d = meeting
    hyp = OraclePipeline().run(d / "mix.wav")  # no meeting_dir argument
    assert hyp.segments == reference_segments(m)


def test_oracle_finds_meeting_json_from_device_subdir(meeting, tmp_path):
    m, d = meeting
    dev = d / "device"
    dev.mkdir(exist_ok=True)
    shutil.copy(d / "mix.wav", dev / "recording.wav")
    hyp = OraclePipeline().run(dev / "recording.wav")
    assert hyp.segments == reference_segments(m)


def test_oracle_without_meeting_json_raises(meeting, tmp_path):
    _, d = meeting
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(d / "mix.wav", lone / "mix.wav")
    with pytest.raises(PipelineError, match="no meeting.json"):
        OraclePipeline().run(lone / "mix.wav")


def test_oracle_rejects_wrong_schema(meeting, tmp_path):
    _, d = meeting
    bad = tmp_path / "bad"
    bad.mkdir()
    doc = json.loads((d / "meeting.json").read_text())
    doc["schema"] = "plaud-harness/meeting/999"
    (bad / "meeting.json").write_text(json.dumps(doc))
    shutil.copy(d / "mix.wav", bad / "mix.wav")
    with pytest.raises(PipelineError, match="schema"):
        OraclePipeline().run(bad / "mix.wav")


def test_oracle_returns_a_copy_not_a_view(meeting):
    m, d = meeting
    hyp = OraclePipeline().run(d / "mix.wav", d)
    hyp.segments[0]["text"] = "mutated"
    assert OraclePipeline().run(d / "mix.wav", d).segments[0]["text"] != "mutated"


# --- perturbed oracle: identity, determinism ------------------------------------


def test_perturbed_oracle_with_zero_rates_equals_oracle(meeting):
    m, d = meeting
    cfg = PerturbationConfig()
    assert cfg.is_identity
    p = PerturbedOraclePipeline(perturbation=cfg)
    hyp = p.run(d / "mix.wav", d)
    assert hyp.segments == OraclePipeline().run(d / "mix.wav", d).segments
    rep = p.last_report
    assert rep.speaker_swaps == rep.word_substitutions == rep.word_deletions == rep.word_insertions == 0
    assert rep.jittered_boundaries == 0 and rep.time_shift_s == 0.0
    assert rep.words_in == rep.words_out == 6 * 5


def test_perturbation_is_deterministic_under_seed(meeting):
    m, d = meeting
    cfg = dict(speaker_swap_rate=0.3, boundary_jitter_s=0.1, word_sub_rate=0.2, word_del_rate=0.2, word_ins_rate=0.2)
    a = PerturbedOraclePipeline(perturbation=PerturbationConfig(seed=7, **cfg)).run(d / "mix.wav", d).to_dict()
    b = PerturbedOraclePipeline(perturbation=PerturbationConfig(seed=7, **cfg)).run(d / "mix.wav", d).to_dict()
    c = PerturbedOraclePipeline(perturbation=PerturbationConfig(seed=8, **cfg)).run(d / "mix.wav", d).to_dict()
    assert a == b
    assert a["segments"] != c["segments"]
    assert a["segments"] != OraclePipeline().run(d / "mix.wav", d).to_dict()["segments"]


def test_seed_flows_from_pipeline_config(meeting):
    m, d = meeting
    p1 = get_pipeline("perturbed-oracle", PipelineConfig(seed=3, params={"word_sub_rate": 0.5}))
    p2 = get_pipeline("perturbed-oracle", PipelineConfig(seed=3, params={"word_sub_rate": 0.5}))
    p3 = get_pipeline("perturbed-oracle", PipelineConfig(seed=4, params={"word_sub_rate": 0.5}))
    h1, h2, h3 = (p.run(d / "mix.wav", d).to_dict() for p in (p1, p2, p3))
    assert h1 == h2 and h1 != h3
    assert p1.is_system_under_test is False


# --- analytic counts at rate 1.0 ----------------------------------------------------


def test_delete_everything(meeting):
    m, d = meeting
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(word_del_rate=1.0))
    hyp = p.run(d / "mix.wav", d)
    assert p.last_report.word_deletions == 30 and p.last_report.words_out == 0
    assert all(s["text"] == "" and s["words"] == [] for s in hyp.segments)
    assert [s["speaker"] for s in hyp.segments] == [s["speaker"] for s in m["segments"]]  # times/speakers intact
    assert [(s["start"], s["end"]) for s in hyp.segments] == [(s["start"], s["end"]) for s in m["segments"]]


def test_substitute_everything_changes_every_word_and_keeps_count(meeting):
    m, d = meeting
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(word_sub_rate=1.0, seed=1))
    hyp = p.run(d / "mix.wav", d)
    ref = reference_segments(m)
    assert p.last_report.word_substitutions == 30 and p.last_report.words_out == 30
    for r, h in zip(ref, hyp.segments):
        assert len(r["words"]) == len(h["words"])
        for rw, hw in zip(r["words"], h["words"]):
            assert rw["w"] != hw["w"]
            assert (rw["start"], rw["end"]) == (hw["start"], hw["end"])  # timing untouched
        assert h["text"] == " ".join(w["w"] for w in h["words"])


def test_insert_after_every_word_doubles_the_count(meeting):
    m, d = meeting
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(word_ins_rate=1.0, seed=2))
    hyp = p.run(d / "mix.wav", d)
    assert p.last_report.word_insertions == 30 and p.last_report.words_out == 60
    for s in hyp.segments:
        assert len(s["words"]) == 10
        for w in s["words"]:
            assert s["start"] <= w["start"] <= w["end"] <= s["end"]


def test_swap_every_speaker_in_two_speaker_meeting_flips_all(meeting):
    m, d = meeting
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(speaker_swap_rate=1.0, seed=5))
    hyp = p.run(d / "mix.wav", d)
    flip = {"spk0": "spk1", "spk1": "spk0"}
    assert [s["speaker"] for s in hyp.segments] == [flip[s["speaker"]] for s in m["segments"]]
    assert p.last_report.speaker_swaps == 6 and p.last_report.speaker_swaps_impossible == 0


def test_swap_in_single_speaker_meeting_is_impossible_and_counted(tmp_path):
    m, d = synthetic_meeting(tmp_path, meeting_id="solo", layout=[(0.5, 1.5, "spk0"), (2.0, 3.0, "spk0")])
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(speaker_swap_rate=1.0))
    hyp = p.run(d / "mix.wav", d)
    assert [s["speaker"] for s in hyp.segments] == ["spk0", "spk0"]
    assert p.last_report.speaker_swaps == 0 and p.last_report.speaker_swaps_impossible == 2


def test_time_shift_is_exact_and_clamps_at_zero(meeting):
    m, d = meeting
    hyp = PerturbedOraclePipeline(perturbation=PerturbationConfig(time_shift_s=0.25)).run(d / "mix.wav", d)
    for r, h in zip(m["segments"], hyp.segments):
        assert h["start"] == pytest.approx(r["start"] + 0.25, abs=1e-9)
        assert h["end"] == pytest.approx(r["end"] + 0.25, abs=1e-9)
        for rw, hw in zip(r["words"], h["words"]):
            assert hw["start"] == pytest.approx(rw["start"] + 0.25, abs=1e-9)
    # a shift past the origin clamps: first segment starts at 0.5 -> 0.0, keeps its end - start >= MIN
    hyp2 = PerturbedOraclePipeline(perturbation=PerturbationConfig(time_shift_s=-1.0)).run(d / "mix.wav", d)
    assert hyp2.segments[0]["start"] == 0.0 and hyp2.segments[0]["end"] == pytest.approx(1.5)
    hyp2.validate()


def test_boundary_jitter_is_bounded_and_keeps_contract(meeting):
    m, d = meeting
    j = 0.1
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(boundary_jitter_s=j, seed=11))
    hyp = p.run(d / "mix.wav", d)
    assert p.last_report.jittered_boundaries == 2 * len(m["segments"])
    # jitter re-sorts; compare by (speaker, text) identity which jitter does not touch
    by_key = {(s["speaker"], s["text"]): s for s in m["segments"]}
    moved = 0
    for h in hyp.segments:
        r = by_key[(h["speaker"], h["text"])]
        assert abs(h["start"] - r["start"]) <= j + 1e-9
        assert abs(h["end"] - r["end"]) <= j + 1e-9
        assert h["end"] > h["start"]
        moved += h["start"] != r["start"]
        for w in h["words"]:
            assert h["start"] <= w["start"] <= w["end"] <= h["end"]
    assert moved > 0
    hyp.validate()  # sorted by start, non-negative


# --- rates move counts ----------------------------------------------------------------


@pytest.mark.parametrize("field", ["word_sub_rate", "word_del_rate", "word_ins_rate", "speaker_swap_rate"])
def test_counts_track_rate_monotonically_and_within_binomial_bounds(tmp_path_factory, field):
    m, d = _big_meeting(tmp_path_factory, n_segments=40, words=5)  # 200 words, 40 segments
    n = 40 if field == "speaker_swap_rate" else 200
    counter = {
        "word_sub_rate": "word_substitutions",
        "word_del_rate": "word_deletions",
        "word_ins_rate": "word_insertions",
        "speaker_swap_rate": "speaker_swaps",
    }[field]
    counts = []
    for rate in (0.1, 0.5, 0.9):
        p = PerturbedOraclePipeline(perturbation=PerturbationConfig(seed=42, **{field: rate}))
        p.run(d / "mix.wav", d)
        c = getattr(p.last_report, counter)
        mean, sd = n * rate, (n * rate * (1 - rate)) ** 0.5
        assert abs(c - mean) <= 4 * sd + 1, f"{field}={rate}: count {c} vs expected {mean}+-{sd}"
        counts.append(c)
    assert counts[0] < counts[1] < counts[2]


def test_pure_function_perturb_segments_matches_pipeline(meeting):
    m, d = meeting
    cfg = PerturbationConfig(word_sub_rate=0.3, word_del_rate=0.3, seed=9)
    segs, rep = perturb_segments(reference_segments(m), ["spk0", "spk1"], cfg)
    p = PerturbedOraclePipeline(perturbation=copy.deepcopy(cfg))
    hyp = p.run(d / "mix.wav", d)
    assert segs == hyp.segments and rep.to_dict() == p.last_report.to_dict()


# --- invalid configs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        dict(word_sub_rate=0.6, word_del_rate=0.6),
        dict(word_sub_rate=1.5),
        dict(speaker_swap_rate=-0.1),
        dict(boundary_jitter_s=-0.5),
    ],
)
def test_invalid_perturbation_config_rejected(kw):
    with pytest.raises(PipelineError):
        PerturbationConfig(**kw).validate()


# --- the metric libraries move the right way ------------------------------------------------


def _der(ref_segments, hyp_segments, uri="m"):
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    def ann(segs):
        a = Annotation(uri=uri)
        for s in segs:
            a[Segment(s["start"], s["end"])] = s["speaker"]
        return a

    return float(DiarizationErrorRate(collar=0.0)(ann(ref_segments), ann(hyp_segments)))


def _cpwer(ref_segments, hyp_segments) -> float:
    from meeteval.wer import cp_word_error_rate

    def by_spk(segs):
        out: dict[str, list[str]] = {}
        for s in segs:
            out.setdefault(s["speaker"], []).append(s["text"])
        return {k: " ".join(v) for k, v in out.items()}

    return float(cp_word_error_rate(by_spk(ref_segments), by_spk(hyp_segments)).error_rate)


def test_oracle_scores_zero_on_both_metrics(meeting):
    m, d = meeting
    hyp = OraclePipeline().run(d / "mix.wav", d)
    assert _der(m["segments"], hyp.segments) == 0.0
    assert _cpwer(m["segments"], hyp.segments) == 0.0


def test_consistent_relabelling_is_invisible_to_der_and_cpwer(meeting):
    """swap_rate=1.0 on a 2-speaker meeting is a permutation, and both
    metrics optimise over speaker mappings: the damage must score ZERO.
    Anyone using swap_rate=1.0 with two speakers to 'raise DER' is wrong."""
    m, d = meeting
    hyp = PerturbedOraclePipeline(perturbation=PerturbationConfig(speaker_swap_rate=1.0)).run(d / "mix.wav", d)
    assert _der(m["segments"], hyp.segments) == 0.0
    assert _cpwer(m["segments"], hyp.segments) == 0.0


def test_partial_swaps_raise_der_and_cpwer(meeting):
    m, d = meeting
    p = PerturbedOraclePipeline(perturbation=PerturbationConfig(speaker_swap_rate=0.5, seed=1))
    hyp = p.run(d / "mix.wav", d)
    assert 0 < p.last_report.speaker_swaps < 6, "seed 1 must swap a strict subset for this test to mean anything"
    assert _der(m["segments"], hyp.segments) > 0.0
    assert _cpwer(m["segments"], hyp.segments) > 0.0


def test_word_damage_raises_cpwer_but_not_der(meeting):
    m, d = meeting
    hyp = PerturbedOraclePipeline(perturbation=PerturbationConfig(word_sub_rate=0.5, seed=3)).run(d / "mix.wav", d)
    assert _der(m["segments"], hyp.segments) == 0.0
    assert _cpwer(m["segments"], hyp.segments) > 0.0
    # More substitutions -> more cpWER, but NOT exactly the substitution
    # fraction: substitutes come from the meeting's own vocabulary, so the
    # alignment can re-match a substituted token elsewhere in the stream.
    half = _cpwer(m["segments"], hyp.segments)
    full = _cpwer(m["segments"], PerturbedOraclePipeline(perturbation=PerturbationConfig(word_sub_rate=1.0, seed=3)).run(d / "mix.wav", d).segments)
    assert 0.0 < half < full <= 1.0
    # Deleting every word IS analytic: N deletions over N reference words.
    gone = PerturbedOraclePipeline(perturbation=PerturbationConfig(word_del_rate=1.0)).run(d / "mix.wav", d)
    assert _cpwer(m["segments"], gone.segments) == pytest.approx(1.0)


def test_boundary_jitter_raises_der_not_cpwer(meeting):
    m, d = meeting
    hyp = PerturbedOraclePipeline(perturbation=PerturbationConfig(boundary_jitter_s=0.2, seed=4)).run(d / "mix.wav", d)
    assert _der(m["segments"], hyp.segments) > 0.0
    assert _cpwer(m["segments"], hyp.segments) == 0.0


def test_der_grows_with_jitter(meeting):
    m, d = meeting
    ders = [
        _der(m["segments"], PerturbedOraclePipeline(perturbation=PerturbationConfig(boundary_jitter_s=j, seed=4)).run(d / "mix.wav", d).segments)
        for j in (0.05, 0.2, 0.4)
    ]
    assert ders[0] < ders[1] < ders[2]
