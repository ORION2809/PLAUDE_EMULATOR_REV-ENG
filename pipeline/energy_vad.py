"""``energy-vad-cluster``: a genuinely model-free diarizer.

No neural model, no downloaded weights, no librosa.  numpy + scipy only:

  1. frame-energy VAD with a noise-floor-relative threshold (lowered towards
     the Otsu split of the energy histogram in low-contrast audio) and
     hangover; a file with no energy contrast at all (e.g. back-to-back talk
     with no pause) is handled explicitly instead of being read as silence;
  2. per-frame features: MFCC (pre-emphasis, Hamming, |rFFT|^2, HTK mel
     filterbank, log, DCT-II ortho) with per-file cepstral mean AND variance
     normalisation (CMVN) over speech frames, plus an autocorrelation f0
     track with a voicing strength;
  3. the speech is cut into short *cells* (the unit that gets a label); each
     cell is described by a longer analysis *window* centred on it: the
     window's mean CMVN-MFCC vector plus its median f0 in semitones;
  4. spectral clustering of the cell vectors (self-tuning affinity, Zelnik-
     Manor & Perona 2004).  The speaker count comes from the ``num_speakers``
     hint when given; otherwise from the eigengap of the affinity's spectrum,
     guarded by an absolute distance test (clusters whose average-linkage
     distance is below ``distance_threshold`` are one speaker, which is also
     the explicit k = 1 test) and by a minimum cluster size;
  5. segment smoothing: adjacent same-label cells merge, short islands are
     absorbed by their longer neighbour, sub-minimum segments dropped.

It produces NO text (a DER/JER-only system).  Every number below is a
HARNESS_POLICY default chosen on synthetic sources (the pulse-train voices
of tests/test_pipeline_energy_vad.py and the generator's formant voices),
not on real speech; docs/pipeline.md says so and reports the measurements.
The f0 feature in particular is *synthetic-voice-friendly*: the generator's
voices differ mainly in f0, while real speakers overlap far more in f0.  The
only device-derived value is the 16 kHz working rate inherited from
``pipeline.base.DEVICE_SAMPLE_RATE_HZ`` (BYTECODE_PROVEN, see base.py).

Memory: every whole-file frame computation (energy, MFCC, f0) runs in
blocks (``BLOCK_BYTES``) over a zero-copy strided view, so peak memory is
O(block) + O(frames x features) rather than O(frames x frame_len); the
clustering is O(cells^2) with cells capped at ``max_chunks``.  Results do
not depend on the block size (tested).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.fft import dct, irfft, rfft
from scipy.spatial.distance import cdist, pdist, squareform

from .base import (
    COMMON_AUDIO_PARAMS,
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    ParamError,
    Pipeline,
    PipelineConfig,
    Turn,
    register,
)

#: HARNESS_POLICY: working-set budget per block for the whole-file frame
#: computations (bytes of one complex spectrum block); the frame count per
#: block follows from the FFT size (1024 frames for the MFCC's 512-point FFT,
#: 256 for the f0 tracker's 2048-point FFT).
BLOCK_BYTES = 8 * 2**20


def _block_frames(n_fft: int, block: int | None) -> int:
    if block is not None:
        return max(1, int(block))
    return max(64, BLOCK_BYTES // (16 * (n_fft // 2 + 1)))


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
    # Low-contrast margin: the margin above the floor is lowered to the
    # two-class (Otsu) split of the frame-energy histogram when that split is
    # closer to the floor, but never below min_threshold_db.
    min_threshold_db: float = 6.0
    # No-contrast handling: when the (contrast_percentile - floor_percentile)
    # energy spread is below threshold_db the percentile floor is not a noise
    # floor.  Such a file is treated as dense speech if it is loud enough and
    # periodic enough; otherwise the ordinary rule applies (and finds nothing).
    contrast_percentile: float = 90.0
    dense_min_db: float = -40.0  # median frame energy (dBFS) a dense file must reach
    dense_min_voiced: float = 0.3  # fraction of frames that must be periodic (f0 tracker)


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
class PitchParams:
    """HARNESS_POLICY defaults for the autocorrelation f0 tracker.

    The tracker shares the MFCC hop so its frames index the same times.
    Per frame: mean-removed window, autocorrelation via FFT, normalised by
    lag 0 and unbiased (x N/(N-lag)); the chosen lag is the FIRST local peak
    within ``f0_peak_frac`` of the highest peak in [1/f0_max, 1/f0_min]
    (a guard against sub-octave picks), refined by parabolic interpolation.
    ``voicing_threshold`` on the normalised peak marks a frame voiced.
    """

    pitch_frame_ms: float = 40.0
    f0_min_hz: float = 60.0
    f0_max_hz: float = 400.0
    f0_peak_frac: float = 0.85
    voicing_threshold: float = 0.5


@dataclass
class ClusterParams:
    """HARNESS_POLICY defaults for cells, features, clustering, smoothing."""

    chunk_s: float = 0.5  # cell length: the unit that receives a speaker label
    window_s: float = 1.5  # analysis window centred on each cell (clipped to its speech region)
    min_chunk_s: float = 0.2  # a trailing cell shorter than this joins its predecessor
    max_chunks: int = 2000  # more cells than this: cluster an evenly spaced subset, assign the rest
    f0_weight: float = 1.0  # embedding units per semitone of window-median f0 (0 disables f0)
    min_voiced_frames: int = 5  # a window with fewer voiced frames has no f0
    octave_fix_frac: float = 0.25  # see window_features: prefer the lower octave (0 disables)
    linkage: str = "average"  # linkage of the distance guard
    distance_threshold: float = 2.0  # clusters closer than this (average linkage) are one speaker
    max_speakers: int = 8
    affinity_knn: int = 7  # self-tuning scale: distance to this neighbour (capped at n // 4)
    min_cluster_frac: float = 0.02  # clusters smaller than this fraction of cells ...
    min_cluster_chunks: int = 3  # ... or than this many cells (1.5 s at 0.5 s cells) are not speakers
    min_segment_s: float = 0.3  # islands shorter than this are absorbed
    merge_gap_s: float = 0.3  # same-speaker segments closer than this merge


# --- framing -------------------------------------------------------------------------


def n_frames_for(n_samples: int, frame_len: int, hop: int) -> int:
    """Frame count with a zero-padded tail frame (0 for an empty signal)."""
    if n_samples <= 0:
        return 0
    if n_samples <= frame_len:
        return 1
    return 1 + int(math.ceil((n_samples - frame_len) / hop))


def _frames(x: np.ndarray, frame_len: int, hop: int, i0: int, i1: int) -> np.ndarray:
    """Frames ``i0..i1-1`` of ``x`` as a (possibly strided) view; zero-padded
    past the end of ``x``.  Only the tail block ever copies samples."""
    start, stop = i0 * hop, (i1 - 1) * hop + frame_len
    seg = x[start : min(stop, len(x))]
    if len(seg) < stop - start:
        seg = np.concatenate([seg, np.zeros(stop - start - len(seg), dtype=x.dtype)])
    return sliding_window_view(seg, frame_len)[::hop]


def _prev_frames(x: np.ndarray, frame_len: int, hop: int, i0: int, i1: int) -> np.ndarray:
    """Frames of the one-sample-delayed signal ``prev[p] = x[p-1]`` (``prev[0]
    = 0``, and 0 past the end of ``x``), without a full-length shifted copy."""
    start, stop = i0 * hop, (i1 - 1) * hop + frame_len
    end = min(stop, len(x))
    seg = x[max(0, start - 1) : max(0, end - 1)]
    if start == 0:
        seg = np.concatenate([np.zeros(1, dtype=x.dtype), seg])
    if len(seg) < stop - start:
        seg = np.concatenate([seg, np.zeros(stop - start - len(seg), dtype=x.dtype)])
    return sliding_window_view(seg, frame_len)[::hop]


def frame_signal(x: np.ndarray, frame_len: int, hop: int) -> np.ndarray:
    """(n_frames, frame_len) copy; zero-pads the tail frame.  Test/debug helper:
    the diarizer itself never materialises the whole-file frame matrix."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if frame_len <= 0 or hop <= 0:
        raise ValueError("frame_len and hop must be positive")
    n = n_frames_for(len(x), frame_len, hop)
    if n == 0:
        return np.zeros((0, frame_len), dtype=np.float32)
    return np.array(_frames(x, frame_len, hop, 0, n))


