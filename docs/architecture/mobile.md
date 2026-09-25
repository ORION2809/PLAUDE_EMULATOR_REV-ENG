# Mobile layer — the partner app as Plaud publishes it

Scope: the product layer that runs on the phone above the packet. Sources are
the two Plaud-published template apps (`reference/plaud-org/plaud-sdk-public/`
`ios/PlaudTemplateApp/**`, 38 Swift files, and `android/app/src/main/java/com/plaud/template/**`,
40 Kotlin files), the SDK README, and the SDK facade as declared in the shipped
`arm64-apple-ios.swiftinterface` files. Evidence classes: **PROVEN_OFFICIAL_SOURCE**
(what the app does), **PROVEN_BYTECODE** (what the AAR does, from R1–R7),
**RUNTIME** (R7-S12), **CLAIM** (a Plaud engineer's comment or README sentence
about the cloud or firmware — recorded, not verified), **UNKNOWN**.

**What is missing, and why it matters:** there is no source for Plaud's own
consumer app. The template is the *partner* app of the Plaud Embedded platform.
Everything below describes what a partner app owns; it constrains but does not
describe the Plaud app (see `docs/architecture/cloud.md` §C and `web.md` for
what community clients show of the consumer product).

## 1. Shape of the app

| | iOS | Android |
|---|---|---|
| Identity | bundle `com.plaud.PlaudTemplateApp1`, v1.0.57 (57), iOS 14+, `UIBackgroundModes: bluetooth-central` (`ios/project.yml:19-71`) | `com.plaud.template`, versionName 1.0.56 (56), minSdk 21 / compileSdk 34 (`app/build.gradle`) |
| Architecture | UIKit programmatic, MVVM + Combine, `.shared` singletons; Mock managers exist but nothing instantiates them | Views + ViewBinding, MVVM + StateFlow/coroutines; `USE_MOCK=false` |
| Navigation | `SceneDelegate` → `MainTabBarController` (Home / Files / Settings, native bar hidden, 272×62 floating capsule that hides at nav depth > 1) or `Welcome → Scanning → ConnectSheet → Success` (`App/SceneDelegate.swift:14-21`, `UI/Main/MainTabBarController.swift:11-57`) | `WelcomeActivity` (LAUNCHER) routes *before inflating* to `MainActivity` (Home/Files/Settings fragments) or the onboarding chain; `RecordingActivity`, `DevicePanelActivity`, `FileDetailActivity`; `FastTransferSheet`, `FirmwareUpdateSheet` (`ui/onboarding/WelcomeActivity.kt:30-36`, `AndroidManifest.xml:32-70`) |
| Launch routing rule (both) | `(pairedDeviceSNs non-empty OR hasSkippedOnboarding) AND userId != nil` → main, else onboarding. `DeviceManager.configure(userId)` runs first. **PROVEN_OFFICIAL_SOURCE.** | |

Same authors, same tickets (`PLA2-3xx/4xx` in comments on both), same release
train. Parity is a design goal ("mirrors iOS/Android"), so where the two
platforms *differ* the difference is diagnostic: identical constants are app
policy; divergences are platform requirements or unfinished work (§9).

## 2. Identity and account state

- The only identity is the **partner-issued user JWT** (`USER_ACCESS_TOKEN`),
  injected at build time (iOS `PartnerConfig.xcconfig` → Info.plist; Android
  `local.properties` → `BuildConfig`) or pasted at runtime in Settings.
  `userId = JWT.sub` (falls back to a **random UUID** if the JWT is unparseable —
  and `initSDK` is still called with it; behaviour then UNKNOWN).
  `X-Client-Id = JWT.client_id`. No signature verification on the phone
  (`Common/JwtUtils.swift:7-32`, `common/JwtUtils.kt:10-38`). **PROVEN_OFFICIAL_SOURCE.**
- Token precedence per use: runtime override > plist/BuildConfig > legacy
  `PartnerToken` key. Replacing the token with a *different* `sub` switches
  account on relaunch; same `sub` → `PlaudDeviceAgent.setUserAccessToken` live
  (`Managers/DeviceManager.swift:127-135`).
- Who owns identity: the **partner backend** mints the user token
  (`POST /open/partner/users/access-token`, official docs); the app never sees
  a password, subscription, or quota. There is **no subscription, entitlement
  or usage logic anywhere in either template** (grep negative; the only gate is
  "Client ID or API key not configured" before transcription). **PROVEN (absence).**

