"""V4: the generator's ground truth round-trips through pyannote.metrics at DER 0.

The reference RTTM is loaded with pyannote's own loader and scored against
(i) itself and (ii) an annotation rebuilt from the sample-level activity mask.
Both must be EXACTLY 0.0 (DER and JER), which the 1/128 s boundary grid makes
a certainty rather than a floating-point accident. A shifted copy must score
> 0 so the metric is demonstrably alive.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from pyannote.core import Annotation, Segment, Timeline
from pyannote.database.util import load_rttm
from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

from generator.contract import SAMPLE_RATE
from generator.export import load_activity_mask
from generator.scenario import Scenario, TurnTaking
from generator.testing import fixture_meeting
from generator.text import TextSource
from generator.tts.formant import FormantBackend
from generator.turns import activity_mask, mask_to_segments, overlap_ratio, plan_turns

warnings.filterwarnings("ignore", message=".*'uem' was approximated.*")


@pytest.fixture(scope="module", params=["plain", "overlap"])
def meeting(request, tmp_path_factory) -> tuple[Path, dict]:
    base = tmp_path_factory.mktemp("v4")
    if request.param == "plain":
        return fixture_meeting(base, "smoke", 3)
    return fixture_meeting(base, "smoke", 8, {"duration_s": 20.0, "turn_taking.overlap_ratio": 0.2, "n_speakers": 3, "export.include_stereo_ogg": False, "export.include_g4": False})


def annotation_from_segments(uri: str, segs) -> Annotation:
    ann = Annotation(uri=uri)
    for spk, start, end in segs:
        ann[Segment(start, end), len(ann)] = spk
    return ann


def reference(meeting) -> tuple[Annotation, Timeline]:
    d, m = meeting
    ref = load_rttm(str(d / "ref.rttm"))[m["meeting_id"]]
    uem = Timeline([Segment(0.0, m["duration_s"])], uri=m["meeting_id"])
    return ref, uem


def test_loaded_rttm_equals_the_meeting_json_floats_exactly(meeting) -> None:
    ref, _ = reference(meeting)
    _, m = meeting
    loaded = sorted((s.start, s.end, lab) for s, _, lab in ref.itertracks(yield_label=True))
    expected = sorted((seg["start"], seg["end"], seg["speaker"]) for seg in m["segments"])
    assert loaded == expected, "pyannote must parse our RTTM back to the identical doubles"


def test_der_and_jer_of_reference_against_itself_are_exactly_zero(meeting) -> None:
    ref, uem = reference(meeting)
    assert DiarizationErrorRate()(ref, ref, uem=uem) == 0.0
    assert JaccardErrorRate()(ref, ref, uem=uem) == 0.0


def test_der_against_annotation_rebuilt_from_the_sample_mask_is_exactly_zero(meeting) -> None:
    d, m = meeting
    ref, uem = reference(meeting)
    mask = load_activity_mask(d)
    rebuilt = annotation_from_segments(m["meeting_id"], [(f"spk{s}", a / SAMPLE_RATE, b / SAMPLE_RATE) for s, a, b in mask_to_segments(mask)])
    der = DiarizationErrorRate()
    assert der(ref, rebuilt, uem=uem) == 0.0
    assert der(ref, rebuilt) == 0.0  # also without an explicit uem
    assert JaccardErrorRate()(ref, rebuilt, uem=uem) == 0.0
    from_json = annotation_from_segments(m["meeting_id"], [(s["speaker"], s["start"], s["end"]) for s in m["segments"]])
    assert der(ref, from_json, uem=uem) == 0.0
    if m["turn_taking"]["overlap_ratio_target"] > 0:
        assert overlap_ratio(mask) > 0.1, "the overlap fixture must actually overlap"


def test_shifted_and_damaged_copies_score_above_zero(meeting) -> None:
    _, m = meeting
    ref, uem = reference(meeting)
    der = DiarizationErrorRate()
    shifted = annotation_from_segments(m["meeting_id"], [(s["speaker"], s["start"] + 0.5, s["end"] + 0.5) for s in m["segments"]])
    assert der(ref, shifted, uem=uem) > 0.05
    dropped = annotation_from_segments(m["meeting_id"], [(s["speaker"], s["start"], s["end"]) for s in m["segments"][1:]])
    assert der(ref, dropped, uem=uem) > 0.0
    permutation = {"spk0": "spk1", "spk1": "spk0"}
    swapped = annotation_from_segments(m["meeting_id"], [(permutation.get(s["speaker"], s["speaker"]), s["start"], s["end"]) for s in m["segments"]])
    assert der(ref, swapped, uem=uem) == 0.0, "DER maps labels optimally; a relabelling is not an error"


def test_in_memory_overlapping_plan_is_consistent_between_table_and_mask() -> None:
    """No files: the planner's table and its own mask agree under heavy overlap."""
    sc = Scenario(name="v4", n_speakers=3, duration_s=40.0, seed=5, turn_taking=TurnTaking(overlap_ratio=0.3, words_min=5, words_max=10)).validate()
    turns = plan_turns(sc, TextSource(5), FormantBackend(), ["fv-alto", "fv-bass", "fv-tenor"])
    n = int(sc.duration_s * SAMPLE_RATE)
    mask = activity_mask(turns, 3, n)
    assert overlap_ratio(mask) > 0.2
    table = annotation_from_segments("v4", [(f"spk{t.speaker}", t.start_s, t.end_s) for t in turns])
    from_mask = annotation_from_segments("v4", [(f"spk{s}", a / SAMPLE_RATE, b / SAMPLE_RATE) for s, a, b in mask_to_segments(mask)])
    uem = Timeline([Segment(0.0, sc.duration_s)], uri="v4")
    assert DiarizationErrorRate()(table, from_mask, uem=uem) == 0.0
    assert JaccardErrorRate()(table, from_mask, uem=uem) == 0.0
