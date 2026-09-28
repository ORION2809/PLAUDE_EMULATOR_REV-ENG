"""Layer 3 CLI: score / batch / validate / selfcheck / suites, exit codes, reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))

from evals.cli import find_hypothesis, main, selfcheck_meeting  # noqa: E402
from evals.io import Hypothesis, Meeting, Segment, load_hypothesis, write_hypothesis_files, write_meeting, write_reference_files  # noqa: E402


def make_meeting_dir(root: Path, meeting_id: str = "selfcheck-0001") -> tuple[Path, "Meeting"]:  # noqa: F821
    m = selfcheck_meeting()
    m.meeting_id = meeting_id
    d = root / "refs" / meeting_id
    write_meeting(m, d)
    write_reference_files(m, d)
    return d, m


def degraded(m) -> Hypothesis:
    """Second segment attributed to the wrong speaker; text unchanged."""
    segs = list(m.segments)
    s = segs[1]
    segs[1] = Segment("spk0", s.start, s.end, s.text, s.words)
    return Hypothesis(m.meeting_id, "degraded", segs)


def test_score_oracle_passes_and_writes_reports(tmp_path: Path, capsys) -> None:
    d, m = make_meeting_dir(tmp_path)
    write_hypothesis_files(Hypothesis(m.meeting_id, "oracle", list(m.segments)), tmp_path / "hyps" / m.meeting_id)
    rc = main(
        ["score", "--ref", str(d), "--hyp", str(tmp_path / "hyps" / m.meeting_id / "hyp.json"),
         "--suite", "oracle", "--report", str(tmp_path / "out" / "r.json"), "--md", str(tmp_path / "out" / "r.md")]
    )
    assert rc == 0
    doc = json.loads((tmp_path / "out" / "r.json").read_text())
    assert doc["schema"] == "plaud-harness/eval-report/1" and doc["kind"] == "meeting"
    assert doc["report"]["der"]["der"] == 0.0 and doc["report"]["cpwer"]["error_rate"] == 0.0
    assert doc["gates"]["passed"] is True and doc["gates"]["suite"] == "oracle"
    assert doc["libraries"]["meeteval"] and doc["libraries"]["pyannote.metrics"]
    md = (tmp_path / "out" / "r.md").read_text()
    assert md.startswith(f"# {m.meeting_id} — oracle") and "[PASS] suite 'oracle'" in md
    out = capsys.readouterr().out
    assert "cpWER 0.0000" in out and "[PASS]" in out


def test_score_degraded_fails_named_gates(tmp_path: Path, capsys) -> None:
    d, m = make_meeting_dir(tmp_path)
    hyp_dir = tmp_path / "hyps" / m.meeting_id
    write_hypothesis_files(degraded(m), hyp_dir)
    rc = main(["score", "--ref", str(d), "--hyp", str(hyp_dir), "--suite", "synthetic-clean", "--report", str(tmp_path / "r.json")])
    assert rc == 1
    doc = json.loads((tmp_path / "r.json").read_text())
    assert doc["gates"]["passed"] is False
    assert "der.der" in doc["gates"]["failed_metrics"] and "cpwer.error_rate" in doc["gates"]["failed_metrics"]
    assert "speaker_count.abs_error" not in doc["gates"]["failed_metrics"]
    # the wrong speaker for a 1.6 s segment, minus a 0.25 s collar, is 1.35 s of confusion
    assert doc["report"]["der"]["confusion"] == pytest.approx(1.35)
    assert doc["report"]["wer_concat"]["wer"] == 0.0  # words are all there, just misattributed
    assert "[FAIL] suite 'synthetic-clean'" in capsys.readouterr().out
    # without a suite the same run exits 0 (scoring succeeded)
    assert main(["score", "--ref", str(d), "--hyp", str(hyp_dir), "--quiet"]) == 0


def test_score_input_errors_exit_2(tmp_path: Path, capsys) -> None:
    d, m = make_meeting_dir(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "plaud-harness/hypothesis/1", "meeting_id": m.meeting_id, "system": "s", "segments": [{"speaker": "a", "start": 2, "end": 1, "text": "x"}]}))
    assert main(["score", "--ref", str(d), "--hyp", str(bad)]) == 2
    assert "must be strictly after start" in capsys.readouterr().err
    other = tmp_path / "other.json"
    other.write_text(json.dumps(Hypothesis("someone-else", "s", []).to_dict()))
    assert main(["score", "--ref", str(d), "--hyp", str(other)]) == 2
    assert "does not match" in capsys.readouterr().err
    assert main(["score", "--ref", str(tmp_path / "missing"), "--hyp", str(other)]) == 2
    assert main(["score", "--ref", str(d), "--hyp", str(bad), "--suite", "nope"]) == 2


def test_score_metric_options_change_the_settings(tmp_path: Path) -> None:
    d, m = make_meeting_dir(tmp_path)
    hyp_dir = tmp_path / "hyps" / m.meeting_id
    write_hypothesis_files(degraded(m), hyp_dir)
    rc = main(["score", "--ref", str(d), "--hyp", str(hyp_dir), "--der-collar", "0", "--tcp-collar", "1", "--skip-overlap",
               "--expand-contractions", "--numbers-to-words", "--report", str(tmp_path / "r.json"), "--quiet"])
    assert rc == 0
    s = json.loads((tmp_path / "r.json").read_text())["report"]["settings"]
    assert s["der_collar"] == 0.0 and s["tcp_collar"] == 1.0 and s["skip_overlap"] is True
    assert s["normalizer"]["expand_contractions"] is True and s["normalizer"]["numbers_to_words"] is True
    rep = json.loads((tmp_path / "r.json").read_text())["report"]
    assert rep["der"]["confusion"] == pytest.approx(1.6)  # no collar now


def test_batch_pairs_meetings_reports_missing_and_aggregates(tmp_path: Path, capsys) -> None:
    d1, m1 = make_meeting_dir(tmp_path, "mtg-a")
    d2, m2 = make_meeting_dir(tmp_path, "mtg-b")
    d3, m3 = make_meeting_dir(tmp_path, "mtg-c")
    write_hypothesis_files(Hypothesis(m1.meeting_id, "sys", list(m1.segments)), tmp_path / "hyps" / "mtg-a")
    # alternative lookup: <hyps>/<meeting_id>.json
    (tmp_path / "hyps").mkdir(exist_ok=True)
    (tmp_path / "hyps" / "mtg-b.json").write_text(json.dumps(degraded(m2).to_dict()))
    # mtg-c has no hypothesis
    args = ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"), "--report", str(tmp_path / "b.json"), "--md", str(tmp_path / "b.md")]
    assert main(args) == 2  # default --missing fail
    assert "missing hypothesis: mtg-c" in capsys.readouterr().err
    assert main(args + ["--missing", "skip"]) == 0
    doc = json.loads((tmp_path / "b.json").read_text())
    assert doc["kind"] == "batch" and doc["n_meetings"] == 2 and doc["gates"] is None
    assert [x["meeting_id"] for x in doc["missing"]] == ["mtg-c"]
    ids = [r["meeting_id"] for r in doc["meetings"]]
    assert ids == ["mtg-a", "mtg-b"]
    a, b = doc["meetings"]
    assert a["der"]["der"] == 0.0 and b["der"]["der"] > 0
    assert doc["macro"]["der.der"] == pytest.approx(b["der"]["der"] / 2)
    pooled = (b["der"]["miss"] + b["der"]["false_alarm"] + b["der"]["confusion"]) / (a["der"]["total"] + b["der"]["total"])
    assert doc["micro"]["der.der"] == pytest.approx(pooled)
    md = (tmp_path / "b.md").read_text()
    assert "| mtg-a | sys |" in md and "| **macro** |" in md and "| **micro** |" in md and "mtg-c" in md


def test_batch_gates_apply_to_the_chosen_aggregate(tmp_path: Path, capsys) -> None:
    d1, m1 = make_meeting_dir(tmp_path, "mtg-a")
    d2, m2 = make_meeting_dir(tmp_path, "mtg-b")
    write_hypothesis_files(Hypothesis(m1.meeting_id, "sys", list(m1.segments)), tmp_path / "hyps" / "mtg-a")
    write_hypothesis_files(degraded(m2), tmp_path / "hyps" / "mtg-b")
    base = ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"), "--suite", "oracle", "--report", str(tmp_path / "b.json")]
    assert main(base) == 1  # macro is not perfect
    doc = json.loads((tmp_path / "b.json").read_text())
    assert [g["target"] for g in doc["gates"]] == ["macro"] and doc["gates"][0]["passed"] is False
    assert main(base + ["--gate-on", "each"]) == 1
    doc = json.loads((tmp_path / "b.json").read_text())
    assert {g["target"]: g["passed"] for g in doc["gates"]} == {"mtg-a": True, "mtg-b": False}
    assert main(base + ["--gate-on", "both", "--quiet"]) == 1
    doc = json.loads((tmp_path / "b.json").read_text())
    assert [g["target"] for g in doc["gates"]] == ["macro", "micro"]
    # no shipped suite tolerates DER 0.2045 / cpWER 0.4, so a custom gates file
    # (also exercising --gates) that does must make every target pass -> exit 0
    custom = tmp_path / "lenient.yaml"
    custom.write_text(
        "schema: plaud-harness/gates/1\nsuites:\n  lenient:\n    gates:\n"
        "      - {metric: der.der, max: 0.5}\n      - {metric: cpwer.error_rate, max: 0.5}\n"
    )
    lenient = [a if a != "oracle" else "lenient" for a in base] + ["--gates", str(custom)]
    assert main(lenient + ["--gate-on", "each", "--quiet"]) == 0
    doc = json.loads((tmp_path / "b.json").read_text())
    assert all(g["passed"] for g in doc["gates"]) and len(doc["gates"]) == 2


def test_batch_skips_cleanly_when_the_corpus_is_absent(tmp_path: Path, capsys) -> None:
    # a path under tmp_path: the real data/corpora/ami/meetings exists on a machine that ran evals.ami
    rc = main(["batch", "--refs", str(tmp_path / "data" / "corpora" / "ami" / "meetings"), "--hyps", str(tmp_path / "out")])
    assert rc == 3
    err = capsys.readouterr().err
    assert "skipped" in err and "does not exist" in err
    assert "expected layout" in err and "data/corpora/ami/meetings/<MEETING_ID>/meeting.json" in err
    assert "python -m evals batch --refs data/corpora/ami/meetings" in err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["batch", "--refs", str(empty), "--hyps", str(tmp_path / "out")]) == 3
    assert "no meeting.json found" in capsys.readouterr().err


def test_batch_reports_a_malformed_meeting_as_an_error(tmp_path: Path, capsys) -> None:
    d, m = make_meeting_dir(tmp_path, "mtg-a")
    write_hypothesis_files(Hypothesis(m.meeting_id, "sys", list(m.segments)), tmp_path / "hyps" / "mtg-a")
    broken = tmp_path / "refs" / "mtg-broken"
    broken.mkdir()
    (broken / "meeting.json").write_text("{}")
    rc = main(["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"), "--report", str(tmp_path / "b.json")])
    assert rc == 2
    doc = json.loads((tmp_path / "b.json").read_text())
    assert doc["n_meetings"] == 1 and "missing required key 'schema'" in doc["errors"][0]["error"]


def test_find_hypothesis_lookup_order(tmp_path: Path) -> None:
    refs, hyps = tmp_path / "refs", tmp_path / "hyps"
    mdir = refs / "sub" / "dirname"
    mdir.mkdir(parents=True)
    assert find_hypothesis(hyps, refs, mdir, "mid") is None
    for rel in ["mid.json", "mid.hyp.json", "mid/hyp.json", "dirname/hyp.json", "sub/dirname/hyp.json"]:
        p = hyps / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        assert find_hypothesis(hyps, refs, mdir, "mid") == p  # each later one takes precedence


def test_validate_and_suites_commands(tmp_path: Path, capsys) -> None:
    d, m = make_meeting_dir(tmp_path)
    hyp_dir = tmp_path / "hyps" / m.meeting_id
    write_hypothesis_files(Hypothesis(m.meeting_id, "sys", list(m.segments)), hyp_dir)
    assert main(["validate", "--ref", str(d), "--hyp", str(hyp_dir / "hyp.json")]) == 0
    out = capsys.readouterr().out
    assert "ok:" in out and "4 segments" in out
    (tmp_path / "bad.json").write_text("[]")
    assert main(["validate", "--hyp", str(tmp_path / "bad.json")]) == 2
    assert "top level must be an object" in capsys.readouterr().err
    assert main(["validate"]) == 2
    assert main(["suites"]) == 0
    out = capsys.readouterr().out
    assert "oracle:" in out and "cpwer.error_rate == 0.0" in out
    assert main(["suites", "--gates", str(tmp_path / "none.yaml")]) == 2


def test_selfcheck_passes_in_process_and_via_python_m(tmp_path: Path) -> None:
    assert main(["selfcheck", "--report", str(tmp_path / "s.json")]) == 0
    doc = json.loads((tmp_path / "s.json").read_text())
    assert doc["gates"]["passed"] is True and doc["report"]["tcpwer"]["word_level_timing"] is True
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-m", "evals", "selfcheck"], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert "[PASS] suite 'oracle'" in proc.stdout
    proc = subprocess.run([sys.executable, "-m", "evals", "--version"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and proc.stdout.startswith("evals ")


def test_selfcheck_meeting_is_a_valid_contract_meeting(tmp_path: Path) -> None:
    m = selfcheck_meeting()
    write_meeting(m, tmp_path)
    from evals.io import load_meeting

    back = load_meeting(tmp_path)
    assert back.to_dict() == m.to_dict()
    assert all(s.words for s in back.segments)
    assert load_hypothesis(write_hypothesis_files(Hypothesis(m.meeting_id, "x", list(m.segments)), tmp_path / "h")[0]).segments == m.segments


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-25): exit codes mean what docs/evals.md says
#   0 ok, 1 a gate failed, 2 input error (or anything unexpected), 3 nothing to score
# --------------------------------------------------------------------------- #


def quiet_meeting(root: Path, meeting_id: str = "quiet") -> Path:
    """A valid meeting with no reference speech at all."""
    m = Meeting(meeting_id, 16000, 10.0, 1, [{"id": "spk0"}], [], {"mix_wav": "mix.wav", "stems": {}, "device": {}}, {"name": "hand"})
    d = root / "refs" / meeting_id
    write_meeting(m, d)
    write_hypothesis_files(Hypothesis(meeting_id, "sys", []), root / "hyps" / meeting_id)
    return d


def test_batch_survives_a_meeting_without_reference_speech(tmp_path: Path, capsys) -> None:
    """EV-2.  Used to die with ZeroDivisionError (exit 1, no report)."""
    d, m = make_meeting_dir(tmp_path, "good")
    write_hypothesis_files(Hypothesis(m.meeting_id, "sys", list(m.segments)), tmp_path / "hyps" / "good")
    quiet_meeting(tmp_path)
    base = ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"), "--suite", "synthetic-clean",
            "--report", str(tmp_path / "b.json"), "--quiet"]
    assert main(base) == 0  # macro: the undefined rates are left out of the mean, and counted
    doc = json.loads((tmp_path / "b.json").read_text())
    assert doc["n_meetings"] == 2 and doc["errors"] == []
    quiet = next(r for r in doc["meetings"] if r["meeting_id"] == "quiet")
    assert quiet["der"]["der"] is None and quiet["jer"]["jer"] is None
    assert doc["macro"]["counts"]["der.der"] == 1
    assert main(base + ["--gate-on", "each"]) == 1  # per meeting the undefined numbers fail closed
    doc = json.loads((tmp_path / "b.json").read_text())
    assert {g["target"]: g["passed"] for g in doc["gates"]} == {"good": True, "quiet": False}


def test_score_on_a_meeting_without_reference_speech_fails_its_gate_closed(tmp_path: Path) -> None:
    d = quiet_meeting(tmp_path)
    hyp = tmp_path / "hyps" / "quiet" / "hyp.json"
    assert main(["score", "--ref", str(d), "--hyp", str(hyp), "--quiet", "--report", str(tmp_path / "r.json")]) == 0
    assert main(["score", "--ref", str(d), "--hyp", str(hyp), "--quiet", "--suite", "synthetic-clean"]) == 1


def test_selfcheck_with_a_bad_gates_path_is_an_input_error(tmp_path: Path, capsys) -> None:
    """EV-5(a).  Was a GateConfigError traceback and exit 1 (= 'gate failed')."""
    assert main(["selfcheck", "--gates", str(tmp_path / "nope.yaml")]) == 2
    assert "file not found" in capsys.readouterr().err


def test_score_with_a_non_utf8_hypothesis_is_an_input_error(tmp_path: Path, capsys) -> None:
    """EV-5(b).  Was a UnicodeDecodeError traceback and exit 1."""
    d, m = make_meeting_dir(tmp_path)
    bad = tmp_path / "latin1.json"
    bad.write_bytes(json.dumps(Hypothesis(m.meeting_id, "caf\u00e9", list(m.segments)).to_dict(), ensure_ascii=False).encode("latin-1"))
    assert main(["score", "--ref", str(d), "--hyp", str(bad), "--suite", "oracle"]) == 2
    assert "not valid UTF-8" in capsys.readouterr().err


def test_batch_rejects_an_unknown_suite_before_looking_for_the_corpus(tmp_path: Path, capsys) -> None:
    """EV-5(c).  A mistyped --suite on the (absent) AMI command exited 3 and hid the typo."""
    rc = main(["batch", "--refs", str(tmp_path / "does-not-exist"), "--hyps", str(tmp_path / "x"), "--suite", "typo-suite"])
    assert rc == 2
    assert "unknown suite 'typo-suite'" in capsys.readouterr().err
    assert main(["batch", "--refs", str(tmp_path / "does-not-exist"), "--hyps", str(tmp_path / "x"), "--suite", "oracle"]) == 3


def test_batch_where_no_meeting_has_a_hypothesis_is_an_input_error_even_with_skip(tmp_path: Path, capsys) -> None:
    """EV-5(d).  A pipeline that produced nothing must not look like 'skipped'."""
    make_meeting_dir(tmp_path, "mtg-a")
    make_meeting_dir(tmp_path, "mtg-b")
    (tmp_path / "hyps").mkdir()
    args = ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"), "--suite", "oracle", "--report", str(tmp_path / "b.json")]
    assert main(args + ["--missing", "skip"]) == 2
    assert "none of the 2 meeting(s) has a hypothesis" in capsys.readouterr().err
    assert json.loads((tmp_path / "b.json").read_text())["n_meetings"] == 0
    assert main(args) == 2


@pytest.mark.parametrize("flag", ["--der-collar", "--tcp-collar"])
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc"])
def test_negative_or_non_finite_collars_are_refused_by_every_command(tmp_path: Path, capsys, flag: str, value: str) -> None:
    """EV-5(e).  pyannote reads a negative collar as 0 while the report recorded
    the negative value; meeteval's own CLI refuses x < 0."""
    d, m = make_meeting_dir(tmp_path)
    write_hypothesis_files(Hypothesis(m.meeting_id, "oracle", list(m.segments)), tmp_path / "h")
    for argv in (
        ["selfcheck", flag, value],
        ["score", "--ref", str(d), "--hyp", str(tmp_path / "h"), flag, value],
        ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "h"), flag, value],
    ):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2, argv
        assert "finite number >= 0" in capsys.readouterr().err
    assert main(["selfcheck", flag, "0"]) == 0  # zero is a legitimate collar


