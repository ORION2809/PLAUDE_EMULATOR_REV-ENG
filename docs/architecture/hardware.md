# Hardware layer — what the artefacts say about the devices

No Plaud hardware was ever in this project. Everything here is what Plaud's
own documents, binaries and template code assert about the devices, plus what
the BLE reconstruction (R1–R7) forces. Classes: **OFFICIAL_DOC**,
**OFFICIAL_SOURCE**, **PROVEN_BYTECODE**, **LINEAGE**, **INFERRED**, **UNKNOWN**.

## 1. The product line

| SN prefix | type string (cloud registry key) | product | Embedded support | source |
|---|---|---|---|---|
| `881` | `notepro` | Plaud Note Pro | yes | README, javap `resolveDeviceType`, every wrapper |
| `882` | `notepins` | Plaud NotePin S | yes | same |
| `880` | `notepin` | Plaud NotePin | **no** | Android template/wrappers (iOS template maps `883` — conflict, UNKNOWN which is right) |
| `888` | `note` | Plaud Note | **no** | Android template; riffado fixture `model: "888"` on `/device/list` |

The SDK's own `resolveDeviceType` knows only 881/882 and **defaults any other
prefix to `notepro` with a log warning** (PROVEN_BYTECODE) — effect on sn-sign
and bind for unsupported models UNKNOWN.

## 2. Specifications (OFFICIAL_DOC, `plaud-embedded/devices.md`)

| | Note Pro | NotePin S |
|---|---|---|
| battery | 30–50 h (endurance mode) | 20 h |
| microphones | **4 MEMS + 1 VPU** | 2 MEMS |
| connectivity | dual-band Wi-Fi + Bluetooth | dual-band Wi-Fi + Bluetooth |
| storage | 64 GB, "with encryption" / "encrypted-at-rest" | 64 GB |
| mode | "smart dual-mode (calls + in-person)"; "designed for phone calls" | in-person, hands-free wearable |
| max single file | 5 h | 5 h |

Compliance language in the docs: GDPR, SOC2, HIPAA practices; "full control
over what files are shared and synced". Marketing and positioning: executives,
sales, clinicians; use cases in the Embedded overview are healthtech
documentation, AI coaching, field sales.

**The VPU is confirmed as a first-class sensor.** The SDK exposes `VPU_GAIN`
(wire 19) and `VPU_CLK` (wire 30) among the 21 CommonType settings (ledger
§5.12), the iOS `BleAgent` has read/set pairs for them, and the docs list "1
VPU" on Note Pro only. This supports the research dump's "vibration conduction
sensor for phone calls" claim to the extent that a separate vibration pickup
unit exists and is tunable; how it captures call audio is not in any source.

## 3. Platform: TinnoTech, not ESP32

- The decompiled AAR contains `com.tinnotech.penblesdk.**` (`TntBleCommUtils`,
  `BleFile`, `BleDevice`); the native codec helper `libtnt_ble_utils.so`
  implements CRC-16/CCITT-FALSE; the Ogg writer tags OpusTags vendor
  `TinnoTech123456789012`. The recorder is a **TinnoTech Pen BLE ODM
  platform** (ledger §5.3; PROVEN_BYTECODE). The `88x` project codes and
  `portVersion` families come from the Tinno advertisement layout (ledger §6).
- The research dump's "device is ESP32-class" is **REFUTED for Note Pro /
  NotePin S**. Plaud's ESP32 repositories are prototypes on Espressif dev kits
  (see `firmware.md`): a "hi plaud" wake-word chatbot on an ESP32-S3-Korvo-2
  V3 and a LiveKit voice agent with audio diagnostics — a different product
  line or an experiment, with no Tinno, `881`, or NotePin mention.

## 4. On-device behaviour the artefacts constrain

