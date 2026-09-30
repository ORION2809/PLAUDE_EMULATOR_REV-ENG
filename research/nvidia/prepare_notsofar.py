#!/usr/bin/env python3
"""Held-out meetings for the NVIDIA comparison: NOTSOFAR-1 "eval-full-only".

NOTSOFAR-1 (Microsoft, CC BY 4.0, https://huggingface.co/datasets/microsoft/NOTSOFAR)
publishes two evaluation sets.  ``240629.1_eval_small`` (80 meetings) is the
one NVIDIA reports for Nemotron 3 Diarization; ``240825.1_eval_full_with_GT``
(129 meetings) contains it.  The 49 meetings in eval-full but not eval-small
are in neither Nemotron's training data (NOTSOFAR train+dev) nor NVIDIA's
published evaluation, and are English workplace meetings of 3-7 people with
word-level timing (docs/nvidia-speech.md, "Held-out data").

For each of those meetings this writes two harness meetings with the SAME
reference (``plaud-harness/meeting/1``, built from ``gt_transcription.json``'s
``word_timing``):

* ``<out>/sc/<MTG>/``   the first single-channel far-field device (sorted by
  name), ch0: the closest analogue of a tabletop recorder;
* ``<out>/ctmix/<MTG>/`` the sum of the participants' close-talk channels
  (an analogue of AMI's headset mix), scaled down only if it would clip.

Downloads go through huggingface.co ``resolve`` URLs pinned to the dataset
commit, and every file's sha256 is recorded in the meeting's ``audio`` block.
Close-talk channels are deleted after mixing unless ``--keep-ct``.

Usage: prepare_notsofar.py [--out data/corpora/notsofar] [--limit N] [--keep-ct]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.io import meeting_from_dict  # noqa: E402

REPO = "microsoft/NOTSOFAR"
API = f"https://huggingface.co/api/datasets/{REPO}"
FULL = "benchmark-datasets/eval_set/240825.1_eval_full_with_GT/MTG"
SMALL = "benchmark-datasets/eval_set/240629.1_eval_small_with_GT/MTG"


def get_json(url: str) -> object:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError


def download(url: str, dest: Path) -> str:
    """Download to ``dest`` (atomically) unless present; return its sha256."""
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
                    while block := r.read(1 << 20):
                        f.write(block)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        part.replace(dest)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def meeting_ids(rev: str, path: str) -> list[str]:
    return sorted(Path(x["path"]).name for x in get_json(f"{API}/tree/{rev}/{path}") if x["type"] == "directory")


def reference_segments(gt: list[dict], duration: float) -> tuple[list[dict], list[str], dict]:
    """gt_transcription.json -> sorted contract segments from word_timing.
    Words are lowercased; a segment keeps only its timed words; words are
    clipped to the audio.  Returns (segments, speakers, counts)."""
    segs, dropped = [], 0
    for g in gt:
        words = []
        for w, a, b in g.get("word_timing") or []:
            a, b = min(max(0.0, float(a)), duration), min(max(0.0, float(b)), duration)
            tok = str(w).strip().lower()
            if tok and b >= a:
                words.append({"w": tok, "start": round(a, 3), "end": round(max(a, b), 3)})
        if not words:
            dropped += 1
            continue
        words.sort(key=lambda w: (w["start"], w["end"]))
        segs.append({"speaker": str(g["speaker_id"]), "start": words[0]["start"],
                     "end": max(w["end"] for w in words), "text": " ".join(w["w"] for w in words), "words": words})
    segs.sort(key=lambda s: (s["start"], s["end"], s["speaker"]))
    speakers = sorted({s["speaker"] for s in segs})
    return segs, speakers, {"gt_segments": len(gt), "segments_without_timed_words": dropped}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "data/corpora/notsofar"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N meetings (0 = all)")
    ap.add_argument("--keep-ct", action="store_true", help="keep the close-talk channels after mixing")
    args = ap.parse_args()
    out = Path(args.out)
    info = get_json(API)
    rev = info["sha"]
    full, small = meeting_ids(rev, FULL), set(meeting_ids(rev, SMALL))
    only = [m for m in full if m not in small]
    print(f"[notsofar] dataset revision {rev}: eval-full {len(full)}, eval-small {len(small)}, eval-full-only {len(only)}")
    if args.limit:
        only = only[: args.limit]
    raw = out / "raw"
    listing = []
    for n, mid in enumerate(only, 1):
        base = f"https://huggingface.co/datasets/{REPO}/resolve/{rev}/{FULL}/{mid}"
        mraw = raw / mid
        meta_sha = {f: download(f"{base}/{f}", mraw / f) for f in ("gt_transcription.json", "gt_meeting_metadata.json", "devices.json")}
        meta = json.loads((mraw / "gt_meeting_metadata.json").read_text())
        devices = json.loads((mraw / "devices.json").read_text())
        sc = sorted(d["device_name"] for d in devices if not d["is_mc"] and not d["is_close_talk"])
        ct = sorted(d["wav_file_names"] for d in devices if d["is_close_talk"])
        if not sc or not ct:
            print(f"[notsofar] {mid}: skipped (single-channel devices {len(sc)}, close-talk {len(ct)})")
            continue
        sc_rel = next(d["wav_file_names"].split(",")[0] for d in devices if d["device_name"] == sc[0])
        sc_sha = download(f"{base}/{sc_rel}", mraw / sc_rel)
        x_sc, sr = sf.read(str(mraw / sc_rel), dtype="float32")
        ct_sha, chans = {}, []
        for rel in ct:
            ct_sha[rel] = download(f"{base}/{rel}", mraw / rel)
            y, sr2 = sf.read(str(mraw / rel), dtype="float32")
            if sr2 != sr:
                raise SystemExit(f"{mid}: {rel} at {sr2} Hz, sc at {sr} Hz")
            chans.append(y)
        n_frames = min(len(x_sc), *(len(c) for c in chans))
        mix = np.sum([c[:n_frames] for c in chans], axis=0)
        peak = float(np.max(np.abs(mix))) if n_frames else 0.0
        gain = 1.0 if peak <= 0.99 else 0.99 / peak
        duration = n_frames / float(sr)
        gt = json.loads((mraw / "gt_transcription.json").read_text())
        segs, speakers, counts = reference_segments(gt, duration)
        for variant, audio, source in (("sc", x_sc[:n_frames], {"device": sc[0], "file": sc_rel, "sha256": sc_sha}),
                                       ("ctmix", mix * gain, {"close_talk": ct_sha, "gain": gain, "peak_before_gain": peak})):
            d = out / variant / mid
            d.mkdir(parents=True, exist_ok=True)
            sf.write(str(d / "mix.wav"), audio, sr, subtype="PCM_16")
            doc = {
                "schema": "plaud-harness/meeting/1", "meeting_id": mid, "sample_rate": int(sr), "duration_s": duration,
                "channels": 1, "speakers": [{"id": s} for s in speakers], "segments": segs,
                "audio": {"mix_wav": "mix.wav", "stems": {}, "device": {},
                          "mix_source": {"dataset": REPO, "revision": rev, "path": f"{FULL}/{mid}", "variant": variant,
                                         **source, "mix_wav_sha256": hashlib.sha256((d / "mix.wav").read_bytes()).hexdigest()}},
                "generator": {"name": "research/nvidia/prepare_notsofar.py", "version": "1", "timing_exact": False,
                              "scenario": {"source": "NOTSOFAR-1 eval-full-only (in 240825.1_eval_full_with_GT, not in "
                                                     "240629.1_eval_small)", "licence": "CC BY 4.0 (Microsoft NOTSOFAR-1)",
                                           "meeting": {k: meta.get(k) for k in ("MeetingDurationSec", "Room",
                                                                                "NumParticipants", "Topic", "Hashtags")},
                                           "reference": "gt_transcription.json word_timing; words lowercased, clipped "
                                                        "to the audio; segments without timed words dropped",
                                           "counts": counts, "metadata_sha256": meta_sha}},
            }
            meeting_from_dict(doc, where=f"{mid}/{variant}")  # the harness contract
            (d / "meeting.json").write_text(json.dumps(doc, indent=2) + "\n")
        if not args.keep_ct:
            for rel in ct:
                (mraw / rel).unlink()
        listing.append({"meeting_id": mid, "duration_s": round(duration, 3), "speakers": len(speakers),
                        "sc_device": sc[0], "ct_channels": len(ct), "ctmix_gain": gain, **counts})
        print(f"[notsofar] {n}/{len(only)} {mid}: {duration:.0f} s, {len(speakers)} speakers, sc {sc[0]}, ct {len(ct)}")
    (out / "listing.json").write_text(json.dumps({"dataset": REPO, "revision": rev, "set": "eval-full-only",
                                                  "meetings": listing}, indent=2) + "\n")
    print(f"[notsofar] {len(listing)} meetings, {sum(m['duration_s'] for m in listing) / 3600:.2f} h -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