## 3. Local persistence — what the phone owns

| Store | iOS | Android |
|---|---|---|
| Key-value | `UserDefaults`: `pairedDeviceSNs[]`, `activeDeviceSN`, `pairedDeviceNames{sn:name}`, `userId`, `autoSyncEnabled`, `hasSkippedOnboarding`, `serverDomainOverride`, `userAccessTokenOverride`, `apiKeyOverride`, `fastTransfer_neverShowAgain`; legacy `lastConnectedDeviceSN` migrated on first read (`Storage/RecordingStore.swift:10-34,100-133`) | `SharedPreferences "plaud_template_prefs"` (MODE_PRIVATE): `last_connected_device_sn` (doubles as active), `paired_device_sns` (Gson array), `paired_device_names`, `user_id`, `is_auto_sync_enabled` (default true), `fast_transfer_never_show`, token/api-key/server overrides, onboarding-skip (`storage/RecordingStore.kt:12-57,79-99`) |
| Recording metadata | `Documents/recordings.json`, Codable `[RecordingFile]`, atomic whole-file rewrite | `filesDir/recordings.json`, Gson, whole-file rewrite |
| `RecordingFile` row | `id` UUID (app), `sessionId` Int (device epoch **seconds**), `deviceSN`, `name` ("Untitled Recording"), `duration`, `createdAt` (= sessionId), `syncedAt?`, `localPath?` (**bare filename** relative to Documents), `summaryText?` (never produced), `transcriptJSON?` | same fields; `createdAt` = sessionId×1000 ms; `localPath` **absolute** under `cacheDir/export`; `duration` seconds, 0 until measured |
| Audio | MP3 mono in `Documents/` (BLE and Wi-Fi exports); WAV share-exports in `Documents/exports` | MP3 mono in **`cacheDir/export` — an OS-evictable directory**; SDK chooses the filename |
| Dead code | `audioFilePath(sessionId)` → `<sid>.ogg` never used | `filesDir/audio/<sid>.opus` helper and `clearAll()` never called |
| Database / cache / encryption at rest | none / none / none in the app (the SDK owns its own encrypted diagnostics logs) | same; `android:allowBackup="true"` with no rules, so prefs + `recordings.json` are auto-backup eligible while the audio in `cacheDir` is not (**INFERRED** platform behaviour) |

**Ownership verdict (PROVEN_OFFICIAL_SOURCE):** the phone owns the pairing list,
the active device, recording *names*, transcript text and the local audio copy.
It does **not** own the recording inventory (the device does — every file-list
reply *rebuilds* `recordings.json` from the device's list plus the locally
synced rows), and nothing about recordings is ever written to the cloud except
through the transcription upload. No cloud file list is fetched by the template.

## 4. Device state and the multi-device model

- Scan results are filtered to SN prefixes **881 (Note Pro)** and **882 (NotePin S)**
  at the single entry point; the app refuses to scan if the SDK reported
  `bleState(powered:false)`. Device type strings: iOS maps `883→notepin`,
  Android maps `880→notepin`, `888/else→note` — **the prefix table disagrees
  between platforms (UNKNOWN which exist)** (`Models/PlaudDevice.swift:38-46`,
  `managers/DeviceManager.kt:791-797`).
- **GATT connect is not success.** `bleConnectState 1` only means the link is up;
  the app's "connected" is `bleBind(status 0)`. A drop or fail
  (`bleConnectState 0/2/-1/-2`) while awaiting the handshake is treated as
  *"the device refused the connection — it may still be locked by another
  account"* and offers recovery (§6). **PROVEN_OFFICIAL_SOURCE**, and the stage
  callbacks were observed live in R7-S12.
- Connection state: `Disconnected | Scanning | Connecting(device) | Connected | Failed(msg)`.
- Multi-device: `pairedDeviceSNs` + `activeDeviceSN`; BLE holds one connection,
  so `switchDevice(sn)` = disconnect → settle (iOS 1.5 s / Android 1.5 s) →
  scan for target; "+ Add device" re-enters Scanning in add mode. Unpairing one
  device leaves the others.
- Auto-reconnect is **foreground-only** on both platforms: on becoming active,
  wait 2 s, then scan; an unexpected drop (not user, not OTA, not during Wi-Fi)
  starts a **30 s timer × 10 attempts** (initial delay iOS 3 s / Android 2 s).
  No background service, receiver, WorkManager, AlarmManager or notification
  exists on Android (manifest grep); iOS relies on `bluetooth-central`
  background mode only for the SDK's link. **PROVEN_OFFICIAL_SOURCE.**
