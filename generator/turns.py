"""Turn-taking planner: the segment table IS the ground truth.

Given a scenario, a text source and a TTS backend, lay utterances on a
timeline. Speaker activity is defined by this table (each turn's
[start, end) span, including its intra-utterance word gaps, which is the
usual diarization convention); nothing downstream detects it from energy.

Overlap control (HARNESS_POLICY): consecutive turns of DIFFERENT speakers may
overlap. Before placing each turn the planner computes the overlap d that
would bring the realised ratio (overlapped seconds / union-of-speech
seconds) to the target after the turn is added,
    d = (target * (union + dur) - overlapped) / (1 + target),
clipped to [0, max_overlap_fraction * min(prev_dur, dur)]. Because d never
exceeds half of either turn, no three turns ever overlap at once and the
running totals stay exact. Same-speaker consecutive turns never overlap
(a speaker cannot talk over themself).

All turn boundaries are quantised to the 1/128 s grid (contract.GRID_SAMPLES):
start is rounded down to the grid, end is rounded up, so a turn contains up
to 124 samples of trailing digital silence and every word lies inside it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from generator.contract import GRID_SAMPLES, SAMPLE_RATE, quantise_down, quantise_up
from generator.scenario import Scenario
from generator.text import TextSource
from generator.tts.base import SynthResult, TTSBackend, WordTiming


@dataclass
class Turn:
    index: int
    speaker: int
    start_sample: int
    end_sample: int
    text: str
    words: list[WordTiming]  # absolute sample positions
    pcm: np.ndarray  # int16 utterance, length <= end - start
    timing_exact: bool = True
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def start_s(self) -> float:
        return self.start_sample / SAMPLE_RATE

    @property
    def end_s(self) -> float:
        return self.end_sample / SAMPLE_RATE


def _seed_int(*parts: object) -> int:
    material = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def _sample_pause(tt, rng: np.random.Generator) -> float:
    if tt.pause_distribution == "fixed":
        return float(np.clip(tt.pause_mean_s, tt.pause_min_s, tt.pause_max_s))
    if tt.pause_distribution == "exponential":
        return float(np.clip(rng.exponential(tt.pause_mean_s), tt.pause_min_s, tt.pause_max_s))
    return float(rng.uniform(tt.pause_min_s, tt.pause_max_s))


def plan_turns(
    scenario: Scenario,
    text_source: TextSource,
    tts: TTSBackend,
    voices: list[str],
) -> list[Turn]:
    tt = scenario.turn_taking
    n_spk = scenario.n_speakers
    n_samples = int(round(scenario.duration_s * SAMPLE_RATE))
    rng = np.random.default_rng(_seed_int("turns", scenario.seed))
    turns: list[Turn] = []
    union = 0  # samples of speech (union over speakers)
    overlapped = 0  # samples where two speakers are active
    prev: Turn | None = None
    cursor = quantise_up(int(round(tt.lead_in_s * SAMPLE_RATE)))
    index = 0
    while True:
        if tt.model == "alternating" or n_spk == 1:
            speaker = index % n_spk
        else:
            if prev is not None and rng.uniform() < tt.p_self_continue:
                speaker = prev.speaker
            else:
                choices = [s for s in range(n_spk) if prev is None or s != prev.speaker]
                speaker = int(rng.choice(choices))
        n_words = int(rng.integers(tt.words_min, tt.words_max + 1))
        pause = _sample_pause(tt, rng)
        placed = None
        while n_words >= 1:
            text = text_source.utterance(n_words)
            result: SynthResult = tts.synth(text, voices[speaker], _seed_int(scenario.seed, index, n_words))
            dur = result.duration_samples
            if prev is None:
                start = cursor
                d = 0
            else:
                start = prev.end_sample + int(round(pause * SAMPLE_RATE))
                d = 0
                if tt.overlap_ratio > 0 and speaker != prev.speaker:
                    prev_dur = prev.end_sample - prev.start_sample
                    need = (tt.overlap_ratio * (union + dur) - overlapped) / (1.0 + tt.overlap_ratio)
                    d_max = int(tt.max_overlap_fraction * min(prev_dur, dur))
                    d = int(np.clip(need, 0, d_max))
                    if d > 0:
                        start = prev.end_sample - d
            start = quantise_down(max(start, 0))
            end = quantise_up(start + dur)
            if end <= n_samples:
                placed = (start, end, result, text)
                break
            n_words = n_words // 2 if n_words > 1 else 0
        if placed is None:
            break
        start, end, result, text = placed
        actual_overlap = max(0, prev.end_sample - start) if prev is not None else 0
        overlapped += actual_overlap
        union += (end - start) - actual_overlap
        words = [WordTiming(w.word, start + w.start_sample, start + w.end_sample) for w in result.words]
        turn = Turn(
            index=index,
            speaker=speaker,
            start_sample=start,
            end_sample=end,
            text=text,
            words=words,
            pcm=result.pcm,
            timing_exact=result.timing_exact,
            notes=dict(result.notes),
        )
        turns.append(turn)
        prev = turn
        index += 1
    return turns


def activity_mask(turns: list[Turn], n_speakers: int, n_samples: int) -> np.ndarray:
    """Sample-accurate (n_speakers, n_samples) boolean mask from the table."""
    mask = np.zeros((n_speakers, n_samples), dtype=bool)
    for t in turns:
        mask[t.speaker, t.start_sample : t.end_sample] = True
    return mask


def overlap_ratio(mask: np.ndarray) -> float:
    """overlapped samples / union-of-speech samples (0.0 when silent)."""
    active = mask.sum(axis=0)
    union = int(np.count_nonzero(active >= 1))
    if union == 0:
        return 0.0
    return float(np.count_nonzero(active >= 2)) / union


def mask_to_segments(mask: np.ndarray) -> list[tuple[int, int, int]]:
    """(speaker, start_sample, end_sample) runs of True per speaker row."""
    out: list[tuple[int, int, int]] = []
    for spk in range(mask.shape[0]):
        row = mask[spk].astype(np.int8)
        edges = np.diff(np.concatenate([[0], row, [0]]))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        out.extend((spk, int(s), int(e)) for s, e in zip(starts, ends))
    return sorted(out, key=lambda t: (t[1], t[0]))


def build_dry_stems(turns: list[Turn], n_speakers: int, n_samples: int) -> np.ndarray:
    """(n_speakers, n_samples) int16 dry stems: each turn's pcm placed at its
    start. Same-speaker turns never overlap, so placement is assignment."""
    stems = np.zeros((n_speakers, n_samples), dtype=np.int16)
    for t in turns:
        n = len(t.pcm)
        if t.start_sample + n > t.end_sample:
            raise AssertionError("turn pcm longer than its grid-quantised span")
        stems[t.speaker, t.start_sample : t.start_sample + n] = t.pcm
    return stems


__all__ = [
    "GRID_SAMPLES",
    "Turn",
    "activity_mask",
    "build_dry_stems",
    "mask_to_segments",
    "overlap_ratio",
    "plan_turns",
]
