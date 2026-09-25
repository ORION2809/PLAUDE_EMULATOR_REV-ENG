"""Device presets: microphone counts (evidenced) and geometries (HARNESS_POLICY).

EVIDENCE for the mic COUNTS and the recorded channel count:
* Note Pro: 4 MEMS + 1 VPU; NotePin S: 2 MEMS
  -- docs/architecture/hardware.md:26 and docs/architecture/audio.md section 1
  (OFFICIAL_DOC class).
* The device stores 1 or 2 channels: `BleFile.calculateOpusDuration(byteLen, ch)`
  = (byteLen / (ch*80)) * 20 and `calculateOpusOffset(byteLen, ch)` = (byteLen/20)*80*ch
  (build/evidence/javap/ALL.txt:10429, :10467; docs/protocol-ledger.md section 8),
  so the on-device recording is a 1- or 2-channel Opus stream, never a 4-channel one.

UNKNOWN (and therefore HARNESS_POLICY here):
* the physical mic array geometry of either product;
* how the firmware reduces 4 (or 2) MEMS capsules to the 1- or 2-channel
  recording (beamforming, selection, or mixing) -- U20-adjacent, needs a device;
* the VPU (vibration pickup) path. Bone/body conduction cannot be modelled by an
  image-source room simulation, so the VPU is NOT simulated at all.

The geometries below are placeholders sized like the products' bodies so the
inter-channel delays are physically plausible; they are not measurements.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DevicePreset:
    name: str
    product: str
    mic_offsets_m: tuple[tuple[float, float, float], ...]  # relative to device centre, x/y/z
    stereo_groups: tuple[tuple[int, ...], tuple[int, ...]]  # (left mics, right mics)
    evidence: str
    policy: str

    @property
    def n_mics(self) -> int:
        return len(self.mic_offsets_m)


PRESETS: dict[str, DevicePreset] = {
    "mono": DevicePreset(
        name="mono",
        product="generic single-capsule reference",
        mic_offsets_m=((0.0, 0.0, 0.0),),
        stereo_groups=((0,), (0,)),
        evidence="none: an idealised single point microphone, used as a control",
        policy="HARNESS_POLICY: one omnidirectional capsule at the device centre",
    ),
    "notepin_s_2mic": DevicePreset(
        name="notepin_s_2mic",
        product="Plaud NotePin S",
        mic_offsets_m=((-0.015, 0.0, 0.0), (0.015, 0.0, 0.0)),
        stereo_groups=((0,), (1,)),
        evidence="2 MEMS: docs/architecture/hardware.md:26 (OFFICIAL_DOC)",
        policy="HARNESS_POLICY: two capsules 30 mm apart on the device x axis; real spacing UNKNOWN",
    ),
    "note_pro_4mic": DevicePreset(
        name="note_pro_4mic",
        product="Plaud Note Pro",
        mic_offsets_m=(
            (-0.025, -0.015, 0.0),
            (0.025, -0.015, 0.0),
            (0.025, 0.015, 0.0),
            (-0.025, 0.015, 0.0),
        ),
        stereo_groups=((0, 3), (1, 2)),
        evidence="4 MEMS + 1 VPU: docs/architecture/hardware.md:26 (OFFICIAL_DOC); VPU not simulated",
        policy="HARNESS_POLICY: four capsules at the corners of a 50 x 30 mm rectangle; real geometry UNKNOWN",
    ),
}


def mic_positions(preset: DevicePreset, position_m: tuple[float, float, float], yaw_deg: float = 0.0) -> np.ndarray:
    """Absolute mic positions, shape (3, n_mics), after rotating the preset's
    offsets about z by `yaw_deg` and translating to `position_m`."""
    yaw = np.deg2rad(yaw_deg)
    rot = np.array([[np.cos(yaw), -np.sin(yaw), 0.0], [np.sin(yaw), np.cos(yaw), 0.0], [0.0, 0.0, 1.0]])
    offsets = np.asarray(preset.mic_offsets_m, dtype=np.float64).T  # (3, n)
    return rot @ offsets + np.asarray(position_m, dtype=np.float64).reshape(3, 1)


def device_mix(mic_signals: np.ndarray, preset: DevicePreset, channels: int) -> np.ndarray:
    """Reduce (n_mics, n) to (channels, n): mono = mean of all capsules, stereo =
    mean of each stereo group. HARNESS_POLICY: the real firmware's reduction is
    UNKNOWN (see module docstring)."""
    if channels == 1:
        return mic_signals.mean(axis=0, keepdims=True)
    if channels == 2:
        left, right = preset.stereo_groups
        return np.stack([mic_signals[list(left)].mean(axis=0), mic_signals[list(right)].mean(axis=0)])
    raise ValueError("device channels must be 1 or 2 (BleFile.channels; ledger section 8)")


__all__ = ["DevicePreset", "PRESETS", "device_mix", "mic_positions"]