| aspect | what is established | class |
|---|---|---|
| storage model | recordings are sessions keyed by `sessionId` = unix seconds; `BleFile{sessionId, fileSize, attribute, scene}`; `StorageRsp{free, total, duration}` — **units UNKNOWN** (U2) | PROVEN_BYTECODE |
| audio format on device | Opus 16 kHz, 20 ms frames, 80 B/frame/channel (32 kbps/ch CBR), 1–2 channels (`channels` in `BleFile`), optionally behind a 512-byte `PLAUD.AI` E2EE header; exact emitted shape UNKNOWN (U20) | PROVEN_BYTECODE |
| encryption at rest | binding "generates the key pair on-device"; E2EE header fields `userId, encryptType, counter, nonce, segment, algParams, keyCipher`; decrypt needs a private key PEM (which one — gen-key's — is UNKNOWN) | OFFICIAL_DOC + swiftinterface |
| ownership lock | firmware holds one `client_user_id`; refuses other handshakes (`TOKEN_NOT_MARCH`, or GATT-then-drop); force-clear via recovery; **`depair` changes the advertised MAC** (Plaud comments; INFERRED mechanism) | OFFICIAL_SOURCE (claims) + RUNTIME (host side) |
| recording control | start/stop/pause/resume with `scene` (templates pass 0); device-initiated events carry `reason`, `fileExist`, `fileSize`; `RecScene` {Normal, Interview, Classroom, Music, Meeting, Memo}; `RecMode` {Normal, NC}; `VadSensitivity` {Quality, lowBitrate, Normal, Aggressive} | swiftinterface names; semantics UNKNOWN |
| settings (21) | backlight time/brightness, language, auto-delete, VAD, scene, mode, VAD sensitivity, VPU gain, battery mode, mic gain, Wi-Fi channel, switch handler id, auto power-off, save raw file, auto record, auto sync, find-my, VPU clock, auto-stop-record (after charging), iBeacon wakeup; real values UNKNOWN (U22) | ledger §5.12 |
| power / charging | `battStatus` (level, charging) pushed unsolicited; OTA refuses on `G101_NO_CHARGING` / `POWER_LOW`; `AUTO_STOP_RECORD` (stop when charger attached?) UNKNOWN | PROVEN_BYTECODE |
| Wi-Fi | device hosts a SoftAP (SSID/passphrase derived from the SN, ledger §7) for fast transfer; connects as a WebSocket client to the phone on `:8081`; self-closes after ~3 heartbeats / ~2–2.5 min (claims); a separate "sync when idle" router-Wi-Fi provisioning exists (`get/set/deleteWifiSyncConfig`, `onWifiSyncUrl`) → destination UNKNOWN | PROVEN_BYTECODE + claims |
| LEDs / buttons | backlight enums; `SwitchHandlerID` (call-scene switching); "blue LED = AP mode" is the only LED behaviour stated | swiftinterface |
| OTA | `FotaInfo{uid, fromVersion, toVersion, thirdVersion, fileSize, crc16[, isSilent]}` (opcode 50), 80-byte data chunks, finish (51), per-pack ack (52), silent-OTA success status 13, 20 named refusal reasons; version string `V`/`T` + decimal code (`V66055` = 1.2.7) | PROVEN_BYTECODE |
| factory reset / misc | `restoreFactory`, `clearAllFiles` (opcode 104), `resetPassword` → `defaultPassword`, `resetFindmy`, `setDeviceActive` exist with **no caller and no documented effect** | swiftinterface / javap |
| BLE radio | GATT 1910/2BB0/2BB1, MTU 255 requested (517 granted by the AVD stack); advertised property bitmask on real hardware UNKNOWN (Q1); advertising branch UNKNOWN (U18) | ledger |

## 5. What would settle the hardware unknowns

One passive scan capture (U18, Q1), one connected session log (U2, U22, LED/
button semantics, settings values), one authentic recording transfer (U20),
and a firmware image (encryption-at-rest details, VPU capture path). None is
available in the corpus; none can be produced without a device.
