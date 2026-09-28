"""AMI NXT annotations -> contract meeting directories (evals/ami.py).

Two groups:

* Tiny-fixture tests (always run, CI included).  ``tests/fixtures/ami_tiny`` is a
  hand-made miniature NXT set (not AMI data, see its README.md).  Every expected
  value below was derived on paper from those XML files, not read back from the
  converter.
* Real-data tests.  They need the local-only AMI files under data/corpora/ami
  (CC BY 4.0, gitignored, never in CI) and skip with the reason prefix
  ``AMI corpus not present: `` when the files are absent.  The pinned numbers
  were measured on 2026-09-25 from ami_public_manual_1.6.2.zip (sha256
  b56e5bab…, its README says release 1.7), the four Mix-Headset WAVs listed in
  data/corpora/ami/raw/SHA256SUMS.local and the BUT AMI-diarization-setup
  only_words RTTMs at commit 2509d893 (data/corpora/ami/but_setup/PROVENANCE.json).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import wave
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))

from evals import ami  # noqa: E402
from evals.ami import (  # noqa: E402
    AmiError,
    AudioInfo,
    NxtToken,
    Policy,
    build_meeting,
    check_meeting_dir,
    compare_turns,
    convert_meeting,
    crosscheck_rttms,
    extract_annotations,
    meeting_stats,
    meeting_turns,
    read_checksums,
    read_participants,
    read_words,
    rttm_turns,
    self_score,
    speaker_segments,
    verify_mix_audio,
    word_tokens,
)
from evals.io import (  # noqa: E402
    DEFAULT_NORMALIZER,
    ContractError,
    Hypothesis,
    Segment,
    Word,
    load_meeting,
    load_rttm,
    load_stm,
    write_hypothesis_files,
)

FIXTURE = REPO / "tests" / "fixtures" / "ami_tiny"
TINY = "TT0001a"
TINY_FRAMES = 12 * 16000  # 12.0 s of audio: the fixture has words past the end

AMI_ROOT = REPO / "data" / "corpora" / "ami"
AMI_ANNOTATIONS = AMI_ROOT / "raw" / "annotations"
AMI_AUDIO = AMI_ROOT / "raw" / "audio"
AMI_CHECKSUMS = AMI_ROOT / "raw" / "SHA256SUMS.local"
AMI_MEETINGS = AMI_ROOT / "meetings"
AMI_BUT_RTTMS = AMI_ROOT / "but_setup" / "only_words" / "rttms"
SKIP_PREFIX = "AMI corpus not present: "
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def write_silence(path: Path, frames: int = TINY_FRAMES, rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return path


@pytest.fixture()
def tiny_audio(tmp_path: Path) -> Path:
    return write_silence(tmp_path / "audio" / f"{TINY}.Mix-Headset.wav")


@pytest.fixture()
def tiny_dir(tmp_path: Path, tiny_audio: Path) -> Path:
    return convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "meetings").meeting_dir


def W(w: str, start: float, end: float) -> Word:
    return Word(w, start, end)


#: Derived on paper from tests/fixtures/ami_tiny (see its README.md).
EXPECTED_SEGMENTS = [
    Segment("FTT001", 0.5, 1.4, "okay mm hmm", (W("okay", 0.5, 0.9), W("mm", 0.9, 1.15), W("hmm", 1.15, 1.4))),
    Segment("MTT002", 1.2, 1.6, "yeah", (W("yeah", 1.2, 1.6),)),
    Segment("FTT001", 2.0, 3.6, "the t v s th kay", (
        W("the", 2.0, 2.3), W("t", 2.3, 2.466667), W("v", 2.466667, 2.633333), W("s", 2.633333, 2.8),
        W("th", 2.8, 2.95), W("kay", 2.95, 3.4),
    )),
    Segment("MTT002", 3.0, 3.6, "no wait stop", (W("no", 3.0, 3.5), W("wait", 3.2, 3.2), W("stop", 3.2, 3.6))),
    Segment("MTT002", 4.2, 4.5, "uh", (W("uh", 4.2, 4.5),)),
    Segment("MTT002", 5.0, 5.3, "", ()),
    Segment("FTT001", 6.0, 7.5, "now i'm home today", (
        W("now", 6.0, 6.2), W("i'm", 6.2, 6.4), W("home", 6.4, 7.0), W("today", 7.0, 7.5),
    )),
    Segment("MTT002", 8.0, 9.2, "right yes", (W("right", 8.0, 8.9), W("yes", 8.9, 9.2))),
    Segment("FTT001", 11.5, 12.0, "late", (W("late", 11.5, 12.0),)),
]

EXPECTED_COUNTS = {
    # speech time (all <w>)
    "w_elements": 27,
    "w_untimed": 1,  # A going
    "w_negative_duration_clamped": 1,  # B wait
    "w_after_audio_end_dropped": 1,  # A words
    "w_ends_clipped_to_audio": 1,  # A late
    "zero_length_turns_dropped": 1,  # A hello
    # neither speech time nor text
    "dropped_vocalsound": 2,
    "dropped_disfmarker": 1,
    "dropped_gap": 1,
    "dropped_transformerror": 1,
    # text
    "punctuation_not_text": 5,
    "dropped_empty_after_normalisation": 2,  # B words7 and the truncated B words8
    "words_without_speech_time_dropped": 3,  # going (untimed), hello (isolated, zero length), words (past the end)
    "kept_truncated": 1,  # th only: the truncated '..' is not counted here
    "nonmonotonic_words_clamped": 1,  # B stop
    "words_split_into_several_tokens": 3,  # Mm-hmm, T_V_s, now.I'm
    # output
    "turns_without_text": 1,  # B [5.0, 5.3]
    "participants_without_words_file": 1,  # D
    "segments": 9,
    "words": 21,
}


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def test_participants_come_from_meetings_xml_sorted_by_agent() -> None:
    ps = read_participants(FIXTURE / "corpusResources" / "meetings.xml", TINY)
    assert [(p.agent, p.global_name, p.channel, p.role) for p in ps] == [
        ("A", "FTT001", 0, "PM"), ("B", "MTT002", 1, "ID"), ("C", "FTT003", 2, "UI"), ("D", "MTT004", 3, "ME"),
    ]
    with pytest.raises(AmiError, match="no meeting with observation='XX9999z'"):
        read_participants(FIXTURE / "corpusResources" / "meetings.xml", "XX9999z")


def test_words_file_keeps_every_element_in_document_order() -> None:
    toks = read_words(FIXTURE / "words" / f"{TINY}.A.words.xml")
    assert [t.kind for t in toks[:8]] == ["w", "w", "w", "w", "vocalsound", "w", "w", "disfmarker"]
    assert toks[1].punc and not toks[0].punc
    assert toks[8].trunc and toks[8].text == "th"
    assert toks[9].text == "'Kay"  # &#39; decoded
    assert toks[12].start is None and toks[12].end is None  # untimed
    assert (toks[17].start, toks[17].end) == (11.5, 12.4)


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("Okay", ["okay"]),
        ("Mm-hmm", ["mm", "hmm"]),
        ("T_V_s", ["t", "v", "s"]),
        ("L_C_D_", ["l", "c", "d"]),
        ("'Kay", ["kay"]),
        ("now.I'm", ["now", "i'm"]),  # the evals normaliser alone would give "nowi'm"
        ("uh,I", ["uh", "i"]),
        ("..", []),
        ("th", ["th"]),
    ],
)
def test_word_tokens_split_like_kaldi_then_normalise_like_evals(text: str, tokens: list[str]) -> None:
    assert word_tokens(text) == tokens
    joined = " ".join(tokens)
    assert DEFAULT_NORMALIZER(joined) == joined  # a fixed point: scoring re-normalises to the same tokens


# --------------------------------------------------------------------------- #
# Conversion of the tiny fixture
# --------------------------------------------------------------------------- #


def test_tiny_fixture_converts_to_the_hand_derived_meeting(tiny_dir: Path) -> None:
    m = load_meeting(tiny_dir)  # evals' own loader accepts it
    assert m.meeting_id == TINY
    assert (m.sample_rate, m.channels, m.duration_s) == (16000, 1, 12.0)
    assert m.speakers == [
        {"id": "FTT001", "nxt_agent": "A", "channel": 0, "role": "PM"},
        {"id": "MTT002", "nxt_agent": "B", "channel": 1, "role": "ID"},
        {"id": "FTT003", "nxt_agent": "C", "channel": 2, "role": "UI"},
        {"id": "MTT004", "nxt_agent": "D", "channel": 3, "role": "ME"},
    ]
    assert m.active_speakers == ["FTT001", "MTT002"]
    assert m.segments == EXPECTED_SEGMENTS
    scen = m.generator["scenario"]
    assert scen["counts"] == EXPECTED_COUNTS
    assert scen["policy"] == {"truncated": "keep"}
    assert scen["meeting_type"] == "scenario" and scen["meetings_xml_duration_s"] == 15.0
    assert m.generator["name"] == "evals.ami" and m.generator["version"] == "2"
    assert m.generator["timing_exact"] is False
    assert m.audio["mix_wav"] == "mix.wav" and m.audio["mix_source"]["frames"] == TINY_FRAMES


def test_tiny_reference_files_are_the_segments(tiny_dir: Path) -> None:
    rttm = (tiny_dir / "ref.rttm").read_text().splitlines()
    stm = (tiny_dir / "ref.stm").read_text().splitlines()
    assert rttm[0] == "SPEAKER TT0001a 1 0.5 0.9 <NA> <NA> FTT001 <NA> <NA>"
    assert rttm[2] == "SPEAKER TT0001a 1 2.0 1.6 <NA> <NA> FTT001 <NA> <NA>"  # punctuation [3.4, 3.6] is speech
    assert rttm[5] == "SPEAKER TT0001a 1 5.0 0.3 <NA> <NA> MTT002 <NA> <NA>"  # '..' only: speech, no text
    assert stm[5] == "TT0001a 1 MTT002 5.0 5.3"
    assert stm[6] == "TT0001a 1 FTT001 6.0 7.5 now i'm home today"
    assert len(rttm) == len(stm) == 9
    rt = load_rttm(tiny_dir / "ref.rttm", meeting_id=TINY)
    assert sum(s.end - s.start for s in rt) == pytest.approx(7.3, abs=1e-9)
    assert {s.speaker for s in rt} == {"FTT001", "MTT002"}
    assert [s.text for s in load_stm(tiny_dir / "ref.stm", meeting_id=TINY)] == [s.text for s in EXPECTED_SEGMENTS]
    check_meeting_dir(tiny_dir)


def test_tiny_statistics_preserve_overlap(tiny_dir: Path) -> None:
    st = meeting_stats(load_meeting(tiny_dir))
    assert st["speakers_declared"] == 4 and st["speakers_active"] == 2
    assert st["segments"] == 9 and st["segments_without_text"] == 1 and st["words"] == 21
    assert st["words_per_speaker"] == {"FTT001": 14, "MTT002": 7, "FTT003": 0, "MTT004": 0}
    assert st["speaker_time_s"] == pytest.approx(7.3, abs=1e-9)
    assert st["speaker_time_per_speaker_s"]["FTT001"] == pytest.approx(4.5, abs=1e-9)
    assert st["speech_union_s"] == pytest.approx(6.5, abs=1e-9)
    assert st["overlap_s"] == pytest.approx(0.8, abs=1e-9)
    assert st["overlap_fraction"] == pytest.approx(0.8 / 6.5, abs=1e-9)


def test_tiny_reference_scored_against_itself_is_zero(tiny_dir: Path) -> None:
    score = self_score(tiny_dir)
    for path in ("meeting_json", "ref_rttm_stm"):
        assert score[path]["der"] == 0.0, path
        assert score[path]["jer"] == 0.0, path
        assert score[path]["cpwer"] == 0.0, path
        assert score[path]["speaker_count_error"] == 0, path
    assert score["meeting_json"]["tcpwer"] == 0.0


def test_tiny_meeting_passes_the_oracle_suite_through_the_evals_cli(tiny_dir: Path, tmp_path: Path) -> None:
    from evals.cli import main as evals_main

    m = load_meeting(tiny_dir)
    write_hypothesis_files(Hypothesis(TINY, "oracle", list(m.segments)), tmp_path / "hyps" / TINY)
    rc = evals_main([
        "batch", "--refs", str(tiny_dir.parent), "--hyps", str(tmp_path / "hyps"),
        "--suite", "oracle", "--gate-on", "each", "--report", str(tmp_path / "r.json"), "--quiet",
    ])
    assert rc == 0
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["n_meetings"] == 1 and rep["meetings"][0]["der"]["der"] == 0.0


def test_pipeline_reader_accepts_the_directory_and_its_mix(tiny_dir: Path) -> None:
    from pipeline.meeting import read_meeting, resolve_audio

    m = read_meeting(tiny_dir)
    assert resolve_audio(tiny_dir, m, "mix") == tiny_dir / "mix.wav"


def test_truncated_words_can_be_dropped_from_the_text_only(tiny_audio: Path) -> None:
    built = build_meeting(FIXTURE, TINY, AudioInfo(16000, 1, TINY_FRAMES), policy=Policy(truncated="drop"))
    third = built.meeting.segments[2]
    assert (third.start, third.end, third.text) == (2.0, 3.6, "the t v s kay")  # th's time stays speech
    assert built.counts["dropped_truncated"] == 1 and "kept_truncated" not in built.counts
    assert built.counts["dropped_empty_after_normalisation"] == 2  # the truncated '..' is still counted as empty
    assert (built.counts["segments"], built.counts["words"]) == (9, 20)
    assert built.meeting.generator["scenario"]["policy"] == {"truncated": "drop"}
    with pytest.raises(ValueError, match="truncated"):
        Policy(truncated="maybe")


def T(i: int, start: float | None, end: float | None, text: str = "w", kind: str = "w", punc: bool = False,
      trunc: bool = False) -> NxtToken:
    return NxtToken(f"X.A.words{i}", kind, text, start, end, punc, trunc)


def test_speech_turns_merge_touching_words_and_split_at_any_gap() -> None:
    counts: Counter = Counter()
    tokens = [
        T(0, 0.0, 1.0, "a"), T(1, 1.0, 2.0, "b"),  # touching: one turn
        T(2, 2.01, 3.0, "c"),  # a 0.01 s pause splits (BUT: pauses are never merged)
        T(3, 3.0, 4.0, kind="vocalsound"),  # not speech: the next word does not touch c
        T(4, 4.0, 5.0, "d"), T(5, 4.5, 4.6, ".", punc=True), T(6, 5.0, 6.0, "e"),  # running end stays 5.0 (max)
        T(7, 7.0, 7.5, "?", punc=True),  # punctuation with a duration is speech, not text
    ]
    segs = speaker_segments("S", tokens, 100.0, Policy(), counts)
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 2.0, "a b"), (2.01, 3.0, "c"), (4.0, 6.0, "d e"), (7.0, 7.5, ""),
    ]
    assert counts["turns_without_text"] == 1 and counts["dropped_vocalsound"] == 1
    assert counts["punctuation_not_text"] == 2


def test_overlapping_words_of_one_speaker_become_one_turn() -> None:
    counts: Counter = Counter()
    tokens = [T(0, 1.0, 3.0, "long"), T(1, 2.0, 2.5, "inner"), T(2, 2.8, 4.0, "late"), T(3, 0.5, 1.2, "early")]
    segs = speaker_segments("S", tokens, 100.0, Policy(), counts)
    assert [(s.start, s.end) for s in segs] == [(0.5, 4.0)]
    assert [(w.w, w.start, w.end) for w in segs[0].words] == [
        ("long", 1.0, 3.0), ("inner", 2.0, 2.5), ("late", 2.8, 4.0), ("early", 2.8, 2.8),  # document order kept
    ]
    assert counts["nonmonotonic_words_clamped"] == 1


def test_a_truncated_word_that_normalises_to_nothing_is_not_counted_as_kept() -> None:
    counts: Counter = Counter()
    tokens = [T(0, 0.0, 1.0, "..", trunc=True), T(1, 1.0, 2.0, "th", trunc=True), T(2, 5.0, 5.0, "x", trunc=True)]
    segs = speaker_segments("S", tokens, 100.0, Policy(), counts)
    assert [s.text for s in segs] == ["th"]
    assert counts["dropped_empty_after_normalisation"] == 1
    assert counts["words_without_speech_time_dropped"] == 1  # x: isolated, zero length
    assert counts["kept_truncated"] == 1


def test_the_segments_layer_is_not_read(tmp_path: Path) -> None:
    root = tmp_path / "ann"
    shutil.copytree(FIXTURE, root)
    (root / "segments").mkdir()
    (root / "segments" / f"{TINY}.A.segments.xml").write_text("this is not XML")
    built = build_meeting(root, TINY, AudioInfo(16000, 1, TINY_FRAMES))
    assert built.meeting.segments == EXPECTED_SEGMENTS


# --------------------------------------------------------------------------- #
# Output directory, audio placement, errors
# --------------------------------------------------------------------------- #


def test_mix_wav_is_a_read_only_hard_link_not_a_symlink(tiny_dir: Path, tiny_audio: Path) -> None:
    mix = tiny_dir / "mix.wav"
    assert not mix.is_symlink()
    assert os.path.samefile(mix, tiny_audio)
    assert load_meeting(tiny_dir).audio["mix_source"]["placed_as"] == "hardlink"
    # the mode belongs to the shared inode: the source WAV is read-only too
    assert stat.S_IMODE(os.stat(mix).st_mode) & WRITE_BITS == 0
    assert stat.S_IMODE(os.stat(tiny_audio).st_mode) & WRITE_BITS == 0
    if os.geteuid() != 0:  # root ignores permission bits
        with pytest.raises(PermissionError):
            open(mix, "r+b").close()  # an in-place writer (e.g. soundfile.write) fails instead of rewriting the source


def test_mix_wav_falls_back_to_a_read_only_copy_when_linking_fails(
    tmp_path: Path, tiny_audio: Path, monkeypatch
) -> None:
    def refuse(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(ami.os, "link", refuse)
    res = convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "out")
    mix = res.meeting_dir / "mix.wav"
    assert res.audio_placed_as == "copy" and not os.path.samefile(mix, tiny_audio)
    assert mix.read_bytes() == tiny_audio.read_bytes()
    assert stat.S_IMODE(os.stat(mix).st_mode) & WRITE_BITS == 0
    assert stat.S_IMODE(os.stat(tiny_audio).st_mode) & WRITE_BITS != 0  # a copy leaves the source alone
    res2 = convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "out2", link="copy")
    assert res2.audio_placed_as == "copy"


def test_a_writable_or_altered_mix_wav_is_caught(tmp_path: Path, tiny_audio: Path) -> None:
    d = convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "out", link="copy").meeting_dir
    mix = d / "mix.wav"
    assert verify_mix_audio(d) == load_meeting(d).audio["mix_source"]["sha256"]
    os.chmod(mix, 0o644)
    with pytest.raises(ContractError, match="must be read-only"):
        check_meeting_dir(d)
    with open(mix, "r+b") as f:  # what a write through a hard link would do
        f.seek(-2, os.SEEK_END)
        f.write(b"\x01\x00")
    with pytest.raises(ContractError, match="differs from the converted"):
        verify_mix_audio(d)


def test_the_source_wav_is_checked_against_a_checksum_file(tmp_path: Path, tiny_audio: Path) -> None:
    good = ami._sha256(tiny_audio)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{good}  data/somewhere/{tiny_audio.name}\n{'0' * 64} *other.wav\n")
    assert read_checksums(sums) == {tiny_audio.name: good, "other.wav": "0" * 64}
    res = convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "ok", checksums=sums)
    assert res.meeting.audio["mix_source"]["sha256_verified_by"] == "SHA256SUMS"
    unlisted = tmp_path / "OTHER_SUMS"
    unlisted.write_text(f"{good}  x.wav\n")
    res = convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "unlisted", checksums=unlisted)
    assert res.meeting.audio["mix_source"]["sha256_verified_by"] is None
    sums.write_text(f"{'f' * 64}  {tiny_audio.name}\n")
    with pytest.raises(AmiError, match="does not match"):
        convert_meeting(FIXTURE, TINY, tiny_audio, tmp_path / "bad", checksums=sums)
    assert not (tmp_path / "bad").exists()  # refused before anything is written
    sums.write_text("not a checksum line\n")
    with pytest.raises(AmiError, match="not a '<sha256>  <path>' line"):
        read_checksums(sums)
    sums.write_text(f"{'a' * 64}  d1/x.wav\n{'b' * 64}  d2/x.wav\n")
    with pytest.raises(AmiError, match="two different digests"):
        read_checksums(sums)


def test_reconversion_replaces_own_files_and_refuses_foreign_ones(tiny_dir: Path, tiny_audio: Path) -> None:
    before = (tiny_dir / "meeting.json").read_text()
    convert_meeting(FIXTURE, TINY, tiny_audio, tiny_dir.parent)  # idempotent rewrite, over a read-only mix.wav
    assert (tiny_dir / "meeting.json").read_text() == before
    (tiny_dir / "hyp.json").write_text("{}")
    with pytest.raises(FileExistsError, match=r"did not write: \['hyp.json'\]"):
        convert_meeting(FIXTURE, TINY, tiny_audio, tiny_dir.parent)
    assert (tiny_dir / "meeting.json").read_text() == before  # untouched


def test_a_meeting_without_annotations_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AmiError, match="TT0002a yields no speech turn"):
        build_meeting(FIXTURE, "TT0002a", AudioInfo(16000, 1, 5 * 16000))


def _broken_copy(tmp_path: Path, agent: str, old: str, new: str) -> Path:
    root = tmp_path / "ann"
    shutil.copytree(FIXTURE, root)
    p = root / "words" / f"{TINY}.{agent}.words.xml"
    text = p.read_text(encoding="latin-1")
    assert old in text
    p.write_text(text.replace(old, new, 1), encoding="latin-1")
    return root


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('nite:id="TT0001a.A.words1"', 'nite:id="TT0001a.A.words0"', "duplicate nite:id TT0001a.A.words0"),
        ('<w nite:id="TT0001a.A.words5" ', "<w ", "<w> without nite:id"),
        ('starttime="0.50"', 'starttime="half"', "attribute starttime='half' is not a number"),
        ("</nite:root>", "", "not well-formed XML"),
    ],
)
def test_broken_annotations_are_reported_with_the_file(tmp_path: Path, old: str, new: str, message: str) -> None:
    root = _broken_copy(tmp_path, "A", old, new)
    with pytest.raises(AmiError) as exc:
        build_meeting(root, TINY, AudioInfo(16000, 1, TINY_FRAMES))
    assert message in str(exc.value) and f"{TINY}.A.words.xml" in str(exc.value)


def test_audio_that_is_not_a_wav_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / f"{TINY}.Mix-Headset.wav"
    bad.write_bytes(b"OggS" + b"\x00" * 60)
    with pytest.raises(AmiError, match="not a PCM WAV"):
        convert_meeting(FIXTURE, TINY, bad, tmp_path / "out")
    with pytest.raises(AmiError, match="audio file not found"):
        convert_meeting(FIXTURE, TINY, tmp_path / "missing.wav", tmp_path / "out")


def test_release_line_is_recorded_when_the_readme_is_present(tmp_path: Path) -> None:
    root = tmp_path / "ann"
    shutil.copytree(FIXTURE, root)
    (root / "00README_MANUAL.txt").write_text("Hand-made release line\nsecond line\n")
    built = build_meeting(root, TINY, AudioInfo(16000, 1, TINY_FRAMES))
    assert built.meeting.generator["scenario"]["annotations_release"] == "Hand-made release line"
    assert build_meeting(FIXTURE, TINY, AudioInfo(16000, 1, TINY_FRAMES)).meeting.generator["scenario"][
        "annotations_release"] is None


# --------------------------------------------------------------------------- #
# Cross-check against published RTTMs
# --------------------------------------------------------------------------- #

#: The fixture's turns as RTTM lines, written by hand from README.md (2-decimal, BUT style).
TINY_RTTM = """\
SPEAKER TT0001a 1 0.50 0.90 <NA> <NA> FTT001 <NA> <NA>
SPEAKER TT0001a 1 1.20 0.40 <NA> <NA> MTT002 <NA> <NA>
SPEAKER TT0001a 1 2.00 1.60 <NA> <NA> FTT001 <NA> <NA>
SPEAKER TT0001a 1 3.00 0.60 <NA> <NA> MTT002 <NA> <NA>
SPEAKER TT0001a 1 4.20 0.30 <NA> <NA> MTT002 <NA> <NA>
SPEAKER TT0001a 1 5.00 0.30 <NA> <NA> MTT002 <NA> <NA>
SPEAKER TT0001a 1 6.00 1.50 <NA> <NA> FTT001 <NA> <NA>
SPEAKER TT0001a 1 8.00 1.20 <NA> <NA> MTT002 <NA> <NA>
SPEAKER TT0001a 1 11.50 0.50 <NA> <NA> FTT001 <NA> <NA>
"""


def test_rttm_turns_sum_in_decimal_at_millisecond_resolution() -> None:
    assert rttm_turns("SPEAKER X 1 0.37 1.37 <NA> <NA> MEE071 <NA> <NA>\n\n") == [("MEE071", 370, 1740)]
    assert rttm_turns("SPEAKER X 1 335.304 0.546 <NA> <NA> S <NA> <NA>") == [("S", 335304, 335850)]
    with pytest.raises(AmiError, match="not an RTTM SPEAKER line"):
        rttm_turns("LEXEME X 1 0 1 a b c", source="f.rttm")


def test_crosscheck_finds_identical_and_differing_turns(tmp_path: Path, capsys) -> None:
    root = tmp_path / "rttms" / "test"
    root.mkdir(parents=True)
    (root / f"{TINY}.rttm").write_text(TINY_RTTM)
    built = build_meeting(FIXTURE, TINY, AudioInfo(16000, 1, TINY_FRAMES))
    assert meeting_turns(built.meeting) == rttm_turns(TINY_RTTM)
    # stand-in duration: 'words' [12.1, 12.3] is speech there, so crosscheck needs an RTTM that has it too
    extra = "SPEAKER TT0001a 1 11.50 0.90 <NA> <NA> FTT001 <NA> <NA>\n"
    (root / f"{TINY}.rttm").write_text(TINY_RTTM.replace(TINY_RTTM.splitlines()[-1] + "\n", extra))
    [diff] = crosscheck_rttms(FIXTURE, tmp_path / "rttms")
    assert diff.identical and (diff.ours, diff.theirs) == (9, 9)
    assert ami.main(["crosscheck", "--annotations", str(FIXTURE), "--rttms", str(tmp_path / "rttms")]) == 0
    assert "meetings compared: 1, identical: 1, differing: 0" in capsys.readouterr().out
    # BUT's replace-the-running-end rule would split MTT002's [8.0, 9.2] at 8.6 / 8.9
    but_style = TINY_RTTM.replace(
        "SPEAKER TT0001a 1 8.00 1.20 <NA> <NA> MTT002 <NA> <NA>\n",
        "SPEAKER TT0001a 1 8.00 0.60 <NA> <NA> MTT002 <NA> <NA>\nSPEAKER TT0001a 1 8.90 0.30 <NA> <NA> MTT002 <NA> <NA>\n",
    ).replace(TINY_RTTM.splitlines()[-1] + "\n", extra)
    (root / f"{TINY}.rttm").write_text(but_style)
    [diff] = crosscheck_rttms(FIXTURE, tmp_path / "rttms")
    assert diff.only_ours == [("MTT002", 8000, 9200)]
    assert diff.only_theirs == [("MTT002", 8000, 8600), ("MTT002", 8900, 9200)]
    assert ami.main(["crosscheck", "--annotations", str(FIXTURE), "--rttms", str(tmp_path / "rttms")]) == 1
    out = capsys.readouterr().out
    assert "only ours:   MTT002 8.000-9.200" in out and "differing: 1" in out
    assert ami.main(["crosscheck", "--annotations", str(FIXTURE), "--rttms", str(tmp_path / "none")]) == 3
    assert compare_turns("X", [("a", 1, 2)], [("a", 1, 2)]).identical


# --------------------------------------------------------------------------- #
# Extraction and CLI
# --------------------------------------------------------------------------- #


def _zip_fixture(path: Path, extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for f in sorted(FIXTURE.rglob("*.xml")):
            z.write(f, f.relative_to(FIXTURE).as_posix())
        z.writestr("abstractive/TT0001a.abssumm.xml", "<x/>")  # layers the converter does not read
        z.writestr("segments/TT0001a.A.segments.xml", "<x/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def test_extract_takes_only_the_members_the_converter_reads(tmp_path: Path) -> None:
    zp = _zip_fixture(tmp_path / "tiny.zip")
    written = extract_annotations(zp, tmp_path / "ann")
    rel = sorted(p.relative_to(tmp_path / "ann").as_posix() for p in written)
    assert rel == sorted(f.relative_to(FIXTURE).as_posix() for f in FIXTURE.rglob("*.xml"))
    assert not (tmp_path / "ann" / "abstractive").exists() and not (tmp_path / "ann" / "segments").exists()
    manifest = json.loads((tmp_path / "ann" / "EXTRACTED_FROM.json").read_text())
    assert manifest["zip"] == "tiny.zip" and manifest["members"] == len(written) and len(manifest["sha256"]) == 64
    built = build_meeting(tmp_path / "ann", TINY, AudioInfo(16000, 1, TINY_FRAMES))
    assert built.meeting.segments == EXPECTED_SEGMENTS
    assert built.meeting.generator["scenario"]["annotations_extracted_from"] == manifest


def test_extract_refuses_a_member_that_escapes_the_destination(tmp_path: Path) -> None:
    zp = _zip_fixture(tmp_path / "evil.zip", {"words/../../escaped.xml": "<x/>"})
    with pytest.raises(AmiError, match="unsafe member name"):
        extract_annotations(zp, tmp_path / "ann" / "inner")
    assert not (tmp_path / "escaped.xml").exists() and not (tmp_path / "ann").exists()


def test_cli_convert_and_stats(tmp_path: Path, tiny_audio: Path, capsys) -> None:
    out = tmp_path / "cli-out"
    sums = tmp_path / "SUMS"
    sums.write_text(f"{ami._sha256(tiny_audio)}  {tiny_audio.name}\n")
    rc = ami.main(["convert", "--annotations", str(FIXTURE), "--audio-dir", str(tiny_audio.parent), "--out", str(out),
                   "--checksums", str(sums)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "TT0001a: 12.000 s, 2/4 speakers, 9 segments, 21 words, overlap 0.1231" in printed
    assert "mix.wav hardlink (read-only), sha256 verified by SUMS" in printed
    assert ami.main(["stats", "--meetings", str(out), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["rttm"] == {"lines": 9, "speakers": 2, "total_duration_s": pytest.approx(7.3, abs=1e-9)}
    assert rows[0]["self_score"]["meeting_json"]["der"] == 0.0
    assert rows[0]["mix_sha256"] == ami._sha256(tiny_audio)
    assert ami.main(["stats", "--meetings", str(out)]) == 0
    assert "9(1)  21" in capsys.readouterr().out
    assert ami.main(["stats", "--meetings", str(tmp_path / "nothing")]) == 3
    (out / TINY / "notes.txt").write_text("x")
    assert ami.main(["convert", "--annotations", str(FIXTURE), "--audio-dir", str(tiny_audio.parent),
                     "--out", str(out)]) == 2
    assert "did not write" in capsys.readouterr().err
    assert ami.main(["convert", "--annotations", str(FIXTURE), "--audio-dir", str(tmp_path / "none"),
                     "--out", str(out)]) == 2
    assert ami.main(["convert", "--annotations", str(FIXTURE), "--audio-dir", str(tiny_audio.parent),
                     "--out", str(tmp_path / "o2"), "--checksums", str(tmp_path / "missing")]) == 2
    assert "--checksums" in capsys.readouterr().err


def test_cli_turns_an_unexpected_error_into_exit_2(tmp_path: Path, tiny_audio: Path, monkeypatch, capsys) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(ami, "convert_meeting", boom)
    assert ami.main(["convert", "--annotations", str(FIXTURE), "--audio-dir", str(tiny_audio.parent),
                     "--out", str(tmp_path / "o")]) == 2
    assert "RuntimeError: injected" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Real AMI data (local-only; skipped when absent)
# --------------------------------------------------------------------------- #

#: Measured 2026-09-25 (see the module docstring for the input files).
#: frames: WAV header; segments/words/overlap: this converter at CONVERTER_VERSION 2,
#: Policy(truncated="keep").  The turns equal the BUT only_words RTTMs line for line.
AMI_PINNED = {
    "EN2002a": {"frames": 34283350, "segments": 746, "words": 7632, "speaker_time_s": 2530.26, "overlap_fraction": 0.2741991},
    "ES2004a": {"frames": 16789675, "segments": 260, "words": 2653, "speaker_time_s": 923.43, "overlap_fraction": 0.1578987},
    "IS1009a": {"frames": 13421333, "segments": 195, "words": 2018, "speaker_time_s": 695.90, "overlap_fraction": 0.1357204},
    "TS3003a": {"frames": 24090282, "segments": 242, "words": 2534, "speaker_time_s": 1025.964, "overlap_fraction": 0.0457581},
}

#: The only BUT only_words RTTMs the converter's turns differ from (of 170), and how.
#: Both come from a merged word that ends before the running end of its turn
#: (docs/evals.md "AMI").
BUT_DIFFERENCES = {
    "EN2006a": {"only_ours": [("FEE088", 2162240, 2162310)], "only_theirs": []},
    "TS3009c": {
        "only_ours": [("MTD036ME", 877690, 880430), ("MTD036ME", 1270410, 1277750), ("MTD036ME", 1495460, 1498670),
                      ("MTD036ME", 1543180, 1547980), ("MTD036ME", 1695780, 1702350)],
        "only_theirs": [("MTD036ME", 877690, 879520), ("MTD036ME", 879530, 880430), ("MTD036ME", 1270410, 1273120),
                        ("MTD036ME", 1273130, 1277750), ("MTD036ME", 1495460, 1496010), ("MTD036ME", 1496020, 1498670),
                        ("MTD036ME", 1543180, 1545470), ("MTD036ME", 1545480, 1547980), ("MTD036ME", 1695780, 1697810),
                        ("MTD036ME", 1697820, 1702350)],
    },
}


def _require_annotations() -> None:
    if not (AMI_ANNOTATIONS / "corpusResources" / "meetings.xml").is_file():
        pytest.skip(f"{SKIP_PREFIX}{AMI_ANNOTATIONS.relative_to(REPO)} (python -m evals.ami extract)")


def _require_ami(meeting_id: str) -> Path:
    _require_annotations()
    wav = AMI_AUDIO / f"{meeting_id}.Mix-Headset.wav"
    if not wav.is_file():
        pytest.skip(f"{SKIP_PREFIX}{wav.relative_to(REPO)}")
    return wav


def _require_but() -> None:
    _require_annotations()
    if not AMI_BUT_RTTMS.is_dir():
        pytest.skip(f"{SKIP_PREFIX}{AMI_BUT_RTTMS.relative_to(REPO)} (BUT AMI-diarization-setup, docs/evals.md)")


@pytest.mark.timeout(300)
@pytest.mark.parametrize("meeting_id", sorted(AMI_PINNED))
def test_real_ami_meeting_converts_validates_and_scores_zero_against_itself(meeting_id: str, tmp_path: Path) -> None:
    wav = _require_ami(meeting_id)
    res = convert_meeting(AMI_ANNOTATIONS, meeting_id, wav, tmp_path, checksums=AMI_CHECKSUMS)
    assert res.meeting.audio["mix_source"]["sha256_verified_by"] == "SHA256SUMS.local"
    m = check_meeting_dir(res.meeting_dir)
    pin = AMI_PINNED[meeting_id]
    assert m.duration_s == pin["frames"] / 16000 and (m.sample_rate, m.channels) == (16000, 1)
    st = meeting_stats(m)
    assert st["speakers_declared"] == st["speakers_active"] == 4
    assert (st["segments"], st["words"]) == (pin["segments"], pin["words"])
    assert st["speaker_time_s"] == pytest.approx(pin["speaker_time_s"], abs=1e-6)
    assert st["overlap_fraction"] == pytest.approx(pin["overlap_fraction"], abs=1e-7)
    assert 0.5 < st["speech_fraction_of_duration"] < 0.95
    counts = res.counts
    for repair in ("w_untimed", "w_negative_duration_clamped", "w_after_audio_end_dropped", "w_ends_clipped_to_audio",
                   "nonmonotonic_words_clamped", "words_without_speech_time_dropped"):
        assert repair not in counts, (repair, counts)
    score = self_score(res.meeting_dir)
    assert score["meeting_json"] == {"der": 0.0, "jer": 0.0, "cpwer": 0.0, "tcpwer": 0.0, "speaker_count_error": 0}
    assert (score["ref_rttm_stm"]["der"], score["ref_rttm_stm"]["cpwer"]) == (0.0, 0.0)


@pytest.mark.parametrize("meeting_id", sorted(AMI_PINNED))
def test_local_ami_meeting_directories_are_current(meeting_id: str, tmp_path: Path) -> None:
    """data/corpora/ami/meetings/<ID> equals a fresh conversion (so a converter change is re-run)."""
    wav = _require_ami(meeting_id)
    local = AMI_MEETINGS / meeting_id
    if not (local / "meeting.json").is_file():
        pytest.skip(f"{SKIP_PREFIX}{local.relative_to(REPO)} (python -m evals.ami convert)")
    fresh = convert_meeting(AMI_ANNOTATIONS, meeting_id, wav, tmp_path, checksums=AMI_CHECKSUMS).meeting_dir
    for name in ("ref.rttm", "ref.stm"):
        assert (local / name).read_text() == (fresh / name).read_text(), name
    ours, new = (json.loads((d / "meeting.json").read_text()) for d in (local, fresh))
    for doc in (ours, new):  # hard link vs copy depends on where tmp_path lives
        doc["audio"]["mix_source"].pop("placed_as")
    assert ours == new
    assert os.path.samefile(local / "mix.wav", wav) or (local / "mix.wav").read_bytes() == wav.read_bytes()
    check_meeting_dir(local)
    verify_mix_audio(local)


@pytest.mark.timeout(300)
def test_every_meeting_in_the_release_builds_and_validates() -> None:
    """All of meetings.xml, with the stand-in duration (no audio needed), pinned by a digest."""
    _require_annotations()
    assert ami.release_digest(AMI_ANNOTATIONS, validate=True) == RELEASE_DIGEST


#: CONVERTER_VERSION 2, Policy(truncated="keep"), stand-in duration; measured 2026-09-25.
RELEASE_DIGEST = {
    "meetings": 171, "segments": 83502, "words": 992675,
    "rttm_sha256": "fab4d7bf588e0771fcd3d867f48cec02b4b675cf2dd1ab18189da766ee1e9294",
}


@pytest.mark.timeout(300)
def test_turns_equal_the_but_only_words_rttms_except_two_pinned_meetings() -> None:
    _require_but()
    diffs = crosscheck_rttms(AMI_ANNOTATIONS, AMI_BUT_RTTMS)
    assert len(diffs) == 170
    differing = {d.meeting_id: {"only_ours": d.only_ours, "only_theirs": d.only_theirs}
                 for d in diffs if not d.identical}
    assert differing == BUT_DIFFERENCES
    test_ids = (AMI_BUT_RTTMS / "test").glob("*.rttm")
    dev_ids = (AMI_BUT_RTTMS / "dev").glob("*.rttm")
    assert not {p.stem for p in [*test_ids, *dev_ids]} & set(differing)  # dev and test are identical


def _but_replace_end_turns(tokens: list[NxtToken]) -> list[tuple[int, int]]:
    """The rule BUT's published RTTMs follow: document order, merge when start <= running end,
    and the running end becomes the merged word's end (not the max); non-positive turns vanish."""
    out, cur = [], None
    for t in tokens:
        if t.kind != "w" or t.start is None or t.end is None:
            continue
        if cur is not None and t.start <= cur[1]:
            cur[1] = t.end
        else:
            if cur is not None:
                out.append(cur)
            cur = [t.start, t.end]
    if cur is not None:
        out.append(cur)
    return [(ami._ms(Decimal(repr(a))), ami._ms(Decimal(repr(b)))) for a, b in out if b > a]


@pytest.mark.timeout(300)
def test_the_replace_end_rule_reproduces_every_but_rttm() -> None:
    """Evidence for the documented cause of the two differences: BUT's rule, as inferred."""
    _require_but()
    meetings_xml = AMI_ANNOTATIONS / "corpusResources" / "meetings.xml"
    for path in sorted(AMI_BUT_RTTMS.rglob("*.rttm")):
        mid = path.stem
        turns = []
        for p in read_participants(meetings_xml, mid):
            words = AMI_ANNOTATIONS / "words" / f"{mid}.{p.agent}.words.xml"
            if words.exists():
                turns += [(p.global_name, a, b) for a, b in _but_replace_end_turns(read_words(words))]
        assert sorted(turns) == rttm_turns(path.read_text()), mid
