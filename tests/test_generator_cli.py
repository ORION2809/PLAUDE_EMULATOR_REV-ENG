"""python -m generator: make / batch / presets / backends / validate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from generator.cli import main
from generator.contract import load_meeting


def run_module(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "generator", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_make_and_validate_in_process(tmp_path: Path, capsys) -> None:
    out = tmp_path / "m"
    assert main(["make", "--scenario", "smoke", "--out", str(out), "--seed", "5", "--set", "duration_s=5", "--set", "export.include_stereo_ogg=false"]) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["meeting_id"] == "synth-smoke-s0005" and summary["duration_s"] == 5.0
    assert "ogg_opus_stereo" not in summary["device_outputs"]
    m = load_meeting(out)
    assert m["generator"]["seed"] == 5
    assert main(["validate", str(out)]) == 0
    # break the RTTM and the validator must notice
    rttm = out / "ref.rttm"
    lines = rttm.read_text().splitlines()
    lines[0] = lines[0].replace("spk0", "spk9")
    rttm.write_text("\n".join(lines) + "\n")
    assert main(["validate", str(out)]) == 1


def test_batch_writes_consecutive_seeds_and_an_index(tmp_path: Path, capsys) -> None:
    out = tmp_path / "set"
    assert main(["batch", "--n", "2", "--scenario", "smoke", "--seed", "40", "--out", str(out), "--set", "duration_s=4", "--set", "export.include_stereo_ogg=false", "--set", "export.include_g4=false"]) == 0
    index = json.loads((out / "index.json").read_text())
    assert [m["seed"] for m in index["meetings"]] == [40, 41]
    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert dirs == ["synth-smoke-s0040", "synth-smoke-s0041"]
    a = load_meeting(out / dirs[0])
    b = load_meeting(out / dirs[1])
    assert a["segments"] != b["segments"], "different seeds, different meetings"


def test_presets_and_backends_commands(capsys) -> None:
    assert main(["presets"]) == 0
    text = capsys.readouterr().out
    assert "smoke" in text and "note_pro_4mic" in text
    assert main(["presets", "--yaml"]) == 0
    assert "n_speakers:" in capsys.readouterr().out
    assert main(["backends"]) == 0
    text = capsys.readouterr().out
    assert "formant  available" in text and "piper" in text and "kokoro" in text


def test_module_entry_point_runs_as_a_subprocess(tmp_path: Path) -> None:
    proc = run_module("make", "--scenario", "smoke", "--out", str(tmp_path / "sub"), "--seed", "2", "--set", "duration_s=4", "--set", "export.include_stereo_ogg=false")
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["meeting_id"] == "synth-smoke-s0002"
    assert (tmp_path / "sub" / "device" / "recording.ogg").is_file()
    bad = run_module("make", "--scenario", "nope", "--out", str(tmp_path / "x"))
    assert bad.returncode != 0
