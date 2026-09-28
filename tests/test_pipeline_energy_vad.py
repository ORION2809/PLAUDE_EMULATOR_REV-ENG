"""energy-vad-cluster: the model-free diarizer against analytically known signals.

Every signal here is built in the test (pipeline.synthetic) so the true
speech regions and speaker identities are known by construction.  The
tests pin the VAD edge behaviour (hangover, min-speech, min-silence, the
no-contrast rule), the MFCC and f0 front end (shape, stability, block
independence, memory), the speaker-count estimate on two and three
synthetic voices without a hint (and NOT splitting one), the clustering
guards, the smoothing rule, and end-to-end DER against a synthetic meeting
dir and against generator meetings (formant voices) on three seeds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipeline import ParamError, PipelineConfig, get_pipeline
from pipeline.energy_vad import (
    ClusterParams,
    EnergyVadClusterDiarizer,
    EnergyVadClusterPipeline,
    MfccParams,
    PitchParams,
    VadParams,
    chunk_regions,
    cluster_embeddings,
    energy_vad,
    frame_energies_db,
    frame_signal,
    mask_to_regions,
    mel_filterbank,
    mfcc,
    pitch_track,
    smooth_turns,
    window_features,
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
    a burst only 6 dB above the floor is not with the 12 dB default -- in a file
    that has a real floor (silence) and real speech to contrast it with."""
    loud_floor = tone_bursts([(0.5, 1.5)], floor=10 ** (-35 / 20))
    assert len(mask_to_regions(*energy_vad(loud_floor, SR))) == 1
    floor = 10 ** (-40 / 20)
    x = tone_bursts([(0.5, 1.5)], floor=floor, total=4.0)  # strong burst, 0.3 peak
    t = np.arange(len(x)) / SR
    weak = (floor * 10 ** (6 / 20) * np.sqrt(2)) * np.sin(2 * np.pi * 1000.0 * t)  # RMS 6 dB over the floor
    x[int(2.5 * SR) : int(3.5 * SR)] += weak[int(2.5 * SR) : int(3.5 * SR)].astype(np.float32)
    info = {}
    regions = mask_to_regions(*energy_vad(x, SR, info=info))
    assert info["mode"] == "contrast"
    assert len(regions) == 1 and regions[0][0] < 0.6 and regions[0][1] < 2.0, regions


# --- PIPE-02: no silence at all --------------------------------------------------------------------


def test_back_to_back_voices_with_no_silence_are_speech_not_silence():
    """Regression for review finding PIPE-02: with ~0 % non-speech the 10th-
    percentile "floor" was itself a speech frame, the VAD found nothing and the
    diarizer returned [] even with num_speakers=2."""
    x = render_layout([(0.0, 5.0, "A"), (5.0, 10.0, "B")], tail_s=0.0)
    info = {}
    regions = mask_to_regions(*energy_vad(x, SR, info=info))
    assert info["mode"] == "dense" and info["voiced_fraction"] > 0.9
    assert sum(e - s for s, e in regions) > 9.5
    cell = ClusterParams().chunk_s  # a change inside one speech region resolves to a cell
    for hint in (2, None):
        turns = EnergyVadClusterDiarizer().diarize(x, SR, num_speakers=hint)
        assert [t[2] for t in turns] == ["spk0", "spk1"], (hint, turns)
        assert abs(turns[0][1] - 5.0) <= cell + 0.01 and abs(turns[1][0] - 5.0) <= cell + 0.01


def test_five_percent_silence_is_also_found():
    x = render_layout([(0.0, 4.75, "A"), (5.0, 9.75, "B")], tail_s=0.0)
    regions = mask_to_regions(*energy_vad(x, SR))
    assert sum(e - s for s, e in regions) > 9.0


