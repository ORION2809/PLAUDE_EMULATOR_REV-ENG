"""Command line: python -m generator {make,batch,presets,backends,validate}.

    make     --scenario <preset|yaml|json> --out DIR [--seed N] [--set k.v=x ...]
    batch    --n K --out DIR [--scenario ...] [--seed N] [--set ...]
    presets  list the built-in scenario presets (as YAML with --yaml)
    backends report which TTS backends are usable offline
    validate DIR   check a meeting directory against the contract, its RTTM/STM
                   against meeting.json, and stems/, device/ and mics.wav for
                   files meeting.json does not list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generator.contract import load_meeting, parse_rttm, parse_stm
from generator.scenario import PRESETS, load_scenario, parse_override


def _overrides(items: list[str] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in items or []:
        key, value = parse_override(item)
        out[key] = value
    return out


def cmd_make(args: argparse.Namespace) -> int:
    from generator.export import write_meeting
    from generator.meeting import generate_meeting

    overrides = _overrides(args.set)
    if args.seed is not None:
        overrides["seed"] = int(args.seed)
    scenario = load_scenario(args.scenario, overrides)
    meeting = generate_meeting(scenario)
    payload = write_meeting(meeting, args.out)
    print(json.dumps({
        "meeting_id": payload["meeting_id"],
        "out": str(Path(args.out)),
        "duration_s": payload["duration_s"],
        "n_turns": payload["turn_taking"]["n_turns"],
        "overlap_ratio_realised": payload["turn_taking"]["overlap_ratio_realised"],
        "device_outputs": payload["audio"]["device"],
    }))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    from generator.export import write_meeting
    from generator.meeting import generate_meeting

    overrides = _overrides(args.set)
    base_seed = int(args.seed)
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    index = []
    for k in range(int(args.n)):
        ov = dict(overrides)
        ov["seed"] = base_seed + k
        scenario = load_scenario(args.scenario, ov)
        meeting = generate_meeting(scenario)
        payload = write_meeting(meeting, root / meeting.meeting_id)
        index.append({"meeting_id": payload["meeting_id"], "dir": meeting.meeting_id, "seed": scenario.seed, "duration_s": payload["duration_s"]})
        print(json.dumps(index[-1]))
    (root / "index.json").write_text(json.dumps({"schema": "plaud-harness/synthetic-set/1", "scenario": args.scenario, "meetings": index}, indent=2, sort_keys=True) + "\n")
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    for name, scenario in PRESETS.items():
        if args.yaml:
            print(f"# --- {name} ---")
            print(scenario.to_yaml())
        else:
            print(f"{name:18s} speakers={scenario.n_speakers} duration={scenario.duration_s:.0f}s device={scenario.device.preset} "
                  f"model={scenario.turn_taking.model} overlap={scenario.turn_taking.overlap_ratio} noise={scenario.noise.kind}")
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    from generator.tts import available_backends

    for name, status in available_backends().items():
        print(f"{name:8s} {status}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    meeting = load_meeting(d)
    rttm = parse_rttm((d / "ref.rttm").read_text())
    stm = parse_stm((d / "ref.stm").read_text())
    segs = meeting["segments"]
    problems = []
    if len(rttm) != len(segs) or len(stm) != len(segs):
        problems.append("ref.rttm / ref.stm line counts differ from meeting.json segments")
    for i, (seg, r, s) in enumerate(zip(segs, rttm, stm)):
        if (r["speaker"], r["start"], r["end"]) != (seg["speaker"], seg["start"], seg["end"]):
            problems.append(f"segment {i}: ref.rttm disagrees with meeting.json")
        if (s["speaker"], s["start"], s["end"], s["text"]) != (seg["speaker"], seg["start"], seg["end"], seg["text"]):
            problems.append(f"segment {i}: ref.stm disagrees with meeting.json")
    from generator.export import unlisted_files

    audio = meeting["audio"]
    listed = [audio["mix_wav"], *audio["stems"].values(), *audio["device"].values()]
    listed += [rel for rel in (audio.get("mics_wav"), (audio.get("activity") or {}).get("path")) if rel]
    for rel in listed:
        if not (d / rel).is_file():
            problems.append(f"missing file {rel}")
    for rel in unlisted_files(d, meeting):
        problems.append(f"unlisted file {rel} (not in meeting.json; left by another meeting?)")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"ok: {meeting['meeting_id']} {len(segs)} segments {meeting['duration_s']} s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m generator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("make", help="generate one meeting directory")
    mk.add_argument("--scenario", default="default", help="preset name or path to .yaml/.json")
    mk.add_argument("--out", required=True)
    mk.add_argument("--seed", type=int, default=None)
    mk.add_argument("--set", action="append", metavar="KEY=VALUE", help="dotted scenario override, repeatable")
    mk.set_defaults(func=cmd_make)

    bt = sub.add_parser("batch", help="generate K meetings with consecutive seeds")
    bt.add_argument("--n", type=int, required=True)
    bt.add_argument("--out", required=True)
    bt.add_argument("--scenario", default="default")
    bt.add_argument("--seed", type=int, default=1)
    bt.add_argument("--set", action="append", metavar="KEY=VALUE")
    bt.set_defaults(func=cmd_batch)

    pr = sub.add_parser("presets", help="list built-in scenarios")
    pr.add_argument("--yaml", action="store_true")
    pr.set_defaults(func=cmd_presets)

    bk = sub.add_parser("backends", help="report TTS backend availability")
    bk.set_defaults(func=cmd_backends)

    va = sub.add_parser("validate", help="validate a meeting directory")
    va.add_argument("dir")
    va.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


__all__ = ["build_parser", "main"]
