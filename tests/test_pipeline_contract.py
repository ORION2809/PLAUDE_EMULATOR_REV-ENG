"""The shared data contract as this package emits and consumes it.

Hypothesis validation failure modes, RTTM/STM writers against both this
package's parsers and the public readers (meeteval, pyannote.database),
the registry, and ComposedPipeline's word-to-speaker rule with stub
components (no models).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipeline import (
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    PipelineConfig,
    PipelineError,
    PipelineUnavailable,
    Transcriber,
    UnknownPipeline,
    describe_registry,
    get_pipeline,
    register,
    registered,
)
from pipeline.base import FALLBACK_SPEAKER, assign_speakers, make_segment, normalize_text
from pipeline.formats import (
    hypothesis_paths,
    parse_rttm,
    parse_stm,
    read_hypothesis,
    segments_to_rttm_lines,
    segments_to_stm_lines,
    write_hypothesis_files,
)
from pipeline.meeting import MeetingFormatError, meeting_from_stm, validate_meeting

SEGS = [
    make_segment("spk0", 0.0, 1.5, "hello world", [{"w": "hello", "start": 0.0, "end": 0.7}, {"w": "world", "start": 0.8, "end": 1.5}]),
    make_segment("spk1", 1.2, 2.0, "yes"),
    make_segment("spk0", 2.5, 3.0, ""),  # diarization-only segment
]


# --- Hypothesis.validate ---------------------------------------------------------------


def test_valid_hypothesis_round_trips_through_dict():
    h = Hypothesis("m1", "sys", SEGS).validate()
    d = h.to_dict()
    assert d["schema"] == "plaud-harness/hypothesis/1"
    assert Hypothesis.from_dict(d).to_dict() == d
    assert h.speakers == ["spk0", "spk1"]


@pytest.mark.parametrize(
    "segments, msg",
    [
        ([make_segment("spk0", -0.1, 1.0)], "bad times"),
        ([make_segment("spk0", 1.0, 0.5)], "bad times"),
        ([make_segment("spk0", 2.0, 3.0), make_segment("spk0", 1.0, 2.0)], "out of start order"),
        ([{"speaker": "spk0", "start": 0.0, "end": 1.0}], "lacks 'text'"),
        ([make_segment("", 0.0, 1.0)], "speaker"),
        ([{"speaker": "spk0", "start": 0.0, "end": 1.0, "text": 5}], "text must be a string"),
        ([make_segment("spk0", 0.0, 1.0, "a", [{"w": "a", "start": 0.9, "end": 0.1}])], "ends before it starts"),
        ([make_segment("spk0", float("nan"), 1.0)], "non-finite"),
    ],
)
def test_validate_rejects(segments, msg):
    with pytest.raises(PipelineError, match=msg):
        Hypothesis("m1", "sys", segments).validate()


def test_validate_rejects_empty_ids_and_wrong_schema():
    with pytest.raises(PipelineError):
        Hypothesis("", "sys", []).validate()
    with pytest.raises(PipelineError):
        Hypothesis("m", "", []).validate()
    with pytest.raises(PipelineError, match="not a plaud-harness/hypothesis/1"):
        Hypothesis.from_dict({"schema": "other", "meeting_id": "m", "system": "s"})


# --- RTTM / STM ------------------------------------------------------------------------------


def test_rttm_lines_format_and_parse_back():
    lines = segments_to_rttm_lines("m1", SEGS)
    assert lines[0] == "SPEAKER m1 1 0.000 1.500 <NA> <NA> spk0 <NA> <NA>"
    assert lines[1] == "SPEAKER m1 1 1.200 0.800 <NA> <NA> spk1 <NA> <NA>"
    back = parse_rttm("\n".join(lines) + "\n# comment\n")
    assert [(b["speaker"], b["start"], round(b["end"], 3)) for b in back] == [("spk0", 0.0, 1.5), ("spk1", 1.2, 2.0), ("spk0", 2.5, 3.0)]


def test_stm_lines_format_including_empty_transcript():
    lines = segments_to_stm_lines("m1", SEGS)
    assert lines[0] == "m1 1 spk0 0.000 1.500 hello world"
    assert lines[2] == "m1 1 spk0 2.500 3.000"  # no trailing space
    back = parse_stm("\n".join(lines))
    assert back[0]["text"] == "hello world" and back[2]["text"] == ""


def test_public_readers_accept_our_formats():
    from meeteval.io.rttm import RTTM
    from meeteval.io.stm import STM

    stm = STM.parse("\n".join(segments_to_stm_lines("m1", SEGS)) + "\n")
    assert [l.transcript for l in stm.lines] == ["hello world", "yes", ""]
    rttm = RTTM.parse("\n".join(segments_to_rttm_lines("m1", SEGS)) + "\n")
    assert [l.speaker_id for l in rttm.lines] == ["spk0", "spk1", "spk0"]


def test_writers_reject_whitespace_in_tokens():
    with pytest.raises(PipelineError):
        segments_to_rttm_lines("m 1", SEGS)
    with pytest.raises(PipelineError):
        segments_to_stm_lines("m1", [make_segment("spk 0", 0.0, 1.0)])


def test_parsers_reject_malformed_lines():
    with pytest.raises(PipelineError):
        parse_rttm("SPEAKER m1 1 0.0\n")
    with pytest.raises(PipelineError):
        parse_stm("m1 1 spk0 0.0\n")


def test_write_hypothesis_files_writes_three_siblings(tmp_path):
    h = Hypothesis("m1", "sys", SEGS)
    paths = write_hypothesis_files(h, tmp_path / "out" / "hyp.json")
    assert set(paths) == {"json", "rttm", "stm"}
    assert all(p.is_file() for p in paths.values())
    assert read_hypothesis(paths["json"]).to_dict() == h.to_dict()
    assert hypothesis_paths(tmp_path / "x")["rttm"].name == "x.rttm"


# --- meeting.json validation ---------------------------------------------------------------


def _meeting(**over):
    m = {
        "schema": "plaud-harness/meeting/1",
        "meeting_id": "m1",
        "sample_rate": 16000,
        "duration_s": 3.0,
        "channels": 1,
        "speakers": [{"id": "spk0", "voice": "v", "position_m": [0, 0, 0]}],
        "segments": [make_segment("spk0", 0.0, 1.0, "a", [{"w": "a", "start": 0.0, "end": 1.0}])],
        "audio": {"mix_wav": "mix.wav", "stems": {}, "device": {}},
        "generator": {"name": "t", "version": "1", "seed": 0, "scenario": {}},
    }
    m.update(over)
    return m


def test_validate_meeting_failure_modes():
    validate_meeting(_meeting())
    with pytest.raises(MeetingFormatError, match="schema"):
        validate_meeting(_meeting(schema="x"))
    with pytest.raises(MeetingFormatError, match="not in speakers"):
        validate_meeting(_meeting(segments=[make_segment("ghost", 0.0, 1.0)]))
    with pytest.raises(MeetingFormatError, match="sorted"):
        validate_meeting(_meeting(segments=[make_segment("spk0", 1.0, 2.0), make_segment("spk0", 0.0, 1.0)]))
    with pytest.raises(MeetingFormatError, match="whitespace"):
        validate_meeting(_meeting(meeting_id="m 1"))
    with pytest.raises(MeetingFormatError, match="unique"):
        validate_meeting(_meeting(speakers=[{"id": "a"}, {"id": "a"}]))


def test_meeting_from_stm_interpolates_words():
    m = meeting_from_stm("m1 1 spkA 0.0 2.0 one two three four\nm1 1 spkB 2.0 3.0 five\n")
    validate_meeting(m)
    assert [s["id"] for s in m["speakers"]] == ["spkA", "spkB"]
    w = m["segments"][0]["words"]
    assert [x["w"] for x in w] == ["one", "two", "three", "four"]
    assert [x["start"] for x in w] == [0.0, 0.5, 1.0, 1.5] and w[-1]["end"] == 2.0
    assert m["duration_s"] == 3.0 and "interpolated" in m["generator"]["scenario"]["word_times"]
    with pytest.raises(MeetingFormatError, match="several meetings"):
        meeting_from_stm("a 1 s 0 1 x\nb 1 s 0 1 y\n")


# --- registry ----------------------------------------------------------------------------------------


def test_registry_lists_expected_names_with_availability():
    rows = {r["name"]: r for r in describe_registry()}
    assert {"oracle", "perturbed-oracle", "energy-vad-cluster", "faster-whisper", "pyannote-audio", "whisperx"} <= set(rows)
    assert rows["oracle"]["available"] and not rows["oracle"]["system_under_test"]
    assert rows["energy-vad-cluster"]["available"] and rows["energy-vad-cluster"]["system_under_test"]
    for name in ("faster-whisper", "pyannote-audio", "whisperx", "faster-whisper+pyannote"):
        assert rows[name]["system_under_test"]
        if not rows[name]["available"]:
            assert "not importable" in rows[name]["reason"]


def test_unknown_and_unavailable_pipelines_raise():
    with pytest.raises(UnknownPipeline):
        get_pipeline("no-such-pipeline")
    assert issubclass(UnknownPipeline, KeyError)
    for name in ("faster-whisper", "pyannote-audio", "whisperx"):
        if not registered()[name].available():
            with pytest.raises(PipelineUnavailable, match="unavailable"):
                get_pipeline(name)
        else:  # pragma: no cover - only when the optional package is installed
            pytest.skip(f"{name} is importable here; adapter construction needs model files")


def test_duplicate_registration_is_an_error():
    with pytest.raises(PipelineError, match="already registered"):
        register("oracle")(lambda cfg=None: None)


# --- ComposedPipeline with stubs ----------------------------------------------------------------------


class StubTranscriber(Transcriber):
    name = "stub-asr"

    def transcribe(self, pcm, sample_rate):
        return [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "Hello, World! yes",
                "words": [
                    {"w": "Hello,", "start": 0.0, "end": 0.5},
                    {"w": "World!", "start": 0.6, "end": 1.0},
                    {"w": "yes", "start": 1.4, "end": 1.9},
                ],
            },
            {"start": 2.5, "end": 3.0, "text": "tail", "words": []},
        ]


class StubDiarizer(Diarizer):
    name = "stub-diar"

    def diarize(self, pcm, sample_rate, num_speakers=None):
        return [(0.0, 1.2, "A"), (1.3, 2.0, "B")]


def test_assign_speakers_rule():
    asr = StubTranscriber().transcribe(None, 16000)
    turns = StubDiarizer().diarize(None, 16000)
    segs = assign_speakers(asr, turns)
    assert [(s["speaker"], s["text"]) for s in segs] == [("A", "hello world"), ("B", "yes"), ("B", "tail")]
    assert segs[0]["words"][0] == {"w": "hello", "start": 0.0, "end": 0.5}
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 1.0  # spans its words, not the ASR segment
    # nothing overlaps -> nearest; no turns -> fallback
    assert assign_speakers(asr, [(10.0, 11.0, "Z")])[0]["speaker"] == "Z"
    assert {s["speaker"] for s in assign_speakers(asr, [])} == {FALLBACK_SPEAKER}


def test_normalize_text():
    assert normalize_text("  Hello, World!  it's (ok) ") == "hello world it's ok"
    assert normalize_text("...") == ""


def _wav(tmp_path):
    import soundfile as sf

    p = tmp_path / "x.wav"
    sf.write(str(p), np.zeros(16000 * 3, dtype=np.float32), 16000, subtype="PCM_16")
    return p


def test_composed_pipeline_variants(tmp_path):
    p = _wav(tmp_path)
    both = ComposedPipeline(StubTranscriber(), StubDiarizer(), PipelineConfig(name="stub"), name="stub").run(p)
    assert both.meeting_id == "x" and both.system == "stub"
    assert [s["speaker"] for s in both.segments] == ["A", "B", "B"]
    assert both.extra["components"] == {"transcriber": "stub-asr", "diarizer": "stub-diar"}
    assert both.extra["audio"]["container"] == "wav" and both.extra["audio"]["duration_s"] == 3.0
    asr_only = ComposedPipeline(StubTranscriber(), None, name="asr").run(p)
    assert {s["speaker"] for s in asr_only.segments} == {FALLBACK_SPEAKER}
    diar_only = ComposedPipeline(None, StubDiarizer(), name="diar").run(p)
    assert [(s["speaker"], s["text"]) for s in diar_only.segments] == [("A", ""), ("B", "")]
    with pytest.raises(PipelineError):
        ComposedPipeline(None, None)


def test_composed_pipeline_passes_num_speakers(tmp_path):
    seen = {}

    class Counting(Diarizer):
        def diarize(self, pcm, sample_rate, num_speakers=None):
            seen["k"] = num_speakers
            return []

    ComposedPipeline(None, Counting(), PipelineConfig(name="c", params={"num_speakers": 3})).run(_wav(tmp_path))
    assert seen["k"] == 3
