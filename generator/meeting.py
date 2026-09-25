"""Generate one synthetic meeting in memory (no files).

Pipeline: scenario -> text -> TTS utterances -> turn table (ground truth)
-> dry stems + sample mask -> room simulation -> device mix -> int16.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from generator.contract import SAMPLE_RATE
from generator.devices import PRESETS as DEVICE_PRESETS
from generator.devices import device_mix
from generator.room import RoomResult, simulate_room
from generator.scenario import Scenario, load_scenario
from generator.text import TextSource
from generator.tts import TTSBackend, get_backend
from generator.turns import Turn, activity_mask, build_dry_stems, overlap_ratio, plan_turns

GENERATOR_NAME = "plaud-harness-generator"
GENERATOR_VERSION = "0.1.0"

#: HARNESS_POLICY: peak-normalise the mic array and the device mix together
#: to this full-scale fraction before int16 conversion.
PEAK_TARGET = 0.9


@dataclass
class Meeting:
    meeting_id: str
    scenario: Scenario
    turns: list[Turn]
    voices: list[str]
    dry_stems: np.ndarray  # (n_speakers, n) int16
    mask: np.ndarray  # (n_speakers, n) bool
    room: RoomResult
    mics: np.ndarray  # (n_mics, n) int16
    mix: np.ndarray  # (device channels, n) int16
    mono_mix: np.ndarray  # (1, n) int16
    stereo_mix: np.ndarray  # (2, n) int16
    gain: float
    tts_backend: str

    @property
    def n_samples(self) -> int:
        return int(self.dry_stems.shape[1])

    @property
    def duration_s(self) -> float:
        return self.n_samples / SAMPLE_RATE

    @property
    def speaker_ids(self) -> list[str]:
        return [f"spk{i}" for i in range(self.scenario.n_speakers)]

    @property
    def overlap_ratio(self) -> float:
        return overlap_ratio(self.mask)


def _seed_int(*parts: object) -> int:
    material = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def meeting_id_for(scenario: Scenario) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in scenario.name.lower()).strip("-") or "scenario"
    return f"synth-{safe}-s{scenario.seed:04d}"


def assign_voices(available: list[str], n_speakers: int, seed: int) -> list[str]:
    """Round-robin over the backend's voices, rotated by the seed (HARNESS_POLICY)."""
    if not available:
        raise ValueError("backend offers no voices")
    offset = seed % len(available)
    return [available[(offset + i) % len(available)] for i in range(n_speakers)]


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)


def generate_meeting(scenario: Scenario | str | dict, tts: TTSBackend | None = None) -> Meeting:
    scenario = load_scenario(scenario)
    backend = tts or get_backend(scenario.tts_backend)
    voices = scenario.speakers.voices or assign_voices(backend.voices(), scenario.n_speakers, scenario.seed)
    for v in voices:
        if v not in backend.voices():
            raise KeyError(f"voice {v!r} is not offered by backend {backend.name!r}")
    text = TextSource(scenario.seed, scenario.text_corpus)
    turns = plan_turns(scenario, text, backend, voices)
    if not turns:
        raise ValueError("no utterance fits inside duration_s; lengthen the scenario or shorten the turns")
    n = int(round(scenario.duration_s * SAMPLE_RATE))
    dry = build_dry_stems(turns, scenario.n_speakers, n)
    mask = activity_mask(turns, scenario.n_speakers, n)
    room = simulate_room(scenario, dry, np.random.default_rng(_seed_int("noise", scenario.seed)))
    preset = DEVICE_PRESETS[scenario.device.preset]
    mono = device_mix(room.mic_signals, preset, 1)
    stereo = device_mix(room.mic_signals, preset, 2)
    peak = max(float(np.max(np.abs(room.mic_signals))), float(np.max(np.abs(mono))), float(np.max(np.abs(stereo))))
    gain = PEAK_TARGET / peak if peak > 0 else 1.0
    mono16 = to_int16(mono * gain)
    stereo16 = to_int16(stereo * gain)
    return Meeting(
        meeting_id=meeting_id_for(scenario),
        scenario=scenario,
        turns=turns,
        voices=list(voices),
        dry_stems=dry,
        mask=mask,
        room=room,
        mics=to_int16(room.mic_signals * gain),
        mix=mono16 if scenario.device.channels == 1 else stereo16,
        mono_mix=mono16,
        stereo_mix=stereo16,
        gain=float(gain),
        tts_backend=backend.name,
    )


__all__ = ["GENERATOR_NAME", "GENERATOR_VERSION", "Meeting", "assign_voices", "generate_meeting", "meeting_id_for", "to_int16"]
