# Layer 3 — evaluation harness (`evals/`)

Status 2026-09-25: **implemented, reviewed, and run on generator output; no
real pipeline or AMI numbers yet.** The package scores a pipeline hypothesis
against a meeting directory's ground truth, applies named CI gates and
aggregates across a corpus. Generator output is scored end to end by
`tests/test_v4_e2e.py` (generator → oracle / perturbed-oracle pipeline →
`evals.score_meeting` and `python -m evals score --suite oracle`), by
`tests/test_integration_emulator_serves_generator.py` and
`tests/test_integration_mockcloud_roundtrip.py`, and by the compose job
(`docker/job.sh` step 6, `python -m evals batch --suite $JOB_SUITE`). The
only system under test scored so far is the model-free energy-VAD + MFCC
clustering diarizer (`pipeline/energy_vad.py`, in the emulator integration
test, DER bounds only, no words). Every other hypothesis is an oracle, a
perturbed oracle or a hand-degraded reference. No ASR system has been scored.
AMI references now exist locally: `evals/ami.py` converts the NXT manual
annotations into contract meeting directories. Four test-split meetings
(EN2002a, ES2004a, IS1009a, TS3003a) are converted under the gitignored
`data/corpora/ami/meetings/`. Each scores exactly 0 against itself. Across
the release, the converter's speaker turns equal the BUT `only_words`
diarization reference line for line in 168 of the 170 meetings BUT covers.
All dev and test meetings are among the 168. The other 2 differ by 0.07 s
and 0.05 s (see [AMI](#ami-the-v5-reference)). No system has been scored on
them yet. A review
on 2026-09-25 found 11
defects (EV-1 … EV-11). All are fixed, each with a regression test; see
[Review fixes](#review-fixes-2026-09-25). Update 2026-09-25: the first real
pipeline numbers (whisper-sherpa and energy-vad-cluster on the four local AMI
test-split meetings and the Piper set) and the calibrated regression suites are in
[docs/v5-results.md](v5-results.md).

| item | where |
|---|---|
| contract I/O + normalisation | `evals/io.py` |
| metrics + report object | `evals/metrics.py` |
| batch aggregation | `evals/aggregate.py` |
| gates | `evals/gates.py`, `evals/gates.yaml` |
| CLI | `evals/cli.py` (`python -m evals`) |
| AMI → contract converter | `evals/ami.py` (`python -m evals.ami`), `tests/test_ami_converter.py`, `tests/fixtures/ami_tiny/` |
| tests (198 on 2026-09-25, parametrised cases counted individually) | `tests/test_evals_io.py`, `tests/test_evals_metrics.py`, `tests/test_evals_gates.py`, `tests/test_evals_cli.py`, `tests/test_evals_docs.py` |
| CI | `.github/workflows/evals.yml` (evals tests on `requirements/evals.txt` alone, selfcheck, suite listing; its header records a green GitHub Actions run for commit 70249ba, not re-checked here) and the full suite in `tests.yml` |
| dependencies | `requirements/evals.txt` |

## Evidence classes

This layer encodes **no device, SDK or cloud behaviour** and cites no
`build/evidence` or `reference/` line: nothing in it is a reconstruction. Two
classes of fact appear:

* **Library-defined** — every metric number is produced by `pyannote.metrics
  4.1`, `jiwer 4.0.0` or `meeteval 0.4.3` (the exact call is named in each
  function's docstring), or is a decomposition of one of those numbers that
  the tests prove sums back to the library's figure.
* **HARNESS_POLICY** — everything the harness chooses. Listed exhaustively
  [below](#harness_policy-items) and labelled in the code where defined.

## The data contract

Restated from the shared contract (Layer 2 → Layer 3 → pipeline). `evals/io.py`
is the only parser and writer; every violation raises `ContractError` with the
file, the element (`meeting.json.segments[2].words[1]`) and the rule.

```
<meeting dir>/meeting.json   schema "plaud-harness/meeting/1"
                              meeting_id, sample_rate, duration_s, channels,
                              speakers[{id, voice, position_m}],
                              segments[{speaker, start, end, text, words[{w,start,end}]}]  sorted by start
                              audio{mix_wav, stems{}, device{}}, generator{name, version, seed, scenario}
<meeting dir>/ref.rttm       SPEAKER <meeting_id> 1 <start> <dur> <NA> <NA> <speaker> <NA> <NA>
<meeting dir>/ref.stm        <meeting_id> 1 <speaker> <start> <end> <text>
hyp.json                     schema "plaud-harness/hypothesis/1"; meeting_id, system,
                              segments[{speaker, start, end, text, words?}]
hyp.rttm / hyp.stm           same formats, written by write_hypothesis_files()
```

Validation that goes beyond types: every number finite (`json.loads`
accepts `NaN`/`Infinity`; they are refused with the element named), segments
sorted by start (reference only), `end > start`, reference segments inside
`[0, duration_s]`, speakers declared, `words` sorted and inside their segment,
and **`words` must spell `text`** token for token. A generator whose word
timings drift from its transcript is rejected here rather than silently
scored. Files that are not UTF-8 or cannot be read are `ContractError`s too.

`meeting_id` (both files) and reference speaker ids must each be one RTTM/STM
field: no whitespace, no leading `;;` (the comment marker, and the meeting id
opens every STM line), not `<NA>`. Hypothesis segments may be unsorted.
Hypothesis speaker labels are opaque, non-blank strings and **may** contain
whitespace; the mock cloud's documented labels are `Speaker 1`, `Speaker 2`
(`mockcloud/oracle.py`). Scoring reads `hyp.json`, where labels are kept
verbatim.

Times are written with six decimals, trailing zeros trimmed. The RTTM/STM
writers map any speaker label that is not one field to one
(`io.rttm_speaker_fields`, HARNESS_POLICY). Whitespace runs become `_`,
`<NA>` becomes `_NA_`, a leading `;;` gets a `_` prefix, and `#2`, `#3`, …
is appended on collision, so distinct speakers stay distinct. `hyp.rttm` and
`hyp.stm` always agree, and STM text is written whitespace-collapsed on one
line. RTTM/STM written by this module, for references and hypotheses, parse
identically under meeteval's own `STM`/`RTTM` readers (tested), so the files
are standard, not a private dialect.

### Text normalisation

`Normalizer` is applied to reference and hypothesis through the same object,
so a punctuation- or case-only difference can never register as an error.
Order: lowercase → contractions (opt-in) → numbers-to-words (opt-in) →
punctuation → whitespace. Hyphens, dashes, slashes and underscores become
spaces (`well-known` → `well known`); apostrophes between word characters
survive (`o'clock`); everything else non-alphanumeric is removed. The
contraction table is deliberately small and leaves `'s` alone (ambiguous).
Numbers cover integers with optional thousands separators and decimals
(`1,234.5` → `one thousand two hundred thirty four point five`). When a
timed word normalises to several tokens its interval is split equally.

## Metric definitions

All rates are reported per meeting in `MeetingReport` and as dotted paths in
`MeetingReport.flat()` (the vocabulary gates and aggregation use).

### DER — `pyannote.metrics.diarization.DiarizationErrorRate`

```
DER = (miss + false_alarm + confusion) / total
```
`total` is reference speech time inside the scoring region after collar
extrusion; `confusion` is time attributed to the wrong speaker under the
optimal (Hungarian) hypothesis→reference label mapping. `der` is `None` when
`total` is 0 (no scorable reference speech); a gate then fails closed.
Overlapping speech of *different* speakers is counted per track (two speakers
talking for 2 s is 4 s of `total`).

* **Speaker activity (HARNESS_POLICY):** pyannote counts tracks, not speakers,
  so two overlapping segments with the same label would count that speaker
  twice. On the hypothesis side that is false alarm; on the reference side it
  doubles `total`. `metrics.to_annotation` therefore merges strictly
  overlapping segments of one label before scoring. Touching segments stay
  apart, so a reference turn boundary keeps its collar. This is also the
  per-speaker union that JER already used, so DER and JER read the same
  activity. ASR chunk overlaps are the typical source.
* **Mapping:** `der.mapping` is the mapping pyannote scored with. The harness
  repeats pyannote's own steps: uemify, rename the reference `A, B, …` and
  the hypothesis `0, 1, …`, then Hungarian
  (`pyannote/metrics/diarization.py:161-173`). Renaming reorders the
  co-occurrence matrix once there are more than 26 reference or 10 hypothesis
  labels, and that order decides ties. The test captures the mapping from
  inside pyannote and compares.
* **Scoring region (UEM):** `[0, duration_s]` from meeting.json. Hypothesis
  speech after the end of the audio is not scored. Without a duration the
  union of both extents is used explicitly. pyannote would fall back to the
  same UEM with a warning (`pyannote/metrics/utils.py:200-202`).
* **Collar:** `der_collar` is **pyannote's semantics — the total width of the
  no-score zone centred on every reference boundary.** The default `0.25`
  ignores ±0.125 s. NIST md-eval's `-c 0.25` (±0.25 s) is `--der-collar 0.5`
  here. The test `test_der_collar_is_total_width_centred_on_reference_boundaries`
  pins this: with collar 0.5 the closed-form case's `total` drops from 20 to
  19 s exactly. A collar must be finite and ≥ 0. pyannote would silently
  treat a negative collar as 0 (`if collar > 0.`,
  `pyannote/metrics/utils.py:74`) while the report recorded the negative
  value, so the CLI and `score_meeting` refuse one.
* **Overlap:** scored by default. `--skip-overlap` removes reference overlap
  regions from the UEM (pyannote's `skip_overlap`). In that case `der.overlap`
  is `null`, because the overlap regions are not scored at all. Otherwise
  `der.overlap.*` reports the DER components restricted **to** the overlap
  regions (UEM = overlap ∩ scoring region, collar still applied), **under the
  meeting's global mapping**. They are computed with pyannote's
  `IdentificationErrorRate` on the mapped labels, so every second in the
  overlap block is counted the same way in the global DER and in the
  per-speaker breakdown. The block is a restriction of the global components
  (tested). A pipeline's behaviour under crosstalk is therefore visible even
  when the headline DER hides it. (Before 2026-09-25 the mapping was
  re-optimised on the overlap alone, which could report 0 where the global
  score charged confusion.)
* **Per-speaker breakdown:** `der.per_speaker[<ref speaker>]` gives total /
  correct / miss / confusion / false-alarm seconds and the mapped hypothesis
  label. Unmapped hypothesis speakers appear as `hyp:<label>` carrying their
  false alarm (`hyp:<label>#2` in the unlikely case a reference speaker is
  literally named `hyp:<label>`). Reference and hypothesis labels are
  compared in disjoint namespaces, as pyannote does. An unmapped hypothesis
  `spk0` is therefore never scored as the reference `spk0`, which matters
  because the generator and `pipeline/energy_vad.py` both emit `spk<N>`. The
  split is exact: the sums over all keys equal pyannote's global components,
  including in overlap regions. There, a sub-segment's miss and confusion are
  shared equally among its unmatched reference speakers, and its false alarm
  among its unmatched hypothesis speakers. The libraries define no such split;
  this rule is HARNESS_POLICY and is tested.

### JER — `pyannote.metrics.diarization.JaccardErrorRate`

Per reference speaker, `1 − |R ∩ H| / |R ∪ H|` against its optimally mapped
hypothesis speaker (1.0 when none); JER is the unweighted mean, so a
20-second speaker weighs as much as a 20-minute one. Same collar/UEM/overlap
conventions as DER. `jer.per_speaker` mirrors pyannote's loop with pyannote's
own mapping, and the tests assert that its mean equals the library's figure,
including on a tie where the candidates' JER differs. When no reference
speaker is left after the UEM, collar and overlap removal, `jer` is `None`
and `speaker_count` is 0. pyannote itself would divide by zero there
(`pyannote/metrics/diarization.py:459`), which used to abort the whole batch.

### WER — `jiwer.process_words`

Reported twice, and neither is the headline:

* `wer_literal` pairs words by **speaker-label identity**: reference `spk0`
  against hypothesis `spk0`. This is what "plain WER" means in a per-speaker
  pipeline and it is the metric that lies: swap two labels and a perfect
  transcript scores 133 % (`test_speaker_swap_makes_literal_wer_lie_but_leaves_cpwer_unchanged`),
  use a diarizer's own labels and it scores 200 %.
* `wer_concat` is speaker-agnostic: all reference words in segment-start
  order against all hypothesis words in segment-start order. It cannot see
  attribution errors at all, and interleaves overlapping segments by start
  time.

Both carry S/D/I/hit counts and `wer_literal` a per-label table.

### cpWER — `meeteval.wer.wer.cp.cp_word_error_rate` (**headline**)

Concatenated minimum-permutation WER: each speaker's words are concatenated,
the reference↔hypothesis speaker assignment that minimises the pooled error
is found, and errors are divided by reference word count. Labels are opaque.
Missing and surplus speakers are counted (`missed_speaker`, `falarm_speaker`)
and their words become deletions/insertions.

**Why cpWER and not WER** (decision log 2026-09-21): a meeting transcript is
a set of *attributed* utterances. Plain WER either ignores attribution
(concatenated) or trusts label strings (literal); the first hides the errors
that matter for a meeting recorder and the second punishes a correct system
for naming its speakers differently. cpWER scores attribution and
transcription together under the best label matching, which is the only fair
reading of an unlabelled diarization output. The swap test asserts the
rationale directly: same words, labels swapped → `wer_literal` 1.33, cpWER 0.

**More than 20 speakers (since 28 Sep 2026).** meeteval 0.4.3 refuses cpWER
and tcpWER when either side has more than 20 speakers: its
`_minimum_permutation_word_error_rate` raises `RuntimeError("Are you
sure?…")`. It is a sanity check, not a computational limit. Until 28 Sep
`evals batch` recorded that as an error and dropped the whole meeting, DER
included, so no un-hinted AMI meeting could be scored (V5, 25 Sep). Now
`cp_wer`/`tcp_wer` return a result with every count `None` and `refused`
set to the reason (`meeteval refuses more than 20 speakers (reference 4,
hypothesis 95)`), and the meeting's DER, JER and WER are reported as usual
(HARNESS_POLICY; `evals.metrics.MEETEVAL_SPEAKER_LIMIT`). The macro and
micro aggregates leave a refused meeting out of the cpWER/tcpWER pools, say
how many meetings each value covers (`counts`) and how many were refused
(`not_scored`). A gate check on a metric with a non-zero `not_scored`
fails closed ("not scored on k of n meetings (meeteval refused)"), so a
gate never passes on the scorable subset. An *undefined* value (DER of a
meeting with no reference speech) is still left out of the mean and
counted, as before. `scripts/run-v5.sh` still runs meeteval with its guard
lifted as a separately labelled, supplementary score.

### tcpWER — `meeteval.wer.wer.time_constrained.tcp_word_error_rate`

cpWER with a time constraint: a hypothesis word may only match a reference
word whose interval overlaps the hypothesis word's interval widened by
`tcp_collar` seconds on each side. It catches a transcript that is right but
in the wrong place (a word attributed to the right speaker minutes late).
Default collar 5 s: **HARNESS_POLICY**, the value meeteval's preprocess
warning recommends ("You may want to set the collar to 5 seconds",
`meeteval/wer/preprocess.py:398`). meeteval itself has no default. Its CLI
declares `--collar` required (`meeteval/wer/__main__.py:612-616`), and
`tcp_word_error_rate` takes `collar` as a required keyword
(`meeteval/wer/wer/time_constrained.py:665-669`). Word timings
are used as-is when both sides carry them on every scorable segment
(`pseudo_word_level_timing='none'`); otherwise meeteval's pseudo timings apply
(reference `character_based`, hypothesis `character_based_points`) and the
report says so (`tcpwer.word_level_timing`). The shift test pins the
behaviour: +10 s with a 5 s collar scores exactly 2.0 (9 deletions + 9
insertions over 9 words) while cpWER stays at 0; a 20 s collar restores 0.

### Speaker-count error

`hypothesis speakers − active reference speakers` (signed) and its absolute
value. "Active" means declared speakers with at least one segment; the
declared count is reported alongside.

## Gates

`evals/gates.yaml` (schema `plaud-harness/gates/1`) holds named suites; each
gate is `{metric: <dotted path>, max|min|eq: <number>, tol?: <number>}`. A
check passes when the value satisfies every bound it declares; a suite passes
when every check passes. **Fail closed:** a metric that is absent, `None` or
non-finite fails its check. The gate file cannot open a gate either. Bounds
and `tol` must be finite: YAML `.nan` or `.inf` used to pass any value,
because `value > nan` is always false. A check with `min` > `max`, or with
`eq` outside its `min`/`max` even after `tol`, is refused on load as a
mistake in the file. The printed constraint
includes the tolerance on every bound it widens (`<= 0.1 (+0.05 tol)`). The
result names every failing metric with its value and bound, e.g.

```
[FAIL] suite 'synthetic-clean' on selfcheck-0001 — failed: der.der <= 0.05 (got 0.2045: exceeds max 0.05); cpwer.error_rate <= 0.1 (got 0.4: exceeds max 0.1)
```

| suite | gates | meaning |
|---|---|---|
| `oracle` | DER = 0, JER = 0, cpWER = 0, tcpWER = 0, concat WER = 0, speaker-count error = 0 | reference (or a lossless round-trip) scored against itself — the V4 rung |
| `synthetic-clean` | DER ≤ 0.05, cpWER ≤ 0.10, JER ≤ 0.10, speaker-count error = 0 | Layer 2 meetings, no noise |
| `synthetic-noisy` | DER ≤ 0.15, cpWER ≤ 0.25, speaker-count error ≤ 1 | Layer 2 meetings with reverberation and noise |
| `ami-headset` | DER ≤ 0.20, cpWER ≤ 0.30 | V5 placeholder, never exercised |

All thresholds are HARNESS_POLICY. The four suites above are uncalibrated placeholders.
Two regression suites calibrated on a measured run, `ami-subset-whisper-sherpa` and
`synthetic-piper-whisper-sherpa`, are documented in [docs/v5-results.md](v5-results.md).

Exit codes:

* `0`: scored, and the gates passed (or none were requested).
* `1`: a gate failed. Nothing else returns 1.
* `2`: input error. This covers a malformed, unreadable or non-UTF-8
  contract or gates file (including under `selfcheck`); an unknown suite,
  checked before anything else so a typo is never hidden behind `3`; a
  negative or non-finite collar; a missing hypothesis under `--missing fail`;
  and a batch where no meeting has a hypothesis, even under `--missing skip`.
  Any unexpected exception also returns 2, with its traceback on stderr,
  because a crash must never look like "gate failed". In `batch`, a meeting
  that raises is recorded under `errors` and the other meetings are still
  scored and reported.
* `3`: skipped. The reference root is absent or holds no `meeting.json`.

## CLI

```
python -m evals score --ref <meeting dir> --hyp <hyp.json> [--gates evals/gates.yaml --suite X]
                      --report out.json [--md out.md] [--der-collar 0.25] [--skip-overlap]
                      [--tcp-collar 5] [--expand-contractions] [--numbers-to-words]
python -m evals batch --refs <root> --hyps <root> [--suite X --gate-on macro|micro|both|each]
                      [--missing fail|skip] --report out.json [--md out.md]
python -m evals validate --ref <meeting dir> --hyp <hyp.json>
python -m evals selfcheck [--report out.json] [--gates file] [--der-collar X] [--tcp-collar Y]
                                                   # built-in meeting vs itself, requires 'oracle'
python -m evals suites [--gates file]              # list suites and their checks
```

`batch` finds every `meeting.json` under `--refs` recursively and looks for
each meeting's hypothesis under `--hyps` in this order:
`<relative meeting dir>/hyp.json`, `<meeting dir name>/hyp.json`,
`<meeting_id>/hyp.json`, `<meeting_id>.hyp.json`, `<meeting_id>.json`.
Reports carry both **macro** (mean of per-meeting rates, every meeting counts
once) and **micro** (pooled numerators over pooled denominators, so a long
meeting weighs more) aggregates; `--gate-on` picks which one the suite is
applied to (default macro). A meeting whose rate is undefined (`None`, e.g.
DER on a meeting with no reference speech) is left out of that metric's macro
mean. `macro.counts` shows how many meetings went in, and the meeting's false
alarm still enters the micro numerator. Use `--gate-on each` to make such a
meeting fail its gate.

The JSON report (`plaud-harness/eval-report/1`) records the library versions,
every setting (collars, UEM, normaliser flags) and, for batches, the missing
and errored meetings — a number without its conventions is not reproducible.

### How the pipeline output plugs in

The pipeline (Layer 3 consumer, built separately) writes one directory per
meeting with `hyp.json` (+ `hyp.rttm`, `hyp.stm` via
`evals.io.write_hypothesis_files`) whose `meeting_id` equals the reference's.
Then:

```
python -m evals batch --refs data/generated/meetings --hyps out/<system> \
    --suite synthetic-clean --report out/<system>/report.json --md out/<system>/report.md
```

The generator's V4 rung is the `oracle` suite applied to
`Hypothesis(meeting_id, "oracle", meeting.segments)`; `python -m evals
selfcheck` proves the harness side of that rung today with a hand-built
meeting round-tripped through disk.

## AMI: the V5 reference

`evals/ami.py` converts the AMI Meeting Corpus manual annotations (NITE XML
Toolkit format) and a Mix-Headset WAV into the contract above. It lives in the
package rather than under `scripts/` because this file makes `evals/io.py` the
only writer of the contract. The converter builds `evals.io.Meeting` objects
and writes them with `write_meeting` and `write_reference_files`. Its own
imports are the standard library and `evals.io`, and the tests import it
directly.

**Licence.** The AMI annotations and audio are CC BY 4.0 (the `LICENCE.txt`
shipped in the annotations zip). Attribution: AMI Consortium; J. Carletta et
al., "The AMI meeting corpus: a pre-announcement", MLMI 2006. The BUT
AMI-diarization-setup repository, whose RTTMs the converter is checked
against, is Apache-2.0; its RTTMs derive from the same CC BY 4.0
annotations. None of it is committed. `data/corpora/` is gitignored, and the
test fixture `tests/fixtures/ami_tiny/` is hand-written (its README.md says
so, and the tests derive every expected value from it on paper).

### Running it

```
# inputs (local-only): data/corpora/ami/raw/ami_public_manual_1.6.2.zip (sha256 b56e5bab…)
#                      data/corpora/ami/raw/audio/<ID>.Mix-Headset.wav
python -m evals.ami extract      # words/, corpusResources/meetings.xml, README, LICENCE, MANIFEST
                                 #   -> data/corpora/ami/raw/annotations/ (+ EXTRACTED_FROM.json: zip name, sha256)
python -m evals.ami convert      # every <ID>.Mix-Headset.wav present; --meeting ID (repeatable), --truncated keep|drop,
                                 #   --link hardlink|copy, --checksums FILE -> data/corpora/ami/meetings/<ID>/
python -m evals.ami stats [--json] [--no-score]
                                 # re-validates each directory, re-hashes mix.wav against meeting.json, prints
                                 #   duration, speakers, words, overlap, RTTM totals and the self-score
python -m evals.ami crosscheck [--rttms DIR]
                                 # compares every meeting's turns with <ID>.rttm under DIR (default: the BUT setup
                                 #   below); exit 0 all identical, 1 some differ, 3 no RTTM found
```

The defaults resolve against the repository root. Each meeting directory
holds `meeting.json`, `ref.rttm`, `ref.stm` and `mix.wav`. A rewrite replaces
those four files and refuses a directory holding anything else (the
generator's rule). Converting the four meetings takes 1.4 s, including a
sha256 of each WAV.

`mix.wav` is a hard link to the Mix-Headset WAV (no extra disk), or a copy
when the filesystem refuses a link. It is never a symlink:
`pipeline.meeting.resolve_audio` refuses an audio path that resolves outside
the meeting directory. **A hard link shares the raw corpus file's inode**, so
anything written to `mix.wav` in place would rewrite
`data/corpora/ami/raw/audio/<ID>.Mix-Headset.wav` too (and every other link
to it; the tests' temporary conversions add more). Three guards, all
HARNESS_POLICY:

* The converter removes the write bits from `mix.wav`. For a hard link that
  is the shared inode, so the raw WAV becomes read-only as well, and an
  in-place writer such as `soundfile.write` (used by `pipeline import-stm`)
  fails with `PermissionError` instead of rewriting the corpus.
  `check_meeting_dir` refuses a writable `mix.wav`.
* `convert` hashes the source WAV and refuses it if a checksum file lists it
  with another digest. By default the file is
  `data/corpora/ami/raw/SHA256SUMS.local` when present. meeting.json records
  the digest and `sha256_verified_by`.
* `stats` re-hashes every `mix.wav` against the digest in meeting.json.

The BUT only_words RTTMs are fetched into `data/corpora/ami/but_setup/` with
the upstream layout, at a pinned commit:

```
C=2509d8933721023fab4def2618aabd5c28eb82e9
B=https://raw.githubusercontent.com/BUTSpeechFIT/AMI-diarization-setup/$C
D=data/corpora/ami/but_setup
mkdir -p $D/lists
for f in README.md LICENSE; do curl -sSf -o $D/$f $B/$f; done
for s in train dev test; do
  mkdir -p $D/only_words/rttms/$s
  curl -sSf -o $D/lists/$s.meetings.txt $B/lists/$s.meetings.txt
  for m in $(cat $D/lists/$s.meetings.txt); do
    curl -sSf -o $D/only_words/rttms/$s/$m.rttm $B/only_words/rttms/$s/$m.rttm
  done
done
```

That makes 175 files (170 RTTMs), 5.0 MB, in 95 s here. The local copy was
assembled from fetches made on 2026-09-25. Each file's git blob SHA-1 equals
the blob in the commit's tree, and running the loop above into a scratch
directory gave byte-identical files. `PROVENANCE.json` beside them records
this.

### Conventions

Sources, read on 2026-09-25. Each file was checked by its git blob SHA
against the commit named.

* **BUT**: BUTSpeechFIT/AMI-diarization-setup at commit `2509d893`
  (`README.md` and `only_words/rttms/`). Its README says: "All words are
  considered as speech and included in the references." It also says:
  "Speaker turns respect precisely the annotations, but adjacent speech
  segments (words) of the same speaker are merged not to create false break
  points. Consecutive speech segments from the same speaker separated by
  pauses (silence) are not merged in any case." It prefers `only_words` to
  `word_and_vocalsounds`. pyannote/AMI-diarization-setup (head `67c2d539`)
  is a fork of it. For the four local meetings its `only_words` RTTMs are
  byte-identical to BUT's. No claim is made here about which reference
  pyannote's published AMI numbers use.
* **lhotse**: lhotse-speech/lhotse at commit `1f22c586` (2025-10-07, the
  last commit touching `lhotse/recipes/ami.py`): `parse_ami_annotations` and
  `lhotse/recipes/utils.py` `normalize_text_ami`. Apache-2.0.

Everything not cited is HARNESS_POLICY.

| question | answer | source |
|---|---|---|
| speaker id | the participant's `global_name` in `corpusResources/meetings.xml` (`MEO015`); every listed participant is declared, and `nxt_agent`, `channel` and `role` are kept beside the id | lhotse (`global_spk_id[...] = speaker.attrib["global_name"]`); the BUT RTTMs use the same labels |
| speech time (ref.rttm) | per speaker, the union of the intervals of **all** timed `<w>` elements, punctuation and `..` included. Intervals that overlap or touch (next start ≤ running end) merge into one turn. Any gap splits, however short (0.01 s splits) | BUT (quoted above) |
| running end of a turn | the max of the merged ends | HARNESS_POLICY. BUT's RTTMs follow a different rule, inferred below; the two differ in 2 of 170 meetings |
| zero-length turn (an isolated zero-length `<w>`) | dropped (497 in the release, nearly all punctuation) | HARNESS_POLICY; the BUT RTTMs have none (shortest line 0.03 s) |
| contract segments | one per speech turn, so one speaker's segments never overlap or touch (the converter asserts it). Different speakers' turns overlap as annotated | HARNESS_POLICY |
| NXT segments layer | not read (see below for why) | – |
| vocal sounds, non-vocal sounds, disfluency markers, gaps, transform errors | neither speech time nor text | BUT `only_words`; lhotse keeps only `<w>` |
| punctuation (`punc="true"`) | its time is speech (most are zero-length; some are not, e.g. EN2006a, IN1007); never text | time: BUT ("all words"); text: lhotse (its Kaldi normaliser removes it) |
| `..` and other `<w>` whose text normalises to nothing (11 in the release) | speech time, no text. A turn holding only such words keeps its time with empty text: 7 turns in 5 meetings, e.g. ES2006a FEO023 0–27.712 s | BUT |
| scorable words | `<w>` without `punc="true"` whose text normalises to at least one token, each in the turn that contains its interval, in document order | HARNESS_POLICY |
| a scorable word with no speech time | dropped from the text and counted: untimed (2 in the release, ES2008a.A.words1315-1316) or an isolated zero-length word (2, TS3003d.A.words0-1 "Good afternoon" at 0.0). A word starting at or after the audio end is dropped too (none in the four meetings) | HARNESS_POLICY; BUT has no time for them either |
| truncated words (`trunc="true"`) | kept as written (`th`); `--truncated drop` removes them from the text only, and their time stays speech | lhotse keeps `word.text`; the option is HARNESS_POLICY |
| text | each run of characters other than word characters and `'` becomes a space, then `evals.io.DEFAULT_NORMALIZER` runs: `T_V_s` → `t v s`, `Mm-hmm` → `mm hmm`, `now.I'm` → `now i'm`, `'Kay` → `kay` | the split is Kaldi-style (lhotse `re.sub(r"[^A-Z0-9']+", " ", text)`); the normaliser is this layer's |
| `Mm-hmm`, `Uh-huh`, `O K` | two tokens each (`mm hmm`, `uh huh`), like every hyphenated word, because the scorer splits the hypothesis the same way. The Kaldi normaliser keeps `MM-HMM`/`UH-HUH` as one token, so AMI word counts here are higher than Kaldi-style counts (4608 `Mm-hmm`/`mm-hmm` and 380 `Uh-huh`/`uh-huh` in the release). Kaldi's `O K` → `OK` rule acts on 3 tokens in the release, all `O_K_A_Y_` (EN2004a): Kaldi gives `OK A Y`, this converter `o k a y`. The release otherwise spells the word `Okay`/`okay` (10481) or `Ok`/`ok` (55, one token either way) | HARNESS_POLICY |
| a word that normalises to several tokens | interval split equally (the `io.normalized_words` rule) | HARNESS_POLICY |
| a word starting before the previous word of its turn | raised to that start (the contract wants a segment's words sorted); none in the release | HARNESS_POLICY |
| timing repairs | end < start → end = start (11 `<w>` in the release, in 9 meetings); a `<w>` starting at or after the audio end is dropped and an end past it clipped. Each repair is counted in `generator.scenario.counts` | HARNESS_POLICY |
| times | rounded to 6 decimals (the RTTM/STM write precision) | HARNESS_POLICY |
| `duration_s`, `sample_rate`, `channels` | from the WAV header (frames / rate); meetings.xml's `duration` attribute is recorded as `meetings_xml_duration_s` but not used (ES2004a: 1186 against 1049.3546875 s of audio; IS1009a: 831 against 838.8333125 s) | HARNESS_POLICY |
| `generator` block | `name: evals.ami`, `version: 2`, `timing_exact: false` (forced alignment), `scenario` = source, licence, attribution, release line, extraction manifest, policy, conventions, citations, counts | HARNESS_POLICY |

The stored text is a fixed point of the evals normaliser (the converter
asserts it). Scoring with or without `--expand-contractions` or
`--numbers-to-words` therefore normalises reference and hypothesis alike;
the corpus has no digits.

### Why the turns come from word times, not NXT segments

Converter version 1 made one contract segment per NXT `<segment>`, from its
first word's start to its last word's end. A review on 2026-09-25 showed
that this counts any vocal sound or silence between two words of one segment
as speech, the opposite of BUT's rule. Version 1 matched the BUT RTTMs line for
line on the four local meetings, but not across the release: 28 of the 170
meetings BUT covers differed. Two of them are test-split meetings. In
EN2002b, BUT splits MEE073's segment 850.39–882.31 at a `<vocalsound>`
868.02–869.74. In EN2002c, BUT splits a segment at a `<vocalsound>`
1248.37–1250.26. So the claim that the other 12 test meetings would convert
to the BUT reference was wrong for version 1. Version 1 also dropped `..`
words with a duration and ignored punctuation with a duration. It also left
overlapping same-speaker NXT segments unmerged (IB4004, EN2005a).

Version 2 builds the turns from word times. `python -m evals.ami crosscheck`
against all 170 BUT `only_words` RTTMs gives this:

* **168 meetings are identical**: same speaker, start and end on every
  line, at millisecond resolution. This includes all 18 dev and all 16 test
  meetings.
* **2 differ, both in the train split.**
  * EN2006a: this converter has one extra turn, FEE088 2162.240–2162.310
    ("What" [2162.24, 2162.31]). The next word, "do", runs from 2162.31 to
    2160.39: it is the release's one negative-length scorable word.
  * TS3009c: MTD036ME has 5 turns (for example 877.690–880.430) that BUT
    splits in two at a 0.01 s step (879.52 / 879.53). There "right"
    [878.02, 879.53] is followed by "." [879.52, 879.52]. The release notes
    say: "Timings are known to be incomplete / incorrect for meetings TS3009c
    (channel 3 only); EN2002a,c; EN2003a." MTD036ME is channel 3.

  DER with BUT as reference and this converter as hypothesis, collar 0, is
  2.5057892681400636e-05 on EN2006a (0.07 s false alarm of 2793.531 s) and
  1.9406020523746016e-05 on TS3009c (0.05 s of 2576.52 s).

The cause, inferred and then tested: BUT's RTTMs are reproduced **exactly,
all 170**, by the rule "in document order, merge a word when its start ≤ the
running end, and set the running end to *that word's* end". Turns whose
duration is not positive are then removed. That rule lets a word inside the
previous one shorten the turn. This converter takes the max instead, so a
turn contains every word interval in it, as the contract requires of a
segment's words. `tests/test_ami_converter.py` pins both the two differences
and the exact reproduction.

### Measured on the four local meetings (2026-09-25)

Inputs: `ami_public_manual_1.6.2.zip` (sha256
`b56e5babb2496b8795deeeda7e71178d7fbc9963f94276cf2a3f4b56ebbc9f9d`; its
`00README_MANUAL.txt` says "release 1.7") and the four Mix-Headset WAVs in
`data/corpora/ami/raw/SHA256SUMS.local` (16 kHz, mono, PCM16; `convert`
verified all four against it). All four are in the Full-corpus-ASR **test**
split (the BUT setup's `lists/test.meetings.txt`, 16 meetings). Converter
version 2, `--truncated keep`. Version 1 gave the same turns and words for
these four.

| meeting | duration_s (WAV) | speakers declared/active | segments (= turns) | words | speaker time s (= RTTM total) | speech union s | overlap s | overlap fraction | speech / duration |
|---|---|---|---|---|---|---|---|---|---|
| EN2002a | 2142.709375 | 4/4 | 746 | 7632 | 2530.260 | 1894.900 | 519.580 | 0.2741991 | 0.8843 |
| ES2004a | 1049.3546875 | 4/4 | 260 | 2653 | 923.430 | 787.340 | 124.320 | 0.1578987 | 0.7503 |
| IS1009a | 838.8333125 | 4/4 | 195 | 2018 | 695.900 | 604.920 | 82.100 | 0.1357204 | 0.7211 |
| TS3003a | 1505.642625 | 4/4 | 242 | 2534 | 1025.964 | 978.100 | 44.756 | 0.0457581 | 0.6496 |

Overlap fraction = time with two or more speakers active / time with at least
one (the generator's definition). Fractions are truncated to 7 decimals, never
rounded up. Overlap is preserved as annotated: turns of different speakers
are never trimmed.

| meeting | `<w>` elements | punctuation (not text) | vocal sounds | disfluency markers | gaps | truncated kept | zero-length turns dropped | turns without text | words split into several tokens | words with `--truncated drop` |
|---|---|---|---|---|---|---|---|---|---|---|
| EN2002a | 9308 | 1792 | 401 | 294 | 54 | 102 | 2 | 0 | 86 | 7530 |
| ES2004a | 3135 | 521 | 60 | 57 | 6 | 36 | 0 | 0 | 36 | 2617 |
| IS1009a | 2393 | 407 | 70 | 55 | 0 | 31 | 0 | 0 | 32 | 1987 |
| TS3003a | 3014 | 496 | 124 | 39 | 13 | 17 | 1 | 0 | 13 | 2517 |

`--truncated drop` leaves the turns unchanged (746/260/195/242). No timing
repair fired on any of the four: no untimed, negative-length, out-of-order or
past-the-audio word, and no scorable word without speech time. The release
notes' known issue for EN2002a is quoted in full above. Release 1.7's only
change was more complete timings for the non-scenario meetings. None of the
four meetings' words lacks a time in this zip.

**Validation.**

* **Loaders.** `evals.io.load_meeting`, `load_rttm` and `load_stm` accept
  every directory. `ref.rttm` and `ref.stm` agree with meeting.json segment
  for segment (`evals.ami.check_meeting_dir`). `pipeline.meeting.read_meeting`
  and `resolve_audio(..., "mix")` accept them too.
* **Self-score, from meeting.json.** Every meeting scored against its own
  segments gives DER 0.0, JER 0.0, cpWER 0.0, tcpWER 0.0 and speaker-count
  error 0, all exactly.
* **Self-score, from the files.** With `ref.rttm` + `ref.stm` parsed back as
  the hypothesis, DER, JER and cpWER are 0.0 on all four meetings. tcpWER is
  0.0 on ES2004a, IS1009a and TS3003a. On EN2002a it is
  0.0007861635220125787: 6 errors (2 S, 2 D, 2 I) in 7632 words. The STM
  carries no word times, so meeteval places the hypothesis words by character
  position (`character_based_points`). Six words land more than the 5 s
  collar away from their aligned time; EN2002a has segments up to 36.6 s
  long. Which segments hold the six was not traced. This is a property of
  scoring an STM-only hypothesis, not a conversion error.
* **End to end.** `python -m pipeline batch --pipeline oracle --root
  data/corpora/ami/meetings` wrote 4 hypotheses. Then `python -m evals batch
  --refs data/corpora/ami/meetings --hyps <those> --suite oracle --gate-on
  each` exited 0, with all 6 checks passing on each meeting. (`--root
  data/corpora/ami` fails: the meetings are one level down.)
* **Against the published diarization reference.** See the crosscheck
  above: the four meetings are among the 168 identical ones. Against BUT's
  `word_and_vocalsounds` RTTMs (fetched for these four only, to scratch) at
  collar 0, DER was measured with version 1, whose turns are identical to
  version 2's for these four meetings. EN2002a 0.03884527416875363 (102.261 s
  miss of 2632.521 s), ES2004a 0.031026298061486095 (29.568 of 952.998),
  IS1009a 0.03663793700146481 (26.466 of 722.366) and TS3003a
  0.08581846176876322 (96.312 of 1122.276). All of it is miss: the
  vocal-sound time left out on purpose. The BUT UEMs span [0, WAV duration]
  and agree with `duration_s` within 1e-6 s. So DER from these directories is
  scored against the same reference turns as BUT's `only_words` setup, over
  the whole recording with overlap included. The collar is not part of the
  reference. The default here is `der_collar` 0.25 (±0.125 s). To compare
  with a published number, score at the collar its source states
  (`--der-collar 0` for none). pyannote's README benchmark table (commit
  `b749285c`) does not state its collar.
* **Whole release.** All 171 meetings in `meetings.xml` build and pass
  `evals.io.meeting_from_dict`, using a 4-hour stand-in duration because
  their audio is not downloaded: 83502 segments and 992675 words (sha256 of
  all RTTM lines `fab4d7bf…`, pinned in the tests). Across the release:
  1147783 `<w>` elements, 169569 of them punctuation. 27073 vocal sounds,
  27395 disfluency markers, 5125 gaps and 30 transform errors were dropped.
  19237 truncated words were kept. 11 `<w>` texts normalise to nothing
  (`..`, `...`). 4 `<w>` are untimed (2 of them scorable). 11
  negative-length `<w>` were clamped. 4 scorable words have no speech time.
  11450 words split into several tokens. (Version 1's "19238 truncated words
  kept" counted one truncated `..`, IN1007.B.words1831, as both kept and
  dropped; that double count is fixed.) The release build takes 9 s.

### Tests

`tests/test_ami_converter.py`. The tiny-fixture tests always run, CI
included. They cover the readers, the text rule, the exact hand-derived
meeting (turns, words, counts, speakers), the turn rules (touching words
merge, any gap splits, punctuation and `..` with a duration are speech,
overlapping words make one turn), the truncated-word count, the unread
segments layer, the RTTM/STM lines, overlap statistics, the self-score
(0 by both paths), the oracle suite through `python -m evals batch`, the
pipeline's reader, `--truncated drop`, read-only hard-link and copy
placement, checksum verification, tamper detection, the rewrite rule,
broken annotations named with their file, non-WAV audio, the crosscheck (its
RTTM reader, identical, differing and missing), extraction (only the needed
members; a `..` member refused) and exit 2 on an unexpected error. The
real-data tests run when the local files exist. They cover the four pinned
meetings (checksum-verified), the local directories being current, the
whole-release digest, the BUT crosscheck (exactly the two pinned
differences) and the exact reproduction of all 170 BUT RTTMs by BUT's
inferred rule. Otherwise they skip with the reason prefix `AMI corpus not
present: `. That prefix is the `ami-corpus` gate in `tests/conftest.py`, a
local-only input never provided in CI.

### Not done

* Only 4 of the 16 test-split meetings, and no dev or train meeting, have
  audio here. `convert` picks up every `<ID>.Mix-Headset.wav` present, and
  the crosscheck shows the other 12 test meetings' turns equal BUT's. Their
  words and self-scores have not been checked here, because that needs their
  audio.
* Other microphone conditions (SDM, MDM, individual headsets) are not wired;
  the contract's `mix.wav` is the headset mix.
* The full AMI test split has not been scored. A four-meeting subset was scored
  on 25 Sep ([`docs/v5-results.md`](v5-results.md)); the `ami-headset` gate is still
  an uncalibrated placeholder, and the new `ami-subset-*` gates are regression
  gates set from those measurements, not quality targets.
* The overlap fraction and speech statistics come from the forced-alignment
  word times. They inherit that alignment's errors, and no manual check
  against the audio was made.
* The BUT rule is inferred from its output. Its generation script was not
  available, so the exact reproduction of all 170 files is the evidence.

## HARNESS_POLICY items

| policy | value | where |
|---|---|---|
| DER/JER collar default (pyannote total-width semantics) | 0.25 (±0.125 s) | `metrics.DEFAULT_DER_COLLAR` |
| tcpWER collar default | 5.0 s | `metrics.DEFAULT_TCP_COLLAR` |
| scoring region | `[0, duration_s]`; union of extents without a duration | `metrics.to_uem` |
| speaker activity for DER/JER | strictly overlapping same-label segments merged; touching ones kept | `metrics.to_annotation` |
| overlap-region DER | UEM = reference overlap ∩ scoring region, collar applied, global mapping; `null` under `--skip-overlap` | `metrics.diarization_error_rate` |
| per-speaker split in overlap regions | equal share among unmatched speakers | `metrics._speaker_breakdown` |
| no scorable reference speech | DER and JER are `None` (gate fails closed) | `metrics.diarization_error_rate`, `metrics.jaccard_error_rate` |
| collars | finite and ≥ 0, else refused | `metrics._check_collar`, `cli._collar` |
| word timings for tcpWER | used as-is when every scorable segment has them, else meeteval pseudo timings | `metrics.tcp_wer` |
| segments with empty normalised text | dropped from cpWER/tcpWER input | `metrics.to_seglst` |
| multi-token normalised word | interval split equally | `io.normalized_words` |
| hypothesis sortedness | not required; sorted when scored | `io.hypothesis_from_dict` |
| hypothesis labels | any non-blank string in hyp.json; mapped to one RTTM/STM field on write, injectively | `io.rttm_speaker_fields` |
| RTTM/STM field rules | meeting ids and reference speaker ids: no whitespace, no leading `;;`, not `<NA>` | `io.field_problem` |
| time formatting | six decimals, trailing zeros trimmed | `io.format_time` |
| contraction table, `'s` untouched; number range < 10¹² | `io.CONTRACTIONS`, `io.integer_to_words` |
| gate on missing/None/non-finite metric | fails | `gates.evaluate_check` |
| gate bounds | finite; contradictory checks refused | `gates.parse_check` |
| gate thresholds | all four suites | `evals/gates.yaml` |
| exit codes | 0/1/2/3; unexpected errors → 2 | `gates.EXIT_*`, `cli.main` |
| batch hypothesis lookup order | five candidates | `cli.find_hypothesis` |
| batch gate target default | macro | `cli` `--gate-on` |
| missing hypothesis | fails the batch unless `--missing skip`; no hypothesis at all always fails (2) | `cli` `--missing` |
| AMI layout | `data/corpora/ami/meetings/<ID>/meeting.json` | `cli.AMI_EXPECTED_LAYOUT` |
| AMI inputs and outputs | `data/corpora/ami/raw/{ami_public_manual_1.6.2.zip,annotations/,audio/<ID>.Mix-Headset.wav}` → `data/corpora/ami/meetings/<ID>/`; defaults resolved against the repository root | `ami.DEFAULT_*` |
| AMI extraction | only `words/`, `corpusResources/meetings.xml` and the release notes/licence/manifest; a `..` or absolute member refused | `ami.ANNOTATION_MEMBERS`, `ami.extract_annotations` |
| AMI turn/word rules not fixed by a cited source | max running end; zero-length turns dropped; one segment per turn; words placed by containment in document order; words without speech time dropped; no MM-HMM/UH-HUH/OK exceptions; equal split of multi-token words; timing repairs; 6-decimal rounding; duration from the WAV; `--truncated drop` option | [AMI conventions](#conventions) |
| AMI `mix.wav` | hard link, copy when linking fails, never a symlink; write bits removed (on the shared inode, so the raw WAV too); source checked against `SHA256SUMS.local` when listed; `stats` re-hashes; rewrite replaces own files and refuses foreign ones | `ami._place_audio`, `ami.convert_meeting`, `ami.verify_mix_audio`, `ami._prepare_dir` |
| AMI empty meeting | a meeting with no speech turn is refused (`AmiError`) | `ami.build_meeting` |
| AMI crosscheck | millisecond resolution, decimal sums; stand-in duration 4 h | `ami.rttm_turns`, `ami.crosscheck_rttms` |
| selfcheck meeting | hand-built 2-speaker, 10 s, word timings | `cli.selfcheck_meeting` |

## Tests

198 tests in five files on 2026-09-25 (parametrised cases counted
individually). All run on hand-built inputs with answers derived on paper;
the generator is not imported here. Generator output is scored by the
integration tests named in the status line.

* **Closed-form DER.** Reference A [0,10] B [10,20]; hypothesis x [0,9],
  y [10,18], x [18,20], y [25,25.5]. At collar 0: miss 1.0, false alarm 0.5,
  confusion 2.0, total 20, so DER 0.175. At collar 0.5: 3.0/19. At the
  default collar: exactly 3.25/19.5. The per-speaker breakdown is exact.
  Overlap case (A [0,10], B [8,12], only A detected): 4/14 scored, 0.2 with
  `skip_overlap`, 0.5 on the overlap region alone. Breakdown sums equal the
  global components under collar and overlap.
* **Label clashes.** The hypothesis reuses reference names (`spk0/spk1/spk2`
  against `spk0/spk1`, and 30 speakers past pyannote's `Z → AA` rollover): an
  unmapped clashing label is neither correct nor charged to its namesake. On
  a tie the reported mapping equals the one captured from inside pyannote,
  and per-speaker JER still averages to the library's JER.
* **Overlap under the global mapping.** A crosstalk-only cluster gives
  overlap confusion 2.0 s and overlap DER 0.5, where the old local re-mapping
  gave 0. The overlap block is a restriction of the global components.
* **Degenerate meetings.** No segments, or all speech inside the collar:
  DER and JER are `None`, the gate fails closed, and a batch with such a
  meeting still writes its report. The same holds for JER when
  `skip_overlap` removes every reference second.
* **Same-speaker overlap.** Two overlapping `s` segments give DER 0 (it used
  to be 0.1) and JER 0. Different speakers still count per track; the
  reference follows the same rule; touching segments keep their collar.
* **JER.** One speaker half-covered gives 0.5; closed form
  (0.25 + 2.5/10.5)/2; the per-speaker mean equals the library.
* **Speaker swap.** Literal WER 8/6 with S4 D2 I2; cpWER 0 before and after,
  with assignment {(A,B),(B,A)}; arbitrary labels give literal 2.0, cpWER 0.
* **tcpWER.** +0.3 s gives 0; +10 s at collar 5 gives exactly 2.0 (0/9/9);
  collar 20 gives 0; cpWER stays 0 throughout; the pseudo-timing fallback is
  flagged.
* **Loaders.** Malformed meeting.json, hypothesis, RTTM and STM cases, each
  asserting that the message names the rule and the location.
  `NaN`/`Infinity` in JSON; non-UTF-8 and unreadable files; meeting ids and
  labels that cannot be one field; comment and blank lines skipped;
  meeting_id cross-check.
* **Normalisation.** Default, contraction and number cases; identical
  treatment of both sides; timed-word splitting.
* **Round trips.** meeting.json → disk → equal dict; meeting → ref.rttm +
  ref.stm → load equals the original segments; hyp.json/rttm/stm, including
  `Speaker 1`-style, `<NA>` and `;;` labels and multi-line text; written
  files parse under meeteval's readers with the same values; float noise
  within 1e-6.
* **Gates.** Pass/fail naming only the failing metrics; fail-closed on
  missing/None/NaN/bool; eq/min/max/tol semantics; malformed configs,
  including non-finite bounds, contradictory bounds and non-UTF-8 files; an
  unknown suite lists the available ones.
* **CLI.** Every subcommand and exit code, including: selfcheck with a bad
  gates path; a non-UTF-8 hypothesis; a typo suite with an absent corpus; a
  batch with no hypotheses under `--missing skip`; negative, non-finite and
  non-numeric collars on every command; an injected crash exiting 2 with the
  batch report still written. Also macro vs micro numbers, all five lookup
  paths, the absent-corpus skip message, and `python -m evals selfcheck` in
  a subprocess.
* **Docs.** This file's tcpWER-default provenance and status line are
  checked against meeteval's signature and the repository
  (`tests/test_evals_docs.py`).

## What is not done

* **V5 is not met.** Only 4 of the 16 AMI test meetings were scored (25 Sep,
  [`docs/v5-results.md`](v5-results.md)), and the results are poor. The converter
  exists and four test-split meetings are converted locally (see
  [AMI](#ami-the-v5-reference)). The inputs stay local-only under the
  gitignored `data/corpora/ami/`, and CI never has them. The layout is:

  ```
  data/corpora/ami/meetings/<MEETING_ID>/meeting.json   # schema plaud-harness/meeting/1, turns + words from NXT words
  data/corpora/ami/meetings/<MEETING_ID>/ref.rttm
  data/corpora/ami/meetings/<MEETING_ID>/ref.stm
  data/corpora/ami/meetings/<MEETING_ID>/mix.wav        # 16 kHz mono headset mix (gitignored)
  ```
  Hypotheses go at `out/ami/<MEETING_ID>/hyp.json`, and the scoring command is
  ```
  python -m evals batch --refs data/corpora/ami/meetings --hyps out/ami \
      --gates evals/gates.yaml --suite ami-headset --report out/ami/report.json
  ```
  On a checkout without the converted corpus that command exits `3` and
  prints this layout. The `ami-headset` thresholds are placeholders. Only 4
  of the 16 test-split meetings have audio here.
* **Generator output is scored almost only through oracles.**
  `tests/test_v4_e2e.py`, `tests/test_integration_mockcloud_roundtrip.py` and
  `docker/job.sh` score generator meetings with an oracle, a perturbed oracle
  or the mock cloud's copy of the ground truth. The one exception is
  `tests/test_integration_emulator_serves_generator.py`. It scores the
  model-free `energy-vad-cluster` diarizer, and only with loose DER bounds
  (no words, so no WER). `docker/job.sh` ran under Docker once, on 25 Sep
  (`build/v6/compose-smoke-2026-09-25.log`).
* **One transcribing system has been scored**, `whisper-sherpa`, on the AMI
  subset and a synthetic Piper set ([`docs/v5-results.md`](v5-results.md)). Its
  numbers are poor, and the gates calibrated from them only catch regressions.
* **CI.** `.github/workflows/evals.yml` runs every `tests/test_evals_*.py`
  (the glob, so `tests/test_evals_docs.py` is included and a new evals test
  module needs no workflow edit) on `requirements/evals.txt` alone, and is
  also triggered by changes to `docs/evals.md` and `tests/conftest.py`. The
  full suite, evals tests included, runs in `tests.yml`.
* **Not included by design:** ORC-WER / MIMO-WER (meeteval offers them; the
  contract always has speaker labels so cpWER/tcpWER are the right pair),
  DER purity/coverage, confidence intervals across meetings, and any
  language-specific normalisation beyond the small English tables above.
* **Known conventions with consequences:**
  * Hypothesis speech after `duration_s` is never scored (UEM).
  * `wer_concat` interleaves overlapping segments by start time.
  * Per-speaker DER attribution inside overlap regions is the harness's
    equal-share rule, not a library definition.
  * Same-speaker overlap is merged before DER/JER. NIST md-eval's treatment
    of it has not been compared here.
  * A meeting with an undefined rate drops out of that metric's macro mean
    (see [CLI](#cli)).
  * `hyp.rttm`/`hyp.stm` carry field-safe speaker labels (`Speaker_1`), not
    necessarily the `hyp.json` labels. The mapping is deterministic
    (`io.rttm_speaker_fields`) but is not written to disk.
  * Duplicate keys in `gates.yaml` (for example, two suites with the same
    name) are not detected: PyYAML keeps the last one.

## Review fixes (2026-09-25)

Each fix has a test that fails on the code as committed in 70249ba and passes
now.

| id | defect | fix | test |
|---|---|---|---|
| EV-1 | per-speaker breakdown scored an unmapped hypothesis label as correct for the reference speaker of the same name | disjoint `R:`/`H:` namespaces | `test_breakdown_is_exact_when_hypothesis_labels_reuse_reference_names`, `…_charges_an_unmapped_clashing_label_its_own_false_alarm`, `…_attributes_clashing_labels_correctly_past_26_speakers` |
| EV-2 | no scorable reference speech → `ZeroDivisionError` inside pyannote's JER; batch aborted with exit 1, no report | JER `None`; batch records any per-meeting exception | `test_meeting_without_scorable_reference_speech_gives_undefined_rates_not_a_crash`, `test_jer_with_every_reference_second_removed_by_skip_overlap_is_undefined`, `test_batch_survives_a_meeting_without_reference_speech`, `test_score_on_a_meeting_without_reference_speech_fails_its_gate_closed` |
| EV-3 | overlap DER re-optimised the mapping on the overlap alone | `IdentificationErrorRate` under the global mapping; `null` under `--skip-overlap` documented | `test_overlap_der_is_scored_under_the_global_mapping` |
| EV-4 | overlapping same-label segments counted twice | merge in `to_annotation` | `test_overlapping_segments_of_one_speaker_count_once` |
| EV-5 | exit codes: selfcheck bad gates → 1; non-UTF-8 input → 1; typo suite → 3; no hypotheses under skip → 3; negative collars accepted | see [Exit codes](#gates) | the six `test_evals_cli.py` tests after "Review fixes", `test_negative_or_non_finite_collars_are_rejected`, `test_loaders_turn_unreadable_files_into_contract_errors` |
| EV-6 | `.nan`/`.inf` bounds or tol passed anything; tol hidden on max/min; min > max accepted | finite bounds, contradiction check, `describe()` | `test_gate_files_with_non_finite_or_contradictory_bounds_are_rejected`, `test_describe_shows_the_tolerance_on_every_bound_it_widens` |
| EV-7 | `Speaker 1`-style labels, whitespace meeting ids and multi-line text produced RTTM/STM the harness's own parsers rejected | meeting-id rule on hypotheses; injective label mapping and one-line text on write | `test_hypothesis_files_with_unwritable_labels_stay_parseable_and_distinct`, `test_written_hypothesis_rttm_and_stm_parse_with_meeteval_independently`, `test_hypothesis_meeting_id_follows_the_reference_rules` |
| EV-8 | `Infinity` passed validation; tcpWER became 2.0 | finite numbers required | `test_hypothesis_rejects_non_finite_times_from_json`, `test_meeting_rejects_non_finite_numbers_and_unwritable_fields` |
| EV-9 | tcpWER 5 s attributed to meeteval; pyannote's UEM fallback called "silent" | relabelled HARNESS_POLICY with citations | `test_tcp_collar_default_is_harness_policy_because_meeteval_has_none` |
| EV-10 | a test assertion that could not fail, with a wrong expected value | exact 3.25/19.5 | `test_score_meeting_rejects_a_hypothesis_for_another_meeting` |
| EV-11 | status said evals had never run on generator output | status and "not done" rewritten | `test_status_reflects_that_generator_output_is_scored` |

Found while fixing EV-1: the harness recomputed the Hungarian mapping on the
original labels, while pyannote computes it on relabelled ones. With 11 or
more hypothesis labels (or 27 or more reference labels) a tie could resolve
differently, and `der.mapping`, `mapped_to` and per-speaker JER then
described a mapping pyannote did not use. The totals were unaffected. Fixed
by `metrics._pyannote_mapping`; tested by
`test_reported_mapping_is_the_one_pyannote_scored_with_even_on_ties` and
`test_per_speaker_jer_uses_pyannotes_mapping_so_its_mean_is_the_library_jer`.
