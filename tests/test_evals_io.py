"""Layer 3 contract I/O: loaders reject malformed input, normalisation, round-trips.

The meeting/hypothesis fixtures here are hand-built (no generator involved)
so every expectation is known by construction.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from evals.io import (  # noqa: E402
    ContractError,
    Hypothesis,
    Meeting,
    Normalizer,
    Segment,
    Word,
    format_time,
    hypothesis_from_dict,
    load_hypothesis,
    load_meeting,
    load_rttm,
    load_stm,
    meeting_from_dict,
    normalized_words,
    number_token_to_words,
    parse_rttm,
    parse_stm,
    rttm_lines,
    stm_lines,
    write_hypothesis,
    write_hypothesis_files,
    write_meeting,
    write_reference_files,
)


def meeting_dict() -> dict:
    return {
        "schema": "plaud-harness/meeting/1",
        "meeting_id": "m-0001",
        "sample_rate": 16000,
        "duration_s": 30.0,
        "channels": 1,
        "speakers": [
            {"id": "spk0", "voice": "v-a", "position_m": [0.0, 0.0, 1.2]},
            {"id": "spk1", "voice": "v-b", "position_m": [1.0, 0.5, 1.2]},
        ],
        "segments": [
            {
                "speaker": "spk0",
                "start": 0.5,
                "end": 2.0,
                "text": "hello there",
                "words": [{"w": "hello", "start": 0.5, "end": 1.1}, {"w": "there", "start": 1.2, "end": 2.0}],
            },
            {
                "speaker": "spk1",
                "start": 1.8,
                "end": 3.25,
                "text": "hi",
                "words": [{"w": "hi", "start": 1.9, "end": 2.4}],
            },
            {"speaker": "spk0", "start": 4.0, "end": 5.125, "text": "ok bye"},
        ],
        "audio": {"mix_wav": "mix.wav", "stems": {"spk0": "stems/spk0.wav"}, "device": {"ogg_opus": "device/recording.ogg"}},
        "generator": {"name": "hand", "version": "0", "seed": 7, "scenario": {"kind": "test"}},
    }


def hyp_dict() -> dict:
    return {
        "schema": "plaud-harness/hypothesis/1",
        "meeting_id": "m-0001",
        "system": "unit-test",
        "segments": [
            {"speaker": "S1", "start": 4.0, "end": 5.0, "text": "ok bye"},
            {"speaker": "S0", "start": 0.5, "end": 2.0, "text": "hello there"},
        ],
    }


# --------------------------------------------------------------------------- #
# meeting.json
# --------------------------------------------------------------------------- #


def test_meeting_loads_and_exposes_contract_fields(tmp_path: Path) -> None:
    p = tmp_path / "meeting.json"
    p.write_text(json.dumps(meeting_dict()))
    m = load_meeting(tmp_path)  # directory form
    assert m.meeting_id == "m-0001" and m.duration_s == 30.0 and m.sample_rate == 16000
    assert m.speaker_ids == ["spk0", "spk1"] and m.active_speakers == ["spk0", "spk1"]
    assert [s.speaker for s in m.segments] == ["spk0", "spk1", "spk0"]
    assert m.segments[0].words == (Word("hello", 0.5, 1.1), Word("there", 1.2, 2.0))
    assert m.segments[2].words == ()
    assert load_meeting(p).to_dict() == m.to_dict()  # file form


def _mutate(fn):
    d = meeting_dict()
    fn(d)
    return d


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda d: d.update(schema="plaud-harness/meeting/2"), "schema is 'plaud-harness/meeting/2'"),
        (lambda d: d.pop("schema"), "missing required key 'schema'"),
        (lambda d: d.pop("segments"), "missing required key 'segments'"),
        (lambda d: d.update(meeting_id="has space"), "must not contain whitespace"),
        (lambda d: d.update(sample_rate=0), "'sample_rate' must be a positive integer"),
        (lambda d: d.update(duration_s=-1), "'duration_s' must be a positive finite number"),
        (lambda d: d.update(speakers=[]), "'speakers' must be a non-empty list"),
        (lambda d: d["speakers"].append({"id": "spk0"}), "duplicate speaker id 'spk0'"),
        (lambda d: d["speakers"][0].update(position_m=[1, 2]), "'position_m' must be [x, y, z]"),
        (lambda d: d["segments"].reverse(), "segments must be sorted by start"),
        (lambda d: d["segments"][2].update(end=4.0), "segment end 4.0 must be strictly after start 4.0"),
        (lambda d: d["segments"][2].update(start=-0.5), "is negative"),
        (lambda d: d["segments"][2].update(speaker="spk9"), "speaker 'spk9' is not declared"),
        (lambda d: d["segments"][2].update(end=31.0), "exceeds the meeting duration 30.0"),
        (lambda d: d["segments"][0].update(text="hello here"), "'words' do not spell 'text'"),
        (lambda d: d["segments"][0]["words"][1].update(end=2.5), "lies outside its segment"),
        (lambda d: d["segments"][0]["words"].reverse(), "words are not sorted by start"),
        (lambda d: d["segments"][0]["words"][0].update(w="two tokens"), "must be a single token"),
        (lambda d: d["segments"][0].update(text=None), "'text' must be a string"),
        (lambda d: d.update(audio="mix.wav"), "'audio' must be an object"),
        (lambda d: d["generator"].update(seed="7"), "'generator.seed' must be an integer"),
    ],
)
def test_meeting_rejects_malformed_input_with_a_reason(mutation, needle) -> None:
    with pytest.raises(ContractError) as exc:
        meeting_from_dict(_mutate(mutation), where="meeting.json")
    assert needle in str(exc.value), str(exc.value)
    assert str(exc.value).startswith("meeting.json")


def test_meeting_error_names_the_offending_segment_index() -> None:
    with pytest.raises(ContractError, match=r"meeting\.json\.segments\[2\]: segment speaker 'zz'"):
        meeting_from_dict(_mutate(lambda d: d["segments"][2].update(speaker="zz")))


def test_meeting_loader_reports_bad_json_and_missing_file(tmp_path: Path) -> None:
    (tmp_path / "meeting.json").write_text("{not json")
    with pytest.raises(ContractError, match="not valid JSON"):
        load_meeting(tmp_path)
    with pytest.raises(ContractError, match="file not found"):
        load_meeting(tmp_path / "nope" / "meeting.json")


# --------------------------------------------------------------------------- #
# hyp.json
# --------------------------------------------------------------------------- #


def test_hypothesis_loads_unsorted_segments_and_any_labels() -> None:
    h = hypothesis_from_dict(hyp_dict())
    assert h.system == "unit-test" and h.speaker_ids == ["S1", "S0"]
    assert h.segments[0].start == 4.0  # order preserved; scoring sorts


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda d: d.update(schema="plaud-harness/meeting/1"), "expected 'plaud-harness/hypothesis/1'"),
        (lambda d: d.pop("system"), "missing required key 'system'"),
        (lambda d: d.update(system=""), "'system' must be a non-empty string"),
        (lambda d: d["segments"][0].update(end=3.9), "must be strictly after start"),
        (lambda d: d["segments"][0].update(speaker=""), "'speaker' must be a non-empty string"),
        (lambda d: d.update(segments={}), "'segments' must be a list"),
    ],
)
def test_hypothesis_rejects_malformed_input(mutation, needle) -> None:
    d = hyp_dict()
    mutation(d)
    with pytest.raises(ContractError) as exc:
        hypothesis_from_dict(d)
    assert needle in str(exc.value), str(exc.value)


# --------------------------------------------------------------------------- #
# RTTM / STM
# --------------------------------------------------------------------------- #


def test_format_time_is_six_decimals_trimmed() -> None:
    assert format_time(20) == "20.0"
    assert format_time(0.175) == "0.175"
    assert format_time(5.125) == "5.125"
    assert format_time(1e-7) == "0.0"
    assert format_time(1.5000001) == "1.5"


def test_rttm_writer_matches_contract_line_format() -> None:
    lines = rttm_lines([Segment("spk0", 0.5, 2.0, "x")], "m-0001")
    assert lines == ["SPEAKER m-0001 1 0.5 1.5 <NA> <NA> spk0 <NA> <NA>"]


def test_stm_writer_matches_contract_line_format() -> None:
    lines = stm_lines([Segment("spk0", 0.5, 2.0, "hello there"), Segment("spk1", 3.0, 4.0, "")], "m-0001")
    assert lines == ["m-0001 1 spk0 0.5 2.0 hello there", "m-0001 1 spk1 3.0 4.0"]


@pytest.mark.parametrize(
    "line, needle",
    [
        ("SPEAKER m 1 0.5 1.5 <NA> <NA> spk0 <NA>", "must have 10 fields, got 9"),
        ("SPEAKER m 1 abc 1.5 <NA> <NA> spk0 <NA> <NA>", "start 'abc' is not a number"),
        ("SPEAKER m 1 0.5 0 <NA> <NA> spk0 <NA> <NA>", "duration 0.0 must be strictly positive"),
        ("SPEAKER m 1 0.5 -1 <NA> <NA> spk0 <NA> <NA>", "must be strictly positive"),
        ("SPEAKER m 1 -0.5 1 <NA> <NA> spk0 <NA> <NA>", "start -0.5 is negative"),
        ("NON-SPEECH m 1 0.5 1.5 <NA> <NA> spk0 <NA> <NA>", "type must be SPEAKER"),
        ("SPEAKER m 2 0.5 1.5 <NA> <NA> spk0 <NA> <NA>", "channel must be 1"),
        ("SPEAKER m 1 0.5 1.5 <NA> <NA> <NA> <NA> <NA>", "speaker field is empty"),
        ("SPEAKER m 1 nan 1.5 <NA> <NA> spk0 <NA> <NA>", "is not finite"),
    ],
)
def test_rttm_parser_rejects_malformed_lines(line: str, needle: str) -> None:
    with pytest.raises(ContractError) as exc:
        parse_rttm(";; comment\n\n" + line + "\n", source="ref.rttm")
    assert needle in str(exc.value), str(exc.value)
    assert str(exc.value).startswith("ref.rttm:3")  # line number after comment + blank


def test_rttm_parser_checks_meeting_id_and_can_skip_other_types() -> None:
    text = "SPEAKER other 1 0.5 1.5 <NA> <NA> spk0 <NA> <NA>\n"
    with pytest.raises(ContractError, match="file id 'other' does not match meeting_id 'm'"):
        parse_rttm(text, meeting_id="m")
    text2 = "NON-SPEECH m 1 0 1 <NA> <NA> <NA> <NA> <NA>\nSPEAKER m 1 0.5 1.5 <NA> <NA> spk0 <NA> <NA>\n"
    assert parse_rttm(text2, meeting_id="m", ignore_other_types=True) == [Segment("spk0", 0.5, 2.0)]


@pytest.mark.parametrize(
    "line, needle",
    [
        ("m 1 spk0 0.5", "must have at least 5 fields, got 4"),
        ("m 1 spk0 2.0 2.0 text", "end 2.0 must be strictly after start 2.0"),
        ("m 1 spk0 x 2.0 text", "start 'x' is not a number"),
        ("m 2 spk0 0.5 2.0 text", "channel must be 1"),
        ("m 1 spk0 -1 2.0 text", "start -1.0 is negative"),
    ],
)
def test_stm_parser_rejects_malformed_lines(line: str, needle: str) -> None:
    with pytest.raises(ContractError) as exc:
        parse_stm(line, source="ref.stm")
    assert needle in str(exc.value), str(exc.value)
    assert str(exc.value).startswith("ref.stm:1")


def test_stm_parser_accepts_empty_text_and_collapses_spaces() -> None:
    segs = parse_stm("m 1 spk0 0.5 2.0\nm 1 spk1 3.0 4.0   two    words \n", meeting_id="m")
    assert segs == [Segment("spk0", 0.5, 2.0, ""), Segment("spk1", 3.0, 4.0, "two words")]


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


def test_meeting_json_round_trips_through_disk(tmp_path: Path) -> None:
    original = meeting_dict()
    m = meeting_from_dict(copy.deepcopy(original))
    write_meeting(m, tmp_path)
    assert load_meeting(tmp_path).to_dict() == original


def test_meeting_to_rttm_and_stm_and_back_equals_original(tmp_path: Path) -> None:
    m = meeting_from_dict(meeting_dict())
    rttm_path, stm_path = write_reference_files(m, tmp_path)
    assert rttm_path.name == "ref.rttm" and stm_path.name == "ref.stm"
    from_stm = load_stm(stm_path, meeting_id=m.meeting_id)
    assert [(s.speaker, s.start, s.end, s.text) for s in from_stm] == [
        (s.speaker, s.start, s.end, s.text) for s in m.segments
    ]
    from_rttm = load_rttm(rttm_path, meeting_id=m.meeting_id)
    assert [(s.speaker, s.start, s.end) for s in from_rttm] == [(s.speaker, s.start, s.end) for s in m.segments]


def test_round_trip_tolerates_binary_float_noise(tmp_path: Path) -> None:
    ugly = 0.1 + 0.2  # 0.30000000000000004
    segs = [Segment("a", ugly, ugly + 1.0, "x")]
    p = tmp_path / "x.stm"
    p.write_text("\n".join(stm_lines(segs, "m")) + "\n")
    back = load_stm(p)
    assert back[0].start == pytest.approx(ugly, abs=1e-6)
    assert back[0].end == pytest.approx(ugly + 1.0, abs=1e-6)


def test_hypothesis_files_round_trip_and_are_sorted_on_disk(tmp_path: Path) -> None:
    h = hypothesis_from_dict(hyp_dict())
    j, r, s = write_hypothesis_files(h, tmp_path)
    assert {p.name for p in (j, r, s)} == {"hyp.json", "hyp.rttm", "hyp.stm"}
    assert load_hypothesis(j).to_dict() == h.to_dict()
    assert [x.start for x in load_rttm(r)] == [0.5, 4.0]  # rttm/stm sorted by start
    assert [x.text for x in load_stm(s)] == ["hello there", "ok bye"]
    write_hypothesis(h, tmp_path / "alt.json")
    assert load_hypothesis(tmp_path / "alt.json").system == "unit-test"


def test_written_rttm_and_stm_parse_with_meeteval_independently(tmp_path: Path) -> None:
    """Our writers must produce what a third-party parser calls RTTM/STM."""
    import meeteval.io

    m = meeting_from_dict(meeting_dict())
    rttm_path, stm_path = write_reference_files(m, tmp_path)
    stm = meeteval.io.STM.load(stm_path)
    assert [(l.speaker_id, float(l.begin_time), float(l.end_time), l.transcript) for l in stm.lines] == [
        (s.speaker, s.start, s.end, s.text) for s in m.segments
    ]
    rttm = meeteval.io.RTTM.load(rttm_path)
    assert [(l.speaker_id, float(l.begin_time), float(l.duration)) for l in rttm.lines] == [
        (s.speaker, s.start, s.duration) for s in m.segments
    ]


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello, World!", "hello world"),
        ("  double   spaces\tand\nnewlines ", "double spaces and newlines"),
        ("well-known thing", "well known thing"),
        ("five o'clock", "five o'clock"),
        ("'quoted' word", "quoted word"),
        ("rock 'n' roll", "rock n roll"),
        ("it’s curly", "it's curly"),
        ("(parens) [brackets] {braces} — dash …", "parens brackets braces dash"),
        ("path/to/thing", "path to thing"),
        ("snake_case", "snake case"),
        ("already clean", "already clean"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_default_normalizer(raw: str, expected: str) -> None:
    assert Normalizer()(raw) == expected


def test_normalizer_is_a_pure_function_of_its_settings() -> None:
    n = Normalizer()
    ref, hyp = "Hello, World!", "hello world"
    assert n(ref) == n(hyp)  # identical treatment collapses a punctuation-only difference
    assert n.tokens("A b  C.") == ["a", "b", "c"]
    assert Normalizer(lowercase=False)("Hello") == "Hello"
    assert Normalizer(strip_punctuation=False)("Hello, World!") == "hello, world!"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Don't stop", "do not stop"),
        ("I can't, won't, shan't.", "i cannot will not shall not"),
        ("they're we've you'll he'd I'm let's", "they are we have you will he would i am let us"),
        ("don’t", "do not"),
        ("it's", "it's"),  # 's is ambiguous and left alone
        ("gonna", "going to"),
    ],
)
def test_contraction_expansion(raw: str, expected: str) -> None:
    assert Normalizer(expand_contractions=True)(raw) == expected
    assert Normalizer(expand_contractions=False)("don't") == "don't"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("I have 12 apples", "i have twelve apples"),
        ("1,234.5 units", "one thousand two hundred thirty four point five units"),
        ("year 2026.", "year two thousand twenty six"),
        ("0 and 100 and 1000000", "zero and one hundred and one million"),
        ("-7 degrees", "minus seven degrees"),
        ("v2 is not a number", "v2 is not a number"),
        ("3.14", "three point one four"),
    ],
)
def test_numbers_to_words(raw: str, expected: str) -> None:
    assert Normalizer(numbers_to_words=True)(raw) == expected
    assert Normalizer(numbers_to_words=False)("12") == "12"


def test_number_token_to_words_edges() -> None:
    assert number_token_to_words("999999999999") is not None
    assert number_token_to_words("1000000000000") is None  # out of range -> untouched
    assert number_token_to_words("12a") is None
    assert number_token_to_words("1,23") is None  # bad grouping is not a number


def test_normalized_words_preserve_timing_and_split_multi_token_words() -> None:
    seg = Segment(
        "a",
        0.0,
        2.0,
        "Don't stop !",
        (Word("Don't", 0.0, 1.0), Word("stop", 1.0, 1.5), Word("!", 1.5, 2.0)),
    )
    plain = normalized_words(seg, Normalizer())
    assert [(w.w, w.start, w.end) for w in plain] == [("don't", 0.0, 1.0), ("stop", 1.0, 1.5)]
    expanded = normalized_words(seg, Normalizer(expand_contractions=True))
    assert [(w.w, w.start, w.end) for w in expanded] == [("do", 0.0, 0.5), ("not", 0.5, 1.0), ("stop", 1.0, 1.5)]
    assert normalized_words(Segment("a", 0.0, 1.0, "no timings"), Normalizer()) == []


def test_meeting_and_hypothesis_dataclasses_report_speakers() -> None:
    m = Meeting("m", 16000, 10.0, 1, [{"id": "a"}, {"id": "b"}, {"id": "c"}], [Segment("a", 0, 1, "x"), Segment("c", 1, 2, "y")])
    assert m.speaker_ids == ["a", "b", "c"] and m.active_speakers == ["a", "c"]
    h = Hypothesis("m", "s", [Segment("q", 0, 1, "x"), Segment("p", 1, 2, "y"), Segment("q", 2, 3, "z")])
    assert h.speaker_ids == ["q", "p"]


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-25): non-finite numbers, unreadable files, labels that
# cannot be one RTTM/STM field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, needle",
    [
        ('"end": Infinity', "segments[0]: segment 'start'/'end' must be finite numbers"),
        ('"end": NaN', "segments[0]: segment 'start'/'end' must be finite numbers"),
        ('"end": -Infinity', "segments[0]: segment 'start'/'end' must be finite numbers"),
    ],
)
def test_hypothesis_rejects_non_finite_times_from_json(tmp_path: Path, text: str, needle: str) -> None:
    """EV-8.  json.loads accepts NaN/Infinity; an infinite end used to load and
    turn tcpWER into 2.0 through NaN pseudo-timings."""
    raw = json.dumps(hyp_dict()).replace('"end": 5.0', text, 1)
    assert text in raw
    (tmp_path / "hyp.json").write_text(raw)
    with pytest.raises(ContractError) as exc:
        load_hypothesis(tmp_path / "hyp.json")
    assert needle in str(exc.value), str(exc.value)


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda d: d.update(duration_s=float("inf")), "'duration_s' must be a positive finite number"),
        (lambda d: d["segments"][2].update(end=float("nan")), "must be finite numbers"),
        (lambda d: d["segments"][0]["words"][0].update(start=float("-inf")), "word 'start'/'end' must be finite numbers"),
        (lambda d: d["speakers"][0].update(position_m=[0.0, float("inf"), 1.0]), "'position_m' must be [x, y, z] finite numbers"),
        (lambda d: d.update(meeting_id=";;m"), "starts with ';;'"),
        (lambda d: (d["speakers"][0].update(id="<NA>"), [s.update(speaker="<NA>") for s in d["segments"] if s["speaker"] == "spk0"]), "'<NA>'"),
    ],
)
def test_meeting_rejects_non_finite_numbers_and_unwritable_fields(mutation, needle) -> None:
    with pytest.raises(ContractError) as exc:
        meeting_from_dict(_mutate(mutation), where="meeting.json")
    assert needle in str(exc.value), str(exc.value)


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda d: d.update(meeting_id="m 0001"), "'meeting_id' 'm 0001' must not contain whitespace"),
        (lambda d: d.update(meeting_id=";;m-0001"), "starts with ';;'"),
        (lambda d: d["segments"][0].update(speaker="   "), "'speaker' must be a non-empty string"),
    ],
)
def test_hypothesis_meeting_id_follows_the_reference_rules(mutation, needle) -> None:
    """EV-7.  The hypothesis meeting_id must equal the reference's, so it obeys
    the same single-field rule; a whitespace-only label is no label."""
    d = hyp_dict()
    mutation(d)
    with pytest.raises(ContractError) as exc:
        hypothesis_from_dict(d)
    assert needle in str(exc.value), str(exc.value)


def spaced_hypothesis() -> Hypothesis:
    """The mock cloud's documented labels are 'Speaker 1', 'Speaker 2' (mockcloud/oracle.py)."""
    return hypothesis_from_dict(
        {
            "schema": "plaud-harness/hypothesis/1",
            "meeting_id": "m-0001",
            "system": "cloud",
            "segments": [
                {"speaker": "Speaker 1", "start": 0.0, "end": 2.0, "text": "hello\nthere"},
                {"speaker": "Speaker_1", "start": 2.0, "end": 3.0, "text": "a\tb"},
                {"speaker": "Speaker 2", "start": 3.0, "end": 4.0, "text": "bye"},
                {"speaker": "<NA>", "start": 4.0, "end": 5.0, "text": "x"},
                {"speaker": ";;odd", "start": 5.0, "end": 6.0, "text": "y"},
            ],
        }
    )


