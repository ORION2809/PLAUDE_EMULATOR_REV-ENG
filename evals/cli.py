"""``python -m evals`` — score, batch, validate, selfcheck, suites.

Exit codes (HARNESS_POLICY, see evals.gates):
  0  scored and every gate passed (or no gates requested)
  1  scored but at least one gate failed (the failing metrics are printed)
  2  input error: malformed or unreadable contract/gates file, unknown suite,
     missing hypothesis, bad arguments -- and any unexpected error (1 is
     reserved for a failed gate, so a crash can never read as one)
  3  skipped: nothing to score (the reference root is absent or holds no
     meeting.json); never used when meetings exist but no hypothesis does
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Sequence

from evals import __version__
from evals.aggregate import BatchResult
from evals.gates import (
    DEFAULT_GATES_PATH,
    EXIT_GATE_FAILED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_SKIPPED,
    GateConfigError,
    GateResult,
    Suite,
    evaluate,
    get_suite,
    load_gates,
)
from evals.io import (
    ContractError,
    Hypothesis,
    Meeting,
    Normalizer,
    Segment,
    Word,
    load_hypothesis,
    load_meeting,
    write_hypothesis_files,
    write_meeting,
    write_reference_files,
)
from evals.metrics import DEFAULT_DER_COLLAR, DEFAULT_TCP_COLLAR, MeetingReport, format_value, score_meeting

REPORT_SCHEMA = "plaud-harness/eval-report/1"

#: HARNESS_POLICY: where the V5 corpus is expected.  docs/evals.md documents
#: the layout; nothing in this checkout provides it.
AMI_EXPECTED_LAYOUT = """\
expected layout (HARNESS_POLICY, docs/evals.md "What is not done"):
  data/corpora/ami/meetings/<MEETING_ID>/meeting.json   contract meeting.json (schema plaud-harness/meeting/1)
  data/corpora/ami/meetings/<MEETING_ID>/ref.rttm       written from meeting.json
  data/corpora/ami/meetings/<MEETING_ID>/ref.stm        written from meeting.json
  data/corpora/ami/meetings/<MEETING_ID>/mix.wav        16 kHz mono headset mix (gitignored)
  <hyps root>/<MEETING_ID>/hyp.json                     pipeline output per meeting
command:
  python -m evals batch --refs data/corpora/ami/meetings --hyps out/ami \\
      --gates evals/gates.yaml --suite ami-headset --report out/ami/report.json"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _lib_versions() -> dict[str, str]:
    out: dict[str, str] = {"evals": __version__}
    for name in ("pyannote.metrics", "pyannote.core", "jiwer", "meeteval", "numpy"):
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "missing"
    return out


def _normalizer(args: argparse.Namespace) -> Normalizer:
    return Normalizer(
        expand_contractions=bool(getattr(args, "expand_contractions", False)),
        numbers_to_words=bool(getattr(args, "numbers_to_words", False)),
    )


def _score_options(args: argparse.Namespace) -> dict[str, Any]:
    return dict(
        normalizer=_normalizer(args),
        der_collar=args.der_collar,
        skip_overlap=args.skip_overlap,
        tcp_collar=args.tcp_collar,
    )


def _resolve_suite(args: argparse.Namespace) -> Suite | None:
    if not getattr(args, "suite", None):
        return None
    suites = load_gates(args.gates)
    return get_suite(suites, args.suite)


def _document(kind: str, body: dict[str, Any], gates: Any) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "libraries": _lib_versions(),
        **body,
        "gates": gates,
    }


def _write_json(path: str | None, doc: dict[str, Any]) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _write_text(path: str | None, text: str) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _headline(report: MeetingReport) -> str:
    flat = report.flat()
    return "  ".join(f"{label} {format_value(flat[key])}" for label, key in report.HEADLINE[:4])


def _collar(text: str) -> float:
    """argparse type: a finite collar >= 0 (pyannote reads a negative collar as
    0, meeteval's CLI refuses one; the report must not record a value that was
    not applied)."""
    try:
        v = float(text)
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"collar must be a finite number >= 0, got {text!r}")
    return v


def _add_metric_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--der-collar",
        type=_collar,
        default=DEFAULT_DER_COLLAR,
        help="DER/JER collar, pyannote semantics: total no-score width centred on each reference "
        f"boundary (default {DEFAULT_DER_COLLAR} = ±{DEFAULT_DER_COLLAR / 2:g} s)",
    )
    p.add_argument("--skip-overlap", action="store_true", help="do not score reference overlap regions")
    p.add_argument(
        "--tcp-collar",
        type=_collar,
        default=DEFAULT_TCP_COLLAR,
        help=f"tcpWER collar in seconds (meeteval semantics, default {DEFAULT_TCP_COLLAR})",
    )
    p.add_argument("--expand-contractions", action="store_true", help="normalise contractions (don't -> do not)")
    p.add_argument("--numbers-to-words", action="store_true", help="normalise digits to words (12 -> twelve)")