@pytest.mark.parametrize("noise_db", [-30.0, -25.0])
def test_low_snr_file_splits_noise_from_speech(noise_db):
    """Spread under 12 dB but with real pauses (voices ~10 dB over white noise):
    the quieter energy class is aperiodic, so it is noise -- the pauses must not
    become speech (and a third 'speaker'), and the speech must not be missed."""
    x = render_layout(LAYOUT, noise_db=noise_db, seed=4)
    info = {}
    regions = mask_to_regions(*energy_vad(x, SR, info=info))
    assert info["mode"] == "split"
    assert len(regions) == 4
    hang = VadParams().hangover_ms / 1000.0
    for (s, e), (ts, te, _) in zip(regions, LAYOUT):
        assert abs(s - ts) <= 0.05 and -0.05 <= e - te <= hang + 0.05
    _check_two_speakers(EnergyVadClusterDiarizer().diarize(x, SR))


@pytest.mark.parametrize(
    "signal, why",
    [
        (lambda: (10 ** (-30 / 20) * np.random.default_rng(1).standard_normal(4 * SR)).astype(np.float32), "aperiodic"),
        (lambda: (10 ** (-50 / 20) * np.sqrt(2) * np.sin(2 * np.pi * 150 * np.arange(4 * SR) / SR)).astype(np.float32), "quiet"),
    ],
)
def test_no_contrast_rule_needs_a_loud_periodic_file(signal, why):
    """The no-contrast rule is narrow: stationary loud NOISE and a quiet tone stay silence."""
    info = {}
    assert mask_to_regions(*energy_vad(signal(), SR, info=info)) == [], why
    assert info["mode"] == "flat"


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


def test_from_params_applies_flat_overrides():
    d = EnergyVadClusterDiarizer.from_params(
        {"hangover_ms": 0, "distance_threshold": 9.5, "n_mfcc": 20, "f0_weight": 0, "num_speakers": 2}
    )
    assert d.vad.hangover_ms == 0.0 and d.cluster.distance_threshold == 9.5 and d.mfcc.n_mfcc == 20
    assert d.cluster.f0_weight == 0.0 and isinstance(d.cluster.f0_weight, float)
    both = EnergyVadClusterDiarizer.from_params({"hop_ms": 20})
    assert both.vad.hop_ms == both.mfcc.hop_ms == 20.0


@pytest.mark.parametrize(
    "params, match",
    [
        ({"distance_treshold": 0.1}, "distance_treshold"),  # typo (review finding PIPE-11)
        ({"num_speaker": 5}, "num_speaker"),
        ({"bogus": 1}, "bogus"),
        ({"hangover_ms": "long"}, "hangover_ms"),
        ({"n_mfcc": 12.5}, "n_mfcc"),
        ({"drop_c0": "false"}, "drop_c0"),
        ({"chunk_s": 0}, "chunk_s"),
    ],
)
def test_from_params_rejects_unknown_keys_and_bad_values(params, match):
    with pytest.raises(ParamError, match=match):
        EnergyVadClusterDiarizer.from_params(params)


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


# --- PIPE-13: blockwise front end ---------------------------------------------------------------


def _reference_mfcc(x, sr=SR, p=MfccParams()):
    """The textbook whole-signal computation (float64 throughout), for comparison."""
    x = np.asarray(x, dtype=np.float64)
    x = np.concatenate([[x[0]], x[1:] - p.preemphasis * x[:-1]])
    fl, hop = int(sr * p.frame_ms / 1000), int(sr * p.hop_ms / 1000)
    n = 1 + int(np.ceil((len(x) - fl) / hop))
    x = np.concatenate([x, np.zeros((n - 1) * hop + fl - len(x))])
    frames = x[np.arange(fl)[None, :] + hop * np.arange(n)[:, None]]
    n_fft = 1 << int(np.ceil(np.log2(fl)))
    spec = np.abs(np.fft.rfft(frames * np.hamming(fl), n=n_fft, axis=1)) ** 2
    mel = np.log(spec @ mel_filterbank(sr, n_fft, p.n_mels, p.fmin_hz, p.fmax_hz).T + 1e-10)
    from scipy.fft import dct

    return dct(mel, type=2, norm="ortho", axis=1)[:, 1 : p.n_mfcc]


