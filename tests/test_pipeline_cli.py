"""python -m pipeline: run / batch / import-stm / list, as a subprocess.

Every ``run`` must leave hyp.json + hyp.rttm + hyp.stm that parse back
with this package's readers AND with meeteval / pyannote.database, and
the oracle's files must score DER 0 against the meeting's own ref.rttm.
Batch must isolate one bad meeting, report it, and still finish.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.cli import main, parse_param
from pipeline.formats import parse_rttm, parse_stm, read_hypothesis
from pipeline.meeting import read_meeting
from pipeline.synthetic import synthetic_meeting


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "pipeline", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300
    )


@pytest.fixture(scope="module")
def meeting(tmp_path_factory):
    return synthetic_meeting(tmp_path_factory.mktemp("cli"), meeting_id="cli1", write_device_ogg=True)


def _der_from_files(ref_rttm: Path, hyp_rttm: Path) -> float:
    from pyannote.database.util import load_rttm
    from pyannote.metrics.diarization import DiarizationErrorRate

    ref = load_rttm(str(ref_rttm))
    hyp = load_rttm(str(hyp_rttm))
    assert set(ref) == set(hyp), (set(ref), set(hyp))
    (uri,) = ref
    return float(DiarizationErrorRate(collar=0.0)(ref[uri], hyp[uri]))


def test_run_oracle_writes_three_files_that_parse_back(meeting, tmp_path):
    m, d = meeting
    out = tmp_path / "o" / "hyp.json"
    r = run_cli("run", "--pipeline", "oracle", "--audio", str(d / "mix.wav"), "--meeting-dir", str(d), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "HARNESS SELF-TEST" in r.stdout
    hyp = read_hypothesis(out)
    assert hyp.meeting_id == "cli1" and hyp.segments == m["segments"]
    rttm = parse_rttm(out.with_suffix(".rttm").read_text())
    stm = parse_stm(out.with_suffix(".stm").read_text())
    assert len(rttm) == len(stm) == len(m["segments"])
    assert [s["text"] for s in stm] == [s["text"] for s in m["segments"]]
    # public readers
    from meeteval.io.stm import STM

    assert len(STM.load(str(out.with_suffix(".stm"))).lines) == len(m["segments"])
    assert _der_from_files(d / "ref.rttm", out.with_suffix(".rttm")) == 0.0


def test_run_energy_vad_on_device_ogg(meeting, tmp_path):
    m, d = meeting
    out = tmp_path / "e" / "hyp.json"
    r = run_cli(
        "run", "--pipeline", "energy-vad-cluster", "--audio", str(d / "device" / "recording.ogg"),
        "--out", str(out), "--param", "num_speakers=2",
    )
    assert r.returncode == 0, r.stderr
    assert "HARNESS SELF-TEST" not in r.stdout
    hyp = read_hypothesis(out)
    assert hyp.meeting_id == "cli1"  # discovered two levels up from device/recording.ogg
    assert hyp.extra["audio"]["container"] == "pyav" and hyp.extra["audio"]["source_sample_rate"] == 48000
    assert hyp.segments and all(s["text"] == "" for s in hyp.segments)
    assert len(hyp.speakers) == 2
    stm_lines = out.with_suffix(".stm").read_text().splitlines()
    assert all(len(l.split()) == 5 for l in stm_lines)  # empty transcripts, no trailing junk
    from meeteval.io.stm import STM

    assert all(l.transcript == "" for l in STM.load(str(out.with_suffix(".stm"))).lines)
    assert _der_from_files(d / "ref.rttm", out.with_suffix(".rttm")) < 0.15


def test_batch_writes_index_and_per_meeting_outputs(tmp_path):
    root = tmp_path / "root"
    synthetic_meeting(root / "a", meeting_id="mA")
    synthetic_meeting(root / "b", meeting_id="mB", layout=[(0.5, 1.5, "spk0"), (2.0, 3.5, "spk1")])
    (root / "not-a-meeting").mkdir()
    out = tmp_path / "hyps"
    r = run_cli("batch", "--pipeline", "perturbed-oracle", "--root", str(root), "--out", str(out), "--param", "word_del_rate=1.0", "--seed", "5")
    assert r.returncode == 0, r.stderr
    idx = json.loads((out / "batch.json").read_text())
    assert idx["schema"] == "plaud-harness/batch/1" and idx["ok"] == 2 and idx["failed"] == 0
    assert idx["system_under_test"] is False and idx["config"]["params"] == {"word_del_rate": 1.0} and idx["config"]["seed"] == 5
    assert [row["meeting_id"] for row in idx["meetings"]] == ["mA", "mB"]
    for mid in ("mA", "mB"):
        for suffix in (".json", ".rttm", ".stm"):
            assert (out / mid / f"hyp{suffix}").is_file()
        hyp = read_hypothesis(out / mid / "hyp.json")
        assert hyp.meeting_id == mid and all(s["text"] == "" for s in hyp.segments)


def test_batch_isolates_a_broken_meeting(tmp_path):
    root = tmp_path / "root"
    synthetic_meeting(root / "good", meeting_id="good")
    _, bad = synthetic_meeting(root / "bad", meeting_id="bad")
    (bad / "mix.wav").unlink()
    out = tmp_path / "hyps"
    r = run_cli("batch", "--pipeline", "oracle", "--root", str(root), "--out", str(out))
    assert r.returncode == 1
    idx = json.loads((out / "batch.json").read_text())
    assert idx["ok"] == 1 and idx["failed"] == 1
    rows = {row["meeting_id"]: row for row in idx["meetings"]}
    assert rows["bad"]["status"] == "error" and "FileNotFoundError" in rows["bad"]["error"]
    assert rows["good"]["status"] == "ok" and (out / "good" / "hyp.stm").is_file()
    assert not (out / "bad").exists()


def test_batch_with_device_audio_key(meeting, tmp_path):
    m, d = meeting
    out = tmp_path / "dev"
    r = run_cli("batch", "--pipeline", "energy-vad-cluster", "--root", str(d), "--out", str(out), "--audio-key", "device")
    assert r.returncode == 0, r.stderr
    idx = json.loads((out / "batch.json").read_text())
    assert idx["meetings"][0]["audio"].endswith("device/recording.ogg")


def test_usage_errors_exit_2(tmp_path, meeting):
    _, d = meeting
    assert run_cli("run", "--pipeline", "nope", "--audio", str(d / "mix.wav"), "--out", str(tmp_path / "h.json")).returncode == 2
    assert run_cli("run", "--pipeline", "oracle", "--audio", str(tmp_path / "missing.wav"), "--out", str(tmp_path / "h.json")).returncode == 2
    assert run_cli("batch", "--pipeline", "oracle", "--root", str(tmp_path / "empty"), "--out", str(tmp_path / "o")).returncode == 2
    assert run_cli("run").returncode == 2
    assert run_cli("run", "--pipeline", "oracle", "--audio", str(d / "mix.wav"), "--out", str(tmp_path / "h.json"), "--param", "novalue").returncode == 2


def test_unavailable_adapter_exits_2_with_reason(meeting, tmp_path):
    from pipeline import registered

    if registered()["faster-whisper"].available():
        pytest.skip("faster-whisper is importable here")
    _, d = meeting
    r = run_cli("run", "--pipeline", "faster-whisper", "--audio", str(d / "mix.wav"), "--out", str(tmp_path / "h.json"))
    assert r.returncode == 2 and "unavailable" in r.stderr and "faster_whisper" in r.stderr


def test_oracle_without_meeting_json_exits_1(tmp_path, meeting):
    _, d = meeting
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "x.wav").write_bytes((d / "mix.wav").read_bytes())
    r = run_cli("run", "--pipeline", "oracle", "--audio", str(lone / "x.wav"), "--out", str(tmp_path / "h.json"))
    assert r.returncode == 1 and "no meeting.json" in r.stderr


def test_import_stm_builds_a_meeting_dir_the_oracle_reproduces(tmp_path, meeting):
    _, d = meeting
    stm = tmp_path / "ref.stm"
    stm.write_text("imp1 1 alice 0.50 2.00 one two three\nimp1 1 bob 3.00 4.50 four five\n")
    rttm = tmp_path / "ref.rttm"
    rttm.write_text("SPEAKER imp1 1 0.500 1.500 <NA> <NA> alice <NA> <NA>\nSPEAKER imp1 1 3.000 1.500 <NA> <NA> bob <NA> <NA>\n")
    out = tmp_path / "corpus" / "imp1"
    r = run_cli("import-stm", "--stm", str(stm), "--rttm", str(rttm), "--audio", str(d / "mix.wav"), "--out", str(out))
    assert r.returncode == 0, r.stderr
    m = read_meeting(out)
    assert m["meeting_id"] == "imp1" and [s["id"] for s in m["speakers"]] == ["alice", "bob"]
    assert m["duration_s"] == pytest.approx(9.0, abs=0.01) and (out / "mix.wav").is_file()
    assert (out / "ref.rttm").read_text() == rttm.read_text()
    assert [l.split(maxsplit=5)[5] for l in (out / "ref.stm").read_text().splitlines()] == ["one two three", "four five"]
    hyp_out = tmp_path / "h" / "hyp.json"
    assert run_cli("run", "--pipeline", "oracle", "--audio", str(out / "mix.wav"), "--out", str(hyp_out)).returncode == 0
    assert [s["text"] for s in read_hypothesis(hyp_out).segments] == ["one two three", "four five"]


def test_list_json_and_text():
    r = run_cli("list", "--json")
    assert r.returncode == 0
    rows = {row["name"]: row for row in json.loads(r.stdout)}
    assert rows["oracle"]["system_under_test"] is False and rows["energy-vad-cluster"]["available"] is True
    r2 = run_cli("list")
    assert "oracle" in r2.stdout and "self-test" in r2.stdout


def test_parse_param_coercion():
    assert parse_param("k=1") == ("k", 1)
    assert parse_param("k=1.5") == ("k", 1.5)
    assert parse_param("k=true") == ("k", True)
    assert parse_param("k=abc") == ("k", "abc")
    assert parse_param("k=") == ("k", "")
    assert parse_param('k=[1,2]') == ("k", [1, 2])
    with pytest.raises(Exception):
        parse_param("novalue")


def test_main_in_process_matches_subprocess_exit_codes(tmp_path):
    assert main(["run", "--pipeline", "nope", "--audio", "x", "--out", "y"]) == 2
    assert main(["list"]) == 0