- Declared but unused: `CONNECT_TIMEOUT` 30 s, `HANDSHAKE_TIMEOUT` 60 s (Android);
  the real connect timeout lives in the SDK ("~10 s", comment — CLAIM).

## 5. Recording state — device-authoritative

`RecordingState = Idle | Recording(sessionId, startedAt) | Paused(sessionId)`.
The app only *sends* `startRecord(scene 0)`, `stopRecord`, `pauseRecord`,
`resumeRecord` and *mirrors* the device's `bleRecordStart/Stop/Pause/Resume`
callbacks; `startedAt` = sessionId as epoch. A 4 s start-ack timeout restores
the button; Android additionally calls `refreshRecordState()` after 1.5 s to
correct drift (device stopped while the app was away, or vice versa).
**PROVEN_OFFICIAL_SOURCE.**

The README's "live PCM waveform" is only real on iOS, which calls
`PlaudDeviceAgent.syncFile(sessionId, start, 0)` on record-start and receives
`blePcmData` (640-byte, 16 kHz mono chunks per README). **Android never wires
PCM**: `RecordingManager.handlePcmData` has no caller, the listener has no
`blePcmData` override, and the AAR bytecode contains no `blePcmData` symbol —
the Android waveform is flat. **PROVEN_OFFICIAL_SOURCE + PROVEN_BYTECODE.**
Whether iOS's `syncFile(…, 0)` opens a live stream from the recording session
is SDK-internal (**UNKNOWN**).

## 6. Sync state — "the device is the inventory"

`SyncState = Idle | Syncing(p) | WiFiConnecting(openingHotspot|connectingWiFi|handshaking) | WiFiTransferring(p) | Completed | Failed(msg)`;
`SyncProgress{totalFiles, syncedFiles, currentFileName?, fileProgress, bytesPerSecond, isConverting}`.

1. **List.** `getFileList(startSessionId: 0)` 3 s after every bind (skipped while
   recording) and 1 s after a recording stops. Every reply *replaces* the store:
   synced local rows are kept, every device session becomes an unsynced row
   named "Untitled Recording" (`createdAt = sessionId`), and unsynced rows for
   sessions no longer on the device are dropped. Files on the device are by
   definition unsynced ("files belong to the phone after sync").
2. **Download.** Sequential `PlaudDeviceAgent.exportAudio(sessionId, dir, .mp3, 1 ch)`;
   the SDK performs the BLE transfer, E2EE decrypt and MP3 transcode (progress
   100 = local transcode); `markAsSynced` stores the path and measured duration.
   A failed file is logged and skipped; it re-appears on the next list.
3. **Delete after sync — unconditional app policy.** BLE: `deleteFile(sessionId)`
   immediately after each success, result only logged, never retried. Wi-Fi:
   one batched delete after the whole transfer (Android waits 15 s for the
   confirm, then a 20 s device-self-close wait; iOS 4 s). A user "Delete
   recording" is **local only**. **PROVEN_OFFICIAL_SOURCE.**
4. **Trigger policy differs (unfinished parity).** iOS: `fetchFileList` after
   connect *auto-downloads* new files, and record-stop always syncs (the
   auto-sync toggle card is built but never added to the Settings view).
   Android: connect only *lists* (silent), and record-stop downloads only if
   the toggle is on. The README's "auto-download on connect" is therefore
   **REFUTED for Android, SUPPORTED for iOS**.