def test_front_end_results_do_not_depend_on_the_block_size():
    """Block boundaries are handled exactly; the arithmetic is equal to
    floating-point rounding.  Bit-identical on x86_64 Linux and macOS arm64;
    on arm64 Linux (GitHub's ubuntu-24.04-arm, 28 Sep 2026) the MFCCs of
    different block sizes differed in the last bits -- the FFT library's
    multi-row vector path and its single-row path round differently -- so
    the comparison allows 1e-9 absolute."""
    x = render_layout(LAYOUT, noise_db=-35.0, seed=2)
    close = dict(rtol=0.0, atol=1e-9)
    for block in (1, 7, 333):
        np.testing.assert_allclose(mfcc(x, SR, block=block)[0], mfcc(x, SR)[0], **close)
        np.testing.assert_allclose(frame_energies_db(x, 400, 160, block=block), frame_energies_db(x, 400, 160), **close)
        f_a, s_a = pitch_track(x, SR, 160, block=block)
        f_b, s_b = pitch_track(x, SR, 160)
        np.testing.assert_allclose(f_a, f_b, **close)
        np.testing.assert_allclose(s_a, s_b, **close)
    # and the blockwise MFCC is the whole-signal formula (to float rounding)
    x32 = x.astype(np.float32)
    assert np.allclose(mfcc(x32, SR)[0], _reference_mfcc(x32), rtol=1e-9, atol=1e-9)


def _alternating(minutes):
    layout, t, k = [], 0.5, 0
    while t < minutes * 60 - 3:
        layout.append((t, t + 2.0, "AB"[k % 2]))
        t, k = t + 2.5, k + 1
    return render_layout(layout, tail_s=0.5)


def _peak_mib(x):
    import tracemalloc

    tracemalloc.start()
    try:
        EnergyVadClusterDiarizer().diarize(x, SR, num_speakers=2)
        return tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()


def test_diarizer_peak_memory_is_bounded_and_grows_slowly_with_length():
    """Review finding PIPE-13: whole-file frame matrices cost ~91 MiB of peak
    allocation per audio minute (tracemalloc), ~5 GiB for a 60-minute meeting.
    Blockwise, the peak is a constant working set plus a few MiB per minute."""
    one, four = _peak_mib(_alternating(1.0)), _peak_mib(_alternating(4.0))
    assert four < 80.0, f"peak {four:.1f} MiB for 4 minutes"
    assert (four - one) / 3.0 < 6.0, f"{(four - one) / 3.0:.1f} MiB per extra audio minute"


# --- f0 tracker -------------------------------------------------------------------------------------


@pytest.mark.parametrize("key, f0", [("A", 100.0), ("B", 230.0), ("C", 160.0)])
def test_pitch_track_finds_the_pulse_train_f0(key, f0):
    x = render_layout([(0.0, 2.0, key)], tail_s=0.0)
    hz, strength = pitch_track(x, SR, 160)
    voiced = strength > PitchParams().voicing_threshold
    assert voiced.mean() > 0.9
    assert abs(np.median(hz[voiced]) / f0 - 1.0) < 0.02


def test_window_f0_prefers_the_lower_octave_when_a_quarter_of_frames_sit_there():
    """Octave-up picks (a formant near 2*f0) are the tracker's typical gross error."""
    n = 100
    feats = np.zeros((n, 2))
    strength = np.ones(n)
    f0 = np.full(n, 175.0)
    f0[:60] = 350.0  # 60 % octave-up errors: the plain median would say 350 Hz
    params = ClusterParams()
    _, semis = window_features(feats, f0, strength, 0.01, [(0.0, 1.0)], params, PitchParams())
    assert semis[0] == pytest.approx(12 * np.log2(175.0))
    params.octave_fix_frac = 0.0
    _, plain = window_features(feats, f0, strength, 0.01, [(0.0, 1.0)], params, PitchParams())
    assert plain[0] == pytest.approx(12 * np.log2(350.0))


# --- the speaker-count estimate ---------------------------------------------------------------------


