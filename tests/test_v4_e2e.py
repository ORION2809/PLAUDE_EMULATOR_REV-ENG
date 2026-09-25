"""V4 end to end: generator -> pipeline -> evals over the shared data contract.

Every component here is the harness's own (Layer 2 generator, the pipeline
seam, Layer 3 evals); nothing touches a Plaud service or device.  The chain:

    generator  fixture_meeting("smoke", seed 3)          12 s, 2 speakers, 5 turns
      -> pipeline "oracle" run on device/recording.ogg   (meeting.json discovered
                                                           two levels up)
      -> evals.score_meeting against the meeting dir     DER == JER == cpWER == 0.0
    the same through the two CLIs                        python -m pipeline run,
                                                         python -m evals score --suite oracle
    "perturbed-oracle" with nonzero rates                DER > 0 and cpWER > 0
    a speaker-swap perturbation                          literal WER > cpWER

Evidence class for the passing chain: EMULATOR_INTEGRATION_PROVEN for our own
layers -- the three packages agree on `plaud-harness/meeting/1` and
`plaud-harness/hypothesis/1` and the metric libraries return exact zeros on
the generator's dyadic-grid ground truth.  The oracle pipelines are harness
self-tests (`is_system_under_test is False`); no number below is a system's
score.  HARNESS_POLICY: the preset/seed, the perturbation rates and the
in-process CLI invocation (``main(argv)`` instead of a subprocess, to keep the
suite fast; the subprocess entry points are covered by tests/test_pipeline_cli.py
and tests/test_evals_cli.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals import load_meeting, score_meeting  # noqa: E402
from evals.gates import evaluate, get_suite, load_gates  # noqa: E402
from evals.io import hypothesis_from_dict, load_hypothesis  # noqa: E402
from generator.export import DEVICE_FILES  # noqa: E402
from generator.testing import fixture_meeting  # noqa: E402
from pipeline import PipelineConfig, get_pipeline, load_audio  # noqa: E402
from pipeline.meeting import reference_segments  # noqa: E402

pytestmark = pytest.mark.timeout(120)


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("v4e2e"), "smoke", 3)


def score(meeting_dir: Path, hyp) -> "MeetingReport":  # noqa: F821
    """Pipeline Hypothesis -> evals report, through the hyp.json contract."""
    return score_meeting(load_meeting(meeting_dir), hypothesis_from_dict(hyp.to_dict()))


def oracle_gate(report) -> None:
    result = evaluate(get_suite(load_gates(), "oracle"), report.flat(), target=report.meeting_id)
    assert result.passed, result.summary()


# --- the loader accepts both audio shapes the generator writes -----------------


def test_pipeline_loader_takes_the_device_ogg_and_the_wav_mix(meeting) -> None:
    d, m = meeting
    ogg = load_audio(d / DEVICE_FILES["ogg_opus"])
    wav = load_audio(d / m["audio"]["mix_wav"])
    assert ogg.container == "pyav" and ogg.source_sample_rate == 48000 and ogg.source_channels == 1
    assert wav.container == "wav" and wav.source_sample_rate == 16000
    assert ogg.sample_rate == wav.sample_rate == 16000
    assert abs(ogg.duration_s - m["duration_s"]) <= 0.020, "one Opus frame of encoder slack"
    assert wav.duration_s == m["duration_s"]


# --- oracle: exact zeros ---------------------------------------------------------


def test_oracle_on_the_device_ogg_scores_exactly_zero(meeting) -> None:
    d, m = meeting
    pipeline = get_pipeline("oracle")
    assert pipeline.is_system_under_test is False, "an oracle score is never a system's score"
    hyp = pipeline.run(d / DEVICE_FILES["ogg_opus"], d)
    assert hyp.meeting_id == m["meeting_id"]
    assert hyp.segments == reference_segments(m) == m["segments"]

    report = score(d, hyp)
    assert report.der.der == 0.0
    assert report.jer.jer == 0.0
    assert report.cpwer.error_rate == 0.0
    assert report.tcpwer.error_rate == 0.0 and report.tcpwer.word_level_timing is True
    assert report.wer_literal.wer == 0.0 and report.wer_concat.wer == 0.0
    assert report.speaker_count.abs_error == 0 and report.speaker_count.hypothesis == len(m["speakers"])
    assert report.der.total > 0, "the zero is over real reference speech, not an empty UEM"
    oracle_gate(report)


def test_oracle_discovers_meeting_json_from_the_audio_path_alone(meeting) -> None:
    d, m = meeting
    hyp = get_pipeline("oracle").run(d / DEVICE_FILES["ogg_opus"])  # no meeting_dir
    assert score(d, hyp).der.der == 0.0


# --- the CLI seam: python -m pipeline run -> hyp.json/.rttm/.stm -> python -m evals score --


def test_cli_seam_pipeline_run_then_evals_score_passes_the_oracle_suite(meeting, tmp_path) -> None:
    from evals.cli import main as evals_main
    from pipeline.cli import main as pipeline_main

    d, m = meeting
    out = tmp_path / "hyp" / "hyp.json"
    rc = pipeline_main([
        "run", "--pipeline", "oracle",
        "--audio", str(d / DEVICE_FILES["ogg_opus"]), "--meeting-dir", str(d),
        "--out", str(out),
    ])
    assert rc == 0
    for suffix in (".json", ".rttm", ".stm"):
        assert out.with_suffix(suffix).is_file(), suffix
    hyp = load_hypothesis(out)  # the evals parser accepts what the pipeline wrote
    assert hyp.meeting_id == m["meeting_id"] and hyp.system == "oracle"

    report_path = tmp_path / "report.json"
    rc = evals_main([
        "score", "--ref", str(d), "--hyp", str(out),
        "--suite", "oracle", "--report", str(report_path), "--quiet",
    ])
    assert rc == 0, "exit 0 == scored and every oracle gate passed"
    doc = json.loads(report_path.read_text())
    assert doc["schema"] == "plaud-harness/eval-report/1"
    assert doc["report"]["der"]["der"] == 0.0 and doc["report"]["cpwer"]["error_rate"] == 0.0
    assert doc["gates"]["suite"] == "oracle" and doc["gates"]["passed"] is True


# --- perturbed oracle: the metrics are alive -------------------------------------


def test_perturbed_oracle_with_zero_rates_is_the_identity(meeting) -> None:
    d, _ = meeting
    hyp = get_pipeline("perturbed-oracle", PipelineConfig(seed=1)).run(d / DEVICE_FILES["ogg_opus"], d)
    report = score(d, hyp)
    assert report.der.der == 0.0 and report.cpwer.error_rate == 0.0


def test_perturbed_oracle_with_nonzero_rates_raises_der_and_cpwer(meeting) -> None:
    d, _ = meeting
    # HARNESS_POLICY rates: word substitutions damage cpWER, boundary jitter damages DER.
    pipeline = get_pipeline(
        "perturbed-oracle",
        PipelineConfig(seed=1, params={"word_sub_rate": 0.3, "boundary_jitter_s": 0.2}),
    )
    assert pipeline.is_system_under_test is False
    hyp = pipeline.run(d / DEVICE_FILES["ogg_opus"], d)
    rep = pipeline.last_report
    assert rep.word_substitutions > 0 and rep.jittered_boundaries > 0

    report = score(d, hyp)
    assert report.der.der > 0.0
    assert report.jer.jer > 0.0
    assert report.cpwer.error_rate > 0.0
    assert report.cpwer.substitutions > 0
    # and the oracle gate now fails, naming the metrics that moved
    result = evaluate(get_suite(load_gates(), "oracle"), report.flat(), target=report.meeting_id)
    assert not result.passed
    assert {"der.der", "cpwer.error_rate"} <= set(result.failed_metrics)


def test_more_word_damage_means_more_cpwer_but_der_untouched(meeting) -> None:
    d, _ = meeting
    rates = []
    for sub in (0.2, 0.6):
        hyp = get_pipeline("perturbed-oracle", PipelineConfig(seed=3, params={"word_sub_rate": sub})).run(
            d / DEVICE_FILES["ogg_opus"], d
        )
        report = score(d, hyp)
        assert report.der.der == 0.0, "word edits never move the speaker timeline"
        rates.append(report.cpwer.error_rate)
    assert 0.0 < rates[0] < rates[1] <= 1.0


# --- speaker swap: literal WER lies, cpWER does not --------------------------------


def test_full_speaker_swap_makes_literal_wer_exceed_cpwer(meeting) -> None:
    """swap_rate 1.0 on a two-speaker meeting is a permutation of the labels.
    cpWER and DER optimise over label mappings and must stay at 0; the
    label-literal WER pairs spk0 with spk0 and scores every word wrong."""
    d, m = meeting
    assert len(m["speakers"]) == 2
    pipeline = get_pipeline("perturbed-oracle", PipelineConfig(seed=1, params={"speaker_swap_rate": 1.0}))
    hyp = pipeline.run(d / DEVICE_FILES["ogg_opus"], d)
    assert pipeline.last_report.speaker_swaps == len(m["segments"])
    flip = {"spk0": "spk1", "spk1": "spk0"}
    assert [s["speaker"] for s in hyp.segments] == [flip[s["speaker"]] for s in m["segments"]]

    report = score(d, hyp)
    assert report.cpwer.error_rate == 0.0
    assert report.der.der == 0.0 and report.jer.jer == 0.0
    assert report.wer_literal.wer > report.cpwer.error_rate
    assert report.wer_literal.wer >= 1.0, "every reference word is paired with the other speaker's words"
    assert report.wer_concat.wer == 0.0, "the speaker-agnostic WER cannot see attribution at all"


def test_partial_speaker_swap_moves_all_three_and_keeps_the_ordering_theorem(meeting) -> None:
    """A partial swap is not a permutation: cpWER and DER rise.  cpWER is the
    minimum over label assignments and the literal WER is one particular
    assignment, so literal >= cpWER holds by construction."""
    d, m = meeting
    pipeline = get_pipeline("perturbed-oracle", PipelineConfig(seed=2, params={"speaker_swap_rate": 0.5}))
    hyp = pipeline.run(d / DEVICE_FILES["ogg_opus"], d)
    swaps = pipeline.last_report.speaker_swaps
    assert 0 < swaps < len(m["segments"]), f"seed 2 must swap a strict subset, swapped {swaps}"
    report = score(d, hyp)
    assert report.der.der > 0.0 and report.der.confusion > 0.0
    assert report.cpwer.error_rate > 0.0
    assert report.wer_literal.wer >= report.cpwer.error_rate
