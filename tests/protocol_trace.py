"""Capture-ready protocol trace model (R5-S11, test harness only).

Raw GATT bytes are authoritative: every event retains the exact payload,
and parsing is an optional, clearly labeled interpretation that must
never replace the bytes. All traces produced here are labeled
SYNTHETIC_EMULATOR_TRACE. A future legitimate device capture uses the
same schema with trace_type set by its importer (never by this module).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

TRACE_TYPE_SYNTHETIC = "SYNTHETIC_EMULATOR_TRACE"

HOST_TO_DEVICE = "HOST_TO_DEVICE"
DEVICE_TO_HOST = "DEVICE_TO_HOST"


@dataclass
class TraceEvent:
    """One observed GATT payload crossing 2BB1/2BB0."""

    ts: float  # monotonic seconds; comparison uses explicit tolerance
    direction: str  # HOST_TO_DEVICE | DEVICE_TO_HOST
    phase: str  # GATT | PREKEY | RSA | SEALED | FILESYNC | AUDIO | UNKNOWN
    characteristic: str  # "2BB1" | "2BB0" | other
    raw: bytes  # EXACT payload; never mutated after capture
    parsed: dict[str, Any] = field(default_factory=dict)  # optional interpretation
    result: str = ""  # e.g. received/sent/accepted/dropped:<reason>/error:<kind>
    classification: str = ""  # SDK_PROVEN | ... | UNKNOWN (of the interpretation)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw"] = self.raw.hex()
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> TraceEvent:
        e = dict(d)
        e["raw"] = bytes.fromhex(e["raw"])
        return TraceEvent(**e)


def classify_raw(raw: bytes) -> tuple[str, dict[str, Any], str]:
    """Best-effort keyless phase/parse/classification of one payload (pure).

    Returns (phase, parsed, classification). Marker and cleartext-control
    frames are recognized exactly (the recovered z$c branch structure).
    Opaque (sealed) payloads yield UNKNOWN here by design: without key
    state no honest classification exists. The live peripheral overrides
    with explicit SEALED/FILESYNC labels once it has opened a frame --
    those carry parsed["keyed"]=True so replay never downgrades stored
    knowledge back through this keyless function.
    """
    from plaudsim.handshake import (
        MARKER_FORCE_CLEAR,
        MARKER_PRE_HANDSHAKE,
        MARKER_RSA_MARKER,
        MARKER_SECRET,
    )

    data = bytes(raw)
    if len(data) >= 4:
        marker = int.from_bytes(data[0:2], "little")
        if marker in (MARKER_PRE_HANDSHAKE, MARKER_FORCE_CLEAR):
            return (
                "PREKEY",
                {"marker": hex(marker), "count": data[2], "index": data[3],
                 "chunk_len": len(data) - 4},
                "SDK_PROVEN",
            )
        if marker == MARKER_RSA_MARKER:
            return ("PREKEY", {"marker": hex(marker)}, "SDK_PROVEN")
        if marker == MARKER_SECRET:
            return (
                "RSA",
                {"marker": hex(marker), "count": data[2], "index": data[3],
                 "chunk_len": len(data) - 4},
                "SDK_PROVEN",
            )
    if len(data) >= 3 and data[0] == 1:
        opcode = int.from_bytes(data[1:3], "little")
        phase = "FILESYNC" if opcode in (26, 28, 29, 30) else "SEALED"
        return (phase, {"opcode": opcode, "len": len(data)}, "SDK_PROVEN")
    if len(data) >= 1 and data[0] == 2:
        return ("FILESYNC", {"frame_type": 2, "len": len(data)}, "SDK_PROVEN")
    return ("UNKNOWN", {}, "UNKNOWN")  # opaque sealed bytes without key state


@dataclass
class Trace:
    """An ordered event list with its provenance label."""

    trace_type: str = TRACE_TYPE_SYNTHETIC
    events: list[TraceEvent] = field(default_factory=list)

    def record(self, direction: str, characteristic: str, raw: bytes,
               result: str = "") -> TraceEvent:
        phase, parsed, classification = classify_raw(raw)
        event = TraceEvent(
            ts=time.monotonic(),
            direction=direction,
            phase=phase,
            characteristic=characteristic,
            raw=bytes(raw),
            parsed=parsed,
            result=result,
            classification=classification,
        )
        self.events.append(event)
        return event

    def to_json(self) -> str:
        return json.dumps(
            {"trace_type": self.trace_type, "events": [e.to_json() for e in self.events]},
            indent=2,
        )

    @staticmethod
    def from_json(text: str) -> Trace:
        d = json.loads(text)
        if d.get("trace_type") != TRACE_TYPE_SYNTHETIC:
            raise ValueError(f"refusing non-synthetic trace: {d.get('trace_type')!r}")
        return Trace(
            trace_type=d["trace_type"],
            events=[TraceEvent.from_json(e) for e in d["events"]],
        )


def compare_traces(ref: Trace, cand: Trace, time_tolerance_s: float = 1.0) -> list[dict[str, Any]]:
    """Deterministic emulator-vs-candidate diff (lists of plain dicts).

    Kinds: BYTE_DIFFERENCE (raw bytes differ; detail names length vs
    marker vs content), ORDERING_MISMATCH (event-count or
    direction/phase-order divergence), TIMING_DIFFERENCE (|dts| over
    tolerance), PARSER_UNKNOWN (informational only -- never a mismatch
    verdict; unknown bytes must survive, not fail).
    """
    diffs: list[dict[str, Any]] = []
    if len(ref.events) != len(cand.events):
        diffs.append({
            "kind": "ORDERING_MISMATCH",
            "detail": f"event count {len(ref.events)} vs {len(cand.events)}",
        })
    for i, (a, b) in enumerate(zip(ref.events, cand.events)):
        index_has_diff = False
        if a.direction != b.direction or a.phase != b.phase:
            diffs.append({
                "kind": "ORDERING_MISMATCH",
                "index": i,
                "detail": f"{a.direction}/{a.phase} vs {b.direction}/{b.phase}",
            })
            index_has_diff = True
            continue
        if a.raw != b.raw:
            if len(a.raw) != len(b.raw):
                detail = f"length {len(a.raw)} vs {len(b.raw)}"
            elif a.raw[:2] != b.raw[:2]:
                detail = f"marker {a.raw[:2].hex()} vs {b.raw[:2].hex()}"
            else:
                detail = "content differs, same length/marker"
            diffs.append({"kind": "BYTE_DIFFERENCE", "index": i, "detail": detail})
            index_has_diff = True
        if abs(a.ts - b.ts) > time_tolerance_s:
            diffs.append({
                "kind": "TIMING_DIFFERENCE",
                "index": i,
                "detail": f"|dts|={abs(a.ts - b.ts):.3f}s > {time_tolerance_s}s",
            })
            index_has_diff = True
        # PARSER_UNKNOWN is context on a genuine divergence at the same
        # index -- never a standalone verdict, never noise on identical
        # traces. Unknown bytes must survive, not fail.
        if index_has_diff and (a.classification == "UNKNOWN" or b.classification == "UNKNOWN"):
            diffs.append({"kind": "PARSER_UNKNOWN", "index": i,
                          "detail": "unparsed bytes retained on both sides"})
    return diffs
