"""Threshold sweeps for sherpa-onnx diarization without re-running its models.

sherpa-onnx 1.13.8's pyannote diarization (``OfflineSpeakerDiarizationPyannoteImpl``,
sherpa-onnx/csrc/offline-speaker-diarization-pyannote-impl.h at tag v1.13.8)
runs four stages: the segmentation model over 10 s windows, powerset ->
multi-label, one speaker embedding per (window, local speaker), then
clustering and label reconstruction.  Only the last stage depends on the
clustering threshold, and the first three cost almost all the time.  This
module runs the first three ONCE per meeting (``precompute``) and the last
one per threshold (``turns_for``), so calibrating ``cluster_threshold``
(scripts/calibrate-sherpa.sh) costs one diarization pass instead of one per
grid point.

What is sherpa's own code here, and what is a port:

* the speaker embeddings come from ``sherpa_onnx.SpeakerEmbeddingExtractor``
  and the clustering from ``sherpa_onnx.FastClustering`` -- sherpa's code;
* the segmentation model runs through the ``onnxruntime`` Python package on
  the SAME model file, while sherpa-onnx runs its own bundled ONNX Runtime,
  so a frame's argmax can differ at a near-tie;
* chunking, powerset mapping, overlap exclusion, sample ranges, speaker
  counts, top-k selection (libc++ ``std::partial_sort``, emulated: ties go
  where libc++'s heap puts them), segment merging and the min-duration
  filter are a line-by-line port, with sherpa's float32 arithmetic.

``validate`` compares ``turns_for`` with real sherpa-onnx runs; the
calibration records that comparison next to its result.  HARNESS_POLICY:
this is calibration tooling; the pipeline itself always runs sherpa-onnx.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

f32 = np.float32


@dataclass
class Meta:
    sample_rate: int
    window_size: int
    window_shift: int
    receptive_field_size: int
    receptive_field_shift: int
    num_speakers: int
    powerset_max_classes: int
    num_classes: int


def _powerset_mapping(meta: Meta) -> np.ndarray:
    m = np.zeros((meta.num_classes, meta.num_speakers), dtype=np.int32)
    k = 1
    for i in range(1, meta.powerset_max_classes + 1):
        if i == 1:
            for j in range(meta.num_speakers):
                m[k, j] = 1
                k += 1
        elif i == 2:
            for j in range(meta.num_speakers):
                for q in range(j + 1, meta.num_speakers):
                    m[k, j] = 1
                    m[k, q] = 1
                    k += 1
        else:
            raise ValueError(f"powerset_max_classes = {i} is not supported (as in sherpa-onnx)")
    return m


def _chunk_start_frame(i: int, meta: Meta) -> int:
    # int32_t start = static_cast<float>(i) * window_shift / receptive_field_shift + 0.5;
    v = f32(f32(i) * f32(meta.window_shift)) / f32(meta.receptive_field_shift)
    return int(float(f32(v)) + 0.5)


def _fma32(a: Any, b: Any, c: Any) -> np.float32:
    """float32 ``a * b + c`` rounded ONCE, as a fused multiply-add.  Apple
    clang contracts such expressions on arm64 (-ffp-contract=on), and the
    sherpa-onnx macOS wheel shows it: without this, turn times differed from
    sherpa's by one float32 ulp (validation, 2026-09-28).  The float64 product
    of two float32 values is exact; the sum is rounded to float64 and then to
    float32 (a double rounding that can differ from a true fma only in rare
    halfway cases)."""
    return f32(float(a) * float(b) + float(c))


def _sample_index(k: int, num_frames: int, meta: Meta, offset: int) -> int:
    # static_cast<float>(k) / num_frames * window_size + sample_offset  -> int32 (truncation)
    v = _fma32(f32(f32(k) / f32(num_frames)), f32(meta.window_size), f32(offset))
    return int(math.trunc(float(v)))


def _sift_down(first: list[int], less, length: int, start: int) -> None:
    """libc++ std::__sift_down (the heap is a max-heap under ``less``)."""
    child = start
    if length < 2 or (length - 2) // 2 < child:
        return
    child = 2 * child + 1
    if child + 1 < length and less(first[child], first[child + 1]):
        child += 1
    if less(first[child], first[start]):
        return
    top = first[start]
    while True:
        first[start] = first[child]
        start = child
        if (length - 2) // 2 < child:
            break
        child = 2 * child + 1
        if child + 1 < length and less(first[child], first[child + 1]):
            child += 1
        if less(first[child], top):
            break
    first[start] = top


def topk_index(vec: np.ndarray, topk: int) -> list[int]:
    """sherpa-onnx math.h ``TopkIndex``: std::partial_sort with
    ``vec[a] > vec[b]``, as libc++ implements it (make_heap on the first k,
    then swap in any later element that compares strictly greater than the
    heap top).  Only the selected SET matters to the caller."""
    size = len(vec)
    k = max(0, min(size, int(topk)))
    idx = list(range(size))
    if k == 0:
        return []
    less = lambda a, b: vec[a] > vec[b]  # noqa: E731  (comp of the partial_sort)
    head = idx[:k]
    if k > 1:
        for start in range((k - 2) // 2, -1, -1):
            _sift_down(head, less, k, start)
    for i in range(k, size):
        if less(idx[i], head[0]):
            head[0] = idx[i]
            _sift_down(head, less, k, 0)
    return head


class SherpaStages:
    """Segmentation + embeddings once, reconstruction per threshold."""

    def __init__(self, segmentation_model: str | Path, embedding_model: str | Path, num_threads: int = 4) -> None:
        import onnxruntime as ort  # type: ignore[import-not-found]
        import sherpa_onnx  # type: ignore[import-not-found]

        self._so = sherpa_onnx
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(num_threads)
        opts.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(segmentation_model), sess_options=opts, providers=["CPUExecutionProvider"])
        md = self.sess.get_modelmeta().custom_metadata_map
        window_size = int(md["window_size"])
        # window_shift = static_cast<double>(0.1f) * window_size, as sherpa's default window_shift_ratio
        shift = float(f32(0.1)) * window_size
        self.meta = Meta(
            sample_rate=int(md["sample_rate"]),
            window_size=window_size,
            window_shift=max(1, min(window_size, int(shift))),
            receptive_field_size=int(md["receptive_field_size"]),
            receptive_field_shift=int(md["receptive_field_shift"]),
            num_speakers=int(md["num_speakers"]),
            powerset_max_classes=int(md["powerset_max_classes"]),
            num_classes=int(md["num_classes"]),
        )
        self.mapping = _powerset_mapping(self.meta)
        self.input_name = self.sess.get_inputs()[0].name
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding_model), num_threads=int(num_threads))
        )

    # --- stages 1-3 ---------------------------------------------------------------------

    def _segment(self, chunk: np.ndarray) -> np.ndarray:
        out = self.sess.run(None, {self.input_name: chunk.reshape(1, 1, -1).astype(np.float32)})[0]
        return out[0]

    def precompute(self, pcm: np.ndarray) -> dict[str, Any]:
        m = self.meta
        audio = np.ascontiguousarray(pcm, dtype=np.float32)
        n = int(audio.size)
        segs: list[np.ndarray] = []
        if n <= 0:
            raise ValueError("no samples")
        if n <= m.window_size:
            buf = np.zeros(m.window_size, dtype=np.float32)
            buf[:n] = audio
            segs.append(self._segment(buf))
        else:
            num_chunks = (n - m.window_size) // m.window_shift + 1
            has_last = (n - m.window_size) % m.window_shift > 0
            for i in range(num_chunks):
                segs.append(self._segment(audio[i * m.window_shift : i * m.window_shift + m.window_size]))
            if has_last:
                buf = np.zeros(m.window_size, dtype=np.float32)
                tail = audio[num_chunks * m.window_shift :]
                buf[: tail.size] = tail
                segs.append(self._segment(buf))
        labels = [self.mapping[np.argmax(s, axis=1)] for s in segs]  # ToMultiLabel (first max on ties)
        pre: dict[str, Any] = {"n": n, "labels": labels}
        if len(labels) == 1:
            return pre
        pre["speakers_per_frame"] = self._speakers_per_frame(labels)
        pairs, ranges = self._chunk_speaker_samples(labels)
        emb, valid = self._embeddings(audio, n, ranges)
        pre["pairs"] = [pairs[i] for i in valid]
        pre["embeddings"] = emb
        return pre

    def _speakers_per_frame(self, labels: list[np.ndarray]) -> np.ndarray:
        m = self.meta
        num_frames = (m.window_size + (len(labels) - 1) * m.window_shift) // m.receptive_field_shift + 1
        count = np.zeros(num_frames, dtype=np.float32)
        weight = np.zeros(num_frames, dtype=np.float32)
        for i, lab in enumerate(labels):
            s = _chunk_start_frame(i, m)
            count[s : s + lab.shape[0]] += lab.sum(axis=1).astype(np.float32)
            weight[s : s + lab.shape[0]] += f32(1)
        v = (count / (weight + f32(1e-12))) + f32(0.5)
        return np.trunc(v).astype(np.int32)

    def _chunk_speaker_samples(self, labels: list[np.ndarray]) -> tuple[list[tuple[int, int]], list[list[tuple[int, int]]]]:
        m = self.meta
        pairs: list[tuple[int, int]] = []
        ranges: list[list[tuple[int, int]]] = []
        for ci, lab in enumerate(labels):
            lab = np.where((lab.sum(axis=1) < 2)[:, None], lab, 0)  # ExcludeOverlap
            t = lab.T
            num_frames = t.shape[1]
            offset = ci * m.window_shift
            for sp in range(m.num_speakers):
                d = t[sp]
                if d.sum() < 10:
                    continue
                spans: list[tuple[int, int]] = []
                active, start = False, 0
                for k in range(num_frames):
                    if d[k] != 0:
                        if not active:
                            active, start = True, k
                    elif active:
                        active = False
                        spans.append((_sample_index(start, num_frames, m, offset), _sample_index(k, num_frames, m, offset)))
                if active:
                    spans.append((_sample_index(start, num_frames, m, offset), _sample_index(num_frames - 1, num_frames, m, offset)))
                pairs.append((ci, sp))
                ranges.append(spans)
        return pairs, ranges

    def _embeddings(self, audio: np.ndarray, n: int, ranges: list[list[tuple[int, int]]]) -> tuple[np.ndarray, list[int]]:
        rows: list[list[float]] = []
        valid: list[int] = []
        sr = self.meta.sample_rate
        for k, spans in enumerate(ranges):
            stream = self.extractor.create_stream()
            for a, b in spans:
                end = b if b <= n else n
                if end - a > 0:
                    stream.accept_waveform(sr, audio[a:end])
            stream.input_finished()
            if not self.extractor.is_ready(stream):
                raise RuntimeError("segment too short for the embedding extractor (sherpa-onnx exits here)")
            e = self.extractor.compute(stream)
            if not any(math.isnan(x) for x in e):
                rows.append(list(e))
                valid.append(k)
        return np.asarray(rows, dtype=np.float32).reshape(len(rows), -1), valid

    # --- stage 4 --------------------------------------------------------------------------

    def turns_for(self, pre: dict[str, Any], threshold: float, num_clusters: int = -1,
                  min_duration_on: float = 0.3, min_duration_off: float = 0.5) -> list[tuple[float, float, int]]:
        """Raw sherpa segments (start, end, speaker int) for one threshold."""
        m = self.meta
        n, labels = pre["n"], pre["labels"]
        if len(labels) == 1:
            final = labels[0]
            if (n - m.window_size) % m.window_shift > 0:
                final = final[: min(n // m.receptive_field_shift, final.shape[0])]
            return self._result(final, min_duration_on, min_duration_off)
        spf = pre["speakers_per_frame"]
        if int(spf.max()) == 0 or pre["embeddings"].shape[0] == 0:
            return []
        clustering = self._so.FastClustering(self._so.FastClusteringConfig(num_clusters=int(num_clusters), threshold=float(threshold)))
        clabels = list(clustering(pre["embeddings"]))
        if not clabels:
            return []
        max_c = max(clabels)
        to_cluster = {p: c for p, c in zip(pre["pairs"], clabels)}
        num_frames = (m.window_size + (len(labels) - 1) * m.window_shift) // m.receptive_field_shift + 1
        count = np.zeros((num_frames, max_c + 1), dtype=np.int32)
        for ci, lab in enumerate(labels):
            new = np.zeros((lab.shape[0], max_c + 1), dtype=np.int32)
            for sp in range(lab.shape[1]):
                c = to_cluster.get((ci, sp))
                if c is None or not 0 <= c <= max_c:
                    continue
                new[lab[:, sp] == 1, c] = 1
            s = _chunk_start_frame(ci, m)
            count[s : s + lab.shape[0]] += new
        if (n - m.window_size) % m.window_shift > 0:
            last = min(n // m.receptive_field_shift, count.shape[0] - 1)
            count = count[: last + 1]
        final = np.zeros_like(count)
        for i in range(count.shape[0]):
            k = int(spf[i])
            if k == 0:
                continue
            for c in topk_index(count[i], k):
                final[i, c] = 1
        return self._result(final, min_duration_on, min_duration_off)

    def _result(self, final: np.ndarray, min_on: float, min_off: float) -> list[tuple[float, float, int]]:
        m = self.meta
        scale = f32(f32(m.receptive_field_shift) / f32(m.sample_rate))
        offset = f32(0.5 * m.receptive_field_size / m.sample_rate)
        t = final.T
        num_frames = t.shape[1]
        out: list[tuple[float, float, int]] = []
        on, off = f32(min_on), f32(min_off)
        for sp in range(t.shape[0]):
            segs: list[list[Any]] = []
            active = t[sp, 0] > 0
            start = 0 if active else -1
            for fi in range(1, num_frames):
                if active:
                    if t[sp, fi] == 0:
                        segs.append([_fma32(start, scale, offset), _fma32(fi, scale, offset)])
                        active = False
                elif t[sp, fi] == 1:
                    active, start = True, fi
            if active:
                segs.append([_fma32(start, scale, offset), _fma32(num_frames - 1, scale, offset)])
            changed = True
            while changed:  # MergeSegments with Segment::Merge(gap = min_duration_off)
                changed = False
                for i in range(len(segs) - 1):
                    a, b = segs[i], segs[i + 1]
                    if a[1] < b[0] and f32(a[1] + off) >= b[0]:
                        segs[i] = [a[0], b[1]]
                    elif b[1] < a[0] and f32(b[1] + off) >= a[0]:
                        segs[i] = [b[0], a[1]]
                    else:
                        continue
                    del segs[i + 1]
                    changed = True
                    break
            for a, b in segs:
                if f32(b - a) > on:
                    out.append((float(a), float(b), sp))
        out.sort(key=lambda s: s[0])
        return out


def save_precomputed(pre: dict[str, Any], path: str | Path) -> None:
    labels = pre["labels"]
    arrays: dict[str, Any] = {"n": np.int64(pre["n"]), "n_chunks": np.int64(len(labels)),
                              "labels": np.stack(labels).astype(np.int8)}
    if len(labels) > 1:
        arrays.update(speakers_per_frame=pre["speakers_per_frame"], pairs=np.asarray(pre["pairs"], dtype=np.int32).reshape(-1, 2),
                      embeddings=pre["embeddings"])
    np.savez_compressed(path, **arrays)


def load_precomputed(path: str | Path) -> dict[str, Any]:
    z = np.load(path)
    pre: dict[str, Any] = {"n": int(z["n"]), "labels": [x.astype(np.int32) for x in z["labels"]]}
    if int(z["n_chunks"]) > 1:
        pre.update(speakers_per_frame=z["speakers_per_frame"], pairs=[tuple(map(int, p)) for p in z["pairs"]],
                   embeddings=z["embeddings"].astype(np.float32))
    return pre


# --- command line (scripts/calibrate-sherpa.sh) -------------------------------------------


def _stages(threads: int) -> SherpaStages:
    from . import model_store
    from .whisper_sherpa import EMBEDDING_ASSET, SEGMENTATION_ASSET

    seg, _ = model_store.require(SEGMENTATION_ASSET)
    emb, _ = model_store.require(EMBEDDING_ASSET)
    return SherpaStages(seg, emb, num_threads=threads)


def _hyp_doc(meeting_id: str, turns: list[tuple[float, float, str]], threshold: float) -> dict[str, Any]:
    return {
        "schema": "plaud-harness/hypothesis/1",
        "meeting_id": meeting_id,
        "system": f"sherpa-onnx-diarization@{threshold:g}:replayed",
        "segments": [{"speaker": s, "start": a, "end": b, "text": ""} for a, b, s in turns],
        "extra": {"diarization": {"settings": {"cluster_threshold": threshold, "min_duration_on": 0.3,
                                               "min_duration_off": 0.5},
                                  "turns": [[a, b, s] for a, b, s in turns]},
                  "note": "pipeline/sherpa_sweep.py: sherpa-onnx's stages replayed from cached embeddings"},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import types

    from .base import load_audio
    from .whisper_sherpa import sherpa_turns

    ap = argparse.ArgumentParser(prog="python -m pipeline.sherpa_sweep")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("precompute", help="segmentation + embeddings of one meeting -> .npz")
    pc.add_argument("--meeting-dir", required=True)
    pc.add_argument("--out", required=True)
    pc.add_argument("--threads", type=int, default=4)
    sw = sub.add_parser("sweep", help="hyp.json per threshold and meeting from cached .npz files")
    sw.add_argument("--pre-dir", required=True, help="<meeting_id>.npz files")
    sw.add_argument("--refs", required=True, help="the meeting directories' root")
    sw.add_argument("--thresholds", required=True, help="space-separated")
    sw.add_argument("--out", required=True, help="writes <out>/<threshold>/<meeting_id>/hyp.json")
    tu = sub.add_parser("turns", help="hyp.json for one threshold from a .npz")
    tu.add_argument("--pre", required=True)
    tu.add_argument("--meeting-dir", required=True)
    tu.add_argument("--threshold", type=float, required=True)
    tu.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "precompute":
        audio = load_audio(Path(a.meeting_dir) / "mix.wav", 16000)
        pre = _stages(a.threads).precompute(audio.pcm)
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part.npz")
        save_precomputed(pre, tmp)
        tmp.replace(out)
        print(json.dumps({"meeting_dir": a.meeting_dir, "n": pre["n"], "chunks": len(pre["labels"]),
                          "embeddings": list(pre.get("embeddings", np.zeros((0, 0))).shape)}))
        return 0
    if a.cmd == "sweep":
        st = _stages(1)
        grid = [float(t) for t in a.thresholds.split()]
        done = 0
        for npz in sorted(Path(a.pre_dir).glob("*.npz")):
            if npz.name.endswith(".part.npz"):
                continue
            meeting = json.loads((Path(a.refs) / npz.stem / "meeting.json").read_text())
            pre = load_precomputed(npz)
            for t in grid:
                raw = st.turns_for(pre, t)
                turns = sherpa_turns([types.SimpleNamespace(start=x, end=y, speaker=s) for x, y, s in raw], pre["n"] / 16000.0)
                out = Path(a.out) / f"{t:g}" / meeting["meeting_id"] / "hyp.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(_hyp_doc(meeting["meeting_id"], turns, t), indent=1) + "\n")
                done += 1
        print(json.dumps({"hypotheses": done, "thresholds": len(grid)}))
        return 0
    meeting = json.loads((Path(a.meeting_dir) / "meeting.json").read_text())
    st = _stages(1)
    pre = load_precomputed(a.pre)
    raw = st.turns_for(pre, a.threshold)
    turns = sherpa_turns([types.SimpleNamespace(start=x, end=y, speaker=s) for x, y, s in raw], pre["n"] / 16000.0)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_hyp_doc(meeting["meeting_id"], turns, a.threshold), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
