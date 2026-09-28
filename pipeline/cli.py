"""``python -m pipeline`` -- run a registered pipeline over audio.

    python -m pipeline list
    python -m pipeline run   --pipeline NAME --audio FILE [--meeting-dir DIR] --out hyp.json
                             [--param k=v ...] [--seed N] [--sample-rate HZ]
    python -m pipeline batch --pipeline NAME --root DIR --out DIR
                             [--audio-key mix|device|device.<name>|stem:<spk>|<relpath>]
                             [--param k=v ...] [--seed N] [--fail-fast]
    python -m pipeline import-stm --stm ref.stm --audio mix.wav --out DIR [--meeting-id ID]
    python -m pipeline models [--verify] [--json] [KEY ...]
    python -m pipeline fetch-models [KEY ...] [--force]

Exit codes (HARNESS_POLICY): 0 ok, 1 a run failed, 2 usage error (unknown
pipeline, unavailable pipeline, unknown ``--param`` key or bad value, missing
input).  ``main`` never lets an exception escape: anything unexpected is
printed and becomes exit 1.
``run`` writes hyp.json, hyp.rttm and hyp.stm side by side; ``batch``
writes ``<out>/<meeting_id>/hyp.*`` plus ``<out>/batch.json``.  A
meeting_id is one safe path component (pipeline.meeting.MEETING_ID_RE), a
batch refuses any output path outside ``--out``, and a second meeting with
an id already written in the same batch is reported as an error instead of
overwriting the first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .base import (
    ParamError,
    PipelineConfig,
    PipelineError,
    PipelineUnavailable,
    UnknownPipeline,
    describe_registry,
    get_pipeline,
)
from .formats import write_hypothesis_files
from .meeting import MEETING_FILENAME, meeting_from_stm, read_meeting, resolve_audio, write_meeting_dir


def parse_param(text: str) -> tuple[str, Any]:
    """``k=v`` with JSON coercion (1 -> int, 1.5 -> float, true -> bool, else str)."""
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"--param expects k=v, got {text!r}")
    k, v = text.split("=", 1)
    k = k.strip()
    if not k:
        raise argparse.ArgumentTypeError(f"--param has empty key: {text!r}")
    try:
        return k, json.loads(v)
    except ValueError:
        return k, v


def build_config(args: argparse.Namespace) -> PipelineConfig:
    params = dict(args.param or [])
    return PipelineConfig(
        name=args.pipeline, seed=int(args.seed), sample_rate=int(args.sample_rate), params=params
    )


def cmd_list(args: argparse.Namespace) -> int:
    rows = describe_registry()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        flag = "available  " if r["available"] else "UNAVAILABLE"
        kind = "self-test" if not r["system_under_test"] else "system   "
        print(f"{r['name']:24s} {flag} {kind} {r['description']}")
        if r["reason"]:
            print(f"{'':24s}   reason: {r['reason']}")
    return 0


def _get_pipeline(args: argparse.Namespace, cfg: PipelineConfig) -> Any:
    """get_pipeline with usage errors mapped to exit code 2 (returns an int)."""
    try:
        return get_pipeline(args.pipeline, cfg)
    except (UnknownPipeline, PipelineUnavailable, ParamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a factory that failed for another reason (model load, ...)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    pipe = _get_pipeline(args, cfg)
    if isinstance(pipe, int):
        return pipe
    audio = Path(args.audio)
    if not audio.is_file():
        print(f"error: audio not found: {audio}", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    try:
        hyp = pipe.run(audio, args.meeting_dir)
        paths = write_hypothesis_files(hyp, args.out)
    except ParamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # the documented exit-code contract holds for any failure
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - t0
    note = "" if pipe.is_system_under_test else "  [HARNESS SELF-TEST, not a system under test]"
    print(
        f"{args.pipeline}: {len(hyp.segments)} segments, {len(hyp.speakers)} speakers, "
        f"{elapsed:.2f}s -> {paths['json']} (+ .rttm, .stm){note}"
    )
    return 0


def iter_meeting_dirs(root: Path) -> list[Path]:
    """Every direct subdirectory that holds meeting.json (sorted), or root
    itself when it is a single meeting dir."""
    if (root / MEETING_FILENAME).is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / MEETING_FILENAME).is_file())


def _hyp_path(out: Path, meeting_id: str) -> Path:
    """``<out>/<meeting_id>/hyp.json``, refusing anything that leaves ``out``."""
    base = out.resolve()
    target = (out / meeting_id / "hyp.json").resolve()
    if base not in target.parents or target.parent == base:
        raise PipelineError(f"meeting_id {meeting_id!r} would write outside --out {out}")
    return out / meeting_id / "hyp.json"


def cmd_batch(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    pipe = _get_pipeline(args, cfg)
    if isinstance(pipe, int):
        return pipe
    root, out = Path(args.root), Path(args.out)
    if not root.is_dir():
        print(f"error: root is not a directory: {root}", file=sys.stderr)
        return 2
    dirs = iter_meeting_dirs(root)
    if not dirs:
        print(f"error: no meeting directories under {root}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    failures = 0
    written: dict[str, str] = {}  # meeting_id -> the dir whose hypothesis is at <out>/<id>/
    for md in dirs:
        row: dict[str, Any] = {"dir": str(md)}
        t0 = time.perf_counter()
        try:
            meeting = read_meeting(md)
            mid = str(meeting["meeting_id"])
            row["meeting_id"] = mid
            if mid in written:
                raise PipelineError(
                    f"duplicate meeting_id {mid!r}: {written[mid]} already wrote {out / mid}; "
                    "refusing to overwrite it"
                )
            target = _hyp_path(out, mid)
            audio = resolve_audio(md, meeting, args.audio_key)
            row["audio"] = str(audio)
            hyp = pipe.run(audio, md)
            if hyp.meeting_id != mid:
                raise PipelineError(f"hypothesis meeting_id {hyp.meeting_id!r} != meeting.json {mid!r}")
            paths = write_hypothesis_files(hyp, target)
            written[mid] = str(md)
            row.update(
                status="ok",
                hyp=str(paths["json"]),
                segments=len(hyp.segments),
                speakers=len(hyp.speakers),
            )
        except Exception as exc:  # one bad meeting must not sink the batch
            failures += 1
            row.update(status="error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            if args.fail_fast:
                rows.append(row)
                break
        finally:
            row["elapsed_s"] = round(time.perf_counter() - t0, 3)
        rows.append(row)
        print(f"{row.get('meeting_id', md.name)}: {row['status']}" + (f" ({row['error']})" if row["status"] == "error" else ""))
    index = {
        "schema": "plaud-harness/batch/1",
        "pipeline": args.pipeline,
        "system_under_test": pipe.is_system_under_test,
        "config": {"seed": cfg.seed, "sample_rate": cfg.sample_rate, "params": cfg.params},
        "root": str(root),
        "audio_key": args.audio_key,
        "meetings": rows,
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "failed": failures,
    }
    (out / "batch.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"batch: {index['ok']} ok, {failures} failed -> {out / 'batch.json'}")
    return 1 if failures else 0


def cmd_import_stm(args: argparse.Namespace) -> int:
    from .base import load_audio

    stm = Path(args.stm)
    audio = Path(args.audio)
    if not stm.is_file() or not audio.is_file():
        print("error: --stm and --audio must exist", file=sys.stderr)
        return 2
    try:
        loaded = load_audio(audio, int(args.sample_rate))
        rttm_text = Path(args.rttm).read_text() if args.rttm else None
        meeting = meeting_from_stm(
            stm.read_text(),
            args.meeting_id,
            sample_rate=loaded.sample_rate,
            duration_s=loaded.duration_s,
            channels=1,
            generator={
                "name": "pipeline.cli.import-stm",
                "version": "1",
                "seed": 0,
                "scenario": {
                    "stm": str(stm),
                    "rttm": str(args.rttm) if args.rttm else None,
                    "source_audio": str(audio),
                    "word_times": "interpolated (HARNESS_POLICY)",
                },
            },
        )
        if rttm_text is not None:
            # Segment times come from the STM; the RTTM is kept verbatim for
            # evals that want the original reference boundaries.
            meeting["generator"]["scenario"]["rttm_lines"] = len(rttm_text.splitlines())
        out = write_meeting_dir(args.out, meeting, loaded.pcm, loaded.sample_rate)
        if rttm_text is not None:
            (out / "ref.rttm").write_text(rttm_text if rttm_text.endswith("\n") else rttm_text + "\n")
    except (PipelineError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # exit-code contract: never let an exception escape
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    renamed = meeting["generator"]["scenario"].get("stm_file_id")
    note = f" (STM file id {renamed!r} renamed)" if renamed else ""
    print(f"imported {meeting['meeting_id']} -> {out} ({len(meeting['segments'])} segments){note}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Pinned model assets: presence (size check) or, with --verify, sha256.

    Exit codes: 2 an unknown key; with ``--verify``, 1 when ANY requested
    asset is missing or has a file whose sha256 differs from its pin (a check
    against an empty or wrong models dir must not pass); without it, 0.
    """
    from . import model_store

    keys = list(getattr(args, "keys", None) or [])
    unknown = [k for k in keys if k not in model_store.ASSETS]
    if unknown:
        print(f"error: unknown model asset(s) {unknown}; known: {sorted(model_store.ASSETS)}", file=sys.stderr)
        return 2
    rows = [r for r in model_store.status() if not keys or r["key"] in keys]
    rc = 0
    for r in rows:
        if not args.verify:
            continue
        if not r["present"]:
            r["verified"] = False
            rc = 1
            continue
        problems = model_store.verify(r["key"])
        r["verified"] = not problems
        if problems:
            r["reason"] = "; ".join(problems)
            rc = 1
    if args.json:
        print(json.dumps({"models_dir": str(model_store.models_dir()), "assets": rows}, indent=2))
        return rc
    print(f"models dir: {model_store.models_dir()}")
    for r in rows:
        state = "present " if r["present"] else "MISSING "
        if "verified" in r and r["present"]:
            state = "verified" if r["verified"] else "BAD SHA "
        print(f"{r['key']:34s} {state} {r['bytes'] / 1e6:8.1f} MB  {r['licence']}")
        if r["reason"]:
            print(f"{'':34s}   {r['reason']}")
    return rc


