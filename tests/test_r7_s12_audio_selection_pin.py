"""R7-S12 falsification pin: audio format selection is SNIFF-based.

R6-S1 established (and the R7 closure re-derived from bytecode) that the
device-profile bit `isOggAudio` is WRITTEN (`BleDevice.setOggAudio`) but never
READ for any decision: AudioExporter selects the converter by the 512-byte
`PLAUD.AI` magic test and then a 4-byte `OggS` sniff. These tests read the
javap dump so that a future SDK build which starts consuming the profile bit
for format selection fails loudly instead of silently changing the model.
They skip when build/evidence is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
JAVAP = ROOT / "build/evidence/javap/ALL.txt"

pytestmark = pytest.mark.skipif(not JAVAP.exists(), reason="build/evidence not built")


@pytest.fixture(scope="module")
def javap() -> str:
    return JAVAP.read_text(errors="replace")


def test_is_ogg_audio_getter_has_zero_invoke_sites(javap: str):
    setter_calls = re.findall(r"invokevirtual .*BleDevice\.setOggAudio:\(Z\)V", javap)
    getter_calls = re.findall(r"invoke\w+ .*BleDevice\.isOggAudio:\(\)Z", javap)
    assert setter_calls, "the profile parser must still write the bit"
    assert getter_calls == [], (
        "isOggAudio() is now consumed somewhere; format selection may no longer be "
        f"sniff-based: {getter_calls[:3]}"
    )


def test_audio_exporter_sniffs_oggs_bytes(javap: str):
    # 'O','g','g','S' as the four compared byte literals: 79 103 103 83
    assert re.search(r"bipush\s+79\n", javap) and re.search(r"bipush\s+103\n", javap)
    assert re.search(r"bipush\s+83\n", javap)
    assert "PLAUD.AI" in javap


def test_only_chacha_for_ble_seal_no_aes_in_ble_helpers(javap: str):
    """The AES-GCM transformation string exists, but BLE seal helpers take no
    algorithm parameter (R7 transport-scope finding); keep the two apart."""
    assert "AES/GCM/NoPadding" in javap
    assert "ChaCha20" in javap or "ChaCha7539" in javap
