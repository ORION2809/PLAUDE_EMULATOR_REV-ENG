"""Turn planner: the segment table is the ground truth, and it must obey the
scenario (order, pauses, overlap target, grid) exactly as documented."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.contract import GRID_SAMPLES, SAMPLE_RATE
from generator.meeting import generate_meeting
from generator.scenario import Scenario, TurnTaking, load_scenario
from generator.text import TextSource
from generator.tts.formant import FormantBackend
from generator.turns import activity_mask, build_dry_stems, mask_to_segments, overlap_ratio, plan_turns

VOICES = ["fv-alto", "fv-bass", "fv-tenor", "fv-mezzo"]


def plan(**kw):
    tt = kw.pop("turn_taking", {})
    sc = Scenario(name="t", n_speakers=kw.pop("n_speakers", 2), duration_s=kw.pop("duration_s", 30.0), seed=kw.pop("seed", 1), turn_taking=TurnTaking(**tt), **kw)
    sc.validate()
    turns = plan_turns(sc, TextSource(sc.seed), FormantBackend(), VOICES[: sc.n_speakers])
    return sc, turns


def test_alternating_order_grid_and_containment() -> None:
    sc, turns = plan(n_speakers=3, duration_s=30.0)
    n = int(sc.duration_s * SAMPLE_RATE)
    assert len(turns) >= 6
    assert [t.speaker for t in turns] == [i % 3 for i in range(len(turns))]
    for t in turns:
        assert t.start_sample % GRID_SAMPLES == 0 and t.end_sample % GRID_SAMPLES == 0
        assert 0 <= t.start_sample < t.end_sample <= n
        assert len(t.pcm) <= t.end_sample - t.start_sample < len(t.pcm) + GRID_SAMPLES
        assert t.words[0].start_sample == t.start_sample
        assert t.words[-1].end_sample <= t.end_sample
        for a, b in zip(t.words, t.words[1:]):
            assert a.end_sample <= b.start_sample
        assert " ".join(w.word for w in t.words) == t.text


def test_no_overlap_means_pauses_within_the_configured_range() -> None:
    sc, turns = plan(turn_taking={"overlap_ratio": 0.0, "pause_min_s": 0.3, "pause_max_s": 0.8})
    mask = activity_mask(turns, 2, int(sc.duration_s * SAMPLE_RATE))
    assert overlap_ratio(mask) == 0.0
    for a, b in zip(turns, turns[1:]):
        gap = (b.start_sample - a.end_sample) / SAMPLE_RATE
        # start is rounded DOWN to the grid, so the gap may shrink by < 1 grid step
        assert 0.3 - GRID_SAMPLES / SAMPLE_RATE <= gap <= 0.8 + 1e-9


def test_fixed_pause_distribution_is_exact_up_to_the_grid() -> None:
    sc, turns = plan(turn_taking={"pause_distribution": "fixed", "pause_mean_s": 0.5, "pause_min_s": 0.1, "pause_max_s": 1.0})
    for a, b in zip(turns, turns[1:]):
        gap = b.start_sample - a.end_sample
        assert 0.5 * SAMPLE_RATE - GRID_SAMPLES < gap <= 0.5 * SAMPLE_RATE


@pytest.mark.parametrize("target", [0.1, 0.2, 0.3])
def test_overlap_target_is_realised_within_tolerance(target: float) -> None:
    sc, turns = plan(n_speakers=3, duration_s=60.0, turn_taking={"overlap_ratio": target, "words_min": 5, "words_max": 12})
    mask = activity_mask(turns, 3, int(sc.duration_s * SAMPLE_RATE))
    realised = overlap_ratio(mask)
    assert abs(realised - target) <= 0.04, (target, realised)
    assert mask.sum(axis=0).max() <= 2, "the controller never stacks three speakers"


def test_overlap_only_between_different_speakers() -> None:
    sc, turns = plan(n_speakers=2, duration_s=40.0, turn_taking={"model": "random", "p_self_continue": 1.0, "overlap_ratio": 0.3})
    assert len({t.speaker for t in turns}) == 1, "p_self_continue=1 keeps the floor"
    mask = activity_mask(turns, 2, int(sc.duration_s * SAMPLE_RATE))
    assert overlap_ratio(mask) == 0.0
    for a, b in zip(turns, turns[1:]):
        assert b.start_sample >= a.end_sample


def test_random_model_never_repeats_speaker_when_self_continue_is_zero() -> None:
    sc, turns = plan(n_speakers=4, duration_s=60.0, turn_taking={"model": "random", "p_self_continue": 0.0})
    assert len(turns) >= 10
    assert all(a.speaker != b.speaker for a, b in zip(turns, turns[1:]))
    assert len({t.speaker for t in turns}) == 4


def test_planner_is_seed_deterministic() -> None:
    def table(seed):
        _, turns = plan(seed=seed, turn_taking={"model": "random", "overlap_ratio": 0.15})
        return [(t.speaker, t.start_sample, t.end_sample, t.text) for t in turns]

    assert table(4) == table(4)
    assert table(4) != table(5)


def test_dry_stems_and_mask_are_the_table() -> None:
    sc, turns = plan(n_speakers=2, duration_s=20.0, turn_taking={"overlap_ratio": 0.2})
    n = int(sc.duration_s * SAMPLE_RATE)
    stems = build_dry_stems(turns, 2, n)
    mask = activity_mask(turns, 2, n)
    for t in turns:
        assert np.array_equal(stems[t.speaker, t.start_sample : t.start_sample + len(t.pcm)], t.pcm)
        assert np.all(mask[t.speaker, t.start_sample : t.end_sample])
    # a stem is silent wherever its speaker's mask is False
    for spk in range(2):
        assert np.all(stems[spk, ~mask[spk]] == 0)
    expected = sorted((t.speaker, t.start_sample, t.end_sample) for t in turns)
    assert sorted(mask_to_segments(mask)) == expected


def test_impossible_duration_raises_clearly() -> None:
    sc = load_scenario("smoke", {"duration_s": 1.0, "turn_taking.words_min": 30, "turn_taking.words_max": 30, "turn_taking.lead_in_s": 0.9})
    with pytest.raises(ValueError, match="no utterance fits"):
        generate_meeting(sc)