def cmd_fetch_models(args: argparse.Namespace) -> int:
    """Download pinned assets (no token, sha256-checked); keep verified files."""
    from . import model_store

    keys = args.keys or sorted(model_store.ASSETS)
    unknown = [k for k in keys if k not in model_store.ASSETS]
    if unknown:
        print(f"error: unknown model asset(s) {unknown}; known: {sorted(model_store.ASSETS)}", file=sys.stderr)
        return 2
    for k in keys:
        try:
            path = model_store.fetch(k, force=args.force)
        except Exception as exc:  # network, sha mismatch, disk
            print(f"error: {k}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"{k}: ok -> {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="show registered pipelines and availability")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pipeline", required=True)
        p.add_argument("--param", action="append", type=parse_param, metavar="K=V")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--sample-rate", type=int, default=16000, help="loader target rate (default: 16000, the recorder's rate)")

    p_run = sub.add_parser("run", help="run one pipeline on one audio file")
    common(p_run)
    p_run.add_argument("--audio", required=True)
    p_run.add_argument("--meeting-dir", default=None)
    p_run.add_argument("--out", required=True, help="hyp.json path; hyp.rttm/hyp.stm are written beside it")
    p_run.set_defaults(func=cmd_run)

    p_batch = sub.add_parser("batch", help="run one pipeline over a directory of meeting dirs")
    common(p_batch)
    p_batch.add_argument("--root", required=True)
    p_batch.add_argument("--out", required=True)
    p_batch.add_argument("--audio-key", default="mix", help="mix | device | device.<name> | stem:<spk> | <relpath>")
    p_batch.add_argument("--fail-fast", action="store_true")
    p_batch.set_defaults(func=cmd_batch)

    p_imp = sub.add_parser("import-stm", help="build a meeting dir from an STM reference + audio")
    p_imp.add_argument("--stm", required=True)
    p_imp.add_argument("--rttm", default=None)
    p_imp.add_argument("--audio", required=True)
    p_imp.add_argument("--out", required=True)
    p_imp.add_argument(
        "--meeting-id",
        default=None,
        help="select this file id from a multi-meeting STM, or rename a single-meeting STM",
    )
    p_imp.add_argument("--sample-rate", type=int, default=16000)
    p_imp.set_defaults(func=cmd_import_stm)

    p_models = sub.add_parser("models", help="list the pinned model assets and whether they are present")
    p_models.add_argument("--verify", action="store_true", help="hash every present file against its pinned sha256")
    p_models.add_argument("--json", action="store_true")
    p_models.add_argument("keys", nargs="*", help="asset keys (default: all); --verify exits 1 if any is missing or bad")
    p_models.set_defaults(func=cmd_models)

    p_fetch = sub.add_parser("fetch-models", help="download pinned model assets into the models dir (no token)")
    p_fetch.add_argument("keys", nargs="*", help="asset keys (default: all)")
    p_fetch.add_argument("--force", action="store_true", help="re-download even when the pinned sha256 matches")
    p_fetch.set_defaults(func=cmd_fetch_models)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:  # argparse uses 2 for usage errors already
        return int(exc.code or 0)
    try:
        return int(args.func(args))
    except Exception as exc:  # last line of the exit-code contract
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
