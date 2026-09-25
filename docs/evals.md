# Layer 3 — evaluation harness (`evals/`)

Status 2026-09-23: **implemented and tested in isolation.** The package scores a
pipeline hypothesis against a meeting directory's ground truth, applies named
CI gates and aggregates across a corpus. It has not yet been run on generator
output (Layer 2 is being built concurrently) and cannot be run on AMI (no
corpus is checked out — see [What is not done](#what-is-not-done)).

| item | where |
|---|---|
| contract I/O + normalisation | `evals/io.py` |
| metrics + report object | `evals/metrics.py` |
| batch aggregation | `evals/aggregate.py` |
| gates | `evals/gates.py`, `evals/gates.yaml` |
| CLI | `evals/cli.py` (`python -m evals`) |
| tests (137, parametrised cases counted individually) | `tests/test_evals_io.py`, `tests/test_evals_metrics.py`, `tests/test_evals_gates.py`, `tests/test_evals_cli.py` |
| CI | `.github/workflows/evals.yml` (not executed here — no runner) |
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

Validation that goes beyond types: segments sorted by start (reference only),
`end > start`, reference segments inside `[0, duration_s]`, speakers declared,
`words` sorted and inside their segment, and **`words` must spell `text`**
token for token — a generator whose word timings drift from its transcript is
rejected here rather than silently scored. Hypothesis segments may be
unsorted and use any label strings.

Times are written with six decimals, trailing zeros trimmed. RTTM/STM written
by this module parse identically under meeteval's own `STM`/`RTTM` readers
(tested), so the files are standard, not a private dialect.

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
optimal (Hungarian) hypothesis→reference label mapping. Overlapping speech is
counted per track (two speakers talking for 2 s is 4 s of `total`).

* **Scoring region (UEM):** `[0, duration_s]` from meeting.json. Hypothesis
  speech after the end of the audio is not scored. Without a duration the
  union of both extents is used explicitly (never pyannote's silent default).
* **Collar:** `der_collar` is **pyannote's semantics — the total width of the
  no-score zone centred on every reference boundary.** The default `0.25`
  ignores ±0.125 s. NIST md-eval's `-c 0.25` (±0.25 s) is `--der-collar 0.5`
  here. The test `test_der_collar_is_total_width_centred_on_reference_boundaries`
  pins this: with collar 0.5 the closed-form case's `total` drops from 20 to
  19 s exactly.
* **Overlap:** scored by default. `--skip-overlap` removes reference overlap
  regions from the UEM (pyannote's `skip_overlap`). Independently,
  `der.overlap.*` reports DER restricted **to** the overlap regions (UEM =
  overlap ∩ scoring region, collar still applied) so a pipeline's behaviour
  under crosstalk is visible even when the headline DER hides it.
* **Per-speaker breakdown:** `der.per_speaker[<ref speaker>]` gives total /
  correct / miss / confusion / false-alarm seconds and the mapped hypothesis
  label; unmapped hypothesis speakers appear as `hyp:<label>` carrying their
  false alarm. The split is exact — the sums over all keys equal pyannote's
  global components, including in overlap regions, where a sub-segment's
  miss/confusion is shared equally among its unmatched reference speakers and
  its false alarm among its unmatched hypothesis speakers (the libraries
  define no such split; this rule is HARNESS_POLICY and is tested).

### JER — `pyannote.metrics.diarization.JaccardErrorRate`

Per reference speaker, `1 − |R ∩ H| / |R ∪ H|` against its optimally mapped
hypothesis speaker (1.0 when none); JER is the unweighted mean, so a
20-second speaker weighs as much as a 20-minute one. Same collar/UEM/overlap
conventions as DER. `jer.per_speaker` is a mirror of pyannote's loop and the
test asserts its mean equals the library's figure.

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

### tcpWER — `meeteval.wer.wer.time_constrained.tcp_word_error_rate`

cpWER with a time constraint: a hypothesis word may only match a reference
word whose interval overlaps the hypothesis word's interval widened by
`tcp_collar` seconds on each side. It catches a transcript that is right but
in the wrong place (a word attributed to the right speaker minutes late).
Default collar 5 s (meeteval's own default and recommendation). Word timings
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
non-finite fails its check. The result names every failing metric with its
value and bound, e.g.

```
[FAIL] suite 'synthetic-clean' on selfcheck-0001 — failed: der.der <= 0.05 (got 0.2045: exceeds max 0.05); cpwer.error_rate <= 0.1 (got 0.4: exceeds max 0.1)
```

| suite | gates | meaning |
|---|---|---|
| `oracle` | DER = 0, JER = 0, cpWER = 0, tcpWER = 0, concat WER = 0, speaker-count error = 0 | reference (or a lossless round-trip) scored against itself — the V4 rung |
| `synthetic-clean` | DER ≤ 0.05, cpWER ≤ 0.10, JER ≤ 0.10, speaker-count error = 0 | Layer 2 meetings, no noise |
| `synthetic-noisy` | DER ≤ 0.15, cpWER ≤ 0.25, speaker-count error ≤ 1 | Layer 2 meetings with reverberation and noise |
| `ami-headset` | DER ≤ 0.20, cpWER ≤ 0.30 | V5 placeholder, never exercised |

All thresholds are HARNESS_POLICY placeholders: none is calibrated against a
pipeline, because none exists yet.

Exit codes: `0` scored and gates passed (or none requested); `1` a gate
failed; `2` input error (malformed file, missing hypothesis under
`--missing fail`, unknown suite); `3` skipped — nothing to score.

## CLI

```
python -m evals score --ref <meeting dir> --hyp <hyp.json> [--gates evals/gates.yaml --suite X]
                      --report out.json [--md out.md] [--der-collar 0.25] [--skip-overlap]
                      [--tcp-collar 5] [--expand-contractions] [--numbers-to-words]
python -m evals batch --refs <root> --hyps <root> [--suite X --gate-on macro|micro|both|each]
                      [--missing fail|skip] --report out.json [--md out.md]
python -m evals validate --ref <meeting dir> --hyp <hyp.json>
python -m evals selfcheck [--report out.json]      # built-in meeting vs itself, requires 'oracle'
python -m evals suites [--gates file]              # list suites and their checks
```

`batch` finds every `meeting.json` under `--refs` recursively and looks for
each meeting's hypothesis under `--hyps` in this order:
`<relative meeting dir>/hyp.json`, `<meeting dir name>/hyp.json`,
`<meeting_id>/hyp.json`, `<meeting_id>.hyp.json`, `<meeting_id>.json`.
Reports carry both **macro** (mean of per-meeting rates, every meeting counts
once) and **micro** (pooled numerators over pooled denominators, so a long
meeting weighs more) aggregates; `--gate-on` picks which one the suite is
applied to (default macro).

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

## HARNESS_POLICY items

| policy | value | where |
|---|---|---|
| DER/JER collar default (pyannote total-width semantics) | 0.25 (±0.125 s) | `metrics.DEFAULT_DER_COLLAR` |
| tcpWER collar default | 5.0 s | `metrics.DEFAULT_TCP_COLLAR` |
| scoring region | `[0, duration_s]`; union of extents without a duration | `metrics.to_uem` |
| overlap-region DER | UEM = reference overlap ∩ scoring region, collar applied | `metrics.diarization_error_rate` |
| per-speaker split in overlap regions | equal share among unmatched speakers | `metrics._speaker_breakdown` |
| word timings for tcpWER | used as-is when every scorable segment has them, else meeteval pseudo timings | `metrics.tcp_wer` |
| segments with empty normalised text | dropped from cpWER/tcpWER input | `metrics.to_seglst` |
| multi-token normalised word | interval split equally | `io.normalized_words` |
| hypothesis sortedness | not required; sorted when scored | `io.hypothesis_from_dict` |
| time formatting | six decimals, trailing zeros trimmed | `io.format_time` |
| contraction table, `'s` untouched; number range < 10¹² | `io.CONTRACTIONS`, `io.integer_to_words` |
| gate on missing/None metric | fails | `gates.evaluate_check` |
| gate thresholds | all four suites | `evals/gates.yaml` |
| exit codes | 0/1/2/3 | `gates.EXIT_*` |
| batch hypothesis lookup order | five candidates | `cli.find_hypothesis` |
| batch gate target default | macro | `cli` `--gate-on` |
| missing hypothesis | fails the batch unless `--missing skip` | `cli` `--missing` |
| AMI layout | `data/corpora/ami/meetings/<ID>/meeting.json` | `cli.AMI_EXPECTED_LAYOUT` |
| selfcheck meeting | hand-built 2-speaker, 10 s, word timings | `cli.selfcheck_meeting` |

## Tests

137 tests in four files (parametrised cases counted individually), all
against hand-built inputs with paper-derived answers; the generator is never
imported.

* **Closed-form DER** — reference A [0,10] B [10,20]; hypothesis x [0,9],
  y [10,18], x [18,20], y [25,25.5]: miss 1.0, false alarm 0.5, confusion
  2.0, total 20 → DER 0.175 at collar 0; 3.0/19 at collar 0.5; per-speaker
  breakdown exact; overlap case (A [0,10], B [8,12], only A detected) →
  4/14 scored, 0.2 with `skip_overlap`, 0.5 on the overlap region alone;
  breakdown sums equal the global components under collar and overlap.
* **JER** — one speaker half-covered → 0.5; closed form
  (0.25 + 2.5/10.5)/2; per-speaker mean equals the library.
* **Speaker swap** — literal WER 8/6 with S4 D2 I2, cpWER 0 before and after,
  assignment {(A,B),(B,A)}; arbitrary labels → literal 2.0, cpWER 0.
* **tcpWER** — +0.3 s → 0; +10 s at collar 5 → exactly 2.0 (0/9/9); collar
  20 → 0; cpWER 0 throughout; pseudo-timing fallback flagged.
* **Loaders** — 21 malformed meeting.json cases, 6 hypothesis cases, 9 RTTM
  and 5 STM line cases, each asserting the message names the rule and the
  location; comments and blank lines skipped; meeting_id cross-check.
* **Normalisation** — 13 default cases, 6 contraction, 7 number cases,
  identical treatment of both sides, timed-word splitting.
* **Round trips** — meeting.json → disk → equal dict; meeting → ref.rttm +
  ref.stm → load equals the original segments; hyp.json/rttm/stm; written
  files parse under meeteval's readers with the same values; float noise
  within 1e-6.
* **Gates** — pass/fail naming only the failing metrics, fail-closed on
  missing/None/NaN/bool, eq/min/max/tol semantics, 10 malformed-config
  cases, unknown suite lists the available ones.
* **CLI** — every subcommand and exit code, macro vs micro numbers, all five
  lookup paths, absent-corpus skip message, `python -m evals selfcheck` in a
  subprocess.

## What is not done

* **No AMI data, so V5 cannot run.** Nothing under `data/corpora/` (the
  directory is gitignored and empty). V5 needs the AMI Meeting Corpus
  (headset mix + NXT word/segment annotations) converted into contract
  meeting directories at exactly:

  ```
  data/corpora/ami/meetings/<MEETING_ID>/meeting.json   # schema plaud-harness/meeting/1, words from NXT
  data/corpora/ami/meetings/<MEETING_ID>/ref.rttm
  data/corpora/ami/meetings/<MEETING_ID>/ref.stm
  data/corpora/ami/meetings/<MEETING_ID>/mix.wav        # 16 kHz mono headset mix (gitignored)
  ```
  and hypotheses at `out/ami/<MEETING_ID>/hyp.json`. The scoring command is
  ```
  python -m evals batch --refs data/corpora/ami/meetings --hyps out/ami \
      --gates evals/gates.yaml --suite ami-headset --report out/ami/report.json
  ```
  Today that command exits `3` and prints this layout. The AMI→contract
  converter (NXT XML → meeting.json, licence CC-BY-4.0, ~10 GB of audio) is
  **not written**: it needs the corpus to be tested against and the task
  brief forbids network access. The `ami-headset` thresholds are placeholders.
* **No generator integration.** Layer 2 is being built concurrently; the
  harness has only been fed hand-built meetings. `test_evals_metrics.py`'s
  cases and `python -m evals selfcheck` are the substitute until the V4 rung
  (generator ground truth → oracle suite) can be wired.
* **No pipeline hypotheses.** All non-oracle hypotheses in the tests are
  hand-degraded references; the numbers say the harness measures what it
  claims, not what any pipeline achieves.
* **CI workflow not executed.** `.github/workflows/evals.yml` installs
  `requirements/evals.txt`, runs the four test files, the selfcheck and the
  suite listing. There is no runner in this environment; it has been checked
  by reading only.
* **Not included by design:** ORC-WER / MIMO-WER (meeteval offers them; the
  contract always has speaker labels so cpWER/tcpWER are the right pair),
  DER purity/coverage, confidence intervals across meetings, and any
  language-specific normalisation beyond the small English tables above.
* **Known conventions with consequences:** hypothesis speech after
  `duration_s` is never scored (UEM); `wer_concat` interleaves overlapping
  segments by start time; per-speaker DER attribution inside overlap regions
  is the harness's equal-share rule, not a library definition.
