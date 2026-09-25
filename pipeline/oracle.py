"""Harness self-test pipelines: ``oracle`` and ``perturbed-oracle``.

NEITHER IS A SYSTEM UNDER TEST.  Both read ``meeting.json`` next to the
audio and return the ground truth (optionally perturbed).  They exist so
the evals layer can be shown to (a) score a perfect hypothesis at zero
error and (b) move in the right direction as controlled damage is applied.
Any report that lists an oracle score as a system's score is wrong by
construction, and ``Pipeline.is_system_under_test`` is False here so the
CLI and the evals layer can refuse to.

All perturbation mechanics are HARNESS_POLICY.  They are driven by
``random.Random(seed)`` so a (seed, config) pair is fully reproducible
across platforms and numpy versions.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .base import (
    Hypothesis,
    Pipeline,
    PipelineConfig,
    PipelineError,
    Segment,
    Word,
    make_segment,
    register,
    sort_segments,
)
from .meeting import find_meeting_json, read_meeting, reference_segments

#: HARNESS_POLICY: substitution/insertion tokens are drawn from the meeting's
#: own vocabulary first; this list is the floor when that vocabulary is too
#: small to pick a *different* token.
FALLBACK_VOCAB = ("uh", "um", "yeah", "the", "and", "okay", "so", "right", "well", "like")

#: HARNESS_POLICY: a jittered/shifted segment never gets shorter than this.
MIN_SEGMENT_S = 0.01
#: HARNESS_POLICY: an inserted word is given this nominal duration.
INSERTED_WORD_S = 0.05


class OraclePipeline(Pipeline):
    """Returns the ground truth from meeting.json verbatim."""

    name = "oracle"
    description = "HARNESS SELF-TEST: returns meeting.json ground truth; never a system under test"
    is_system_under_test = False
    requires_meeting_dir = True

    def _meeting(self, audio_path: str | Path, meeting_dir: str | Path | None) -> dict[str, Any]:
        mj = find_meeting_json(audio_path, meeting_dir)
        if mj is None:
            raise PipelineError(
                f"{self.name}: no meeting.json found for {audio_path} (meeting_dir={meeting_dir}); "
                "the oracle needs ground truth"
            )
        return read_meeting(mj)

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        m = self._meeting(audio_path, meeting_dir)
        return Hypothesis(
            meeting_id=str(m["meeting_id"]),
            system=self.config.name or self.name,
            segments=reference_segments(m),
            extra={"harness_self_test": True, "source": "meeting.json"},
        ).validate()


@dataclass
class PerturbationConfig:
    """Rates are per-item probabilities in [0, 1]; times in seconds."""

    speaker_swap_rate: float = 0.0  # per segment: relabel to another speaker
    time_shift_s: float = 0.0  # constant offset added to every time
    boundary_jitter_s: float = 0.0  # per segment boundary: U(-j, +j)
    word_sub_rate: float = 0.0  # per word: replace with a different token
    word_del_rate: float = 0.0  # per word: delete
    word_ins_rate: float = 0.0  # per surviving word: insert a token after it
    seed: int = 0

    def validate(self) -> "PerturbationConfig":
        for k in ("speaker_swap_rate", "word_sub_rate", "word_del_rate", "word_ins_rate"):
            v = float(getattr(self, k))
            if not 0.0 <= v <= 1.0:
                raise PipelineError(f"{k}={v} must be in [0, 1]")
        if float(self.word_sub_rate) + float(self.word_del_rate) > 1.0:
            raise PipelineError("word_sub_rate + word_del_rate must not exceed 1")
        if float(self.boundary_jitter_s) < 0:
            raise PipelineError("boundary_jitter_s must be >= 0")
        return self

    @property
    def is_identity(self) -> bool:
        return (
            float(self.speaker_swap_rate) == 0.0
            and float(self.time_shift_s) == 0.0
            and float(self.boundary_jitter_s) == 0.0
            and float(self.word_sub_rate) == 0.0
            and float(self.word_del_rate) == 0.0
            and float(self.word_ins_rate) == 0.0
        )

    @classmethod
    def from_params(cls, params: dict[str, Any], seed: int = 0) -> "PerturbationConfig":
        kw = {k: params[k] for k in asdict(cls()) if k in params}
        kw.setdefault("seed", seed)
        return cls(**kw).validate()


@dataclass
class PerturbationReport:
    segments_in: int = 0
    segments_out: int = 0
    words_in: int = 0
    words_out: int = 0
    speaker_swaps: int = 0
    speaker_swaps_impossible: int = 0  # single-speaker meeting: nothing to swap to
    word_substitutions: int = 0
    word_deletions: int = 0
    word_insertions: int = 0
    jittered_boundaries: int = 0
    time_shift_s: float = 0.0
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _words_of(seg: Segment) -> list[Word]:
    ws = seg.get("words")
    if ws:
        return [{"w": str(w["w"]), "start": float(w["start"]), "end": float(w["end"])} for w in ws]
    # Contract says words are present; tolerate their absence by interpolating.
    from .meeting import interpolate_words

    return interpolate_words(str(seg.get("text", "")), float(seg["start"]), float(seg["end"]))


def perturb_segments(
    segments: list[Segment],
    speakers: list[str],
    cfg: PerturbationConfig,
) -> tuple[list[Segment], PerturbationReport]:
    """Apply the configured perturbations deterministically.

    Order (HARNESS_POLICY, fixed so a seed means one thing): speaker swaps
    -> word edits (delete | substitute | keep, then insert-after) -> global
    time shift -> boundary jitter -> re-sort by start.
    """
    cfg.validate()
    rng = random.Random(int(cfg.seed))
    rep = PerturbationReport(segments_in=len(segments), time_shift_s=float(cfg.time_shift_s), config=asdict(cfg))
    vocab: list[str] = sorted({w["w"] for s in segments for w in _words_of(s)} | set(FALLBACK_VOCAB))
    out: list[Segment] = []
    for seg in segments:
        spk = str(seg["speaker"])
        words = _words_of(seg)
        rep.words_in += len(words)

        # 1. speaker swap
        if cfg.speaker_swap_rate > 0 and rng.random() < cfg.speaker_swap_rate:
            others = [s for s in speakers if s != spk]
            if others:
                spk = rng.choice(others)
                rep.speaker_swaps += 1
            else:
                rep.speaker_swaps_impossible += 1

        # 2. word edits
        new_words: list[Word] = []
        for w in words:
            u = rng.random()
            if u < cfg.word_del_rate:
                rep.word_deletions += 1
                continue
            if u < cfg.word_del_rate + cfg.word_sub_rate:
                choices = [v for v in vocab if v != w["w"]]
                tok = rng.choice(choices) if choices else w["w"] + "x"
                new_words.append({"w": tok, "start": w["start"], "end": w["end"]})
                rep.word_substitutions += 1
            else:
                new_words.append(dict(w))
            if cfg.word_ins_rate > 0 and rng.random() < cfg.word_ins_rate:
                prev = new_words[-1]
                tok = rng.choice(vocab)
                new_words.append(
                    {"w": tok, "start": prev["end"], "end": min(float(seg["end"]), prev["end"] + INSERTED_WORD_S)}
                )
                rep.word_insertions += 1

        start, end = float(seg["start"]), float(seg["end"])

        # 3. global time shift
        if cfg.time_shift_s:
            d = float(cfg.time_shift_s)
            start, end = start + d, end + d
            for w in new_words:
                w["start"], w["end"] = w["start"] + d, w["end"] + d

        # 4. boundary jitter
        if cfg.boundary_jitter_s > 0:
            j = float(cfg.boundary_jitter_s)
            start += rng.uniform(-j, j)
            end += rng.uniform(-j, j)
            rep.jittered_boundaries += 2

        start = max(0.0, start)
        end = max(start + MIN_SEGMENT_S, end)
        for w in new_words:
            w["start"] = min(max(w["start"], start), end)
            w["end"] = min(max(w["end"], w["start"]), end)

        rep.words_out += len(new_words)
        out.append(make_segment(spk, start, end, " ".join(w["w"] for w in new_words), new_words))

    out = sort_segments(out)
    rep.segments_out = len(out)
    return out, rep


class PerturbedOraclePipeline(OraclePipeline):
    """Oracle + deterministic, seeded damage (see PerturbationConfig)."""

    name = "perturbed-oracle"
    description = (
        "HARNESS SELF-TEST: ground truth with seeded speaker swaps, time shift, boundary "
        "jitter and word sub/del/ins at configurable rates; validates that evals move"
    )
    is_system_under_test = False

    def __init__(self, config: PipelineConfig | None = None, perturbation: PerturbationConfig | None = None) -> None:
        super().__init__(config)
        self.perturbation = perturbation or PerturbationConfig.from_params(self.config.params, seed=self.config.seed)
        self.last_report: PerturbationReport | None = None

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        m = self._meeting(audio_path, meeting_dir)
        speakers = [str(s["id"]) for s in m["speakers"]]
        segs, rep = perturb_segments(reference_segments(m), speakers, self.perturbation)
        self.last_report = rep
        return Hypothesis(
            meeting_id=str(m["meeting_id"]),
            system=self.config.name or self.name,
            segments=segs,
            extra={"harness_self_test": True, "source": "meeting.json", "perturbation": rep.to_dict()},
        ).validate()


@register("oracle", description=OraclePipeline.description, is_system_under_test=False)
def _make_oracle(config: PipelineConfig | None = None) -> Pipeline:
    return OraclePipeline(config)


@register("perturbed-oracle", description=PerturbedOraclePipeline.description, is_system_under_test=False)
def _make_perturbed_oracle(config: PipelineConfig | None = None) -> Pipeline:
    return PerturbedOraclePipeline(config)
