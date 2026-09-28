"""Layer 3 gates: suites load, checks pass/fail naming the metric, exit codes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))

from evals.gates import (  # noqa: E402
    DEFAULT_GATES_PATH,
    EXIT_GATE_FAILED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_SKIPPED,
    Check,
    GateConfigError,
    Suite,
    evaluate,
    evaluate_check,
    get_suite,
    load_gates,
    suites_from_dict,
)

CLEAN_PASS = {"der.der": 0.03, "cpwer.error_rate": 0.08, "jer.jer": 0.05, "speaker_count.abs_error": 0}


def test_shipped_gates_file_loads_with_the_expected_suites() -> None:
    suites = load_gates(DEFAULT_GATES_PATH)
    assert {"oracle", "synthetic-clean", "synthetic-noisy", "ami-headset"} <= set(suites)
    oracle = suites["oracle"]
    assert {c.metric for c in oracle.checks} >= {"der.der", "cpwer.error_rate", "tcpwer.error_rate"}
    assert all(c.eq == 0.0 for c in oracle.checks)
    clean = suites["synthetic-clean"]
    assert {c.metric: c.max for c in clean.checks}["der.der"] == 0.05
    assert {c.metric: c.max for c in clean.checks}["cpwer.error_rate"] == 0.10


def test_suite_passes_and_fails_naming_the_metric() -> None:
    suite = get_suite(load_gates(), "synthetic-clean")
    ok = evaluate(suite, CLEAN_PASS, target="m1")
    assert ok.passed and ok.failed_metrics == [] and ok.exit_code == EXIT_OK
    assert ok.summary().startswith("[PASS] suite 'synthetic-clean' on m1")

    bad = evaluate(suite, {**CLEAN_PASS, "cpwer.error_rate": 0.12}, target="m1")
    assert not bad.passed and bad.failed_metrics == ["cpwer.error_rate"] and bad.exit_code == EXIT_GATE_FAILED
    assert "cpwer.error_rate <= 0.1 (got 0.12: exceeds max 0.1)" in bad.summary()
    assert "der.der" not in bad.summary()

    worse = evaluate(suite, {**CLEAN_PASS, "der.der": 0.5, "speaker_count.abs_error": 2})
    assert worse.failed_metrics == ["der.der", "speaker_count.abs_error"]
    d = worse.to_dict()
    assert d["passed"] is False and [c["metric"] for c in d["checks"] if not c["passed"]] == worse.failed_metrics


def test_missing_or_non_numeric_metric_fails_closed() -> None:
    suite = get_suite(load_gates(), "synthetic-clean")
    res = evaluate(suite, {k: v for k, v in CLEAN_PASS.items() if k != "jer.jer"})
    assert res.failed_metrics == ["jer.jer"]
    assert "metric not in report" in res.summary()
    res2 = evaluate(suite, {**CLEAN_PASS, "der.der": None})
    assert res2.failed_metrics == ["der.der"] and "no numeric value" in res2.summary()
    res3 = evaluate(suite, {**CLEAN_PASS, "der.der": float("nan")})
    assert res3.failed_metrics == ["der.der"]
    res4 = evaluate(suite, {**CLEAN_PASS, "der.der": True})
    assert res4.failed_metrics == ["der.der"]


def test_eq_min_max_and_tolerance_semantics() -> None:
    eq = Check("x", eq=0.0, tol=1e-9)
    assert evaluate_check(eq, {"x": 1e-10}).passed
    assert not evaluate_check(eq, {"x": 1e-8}).passed
    assert not evaluate_check(Check("x", eq=0.0), {"x": 1e-12}).passed  # exact when tol is 0
    mn = Check("x", min=0.9)
    assert evaluate_check(mn, {"x": 0.95}).passed and not evaluate_check(mn, {"x": 0.85}).passed
    both = Check("x", min=0.2, max=0.4)
    assert evaluate_check(both, {"x": 0.3}).passed
    assert evaluate_check(both, {"x": 0.5}).reason == "exceeds max 0.4"
    assert evaluate_check(both, {"x": 0.1}).reason == "below min 0.2"
    assert evaluate_check(Check("x", max=0.1, tol=0.01), {"x": 0.105}).passed
    assert Check("x", min=0.2, max=0.4, eq=0.3, tol=0.1).describe() == "x <= 0.4 (+0.1 tol) and >= 0.2 (-0.1 tol) and == 0.3 ±0.1"


@pytest.mark.parametrize(
    "doc, needle",
    [
        ({"schema": "nope", "suites": {}}, "schema is 'nope'"),
        ({"schema": "plaud-harness/gates/1"}, "'suites' must be a non-empty mapping"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {}}}, "must be a mapping with a 'gates' list"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": []}}}, "must be a non-empty list"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": [{"metric": "a"}]}}}, "declares no bound"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": [{"metric": "a", "lt": 1}]}}}, "unknown keys ['lt']"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": [{"metric": "a", "max": "1"}]}}}, "expected a number"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": [{"metric": "a", "max": 1, "tol": -1}]}}}, "'tol' must be >= 0"),
        ({"schema": "plaud-harness/gates/1", "suites": {"s": {"gates": [{"max": 1}]}}}, "with a 'metric' key"),
        ([], "top level must be a mapping"),
    ],
)
def test_malformed_gate_files_are_rejected(doc, needle) -> None:
    with pytest.raises(GateConfigError) as exc:
        suites_from_dict(doc, where="g.yaml")
    assert needle in str(exc.value), str(exc.value)


def test_unknown_suite_lists_the_available_ones() -> None:
    suites = load_gates()
    with pytest.raises(GateConfigError, match=r"unknown suite 'zzz'; available: \['ami-headset'"):
        get_suite(suites, "zzz")


def test_gate_file_errors_name_the_file(tmp_path: Path) -> None:
    with pytest.raises(GateConfigError, match="file not found"):
        load_gates(tmp_path / "missing.yaml")
    p = tmp_path / "bad.yaml"
    p.write_text("schema: [unclosed")
    with pytest.raises(GateConfigError, match="not valid YAML"):
        load_gates(p)
    p2 = tmp_path / "custom.yaml"
    p2.write_text(yaml.safe_dump({"schema": "plaud-harness/gates/1", "suites": {"mine": {"description": "d", "gates": [{"metric": "der.der", "max": 0.3}]}}}))
    s = load_gates(p2)["mine"]
    assert isinstance(s, Suite) and s.description == "d" and evaluate(s, {"der.der": 0.2}).passed


def test_exit_codes_are_distinct_and_documented() -> None:
    assert (EXIT_OK, EXIT_GATE_FAILED, EXIT_INPUT_ERROR, EXIT_SKIPPED) == (0, 1, 2, 3)


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-25): gate files cannot fail open
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "check_yaml, needle",
    [
        ("{metric: der.der, max: .nan}", "max: expected a finite number"),
        ("{metric: der.der, max: .inf}", "max: expected a finite number"),
        ("{metric: der.der, min: -.inf}", "min: expected a finite number"),
        ("{metric: der.der, eq: .nan}", "eq: expected a finite number"),
        ("{metric: cpwer.error_rate, max: 0.1, tol: .inf}", "tol: expected a finite number"),
        ("{metric: cpwer.error_rate, max: 0.1, tol: .nan}", "tol: expected a finite number"),
        ("{metric: der.der, min: 0.5, max: 0.1}", "min 0.5 is above max 0.1"),
        ("{metric: der.der, eq: 0.9, max: 0.1}", "eq 0.9 lies outside"),
        ("{metric: der.der, eq: 0.0, min: 0.5, tol: 0.1}", "eq 0.0 lies outside"),
    ],
)
def test_gate_files_with_non_finite_or_contradictory_bounds_are_rejected(tmp_path: Path, check_yaml: str, needle: str) -> None:
    """EV-6.  ``max: .nan`` made ``value > nan`` always False and ``tol: .inf``
    widened a max to infinity: both passed any value.  min > max never passes."""
    p = tmp_path / "g.yaml"
    p.write_text(f"schema: plaud-harness/gates/1\nsuites:\n  s:\n    gates:\n      - {check_yaml}\n")
    with pytest.raises(GateConfigError) as exc:
        load_gates(p)
    assert needle in str(exc.value), str(exc.value)


def test_describe_shows_the_tolerance_on_every_bound_it_widens() -> None:
    assert Check("cpwer.error_rate", max=0.1, tol=0.05).describe() == "cpwer.error_rate <= 0.1 (+0.05 tol)"
    assert Check("x", min=0.9, tol=0.01).describe() == "x >= 0.9 (-0.01 tol)"
    assert Check("x", max=0.1).describe() == "x <= 0.1"
    assert Check("der.der", eq=0.0, tol=1e-9).describe() == "der.der == 0.0 ±1e-09"
    res = evaluate(Suite("s", (Check("cpwer.error_rate", max=0.1, tol=0.05),)), {"cpwer.error_rate": 0.2})
    assert "cpwer.error_rate <= 0.1 (+0.05 tol) (got 0.2: exceeds max 0.1" in res.summary()


def test_gate_file_that_is_not_utf8_or_not_a_file_is_a_config_error(tmp_path: Path) -> None:
    p = tmp_path / "latin1.yaml"
    p.write_bytes(b"schema: plaud-harness/gates/1\n# caf\xe9\n")
    with pytest.raises(GateConfigError, match="not valid UTF-8"):
        load_gates(p)
    with pytest.raises(GateConfigError, match="cannot read"):
        load_gates(tmp_path)
