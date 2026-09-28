"""Turn planner: the segment table is the ground truth, and it must obey the
scenario (order, pauses, overlap target, grid) exactly as documented."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.contract import GRID_SAMPLES, SAMPLE_RATE
from generator.meeting import assign_voices, generate_meeting
from generator.scenario import Scenario, TurnTaking, load_scenario
from generator.text import TextSource
from generator.tts.formant import FormantBackend
from generator.turns import Turn, activity_mask, build_dry_stems, mask_to_segments, overlap_ratio, plan_turns

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
    assert invariant_violations(turns, sc) == []


# --- planner invariants as a property over many seeds and presets (GEN-1) ---------
#
# The documented guarantees: no three speakers ever stack, a speaker never
# overlaps themself, an overlap never exceeds max_overlap_fraction of the shorter
# of the two turns (measured on the grid-quantised spans), a turn only ever
# overlaps its immediate neighbours, and the dry stem holds each turn's PCM
# untouched. One seed proved nothing: overlap_heavy seed 1 stacked three
# speakers for 250 samples and seed 27 overwrote the tail of a spk2 word.


def plan_scenario(sc: Scenario) -> list:
    backend = FormantBackend()
    return plan_turns(sc, TextSource(sc.seed, sc.text_corpus), backend, assign_voices(backend.voices(), sc.n_speakers, sc.seed))


def invariant_violations(turns: list, sc: Scenario) -> list[str]:
    n = int(round(sc.duration_s * SAMPLE_RATE))
    frac = sc.turn_taking.max_overlap_fraction
    problems: list[str] = []
    mask = activity_mask(turns, sc.n_speakers, n)
    peak = int(mask.sum(axis=0).max())
    if peak > 2:
        problems.append(f"{peak} speakers stack for {int(np.count_nonzero(mask.sum(axis=0) > 2))} samples")
    last_by_speaker: dict[int, object] = {}
    for t in turns:
        if t.start_sample % GRID_SAMPLES or t.end_sample % GRID_SAMPLES:
            problems.append(f"turn {t.index} is off the grid")
        before = last_by_speaker.get(t.speaker)
        if before is not None and t.start_sample < before.end_sample:
            problems.append(f"spk{t.speaker} overlaps itself: turn {before.index} ends {before.end_sample}, turn {t.index} starts {t.start_sample}")
        last_by_speaker[t.speaker] = t
    for a, b in zip(turns, turns[1:]):
        ov = a.end_sample - b.start_sample
        shorter = min(a.end_sample - a.start_sample, b.end_sample - b.start_sample)
        if ov > 0 and ov > frac * shorter:
            problems.append(f"turns {a.index}/{b.index} overlap {ov} samples > {frac} x {shorter}")
    for i, a in enumerate(turns):
        for b in turns[i + 2 :]:
            if b.start_sample < a.end_sample:
                problems.append(f"turn {b.index} overlaps non-adjacent turn {a.index}")
    # the mask is the table: its runs are the turns, except that same-speaker
    # turns which touch (end == next start) merge into one run
    merged: list[list[int]] = []
    for spk, a, b in sorted((t.speaker, t.start_sample, t.end_sample) for t in turns):
        if merged and merged[-1][0] == spk and merged[-1][2] == a:
            merged[-1][2] = b
        else:
            merged.append([spk, a, b])
    if sorted(mask_to_segments(mask)) != sorted(tuple(m) for m in merged):
        problems.append("activity mask runs differ from the (touch-merged) turn table")
    try:
        stems = build_dry_stems(turns, sc.n_speakers, n)
    except ValueError as exc:
        problems.append(f"build_dry_stems refused the table: {exc}")
    else:
        for t in turns:
            if not np.array_equal(stems[t.speaker, t.start_sample : t.start_sample + len(t.pcm)], t.pcm):
                problems.append(f"turn {t.index}: dry stem differs from its utterance (clobbered)")
    return problems


def stress(n_speakers: int, model: str, seed: int) -> Scenario:
    """Short turns and near-zero pauses at the maximum overlap: every cap binds."""
    return Scenario(
        name="stress", n_speakers=n_speakers, duration_s=30.0, seed=seed,
        turn_taking=TurnTaking(model=model, overlap_ratio=0.45, words_min=1, words_max=3, pause_min_s=0.0, pause_max_s=0.2),
    ).validate()


PROPERTY_CASES = [
    ("overlap_heavy", {}, range(1, 31)),
    ("notepin_s_noisy", {"turn_taking.overlap_ratio": 0.3}, range(1, 21)),
    ("default", {"turn_taking.overlap_ratio": 0.3}, range(1, 11)),
    ("smoke", {"n_speakers": 3, "turn_taking.overlap_ratio": 0.2}, range(1, 11)),
]


@pytest.mark.parametrize("preset, overrides, seeds", PROPERTY_CASES, ids=[c[0] for c in PROPERTY_CASES])
def test_planner_invariants_hold_for_every_seed_of_the_presets(preset: str, overrides: dict, seeds: range) -> None:
    failures = {}
    for seed in seeds:
        sc = load_scenario(preset, dict(overrides, seed=seed))
        problems = invariant_violations(plan_scenario(sc), sc)
        if problems:
            failures[seed] = problems[:3]
    assert failures == {}


@pytest.mark.parametrize("n_speakers, model", [(2, "alternating"), (3, "alternating"), (4, "random")])
def test_planner_invariants_hold_when_every_overlap_cap_binds(n_speakers: int, model: str) -> None:
    failures = {}
    for seed in range(1, 13):
        sc = stress(n_speakers, model, seed)
        turns = plan_scenario(sc)
        assert len(turns) >= 20
        problems = invariant_violations(turns, sc)
        if problems:
            failures[seed] = problems[:3]
    assert failures == {}


#: Measured |realised - target| (docs/generator.md section 1). Alternating turns
#: land within 0.0064 of 0.30 (default preset, seeds 1..30). In the random model a
#: same-speaker continuation adds speech that cannot be overlapped and catch-up is
#: capped at half the shorter turn, so overlap_heavy (target 0.30) falls short by
#: up to 0.043 (seed 10 of 1..30; mean -0.004). Bounds below: measured + margin.
TARGET_CASES = [
    ("overlap_heavy", {}, range(1, 31), 0.045, 0.01),
    ("default", {"turn_taking.overlap_ratio": 0.3}, range(1, 11), 0.01, 0.005),
]


@pytest.mark.parametrize("preset, overrides, seeds, worst, mean_tol", TARGET_CASES, ids=[c[0] for c in TARGET_CASES])
def test_overlap_target_is_met_across_seeds(preset: str, overrides: dict, seeds: range, worst: float, mean_tol: float) -> None:
    """The controller tracks the running ratio; measured over many seeds."""
    errors = []
    for seed in seeds:
        sc = load_scenario(preset, dict(overrides, seed=seed))
        mask = activity_mask(plan_scenario(sc), sc.n_speakers, int(round(sc.duration_s * SAMPLE_RATE)))
        errors.append(overlap_ratio(mask) - sc.turn_taking.overlap_ratio)
    assert max(abs(e) for e in errors) <= worst, errors
    assert abs(float(np.mean(errors))) <= mean_tol, errors


def test_build_dry_stems_refuses_a_same_speaker_overlap() -> None:
    """Assignment would silently overwrite the earlier turn's tail; refuse instead."""
    pcm = np.full(1000, 1000, dtype=np.int16)
    a = Turn(index=0, speaker=0, start_sample=0, end_sample=1000, text="a", words=[], pcm=pcm)
    b = Turn(index=1, speaker=0, start_sample=875, end_sample=1875, text="b", words=[], pcm=pcm)
    with pytest.raises(ValueError, match="overlap"):
        build_dry_stems([a, b], 1, 4000)
    c = Turn(index=1, speaker=0, start_sample=1000, end_sample=2000, text="c", words=[], pcm=pcm)
    assert build_dry_stems([a, c], 1, 4000)[0, :2000].min() == 1000  # touching spans are fine


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


def test_auto_assigned_voices_are_never_shared_between_speakers() -> None:
    """Two ground-truth speakers with one voice are indistinguishable by design;
    auto-assignment refuses instead of silently wrapping around the voice list."""
    voices = FormantBackend().voices()
    assert len(set(assign_voices(voices, len(voices), 3))) == len(voices)
    with pytest.raises(ValueError, match="speakers.voices"):
        assign_voices(voices, len(voices) + 1, 3)
    # an explicit list may repeat a voice on purpose
    sc = load_scenario("smoke", {"duration_s": 3.0, "speakers.voices": ["fv-alto", "fv-alto"]})
    assert generate_meeting(sc).voices == ["fv-alto", "fv-alto"]
