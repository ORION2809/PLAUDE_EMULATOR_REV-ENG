"""``energy-vad-cluster``: a genuinely model-free diarizer.

No neural model, no downloaded weights, no librosa.  numpy + scipy only:

  1. frame energy VAD with a noise-floor-relative threshold and hangover;
  2. MFCC features (pre-emphasis, Hamming, |rFFT|^2, HTK mel filterbank,
     log, DCT-II ortho) with per-file cepstral mean normalisation;
  3. fixed-length chunks inside each speech region, one mean vector each;
  4. agglomerative (average-linkage, euclidean) clustering, cut either at
     a known speaker count or at a distance threshold;
  5. segment smoothing: adjacent same-label chunks merge, short islands are
     absorbed by their longer neighbour, sub-minimum segments dropped.

It produces NO text (a DER/JER-only system).  Every number below is a
HARNESS_POLICY default: the thresholds were chosen on the synthetic sources
in tests/test_pipeline_energy_vad.py, not on speech, and the docs say so.
The only device-derived value is the 16 kHz working rate inherited from
``pipeline.base.DEVICE_SAMPLE_RATE_HZ`` (BYTECODE_PROVEN, see base.py).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.fft import dct, rfft

from .base import (
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    Pipeline,
    PipelineConfig,
    Turn,
    register,
)


@dataclass
class VadParams:
    """HARNESS_POLICY defaults for the energy VAD."""

    frame_ms: float = 25.0
    hop_ms: float = 10.0
    threshold_db: float = 12.0  # above the estimated noise floor
    floor_percentile: float = 10.0  # frame-energy percentile taken as the floor
    hangover_ms: float = 150.0  # keep "speech" this long after the last loud frame
    min_speech_ms: float = 200.0  # drop speech runs shorter than this
    min_silence_ms: float = 150.0  # bridge silences shorter than this
    absolute_floor_db: float = -60.0  # frames below this are never speech


@dataclass
class MfccParams:
    """HARNESS_POLICY defaults for the MFCC front end."""

    n_mfcc: int = 13
    n_mels: int = 26
    frame_ms: float = 25.0
    hop_ms: float = 10.0
    fmin_hz: float = 50.0
    fmax_hz: float | None = None  # None -> Nyquist
    preemphasis: float = 0.97
    drop_c0: bool = True  # c0 is loudness; speaker identity lives in c1..


@dataclass
class ClusterParams:
    """HARNESS_POLICY defaults for chunking/clustering/smoothing."""

    chunk_s: float = 1.0
    min_chunk_s: float = 0.4  # a trailing chunk shorter than this joins its predecessor
    linkage: str = "average"
    distance_threshold: float = 2.5  # in units of the per-dimension frame std; see docs/pipeline.md
    min_segment_s: float = 0.3  # islands shorter than this are absorbed
    merge_gap_s: float = 0.3  # same-speaker segments closer than this merge


# --- framing & VAD ------------------------------------------------------------


def frame_signal(x: np.ndarray, frame_len: int, hop: int) -> np.ndarray:
    """(n_frames, frame_len) view-free copy; zero-pads the tail frame."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if frame_len <= 0 or hop <= 0:
        raise ValueError("frame_len and hop must be positive")
    if len(x) == 0:
        return np.zeros((0, frame_len), dtype=np.float32)
    n_frames = 1 + max(0, int(np.ceil((len(x) - frame_len) / hop))) if len(x) > frame_len else 1
    pad = (n_frames - 1) * hop + frame_len - len(x)
    if pad > 0:
        x = np.concatenate([x, np.zeros(pad, dtype=np.float32)])
    idx = np.arange(frame_len)[None, :] + hop * np.arange(n_frames)[:, None]
    return x[idx]


