import json, sys, time, types
from pathlib import Path
import numpy as np
sys.path.insert(0, "/Users/mivi/Desktop/plaud-harness")
from pipeline import model_store
from pipeline.base import load_audio
from pipeline.sherpa_sweep import SherpaStages, save_precomputed
from pipeline.whisper_sherpa import SEGMENTATION_ASSET, EMBEDDING_ASSET, sherpa_turns
root = Path("/Users/mivi/Desktop/plaud-harness"); sp = Path(sys.argv[1])
seg, _ = model_store.require(SEGMENTATION_ASSET); emb, _ = model_store.require(EMBEDDING_ASSET)
st = SherpaStages(seg, emb, num_threads=4)
print("meta", st.meta)
audio = load_audio(root / "data/corpora/ami/dev/IS1008a/mix.wav", 16000)
t0 = time.time(); pre = st.precompute(audio.pcm); print("precompute_s", round(time.time() - t0, 1), "embeddings", pre["embeddings"].shape)
save_precomputed(pre, sp / "IS1008a.npz")
refs = {0.7: sp / "calib-probe/0.7/IS1008a/hyp.json", 0.9: sp / "calib-probe/0.9/IS1008a/hyp.json", 1.1: sp / "calib-probe/1.1/IS1008a/hyp.json",
        1.3: sp / "calib-probe/1.3/IS1008a/hyp.json", 0.95: root / "build/v5-calib/hyp/0.95/IS1008a/hyp.json"}
dur = audio.pcm.size / 16000.0
for t, ref in refs.items():
    raw = st.turns_for(pre, t)
    mine = sherpa_turns([types.SimpleNamespace(start=a, end=b, speaker=s) for a, b, s in raw], dur)
    theirs = [tuple(x) for x in json.loads(ref.read_text())["extra"]["diarization"]["turns"]]
    mine = [(a, b, s) for a, b, s in mine]
    same = mine == theirs
    print(t, "identical" if same else "DIFFERENT", len(mine), len(theirs), len({x[2] for x in mine}), len({x[2] for x in theirs}))
    if not same:
        diff = [(i, a, b) for i, (a, b) in enumerate(zip(mine, theirs)) if a != b][:3]
        print("  first diffs", diff)
