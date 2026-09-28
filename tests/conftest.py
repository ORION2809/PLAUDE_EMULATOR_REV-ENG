"""Test-session guards: keep reference/** pristine, and keep skips visible and honest.

1. Derived artifacts stay out of reference/**.
   `bumble` is installed editable from reference/upstream/bumble, so importing it
   would otherwise write __pycache__/*.pyc underneath the immutable evidence tree.
   Disabling bytecode writing here (before any test module imports bumble) keeps
   `reference/**` byte-identical across test runs. Verifier scripts run outside
   pytest should be invoked with PYTHONDONTWRITEBYTECODE=1 for the same reason.

2. Environment-gated skips are reported, and in CI they must be documented
   (HARNESS_POLICY).
   Some inputs exist only on a machine that ran the local-only steps: the
   decompiled SDK under build/evidence/** (./scripts/build-evidence.sh), the SDK
   .aar under reference/plaud-org/ (./scripts/fetch-references.sh), a generated
   meeting set under build/synthetic/. The SDK binaries are proprietary and are
   never fetched in CI (PROJECT.md section 2, decision log 2026-09-21), so the
   tests that need them skip there, while the tests that read the committed
   docs/evidence-digest.json still run.

   Each such test skips itself, with a reason. This file does not skip anything:
   the per-test checks are finer-grained than any module list could be
   (test_evidence_conformance.py, for one, mixes digest-only tests that must run
   with regenerate-from-bytecode tests that cannot). What this file adds:

   * a report-header line saying which local-only inputs are present;
   * a terminal summary that groups every skip by reason and labels it with the
     gate in ENV_GATES that explains it, or UNDOCUMENTED;
   * strict mode (PLAUD_STRICT_SKIPS=1, which .github/workflows/tests.yml sets):
     a skip that no gate documents, or one whose input CI is supposed to provide,
     fails the run. A missing dependency (importorskip), missing committed
     evidence, or a digest test that stopped running then turns the build red
     instead of quietly shrinking the suite. To accept a new
     environment-dependent skip, add a Gate naming the input that lifts it.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STRICT_ENV = "PLAUD_STRICT_SKIPS"


@dataclass(frozen=True)
class Gate:
    """One documented reason a test may skip for lack of a local-only input."""

    name: str
    pattern: str  # re.search()ed against the skip reason
    lifted_by: str  # the input whose presence (or absence) makes the test run
    ci_provides: bool  # True: CI supplies the input, so this skip is a failure there


ENV_GATES: tuple[Gate, ...] = (
    Gate("decompiled-sdk",
         r"no decompiled evidence|build/evidence not built|^missing \S*build/evidence/",
         "build/evidence/** from ./scripts/build-evidence.sh (proprietary SDK; never in CI)",
         ci_provides=False),
    Gate("sdk-aar",
         r"^SDK not fetched",
         "reference/plaud-org/plaud-sdk-public from ./scripts/fetch-references.sh (proprietary; never in CI)",
         ci_provides=False),
    Gate("synthetic-set",
         r"^no generated meeting under build/synthetic",
         "build/synthetic/** from scripts/make-synthetic-set.sh (CI generates one before the tests)",
         ci_provides=True),
    Gate("docker-present",
         r"^docker is present",
         "no docker on PATH (GitHub's ubuntu runners ship docker)",
         ci_provides=False),
    Gate("docker-cli-absent",
         r"^docker compose CLI is not on PATH",
         "the docker CLI with the compose v2 plugin (GitHub's ubuntu runners ship both; "
         "its macOS runners ship neither, so there the skip is expected)",
         ci_provides=sys.platform.startswith("linux")),
    Gate("optional-engine-installed",
         r" is installed locally with voices|is importable here",
         "the optional TTS/ASR engine being absent (only a requirements change installs one in CI)",
         ci_provides=False),
    Gate("ami-corpus",
         r"^AMI (corpus|data) not present: ",
         "data/corpora/ami/raw/{ami_public_manual_1.6.2.zip,audio/<ID>.Mix-Headset.wav} (CC BY 4.0; fetched and "
         "sha256-checked by the inputs stage of scripts/run-v5.sh), then `python -m evals.ami extract` and "
         "`python -m evals.ami convert` -> data/corpora/ami/meetings/<ID>/; the crosscheck tests also need "
         "data/corpora/ami/but_setup/only_words/rttms (BUT AMI-diarization-setup, Apache-2.0; the fetch loop in "
         "docs/evals.md 'AMI'). Local-only, never in CI",
         ci_provides=False),
    Gate("model-weights",
         r"^model weights not fetched: ",
         "data/models/** (faster-whisper small.en, pyannote segmentation-3.0 ONNX, 3D-Speaker CAM++; pinned "
         "and sha256-checked in pipeline/model_store.py) from `python -m pipeline fetch-models` (no token; "
         "also run by scripts/run-v5.sh). Local-only, never in CI",
         ci_provides=False),
    Gate("piper-voice",
         r"^piper voice unavailable:",
         "piper-tts + data/voices/piper/en_US-libritts_r-medium{.onnx,.onnx.json} (rhasspy/piper-voices at "
         "c10ece1a, fetched and sha256-checked by the inputs stage of scripts/run-v5.sh) and either the verified "
         "en_US-libritts_r-medium.aligned.onnx + .aligned.source.json (`python -m generator.tts.piper align "
         "data/voices/piper/en_US-libritts_r-medium.onnx`) or the onnx package. Local-only; CI installs neither",
         ci_provides=False),
)

_SKIPS: list[tuple[str, str]] = []  # (location, reason), one per skipped test or module
_VIOLATIONS: list[tuple[str, str]] = []


def _strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip() not in ("", "0")


def gate_for(reason: str) -> Gate | None:
    for gate in ENV_GATES:
        if re.search(gate.pattern, reason):
            return gate
    return None


def _record(report) -> None:
    if not report.skipped or hasattr(report, "wasxfail"):
        return
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        path, lineno, reason = longrepr
        try:
            path = os.path.relpath(path, ROOT)
        except ValueError:
            pass
        loc = f"{path}:{lineno}" if lineno is not None else str(path)
    else:
        loc, reason = report.nodeid, str(longrepr)
    reason = str(reason)
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: "):]
    _SKIPS.append((loc, reason))


def pytest_runtest_logreport(report) -> None:
    _record(report)


def pytest_collectreport(report) -> None:
    _record(report)


def pytest_report_header(config) -> list[str]:
    def state(rel: str) -> str:
        return f"{rel}={'present' if (ROOT / rel).exists() else 'absent'}"

    inputs = ", ".join(state(rel) for rel in (
        "build/evidence/javap",
        "reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar",
        "docs/evidence-digest.json",
        "build/synthetic",
    ))
    return [f"plaud-harness inputs: {inputs}",
            f"plaud-harness skip policy: {STRICT_ENV}={'1 (strict)' if _strict() else 'unset (report only)'}"]


def pytest_sessionfinish(session, exitstatus) -> None:
    _VIOLATIONS.clear()
    if not _strict():
        return
    for loc, reason in _SKIPS:
        gate = gate_for(reason)
        if gate is None or gate.ci_provides:
            _VIOLATIONS.append((loc, reason))
    if _VIOLATIONS and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not _SKIPS:
        return
    tr = terminalreporter
    tr.section("skips by reason (tests/conftest.py)")
    groups: dict[str, list[str]] = {}
    for loc, reason in _SKIPS:
        groups.setdefault(reason, []).append(loc)
    for reason, locs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        gate = gate_for(reason)
        label = gate.name if gate else "UNDOCUMENTED"
        files = sorted({loc.rsplit(":", 1)[0] for loc in locs})
        tr.write_line(f"{len(locs):4d}  [{label}] {reason}")
        tr.write_line(f"      in {', '.join(files)}")
    if _VIOLATIONS:
        tr.write_line("")
        tr.write_line(
            f"{STRICT_ENV}: {len(_VIOLATIONS)} skip(s) are not a documented CI gate; the run FAILS. "
            "Provide the input in CI, or add a Gate to ENV_GATES in tests/conftest.py naming it.",
            red=True, bold=True)
        for loc, reason in _VIOLATIONS:
            tr.write_line(f"  {loc}: {reason}", red=True)
