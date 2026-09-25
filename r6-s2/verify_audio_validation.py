#!/usr/bin/env python3
"""R6-S2 verifier: fixture pins + independent validation headline numbers.

Re-asserts manifest hashes, Ogg structure counts, R6-vs-demuxer packet
identity, per-packet decode shape, and full-decode PCM properties without
pytest. Requires PyAV + numpy (test-only deps). Exit 0 = verified.
"""

from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


try:
    import av  # noqa: E402
    import numpy as np  # noqa: E402
except ImportError as exc:
    print(f"ENVIRONMENTAL BLOCKER: {exc}")
    sys.exit(2)

_spec = _ilu.spec_from_file_location("plaudsim_audio", ROOT / "emulator/plaudsim/audio.py")
assert _spec is not None and _spec.loader is not None
audio = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(audio)

manifest = json.loads((FIX / "r6s2_manifest.json").read_text())
by_name = {f["path"]: f for f in manifest["fixtures"]}
for name in ("r6s2_16k_mono.ogg", "r6s2_16k_stereo.ogg"):
    meta = by_name[name]
    actual = hashlib.sha256((FIX / name).read_bytes()).hexdigest()
    check(f"{name} sha256 pinned + synthetic-labeled",
          actual == meta["sha256"] and meta["synthetic"] and meta["not_plaud_capture"])

for name, channels in (("r6s2_16k_mono.ogg", 1), ("r6s2_16k_stereo.ogg", 2)):
    raw = (FIX / name).read_bytes()
    pages = list(audio.iter_ogg_pages(raw))
    roles = audio.classify_ogg_pages(pages)
    packets = []
    for page in roles["audio_pages"]:
        packets += audio.reassemble_page_packets(page["lacing"], page["payload"])
    check(f"{name}: 5 pages / 101 packets / ch{channels}",
          len(pages) == 5 and len(packets) == 101
          and roles["head"]["channels"] == channels
          and roles["head"]["sample_rate"] == 16000)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        container = av.open(tmp.name)
        demuxed = [bytes(p) for p in container.demux(container.streams.audio[0])]
    check(f"{name}: R6 packets == demuxer audio packets",
          demuxed[:-1] == packets and demuxed[-1] == b"")
    from av import CodecContext, Packet  # noqa: E402

    decoder = CodecContext.create("opus", "r")
    first = decoder.decode(Packet(packets[0]))
    check(f"{name}: extracted packet decodes to 960 samples @48 kHz",
          len(first) == 1 and first[0].samples == 960 and first[0].sample_rate == 48000)

with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as tmp:
    tmp.write((FIX / "r6s2_16k_mono.ogg").read_bytes())
    tmp.flush()
    container = av.open(tmp.name)
    stream = container.streams.audio[0]
    frames = [f for f in container.decode(stream)]
decoded = np.concatenate([f.to_ndarray() for f in frames], axis=1)
check("full decode: mono 96000 samples @48 kHz", decoded.shape == (1, 96000))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
