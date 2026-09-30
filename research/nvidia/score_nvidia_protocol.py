#!/usr/bin/env python3
"""DER/JER under NVIDIA's AMI protocol, beside the harness's own.

NVIDIA's Nemotron 3 Diarization card scores "AMI Test MHM" with collar 0,
overlap included, against the forced-alignment RTTMs of
nttcslab-sp/diar-forced-alignment (MFA on the BUT *only_words* setup), and
lets NeMo derive the scored region from the reference and hypothesis extents
(no UEM).  The harness scores against its own references (BUT only_words
from the NXT annotations) with pyannote.metrics collar 0.25 total width.
This script applies NVIDIA's protocol to any harness hypothesis so that the
harness's numbers can be checked against the card (9.25 % at the 30.4 s
setting) and every system is compared under both protocols.

Speaker labels of the forced-alignment RTTMs are ``<meeting>.<agent>``; the
harness meeting.json records each speaker's NXT agent letter, which maps
them.  pyannote.metrics' ``DiarizationErrorRate(collar=0.0,
skip_overlap=False)`` without a UEM uses the union of the reference and
hypothesis extents, as NeMo does.

Usage: score_nvidia_protocol.py --fa-dir DIR --meetings data/corpora/ami/test
         --hyps build/nvidia/test/hyp/ami --systems a b ... --out report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def annotation(turns, uri):
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri=uri)
    for i, (a, b, spk) in enumerate(turns):
        if b > a:
            ann[Segment(float(a), float(b)), i] = str(spk)
    return ann


def fa_reference(fa_dir: Path, meeting_dir: Path):
    m = json.loads((meeting_dir / "meeting.json").read_text())
    agent_to_id = {s["nxt_agent"]: s["id"] for s in m["speakers"] if "nxt_agent" in s}
    turns = []
    for line in (fa_dir / f"{m['meeting_id']}.rttm").read_text().splitlines():
        f = line.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            agent = f[7].split(".")[-1]
            turns.append((float(f[3]), float(f[3]) + float(f[4]), agent_to_id.get(agent, f[7])))
    return m["meeting_id"], turns


def hyp_turns(path: Path):
    d = json.loads(path.read_text())
    return [(float(s["start"]), float(s["end"]), str(s["speaker"])) for s in d["segments"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fa-dir", required=True, help="diar-forced-alignment/AMI/test")
    ap.add_argument("--meetings", required=True, help="harness AMI meeting dirs (meeting.json with nxt_agent)")
    ap.add_argument("--hyps", required=True, nargs="+", help="hyp roots: <root>/<system>/<meeting>/hyp.json")
    ap.add_argument("--systems", required=True, nargs="+")
    ap.add_argument("--collar", type=float, default=0.0, help="pyannote total width (NVIDIA: 0)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

    fa_dir = Path(args.fa_dir)
    refs = {}
    for md in sorted(p for p in Path(args.meetings).iterdir() if (p / "meeting.json").is_file()):
        mid, turns = fa_reference(fa_dir, md)
        refs[mid] = annotation(turns, mid)
    report = {"protocol": {"references": "nttcslab-sp/diar-forced-alignment AMI/test (MFA, BUT only_words)",
                           "collar_total_width_s": args.collar, "skip_overlap": False, "uem": "none (union of extents)",
                           "tool": "pyannote.metrics DiarizationErrorRate / JaccardErrorRate"},
              "meetings": sorted(refs), "systems": {}}
    for system in args.systems:
        der = DiarizationErrorRate(collar=args.collar, skip_overlap=False)
        jer = JaccardErrorRate(collar=args.collar, skip_overlap=False)
        per, missing = {}, []
        for mid, ref in refs.items():
            hp = next((Path(r) / system / mid / "hyp.json" for r in args.hyps if (Path(r) / system / mid / "hyp.json").is_file()), None)
            if hp is None:
                missing.append(mid)
                continue
            hyp = annotation(hyp_turns(hp), mid)
            comp = der(ref, hyp, detailed=True)
            j = jer(ref, hyp)
            per[mid] = {"der": comp["diarization error rate"], "miss": comp["missed detection"],
                        "false_alarm": comp["false alarm"], "confusion": comp["confusion"], "total": comp["total"],
                        "jer": j}
        if missing:
            report["systems"][system] = {"error": f"missing hypotheses: {missing}"}
            print(f"{system}: missing {len(missing)} meetings", file=sys.stderr)
            continue
        tot = sum(v["total"] for v in per.values())
        pooled = {k: sum(v[k] for v in per.values()) / tot for k in ("miss", "false_alarm", "confusion")}
        pooled["der"] = sum(pooled.values())
        report["systems"][system] = {
            "pooled": pooled, "macro_der": sum(v["der"] for v in per.values()) / len(per),
            "pooled_der_pyannote": abs(der), "macro_jer": sum(v["jer"] for v in per.values()) / len(per),
            "per_meeting": per}
        print(f"{system:40s} pooled DER {pooled['der']:.4f} (miss {pooled['miss']:.4f}, fa {pooled['false_alarm']:.4f}, "
              f"conf {pooled['confusion']:.4f})  macro {report['systems'][system]['macro_der']:.4f}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
