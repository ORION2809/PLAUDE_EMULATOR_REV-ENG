"""Aggregation of per-meeting reports across a batch (macro and micro averages).

* **macro**: the unweighted mean of each per-meeting metric (every meeting
  counts once, however long).  Metrics that are ``None`` for a meeting are
  left out of that metric's mean; the count that went in is reported.
* **micro**: pooled numerators over pooled denominators (a long meeting
  weighs more).  DER = Σ(miss+fa+conf)/Σtotal, JER = Σspeaker_error/Σspeakers,
  WER/cpWER/tcpWER = Σerrors/Σreference words.  Speaker-count error has no
  natural pooling, so micro reports the same mean as macro plus the sums.

HARNESS_POLICY: which average a gate is applied to is the CLI's ``--gate-on``
choice (default ``macro``); both are always in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from evals.metrics import MeetingReport, _rate


@dataclass
class BatchResult:
    reports: list[MeetingReport]
    missing: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def macro(self) -> dict[str, Any]:
        return macro_average(self.reports)

    @property
    def micro(self) -> dict[str, Any]:
        return micro_average(self.reports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_meetings": len(self.reports),
            "meetings": [r.to_dict() for r in self.reports],
            "missing": self.missing,
            "errors": self.errors,
            "macro": self.macro,
            "micro": self.micro,
        }


def macro_average(reports: Sequence[MeetingReport]) -> dict[str, Any]:
    if not reports:
        return {"n": 0}
    flats = [r.flat() for r in reports]
    keys: list[str] = []
    for f in flats:
        for k in f:
            if k not in keys:
                keys.append(k)
    out: dict[str, Any] = {"n": len(reports)}
    counts: dict[str, int] = {}
    for k in keys:
        vals = [f[k] for f in flats if k in f and isinstance(f[k], (int, float)) and not isinstance(f[k], bool)]
        counts[k] = len(vals)
        out[k] = (sum(vals) / len(vals)) if vals else None
    out["counts"] = counts
    return out


def micro_average(reports: Sequence[MeetingReport]) -> dict[str, Any]:
    if not reports:
        return {"n": 0}
    total = correct = miss = fa = conf = 0.0
    ov_total = ov_err = ov_time = 0.0
    jer_err = 0.0
    jer_n = 0
    wl_err = wl_n = wc_err = wc_n = 0
    cp_err = cp_len = tcp_err = tcp_len = 0
    cp_missed = cp_falarm = 0
    sc_err = sc_abs = 0
    for r in reports:
        d = r.der
        total += d.total
        correct += d.correct
        miss += d.miss
        fa += d.false_alarm
        conf += d.confusion
        if d.overlap is not None:
            ov_total += d.overlap.total
            ov_err += d.overlap.miss + d.overlap.false_alarm + d.overlap.confusion
            ov_time += d.overlap.overlap_time
        jer_err += r.jer.speaker_error
        jer_n += r.jer.speaker_count
        wl_err += r.wer_literal.errors
        wl_n += r.wer_literal.reference_words
        wc_err += r.wer_concat.errors
        wc_n += r.wer_concat.reference_words
        cp_err += r.cpwer.errors
        cp_len += r.cpwer.length
        cp_missed += r.cpwer.missed_speaker
        cp_falarm += r.cpwer.falarm_speaker
        tcp_err += r.tcpwer.errors
        tcp_len += r.tcpwer.length
        sc_err += r.speaker_count.error
        sc_abs += r.speaker_count.abs_error
    n = len(reports)
    return {
        "n": n,
        "der.der": _rate(miss + fa + conf, total),
        "der.total": total,
        "der.correct": correct,
        "der.miss": miss,
        "der.false_alarm": fa,
        "der.confusion": conf,
        "der.miss_rate": _rate(miss, total),
        "der.false_alarm_rate": _rate(fa, total),
        "der.confusion_rate": _rate(conf, total),
        "der.overlap.der": _rate(ov_err, ov_total),
        "der.overlap.total": ov_total,
        "der.overlap.overlap_time": ov_time,
        "jer.jer": _rate(jer_err, jer_n),
        "jer.speaker_count": jer_n,
        "jer.speaker_error": jer_err,
        "wer_literal.wer": _rate(wl_err, wl_n),
        "wer_literal.errors": wl_err,
        "wer_literal.reference_words": wl_n,
        "wer_concat.wer": _rate(wc_err, wc_n),
        "wer_concat.errors": wc_err,
        "wer_concat.reference_words": wc_n,
        "cpwer.error_rate": _rate(cp_err, cp_len),
        "cpwer.errors": cp_err,
        "cpwer.length": cp_len,
        "cpwer.missed_speaker": cp_missed,
        "cpwer.falarm_speaker": cp_falarm,
        "tcpwer.error_rate": _rate(tcp_err, tcp_len),
        "tcpwer.errors": tcp_err,
        "tcpwer.length": tcp_len,
        "speaker_count.error": sc_err / n,
        "speaker_count.abs_error": sc_abs / n,
        "speaker_count.error_sum": sc_err,
        "speaker_count.abs_error_sum": sc_abs,
    }