def test_hypothesis_files_with_unwritable_labels_stay_parseable_and_distinct(tmp_path: Path) -> None:
    """EV-7.  hyp.json keeps any label; hyp.rttm/hyp.stm replace what one
    RTTM/STM field cannot hold, injectively, and write text on one line."""
    h = spaced_hypothesis()
    j, r, s = write_hypothesis_files(h, tmp_path)
    assert load_hypothesis(j).speaker_ids == ["Speaker 1", "Speaker_1", "Speaker 2", "<NA>", ";;odd"]
    rttm, stm = load_rttm(r, meeting_id="m-0001"), load_stm(s, meeting_id="m-0001")
    assert len(rttm) == len(stm) == 5
    labels = [x.speaker for x in rttm]
    assert labels == [x.speaker for x in stm]
    assert labels == ["Speaker_1#2", "Speaker_1", "Speaker_2", "_NA_", "_;;odd"]
    assert len(set(labels)) == 5  # five speakers in, five speakers out
    assert [(x.start, x.end) for x in stm] == [(x.start, x.end) for x in h.segments]
    assert [x.text for x in stm] == ["hello there", "a b", "bye", "x", "y"]


def test_written_hypothesis_rttm_and_stm_parse_with_meeteval_independently(tmp_path: Path) -> None:
    import meeteval.io

    _, r, s = write_hypothesis_files(spaced_hypothesis(), tmp_path)
    stm = meeteval.io.STM.load(s)
    rttm = meeteval.io.RTTM.load(r)
    assert [l.speaker_id for l in stm.lines] == [l.speaker_id for l in rttm.lines] == [x.speaker for x in load_stm(s)]
    assert [l.transcript for l in stm.lines] == ["hello there", "a b", "bye", "x", "y"]
    assert [(float(l.begin_time), float(l.duration)) for l in rttm.lines] == [(x.start, x.end - x.start) for x in load_rttm(r)]


