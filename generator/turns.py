"""Turn-taking planner: the segment table IS the ground truth.

Given a scenario, a text source and a TTS backend, lay utterances on a
timeline. Speaker activity is defined by this table (each turn's
[start, end) span, including its intra-utterance word gaps, which is the
usual diarization convention); nothing downstream detects it from energy.

Overlap control (HARNESS_POLICY): consecutive turns of DIFFERENT speakers may
overlap. Before placing each turn the planner computes the overlap d that
would bring the realised ratio (overlapped samples / union-of-speech
samples) to the target after the turn is added,
    d = (target * (union + dur_q) - overlapped) / (1 + target),
where dur_q is the new turn's grid-quantised span. d is rounded to the
NEAREST grid step and then capped, on the grid, at
    quantise_down(max_overlap_fraction * min(prev_span, dur_q))
and at prev.end - earliest, where `earliest` is the end of every turn before
the previous one (and of the same speaker's last turn). The overlap itself is
quantised, never the start: prev.end is on the grid, so start = prev.end - d
is too, and the realised overlap is exactly d. (Quantising the start instead
turned d into ceil_grid(d), up to 124 samples over the cap; that stacked
three speakers and let a speaker overwrite their own previous turn.)

Invariants (property-tested over many seeds, tests/test_generator_turns.py):
* a turn overlaps at most its immediate predecessor and successor, so no
  three turns are ever active at once and the running totals stay exact;
* an overlap never exceeds max_overlap_fraction (<= 0.5) of the shorter of
  the two grid-quantised spans;
* a speaker never overlaps themself (a speaker cannot talk over themself),
  which `build_dry_stems` enforces by refusing such a table.

All turn boundaries are quantised to the 1/128 s grid (contract.GRID_SAMPLES):
a non-overlapping start is rounded down to the grid, end is rounded up, so a
turn contains up to 124 samples of trailing digital silence and every word
lies inside it.
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
    # No new turn may start before `earliest`: the latest end of every turn
    # except the previous one. It is on the grid (every end is).
    earliest = 0
    last_end_by_speaker: dict[int, int] = {}
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
            dur_q = quantise_up(dur)  # the span the turn will occupy (start is on the grid)
            floor = max(earliest, last_end_by_speaker.get(speaker, 0))
            if prev is None:
                start = quantise_down(max(cursor, 0))
            else:
                start = quantise_down(max(prev.end_sample + int(round(pause * SAMPLE_RATE)), 0))
                if tt.overlap_ratio > 0 and speaker != prev.speaker:
                    d = overlap_samples(
                        target=tt.overlap_ratio,
                        union=union,
                        overlapped=overlapped,
                        dur_q=dur_q,
                        prev_span=prev.end_sample - prev.start_sample,
                        max_fraction=tt.max_overlap_fraction,
                        room=prev.end_sample - floor,
                    )
                    if d > 0:
                        start = prev.end_sample - d
            start = max(start, floor)
            end = start + dur_q
            if end <= n_samples:
                placed = (start, end, result, text)
                break
            n_words = n_words // 2 if n_words > 1 else 0
        if placed is None:
            break
        start, end, result, text = placed
        actual_overlap = max(0, prev.end_sample - start) if prev is not None else 0
        # start >= earliest, so the new turn can only overlap `prev`: exact totals
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
        if prev is not None:
            earliest = max(earliest, prev.end_sample)
        last_end_by_speaker[speaker] = end
        prev = turn
        index += 1
    return turns


def overlap_samples(
    target: float,
    union: int,
    overlapped: int,
    dur_q: int,
    prev_span: int,
    max_fraction: float,
    room: int,
) -> int:
    """Overlap (samples, a multiple of GRID_SAMPLES) for the next turn.

    `need` brings overlapped/union to `target` once the turn is added; it is
    rounded to the nearest grid step (unbiased), then capped on the grid at
    max_fraction of the shorter span and at `room` (prev.end - earliest start).
    """
    need = (target * (union + dur_q) - overlapped) / (1.0 + target)
    if need <= 0:
        return 0
    d = GRID_SAMPLES * int(np.floor(need / GRID_SAMPLES + 0.5))
    cap = quantise_down(int(max_fraction * min(prev_span, dur_q)))
    return int(max(0, min(d, cap, quantise_down(max(room, 0)))))


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
    start. Placement is assignment, which is only exact when a speaker's turns
    never overlap; a table where they do is refused (ValueError) rather than
    letting a later turn silently overwrite an earlier one's words."""
    stems = np.zeros((n_speakers, n_samples), dtype=np.int16)
    by_speaker: dict[int, list[Turn]] = {}
    for t in turns:
        by_speaker.setdefault(t.speaker, []).append(t)
    for spk, own in by_speaker.items():
        own = sorted(own, key=lambda t: t.start_sample)
        for a, b in zip(own, own[1:]):
            if b.start_sample < a.end_sample:
                raise ValueError(
                    f"spk{spk} turns {a.index} [{a.start_sample},{a.end_sample}) and {b.index} "
                    f"[{b.start_sample},{b.end_sample}) overlap: a speaker cannot overlap themself"
                )
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
    "overlap_samples",
    "plan_turns",
]
