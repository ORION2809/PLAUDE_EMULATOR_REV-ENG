"""Scenario specification: dataclasses + YAML/JSON loading + named presets.

Every default value in this file is HARNESS_POLICY unless its comment cites
evidence. The scenario is the single input of the generator; the same
scenario and seed always produce byte-identical output.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from generator.devices import PRESETS as DEVICE_PRESETS

TURN_MODELS = ("alternating", "random")
PAUSE_DISTRIBUTIONS = ("uniform", "exponential", "fixed")
NOISE_KINDS = ("none", "white", "musan")

#: HARNESS_POLICY: image-source order cap. inverse_sabine asks for ~40 at RT60
#: 0.3 s in a 6x5x3 m room; 24 keeps every simulation under a second while
#: realising most of the requested tail (the measured RT60 is reported).
DEFAULT_MAX_ORDER_CAP = 24


@dataclass
class TurnTaking:
    model: str = "alternating"
    overlap_ratio: float = 0.0  # target: overlapped speech / union of speech (both in seconds)
    max_overlap_fraction: float = 0.5  # overlap <= this fraction of the shorter of the two turns
    pause_distribution: str = "uniform"
    pause_min_s: float = 0.2
    pause_max_s: float = 1.0
    pause_mean_s: float = 0.5  # exponential / fixed only
    p_self_continue: float = 0.15  # random model: probability the same speaker takes the next turn
    words_min: int = 4
    words_max: int = 14
    lead_in_s: float = 0.5


@dataclass
class Room:
    dims_m: tuple[float, float, float] = (6.0, 5.0, 3.0)
    rt60_s: float | None = 0.3
    absorption: float | None = None  # used instead of rt60 when set
    max_order: int | None = None  # None -> inverse_sabine, capped at DEFAULT_MAX_ORDER_CAP


@dataclass
class Device:
    preset: str = "note_pro_4mic"
    position_m: tuple[float, float, float] = (3.0, 2.5, 0.75)
    yaw_deg: float = 0.0
    channels: int = 1  # recorded channels: 1 or 2 (BleFile.channels, ledger section 8)


@dataclass
class Noise:
    kind: str = "none"
    snr_db: float = 20.0
    musan_dir: str | None = None  # default data/corpora/musan/<category> when kind == musan
    category: str = "noise"
    position_m: tuple[float, float, float] | None = None  # musan source position; None -> a room corner


@dataclass
class Speakers:
    positions_m: list[tuple[float, float, float]] | None = None  # None -> auto ring around the device
    voices: list[str] | None = None  # None -> assigned from the backend's voice list
    radius_m: float = 1.2
    height_m: float = 1.2


@dataclass
class Export:
    opus_bitrate_per_channel: int = 32000  # EVIDENCE: 80 B / 20 ms / channel (ledger section 8)
    opus_complexity: int = 5  # HARNESS_POLICY: libopus complexity 0-10; firmware value UNKNOWN; 5 halves encode time
    include_stereo_ogg: bool = True  # required when device.channels == 2 (it is then the primary recording)
    include_raw_opus: bool = True
    include_g4: bool = True
    include_e2ee: bool = True  # both encrypted shapes: sealed Ogg and sealed bare packet stream
    write_mics_wav: bool = True


@dataclass
class Scenario:
    name: str = "default"
    n_speakers: int = 3
    duration_s: float = 40.0
    seed: int = 1
    tts_backend: str = "formant"
    text_corpus: str | None = None
    turn_taking: TurnTaking = field(default_factory=TurnTaking)
    room: Room = field(default_factory=Room)
    device: Device = field(default_factory=Device)
    noise: Noise = field(default_factory=Noise)
    speakers: Speakers = field(default_factory=Speakers)
    export: Export = field(default_factory=Export)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return _build(cls, dict(data))

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.to_dict(), sort_keys=True)

    def with_overrides(self, overrides: dict[str, Any]) -> "Scenario":
        """Apply dotted-key overrides, e.g. {"turn_taking.overlap_ratio": 0.2}."""
        data = self.to_dict()
        for key, value in overrides.items():
            node = data
            parts = key.split(".")
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    raise KeyError(f"unknown scenario key {key!r}")
                node = node[part]
            if parts[-1] not in node:
                raise KeyError(f"unknown scenario key {key!r}")
            node[parts[-1]] = value
        return Scenario.from_dict(data)

    # -- validation ---------------------------------------------------------
    def validate(self) -> "Scenario":
        errors: list[str] = []
        if self.n_speakers < 1:
            errors.append("n_speakers must be >= 1")
        if not (1.0 <= self.duration_s <= 3600.0):
            errors.append("duration_s must be within [1, 3600]")
        tt = self.turn_taking
        if tt.model not in TURN_MODELS:
            errors.append(f"turn_taking.model must be one of {TURN_MODELS}")
        if not (0.0 <= tt.overlap_ratio <= 0.45):
            errors.append("turn_taking.overlap_ratio must be within [0, 0.45]")
        if not (0.0 < tt.max_overlap_fraction <= 0.5):
            errors.append("turn_taking.max_overlap_fraction must be within (0, 0.5]")
        if tt.pause_distribution not in PAUSE_DISTRIBUTIONS:
            errors.append(f"turn_taking.pause_distribution must be one of {PAUSE_DISTRIBUTIONS}")
        if not (0.0 <= tt.pause_min_s <= tt.pause_max_s):
            errors.append("turn_taking pause range must satisfy 0 <= min <= max")
        if not (1 <= tt.words_min <= tt.words_max):
            errors.append("turn_taking words range must satisfy 1 <= min <= max")
        if tt.lead_in_s < 0:
            errors.append("turn_taking.lead_in_s must be >= 0")
        if self.n_speakers == 1 and tt.overlap_ratio > 0:
            errors.append("overlap requires at least two speakers")
        dims = self.room.dims_m
        if len(dims) != 3 or any(d <= 0 for d in dims):
            errors.append("room.dims_m must be three positive lengths")
        if self.room.rt60_s is None and self.room.absorption is None:
            errors.append("room needs rt60_s or absorption")
        if self.room.absorption is not None and not (0.0 < self.room.absorption <= 1.0):
            errors.append("room.absorption must be within (0, 1]")
        if self.room.rt60_s is not None and self.room.rt60_s <= 0:
            errors.append("room.rt60_s must be positive")
        if self.device.preset not in DEVICE_PRESETS:
            errors.append(f"device.preset must be one of {sorted(DEVICE_PRESETS)}")
        if self.device.channels not in (1, 2):
            errors.append("device.channels must be 1 or 2")
        if self.device.channels == 2 and not self.export.include_stereo_ogg:
            errors.append(
                "device.channels=2 makes the stereo Ogg the primary device recording "
                "(audio.device_primary); export.include_stereo_ogg=false would drop it"
            )
        if not _inside(self.device.position_m, dims):
            errors.append("device.position_m must lie inside the room")
        if self.noise.kind not in NOISE_KINDS:
            errors.append(f"noise.kind must be one of {NOISE_KINDS}")
        if self.speakers.positions_m is not None:
            if len(self.speakers.positions_m) != self.n_speakers:
                errors.append("speakers.positions_m must have n_speakers entries")
            for p in self.speakers.positions_m:
                if not _inside(p, dims):
                    errors.append(f"speaker position {p} lies outside the room")
        if self.speakers.voices is not None and len(self.speakers.voices) != self.n_speakers:
            errors.append("speakers.voices must have n_speakers entries")
        if self.export.opus_bitrate_per_channel <= 0:
            errors.append("export.opus_bitrate_per_channel must be positive")
        if not (0 <= self.export.opus_complexity <= 10):
            errors.append("export.opus_complexity must be within [0, 10]")
        if errors:
            raise ValueError("invalid scenario: " + "; ".join(errors))
        return self


def _inside(p: Any, dims: Any, margin: float = 0.05) -> bool:
    try:
        return all(margin <= float(p[i]) <= float(dims[i]) - margin for i in range(3))
    except (TypeError, IndexError, ValueError):
        return False


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _build(cls: type, data: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise KeyError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        sub = _SUBTYPES.get((cls, name))
        if sub is not None and isinstance(value, dict):
            value = _build(sub, value)
        elif name in _TUPLE3 and value is not None:
            value = tuple(float(v) for v in value)
        elif name == "positions_m" and value is not None:
            value = [tuple(float(v) for v in p) for p in value]
        kwargs[name] = value
    return cls(**kwargs)


_SUBTYPES: dict[tuple[type, str], type] = {
    (Scenario, "turn_taking"): TurnTaking,
    (Scenario, "room"): Room,
    (Scenario, "device"): Device,
    (Scenario, "noise"): Noise,
    (Scenario, "speakers"): Speakers,
    (Scenario, "export"): Export,
}
_TUPLE3 = {"dims_m", "position_m"}


# --- presets (all HARNESS_POLICY; kept <= 60 s so tests run in seconds) -----

PRESETS: dict[str, Scenario] = {
    "smoke": Scenario(
        name="smoke",
        n_speakers=2,
        duration_s=12.0,
        room=Room(dims_m=(4.0, 3.0, 2.5), rt60_s=0.2),
        device=Device(preset="mono", position_m=(2.0, 1.5, 0.75)),
        turn_taking=TurnTaking(words_min=3, words_max=8, pause_min_s=0.2, pause_max_s=0.6),
    ),
    "default": Scenario(
        name="default",
        n_speakers=3,
        duration_s=40.0,
        turn_taking=TurnTaking(model="alternating", overlap_ratio=0.10),
        noise=Noise(kind="white", snr_db=25.0),
    ),
    "overlap_heavy": Scenario(
        name="overlap_heavy",
        n_speakers=4,
        duration_s=60.0,
        turn_taking=TurnTaking(model="random", overlap_ratio=0.30, pause_min_s=0.1, pause_max_s=0.6),
        room=Room(dims_m=(7.0, 6.0, 3.0), rt60_s=0.4),
    ),
    "notepin_s_noisy": Scenario(
        name="notepin_s_noisy",
        n_speakers=2,
        duration_s=45.0,
        device=Device(preset="notepin_s_2mic", position_m=(3.0, 2.5, 1.0), channels=2),
        turn_taking=TurnTaking(model="alternating", overlap_ratio=0.05),
        noise=Noise(kind="white", snr_db=10.0),
    ),
    "single_speaker": Scenario(
        name="single_speaker",
        n_speakers=1,
        duration_s=30.0,
        device=Device(preset="mono", position_m=(3.0, 2.5, 0.75)),
        turn_taking=TurnTaking(pause_min_s=0.4, pause_max_s=1.5),
    ),
    # Real, intelligible speech (generator/tts/piper.py; needs the local-only
    # piper-tts package and data/voices/piper). Voices come from the piper
    # palette (PiperBackend.assign_voices). Longer piper meetings override
    # duration_s and friends on the command line (docs/generator.md section 6).
    "piper_smoke": Scenario(
        name="piper_smoke",
        n_speakers=2,
        duration_s=25.0,
        tts_backend="piper",
        room=Room(dims_m=(4.0, 3.0, 2.5), rt60_s=0.2),
        device=Device(preset="mono", position_m=(2.0, 1.5, 0.75)),
        turn_taking=TurnTaking(overlap_ratio=0.1, words_min=4, words_max=10, pause_min_s=0.2, pause_max_s=0.6),
    ),
    "piper_meeting": Scenario(
        name="piper_meeting",
        n_speakers=2,
        duration_s=60.0,
        tts_backend="piper",
        turn_taking=TurnTaking(model="alternating", overlap_ratio=0.0),
    ),
}


def load_scenario(source: str | Path | dict[str, Any] | Scenario, overrides: dict[str, Any] | None = None) -> Scenario:
    """Accept a preset name, a .yaml/.yml/.json path, a dict or a Scenario."""
    if isinstance(source, Scenario):
        scenario = source
    elif isinstance(source, dict):
        scenario = Scenario.from_dict(source)
    else:
        text = str(source)
        if text in PRESETS:
            scenario = Scenario.from_dict(PRESETS[text].to_dict())
        else:
            path = Path(text)
            if not path.is_file():
                raise FileNotFoundError(f"scenario {text!r}: not a preset ({sorted(PRESETS)}) and not a file")
            raw = path.read_text(encoding="utf-8")
            if path.suffix.lower() in (".yaml", ".yml"):
                import yaml

                data = yaml.safe_load(raw)
            else:
                data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"scenario file {path} must contain a mapping")
            scenario = Scenario.from_dict(data)
    if overrides:
        scenario = scenario.with_overrides(overrides)
    return scenario.validate()


def parse_override(text: str) -> tuple[str, Any]:
    """'key.path=value' -> (key, parsed value) using YAML scalar rules."""
    import yaml

    if "=" not in text:
        raise ValueError(f"override must look like key=value: {text!r}")
    key, _, raw = text.partition("=")
    return key.strip(), yaml.safe_load(raw)


__all__ = [
    "DEFAULT_MAX_ORDER_CAP",
    "Device",
    "Export",
    "Noise",
    "PRESETS",
    "Room",
    "Scenario",
    "Speakers",
    "TurnTaking",
    "load_scenario",
    "parse_override",
]
