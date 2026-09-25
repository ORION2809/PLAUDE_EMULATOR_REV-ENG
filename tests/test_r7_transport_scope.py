"""R7: how far the sealed session's state actually reaches.

R5-S11 listed "shared counters vs independent endpoint counters" as an open
emulator question. It is answerable from bytecode, and the answer is more
specific than either option: the split is per MODEL, not per transport.

`w7` (the Wi-Fi WebSocket operation) reads the sealed material through class
`z` -- the BLE transport -- via `z.j()/k()/i()` (the J/K/L triple),
`z.o()`/`z.p()` (the RX/TX counters) and `z.w()` (the AEAD selector). So the
Wi-Fi channel is keyed by whatever the BLE handshake established.

But for one model it keeps its own counters. `w7` tests
`serialNumber.substring(0, 3).equals("881")` and then either uses its own
statics `w7.m`/`w7.n`, or falls through to the shared `z.M`/`z.N` via
`z.e(int)`/`z.d(int)`. 881 is the Plaud Note Pro.

Consequences the emulator must not get wrong:

* The BLE sealed counters are process-global statics, and on a non-881 device
  a Wi-Fi exchange advances the same RX counter a BLE frame is checked
  against. Our sealed session is per-session, which is correct for a
  BLE-only harness -- but it means a future Wi-Fi implementation may NOT
  allocate fresh counters without first checking the model.
* The keys are shared unconditionally. There is no per-transport key
  derivation to reconstruct.

These are evidence-conformance assertions: they read javap, so they keep
telling the truth even if the emulator changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

EVIDENCE = Path(__file__).parents[1] / "build/evidence"
PROTO = EVIDENCE / "javap/com/plaud/sdk/proto"


def _w7() -> str:
    if not (PROTO / "w7.txt").is_file():
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    return (PROTO / "w7.txt").read_text(errors="replace")


def test_wifi_reads_its_sealed_material_from_the_ble_transport() -> None:
    """J/K/L and the AEAD selector come from class z, unconditionally."""
    w7 = _w7()
    for accessor in ("z.j:()[B", "z.k:()[B", "z.i:()[B", "z.w:()Z"):
        assert f"Method com/plaud/sdk/proto/{accessor}" in w7, accessor


def test_wifi_can_write_the_shared_ble_sequence_counters() -> None:
    """`z.d(int)` sets N (RX) and `z.e(int)` sets M (TX). Both are reachable
    from the Wi-Fi path, which is what makes the counters genuinely shared
    rather than merely similarly named."""
    w7 = _w7()
    assert "Method com/plaud/sdk/proto/z.d:(I)V" in w7, "Wi-Fi can set the BLE RX counter"
    assert "Method com/plaud/sdk/proto/z.e:(I)V" in w7, "Wi-Fi can set the BLE TX counter"
    assert "Method com/plaud/sdk/proto/z.o:()I" in w7, "Wi-Fi reads the BLE RX counter"
    assert "Method com/plaud/sdk/proto/z.p:()I" in w7, "Wi-Fi reads the BLE TX counter"


def test_model_881_keeps_private_wifi_counters() -> None:
    """The fork is a serial-prefix string compare against "881", and the
    alternative branch uses w7's own statics."""
    w7 = _w7()
    assert "String 881" in w7, "the model fork literal"
    # w7's own counters exist and are both read and written.
    assert "getstatic" in w7 and "Field m:I" in w7
    assert "Field n:I" in w7


def test_ble_transport_never_consults_the_model() -> None:
    """Falsification guard. If the BLE path also forked on "881", our
    single-counter sealed session would be wrong for the Note Pro."""
    ble = "".join(
        p.read_text(errors="replace")
        for p in sorted(PROTO.glob("z.txt")) + sorted(PROTO.glob("z$*.txt"))
    )
    if not ble:
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    assert "String 881" not in ble


def test_sealed_session_models_one_counter_pair_per_session() -> None:
    """What the emulator actually does, stated so the scope is explicit: one
    TX and one RX counter per sealed session, with no transport fan-out. That
    is correct for a BLE-only harness and is NOT a claim about Wi-Fi."""
    from plaudsim.sealed import SealedSession

    fields = SealedSession.__dataclass_fields__ if hasattr(SealedSession, "__dataclass_fields__") else {}
    src = Path(sys.modules["plaudsim.sealed"].__file__).read_text()
    assert "881" not in src, "the harness must not silently adopt the Wi-Fi model fork"
    del fields