def _add_gate_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gates", default=str(DEFAULT_GATES_PATH), help="gates.yaml (default: evals/gates.yaml)")
    p.add_argument("--suite", help="suite name inside the gates file; omit to skip gating")


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #


def cmd_score(args: argparse.Namespace) -> int:
    try:
        meeting = load_meeting(args.ref)
        hyp = load_hypothesis(args.hyp)
        suite = _resolve_suite(args)
    except (ContractError, GateConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    try:
        report = score_meeting(meeting, hyp, **_score_options(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    gate: GateResult | None = evaluate(suite, report.flat(), target=meeting.meeting_id) if suite else None
    doc = _document("meeting", {"report": report.to_dict()}, gate.to_dict() if gate else None)
    _write_json(args.report, doc)
    md = report.to_markdown()
    if gate:
        md += "\n" + gate.summary() + "\n"
    _write_text(args.md, md)

    if not args.quiet:
        print(f"{meeting.meeting_id} [{hyp.system}]  {_headline(report)}")
        if gate:
            print(gate.summary())
    return gate.exit_code if gate else EXIT_OK


# --------------------------------------------------------------------------- #
# batch
# --------------------------------------------------------------------------- #


def find_meetings(root: Path) -> list[Path]:
    """Meeting directories under ``root``: every directory holding a meeting.json."""
    return sorted(p.parent for p in root.rglob("meeting.json"))


def find_hypothesis(hyps_root: Path, refs_root: Path, meeting_dir: Path, meeting_id: str) -> Path | None:
    """HARNESS_POLICY: hypothesis lookup order for a meeting.

    1. ``<hyps>/<relative meeting dir>/hyp.json``
    2. ``<hyps>/<meeting dir name>/hyp.json``
    3. ``<hyps>/<meeting_id>/hyp.json``
    4. ``<hyps>/<meeting_id>.hyp.json``
    5. ``<hyps>/<meeting_id>.json``
    """
    rel = meeting_dir.relative_to(refs_root)
    candidates = [
        hyps_root / rel / "hyp.json",
        hyps_root / meeting_dir.name / "hyp.json",
        hyps_root / meeting_id / "hyp.json",
        hyps_root / f"{meeting_id}.hyp.json",
        hyps_root / f"{meeting_id}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def batch_markdown(result: BatchResult, gates: Sequence[GateResult]) -> str:
    lines = ["# Batch evaluation", "", f"{len(result.reports)} meeting(s) scored", ""]
    header = ["meeting", "system"] + [label for label, _ in MeetingReport.HEADLINE]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for r in result.reports:
        flat = r.flat()
        lines.append(
            "| " + " | ".join([r.meeting_id, r.system] + [format_value(flat[k]) for _, k in MeetingReport.HEADLINE]) + " |"
        )
    for name, agg in (("macro", result.macro), ("micro", result.micro)):
        lines.append(
            "| " + " | ".join([f"**{name}**", ""] + [format_value(agg.get(k)) for _, k in MeetingReport.HEADLINE]) + " |"
        )
    if result.missing:
        lines += ["", "Missing hypotheses:", ""] + [f"- {m['meeting_id']} ({m['meeting_dir']})" for m in result.missing]
    if result.errors:
        lines += ["", "Errors:", ""] + [f"- {e['meeting_dir']}: {e['error']}" for e in result.errors]
    if gates:
        lines += ["", "Gates:", ""] + [f"- {g.summary()}" for g in gates]
    lines.append("")
    return "\n".join(lines)


def cmd_batch(args: argparse.Namespace) -> int:
    refs_root = Path(args.refs)
    hyps_root = Path(args.hyps)
    # The suite is resolved first: a mistyped --suite is an input error even
    # while the corpus is absent (otherwise the typo hides behind exit 3).
    try:
        suite = _resolve_suite(args)
    except GateConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if not refs_root.is_dir():
        print(f"skipped: reference root {refs_root} does not exist — nothing to score.", file=sys.stderr)
        print(AMI_EXPECTED_LAYOUT, file=sys.stderr)
        return EXIT_SKIPPED
    meeting_dirs = find_meetings(refs_root)
    if not meeting_dirs:
        print(f"skipped: no meeting.json found under {refs_root} — nothing to score.", file=sys.stderr)
        print(AMI_EXPECTED_LAYOUT, file=sys.stderr)
        return EXIT_SKIPPED

    result = BatchResult(reports=[])
    options = _score_options(args)
    for mdir in meeting_dirs:
        # One meeting's failure -- whatever it is -- is recorded against that
        # meeting and the batch goes on, so the report is always written.
        try:
            meeting = load_meeting(mdir)
        except ContractError as exc:
            result.errors.append({"meeting_dir": str(mdir), "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"meeting_dir": str(mdir), "error": f"unexpected {type(exc).__name__}: {exc}"})
            continue
        hyp_path = find_hypothesis(hyps_root, refs_root, mdir, meeting.meeting_id)
        if hyp_path is None:
            result.missing.append({"meeting_id": meeting.meeting_id, "meeting_dir": str(mdir)})
            continue
        try:
            hyp = load_hypothesis(hyp_path)
            result.reports.append(score_meeting(meeting, hyp, **options))
        except (ContractError, ValueError) as exc:
            result.errors.append({"meeting_dir": str(mdir), "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"meeting_dir": str(mdir), "error": f"unexpected {type(exc).__name__}: {exc}"})

    gates: list[GateResult] = []
    if suite and result.reports:
        if args.gate_on in ("macro", "both"):
            gates.append(evaluate(suite, result.macro, target="macro"))
        if args.gate_on in ("micro", "both"):
            gates.append(evaluate(suite, result.micro, target="micro"))
        if args.gate_on == "each":
            gates.extend(evaluate(suite, r.flat(), target=r.meeting_id) for r in result.reports)

    doc = _document("batch", result.to_dict(), [g.to_dict() for g in gates] if suite else None)
    _write_json(args.report, doc)
    _write_text(args.md, batch_markdown(result, gates))

    if not args.quiet:
        for r in result.reports:
            print(f"{r.meeting_id} [{r.system}]  {_headline(r)}")
        for name, agg in (("macro", result.macro), ("micro", result.micro)):
            if result.reports:
                print(
                    f"{name:<6} n={agg['n']}  "
                    + "  ".join(f"{label} {format_value(agg.get(k))}" for label, k in MeetingReport.HEADLINE[:4])
                )
        for m in result.missing:
            print(f"missing hypothesis: {m['meeting_id']} ({m['meeting_dir']})", file=sys.stderr)
        for e in result.errors:
            print(f"error: {e['meeting_dir']}: {e['error']}", file=sys.stderr)
        for g in gates:
            print(g.summary())

    if result.errors:
        return EXIT_INPUT_ERROR
    if result.missing and args.missing == "fail":
        return EXIT_INPUT_ERROR
    if not result.reports:
        # Meetings exist but none has a hypothesis: the pipeline produced
        # nothing.  That is an input error even under --missing skip -- it must
        # not read as "skipped" (3), which a CI step may treat as benign.
        print(
            f"error: none of the {len(meeting_dirs)} meeting(s) has a hypothesis under {hyps_root} "
            "— nothing was scored.",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
    if any(not g.passed for g in gates):
        return EXIT_GATE_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------- #
# validate / suites / selfcheck
# --------------------------------------------------------------------------- #


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.ref and not args.hyp:
        print("error: give --ref and/or --hyp", file=sys.stderr)
        return EXIT_INPUT_ERROR
    rc = EXIT_OK
    meeting: Meeting | None = None
    if args.ref:
        try:
            meeting = load_meeting(args.ref)
            print(
                f"ok: {meeting.path} — meeting_id {meeting.meeting_id}, {len(meeting.segments)} segments, "
                f"{len(meeting.active_speakers)}/{len(meeting.speaker_ids)} speakers active, {meeting.duration_s} s"
            )
        except ContractError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            rc = EXIT_INPUT_ERROR
    if args.hyp:
        try:
            hyp = load_hypothesis(args.hyp)
            print(f"ok: {hyp.path} — meeting_id {hyp.meeting_id}, system {hyp.system}, {len(hyp.segments)} segments")
            if meeting and hyp.meeting_id != meeting.meeting_id:
                print(f"invalid: hypothesis meeting_id {hyp.meeting_id!r} != reference {meeting.meeting_id!r}", file=sys.stderr)
                rc = EXIT_INPUT_ERROR
        except ContractError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            rc = EXIT_INPUT_ERROR
    return rc


def cmd_suites(args: argparse.Namespace) -> int:
    try:
        suites = load_gates(args.gates)
    except GateConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    for name, suite in suites.items():
        print(f"{name}: {' '.join(suite.description.split())}")
        for c in suite.checks:
            print(f"    {c.describe()}")
    return EXIT_OK


def selfcheck_meeting() -> Meeting:
    """A hand-built two-speaker meeting with word timings (synthetic, no audio)."""

    def seg(spk: str, start: float, words: Sequence[str]) -> Segment:
        step = 0.4
        ws = tuple(Word(w, start + i * step, start + i * step + 0.3) for i, w in enumerate(words))
        return Segment(spk, start, start + len(words) * step, " ".join(words), ws)

    segments = [
        seg("spk0", 0.5, ["good", "morning", "everyone", "shall", "we", "start"]),
        seg("spk1", 3.4, ["yes", "let", "us", "begin"]),
        seg("spk0", 5.2, ["first", "item", "is", "the", "budget"]),
        seg("spk1", 7.0, ["i", "have", "the", "numbers", "here"]),
    ]
    return Meeting(
        meeting_id="selfcheck-0001",
        sample_rate=16000,
        duration_s=10.0,
        channels=1,
        speakers=[
            {"id": "spk0", "voice": "synthetic-a", "position_m": [0.0, 0.0, 1.2]},
            {"id": "spk1", "voice": "synthetic-b", "position_m": [1.0, 0.5, 1.2]},
        ],
        segments=segments,
        audio={"mix_wav": "mix.wav", "stems": {}, "device": {}},
        generator={"name": "evals.selfcheck", "version": __version__, "seed": 0, "scenario": {"kind": "selfcheck"}},
    )


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """Write a hand-built meeting, score it against itself, require the oracle suite."""
    with tempfile.TemporaryDirectory(prefix="evals-selfcheck-") as tmp:
        root = Path(tmp)
        meeting = selfcheck_meeting()
        mdir = root / "refs" / meeting.meeting_id
        write_meeting(meeting, mdir)
        write_reference_files(meeting, mdir)
        hyp = Hypothesis(meeting.meeting_id, "oracle", list(meeting.segments))
        write_hypothesis_files(hyp, root / "hyps" / meeting.meeting_id)
        # Round-trip through disk so the loaders are part of the check.
        loaded_meeting = load_meeting(mdir)
        loaded_hyp = load_hypothesis(root / "hyps" / meeting.meeting_id / "hyp.json")
        report = score_meeting(loaded_meeting, loaded_hyp, der_collar=args.der_collar, tcp_collar=args.tcp_collar)
        try:
            suite = get_suite(load_gates(args.gates), "oracle")
        except GateConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        gate = evaluate(suite, report.flat(), target=meeting.meeting_id)
        doc = _document("meeting", {"report": report.to_dict()}, gate.to_dict())
        _write_json(args.report, doc)
        print(f"selfcheck {meeting.meeting_id}  {_headline(report)}")
        print(gate.summary())
        return gate.exit_code


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"evals {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("score", help="score one hyp.json against one meeting directory")
    p.add_argument("--ref", required=True, help="meeting directory (or meeting.json)")
    p.add_argument("--hyp", required=True, help="hyp.json (or a directory holding one)")
    _add_gate_options(p)
    p.add_argument("--report", help="write the JSON report here")
    p.add_argument("--md", help="write a Markdown summary here")
    _add_metric_options(p)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("batch", help="score every meeting under --refs against hypotheses under --hyps")
    p.add_argument("--refs", required=True, help="root holding meeting directories (searched recursively)")
    p.add_argument("--hyps", required=True, help="root holding hypotheses (see find_hypothesis for the lookup order)")
    _add_gate_options(p)
    p.add_argument("--gate-on", choices=("macro", "micro", "both", "each"), default="macro", help="which aggregate the suite is applied to (default macro)")
    p.add_argument("--missing", choices=("fail", "skip"), default="fail", help="a meeting without a hypothesis fails the run (default) or is skipped")
    p.add_argument("--report", help="write the JSON report here")
    p.add_argument("--md", help="write a Markdown summary here")
    _add_metric_options(p)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("validate", help="validate contract files without scoring")
    p.add_argument("--ref", help="meeting directory (or meeting.json)")
    p.add_argument("--hyp", help="hyp.json")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("suites", help="list the suites in a gates file")
    p.add_argument("--gates", default=str(DEFAULT_GATES_PATH))
    p.set_defaults(func=cmd_suites)

    p = sub.add_parser("selfcheck", help="score a built-in meeting against itself and require the oracle suite")
    p.add_argument("--gates", default=str(DEFAULT_GATES_PATH))
    p.add_argument("--report", help="write the JSON report here")
    p.add_argument("--der-collar", type=_collar, default=DEFAULT_DER_COLLAR)
    p.add_argument("--tcp-collar", type=_collar, default=DEFAULT_TCP_COLLAR)
    p.set_defaults(func=cmd_selfcheck)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ContractError, GateConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as exc:  # noqa: BLE001 -- exit 1 means "a gate failed"; a crash must not look like one
        traceback.print_exc(file=sys.stderr)
        print(
            f"error: unexpected {type(exc).__name__}: {exc} (exit {EXIT_INPUT_ERROR}; exit {EXIT_GATE_FAILED} "
            "is reserved for a failed gate)",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