def _blobs(centres, n=30, sd=0.3, seed=0):
    rng = np.random.default_rng(seed)
    return np.concatenate([np.asarray(c, float) + sd * rng.standard_normal((n, len(c))) for c in centres])


def test_cluster_embeddings_counts_separated_blobs_and_does_not_split_one():
    info = {}
    lab = cluster_embeddings(_blobs([[0, 0], [6, 0], [0, 6]]), num_speakers=None, info=info)
    assert len(set(lab.tolist())) == 3 and info["k_eigengap"] == 3
    assert all(len(set(lab[i * 30 : (i + 1) * 30].tolist())) == 1 for i in range(3))
    one = cluster_embeddings(_blobs([[0, 0]], n=60), num_speakers=None, info=(i1 := {}))
    assert set(one.tolist()) == {0} and i1["k"] == 1


def test_cluster_embeddings_absorbs_an_outlier_island():
    """A few far-away rows (e.g. octave-error windows) are not a speaker, with or
    without the hint: the hint must not spend one of its slots on them."""
    x = np.concatenate([_blobs([[0, 0], [6, 0]], n=40), np.array([[30.0, 30.0], [30.2, 30.1]])])
    for hint in (None, 2):
        lab = cluster_embeddings(x, num_speakers=hint)
        assert len(set(lab.tolist())) == 2, hint
        assert len(set(lab[:40].tolist())) == 1 and len(set(lab[40:80].tolist())) == 1 and lab[0] != lab[40]


def test_three_voices_counted_without_hint():
    layout = [(0.5, 2.0, "A"), (2.5, 4.0, "B"), (4.5, 6.0, "C"), (6.5, 8.0, "A"), (8.5, 10.0, "C")]
    d = EnergyVadClusterDiarizer()
    turns = d.diarize(render_layout(layout), SR)
    labels = [t[2] for t in turns]
    assert len(labels) == 5 and len(set(labels)) == 3
    assert labels[0] == labels[3] and labels[2] == labels[4]
    assert d.last_trace.count["k"] == 3 and d.last_trace.count["k_threshold"] >= 3


def test_one_long_voice_is_one_speaker():
    d = EnergyVadClusterDiarizer()
    turns = d.diarize(render_layout([(0.5, 9.5, "B")]), SR)
    assert {t[2] for t in turns} == {"spk0"} and turns[0][0] < 0.6 and turns[-1][1] > 9.4


# --- generator meetings (formant voices): the known gap --------------------------------------------


@pytest.fixture(scope="module")
def generator_meetings(tmp_path_factory):
    from generator.testing import fixture_meeting

    base = tmp_path_factory.mktemp("gen")
    out = {}
    for preset, seeds in (("smoke", (1, 2, 3)), ("default", (1,)), ("single_speaker", (2,))):
        for seed in seeds:
            out[(preset, seed)] = fixture_meeting(base, preset=preset, seed=seed)
    return out


def _score(d, hyp):
    from evals import load_meeting, score_meeting
    from evals.io import hypothesis_from_dict

    return score_meeting(load_meeting(d), hypothesis_from_dict(hyp.to_dict()))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_generator_smoke_meeting_two_voices_without_hint(generator_meetings, seed):
    """The model-free diarizer used to merge the generator's two formant voices
    unless told the count (review finding PIPE-01: unaided n=2/1/1, DER
    0.507/0.585/0.373 on seeds 1/2/3 of the device Ogg).  With the f0 feature
    and the eigengap count it separates them unaided on all three seeds."""
    d, m = generator_meetings[("smoke", seed)]
    for audio in (d / "device" / "recording.ogg", d / m["audio"]["mix_wav"]):
        hyp = get_pipeline("energy-vad-cluster").run(audio, d)
        report = _score(d, hyp)
        assert len(hyp.speakers) == 2, (seed, audio.name, len(hyp.speakers), hyp.extra["diarization"].get("count"))
        assert report.der.der < 0.25, (seed, audio.name, report.der.der)
        assert report.der.miss / report.der.total <= 0.02
        hinted = get_pipeline("energy-vad-cluster", PipelineConfig(params={"num_speakers": 2})).run(audio, d)
        assert _score(d, hinted).der.der < 0.25