def test_an_unexpected_exception_exits_2_never_1(tmp_path: Path, capsys, monkeypatch) -> None:
    """EV-5(f).  Exit 1 is reserved for 'a gate failed'."""
    import evals.cli as cli

    d, m = make_meeting_dir(tmp_path)
    hyps = tmp_path / "hyps"
    write_hypothesis_files(Hypothesis(m.meeting_id, "oracle", list(m.segments)), hyps / m.meeting_id)

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(cli, "score_meeting", boom)
    assert main(["score", "--ref", str(d), "--hyp", str(hyps / m.meeting_id)]) == 2
    assert "RuntimeError: synthetic failure" in capsys.readouterr().err
    rc = main(["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(hyps), "--report", str(tmp_path / "b.json")])
    assert rc == 2
    doc = json.loads((tmp_path / "b.json").read_text())  # the report is still written, naming the meeting
    assert "RuntimeError: synthetic failure" in doc["errors"][0]["error"]
    assert main(["selfcheck"]) == 2


def _fragmented(m: "Meeting", n_speakers: int) -> Hypothesis:  # noqa: F821
    """The reference's words, each segment split into one-word pieces spread
    over ``n_speakers`` hypothesis labels (a 'many speakers' hypothesis); labels
    left without a word get one inserted "uh" after the last reference word."""
    segs: list[Segment] = []
    k = 0
    for s in m.segments:
        words = s.text.split()
        step = (s.end - s.start) / max(1, len(words))
        for i, w in enumerate(words):
            segs.append(Segment(f"h{k % n_speakers:02d}", s.start + i * step, s.start + (i + 1) * step, w))
            k += 1
    t = max(s.end for s in m.segments)
    for j in range(k, n_speakers):
        segs.append(Segment(f"h{j:02d}", min(t, m.duration_s - 0.1), min(t + 0.05, m.duration_s), "uh"))
    assert len({s.speaker for s in segs}) == n_speakers
    return Hypothesis(m.meeting_id, "fragmented", segs)


def test_batch_keeps_der_when_meeteval_refuses_more_than_20_speakers(tmp_path: Path) -> None:
    """V5 no-hint AMI runs (docs/v5-results.md): meeteval 0.4.3 refuses
    cpWER/tcpWER above 20 speakers, and ``evals batch`` used to drop the
    whole meeting, DER included.  Now cpWER/tcpWER are recorded as not
    scored, with the reason; DER/JER/WER are reported; a gate on cpWER fails
    closed, and the macro/micro pools say how many meetings were refused."""
    d, m = make_meeting_dir(tmp_path, "many")
    write_hypothesis_files(_fragmented(m, 21), tmp_path / "hyps" / "many")
    good, gm = make_meeting_dir(tmp_path, "good")
    write_hypothesis_files(Hypothesis(gm.meeting_id, "sys", list(gm.segments)), tmp_path / "hyps" / "good")
    base = ["batch", "--refs", str(tmp_path / "refs"), "--hyps", str(tmp_path / "hyps"),
            "--report", str(tmp_path / "b.json"), "--md", str(tmp_path / "b.md"), "--quiet"]
    assert main(base) == 0
    doc = json.loads((tmp_path / "b.json").read_text())
    assert doc["errors"] == [] and doc["n_meetings"] == 2
    many = next(r for r in doc["meetings"] if r["meeting_id"] == "many")
    assert isinstance(many["der"]["der"], float) and isinstance(many["jer"]["jer"], float)
    assert isinstance(many["wer_concat"]["wer"], float)
    for k in ("cpwer", "tcpwer"):
        assert many[k]["error_rate"] is None and many[k]["errors"] is None
        assert many[k]["refused"] == "meeteval refuses more than 20 speakers (reference 2, hypothesis 21)"
    assert doc["macro"]["counts"]["der.der"] == 2 and doc["macro"]["counts"]["cpwer.error_rate"] == 1
    assert doc["macro"]["not_scored"]["cpwer.error_rate"] == 1 and "der.der" not in doc["macro"]["not_scored"]
    assert doc["micro"]["counts"]["cpwer.error_rate"] == 1 and doc["micro"]["cpwer.error_rate"] == 0.0
    # a cpWER gate fails closed on the refused meeting; the per-meeting gate names it
    assert main(base + ["--suite", "synthetic-clean"]) == 1
    doc = json.loads((tmp_path / "b.json").read_text())
    cp = next(c for c in doc["gates"][0]["checks"] if c["metric"] == "cpwer.error_rate")
    assert cp["passed"] is False and "not scored on 1 of 2 meetings" in cp["reason"]


def test_score_reports_a_refused_cpwer_in_its_markdown(tmp_path: Path) -> None:
    d, m = make_meeting_dir(tmp_path, "many")
    hyp_dir = tmp_path / "hyps" / "many"
    write_hypothesis_files(_fragmented(m, 21), hyp_dir)
    md = tmp_path / "r.md"
    assert main(["score", "--ref", str(d), "--hyp", str(hyp_dir / "hyp.json"), "--quiet", "--md", str(md)]) == 0
    text = md.read_text()
    assert "cpWER: not scored (meeteval refuses more than 20 speakers (reference 2, hypothesis 21))" in text
    assert "tcpWER: not scored" in text
