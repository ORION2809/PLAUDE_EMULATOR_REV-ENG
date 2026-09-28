"""CI gates: named suites of metric thresholds with pass/fail and exit codes.

A suite is a list of checks on the dotted metric paths that
:meth:`evals.metrics.MeetingReport.flat` produces (``der.der``,
``cpwer.error_rate``, ...).  A check passes when the metric satisfies every
bound it declares (``max``, ``min``, ``eq`` with optional ``tol``).

HARNESS_POLICY (fail closed): a metric that is missing, ``None`` (for
example DER/JER on a meeting with no scorable reference speech) or non-finite
fails its check — a gate can never pass because a number was not produced.
The gate file itself cannot open a gate either: bounds and ``tol`` must be
finite (``max: .nan`` or ``tol: .inf`` would pass any value).  A check with
``min`` > ``max``, or whose ``eq`` lies outside its ``min``/``max`` even
after ``tol``, is refused as a mistake in the file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

GATES_SCHEMA = "plaud-harness/gates/1"
DEFAULT_GATES_PATH = Path(__file__).with_name("gates.yaml")

#: Process exit codes (HARNESS_POLICY, documented in docs/evals.md).
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_SKIPPED = 3


class GateConfigError(ValueError):
    """gates.yaml is malformed or names an unknown suite."""


@dataclass(frozen=True)
class Check:
    metric: str
    max: float | None = None
    min: float | None = None
    eq: float | None = None
    tol: float = 0.0

    def describe(self) -> str:
        """The constraint as evaluated, including the tolerance on every bound it widens."""
        parts = []
        if self.max is not None:
            parts.append(f"<= {self.max}" + (f" (+{self.tol} tol)" if self.tol else ""))
        if self.min is not None:
            parts.append(f">= {self.min}" + (f" (-{self.tol} tol)" if self.tol else ""))
        if self.eq is not None:
            parts.append(f"== {self.eq}" + (f" ±{self.tol}" if self.tol else ""))
        return f"{self.metric} " + " and ".join(parts)


@dataclass(frozen=True)
class Suite:
    name: str
    checks: tuple[Check, ...]
    description: str = ""


@dataclass
class CheckResult:
    check: Check
    value: float | int | None
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.check.metric,
            "constraint": self.check.describe(),
            "value": self.value,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass
class GateResult:
    suite: str
    target: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed_metrics(self) -> list[str]:
        return [r.check.metric for r in self.results if not r.passed]

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.passed else EXIT_GATE_FAILED

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        head = f"[{status}] suite {self.suite!r} on {self.target}"
        if self.passed:
            return head + f" ({len(self.results)} checks)"
        return head + " — failed: " + "; ".join(
            f"{r.check.describe()} (got {r.value!r}: {r.reason})" for r in self.results if not r.passed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "target": self.target,
            "passed": self.passed,
            "failed_metrics": self.failed_metrics,
            "checks": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _number(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise GateConfigError(f"{where}: expected a number, got {v!r}")
    if not math.isfinite(v):
        # YAML's .nan/.inf: `value > nan` is always False and `max + inf` is
        # infinite, so either would make the check pass anything.
        raise GateConfigError(f"{where}: expected a finite number, got {v!r}")
    return float(v)


def parse_check(raw: Any, where: str) -> Check:
    if not isinstance(raw, Mapping) or "metric" not in raw:
        raise GateConfigError(f"{where}: each check must be a mapping with a 'metric' key, got {raw!r}")
    metric = raw["metric"]
    if not isinstance(metric, str) or not metric:
        raise GateConfigError(f"{where}: 'metric' must be a non-empty string")
    unknown = set(raw) - {"metric", "max", "min", "eq", "tol"}
    if unknown:
        raise GateConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: max, min, eq, tol)")
    bounds = {k: _number(raw[k], f"{where}.{k}") for k in ("max", "min", "eq") if k in raw}
    if not bounds:
        raise GateConfigError(f"{where}: check on {metric!r} declares no bound (max/min/eq)")
    tol = _number(raw.get("tol", 0.0), f"{where}.tol")
    if tol < 0:
        raise GateConfigError(f"{where}: 'tol' must be >= 0")
    lo, hi, eq = bounds.get("min"), bounds.get("max"), bounds.get("eq")
    if lo is not None and hi is not None and lo > hi:
        # Refused even when tol would leave a sliver [min - tol, max + tol] open:
        # an inverted range is a mistake in the file, not a threshold.
        raise GateConfigError(f"{where}: check on {metric!r} has contradictory bounds: min {lo} is above max {hi}")
    if eq is not None and ((lo is not None and eq + tol < lo - tol) or (hi is not None and eq - tol > hi + tol)):
        raise GateConfigError(f"{where}: check on {metric!r} can never pass: eq {eq} lies outside [min, max] = [{lo}, {hi}]")
    return Check(metric=metric, max=hi, min=lo, eq=eq, tol=tol)


def suites_from_dict(data: Any, *, where: str = "gates.yaml") -> dict[str, Suite]:
    if not isinstance(data, Mapping):
        raise GateConfigError(f"{where}: top level must be a mapping")
    if data.get("schema") != GATES_SCHEMA:
        raise GateConfigError(f"{where}: schema is {data.get('schema')!r}, expected {GATES_SCHEMA!r}")
    raw_suites = data.get("suites")
    if not isinstance(raw_suites, Mapping) or not raw_suites:
        raise GateConfigError(f"{where}: 'suites' must be a non-empty mapping")
    suites: dict[str, Suite] = {}
    for name, body in raw_suites.items():
        w = f"{where}.suites.{name}"
        if not isinstance(body, Mapping) or "gates" not in body:
            raise GateConfigError(f"{w}: a suite must be a mapping with a 'gates' list")
        gates = body["gates"]
        if not isinstance(gates, list) or not gates:
            raise GateConfigError(f"{w}.gates: must be a non-empty list")
        checks = tuple(parse_check(c, f"{w}.gates[{i}]") for i, c in enumerate(gates))
        suites[str(name)] = Suite(name=str(name), checks=checks, description=str(body.get("description", "")))
    return suites


def load_gates(path: str | Path = DEFAULT_GATES_PATH) -> dict[str, Suite]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise GateConfigError(f"{p}: file not found") from None
    except UnicodeDecodeError as exc:
        raise GateConfigError(f"{p}: not valid UTF-8 ({exc.reason} at byte {exc.start})") from None
    except OSError as exc:
        raise GateConfigError(f"{p}: cannot read file ({exc.strerror or exc})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateConfigError(f"{p}: not valid YAML ({exc})") from None
    return suites_from_dict(data, where=str(p))


def get_suite(suites: Mapping[str, Suite], name: str) -> Suite:
    if name not in suites:
        raise GateConfigError(f"unknown suite {name!r}; available: {sorted(suites)}")
    return suites[name]


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate_check(check: Check, metrics: Mapping[str, Any]) -> CheckResult:
    if check.metric not in metrics:
        return CheckResult(check, None, False, "metric not in report")
    value = metrics[check.metric]
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return CheckResult(check, value, False, "metric has no numeric value")
    reasons = []
    if check.max is not None and value > check.max + check.tol:
        reasons.append(f"exceeds max {check.max}")
    if check.min is not None and value < check.min - check.tol:
        reasons.append(f"below min {check.min}")
    if check.eq is not None and abs(value - check.eq) > check.tol:
        reasons.append(f"differs from {check.eq}" + (f" by more than {check.tol}" if check.tol else ""))
    if reasons:
        return CheckResult(check, value, False, "; ".join(reasons))
    return CheckResult(check, value, True, "ok")


def evaluate(suite: Suite, metrics: Mapping[str, Any], *, target: str = "report") -> GateResult:
    """Apply a suite to a flat metric mapping (``MeetingReport.flat()`` or an aggregate)."""
    return GateResult(suite=suite.name, target=target, results=[evaluate_check(c, metrics) for c in suite.checks])