def test_generator_default_meeting_three_voices_without_hint(generator_meetings):
    """Finding PIPE-01's failure scenario: default preset seed 1 gave 1 speaker (DER 0.633)."""
    d, m = generator_meetings[("default", 1)]
    hyp = get_pipeline("energy-vad-cluster").run(d / "device" / "recording.ogg", d)
    assert len(hyp.speakers) == 3, (len(hyp.speakers), hyp.extra["diarization"].get("count"))
    assert _score(d, hyp).der.der < 0.25


def test_generator_noisy_preset_speech_is_not_missed(tmp_path_factory):
    """At 10 dB SNR (notepin_s_noisy) speech sits ~10-14 dB over the noise, so a
    fixed 12 dB margin missed most of it (mix.wav: miss 0.70, DER 0.71 for seed
    1); the Otsu-lowered margin recovers it."""
    from generator.testing import fixture_meeting

    d, m = fixture_meeting(tmp_path_factory.mktemp("noisy"), preset="notepin_s_noisy", seed=1)
    hyp = get_pipeline("energy-vad-cluster").run(d / m["audio"]["mix_wav"], d)
    report = _score(d, hyp)
    assert report.der.miss / report.der.total < 0.1, report.der
    assert report.der.der < 0.15 and len(hyp.speakers) == 2


def test_generator_single_speaker_is_not_split(generator_meetings):
    """Finding PIPE-01's other failure scenario: single_speaker seed 2 on mix.wav gave 3 speakers."""
    d, m = generator_meetings[("single_speaker", 2)]
    for audio in (d / "device" / "recording.ogg", d / m["audio"]["mix_wav"]):
        hyp = get_pipeline("energy-vad-cluster").run(audio, d)
        assert len(hyp.speakers) == 1, (audio.name, len(hyp.speakers), hyp.extra["diarization"].get("count"))


# --- R-CI-1 (28 Sep 2026): tied eigenvalues must not make the cut platform-dependent ------------
# GitHub's x86_64 runner failed test_cluster_embeddings_absorbs_an_outlier_island while macOS
# passed. With k disconnected groups the top k eigenvalues are all exactly 1.0, so ANY rotation of
# their eigenvectors is an equally valid eigh() result; which one LAPACK returns differs by build.
# Taking "the first k0 eigenvectors" inside such a tie cut an arbitrary 2-D slice of a 3-D space.
# These tests force the rotations LAPACK is free to return, on every machine.


def _outlier_island():
    return np.concatenate([_blobs([[0, 0], [6, 0]], n=40), np.array([[30.0, 30.0], [30.2, 30.1]])])


@pytest.mark.parametrize("seed", range(12))
def test_cluster_embeddings_is_invariant_to_the_basis_of_tied_eigenvectors(monkeypatch, seed):
    import pipeline.energy_vad as ev

    real = ev._spectrum
    rng = np.random.default_rng(1000 + seed)

    def rotated(dist, knn, n_vec):
        w, v = real(dist, knn, n_vec)
        tied = int(np.sum(np.abs(w - w[0]) <= 1e-8))
        if tied > 1:  # replace the tied block by a random orthonormal basis of the same subspace
            q, _ = np.linalg.qr(rng.standard_normal((tied, tied)))
            v = v.copy()
            v[:, :tied] = v[:, :tied] @ q
        return w, v

    monkeypatch.setattr(ev, "_spectrum", rotated)
    x = _outlier_island()
    for hint in (None, 2):
        lab = ev.cluster_embeddings(x, num_speakers=hint)
        assert len(set(lab.tolist())) == 2, (seed, hint)
        assert len(set(lab[:40].tolist())) == 1 and len(set(lab[40:80].tolist())) == 1, (seed, hint)
        assert lab[0] != lab[40], (seed, hint)
