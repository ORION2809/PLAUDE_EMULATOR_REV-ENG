#!/usr/bin/env python3
"""Calibrate embedding-cluster's cosine distance guard on the AMI dev split.

Run with an interpreter that has torch and speechbrain (e.g. .venv-torch):

    PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python scripts/calibrate-embedding-cluster.py \
        [--dev data/corpora/ami/dev] [--out build/v5-calib/embedding-cluster]

Per dev meeting it runs the SAME steps as EmbeddingClusterDiarizer.diarize
(pipeline/adapters.py): energy VAD, regions, cells, windows, ECAPA embeddings
(once, cached as .npz), then for every threshold of the grid the clustering
(pipeline.energy_vad.cluster_embeddings, cosine), smoothing and relabelling.
`--validate MEETING` first checks that this replay at the default threshold
equals a real `embedding-cluster` run.  Each threshold's hypotheses are scored
with `python -m evals batch` (no hint, DER collar 0.25) and the lowest macro
DER is selected (ties: smaller |speaker-count error|, then the lower
threshold).  HARNESS_POLICY: the grid and the criterion.  The test split is
never used for tuning.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.adapters import (  # noqa: E402
    EMBEDDING_DISTANCE_THRESHOLD,
    EmbeddingClusterDiarizer,
    speechbrain_ecapa_embedder,
)
from pipeline.base import load_audio  # noqa: E402
from pipeline.energy_vad import (  # noqa: E402
    _relabel_by_first_appearance,
    cell_windows,
    chunk_regions,
    cluster_embeddings,
    energy_vad,
    mask_to_regions,
    smooth_turns,
)
from pipeline import model_store  # noqa: E402

# 0.100 .. 1.200.  The threshold caps the eigengap's speaker count by the number of
# average-linkage clusters that reach the minimum size; on the dev meetings that cap
# only moves between about 0.4 and 1.0 (below 0.4 no cluster is big enough and k = 1
# everywhere; from 1.0 up everything merges).  A first grid stopped at 0.6.
GRID = [round(0.10 + 0.025 * i, 3) for i in range(45)]
SCORE_WORKERS = 4  # evals batch processes at once (one per threshold)


def stages(pcm, embed, v, c):
    mask, hop_s = energy_vad(pcm, 16000, v)
    regions = mask_to_regions(mask, hop_s)
    cells = chunk_regions(regions, c.chunk_s, c.min_chunk_s)
    windows = cell_windows(regions, cells, max(c.window_s, c.chunk_s))
    x = np.asarray(pcm, dtype=np.float32)
    clips = [x[int(round(a * 16000)): max(int(round(b * 16000)), int(round(a * 16000)) + 1)] for a, b in windows]
    emb = np.asarray(embed(clips, 16000), dtype=np.float64) if cells else np.zeros((0, 192))
    return cells, emb


def save_cache(f: Path, cells, emb) -> None:
    """Write the .npz under a temporary name and rename it, so an interrupted
    run never leaves a truncated cache that a resumed run would trust."""
    tmp = f.with_name(f.stem + ".partial.npz")
    np.savez_compressed(tmp, cells=np.asarray(cells, dtype=np.float64).reshape(-1, 2), emb=emb)
    os.replace(tmp, f)


def turns_for(cells, emb, c, threshold, num_speakers=None):
    p = dataclasses.replace(c, distance_threshold=float(threshold))
    labels = np.zeros(len(cells), dtype=int)
    if cells:
        labels = cluster_embeddings(emb, num_speakers=num_speakers, params=p, metric="cosine", info={})
    raw = [(a, b, f"c{int(l)}") for (a, b), l in zip(cells, labels)]
    return _relabel_by_first_appearance(smooth_turns(raw, min_segment_s=p.min_segment_s, merge_gap_s=p.merge_gap_s))


def hyp_doc(mid, turns, threshold, num_speakers=None, note=None):
    return {"schema": "plaud-harness/hypothesis/1", "meeting_id": mid,
            "system": f"embedding-cluster@{threshold:g}" + (f"+hint{num_speakers}" if num_speakers else "") + ":replayed",
            "segments": [{"speaker": s, "start": a, "end": b, "text": ""} for a, b, s in turns],
            "extra": {"diarization": {"turns": [[a, b, s] for a, b, s in turns],
                                      "settings": {"distance_threshold": threshold, "num_speakers": num_speakers}},
                      "note": note or "scripts/calibrate-embedding-cluster.py: replayed from cached ECAPA embeddings"}}


def test_mode(a, v, c, embed) -> int:
    """One ECAPA pass over the TEST meetings; default, hinted and calibrated
    hypotheses from it.  Checked first: the replay at the default threshold
    equals every real embedding-cluster run in --validate-real."""
    import time

    root, out = Path(a.test_root), Path(a.out)
    pre = out / "pre-test"
    pre.mkdir(parents=True, exist_ok=True)
    cache = {}
    for md in sorted(p for p in root.iterdir() if (p / "meeting.json").is_file()):
        f = pre / f"{md.name}.npz"
        if not f.is_file():
            cells, emb = stages(load_audio(md / "mix.wav", 16000).pcm, embed, v, c)
            save_cache(f, cells, emb)
            print(f"embedded test {md.name}: {len(cells)} cells", flush=True)
        z = np.load(f)
        cache[md.name] = ([tuple(map(float, r)) for r in z["cells"]], z["emb"])
    while not (out / "selected.json").is_file():  # the dev calibration may still be running
        time.sleep(30)
    t_cal = float(json.loads((out / "selected.json").read_text())["distance_threshold"])
    checks = {}
    for real in sorted(Path(a.validate_real).glob("*/hyp.json")):
        mid = real.parent.name
        mine = [list(t) for t in turns_for(*cache[mid], c, EMBEDDING_DISTANCE_THRESHOLD)]
        checks[mid] = mine == json.loads(real.read_text())["extra"]["diarization"]["turns"]
    print(f"replay vs real embedding-cluster on the test split: {checks}", flush=True)
    if not checks or not all(checks.values()):
        return 2
    note = (f"replayed from cached ECAPA embeddings by scripts/calibrate-embedding-cluster.py --test-root; the replay "
            f"at the default threshold is identical to the real pipeline on {', '.join(sorted(checks))}")
    hyp_root = Path(a.test_hyps)
    for mid, (cells, emb) in cache.items():
        meeting = json.loads((root / mid / "meeting.json").read_text())
        k = len({s["speaker"] for s in meeting["segments"]})
        for name, t, n in (("embedding-cluster", EMBEDDING_DISTANCE_THRESHOLD, None),
                           ("embedding-cluster-hint", EMBEDDING_DISTANCE_THRESHOLD, k),
                           ("embedding-cluster-cal", t_cal, None)):
            d = hyp_root / name / mid
            d.mkdir(parents=True, exist_ok=True)
            (d / "hyp.json").write_text(json.dumps(hyp_doc(mid, turns_for(cells, emb, c, t, n), t, n, note)) + "\n")
    (out / "test-replay.json").write_text(json.dumps({"validated_against_real": checks, "calibrated_threshold": t_cal,
                                                      "default_threshold": EMBEDDING_DISTANCE_THRESHOLD}, indent=2) + "\n")
    print(f"wrote embedding-cluster, -hint, -cal for {len(cache)} test meetings", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=str(ROOT / "data/corpora/ami/dev"))
    ap.add_argument("--out", default=str(ROOT / "build/v5-calib/embedding-cluster"))
    ap.add_argument("--validate", default="IS1008a")
    ap.add_argument("--test-root", help="test mode: the converted test meetings (after the dev calibration)")
    ap.add_argument("--test-hyps", default=str(ROOT / "build/v5-test/hyp/ami"))
    ap.add_argument("--validate-real", default=str(ROOT / "build/v5-calib/embedding-cluster/validation-test/real"))
    a = ap.parse_args()
    dev, out = Path(a.dev), Path(a.out)
    (out / "pre").mkdir(parents=True, exist_ok=True)
    if not a.test_root:
        # a test pass waits for selected.json: never let it read a previous dev run's
        (out / "selected.json").unlink(missing_ok=True)
    v, c = EmbeddingClusterDiarizer.params_from({})
    embed = speechbrain_ecapa_embedder(savedir=str(model_store.models_dir() / "speechbrain-ecapa"))
    if a.test_root:
        return test_mode(a, v, c, embed)
    meetings = sorted(p for p in dev.iterdir() if (p / "meeting.json").is_file())
    cache: dict[str, tuple] = {}
    for md in meetings:
        f = out / "pre" / f"{md.name}.npz"
        if not f.is_file():
            cells, emb = stages(load_audio(md / "mix.wav", 16000).pcm, embed, v, c)
            save_cache(f, cells, emb)
            print(f"embedded {md.name}: {len(cells)} cells", flush=True)
        z = np.load(f)
        cache[md.name] = ([tuple(map(float, r)) for r in z["cells"]], z["emb"])
    # validation against the real pipeline at the default threshold
    vm = dev / a.validate
    real_out = out / "validation" / a.validate / "hyp.json"
    if not real_out.is_file():
        subprocess.run([sys.executable, "-m", "pipeline", "run", "--pipeline", "embedding-cluster", "--audio",
                        str(vm / "mix.wav"), "--meeting-dir", str(vm), "--out", str(real_out)], check=True,
                       capture_output=True, cwd=ROOT)
    real = json.loads(real_out.read_text())["extra"]["diarization"]["turns"]
    mine = [list(t) for t in turns_for(*cache[a.validate], c, EMBEDDING_DISTANCE_THRESHOLD)]
    identical = mine == real
    print(f"replay vs real embedding-cluster at {EMBEDDING_DISTANCE_THRESHOLD} on {a.validate}: identical={identical}", flush=True)
    if not identical:
        return 2
    for t in GRID:
        hdir = out / "hyp" / f"{t:g}"
        for mid, (cells, emb) in cache.items():
            p = hdir / mid / "hyp.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(hyp_doc(mid, turns_for(cells, emb, c, t), t)) + "\n")

    def score(t):
        rep = out / "reports" / f"{t:g}.json"
        rep.parent.mkdir(parents=True, exist_ok=True)
        rep.unlink(missing_ok=True)  # never read a previous run's report
        p = subprocess.run([sys.executable, "-m", "evals", "batch", "--refs", str(dev), "--hyps",
                            str(out / "hyp" / f"{t:g}"), "--report", str(rep), "--quiet"],
                           cwd=ROOT, capture_output=True, text=True)
        r = json.loads(rep.read_text()) if rep.is_file() else None
        if p.returncode != 0 or r is None or r["errors"] or r["missing"] or r["macro"].get("n") != len(cache):
            raise SystemExit(f"threshold {t:g}: evals exit {p.returncode}, report incomplete\n{p.stderr[-2000:]}")
        return rep

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=SCORE_WORKERS) as pool:
        reports = list(pool.map(score, GRID))
    rows = []
    for t, rep in zip(GRID, reports):
        r = json.loads(rep.read_text())
        m = r["macro"]
        rows.append({"threshold": t, "der": m["der.der"], "jer": m["jer.jer"], "confusion": m["der.confusion_rate"],
                     "speaker_count_error": m["speaker_count.error"], "speaker_count_abs_error": m["speaker_count.abs_error"],
                     "hyp_speakers": [x["speaker_count"]["hypothesis"] for x in r["meetings"]], "n": m["n"]})
        print(f"threshold {t:g}: macro DER {m['der.der']:.4f}, speakers {rows[-1]['hyp_speakers']}", flush=True)
    best = min(rows, key=lambda x: (x["der"], x["speaker_count_abs_error"], x["threshold"]))
    doc = {"schema": "plaud-harness/embedding-cluster-calibration/1", "distance_threshold": best["threshold"],
           "default": EMBEDDING_DISTANCE_THRESHOLD, "validated_on": a.validate, "replay_identical": identical,
           "criterion": "lowest macro DER (collar 0.25) over the AMI dev meetings, no hint; ties: smaller macro "
                        "|speaker-count error|, then the lower threshold (HARNESS_POLICY)", "rows": rows}
    tmp = out / "selected.json.partial"
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, out / "selected.json")  # the waiting test pass must never read half a file
    print(f"selected distance_threshold = {best['threshold']:g} (macro DER {best['der']:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
