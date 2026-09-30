#!/usr/bin/env python3
"""Profile sherpa-onnx's Nemotron-EN NPU (QNN HTP) context binaries on real Snapdragon 8
phones through Qualcomm AI Hub (docs/nvidia-speech.md, "Snapdragon NPU").

sherpa-onnx v1.13.8 publishes per-SoC QNN context binaries of NVIDIA's
nemotron-speech-streaming-en-0.6b (release tag asr-models-qnn-binary-2): the encoder,
the RNNT prediction network (decoder) and the joiner, each its own graph, 16-bit
activations (encoder/joiner) and fp16 (decoder). For each chip this script downloads the
1120 ms package, uploads the three binaries to AI Hub, runs a profiling inference job for
each on the matching hosted phone (random inputs, 100 iterations, AI Hub's default BURST
power mode; the encoder is also profiled in BALANCED mode), and saves the profiles.

  AIHUB_TOKEN_FILE=<file> research/nvidia/aihub/profile_qnn_binaries.py [--socs SM8650 ...]

The token is read from the file into memory (never printed or stored). Results:
build/nvidia/aihub/qnn-binaries/<soc>/{<graph>.<mode>.profile.json, jobs.json} and
build/nvidia/aihub/qnn-binaries/summary.json. Local downloads are deleted after upload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import qai_hub as hub

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "build/nvidia/aihub/qnn-binaries"
RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models-qnn-binary-2"
PACKAGE = "sherpa-onnx-qnn-{soc}-binary-nemotron-speech-streaming-en-0.6b-1120ms"
DEVICES = {
    "SM8550": "Samsung Galaxy S23 (Family)",   # Snapdragon 8 Gen 2, HTP v73
    "SM8650": "Samsung Galaxy S24 (Family)",   # Snapdragon 8 Gen 3, HTP v75
    "SM8750": "Samsung Galaxy S25 (Family)",   # Snapdragon 8 Elite, HTP v79
    "SM8850": "Samsung Galaxy S26 (Family)",   # Snapdragon 8 Elite Gen 5, HTP v81
}
GRAPHS = ("encoder", "decoder", "joiner")


def client() -> hub.Client:
    path = os.environ.get("AIHUB_TOKEN_FILE")
    if not path or not Path(path).is_file():
        raise SystemExit("set AIHUB_TOKEN_FILE to a file holding the Qualcomm AI Hub API token")
    return hub.Client(config=hub.ClientConfig(api_token=Path(path).read_text().strip()))


def fetch(soc: str, work: Path) -> tuple[Path, dict]:
    name = PACKAGE.format(soc=soc)
    tarball = work / f"{name}.tar.bz2"
    if not tarball.is_file():
        part = tarball.with_suffix(".part")
        with urllib.request.urlopen(f"{RELEASE}/{name}.tar.bz2", timeout=300) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        part.rename(tarball)
    info = {"url": f"{RELEASE}/{name}.tar.bz2", "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
            "bytes": tarball.stat().st_size}
    with tarfile.open(tarball) as t:
        t.extractall(work, filter="data")
    tarball.unlink()
    d = work / name
    info["files"] = {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in sorted(d.iterdir()) if p.is_file()}
    return d, info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--socs", nargs="*", default=list(DEVICES))
    args = ap.parse_args()
    c = client()
    summary = json.loads((OUT / "summary.json").read_text()) if (OUT / "summary.json").is_file() else {}
    for soc in args.socs:
        dest = OUT / soc
        if (dest / "jobs.json").is_file() and all((dest / f"{g}.burst.profile.json").is_file() for g in GRAPHS):
            print(f"[aihub] {soc}: done already")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        work = OUT / "_work"
        work.mkdir(exist_ok=True)
        print(f"[aihub] {soc}: downloading the sherpa-onnx package", flush=True)
        pkg, info = fetch(soc, work)
        device = hub.Device(DEVICES[soc])
        jobs = {"soc": soc, "device": DEVICES[soc], "package": info, "jobs": {}}
        submitted = []
        for g in GRAPHS:
            print(f"[aihub] {soc}: uploading {g}.bin", flush=True)
            model = c.upload_model(str(pkg / f"{g}.bin"))
            modes = [("burst", None), ("balanced", "--qnn_options context_htp_performance_mode=balanced")] \
                if g == "encoder" else [("burst", None)]
            for mode, opts in modes:
                job = c.submit_inference_job(model, device=device, profile=True, name=f"nemotron-en-1120-{soc}-{g}-{mode}",
                                             **({"options": opts} if opts else {}))
                jobs["jobs"][f"{g}.{mode}"] = {"job_id": job.job_id, "url": job.url, "model_id": model.model_id}
                submitted.append((g, mode, job))
        (dest / "jobs.json").write_text(json.dumps(jobs, indent=2) + "\n")
        shutil.rmtree(pkg)
        for g, mode, job in submitted:
            status = job.wait()
            print(f"[aihub] {soc} {g} {mode}: {status.code} {status.message or ''}", flush=True)
            if status.success:
                (dest / f"{g}.{mode}.profile.json").write_text(json.dumps(job.download_profile(), indent=2) + "\n")
        summary[soc] = {}
        for g, mode, _ in submitted:
            p = dest / f"{g}.{mode}.profile.json"
            if p.is_file():
                es = json.loads(p.read_text()).get("execution_summary", {})
                summary[soc][f"{g}.{mode}"] = {k: es.get(k) for k in ("estimated_inference_time", "estimated_inference_peak_memory",
                                                                      "first_load_time", "warm_load_time", "inference_memory_peak_range")}
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
