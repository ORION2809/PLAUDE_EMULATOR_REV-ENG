# Firmware layer — what can and cannot be reconstructed

No Plaud recorder firmware image exists in the corpus, and none was sought
from outside it. What *is* legitimately available splits into three unequal
piles: (1) what the shipped SDK proves about the recorder's firmware
behaviour from the outside; (2) two ESP32 forks that are Plaud engineers'
**voice-agent prototypes on Espressif dev kits**, fully reconstructable but
unrelated to the recorder; (3) names of other hardware lines that leak
through OTA status strings. Classes: **PROVEN_BYTECODE**, **OFFICIAL_SOURCE**
(incl. shipped iOS headers/swiftinterface), **OFFICIAL_DOC**, **LINEAGE**,
**INFERRED**, **UNKNOWN**.

## 1. The recorder (Note Pro / NotePin S / NotePin / Note) — outside-in

| firmware behaviour | what the artefacts establish | class |
|---|---|---|
| platform / radio | TinnoTech Pen BLE SDK (`com.tinnotech.penblesdk`, 1 301 references; JNI `tntGetCrc/tntGetFileCrc`, `ogg_opus_build_tinno_buf`; OpusTags vendor `TinnoTech123456789012`). The SDK maps the advertised BLE company ID to exactly two vendors: **70 = MediaTek, 89 = Nordic**; no Espressif/ESP32 string exists in the AAR or its native libs. | PROVEN_BYTECODE |
| which radio real units use | UNKNOWN — the SDK never gates on it; one scan capture would tell (U18/U1). | — |
| identity & lock | firmware holds one bound `client_user_id`; refuses other handshake tokens (`TOKEN_NOT_MARCH` / GATT-then-drop); force-clear on recovery; `depair` (opcode 5) → **advertised MAC changes** (Plaud comments). pv ≥ 20 units verify a cloud `snSignature` and exchange an RSA-wrapped secret (J/K/L) for ChaCha20-Poly1305 sealing. | BYTECODE (host side) + claims |
| storage | 64 GB, "encrypted"; sessions keyed by unix-second `sessionId`; `StorageRsp{free,total,duration}` units UNKNOWN; `AUTO_DELETE_RECORD_FILE`/`AutoClear` and `clearAllFiles` (104) exist. | OFFICIAL_DOC + BYTECODE |
| audio | Opus 16 kHz, 20 ms, 80 B/frame/ch, 1–2 ch; Ogg or raw packets; optional 512-byte `PLAUD.AI` E2EE header (fields listed in `audio.md`); `SAVE_RAW_FILE` setting suggests a second artefact. Which shape is emitted: U20. | PROVEN_BYTECODE (what the SDK accepts) |
| capabilities | opcode-138 bitmap (bit 3 = Wi-Fi AES); `portVersion` families (`≥ 5` new battery service, `≥ 7` session ids in DATA, `≥ 9` 32-char token, `≥ 20` sealed); "V20+ = NotePro and newer" (docs). | PROVEN_BYTECODE / OFFICIAL_DOC |
| device status bitfield (32-bit, `bleDeviceStatus`) | bit0 BLE file transfer · bit1 Wi-Fi fast transfer · bit2 Wi-Fi testing · bit3 wired transfer · bit4 USB-drive mode · bit5 Wi-Fi cloud upload · bit6 "pan" cloud upload · bit7 BLE-OTA download · bit8 Wi-Fi OTA — i.e. the firmware has **direct-to-cloud upload and USB-disk modes** the partner product never exposes. | OFFICIAL_SOURCE (iOS header) |
| device-to-cloud channel | `Get/SetWebsocket` (opcodes 16/17; `url/serToken/devToken`), `testWebsocket`, "sync when idle" router Wi-Fi configs with SDK-shipped UI pages, `sendApiToken` (api_token + CRC pushed to the device), `onWifiSyncUrl`. Destination and auth: UNKNOWN. | swiftinterface / BYTECODE |
| telemetry counters | `TntStatUserItem{rtRecCnt, offRecCnt, recTotalDuration, lowPowerOffCnt, uDiskCnt, earphoneCnt, recUploadCnt, keyPressCnt, voicePlayCnt, mp3PlayCnt, rereadCnt, fotaCnt, …}` — playback/earphone/USB-disk counters imply Tinno pen-line features not marketed on Plaud recorders. | PROVEN_BYTECODE |
| markers | `getMarking(sessionId) → markList:[UInt32]`, `getRecordMarkingTags(uid, start, end)` → `BleRecordMarkingTag{timestamp, type, status}` — the device side of press-to-highlight. | swiftinterface |
| OTA | cloud `version/latest` (`X-Device-Signature`) → phone downloads + MD5 → BLE push: `FotaInfo` (50: `uid, fromVersion 'T0012'/'V0012', toVersion, thirdVersion (G101 only), fileSize, CRC-16/CCITT-FALSE[, isSilent]`) → 80-byte packets (pacing 40/160) → per-pack request (52) → verdict (51) → device restarts and re-advertises; `SyncOtaFileInfo` (151) for Wi-Fi OTA; status table 0–12 (Android) + 13 "silent OTA, upgrade on next reboot" (iOS); 20 named refusal reasons incl. `G101_NO_CHARGING`, `POWER_LOW`, recording/USB busy; **"firmware updates wipe recordings"** (docs). Version strings `V`/`T` + decimal (`V66055` = 1.2.7) or legacy short codes. | PROVEN_BYTECODE + OFFICIAL_DOC |
| factory / reset | `restoreFactory`, `resetPassword → defaultPassword`, `resetFindmy`, `setDeviceActive` exist with no caller; effect UNKNOWN. | swiftinterface |
| Wi-Fi | SoftAP `PLAUD`+last-4-of-SN / WPA2 pass = last 8 of SN; device is the WebSocket *client* to the phone's server on `:8081`; self-closes after ~3 heartbeats / ~2 min (claims). | ledger §7 + claims |

