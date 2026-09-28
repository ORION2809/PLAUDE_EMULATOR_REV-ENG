# ami_tiny: hand-made miniature NXT annotation set

Written by hand for `tests/test_ami_converter.py`. **This is not AMI data.** No
file was copied or derived from the AMI corpus. The meeting ids (`TT0001a`,
`TT0002a`) and participant names (`FTT001` …) are fictional. The layout, element
names and attributes follow the AMI NXT release that `evals/ami.py` reads:
`corpusResources/meetings.xml` and `words/<ID>.<AGENT>.words.xml`. There is no
`segments/` layer, because the converter does not read it (speech turns come
from the word times).

The tests write the audio themselves: 12.0 s of 16 kHz mono PCM16 silence named
`TT0001a.Mix-Headset.wav`. No WAV is committed.

## What each element exercises

Speech time is the union of each speaker's timed `<w>` intervals (punctuation
and `..` included). Intervals that touch or overlap merge into one turn, and any
gap splits. Text is the scorable words inside each turn.

| where | elements | exercises | expected |
|---|---|---|---|
| A words0-3 | `Okay` [0.5, 0.9], `.`, `Mm-hmm` [0.9, 1.4], `,` | touching words merge; zero-length punctuation is not text; hyphen split | turn [0.5, 1.4] `okay mm hmm`; mm [0.9, 1.15], hmm [1.15, 1.4] |
| A words4 | vocalsound [1.4, 1.8] | not speech: the silence to 2.0 splits the turn | no turn of its own |
| A words5-10 | `the`, `T_V_s`, disfmarker, `th` (trunc), `'Kay`, `.` [3.4, 3.6] | acronym split; truncated word kept; stray apostrophe; punctuation **with a duration** is speech time but not text | turn [2.0, 3.6] `the t v s th kay` |
| A words11-14 | `now.I'm` [6.0, 6.4], untimed `going`, `home` [6.4, 7.0], `today` [7.0, 7.5] | Kaldi-style split at `.`; an untimed word has no speech time | turn [6.0, 7.5] `now i'm home today`; `going` dropped |
| A words15 | gap | not speech, not text | – |
| A words16 | `hello` [9.0, 9.0] | an isolated zero-length word | no turn; dropped from the text |
| A words17-18 | `late` [11.5, 12.4], `words` [12.1, 12.3] | the audio ends at 12.0 | `late` clipped to [11.5, 12.0]; `words` dropped |
| B words0-1 | `Yeah` [1.2, 1.6], `.` | overlaps A's first turn | turn [1.2, 1.6] `yeah` |
| B words2-4 | `No` [3.0, 3.5], `wait` [3.2, 3.1], `stop` [3.1, 3.6] | end < start clamped; a start before the previous word's start is raised | turn [3.0, 3.6]; wait [3.2, 3.2], stop [3.2, 3.6] |
| B words5-6 | transformerror, `uh` [4.2, 4.5] | transform error is not speech | turn [4.2, 4.5] `uh` |
| B words7-8 | `..` [5.0, 5.1], `..` [5.1, 5.3] with `trunc="true"` | `..` is speech time with no text; a truncated word that normalises to nothing is counted as empty, not as a kept truncated word | turn [5.0, 5.3] with empty text |
| B words9-11 | `right` [8.0, 8.9], `.` [8.6, 8.6], `yes` [8.9, 9.2] | a word inside the previous one: the running end stays 8.9 (max), so `yes` touches it. BUT's published rule replaces the running end with 8.6 and would split at 8.9 | turn [8.0, 9.2] `right yes` |
| C words0 | vocalsound only | participant declared but never active | no turn |
| D | no words file | participant without annotation files | declared, inactive |
| TT0002a | no files at all | an empty reference is refused | `AmiError` |

Turns: FTT001 [0.5, 1.4], [2.0, 3.6], [6.0, 7.5], [11.5, 12.0] = 4.5 s; MTT002
[1.2, 1.6], [3.0, 3.6], [4.2, 4.5], [5.0, 5.3], [8.0, 9.2] = 2.8 s; total 7.3 s
in 9 segments with 21 words (14 + 7). The union of speech is 6.5 s. Two speakers
talk at once for 0.8 s (1.2-1.4 and 3.0-3.6), so the overlap fraction is
0.8 / 6.5.