def frame_energy_db(frames: np.ndarray) -> np.ndarray:
    """10*log10(mean square) per frame, floored at -120 dB."""
    e = np.mean(frames.astype(np.float64) ** 2, axis=1)
    return 10.0 * np.log10(np.maximum(e, 1e-12))


def frame_energies_db(x: np.ndarray, frame_len: int, hop: int, block: int | None = None) -> np.ndarray:
    """``frame_energy_db`` over the whole signal, computed block by block."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    n = n_frames_for(len(x), frame_len, hop)
    block = block if block is not None else max(64, BLOCK_BYTES // (8 * max(1, frame_len)))
    out = np.empty(n, dtype=np.float64)
    for i in range(0, n, block):
        j = min(n, i + block)
        out[i:j] = frame_energy_db(_frames(x, frame_len, hop, i, j))
    return out


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


# --- pitch ----------------------------------------------------------------------------


def pitch_track(
    pcm: np.ndarray,
    sample_rate: int,
    hop: int,
    params: PitchParams | None = None,
    n_frames: int | None = None,
    frame_index: np.ndarray | None = None,
    block: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (f0_hz, voicing_strength) per frame (frame i starts at i*hop).

    ``n_frames`` defaults to the count a ``pitch_frame_ms`` framing gives; the
    diarizer passes the MFCC frame count so both index the same frames.
    ``frame_index`` restricts the computation to those frames (the VAD's
    no-contrast check samples a subset).
    """
    p = params or PitchParams()
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    fl = max(8, int(round(sample_rate * p.pitch_frame_ms / 1000.0)))
    if n_frames is None:
        n_frames = n_frames_for(len(x), fl, hop)
    idx = np.arange(n_frames) if frame_index is None else np.asarray(frame_index, dtype=np.int64)
    f0 = np.zeros(len(idx))
    st = np.zeros(len(idx))
    if len(idx) == 0 or len(x) == 0:
        return f0, st
    lo = max(2, int(math.floor(sample_rate / p.f0_max_hz)))
    hi = max(lo + 1, min(int(math.ceil(sample_rate / p.f0_min_hz)), fl // 2))
    n_fft = 1 << int(math.ceil(math.log2(2 * fl)))
    block = _block_frames(n_fft, block)
    unbias = fl / (fl - np.arange(hi + 2, dtype=np.float64))
    for b0 in range(0, len(idx), block):
        sel = idx[b0 : b0 + block]
        if frame_index is None:
            fr = _frames(x, fl, hop, int(sel[0]), int(sel[-1]) + 1).astype(np.float64)
        else:
            fr = np.stack([_frames(x, fl, hop, int(i), int(i) + 1)[0] for i in sel]).astype(np.float64)
        fr = fr - fr.mean(axis=1, keepdims=True)
        ac = irfft(np.abs(rfft(fr, n=n_fft, axis=1)) ** 2, n=n_fft, axis=1)[:, : hi + 2]
        r = ac / np.maximum(ac[:, :1], 1e-12) * unbias[None, :]
        seg = r[:, lo - 1 : hi + 2]  # lags lo-1 .. hi+1
        mid = seg[:, 1:-1]  # lags lo .. hi
        peak = (mid >= seg[:, :-2]) & (mid >= seg[:, 2:])
        rmax = np.where(peak, mid, -np.inf).max(axis=1)
        ok = peak & (mid >= p.f0_peak_frac * rmax[:, None])
        kk = np.where(ok.any(axis=1), ok.argmax(axis=1), mid.argmax(axis=1))
        lag = lo + kk
        rows = np.arange(len(r))
        a, c0, c = r[rows, lag - 1], r[rows, lag], r[rows, lag + 1]
        den = a - 2.0 * c0 + c
        safe = np.abs(den) > 1e-12
        delta = np.clip(np.where(safe, 0.5 * (a - c) / np.where(safe, den, 1.0), 0.0), -0.5, 0.5)
        f0[b0 : b0 + len(sel)] = sample_rate / (lag + delta)
        st[b0 : b0 + len(sel)] = np.where(ac[:, 0] > 1e-12, c0, 0.0)
    return f0, st


# --- VAD ---------------------------------------------------------------------------


def otsu_threshold(values: np.ndarray, bin_db: float = 0.5) -> float:
    """Two-class split (Otsu 1979) of a 1-D sample via a fixed-width histogram:
    the bin edge that maximises the between-class variance."""
    v = np.asarray(values, dtype=np.float64)
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 2 * bin_db:
        return hi
    h, edges = np.histogram(v, bins=max(2, int(math.ceil((hi - lo) / bin_db))), range=(lo, hi))
    c = 0.5 * (edges[:-1] + edges[1:])
    w0 = np.cumsum(h).astype(np.float64)
    w1 = w0[-1] - w0
    s0 = np.cumsum(h * c)
    m0 = s0 / np.maximum(w0, 1.0)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1.0)
    between = w0 * w1 * (m0 - m1) ** 2
    return float(edges[1:][int(np.argmax(between))])


def energy_vad(
    pcm: np.ndarray,
    sample_rate: int,
    params: VadParams | None = None,
    info: dict[str, Any] | None = None,
    pitch: PitchParams | None = None,
) -> tuple[np.ndarray, float]:
    """Return (speech_mask per frame, hop_s).

    Ordinary rule: threshold = max(absolute_floor_db, floor + margin) where
    floor is the ``floor_percentile`` of frame energies and margin is
    ``threshold_db``, lowered (never below ``min_threshold_db``) to the Otsu
    split of the frame energies above ``absolute_floor_db`` when that split
    lies closer to the floor -- at 10 dB SNR, or in reverberant dense talk,
    speech frames sit only ~10-15 dB above the floor and a fixed 12 dB margin
    misses most of them (HARNESS_POLICY; measured in docs/pipeline.md).
    Hangover extends every speech run by ``hangover_ms``; then gaps shorter
    than ``min_silence_ms`` are bridged and runs shorter than
    ``min_speech_ms`` are dropped.

    Low-spread rule (HARNESS_POLICY): when fewer than ~10 % of frames are
    non-speech, or the SNR is low, the percentile "floor" is not a noise
    floor and the ordinary rule rejects nearly everything.  That shows up as
    a spread ``P[contrast_percentile] - P[floor_percentile] < threshold_db``.
    A quiet file (median below ``dense_min_db``) keeps the ordinary rule.
    Otherwise the frame energies are split in two (Otsu) and periodicity
    decides, on a sample of at most 2000 frames:
      * two classes at least ``min_threshold_db`` apart whose QUIETER class is
        mostly aperiodic (voiced fraction < ``dense_min_voiced``): that class
        is noise -- threshold = the split (mode "split");
      * otherwise, if at least ``dense_min_voiced`` of all frames are
        periodic: dense speech -- threshold = max(absolute_floor_db,
        median - threshold_db) (mode "dense", e.g. back-to-back talk);
      * otherwise the ordinary rule stands (mode "flat": stationary noise).
    ``info``, when given, receives the decision.
    """
    p = params or VadParams()
    frame_len = max(1, int(round(sample_rate * p.frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * p.hop_ms / 1000.0)))
    hop_s = hop / float(sample_rate)
    e = frame_energies_db(pcm, frame_len, hop)
    if e.size == 0:
        if info is not None:
            info.update(mode="empty")
        return np.zeros(0, dtype=bool), hop_s
    floor = float(np.percentile(e, p.floor_percentile))
    top = float(np.percentile(e, p.contrast_percentile))
    mode = "contrast"
    margin = p.threshold_db
    split = None
    loud = e[e > p.absolute_floor_db]
    if len(loud) >= 10:
        split = otsu_threshold(loud)
        margin = min(p.threshold_db, max(p.min_threshold_db, split - floor))
    thr = max(p.absolute_floor_db, floor + margin)
    voiced_frac = None
    if top - floor < p.threshold_db:
        mode = "flat"
        thr = max(p.absolute_floor_db, floor + p.threshold_db)
        level = float(np.median(e))
        if level >= p.dense_min_db:
            sample = np.unique(np.linspace(0, len(e) - 1, num=min(len(e), 2000)).astype(np.int64))
            _, strength = pitch_track(pcm, sample_rate, hop, pitch, n_frames=len(e), frame_index=sample)
            voiced = strength > (pitch or PitchParams()).voicing_threshold
            voiced_frac = float(np.mean(voiced))
            quiet_voiced = None
            if split is not None:
                lo_cls, hi_cls = loud[loud <= split], loud[loud > split]
                low = e[sample] <= split
                if len(lo_cls) and len(hi_cls) and hi_cls.mean() - lo_cls.mean() >= p.min_threshold_db and low.any():
                    quiet_voiced = float(np.mean(voiced[low]))
            if quiet_voiced is not None and quiet_voiced < p.dense_min_voiced:
                mode = "split"
                thr = max(p.absolute_floor_db, split)
            elif voiced_frac >= p.dense_min_voiced:
                mode = "dense"
                thr = max(p.absolute_floor_db, level - p.threshold_db)
    if info is not None:
        info.update(
            mode=mode,
            floor_db=floor,
            contrast_db=top - floor,
            otsu_db=split,
            threshold_db=thr,
            voiced_fraction=voiced_frac,
        )
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


def mfcc(
    pcm: np.ndarray, sample_rate: int, params: MfccParams | None = None, block: int | None = None
) -> tuple[np.ndarray, float]:
    """Return (features (n_frames, n_coeffs), hop_s), computed block by block.

    Pre-emphasis ``y[n] = x[n] - a*x[n-1]`` (``y[0] = x[0]``) is applied per
    frame from a one-sample-shifted view, and the zero-padded tail stays zero,
    so the result is the whole-signal computation's, independent of ``block``.
    """
    p = params or MfccParams()
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    frame_len = max(1, int(round(sample_rate * p.frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * p.hop_ms / 1000.0)))
    hop_s = hop / float(sample_rate)
    n_out = p.n_mfcc - (1 if p.drop_c0 else 0)
    n = n_frames_for(len(x), frame_len, hop)
    if n == 0:
        return np.zeros((0, n_out)), hop_s
    n_fft = 1 << int(math.ceil(math.log2(frame_len)))
    block = _block_frames(n_fft, block)
    win = np.hamming(frame_len)
    fb_t = mel_filterbank(sample_rate, n_fft, p.n_mels, p.fmin_hz, p.fmax_hz).T.copy()
    out = np.empty((n, n_out), dtype=np.float64)
    for i in range(0, n, block):
        j = min(n, i + block)
        fr = _frames(x, frame_len, hop, i, j).astype(np.float64)
        if p.preemphasis:
            fr -= p.preemphasis * _prev_frames(x, frame_len, hop, i, j).astype(np.float64)
        spec = np.abs(rfft(fr * win, n=n_fft, axis=1)) ** 2
        mel = np.log(np.einsum("ij,jk->ik", spec, fb_t) + 1e-10)
        c = dct(mel, type=2, norm="ortho", axis=1)[:, : p.n_mfcc]
        out[i:j] = c[:, 1:] if p.drop_c0 else c
    return out, hop_s


# --- cells, windows, features ----------------------------------------------------------


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


def cell_windows(
    regions: list[tuple[float, float]], cells: list[tuple[float, float]], window_s: float
) -> list[tuple[float, float]]:
    """A window of ``max(window_s, cell length)`` centred on each cell,
    clipped to the speech region that contains the cell."""
    out: list[tuple[float, float]] = []
    ri = 0
    for a, b in cells:
        while ri < len(regions) - 1 and regions[ri][1] < b - 1e-9:
            ri += 1
        r0, r1 = regions[ri] if regions else (a, b)
        c, h = 0.5 * (a + b), 0.5 * max(window_s, b - a)
        out.append((max(r0, c - h), min(r1, c + h)))
    return out


def _frame_span(a: float, b: float, hop_s: float, n: int) -> tuple[int, int]:
    ia = max(0, min(n - 1, int(math.floor(a / hop_s + 1e-9)))) if n else 0
    ib = min(n, max(int(math.ceil(b / hop_s - 1e-9)), ia + 1))
    return ia, ib


def window_features(
    feats: np.ndarray,
    f0: np.ndarray,
    strength: np.ndarray,
    hop_s: float,
    windows: list[tuple[float, float]],
    params: ClusterParams,
    pitch: PitchParams,
) -> tuple[np.ndarray, np.ndarray]:
    """(mean feature vector per window, median f0 in semitones per window).

    The f0 of a window is NaN when it has fewer than ``min_voiced_frames``
    voiced frames.  Octave guard (HARNESS_POLICY): the autocorrelation
    tracker's typical gross error is an octave-UP pick when a formant sits
    near 2*f0; if at least ``octave_fix_frac`` of a window's voiced frames
    lie an octave (+-3 semitones) below its median, the median of that lower
    group is used instead.
    """
    n = len(feats)
    d = feats.shape[1] if feats.ndim == 2 else 0
    emb = np.zeros((len(windows), d), dtype=np.float64)
    semis = np.full(len(windows), np.nan)
    if n == 0 or not windows:
        return emb, semis
    cs = np.concatenate([np.zeros((1, d)), np.cumsum(feats, axis=0)])
    for i, (a, b) in enumerate(windows):
        ia, ib = _frame_span(a, b, hop_s, n)
        emb[i] = (cs[ib] - cs[ia]) / float(ib - ia)
        jb = min(ib, len(strength))
        if jb <= ia:
            continue
        v = f0[ia:jb][strength[ia:jb] > pitch.voicing_threshold]
        if len(v) < max(1, params.min_voiced_frames):
            continue
        sv = 12.0 * np.log2(v)
        med = float(np.median(sv))
        if params.octave_fix_frac > 0:
            low = sv[np.abs(sv - (med - 12.0)) <= 3.0]
            if len(low) >= params.octave_fix_frac * len(sv):
                med = float(np.median(low))
        semis[i] = med
    return emb, semis


# --- clustering ------------------------------------------------------------------------


def _pairwise(x: np.ndarray, metric: str) -> np.ndarray:
    return squareform(pdist(x, metric=metric))


def _spectrum(dist: np.ndarray, knn: int, n_vec: int) -> tuple[np.ndarray, np.ndarray]:
    """Top ``n_vec`` eigenpairs (descending) of the normalised self-tuning
    affinity  A_ij = exp(-d_ij^2 / (s_i s_j)),  s_i = distance to the knn-th
    neighbour,  L = D^-1/2 A D^-1/2."""
    from scipy.linalg import eigh

    n = len(dist)
    k = max(1, min(knn, n - 1))
    sig = np.maximum(np.partition(dist, k, axis=1)[:, k], 1e-9)
    # in place: one n x n working matrix besides ``dist`` (cells are capped at max_chunks)
    aff = np.square(dist)
    aff /= sig[:, None]
    aff /= sig[None, :]
    np.negative(aff, out=aff)
    np.exp(aff, out=aff)
    np.fill_diagonal(aff, 0.0)
    root = np.sqrt(np.maximum(aff.sum(axis=1), 1e-12))
    aff /= root[:, None]
    aff /= root[None, :]
    m = max(1, min(n_vec, n))
    w, v = eigh(aff, subset_by_index=[n - m, n - 1], overwrite_a=True, check_finite=False)
    order = np.argsort(w, kind="stable")[::-1]
    return w[order], v[:, order]


def kmeans_deterministic(x: np.ndarray, k: int, iters: int = 100) -> np.ndarray:
    """Lloyd's k-means with a deterministic farthest-point initialisation
    (first centre: the point nearest the mean).  HARNESS_POLICY."""
    n = len(x)
    k = max(1, min(k, n))
    centres = [int(np.argmin(((x - x.mean(axis=0)) ** 2).sum(axis=1)))]
    dmin = ((x - x[centres[0]]) ** 2).sum(axis=1)
    for _ in range(1, k):
        nxt = int(np.argmax(dmin))
        centres.append(nxt)
        dmin = np.minimum(dmin, ((x - x[nxt]) ** 2).sum(axis=1))
    c = x[centres].copy()
    lab = np.full(n, -1)
    for _ in range(iters):
        new = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=-1).argmin(axis=1)
        if np.array_equal(new, lab):
            break
        lab = new
        for j in range(k):
            if (lab == j).any():
                c[j] = x[lab == j].mean(axis=0)
    return lab