**Reconstructable from legitimate artefacts:** the complete *host-facing*
contract (every opcode, the OTA transfer protocol, settings, status bits,
telemetry schema). **Not reconstructable:** the image format, bootloader,
partitioning, key storage, the VPU capture path, and every value real
firmware returns. A firmware image was neither present nor pursued.

## 2. Other hardware lines leaking through the SDK

| name in artefacts | where | what it implies |
|---|---|---|
| **G101 "glasses"** | OTA status strings ("G101 glasses: charging-only upgrade, low battery, OTA_MODE"), `thirdVersion` OTA field "G101 project, else 0", iOS `GlassProtocol` / `readGlassData` / `GlassData{year, month, day, time}` | a glasses-form-factor device on the same Tinno stack |
| **Heili (黑黎) three-way-switch pen** | OTA status 255 "not in recording mode" | a recording pen with a physical mode switch |
| **"NiceBuild"** | the AAR's `sdk.NiceBuildSdk` class **and** the ESP32 prototype's second wake word "hi nicebuild" | an internal codename spanning the recorder SDK and the voice-agent prototype — meaning UNKNOWN |
| `avcToNoiseReductionWav(sound_plus:)` + `setSoundPlusToken(licenseKey:)` | iOS `JXFileDecoder` | a licensed third-party noise-reduction library ("SoundPlus") in the phone-side toolchain |

## 3. The ESP32 prototypes (LINEAGE; fully readable, not the recorder)

`xiaozhi-esp32` (Plaud fork = upstream v2.0.3; upstream now 2.5.0, so the
manifest's 376 "Plaud-only" paths are ~350 relocated upstream files; the true
delta is < 10 files):

- board: Espressif **ESP32-S3-Korvo-2 v3** dev kit (`CONFIG_BOARD_TYPE_ESP32S3_KORVO2_V3=y`);
  an earlier build (`sdkconfig.backup.wrong_board_20251010`) targeted
  bread-compact-wifi + SSD1306 OLED; **no Plaud board definition, no pin map**;
  Bluetooth compiled out.
- audio: ES8311 DAC + ES7210 ADC over I2S (MCLK 16, WS 45, BCLK 9, DIN 10, DOUT 8),
  PA GPIO48, 24 kHz codec resampled to 16 kHz Opus 60 ms frames (complexity 0),
  AEC reference channel; 16 MB flash, OCT PSRAM; partitions `nvs/otadata/phy_init/ota_0/ota_1 (0x3f0000 each)/assets (8 MB SPIFFS)`.
- Plaud-authored: MultiNet phoneme wake words **"hi plaud"** (4 spellings) and
  **"hi nicebuild"** (2), threshold hard-coded 0.15 (config says 0.5),
  debug logging every 100 frames; OTA/config server moved from upstream's
  `api.tenclass.net` to a private LAN `http://10.1.164.12:8002/xiaozhi/ota/` and
  WebSocket `ws://10.1.164.12:8000/xiaozhi/v1/` — the same ports Plaud's
  `live-agent` server fork documents.
- OTA: upstream xiaozhi's `esp_ota_begin/write/end` with HMAC-SHA256 activation
  (efuse serial + `HMAC_KEY0` challenge); device-side MCP tools
  (`self.get_device_status`, speaker volume, screen brightness/theme, camera).

`client-sdk-esp32` (LiveKit ESP32 SDK 0.3.0 snapshot; upstream 0.3.11): a
`voice_agent` example with a dynamic-token path (`POST cloud-api.livekit.io`
with an `X-Sandbox-ID`, joins `demo-room` with a `hospital_assistant` agent),
six Plaud-authored audio diagnostic/monitor modules and ESP console test
commands, and a committed office test Wi-Fi credential (existence noted; value
not reproduced). Also on an Espressif Korvo board.

**Verdict on "device is ESP32-class" / "xiaozhi as reference firmware":**
REFUTED for the recorder (Tinno platform; MediaTek/Nordic company IDs;
Bluetooth is even compiled out of the ESP32 fork), PARTIAL for the existence
of an ESP32 *voice-companion* prototype line (see `ai.md` §3 for its server).

## 4. Unknowns that need hardware

U1/U18 (advertisement branch, company id), U2 (storage units), U8 (`0x02`
at k3 offset 3), U9/U10 (MTU, force-clear effect), U13 (TAIL CRC coverage),
U20 (emitted audio shape), U22 (settings/capability values), the device-to-
cloud upload path (`pan`/Wi-Fi cloud, `sendApiToken`), G101/Heili identities,
what `depair` does to the MAC, and every "claims" row above.
