# Bluetooth layer — the device lifecycle as messages

The packet layer is **frozen**: framing, opcodes, handshake, sealed transport,
transfer and audio chain live in `docs/protocol-ledger.md` and
`docs/final-closure-report.md` and are not re-derived here. This document is
the *product-level* view of the same link: which exchanges implement each
lifecycle step, what the official docs say about them, and where the docs and
the bytecode disagree. Classes as in the ledger plus **OFFICIAL_DOC**.

## 1. Message map of the lifecycle

| lifecycle step | BLE exchange | ledger | class |
|---|---|---|---|
| discover | advertisement: manufacturer data with project code (`881`/`882`), SN, `bindInfo`, `portVersion`; SDK filters in software | §6 | SDK_PROVEN rules; live branch U18 |
| connect | GATT 1910 / 2BB0 notify-or-indicate / 2BB1 write; MTU 255 requested; stages `start → gatt_connect → set_notify → set_battery_notify → read_battery → set_data_notify → [pre_handshake] → first_handshake → [second_handshake] → sync_time` | §1, §4.1 | RUNTIME (R4-S3, R5-S1, R7-S12) |
| bind (legacy, advertised pv < 20) | k3 (opcode 1) carrying the token = JWT `sub` minus `client_user_` minus `-`, padded/truncated to 16/32; device answers l3 `{status, portVersion, timezone, …}`; `status 0` = `old_protocol_ok` → syncTime → `bleBind` | §5.5, §14 | RUNTIME_PROVEN byte-exact |
| bind (modern, pv ≥ 20) | FE10/FE20 chunks of the cloud `snSignature` → FE11 → FE12 secret package → RSA/ECB/PKCS1 → J/K/L → every frame sealed with ChaCha20-Poly1305 → k3/j3/l3 as above | §4.3, §5.5 | BYTECODE_PROVEN; values CRED-1 |
| post-bind configure | syncTime (4) → battStatus (9) → getState (3) → CommonSettings READ `ENABLE_VAD`, `REC_MODE` (8) → getStorage (6) → file list (26) | §5.1–5.4, §5.12 | RUNTIME_PROVEN order (R7-S12) |
| capability exchange | device push opcode 138 bitmap → host `01 8A 00 FF`; bit 3 selects AES-GCM **for Wi-Fi only** | §5.13 | BYTECODE + EMULATOR |
| record | start/stop/pause/resume with `scene`; device-initiated `RecordStart/Stop/Pause/Resume` events (`sessionId` = unix seconds, `reason`, `fileExist`, `fileSize`) | §5.7 (resume 22) | BYTECODE; scene semantics UNKNOWN |
| sync | fileList (26, paged, pv-dependent stride) → syncFile (28) HEAD → type-2 DATA → **EMPTY_PACKAGE (the completion, must precede TAIL)** → TAIL (29, u16 never verified); on a gap the client sends stopSync (29→30) immediately and restarts from its cursor; the 5 s stall path also ends in stopSync; deleteFile (30→31) | §5.8–5.10, §15 | BYTECODE_PROVEN + RUNTIME_PROVEN (R7-S13) |
| Wi-Fi fast transfer | setDeviceWiFi (opcode 10) → `bleWiFiOpen(ssid, pass, url)` → phone joins the device SoftAP → device dials the phone's WebSocket server `:8081` → handshake with the **same BLE handshake token** → JSON/binary PDUs (20 types) → batch delete → close; AES-GCM iff capability bit 3 else ChaCha20-Poly1305 | §7 | SOURCE-DERIVED, not emulated |
| OTA | `FotaInfo` (opcode 50: uid, from/to version, thirdVersion, fileSize, CRC-16, isSilent) → 80-byte data packets (pacing constants 40/160) → finish (51) → per-pack ack (52); silent-OTA success status 13; 20 named refusal reasons (no charging, low power, …); device restarts and re-advertises | §13 note; bytecode | BYTECODE (framing summary) |
| unbind | `depair(clear)` (opcode 5) → `DepairRsp{status}`; device then **changes its advertised MAC** (Plaud comments) | bytecode | claim on MAC |
| recover | `recoveryConnectBleDevice(historicalId)` = k3 with the historical token + force-clear flag (FE20 on pv ≥ 20; inert below) → on `bleBind(0)` depair → rescan → reconnect as current user | §14 | RUNTIME_PROVEN (host side) |
| device-to-cloud (unexplained) | Get/SetWebsocket (16/17, `url/serToken/devToken`), testWebsocket, Wi-Fi "sync when idle" configs, `sendApiToken` (api_token + CRC pushed to the device) | swiftinterface | destination UNKNOWN |
| misc | rate test (101, type 4); `clearAllFiles` (104); third version (54); `restoreFactory`; `resetPassword`; `resetFindmy`; device log file sync | bytecode | no caller / semantics UNKNOWN |

## 2. Connection outcome semantics (OFFICIAL_DOC, confirmed by RUNTIME)

- `bleConnectState 1` = GATT link up, **not** success; `0` = disconnected; `2`
  = failed. `bleBind(sn, status, protVersion, timezone)` with `status == 0` is
  the only success. A device locked to another account accepts GATT then drops
  (`status_1` = token mismatch, `TOKEN_NOT_MARCH`; Android reports `2` after
  ~10 s). `bleConnectStage(sn, stage, detail)` is "the only way to see which
  layer refused"; details include `ok`, `old_protocol_ok`, `status_<n>`,
  `sn_signature_empty`, `sn_signature_invalid`, `user_rsa_public_key_*`.
- **Doc vs code:** the docs present `bleBind.protVersion` as a protocol
  version; R7-S12 run 4 proved the facade loads both `protVersion` and
  `timezone` from the l3 **timezone byte** (ledger §14). The docs describe
  binding as "an encrypted handshake that generates the key-pair on the
  device" unconditionally; the bytecode shows a cleartext token-only path
  below advertised portVersion 20 and a cloud-issued RSA pair (private key
  returned by `gen-key`) above it (§4.3–4.6). The docs say recovery's
  force-clear "wipes the device's key material"; the flag only selects the
  FE20 marker and its firmware effect is UNKNOWN (U10).

## 3. Identity on the link

The handshake token is derived **locally** from the partner JWT (`sub` →
strip `client_user_` → drop `-` → 32-hex, padded/truncated to the width the
advertised portVersion selects). Recovery substitutes a historical
`client_user_id` from the cloud's `bind_history`. The SN signature (`sn-sign`)
is memory-only in the Android SDK ("needs a live sn-sign every launch"), the
RSA pair is persisted AES-wrapped in the Android Keystore. On pv < 20 none of
the cloud material is used on the link at all — the device decides on the
token alone (RUNTIME_PROVEN with a synthetic id against our emulator; **not**
evidence about real firmware).

## 4. What the link never carries

- No cloud bind message: cloud registration is an HTTP call the *app* makes.
- No transport ACK on the sealed path; no NAK/selective retransmit on transfer.
- No search, summary, tags, or account data — the link is device-only.
- No highlight/mark message identified, although the consumer product exposes
  device highlight-button marks (`mark_memo`); the SDK's `getRecMark*` raw
  methods exist (`IBleAgent`), opcode unrecovered.

## 5. Emulator coverage vs this map

Implemented and tested in `emulator/plaudsim`: discovery, GATT, legacy bind
(k3/l3, byte-exact vs the real SDK), post-bind configure incl. CommonSettings,
capability exchange, record-state responses, file list, transfer with
resume/gap injection, delete, sealed transport with synthetic keys, OTA and
Wi-Fi **not** implemented (framing recovered; see `firmware.md` for what OTA
tells us about the device).