**Wi-Fi fast transfer** (both): entry from the sync banner pill (iOS Home only —
the Files tab's pill has no handler; Android Home and Files); optional
"10x" sheet with never-show-again; iOS gates on CoreLocation; then
`setDeviceWiFi(open:true)` → `bleWiFiOpen(ssid, password)` (SSID = device
name, password SDK-filled, "SDK 1.0.9+ falls back to an SN-derived default" —
CLAIM) → `PlaudWiFiAgent.connectWifi(ssid, pass, 60)` (iOS needs the Hotspot
Configuration entitlement; Android asks the user to "tap Join") →
`wifiHandshake(0)` → `exportAudioViaWiFi` / `startWifiTransfer(userId, callback)`
(12-member callback: connection state, file list, batch started/progress/
completed, delete completed, error) → batched delete → close. BLE drops during
the session are swallowed (`bleDroppedDuringWiFi`), and the hotspot-close
command is re-sent on the next BLE bind if the link was lost. iOS runs a
185 s watchdog; the SDK retries the join for 180 s. Firmware facts asserted
only in comments (CLAIM): the device self-disconnects the Wi-Fi session after
3 heartbeats, times out in ~2–2.5 min, and refuses `openWiFi` with status 4
while streaming. The device MAC **changes** after depair (comment; corroborated
by the recovery flow needing a rescan).

## 7. Transcription — what the app drives and what it keeps

`TranscriptionState = idle | uploading(p) | submitting | processing(status) | completed([results]) | failed(msg)`.

Preconditions: local file exists; `X-Client-Id` (from JWT) and API key set;
Android refuses `.wav` with a message (comment: "upload API rejects wav with
FILE_TYPE_INVALID" — CLAIM). Flow, identical on both platforms
(**PROVEN_OFFICIAL_SOURCE**, request shapes CORROBORATED with the official
OpenAPI specs):

```
POST /open/partner/files/upload/generate-presigned-urls  {filesize, filetype}   Bearer user token
PUT  <PresignedUrl> × N   (chunk = ChunkSize ?? 5 MiB, sequential, ETag kept; 120 s timeout)
POST /open/partner/files/upload/complete-upload  {file_id, upload_id, part_list, filetype, file_md5}
POST /open/partner/ai/transcriptions/  {file_url: DownloadUrl, params: {transcribe:{language:"auto", model:"plaud-fast-whisper"},
                                        vad:{decode_silence:false}, diarization:{enabled:false, return_embedding:false}}}
GET  /open/partner/ai/transcriptions/{id}   every 5 s, max 240 polls (20 min); terminal FAILURE / REVOKED
```

Headers: Bearer for files, `X-Client-Id` + `X-Client-Api-Key` for AI. **No
retry, no backoff anywhere**; Android reads the whole file into memory. On
`SUCCESS`, `data.results[] {speaker_id, start, end, text, language}` is
JSON-encoded into `RecordingFile.transcriptJSON` and rendered as
"Speaker N · HH:MM:SS" paragraphs. `transcription_id` is **never persisted**:
a task started before process death is unrecoverable from the app.

**Summary is not implemented.** `summaryText` and "Copy Summary" exist, the
iOS Summary tab is hidden ("Summary not yet integrated"), `updateSummary` has
no caller, and the button copy "Transcribe and summarize" only transcribes.
**PROVEN_OFFICIAL_SOURCE.** Which cloud API would produce a partner-side summary
is **UNKNOWN** (none exists on the partner surface, see `cloud.md`).

Diarization is **off** in the template even though the API supports it; the
model is pinned to `plaud-fast-whisper`.

## 8. Device ownership, binding and brick recovery

Ownership is **dual** and the app orchestrates both halves (**PROVEN_OFFICIAL_SOURCE**):

| layer | what it holds | how the app touches it |
|---|---|---|
| firmware lock | the handshake must carry the bound `client_user_id`; the device refuses others | `connectBleDevice(bleDevice, deviceToken: userId)`; success = `bleBind(0)` |
| cloud registry | `is_bind` (tri-state) + `bind_history[]` per SN | `POST /open/partner/sdk/bind {type, sn}` after **every** successful handshake (403 → "already bound to another account" alert, but the app **stays connected**); `POST …/unbind` fire-and-forget before local `depair`; `GET …/binding?type&sn` for recovery |

**Brick recovery** (iOS `DeviceManager.swift:339-464`, Android
`DeviceManager.kt:1071-1195`): on a rejected handshake the app offers "Recover";
it `GET`s the binding, stops if `is_bind == true` ("that owner must unbind"),
dedupes `bind_history` (comment: "one entry per bind event, 50+ duplicates of
one id, live-verified" — CLAIM), takes ≤ 5 ids, and for each calls
`recoveryConnectBleDevice(bleDevice, historicalId)` with a 25 s handshake wait;
on success it `depair`s (5 s), rescans for 20 s because the MAC changes, and
reconnects as the current user. The SDK side of this — historical id →
`client_user_` strip → hyphen strip → k3 token, force-clear flag, no backend
call — is **RUNTIME-PROVEN** in R7-S12 against our emulator. Why the firmware
accepts the *current* user's sn-sign/RSA material together with a *historical*
id is not explained by any source (**UNKNOWN**; only relevant on pv ≥ 20).

Depair semantics differ: iOS `depair(clear:true)` on unpair and
`depair(clear:false)` during recovery; Android `depair(false)` everywhere with a
3 s fallback. The `clear` flag's meaning is SDK-internal (**UNKNOWN**).

## 9. OTA

- iOS delegates entirely to the SDK: `checkFirmwareUpdate` after every connect,
  `startFirmwareUpdate(progress:completion:)` with phases
  `downloading → installing → restarting → complete`; the app only holds
  `isOTAInProgress` to suppress reconnect and waits for the restart+reconnect
  before dismissing the sheet.
- Android **re-implements the version check itself**:
  `GET /open/partner/sdk/version/latest?type&sn&current_version=-1` with header
  `X-Device-Signature: PlaudDeviceAgent.getSnSignature()` (the sn-sign result),
  because — per comment — the AAR's own path needs `/api/oauth/sdk-token`,
  which "404s on platform-us". Response fields read: `version_code` (Long),
  `download_url`, `version_type` (V), `version_number`, `version_description`,
  `is_force`, `is_strong_guidance`, `file_md5` (the last two are ignored).
  Then facade `downloadFirmware` (MD5-verified) and `installFirmware` (BLE push,
  ~80 B packets per ledger; watchdog `fileLen/2 KBps + 5 min`). Version-code
  arithmetic `major*65536 + minor*256 + patch` with `V`/`T` prefix, install
  regex `^[VT]\d+$`; legacy short codes like `V0147` also appear (which models
  use which is **UNKNOWN**).
- This is the strongest **corroboration of the R7 cloud-inventory contradiction**:
  the AAR binary targets an `/api/...` surface whose token endpoint does not
  exist on the partner host, so the partner app routes around it. The official
  changelog later deprecates the `/api/workflows` queries (2026-09-14).

## 10. Settings screen, deletion, export, playback, logs

- Settings (Android, top to bottom): Automatic Sync toggle; Device Firmware
  version + Update; Server environment (Production `platform-us.plaud.ai`,
  Pre-release `platform-us-pre.plaud.ai`, Test `platform-test.plaud.ai`);
  User Access Token (Replace); API key; Export logs; Sign out. **Both templates
  default to the TEST environment** (comment: "cloud bind/unbind testing") even
  though the README documents `platform-us` — **unexplained**.
- Deletion: user delete removes the local row and file only (iOS's
  `removeItem(atPath: bareFilename)` almost certainly no-ops → orphaned audio,
  **unexplained**); device copies are deleted only by the sync policy (§6).
- "Export Audio" **re-exports from the device** via `exportAudio` (iOS WAV,
  Android MP3) instead of sharing the local file — even though the device copy
  was deleted after sync (**unexplained**).
- Playback: iOS `AVAudioPlayer` floating player; Android inline ExoPlayer with an
  `OpusRepair` helper (README's "floating player" is iOS-only).
- Logs: the SDK owns rotating, encrypted diagnostics logs and a
  `PlaudLogUploadManager` (auto-upload **disabled** by the template); the app
  raises rotation (iOS 50 × 50 MB × 7 d; Android overrides `logback.xml`) and
  exports manually. iOS routes `BleAgent mlog/wlog` to NSLog so handshake stage
  lines are visible.

## 11. Ownership map (who owns what)

| capability | owner | evidence |
|---|---|---|
| user identity, token minting, X-Client-Id/API key | partner backend (mint) / Plaud cloud (issue) | README; docs auth API |
| pairing list, active device, names, server env, auto-sync policy | **mobile** | RecordingStore (both) |
| recording start/stop/pause truth; file inventory; file bytes until synced; Wi-Fi hotspot lifecycle; ownership lock | **device** | callbacks mirrored only |
| BLE/Wi-Fi transfer, E2EE decrypt, MP3/WAV transcode, sn-sign/gen-key/metadata calls, log encryption | **SDK** (in-process) | facade calls only |
| delete-after-sync, local delete, transcript persistence and display | **mobile** | SyncManager, RecordingStore |
| cloud binding record, bind history, 403 exclusivity | **cloud** | sdk/bind, unbind, binding |
| brick-recovery orchestration | **mobile** | DeviceManager |
| firmware availability + binary | **cloud**; install by device; iOS check by SDK, Android by app | §9 |
| ASR, VAD, diarization, model choice | **cloud**; params chosen by mobile | TranscriptionManager |
| summary, search, memory/RAG, subscription, cloud recording library, web app | **absent from the partner app** | grep negative |

## 12. Recording lifecycle as the app sees it

```
device flash ──getFileList──▶ unsynced row (recordings.json, localPath=null)
   │  (3 s after bind; 1 s after record stop)
   ▼ startSync / auto (policy §6.4)
exportAudio(sessionId, dir, MP3, 1ch)  [SDK: BLE or Wi-Fi, decrypt, transcode]
   ▼ onComplete
local-synced (localPath, syncedAt, duration)  ──deleteFile(sessionId)──▶ device copy gone (fire-and-forget)
   ▼ user "Generate"
presign → PUT parts → complete-upload → DownloadUrl (24 h claim)
   ▼
POST transcriptions (plaud-fast-whisper, auto, diarization off) → task id (not persisted)
   ▼ poll 5 s × 240
transcriptJSON stored locally; cloud copy/retention of the audio and transcript: UNKNOWN from the app
```

Retry and failure handling at every step: none (single attempt, error → `Failed`),
except sync which continues to the next file and re-lists failures on the next
fetch.

## 13. Cross-platform parity table (differences = evidence)

| behaviour | iOS | Android | reading |
|---|---|---|---|
| auto-download on connect | yes | list only | unfinished parity (README follows iOS) |
| auto-sync toggle | dead (card never shown, always on) | honoured, default on | |
| PCM waveform | fed via `syncFile(...,0)` + `blePcmData` | never fed; no callback in AAR | Android AAR lacks the path |
| firmware check | SDK `checkFirmwareUpdate` | app-side `/sdk/version/latest` + `X-Device-Signature` | AAR path needs a token endpoint absent on the partner host |
| share/export format | WAV | MP3 (refuses to transcribe WAV) | |
| sign out with other paired devices | always to Welcome | stays in app | |
| depair flag | `clear:true` (unpair) / `false` (recovery) | `false` + 3 s fallback | |
| initial reconnect delay | 3 s | 2 s | |
| Wi-Fi delete-confirm wait / pre-hotspot wait | 4 s / 3 s after `bleWiFiOpen` | 15 s (PLA2-309) / 1.5 s before | |
| device-type prefix map | 883→notepin | 880→notepin, 888→note | conflicting; UNKNOWN |
| audio location | Documents (durable) | cacheDir (evictable) | |
| localPath | bare filename | absolute path | |

Identical on both (→ app **policy** constants): recovery 25 s / 5 s / 20 s /
5 attempts; auto-reconnect 30 s × 10; foreground reconnect 2 s; post-connect list
3 s; record-stop sync 1 s; start-ack 4 s; device-close fallback 20 s; BLE settle
1.2–1.5 s; transcription params and poll cadence; storage schema; delete-after-sync.

## 14. Unexplained behaviours (observable, no source explains)

1. Both public templates default to `platform-test.plaud.ai`.
2. "Export Audio" re-exports from the device after the device copy was deleted.
3. iOS local delete targets a bare filename (likely orphaning audio).
4. Android keeps synced audio in an evictable cache dir while the durable helper is dead code; `allowBackup` may restore rows whose audio is gone.
5. `bleDeleteFile` failures are never retried → device storage reclamation path unclear.
6. `transcription_id` never persisted; no cloud list endpoint used → whether results are retrievable later is unknown.
7. `current_version=-1` always sent to the version endpoint; `is_force`/`is_strong_guidance` ignored.
8. `bind_history` is described as *not client-scoped* — the cloud may return other partners' `client_user_id`s to any bearer for that SN (authorisation model unexplained).
9. On a 403 from `sdk/bind` the app stays BLE-connected: which layer enforces exclusivity is unclear.
10. Comments cite documents absent from the corpus (`sdk-requests-android-parity.md`, `PARTNER_API_GUIDE.md`, a "lifecycle guide", tickets PLA2-309…401).
11. `blePenState` fields (`privacy`, `keyState`, `uDisk`, `findMyToken`, `hasSndpKey`, `deviceAccessToken`) and the magic `4099 (0x1003)` = "currently recording" are not explained by any source.
12. The SDK exposes `uploadRecording(sn:sessionId:duration:)` and `uploadLogsAfterRecording` — a cloud recording/log upload path the template never uses; destination UNKNOWN.
13. A `marks`/highlight concept exists on the consumer side (`web.md`) but the partner app has no highlight UI or callback.