def frame_energy_db(frames: np.ndarray) -> np.ndarray:
    """10*log10(mean square) per frame, floored at -120 dB."""
    e = np.mean(frames.astype(np.float64) ** 2, axis=1)
    return 10.0 * np.log10(np.maximum(e, 1e-12))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[(start_idx, end_idx_exclusive), ...] of True runs."""
    out: list[tuple[int, int]] = []
    if mask.size == 0:
        return out
    m = mask.astype(np.int8)
    d = np.diff(np.concatenate([[0], m, [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def energy_vad(pcm: np.ndarray, sample_rate: int, params: VadParams | None = None) -> tuple[np.ndarray, float]:
    """Return (speech_mask per frame, hop_s).

    Threshold = max(absolute_floor_db, floor + threshold_db) where floor is
    the ``floor_percentile`` of frame energies.  Hangover extends every
    speech run by ``hangover_ms``; then runs shorter than ``min_speech_ms``
    are dropped and gaps shorter than ``min_silence_ms`` are bridged.
    """
    p = params or VadParams()
    frame_len = max(1, int(round(sample_rate * p.frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * p.hop_ms / 1000.0)))
    frames = frame_signal(pcm, frame_len, hop)
    hop_s = hop / float(sample_rate)
    if frames.shape[0] == 0:
        return np.zeros(0, dtype=bool), hop_s
    e = frame_energy_db(frames)
    floor = float(np.percentile(e, p.floor_percentile))
    thr = max(p.absolute_floor_db, floor + p.threshold_db)
    active = e > thr
    # hangover
    hang = int(round(p.hangover_ms / p.hop_ms))
    if hang > 0 and active.any():
        ext = active.copy()
        for s, t in _runs(active):
            ext[t : min(len(ext), t + hang)] = True
        active = ext
    # bridge short silences
    min_sil = int(round(p.min_silence_ms / p.hop_ms))
    if min_sil > 0:
        for s, t in _runs(~active):
            if t - s < min_sil and s > 0 and t < len(active):
                active[s:t] = True
    # drop short speech
    min_sp = int(round(p.min_speech_ms / p.hop_ms))
    if min_sp > 0:
        for s, t in _runs(active):
            if t - s < min_sp:
                active[s:t] = False
    return active, hop_s


def mask_to_regions(mask: np.ndarray, hop_s: float, frame_s: float = 0.0) -> list[tuple[float, float]]:
    """Frame mask -> [(start_s, end_s)].  ``end`` includes the last frame's
    hop (plus ``frame_s - hop_s`` if the caller wants frame extent)."""
    out = []
    for s, t in _runs(mask):
        out.append((s * hop_s, t * hop_s + max(0.0, frame_s - hop_s)))
    return out


# --- MFCC --------------------------------------------------------------------


def hz_to_mel(f: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def mel_to_hz(m: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)


def mel_filterbank(sample_rate: int, n_fft: int, n_mels: int, fmin: float, fmax: float | None) -> np.ndarray:
    """(n_mels, n_fft//2 + 1) triangular HTK-style filters."""
    fmax = float(fmax) if fmax else sample_rate / 2.0
    n_bins = n_fft // 2 + 1
    mel_pts = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    hz_pts = mel_to_hz(mel_pts)
    bins = np.floor((n_fft + 1) * hz_pts / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    fb = np.zeros((n_mels, n_bins), dtype=np.float64)
    for m in range(1, n_mels + 1):
        lo, c, hi = bins[m - 1], bins[m], bins[m + 1]
        if c == lo:
            c = min(lo + 1, n_bins - 1)
        if hi == c:
            hi = min(c + 1, n_bins - 1)
        for k in range(lo, c):
            fb[m - 1, k] = (k - lo) / float(c - lo)
        for k in range(c, hi):
            fb[m - 1, k] = (hi - k) / float(hi - c)
    return fb


def mfcc(pcm: np.ndarray, sample_rate: int, params: MfccParams | None = None) -> tuple[np.ndarray, float]:
    """Return (features (n_frames, n_coeffs), hop_s)."""
    p = params or MfccParams()
    x = np.asarray(pcm, dtype=np.float64).reshape(-1)
    if p.preemphasis:
        x = np.concatenate([[x[0]], x[1:] - p.preemphasis * x[:-1]]) if len(x) > 1 else x
    frame_len = max(1, int(round(sample_rate * p.frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * p.hop_ms / 1000.0)))
    frames = frame_signal(x, frame_len, hop).astype(np.float64)
    hop_s = hop / float(sample_rate)
    if frames.shape[0] == 0:
        return np.zeros((0, p.n_mfcc - (1 if p.drop_c0 else 0))), hop_s
    n_fft = 1 << int(np.ceil(np.log2(frame_len)))
    win = np.hamming(frame_len)
    spec = np.abs(rfft(frames * win, n=n_fft, axis=1)) ** 2
    fb = mel_filterbank(sample_rate, n_fft, p.n_mels, p.fmin_hz, p.fmax_hz)
    mel = np.log(spec @ fb.T + 1e-10)
    c = dct(mel, type=2, norm="ortho", axis=1)[:, : p.n_mfcc]
    if p.drop_c0:
        c = c[:, 1:]
    return c, hop_s


# --- chunking, clustering, smoothing -------------------------------------------


def chunk_regions(regions: list[tuple[float, float]], chunk_s: float, min_chunk_s: float) -> list[tuple[float, float]]:
    """Split each region into ~chunk_s pieces; a short tail joins its predecessor."""
    out: list[tuple[float, float]] = []
    for r0, r1 in regions:
        t = r0
        pieces: list[tuple[float, float]] = []
        while t < r1 - 1e-9:
            e = min(r1, t + chunk_s)
            pieces.append((t, e))
            t = e
        if len(pieces) >= 2 and (pieces[-1][1] - pieces[-1][0]) < min_chunk_s:
            a, b = pieces[-2], pieces[-1]
            pieces[-2:] = [(a[0], b[1])]
        out.extend(pieces)
    return out


def chunk_embeddings(feats: np.ndarray, hop_s: float, chunks: list[tuple[float, float]]) -> np.ndarray:
    """Mean feature vector per chunk (chunks with no frames get zeros)."""
    if not chunks:
        return np.zeros((0, feats.shape[1] if feats.ndim == 2 else 0))
    emb = np.zeros((len(chunks), feats.shape[1]), dtype=np.float64)
    for i, (a, b) in enumerate(chunks):
        ia, ib = int(np.floor(a / hop_s)), int(np.ceil(b / hop_s))
        ia, ib = max(0, ia), min(len(feats), max(ib, ia + 1))
        if ib > ia:
            emb[i] = feats[ia:ib].mean(axis=0)
    return emb


def cluster_embeddings(
    emb: np.ndarray,
    *,
    num_speakers: int | None,
    distance_threshold: float,
    method: str = "average",
) -> np.ndarray:
    """Return integer labels (0..k-1) for the rows of ``emb``."""
    n = emb.shape[0]
    if n == 0:
        return np.zeros(0, dtype=int)
    if n == 1:
        return np.zeros(1, dtype=int)
    if num_speakers is not None and num_speakers >= n:
        return np.arange(n)
    z = linkage(emb, method=method, metric="euclidean")
    if num_speakers is not None:
        labels = fcluster(z, t=max(1, int(num_speakers)), criterion="maxclust")
    else:
        labels = fcluster(z, t=float(distance_threshold), criterion="distance")
    return labels - 1


def _relabel_by_first_appearance(turns: list[Turn]) -> list[Turn]:
    order: dict[str, str] = {}
    out = []
    for t0, t1, lab in turns:
        if lab not in order:
            order[lab] = f"spk{len(order)}"
        out.append((t0, t1, order[lab]))
    return out


def smooth_turns(turns: list[Turn], *, min_segment_s: float, merge_gap_s: float) -> list[Turn]:
    """Merge adjacent same-label turns, absorb short islands, drop residue.

    Islands: a turn shorter than ``min_segment_s`` sandwiched (gap <
    merge_gap_s on both sides) between two turns of the same label takes
    that label.  A short turn adjacent to only one neighbour joins it.
    """
    if not turns:
        return []
    ts = sorted(turns)

    def merge(seq: list[Turn]) -> list[Turn]:
        out: list[Turn] = []
        for t0, t1, lab in seq:
            if out and out[-1][2] == lab and t0 - out[-1][1] <= merge_gap_s + 1e-9:
                out[-1] = (out[-1][0], max(out[-1][1], t1), lab)
            else:
                out.append((t0, t1, lab))
        return out

    ts = merge(ts)
    changed = True
    while changed:
        changed = False
        for i, (t0, t1, lab) in enumerate(ts):
            if t1 - t0 >= min_segment_s:
                continue
            prev = ts[i - 1] if i > 0 and t0 - ts[i - 1][1] <= merge_gap_s + 1e-9 else None
            nxt = ts[i + 1] if i + 1 < len(ts) and ts[i + 1][0] - t1 <= merge_gap_s + 1e-9 else None
            new_lab = None
            if prev and nxt and prev[2] == nxt[2]:
                new_lab = prev[2]
            elif prev and not nxt:
                new_lab = prev[2]
            elif nxt and not prev:
                new_lab = nxt[2]
            elif prev and nxt:
                new_lab = prev[2] if (prev[1] - prev[0]) >= (nxt[1] - nxt[0]) else nxt[2]
            if new_lab is not None and new_lab != lab:
                ts[i] = (t0, t1, new_lab)
                ts = merge(ts)
                changed = True
                break
    return [t for t in ts if t[1] - t[0] >= min_segment_s]


@dataclass
class DiarizationTrace:
    """What the diarizer did, for tests and for the hypothesis ``extra``."""

    regions: list[tuple[float, float]]
    chunks: list[tuple[float, float]]
    labels: list[int]
    n_clusters: int
    params: dict[str, Any]


class EnergyVadClusterDiarizer(Diarizer):
    name = "energy-vad-cluster"

    def __init__(
        self,
        vad: VadParams | None = None,
        mfcc_params: MfccParams | None = None,
        cluster: ClusterParams | None = None,
    ) -> None:
        self.vad = vad or VadParams()
        self.mfcc = mfcc_params or MfccParams()
        self.cluster = cluster or ClusterParams()
        self.last_trace: DiarizationTrace | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "EnergyVadClusterDiarizer":
        """Build from a flat ``--param`` bag (unknown keys are ignored)."""
        v, m, c = VadParams(), MfccParams(), ClusterParams()
        for obj in (v, m, c):
            for k in asdict(obj):
                if k in params:
                    setattr(obj, k, type(getattr(obj, k))(params[k]) if getattr(obj, k) is not None else params[k])
        return cls(v, m, c)

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        mask, hop_s = energy_vad(pcm, sample_rate, self.vad)
        regions = mask_to_regions(mask, hop_s)
        chunks = chunk_regions(regions, self.cluster.chunk_s, self.cluster.min_chunk_s)
        feats, f_hop = mfcc(pcm, sample_rate, self.mfcc)
        if feats.shape[0] and mask.size:
            # cepstral mean normalisation over speech frames only (HARNESS_POLICY)
            n = min(len(mask), len(feats))
            sp = feats[:n][mask[:n]]
            if len(sp) >= 2:
                mu, sd = sp.mean(axis=0), sp.std(axis=0)
                feats = (feats - mu) / np.maximum(sd, 1e-6)
        emb = chunk_embeddings(feats, f_hop, chunks)
        labels = cluster_embeddings(
            emb,
            num_speakers=num_speakers,
            distance_threshold=self.cluster.distance_threshold,
            method=self.cluster.linkage,
        )
        raw: list[Turn] = [(a, b, f"c{int(l)}") for (a, b), l in zip(chunks, labels)]
        turns = smooth_turns(
            raw, min_segment_s=self.cluster.min_segment_s, merge_gap_s=self.cluster.merge_gap_s
        )
        turns = _relabel_by_first_appearance(turns)
        self.last_trace = DiarizationTrace(
            regions=regions,
            chunks=chunks,
            labels=[int(l) for l in labels],
            n_clusters=len({t[2] for t in turns}),
            params={"vad": asdict(self.vad), "mfcc": asdict(self.mfcc), "cluster": asdict(self.cluster)},
        )
        return turns


class EnergyVadClusterPipeline(ComposedPipeline):
    name = "energy-vad-cluster"
    description = "model-free: energy VAD + MFCC + agglomerative clustering; no ASR (DER/JER only)"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        cfg = config or PipelineConfig(name=self.name)
        super().__init__(None, EnergyVadClusterDiarizer.from_params(cfg.params), cfg, name=self.name)

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        hyp = super().run(audio_path, meeting_dir)
        tr = self.diarizer.last_trace  # type: ignore[union-attr]
        if tr is not None:
            hyp.extra["diarization"] = {
                "n_clusters": tr.n_clusters,
                "n_regions": len(tr.regions),
                "n_chunks": len(tr.chunks),
                "params": tr.params,
            }
        return hyp


@register(
    "energy-vad-cluster",
    description=EnergyVadClusterPipeline.description,
    is_system_under_test=True,
)
def _make_energy_vad_cluster(config: PipelineConfig | None = None) -> Pipeline:
    return EnergyVadClusterPipeline(config)
