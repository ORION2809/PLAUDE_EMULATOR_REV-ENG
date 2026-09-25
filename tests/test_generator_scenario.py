"""Scenario loading (presets, YAML, JSON, overrides) and validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.scenario import PRESETS, Scenario, load_scenario, parse_override


def test_presets_validate_and_stay_small() -> None:
    assert {"smoke", "default", "overlap_heavy", "notepin_s_noisy", "single_speaker"} <= set(PRESETS)
    for name, sc in PRESETS.items():
        assert sc.validate() is sc
        assert sc.duration_s <= 60.0, f"{name} must stay small for CI"
        assert sc.name == name


def test_round_trip_through_dict_yaml_and_json(tmp_path: Path) -> None:
    sc = PRESETS["notepin_s_noisy"]
    assert Scenario.from_dict(sc.to_dict()) == sc
    y = tmp_path / "s.yaml"
    y.write_text(sc.to_yaml())
    j = tmp_path / "s.json"
    j.write_text(json.dumps(sc.to_dict()))
    assert load_scenario(y) == sc
    assert load_scenario(j) == sc


def test_dotted_overrides_and_unknown_keys() -> None:
    sc = load_scenario("smoke", {"seed": 9, "turn_taking.overlap_ratio": 0.2, "device.preset": "notepin_s_2mic", "room.dims_m": [5, 4, 3]})
    assert sc.seed == 9 and sc.turn_taking.overlap_ratio == 0.2 and sc.device.preset == "notepin_s_2mic"
    assert sc.room.dims_m == (5.0, 4.0, 3.0)
    with pytest.raises(KeyError):
        load_scenario("smoke", {"turn_taking.bogus": 1})
    with pytest.raises(KeyError):
        Scenario.from_dict({"nonsense": 1})
    assert parse_override("noise.snr_db=12.5") == ("noise.snr_db", 12.5)
    assert parse_override("noise.kind=white") == ("noise.kind", "white")
    with pytest.raises(ValueError):
        parse_override("no-equals")
    with pytest.raises(FileNotFoundError):
        load_scenario("not-a-preset-or-file")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"device.channels": 3}, "device.channels"),
        ({"n_speakers": 1, "turn_taking.overlap_ratio": 0.1}, "overlap requires"),
        ({"turn_taking.overlap_ratio": 0.9}, "overlap_ratio"),
        ({"device.position_m": [9, 9, 9]}, "inside the room"),
        ({"speakers.positions_m": [[1, 1, 1]]}, "n_speakers entries"),
        ({"noise.kind": "pink"}, "noise.kind"),
        ({"device.preset": "note_ultra"}, "device.preset"),
        ({"export.opus_complexity": 11}, "opus_complexity"),
        ({"turn_taking.words_min": 20}, "words range"),
    ],
)
def test_validation_rejects_bad_scenarios(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_scenario("smoke", overrides)
