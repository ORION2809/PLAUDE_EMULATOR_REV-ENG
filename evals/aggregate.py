"""Aggregation of per-meeting reports across a batch (macro and micro averages).

* **macro**: the unweighted mean of each per-meeting metric (every meeting
  counts once, however long).  Metrics that are ``None`` for a meeting are
  left out of that metric's mean; the count that went in is reported.
* **micro**: pooled numerators over pooled denominators (a long meeting
  weighs more).  DER = Σ(miss+fa+conf)/Σtotal, JER = Σspeaker_error/Σspeakers,
  WER/cpWER/tcpWER = Σerrors/Σreference words.  Speaker-count error has no
  natural pooling, so micro reports the same mean as macro plus the sums.
  A meeting whose cpWER/tcpWER meeteval refused (``CpWerResult.refused``) is
  left out of those pools; ``counts`` says how many meetings each pooled
  cpWER/tcpWER value covers.

Both aggregates carry ``counts`` (metric -> meetings that produced a value)
and ``not_scored`` (cpWER/tcpWER metric -> meetings meeteval refused); a gate
on a metric with a non-zero ``not_scored`` fails closed (evals/gates.py).

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
    out["not_scored"] = _not_scored(reports)
    return out


def _not_scored(reports: Sequence[MeetingReport]) -> dict[str, int]:
    """cpWER/tcpWER metric path -> number of meetings meeteval refused."""
    out: dict[str, int] = {}
    for r in reports:
        for name, res in (("cpwer", r.cpwer), ("tcpwer", r.tcpwer)):
            if res.refused is not None:
                for k in r.flat():
                    if k.startswith(name + "."):
                        out[k] = out.get(k, 0) + 1
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
    n_cp = n_tcp = 0
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
        if r.cpwer.refused is None:
            n_cp += 1
            cp_err += r.cpwer.errors
            cp_len += r.cpwer.length
            cp_missed += r.cpwer.missed_speaker
            cp_falarm += r.cpwer.falarm_speaker
        if r.tcpwer.refused is None:
            n_tcp += 1
            tcp_err += r.tcpwer.errors
            tcp_len += r.tcpwer.length
        sc_err += r.speaker_count.error
        sc_abs += r.speaker_count.abs_error
    n = len(reports)
    cp_keys = ("cpwer.error_rate", "cpwer.errors", "cpwer.length", "cpwer.missed_speaker", "cpwer.falarm_speaker")
    tcp_keys = ("tcpwer.error_rate", "tcpwer.errors", "tcpwer.length")
    counts = {**{k: n_cp for k in cp_keys}, **{k: n_tcp for k in tcp_keys}}
    return {
        "n": n,
        "counts": counts,
        "not_scored": _not_scored(reports),
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
        "cpwer.error_rate": _rate(cp_err, cp_len) if n_cp else None,
        "cpwer.errors": cp_err if n_cp else None,
        "cpwer.length": cp_len if n_cp else None,
        "cpwer.missed_speaker": cp_missed if n_cp else None,
        "cpwer.falarm_speaker": cp_falarm if n_cp else None,
        "tcpwer.error_rate": _rate(tcp_err, tcp_len) if n_tcp else None,
        "tcpwer.errors": tcp_err if n_tcp else None,
        "tcpwer.length": tcp_len if n_tcp else None,
        "speaker_count.error": sc_err / n,
        "speaker_count.abs_error": sc_abs / n,
        "speaker_count.error_sum": sc_err,
        "speaker_count.abs_error_sum": sc_abs,
    }
