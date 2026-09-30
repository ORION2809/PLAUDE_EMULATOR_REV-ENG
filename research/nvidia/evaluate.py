#!/usr/bin/env python3
"""Score the NVIDIA comparison (docs/nvidia-speech.md) from saved hypotheses.

For each evaluation set (``test`` = AMI test split, ``nsf-sc`` / ``nsf-ct`` =
NOTSOFAR-1 eval-full-only far-field / close-talk mix):

1. ``-turns`` views: the raw diarization turns of every composed run
   (``extra.diarization.turns``), so diarizers are scored on their own turns;
2. the ASR x diarizer matrix: every transcript source's words on every
   diarizer's turns, with the harness rule (research/nvidia/recombine.py; no
   model runs);
3. ``python -m evals batch`` on every system (harness protocol: DER/JER collar
   0.25 total width, cpWER/tcpWER/WER with meeteval);
4. for the AMI test split, DER/JER under NVIDIA's protocol as well
   (research/nvidia/score_nvidia_protocol.py);
5. ``build/nvidia/summary.{json,md}``: accuracy per system, with the timing
   and memory the runs recorded (under contention; speed.sh measures quietly).

Anything whose inputs do not exist yet is skipped, so this can be re-run as
the queues fill.  ``--only-summary`` rebuilds the tables from existing reports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PY = sys.executable
B = ROOT / "build"

# words sources: name -> (hyp dir, asr cache dir)   | turns sources: name -> hyp dir
SETS = {
    "test": {
        "refs": ROOT / "data/corpora/ami/test",
        "out": B / "nvidia/test",
        "systems": {  # scored as they are (composed runs, diarizer-only runs)
            "nemotron": B / "nvidia/test/hyp/ami/nemotron",
            "nemotron-tagged": B / "nvidia/test/hyp/ami/nemotron-tagged",
            "faster-whisper+nemotron-diar": B / "nvidia/test/hyp/ami/faster-whisper+nemotron-diar",
            "nemo-diar-v3offline": B / "nvidia/test/hyp/ami/nemo-diar-v3offline",
            "sortformer-v2": B / "nvidia/test/hyp/ami/sortformer-v2",
            "whisper-pyannote": B / "v5-test/hyp/ami/whisper-pyannote",
            "pyannote-audio": B / "v5-test/hyp/ami/pyannote-audio",
            "whisper-sherpa-cal": B / "v5-test/hyp/ami/whisper-sherpa-cal",
            "whisper-sherpa-ecapa": B / "v5-test/hyp/ami/whisper-sherpa-ecapa",
        },
        "words": {
            "whisper": (B / "v5-test/hyp/ami/whisper-sherpa-cal", B / "v5-test/asr-cache"),
            "whisper-cpp": (B / "nvidia/test/hyp/ami/whisper-cpp", B / "nvidia/test/asr-cache-wcpp"),
            "nemotron35": (B / "nvidia/test/hyp/ami/nemotron", B / "nvidia/test/asr-cache"),
            "nemotron35-rc13": (B / "nvidia/test/hyp/ami/nemotron35-rc13", B / "nvidia/test/asr-cache-nemo"),
            "nemotron-en-rc13": (B / "nvidia/test/hyp/ami/nemotron-en-rc13", B / "nvidia/test/asr-cache-nemo"),
            "parakeet-tdt": (B / "nvidia/test/hyp/ami/parakeet-tdt", B / "nvidia/test/asr-cache-nemo"),
        },
        "turns": {
            "nemotron-diar": B / "nvidia/test/hyp/ami/nemotron",
            "nemotron-diar-v3offline": B / "nvidia/test/hyp/ami/nemo-diar-v3offline",
            "sortformer-v2": B / "nvidia/test/hyp/ami/sortformer-v2",
            "pyannote-exclusive": B / "v5-test/hyp/ami/whisper-pyannote",
            "pyannote": B / "v5-test/hyp/ami/pyannote-audio",
            "sherpa-cal": B / "v5-test/hyp/ami/whisper-sherpa-cal",
            "ecapa": B / "v5-test/hyp/ami/whisper-sherpa-ecapa",
        },
        "fa_refs": Path.home() / "mivi-toolchain/deps/src/diar-forced-alignment/AMI/test",
    },
}
for variant, tag in (("sc", "nsf-sc"), ("ctmix", "nsf-ct")):
    o = B / f"nvidia/notsofar-{variant}"
    h = o / "hyp/ami"
    SETS[tag] = {
        "refs": ROOT / f"data/corpora/notsofar/{variant}",
        "out": o,
        "systems": {s: h / s for s in ("nemotron", "nemotron-tagged", "whisper-pyannote", "pyannote-audio",
                                       "whisper-sherpa-cal", "sortformer-v2")},
        "words": {
            "whisper": (h / "whisper-sherpa-cal", o / "asr-cache-whisper"),
            "whisper-cpp": (h / "whisper-cpp", o / "asr-cache-wcpp"),
            "nemotron35": (h / "nemotron", o / "asr-cache-nemo"),
            "nemotron35-rc13": (h / "nemotron35-rc13", o / "asr-cache-nemo"),
            "nemotron-en-rc13": (h / "nemotron-en-rc13", o / "asr-cache-nemo"),
        },
        "turns": {
            "nemotron-diar": h / "nemotron",
            "sortformer-v2": h / "sortformer-v2",
            "pyannote-exclusive": h / "whisper-pyannote",
            "pyannote": h / "pyannote-audio",
            "sherpa-cal": h / "whisper-sherpa-cal",
        },
        "fa_refs": None,
    }


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def complete(d: Path, refs: Path) -> bool:
    want = {p.name for p in refs.iterdir() if (p / "meeting.json").is_file()}
    have = {p.parent.name for p in d.glob("*/hyp.json")} if d.is_dir() else set()
    return bool(want) and want <= have


def derive_turns(src: Path, dest: Path) -> bool:
    """<system>-turns: the raw extra.diarization.turns as a text-free hypothesis."""
    n = 0
    for h in sorted(src.glob("*/hyp.json")):
        d = json.loads(h.read_text())
        turns = (d.get("extra") or {}).get("diarization", {}).get("turns")
        if turns is None:
            return False
        segs = [{"speaker": str(s), "start": float(a), "end": float(b), "text": ""} for a, b, s in turns if float(b) > float(a)]
        segs.sort(key=lambda g: (g["start"], g["end"], g["speaker"]))
        doc = {"schema": "plaud-harness/hypothesis/1", "meeting_id": d["meeting_id"], "system": f"{d['system']}:diarization-turns",
               "segments": segs, "extra": {"derived_from": rel(h), "derived_from_sha256":
                                           hashlib.sha256(h.read_bytes()).hexdigest(),
                                           "components": (d.get("extra") or {}).get("components"),
                                           "note": "raw diarization turns from extra.diarization.turns; DER/JER only"}}
        (dest / h.parent.name).mkdir(parents=True, exist_ok=True)
        (dest / h.parent.name / "hyp.json").write_text(json.dumps(doc) + "\n")
        n += 1
    return n > 0


def score(refs: Path, hyps: Path, report: Path, force: bool) -> dict | None:
    if report.is_file() and not force and report.stat().st_mtime >= max(p.stat().st_mtime for p in hyps.glob("*/hyp.json")):
        return json.loads(report.read_text())
    report.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([PY, "-m", "evals", "batch", "--refs", str(refs), "--hyps", str(hyps), "--report", str(report), "--quiet"],
                       cwd=ROOT, capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    if not report.is_file():
        print(f"  ! evals failed for {hyps}: {p.stderr[-600:]}", file=sys.stderr)
        return None
    return json.loads(report.read_text())


def run_timing(hyps: Path) -> dict:
    """Mean RTF / peak memory the runs recorded (under contention)."""
    rtf, rss_asr, rss_diar, audio = [], [], [], 0.0
    for h in hyps.glob("*/hyp.json"):
        e = json.loads(h.read_text()).get("extra") or {}
        t = e.get("timing") or {}
        if t.get("rtf") is not None:
            rtf.append(float(t["rtf"]))
        audio += float(t.get("audio_s") or 0)
        for key, acc in (("asr", rss_asr), ("diarization", rss_diar)):
            cli = (e.get(key) or {}).get("cli") or {}
            if cli.get("max_rss_bytes"):
                acc.append(cli["max_rss_bytes"])
    return {"mean_rtf": sum(rtf) / len(rtf) if rtf else None, "audio_h": audio / 3600,
            "max_asr_rss_mb": max(rss_asr) / 2**20 if rss_asr else None,
            "max_diar_rss_mb": max(rss_diar) / 2**20 if rss_diar else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", nargs="*", default=list(SETS))
    ap.add_argument("--force", action="store_true", help="rescore even if the report is newer than the hypotheses")
    ap.add_argument("--only-summary", action="store_true")
    args = ap.parse_args()
    summary: dict = {"schema": "plaud-harness/nvidia-summary/1", "sets": {}}
    for name in args.sets:
        cfg = SETS[name]
        refs, out = cfg["refs"], cfg["out"]
        if not refs.is_dir():
            continue
        matrix, derived = out / "hyp-matrix", out / "hyp-derived"
        systems: dict[str, Path] = {}
        for sysname, d in cfg["systems"].items():
            if complete(d, refs):
                systems[sysname] = d
                if not args.only_summary and derive_turns(d, derived / f"{sysname}-turns"):
                    systems[f"{sysname}-turns"] = derived / f"{sysname}-turns"
                elif (derived / f"{sysname}-turns").is_dir():
                    systems[f"{sysname}-turns"] = derived / f"{sysname}-turns"
        for wname, (wdir, cache) in cfg["words"].items():
            if not complete(wdir, refs):
                continue
            for tname, tdir in cfg["turns"].items():
                if not complete(tdir, refs):
                    continue
                dest = matrix / f"{wname}+{tname}"
                if not args.only_summary:
                    subprocess.run([PY, str(ROOT / "research/nvidia/recombine.py"), "--words", str(wdir), "--asr-cache", str(cache),
                                    "--turns", str(tdir), "--out", str(dest), "--system", f"{wname}+{tname}"],
                                   cwd=ROOT, check=True, capture_output=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
                systems[f"{wname}+{tname}"] = dest
        rows = {}
        for sysname, d in sorted(systems.items()):
            print(f"[evaluate] {name}: {sysname}", flush=True)
            rep = score(refs, d, out / "reports" / f"{sysname}.json", args.force) if not args.only_summary else (
                json.loads((out / "reports" / f"{sysname}.json").read_text()) if (out / "reports" / f"{sysname}.json").is_file() else None)
            if not rep:
                continue
            m = rep["macro"]
            rows[sysname] = {k: m.get(k) for k in ("der.der", "der.miss_rate", "der.false_alarm_rate", "der.confusion_rate",
                                                     "jer.jer", "cpwer.error_rate", "tcpwer.error_rate", "wer_concat.wer",
                                                     "speaker_count.error", "speaker_count.abs_error")}
            rows[sysname]["n"] = m.get("n")
            rows[sysname]["path"] = rel(d)
            if sysname in cfg["systems"]:
                rows[sysname]["recorded_timing"] = run_timing(d)
        nv = None
        if name == "test" and cfg.get("fa_refs") and Path(cfg["fa_refs"]).is_dir() and not args.only_summary:
            turn_systems = [s for s in systems if s.endswith("-turns") or s in ("pyannote-audio", "nemo-diar-v3offline", "sortformer-v2")]
            roots = sorted({str(systems[s].parent) for s in turn_systems})
            nv_path = out / "reports" / "nvidia-protocol.json"
            subprocess.run([PY, str(ROOT / "research/nvidia/score_nvidia_protocol.py"), "--fa-dir", str(cfg["fa_refs"]),
                            "--meetings", str(refs), "--hyps", *roots, "--systems", *[systems[s].name for s in turn_systems],
                            "--out", str(nv_path)], cwd=ROOT, check=False, capture_output=True,
                           env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
        if (out / "reports" / "nvidia-protocol.json").is_file():
            nv = json.loads((out / "reports" / "nvidia-protocol.json").read_text())
            nv = {s: {"pooled_der": v.get("pooled", {}).get("der"), "macro_der": v.get("macro_der"), "pooled": v.get("pooled")}
                  for s, v in nv["systems"].items() if "error" not in v}
        summary["sets"][name] = {"refs": rel(refs), "systems": rows, "nvidia_protocol": nv}
    (B / "nvidia").mkdir(parents=True, exist_ok=True)
    old = json.loads((B / "nvidia/summary.json").read_text()) if (B / "nvidia/summary.json").is_file() else {"sets": {}}
    old["sets"].update(summary["sets"])
    old["schema"] = summary["schema"]
    (B / "nvidia/summary.json").write_text(json.dumps(old, indent=2) + "\n")
    lines = ["# NVIDIA comparison: accuracy (research/nvidia/evaluate.py)", ""]
    for name, s in old["sets"].items():
        lines += [f"## {name} ({s['refs']})", "",
                  "| system | n | cpWER | tcpWER | WER | DER | miss | FA | confusion | JER | spk err |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        f = lambda v: "–" if v is None else f"{v:.4f}"
        for sysname, r in sorted(s["systems"].items(), key=lambda kv: (kv[1].get("cpwer.error_rate") is None,
                                                                       kv[1].get("cpwer.error_rate") or kv[1].get("der.der") or 9)):
            lines.append(f"| {sysname} | {r['n']} | {f(r['cpwer.error_rate'])} | {f(r['tcpwer.error_rate'])} | "
                         f"{f(r['wer_concat.wer'])} | {f(r['der.der'])} | {f(r['der.miss_rate'])} | {f(r['der.false_alarm_rate'])} | "
                         f"{f(r['der.confusion_rate'])} | {f(r['jer.jer'])} | {f(r['speaker_count.error'])} |")
        if s.get("nvidia_protocol"):
            lines += ["", "NVIDIA protocol (forced-alignment references, collar 0, overlap scored, no UEM):", "",
                      "| turns | pooled DER | macro DER |", "|---|---:|---:|"]
            for sysname, v in sorted(s["nvidia_protocol"].items(), key=lambda kv: kv[1]["pooled_der"] or 9):
                lines.append(f"| {sysname} | {v['pooled_der']:.4f} | {v['macro_der']:.4f} |")
        lines.append("")
    (B / "nvidia/summary.md").write_text("\n".join(lines) + "\n")
    print((B / "nvidia/summary.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