def test_writers_refuse_a_meeting_id_that_is_not_one_field() -> None:
    with pytest.raises(ContractError, match="whitespace"):
        rttm_lines([Segment("a", 0.0, 1.0)], "m 1")
    with pytest.raises(ContractError, match="starts with ';;'"):
        stm_lines([Segment("a", 0.0, 1.0)], ";;m")


def test_loaders_turn_unreadable_files_into_contract_errors(tmp_path: Path) -> None:
    """EV-5(b).  A non-UTF-8 or unreadable file is an input error, not a traceback."""
    latin1 = tmp_path / "latin1.json"
    latin1.write_bytes(json.dumps(hyp_dict()).replace("unit-test", "caf\u00e9").encode("latin-1"))  # one 0xE9 byte
    assert b"caf\xe9" in latin1.read_bytes()
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load_hypothesis(latin1)
    (tmp_path / "meeting.json").write_bytes(b'{"schema": "caf\xe9"}')
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load_meeting(tmp_path)
    bad_rttm = tmp_path / "x.rttm"
    bad_rttm.write_bytes(b"SPEAKER m 1 0 1 <NA> <NA> caf\xe9 <NA> <NA>\n")
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load_rttm(bad_rttm)
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load_stm(bad_rttm)
    a_dir = tmp_path / "adir.rttm"
    a_dir.mkdir()
    with pytest.raises(ContractError, match="cannot read"):
        load_rttm(a_dir)
    with pytest.raises(ContractError, match="cannot read"):
        load_stm(a_dir)
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "hyp.json").mkdir()  # a directory where the file should be
    with pytest.raises(ContractError, match="cannot read"):
        load_hypothesis(tmp_path / "h")  # directory form -> h/hyp.json, which is not a file
