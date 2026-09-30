"""The V5 regression gates in evals/gates.yaml agree with the measurement in docs/v5-results.md.

These tests need no model, voice or corpus, so they run everywhere (CI included).
They pin four things:

* the two calibrated suites exist, are one-sided regression gates (``max`` only), and
  each threshold equals the measured value recorded in docs/v5-results.md plus the
  stated margin, rounded up to 3 decimals (HARNESS_POLICY, docs/v5-results.md "Gates");
* the gates pass on the recorded measurement (evals.gates.evaluate), so the suite is
  not already red on the numbers it was calibrated from;
* ``ami-headset`` stays labelled as the full-test-set target that has not been measured;
* scripts/run-v5.sh parses and applies both suites, and the skip reasons of the model,
  voice and AMI tests map to documented, local-only gates in tests/conftest.py.

The measured values themselves come from build/v5/ (git-ignored), produced by
scripts/run-v5.sh on local-only inputs; the doc's JSON block is the committed copy.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evals.gates import DEFAULT_GATES_PATH, evaluate, get_suite, load_gates  # noqa: E402

DOC = REPO / "docs" / "v5-results.md"
TEST_DOC = REPO / "docs" / "v5-test-split.md"
SCRIPT = REPO / "scripts" / "run-v5.sh"
V5_SUITES = ("ami-subset-whisper-sherpa", "synthetic-piper-whisper-sherpa")
#: the 2026-09-28 full-test-split regression suites, calibrated in docs/v5-test-split.md
V5_TEST_SUITES = ("ami-test-whisper-sherpa-cal", "ami-test-whisper-sherpa-hint", "ami-test-whisper-sherpa-ecapa",
                  "ami-test-whisper-pyannote", "ami-test-pyannote-audio")


def calibration(doc: Path = DOC, tag: str = "v5-gate-calibration") -> dict:
    text = doc.read_text(encoding="utf-8")
    m = re.search(rf"```json {tag}\n(.*?)\n```", text, re.S)
    assert m, f"{doc.name} has no ```json {tag} block"
    return json.loads(m.group(1))


def suite_record(suite_name: str) -> tuple[dict, str]:
    if suite_name in V5_TEST_SUITES:
        return calibration(TEST_DOC, "v5-test-gate-calibration")["suites"][suite_name], "docs/v5-test-split.md"
    return calibration()["suites"][suite_name], "docs/v5-results.md"


def round_up(x: float, places: int = 3) -> float:
    q = 10**places
    return math.ceil(round(x * q, 9)) / q


def test_calibration_block_names_both_suites_and_the_margin_policy() -> None:
    cal = calibration()
    assert cal["schema"] == "plaud-harness/v5-gate-calibration/1"
    assert set(cal["suites"]) == set(V5_SUITES)
    assert "HARNESS_POLICY" in cal["margin_policy"]
    for name, s in cal["suites"].items():
        assert s["gate_on"] == "macro", name
        assert set(s["measured"]) == set(s["margin"]), name
        assert all(v > 0 for v in s["margin"].values()), name


def test_test_split_calibration_block_names_its_suites() -> None:
    cal = calibration(TEST_DOC, "v5-test-gate-calibration")
    assert cal["schema"] == "plaud-harness/v5-gate-calibration/1" and "HARNESS_POLICY" in cal["margin_policy"]
    assert set(cal["suites"]) == set(V5_TEST_SUITES)
    for name, s in cal["suites"].items():
        assert s["gate_on"] == "macro" and set(s["measured"]) == set(s["margin"]), name


@pytest.mark.parametrize("suite_name", V5_SUITES + V5_TEST_SUITES)
def test_v5_suite_thresholds_are_measured_plus_the_stated_margin(suite_name: str) -> None:
    suite = get_suite(load_gates(DEFAULT_GATES_PATH), suite_name)
    rec, doc = suite_record(suite_name)
    assert "regression" in suite.description.lower() and doc in suite.description
    assert "HARNESS_POLICY" in suite.description
    metrics = {c.metric for c in suite.checks}
    assert metrics == set(rec["measured"]), (metrics, set(rec["measured"]))
    for c in suite.checks:
        assert c.min is None and c.eq is None and c.tol == 0.0, f"{suite_name}: {c.metric} must be max-only"
        assert c.max is not None and math.isfinite(c.max)
        want = round_up(rec["measured"][c.metric] + rec["margin"][c.metric])
        assert c.max == pytest.approx(want, abs=1e-12), (c.metric, c.max, want)


@pytest.mark.parametrize("suite_name", V5_SUITES + V5_TEST_SUITES)
def test_v5_suite_passes_on_the_recorded_measurement(suite_name: str) -> None:
    suite = get_suite(load_gates(DEFAULT_GATES_PATH), suite_name)
    rec, _ = suite_record(suite_name)
    res = evaluate(suite, rec["measured"], target="macro")
    assert res.passed, res.summary()
    # ... and it is a real bound: the measurement plus twice the margin fails every check.
    worse = {k: v + 2 * rec["margin"][k] + 0.002 for k, v in rec["measured"].items()}
    assert evaluate(suite, worse, target="macro").failed_metrics == [c.metric for c in suite.checks]


def test_ami_headset_is_the_full_test_set_target_and_records_that_it_was_missed() -> None:
    suite = get_suite(load_gates(DEFAULT_GATES_PATH), "ami-headset")
    desc = suite.description.lower()
    assert "full" in desc and "16" in desc and "failed" in desc and "docs/v5-test-split.md" in suite.description
    for name in ("ami-subset-whisper-sherpa", *V5_TEST_SUITES):
        assert name in suite.description
    # and it does fail on the recorded full-split measurements
    # (pyannote-audio has no words, so no cpWER: it meets the DER half, which the description records)
    for name in V5_TEST_SUITES:
        rec, _ = suite_record(name)
        res = evaluate(suite, rec["measured"], target="macro")
        if "cpwer.error_rate" in rec["measured"]:
            assert not res.passed and "der.der" in res.failed_metrics
        else:
            assert rec["measured"]["der.der"] <= 0.20 and res.failed_metrics == ["cpwer.error_rate"]


def test_run_v5_script_is_valid_bash_and_applies_both_suites() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    text = SCRIPT.read_text(encoding="utf-8")
    for name in V5_SUITES:
        # the ami suite is the default of V5_AMI_GATES; the others are direct calls
        assert f"gate_one {name} " in text or f"{name}:whisper-sherpa-hint:macro" in text, name
    assert "build/v5" in text and "python -m pipeline fetch-models" in text


def test_local_input_skip_reasons_map_to_documented_local_only_gates() -> None:
    import importlib.util  # noqa: PLC0415

    name = "_v5_conftest_copy"
    spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "conftest.py")
    assert spec is not None and spec.loader is not None
    conftest = importlib.util.module_from_spec(spec)
    sys.modules[name] = conftest  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(conftest)  # a private copy: only its pure gate_for/ENV_GATES are used
    finally:
        sys.modules.pop(name, None)
    gate_for = conftest.gate_for
    cases = {
        "piper voice unavailable: no en_US-libritts_r-medium.aligned.onnx": "piper-voice",
        "model weights not fetched: faster-whisper-small.en under data/models (model.bin absent)": "model-weights",
        "AMI data not present: data/corpora/ami/raw/ami_public_manual_1.6.2.zip": "ami-corpus",
        "AMI corpus not present: data/corpora/ami/meetings/ES2004a (python -m evals.ami convert)": "ami-corpus",
    }
    for reason, name in cases.items():
        gate = gate_for(reason)
        assert gate is not None and gate.name == name, (reason, gate)
        assert gate.ci_provides is False
        assert "Local-only" in gate.lifted_by
