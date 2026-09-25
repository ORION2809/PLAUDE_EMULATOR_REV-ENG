# R6-S2: independent Ogg/Opus validation (SYNTHETIC -- NOT PLAUD CAPTURE)

Verdict: **GENERIC OGG/OPUS VERIFIED; PLAUD FORMAT REMAINS UNCONFIRMED.**

`verify_audio_validation.py` (ALL CHECKS PASSED) +
`tests/test_r6_s2_audio_validation.py` (10 passed). Full suite 204 → 214.
No frozen change. Claims A established and B strengthened; C stays OPEN.

## 1-2. Encoder / decoder

PyAV 18.1.0 with bundled libopus (encoder `libopus`, decoder `opus`);
numpy 2.4.6 for similarity math. Test-only dependencies: `audio.py`
itself stays dependency-free. No Opus encoder was implemented; no system
packages were required.

## 3. Fixtures (tests/fixtures/, pinned by sha256 in r6s2_manifest.json)

Generator: `scripts/generate_r6s2_fixture.py` (deterministic 440+880 Hz
sine mix, int16). `r6s2_16k_mono.ogg` (11 271 B,
`0f45367b…cbb48c3d`) and `r6s2_16k_stereo.ogg` (13 201 B,
`e13d47c5…f73b2`): 16 kHz, 2.0 s, 32 kbps CBR (the Plaud wire rate).

## 4-6. Ogg structure / packets / R6 results

Both fixtures: 5 Ogg pages (OpusHead + tags + 3 audio), OpusHead
channels 1/2, preSkip 312, sampleRate 16000; 101 audio packets each,
VBR lengths (standard encoders do not emit fixed-80 frames -- Plaud's
80-B CBR is a device property, not Ogg's). R6 extraction is
byte-identical and ordered vs PyAV's own demuxer (modulo its zero-length
trailing flush packet).

## 7-8. Independent decode / PCM

R6-extracted packets decode independently at 960 samples @48 kHz each
(20 ms; Plaud's m4 at 16 kHz would yield 320 for the same packet --
same duration, different valid decoder rate). Full decode: mono
(1, 96000) @48 kHz == 2.0 s; central-window correlation with the source
0.9999 (threshold asserted at 0.90; lossy, never bit-identity). Stereo
(2, 96000). Malformed-input behavior recorded for the independent
decoder only (0xFF×80 → InvalidDataError; degenerate → PLC-ish frames).

## 9. OPEN (Plaud-specific)

512-B header content (no wrapper built -- would require guessing);
streaming-pipeline liveness; native decoder internals; and the headline:
real Plaud bytes confirmed Ogg/Opus. Standard Ogg demonstrably does NOT
match h4's 512+1536·ch wire framing -- that honest mismatch is exactly
why C stays open.

## 10-13. Files / counts / gaps

New: `scripts/generate_r6s2_fixture.py`, `tests/fixtures/` (2 ogg +
manifest), `tests/test_r6_s2_audio_validation.py`, `r6-s2/`. Extended:
nothing in `emulator/` (no change needed). 214 tests green; all 8 prior
verifiers re-run green (see final report). No contradictions. Next gap:
an authentic captured recording to close claim C.