def _min_cluster_size(n: int, params: ClusterParams) -> int:
    return max(1, int(params.min_cluster_chunks), int(math.ceil(params.min_cluster_frac * n)))


def _absorb_small(x: np.ndarray, labels: np.ndarray, min_size: int, metric: str, keep_k: int | None = None) -> np.ndarray:
    """Clusters smaller than ``min_size`` (or beyond the ``keep_k`` largest)
    are not speakers: their members join the nearest kept centroid.  Labels
    are returned compacted to 0..k-1 in order of size (ties: lower label)."""
    sizes = np.bincount(labels)
    order = [int(c) for c in np.argsort(-sizes, kind="stable") if sizes[c] > 0]
    keep = order[:keep_k] if keep_k is not None else [c for c in order if sizes[c] >= min_size]
    if not keep:
        keep = order[:1]
    centres = np.array([x[labels == c].mean(axis=0) for c in keep])
    out = labels.copy()
    lost = np.flatnonzero(~np.isin(labels, keep))
    if len(lost):
        near = cdist(x[lost], centres, metric=metric).argmin(axis=1)
        out[lost] = np.array(keep)[near]
    remap = {c: j for j, c in enumerate(keep)}
    return np.array([remap[int(c)] for c in out], dtype=int)


def cluster_embeddings(
    emb: np.ndarray,
    *,
    num_speakers: int | None,
    params: ClusterParams | None = None,
    metric: str = "euclidean",
    info: dict[str, Any] | None = None,
) -> np.ndarray:
    """Return integer labels (0..k-1) for the rows of ``emb``.

    With ``num_speakers``: spectral clustering into exactly that many
    clusters (small clusters are absorbed and the cut is retried with a
    larger k so an outlier island cannot take one of the requested slots).
    Without: k = eigengap estimate, capped by the number of average-linkage
    clusters at ``distance_threshold`` that reach the minimum cluster size
    (1 of those means one speaker -- the explicit k = 1 test), then small
    clusters are absorbed.  Fewer than 4 rows: plain agglomerative cut.
    More than ``max_chunks`` rows: an evenly spaced subset of ``max_chunks``
    rows is clustered and every row takes its nearest cluster centroid, so
    the O(n^2) matrices stay bounded (HARNESS_POLICY).
    ``info`` receives k_eigengap, k_threshold, eigenvalues, method.
    """
    p = params or ClusterParams()
    info = info if info is not None else {}
    n = emb.shape[0]
    cap = max(4, int(p.max_chunks))
    if n > cap:
        sub = np.unique(np.linspace(0, n - 1, num=cap).round().astype(np.int64))
        lab_sub = cluster_embeddings(emb[sub], num_speakers=num_speakers, params=p, metric=metric, info=info)
        k = int(lab_sub.max()) + 1
        centres = np.array([emb[sub][lab_sub == j].mean(axis=0) for j in range(k)])
        lab = cdist(emb, centres, metric=metric).argmin(axis=1).astype(int)
        lab[sub] = lab_sub
        info.update(subsampled=int(len(sub)), n=int(n))
        return lab
    if n == 0:
        info.update(method="none", k=0)
        return np.zeros(0, dtype=int)
    if n == 1:
        info.update(method="single", k=1)
        return np.zeros(1, dtype=int)
    if n < 4:
        z = linkage(emb, method=p.linkage, metric=metric)
        if num_speakers is not None:
            lab = fcluster(z, t=max(1, min(int(num_speakers), n)), criterion="maxclust") - 1
        else:
            lab = fcluster(z, t=float(p.distance_threshold), criterion="distance") - 1
        info.update(method="agglomerative", k=int(len(set(lab.tolist()))))
        return _absorb_small(emb, lab, 1, metric)
    min_size = _min_cluster_size(n, p)
    k_cap = max(1, int(p.max_speakers))
    want = None if num_speakers is None else max(1, min(int(num_speakers), n))
    n_vec = min(n, max(k_cap, (want or 1) + 4) + 1)
    knn = max(2, min(int(p.affinity_knn), n // 4))
    evals, evecs = _spectrum(_pairwise(emb, metric), knn, n_vec)

    def spectral_labels(k: int) -> np.ndarray:
        if k <= 1:
            return np.zeros(n, dtype=int)
        u = evecs[:, :k]
        u = u / np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-12)
        return kmeans_deterministic(u, k)

    info.update(method="spectral", affinity_knn=knn, eigenvalues=[float(v) for v in evals[: k_cap + 1]])
    if want is not None:
        lab = np.zeros(n, dtype=int)
        for k_run in range(want, min(want + 4, len(evals)) + 1):
            lab = spectral_labels(k_run)
            if int((np.bincount(lab) >= min_size).sum()) >= want:
                break
        lab = _absorb_small(emb, lab, min_size, metric, keep_k=want)
        info.update(k=int(len(set(lab.tolist()))), hint=want)
        return lab
    km = max(1, min(k_cap, len(evals) - 1))
    gaps = evals[:km] - evals[1 : km + 1]
    k_eig = int(np.argmax(gaps)) + 1 if len(gaps) else 1
    z = linkage(emb, method=p.linkage, metric=metric)
    lt = fcluster(z, t=float(p.distance_threshold), criterion="distance") - 1
    k_thr = int((np.bincount(lt) >= min_size).sum())
    k0 = 1 if k_thr <= 1 else min(k_thr, k_eig)
    lab = _absorb_small(emb, spectral_labels(k0), min_size, metric)
    info.update(k_eigengap=k_eig, k_threshold=k_thr, k=int(len(set(lab.tolist()))))
    return lab


# --- smoothing ---------------------------------------------------------------------------


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


# --- the diarizer ---------------------------------------------------------------------------


@dataclass
class DiarizationTrace:
    """What the diarizer did, for tests and for the hypothesis ``extra``."""

    regions: list[tuple[float, float]]
    chunks: list[tuple[float, float]]
    labels: list[int]
    n_clusters: int
    params: dict[str, Any]
    windows: list[tuple[float, float]] = field(default_factory=list)
    vad: dict[str, Any] = field(default_factory=dict)
    count: dict[str, Any] = field(default_factory=dict)
    n_without_f0: int = 0
    chunk_s: float = 0.0


_PARAM_CLASSES = (VadParams, MfccParams, PitchParams, ClusterParams)
#: every flat ``--param`` key the diarizer reads
DIARIZER_PARAMS = frozenset(f.name for cls in _PARAM_CLASSES for f in fields(cls))


def _coerce(key: str, default: Any, value: Any) -> Any:
    """Coerce a flat --param value to the dataclass field's type (strict)."""
    if default is None:  # fmax_hz: None or a number
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamError(f"{key} must be a number or null, got {value!r}")
        return float(value)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ParamError(f"{key} must be true or false, got {value!r}")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and float(value).is_integer())):
            raise ParamError(f"{key} must be an integer, got {value!r}")
        return int(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ParamError(f"{key} must be a finite number, got {value!r}")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ParamError(f"{key} must be a string, got {value!r}")
        return value
    return value


class EnergyVadClusterDiarizer(Diarizer):
    name = "energy-vad-cluster"

    def __init__(
        self,
        vad: VadParams | None = None,
        mfcc_params: MfccParams | None = None,
        cluster: ClusterParams | None = None,
        pitch: PitchParams | None = None,
    ) -> None:
        self.vad = vad or VadParams()
        self.mfcc = mfcc_params or MfccParams()
        self.cluster = cluster or ClusterParams()
        self.pitch = pitch or PitchParams()
        self.last_trace: DiarizationTrace | None = None

    @classmethod
    def from_params(
        cls, params: dict[str, Any], *, allow: frozenset[str] | set[str] = COMMON_AUDIO_PARAMS
    ) -> "EnergyVadClusterDiarizer":
        """Build from a flat ``--param`` bag.

        Every key must be a field of VadParams/MfccParams/PitchParams/
        ClusterParams or be listed in ``allow`` (pipeline-level keys such as
        ``num_speakers``); anything else raises ParamError.  A key present in
        two dataclasses (``frame_ms``, ``hop_ms``) sets both.
        """
        unknown = sorted(set(params) - DIARIZER_PARAMS - set(allow))
        if unknown:
            raise ParamError(
                f"energy-vad-cluster does not accept parameter(s) {', '.join(unknown)}; "
                f"accepted: {', '.join(sorted(DIARIZER_PARAMS | set(allow)))}"
            )
        objs = (VadParams(), MfccParams(), PitchParams(), ClusterParams())
        for obj in objs:
            for f in fields(obj):
                if f.name in params:
                    setattr(obj, f.name, _coerce(f.name, getattr(obj, f.name), params[f.name]))
        v, m, pt, c = objs
        if c.chunk_s <= 0 or c.window_s <= 0 or c.max_chunks < 1 or c.max_speakers < 1:
            raise ParamError("chunk_s and window_s must be > 0; max_chunks and max_speakers >= 1")
        return cls(v, m, c, pt)

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        c = self.cluster
        vad_info: dict[str, Any] = {}
        mask, hop_s = energy_vad(pcm, sample_rate, self.vad, info=vad_info, pitch=self.pitch)
        regions = mask_to_regions(mask, hop_s)
        cells = chunk_regions(regions, c.chunk_s, c.min_chunk_s)
        windows = cell_windows(regions, cells, max(c.window_s, c.chunk_s))
        count: dict[str, Any] = {}
        n_without_f0 = 0
        labels = np.zeros(len(cells), dtype=int)
        if cells:
            feats, f_hop = mfcc(pcm, sample_rate, self.mfcc)
            n = min(len(mask), len(feats))
            sp = feats[:n][mask[:n]]
            if len(sp) >= 2:
                # CMVN over speech frames only (HARNESS_POLICY)
                feats = (feats - sp.mean(axis=0)) / np.maximum(sp.std(axis=0), 1e-6)
            if c.f0_weight > 0:
                hop = max(1, int(round(sample_rate * self.mfcc.hop_ms / 1000.0)))
                f0, strength = pitch_track(pcm, sample_rate, hop, self.pitch, n_frames=len(feats))
            else:
                f0 = strength = np.zeros(0)
            emb, semis = window_features(feats, f0, strength, f_hop, windows, c, self.pitch)
            anchored = np.isfinite(semis) if c.f0_weight > 0 else np.ones(len(cells), dtype=bool)
            if anchored.sum() < min(3, len(cells)):
                anchored = np.ones(len(cells), dtype=bool)
            n_without_f0 = int((~np.isfinite(semis)).sum()) if c.f0_weight > 0 else len(cells)
            if c.f0_weight > 0:
                centre = float(np.nanmedian(semis)) if np.isfinite(semis).any() else 0.0
                col = np.where(np.isfinite(semis), semis, centre) - centre
                x = np.column_stack([emb, c.f0_weight * col])
            else:
                x = emb
            lab_a = cluster_embeddings(x[anchored], num_speakers=num_speakers, params=c, info=count)
            labels[anchored] = lab_a
            rest = np.flatnonzero(~anchored)
            if len(rest):
                # windows without an f0 join the nearest cluster in MFCC space
                k = int(lab_a.max()) + 1
                centres = np.array([emb[anchored][lab_a == j].mean(axis=0) for j in range(k)])
                labels[rest] = cdist(emb[rest], centres).argmin(axis=1)
        raw: list[Turn] = [(a, b, f"c{int(l)}") for (a, b), l in zip(cells, labels)]
        turns = smooth_turns(raw, min_segment_s=c.min_segment_s, merge_gap_s=c.merge_gap_s)
        turns = _relabel_by_first_appearance(turns)
        self.last_trace = DiarizationTrace(
            regions=regions,
            chunks=cells,
            labels=[int(l) for l in labels],
            n_clusters=len({t[2] for t in turns}),
            params={
                "vad": asdict(self.vad),
                "mfcc": asdict(self.mfcc),
                "pitch": asdict(self.pitch),
                "cluster": asdict(self.cluster),
            },
            windows=windows,
            vad=vad_info,
            count=count,
            n_without_f0=n_without_f0,
            chunk_s=c.chunk_s,
        )
        return turns


class EnergyVadClusterPipeline(ComposedPipeline):
    name = "energy-vad-cluster"
    description = (
        "model-free: energy VAD + MFCC/f0 cells + spectral clustering with an eigengap speaker "
        "count; no ASR (DER/JER only)"
    )

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
                "chunk_s": tr.chunk_s,
                "n_without_f0": tr.n_without_f0,
                "vad": tr.vad,
                "count": tr.count,
                "params": tr.params,
            }
        return hyp


@register(
    "energy-vad-cluster",
    description=EnergyVadClusterPipeline.description,
    is_system_under_test=True,
    params=DIARIZER_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_energy_vad_cluster(config: PipelineConfig | None = None) -> Pipeline:
    return EnergyVadClusterPipeline(config)
