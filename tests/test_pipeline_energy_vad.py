"""energy-vad-cluster: the model-free diarizer against analytically known signals.

Every signal here is built in the test (pipeline.synthetic) so the true
speech regions and speaker identities are known by construction.  The
tests pin the VAD edge behaviour (hangover, min-speech, min-silence),
the MFCC front end's shape and stability, the clustering's ability to
recover TWO distinct synthetic voices (and to NOT split ONE), the
smoothing rule, and an end-to-end DER against a synthetic meeting dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipeline import PipelineConfig, get_pipeline
from pipeline.energy_vad import (
    ClusterParams,
    EnergyVadClusterDiarizer,
    EnergyVadClusterPipeline,
    MfccParams,
    VadParams,
    chunk_regions,
    energy_vad,
    frame_signal,
    mask_to_regions,
    mel_filterbank,
    mfcc,
    smooth_turns,
)
from pipeline.synthetic import render_layout, synthetic_meeting

SR = 16000
LAYOUT = [(0.5, 2.5, "A"), (3.0, 5.0, "B"), (5.5, 7.0, "A"), (7.5, 8.5, "B")]


def tone_bursts(bursts, freq=1000.0, total=None, floor=1e-4, seed=0):
    total = total or (max(e for _, e in bursts) + 0.5)
    rng = np.random.default_rng(seed)
    x = floor * rng.standard_normal(int(total * SR))
    t = np.arange(len(x)) / SR
    for s, e in bursts:
        i0, i1 = int(s * SR), int(e * SR)
        x[i0:i1] += 0.3 * np.sin(2 * np.pi * freq * t[i0:i1])
    return x.astype(np.float32)


# --- framing -------------------------------------------------------------------------


def test_frame_signal_counts_and_pads():
    x = np.arange(1000, dtype=np.float32)
    f = frame_signal(x, 400, 160)
    assert f.shape == (5, 400)  # 1 + ceil((1000-400)/160) = 5
    assert np.array_equal(f[0], x[:400])
    assert np.array_equal(f[4, :360], x[640:1000]) and np.all(f[4, 360:] == 0)
    assert frame_signal(np.zeros(0), 400, 160).shape == (0, 400)
    assert frame_signal(np.ones(10), 400, 160).shape == (1, 400)


# --- VAD edges -----------------------------------------------------------------------------


def test_vad_edges_without_hangover_are_within_one_frame():
    x = tone_bursts([(0.5, 1.5), (2.0, 3.0)])
    p = VadParams(hangover_ms=0, min_silence_ms=0, min_speech_ms=0)
    mask, hop = energy_vad(x, SR, p)
    regions = mask_to_regions(mask, hop)
    assert len(regions) == 2
    for (s, e), (ts, te) in zip(regions, [(0.5, 1.5), (2.0, 3.0)]):
        assert abs(s - ts) <= 0.03  # a 25 ms frame straddling the onset
        assert abs(e - te) <= 0.03


def test_hangover_extends_only_the_end():
    x = tone_bursts([(0.5, 1.5)])
    base = VadParams(hangover_ms=0, min_silence_ms=0, min_speech_ms=0)
    hang = VadParams(hangover_ms=200, min_silence_ms=0, min_speech_ms=0)
    (s0, e0), = mask_to_regions(*energy_vad(x, SR, base))
    (s1, e1), = mask_to_regions(*energy_vad(x, SR, hang))
    assert s1 == pytest.approx(s0, abs=1e-9)
    assert e1 - e0 == pytest.approx(0.2, abs=0.011)


def test_min_speech_drops_a_click_but_keeps_it_when_disabled():
    x = tone_bursts([(1.0, 1.03)], total=2.0)
    assert mask_to_regions(*energy_vad(x, SR, VadParams(hangover_ms=0, min_speech_ms=200))) == []
    assert len(mask_to_regions(*energy_vad(x, SR, VadParams(hangover_ms=0, min_speech_ms=0)))) == 1


def test_min_silence_bridges_a_short_gap():
    x = tone_bursts([(0.5, 1.5), (1.6, 2.6)])  # 100 ms gap
    assert len(mask_to_regions(*energy_vad(x, SR, VadParams(hangover_ms=0, min_silence_ms=150)))) == 1
    assert len(mask_to_regions(*energy_vad(x, SR, VadParams(hangover_ms=0, min_silence_ms=50)))) == 2


def test_silence_and_faint_noise_yield_no_speech():
    quiet = (1e-4 * np.random.default_rng(0).standard_normal(3 * SR)).astype(np.float32)
    assert mask_to_regions(*energy_vad(quiet, SR)) == []
    assert mask_to_regions(*energy_vad(np.zeros(3 * SR, dtype=np.float32), SR)) == []
    assert energy_vad(np.zeros(0, dtype=np.float32), SR)[0].size == 0


def test_threshold_is_relative_to_floor():
    """The same bursts over a -35 dB floor are still found (floor-relative), while
    a burst only 6 dB above the floor is not with the 12 dB default."""
    loud_floor = tone_bursts([(0.5, 1.5)], floor=10 ** (-35 / 20))
    assert len(mask_to_regions(*energy_vad(loud_floor, SR))) == 1
    weak = tone_bursts([(0.5, 1.5)], floor=0.3 / 10 ** (6 / 20) / np.sqrt(2))  # burst RMS 6 dB over floor
    assert mask_to_regions(*energy_vad(weak, SR)) == []


# --- MFCC front end ------------------------------------------------------------------------


def test_mel_filterbank_shape_and_unit_peaks():
    fb = mel_filterbank(SR, 512, 26, 50.0, None)
    assert fb.shape == (26, 257)
    assert np.all(fb >= 0)
    assert np.allclose(fb.max(axis=1), 1.0)
    peaks = fb.argmax(axis=1)
    assert np.all(np.diff(peaks) > 0)  # centres strictly ascending


def test_mfcc_shape_matches_vad_frames_and_is_stable_on_a_stationary_source():
    x = render_layout([(0.0, 3.0, "A")], tail_s=0.0)
    feats, hop = mfcc(x, SR, MfccParams())
    mask, vhop = energy_vad(x, SR, VadParams(frame_ms=25, hop_ms=10))
    assert hop == vhop == pytest.approx(0.01)
    assert feats.shape == (len(mask), 12)  # 13 coefficients minus c0
    mid = feats[50:250]
    assert np.all(mid.std(axis=0) < 0.5 * np.abs(mid).mean(axis=0).max() + 1e-6)


def test_mfcc_separates_the_two_synthetic_voices():
    a, _ = mfcc(render_layout([(0.0, 2.0, "A")], tail_s=0.0), SR)
    b, _ = mfcc(render_layout([(0.0, 2.0, "B")], tail_s=0.0), SR)
    within = np.linalg.norm(a[20:100].mean(0) - a[100:180].mean(0))
    between = np.linalg.norm(a[20:180].mean(0) - b[20:180].mean(0))
    assert between > 5 * within


# --- chunking & smoothing ----------------------------------------------------------------------


def test_chunk_regions_absorbs_short_tail():
    assert chunk_regions([(0.0, 2.3, )], 1.0, 0.4) == [(0.0, 1.0), (1.0, 2.3)]
    assert chunk_regions([(0.0, 2.6)], 1.0, 0.4) == [(0.0, 1.0), (1.0, 2.0), (2.0, 2.6)]
    assert chunk_regions([(0.0, 0.3)], 1.0, 0.4) == [(0.0, 0.3)]
    assert chunk_regions([], 1.0, 0.4) == []


def test_smooth_turns_absorbs_islands_and_merges_neighbours():
    turns = [(0.0, 1.0, "a"), (1.0, 1.2, "b"), (1.2, 2.0, "a"), (2.5, 3.0, "b"), (3.0, 3.1, "a")]
    out = smooth_turns(turns, min_segment_s=0.3, merge_gap_s=0.3)
    assert out == [(0.0, 2.0, "a"), (2.5, 3.1, "b")]
    assert smooth_turns([], min_segment_s=0.3, merge_gap_s=0.3) == []
    # a lone short turn with no neighbour is dropped
    assert smooth_turns([(0.0, 0.1, "a")], min_segment_s=0.3, merge_gap_s=0.3) == []


# --- the diarizer on known signals -----------------------------------------------------------------


def _check_two_speakers(turns):
    assert len(turns) == 4, turns
    labels = [t[2] for t in turns]
    assert labels[0] == labels[2] and labels[1] == labels[3] and labels[0] != labels[1], labels
    assert set(labels) == {"spk0", "spk1"}
    hang = VadParams().hangover_ms / 1000.0
    for (s, e, _), (ts, te, _) in zip(turns, LAYOUT):
        assert abs(s - ts) <= 0.05, (s, ts)
        assert -0.05 <= e - te <= hang + 0.05, (e, te)


def test_two_voices_with_known_count():
    x = render_layout(LAYOUT)
    d = EnergyVadClusterDiarizer()
    _check_two_speakers(d.diarize(x, SR, num_speakers=2))
    assert d.last_trace.n_clusters == 2 and len(d.last_trace.regions) == 4


def test_two_voices_with_distance_threshold():
    x = render_layout(LAYOUT)
    d = EnergyVadClusterDiarizer()
    _check_two_speakers(d.diarize(x, SR))


def test_two_voices_survive_a_noisy_floor():
    x = render_layout(LAYOUT, noise_db=-35.0, seed=3)
    _check_two_speakers(EnergyVadClusterDiarizer().diarize(x, SR))


def test_one_voice_is_not_split_by_the_threshold():
    x = render_layout([(0.5, 2.5, "A"), (3.0, 5.0, "A"), (5.5, 7.0, "A")])
    turns = EnergyVadClusterDiarizer().diarize(x, SR)
    assert len(turns) == 3 and {t[2] for t in turns} == {"spk0"}


def test_known_count_overrides_the_threshold():
    x = render_layout(LAYOUT)
    turns = EnergyVadClusterDiarizer().diarize(x, SR, num_speakers=1)
    assert len(turns) == 4 and {t[2] for t in turns} == {"spk0"}


def test_three_voices_with_known_count():
    layout = [(0.5, 2.0, "A"), (2.5, 4.0, "B"), (4.5, 6.0, "C"), (6.5, 8.0, "A"), (8.5, 10.0, "C")]
    turns = EnergyVadClusterDiarizer().diarize(render_layout(layout), SR, num_speakers=3)
    labels = [t[2] for t in turns]
    assert len(labels) == 5
    assert labels[0] == labels[3] and labels[2] == labels[4] and len(set(labels)) == 3


def test_silence_yields_no_turns():
    quiet = (1e-4 * np.random.default_rng(0).standard_normal(3 * SR)).astype(np.float32)
    d = EnergyVadClusterDiarizer()
    assert d.diarize(quiet, SR) == []
    assert d.last_trace.n_clusters == 0


def test_a_too_loose_threshold_collapses_to_one_cluster():
    """Failure mode made explicit: the threshold is a HARNESS_POLICY knob and a
    bad value silently merges speakers.  This is why docs/pipeline.md says it
    must be re-tuned on dev data before any real-speech claim."""
    x = render_layout(LAYOUT)
    turns = EnergyVadClusterDiarizer(cluster=ClusterParams(distance_threshold=50.0)).diarize(x, SR)
    assert {t[2] for t in turns} == {"spk0"}


def test_from_params_applies_flat_overrides_and_ignores_unknown_keys():
    d = EnergyVadClusterDiarizer.from_params({"hangover_ms": 0, "distance_threshold": 9.5, "n_mfcc": 20, "bogus": 1})
    assert d.vad.hangover_ms == 0.0 and d.cluster.distance_threshold == 9.5 and d.mfcc.n_mfcc == 20


# --- end to end against a meeting dir ----------------------------------------------------------------


def _der(ref_segments, hyp_segments):
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    def ann(segs):
        a = Annotation(uri="m")
        for s in segs:
            a[Segment(s["start"], s["end"])] = s["speaker"]
        return a

    return float(DiarizationErrorRate(collar=0.0)(ann(ref_segments), ann(hyp_segments)))


def test_pipeline_on_synthetic_meeting_scores_low_der(tmp_path):
    m, d = synthetic_meeting(tmp_path, meeting_id="evc", layout=[(s, e, {"A": "spk0", "B": "spk1"}[v]) for s, e, v in LAYOUT])
    pipe = get_pipeline("energy-vad-cluster", PipelineConfig(params={"num_speakers": 2}))
    assert isinstance(pipe, EnergyVadClusterPipeline) and pipe.is_system_under_test
    hyp = pipe.run(d / "mix.wav", d)
    assert hyp.meeting_id == "evc" and hyp.system == "energy-vad-cluster"
    assert all(s["text"] == "" for s in hyp.segments)
    assert len(hyp.speakers) == 2
    assert hyp.extra["diarization"]["n_clusters"] == 2
    der = _der(m["segments"], hyp.segments)
    # 6.5 s of speech; hangover adds <=0.15 s per segment end (4 x 0.15 = 0.6 s
    # false alarm at most), onsets are within a frame -> DER well under 0.15
    assert der < 0.15, der


def test_pipeline_result_is_deterministic(tmp_path):
    m, d = synthetic_meeting(tmp_path, meeting_id="det")
    a = get_pipeline("energy-vad-cluster").run(d / "mix.wav", d).to_dict()
    b = get_pipeline("energy-vad-cluster").run(d / "mix.wav", d).to_dict()
    a["extra"].pop("audio"); b["extra"].pop("audio")
    assert a == b
