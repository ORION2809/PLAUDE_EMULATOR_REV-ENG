#!/usr/bin/env python3
"""Profile the LiteRT port of NVIDIA Nemotron 3 Diarization on real Snapdragon phones
through Qualcomm AI Hub (docs/nvidia-speech.md, "Diarization on phones").

litert-community/Nemotron-3-Diarization-LiteRT (OpenMDW-1.1, pinned revision below) splits
the model into graph A (frontend: 8-frame mel stacking + projection, float32) and graph B
(31-layer encoder + head, float16 weights with float compute) in two fixed shapes:
low_latency (streaming, 0.72 s step) and offline (whole file, 27.2 s step, what the app
uses after the user stops recording). No NPU build of this model exists; the port targets
the GPU. Each graph is profiled (100 iterations, random inputs) with:

  gpu-fp32   GPU delegate, MAX_PRECISION first (the port's card: FP16 changes segments)
  gpu-fp16   GPU delegate, AI Hub's default priorities (min latency, effectively FP16)
  cpu        XNNPACK on the CPU (the fallback)

  AIHUB_TOKEN_FILE=<file> research/nvidia/aihub/profile_litert_diar.py [--graphs ...] [--devices ...]

Results: build/nvidia/aihub/litert-diar/<graph>/<device slug>.<mode>.profile.json and
build/nvidia/aihub/litert-diar/summary.json. Downloads are deleted after upload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path

import qai_hub as hub

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "build/nvidia/aihub/litert-diar"
REPO = "litert-community/Nemotron-3-Diarization-LiteRT"
REV = "4a6b8c830a7384169f54668d474865b3b4745c32"
GRAPHS = {
    "frontend": "nemotron3_diar_frontend.tflite",
    "offline": "nemotron3_diar_encoder_offline_fp16.tflite",
    "low_latency": "nemotron3_diar_encoder_low_latency_fp16.tflite",
}
DEVICES = ["Samsung Galaxy S23 (Family)", "Samsung Galaxy S24 (Family)", "Samsung Galaxy S25 (Family)",
           "Samsung Galaxy S26 (Family)", "Snapdragon 7 Gen 4 QRD", "Samsung Galaxy A73 5G"]
PRIO = "gpu_inference_priority{}=TFLITE_GPU_INFERENCE_PRIORITY_{}"
MODES = {
    # AI Hub accepts either --compute_unit or --tflite_delegates, not both (rejected on 2026-09-30)
    "gpu-fp32": "--tflite_delegates gpu --tflite_options "
                + ";".join([PRIO.format(1, "MAX_PRECISION"), PRIO.format(2, "AUTO"), PRIO.format(3, "AUTO")]),
    "gpu-fp16": "--tflite_delegates gpu",
    "cpu": "--tflite_delegates xnnpack",
}


def client() -> hub.Client:
    path = os.environ.get("AIHUB_TOKEN_FILE")
    if not path or not Path(path).is_file():
        raise SystemExit("set AIHUB_TOKEN_FILE to a file holding the Qualcomm AI Hub API token")
    return hub.Client(config=hub.ClientConfig(api_token=Path(path).read_text().strip()))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graphs", nargs="*", default=list(GRAPHS))
    ap.add_argument("--devices", nargs="*", default=DEVICES)
    ap.add_argument("--modes", nargs="*", default=list(MODES))
    args = ap.parse_args()
    c = client()
    summary = json.loads((OUT / "summary.json").read_text()) if (OUT / "summary.json").is_file() else {}
    work = OUT / "_work"
    work.mkdir(parents=True, exist_ok=True)
    for g in args.graphs:
        dest = OUT / g
        dest.mkdir(parents=True, exist_ok=True)
        todo = [(d, m) for d in args.devices for m in args.modes
                if not (dest / f"{slug(d)}.{m}.profile.json").is_file()]
        if not todo:
            continue
        f = work / GRAPHS[g]
        if not f.is_file():
            with urllib.request.urlopen(f"https://huggingface.co/{REPO}/resolve/{REV}/{GRAPHS[g]}", timeout=300) as r, \
                    open(f.with_suffix(".part"), "wb") as o:
                shutil.copyfileobj(r, o, 1 << 20)
            f.with_suffix(".part").rename(f)
        meta = {"repo": REPO, "revision": REV, "file": GRAPHS[g], "bytes": f.stat().st_size,
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
        print(f"[litert] {g}: uploading {meta['bytes']} B", flush=True)
        model = c.upload_model(str(f))
        jobs = []
        for d, m in todo:
            job = c.submit_inference_job(model, device=hub.Device(d), profile=True, options=MODES[m],
                                         name=f"nemotron3-diar-{g}-{m}-{slug(d)}")
            jobs.append((d, m, job))
        (dest / "jobs.json").write_text(json.dumps({"model": meta, "model_id": model.model_id, "jobs": {
            f"{slug(d)}.{m}": {"job_id": j.job_id, "url": j.url, "options": MODES[m]} for d, m, j in jobs}}, indent=2) + "\n")
        f.unlink()
        for d, m, job in jobs:
            st = job.wait()
            print(f"[litert] {g} {d} {m}: {st.code} {st.message or ''}", flush=True)
            if st.success:
                prof = job.download_profile()
                (dest / f"{slug(d)}.{m}.profile.json").write_text(json.dumps(prof, indent=2) + "\n")
                es = prof.get("execution_summary", {})
                summary.setdefault(g, {})[f"{d} | {m}"] = {
                    k: es.get(k) for k in ("estimated_inference_time", "estimated_inference_peak_memory", "first_load_time")}
                units = {}
                for op in (prof.get("execution_detail") or []):
                    u = op.get("compute_unit")
                    units[u] = units.get(u, 0) + 1
                summary[g][f"{d} | {m}"]["ops_by_unit"] = units
            else:
                summary.setdefault(g, {})[f"{d} | {m}"] = {"failed": f"{st.code} {st.message or ''}"}
            (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
