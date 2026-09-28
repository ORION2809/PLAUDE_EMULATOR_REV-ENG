# R7-S14: the official SDK's Wi-Fi transfer against our Wi-Fi device (RUNTIME, synthetic-only)

Verdict: **blocked at the SoftAP join, before the phone's WebSocket server
exists.** The unmodified SDK (`plaud-sdk.aar`, same build as R7-S12/S13)
bound to our BLE emulator, sent OpenWiFi (opcode 10), took the answer, and then
asked Android to join the pen's hotspot with
`ConnectivityManager.requestNetwork(WifiNetworkSpecifier{SSID "PLAUD0001",
WPA2 "10000001"}, cb, 30000)`. The AVD cannot see that network (only
`AndroidWifi`), so after 30.0 s the request was released, the SDK reported
`onError(1003, "Failed to connect to device WiFi")` and never started its
server on 8081. Our Wi-Fi pen, dialling the AVD's 8081 through `adb forward`,
never got a WebSocket, and **not one Wi-Fi PDU was exchanged in either
direction** in any run. Nothing on the Wi-Fi protocol (SayHello, Handshake,
file list, FileSync, content, WifiClose) was proven at runtime by this task.

How far, in the task's terms: *BLE bind ✓ → OpenWiFi (opcode 10) ✓ → SSID and
passphrase derived ✓ → join requested ✓ → **blocked at
`ConnectivityManager.requestNetwork` (WifiConnectionManager.txt:103-104) because
the AVD's radio environment has no `PLAUD0001` AP** → server started ✗ →
handshake ✗ → file list ✗ → file ✗.* Per the task, the SDK and the OS were not
patched and that path was stopped there.

Along the way the runs **exposed and fixed one emulator bug** (opcode 10 mode 0
read as "hotspot off", §6) and found five discrepancies between the SDK and
our docs (§5).

> **Disclosure: Plaud cloud contact (unintended).** Every run's
> `PlaudDeviceAgent.initSDK(context, <synthetic JWT>, "api.plaud.ai")`, the
> same call the R7-S12/S13 drivers make, made the SDK itself POST
> `https://platform-jp.plaud.ai/developer/api/open/partner/sdk/gen-key` with
> `Authorization: Bearer <synthetic JWT>`; the server answered **401
> `{"detail":"ACCESS_TOKEN_INVALID"}`** (e.g.
> `logcat-run1b-transfer-shipped.filtered.log`, lines 33-102). That is one
> request per app start, six in total (runs 1, 1b, 2, 3, 4, 5; run 1's log line
> was lost to the ring buffer). The token is synthetic, nothing authenticated,
> and no response data was used. This was **not** anticipated: R7-S12's
> "cloud-free" analysis covers `recoveryConnectBleDevice` only, and
> `initSdk` fires `gen-key` whenever the init token is non-blank
> (`NiceBuildSdk.txt:2295` `initSdk`, `:2440-2465` `isBlank` →
> `setUserAccessToken` → `hasUserAccessToken` → "Partner API: Token 可用，正在获取
> RSA 密钥对..."). The R7-S13 evidence shows the same request
> (`r7-s13-evidence/logcat-run11-shipped-default-export.filtered.log`), so this
> was already happening in the rig before this task. It contradicts the task's
> "no Plaud cloud calls" rule and `cloud-endpoint-inventory.md`'s "none
> exercised" (§5, D7). **Before any further AVD run, do one of these:** pass
> a blank init token (tested 25 Sep, offline, on the pull driver only: no `gen-key`
> line, byte-exact pull — `r7-s14-evidence/blank-token-offline-check/`; it also empties the
> Wi-Fi handshake token, which our pen accepts), or cut the AVD's egress
> (untested, e.g. a DNS sinkhole).

## 1. Topology and driver

```
API-34 AVD (mivi_test_34) ── netsimd ── bumble android-netsim ── r7/wifi_capture_device.py
  com.plaud.template (r7/android-app)                              WifiCapturingPeripheral = PlaudPeripheral.for_real_sdk (pv 7,
    debug/WifiCaptureActivity.kt                                     SN 8810000001, project 881), file_table: session 1700000000
      recoveryConnectBleDevice(dev, "SYNTH-HIST-0001")               = tests/fixtures/r6s2_16k_mono.ogg (11 271 B, 0f45367b…48c3d)
      bleBind(0)                                                   + wifi_device_factory -> CapturingWifiDevice (plaudsim WifiDevice)
      mode=transfer: stopSyncFile -> 1.5 s -> startWifiTransfer        serving the same table, dialling ws://127.0.0.1:18081
                     READY -> getWifiAgent().getFileList()             (90 x 1 s), every frame/dial event -> capture-<run>.json
                     list -> exportAudioViaWiFi(OPUS)
                     error -> guards -> endWiFiTransfer + setDeviceWiFi(false)
      mode=open:     setDeviceWiFi(true) ... 8 s ... setDeviceWiFi(false)
  adb -s emulator-5554 forward tcp:18081 tcp:8081   (host -> the phone's java-websocket port)
```

Per run (`r7-s14-evidence/run.sh`): `am force-stop`, `pm clear`, re-grant
BLUETOOTH_SCAN / BLUETOOTH_CONNECT / ACCESS_FINE_LOCATION (+
NEARBY_WIFI_DEVICES in run 5), a fresh device process, the forward, `logcat
-G 16M` with a streamed `logcat -v threadtime` for the whole run, `am start`,
two read-only probes inside the join window (`/proc/net/tcp{,6}` rows for port
0x1F91, the top activity, the app's specifier requests in `dumpsys
connectivity`, `cmd wifi status`, `cmd wifi list-scan-results`, a
screenshot), then collection. **Nothing tapped, approved or configured any
network.** The CHANGE_NETWORK_STATE permission that `requestNetwork` needs
comes from the SDK AAR's own manifest (granted in every run, `perms-*.txt`).

## 2. Bytecode facts (javap; `…/wifi/` = `build/evidence/javap/sdk/ble/wifi/`)

### 2.1 What `startWifiTransfer`'s String is

| step | fact | citation |
|---|---|---|
| facade | `PlaudDeviceAgent.startWifiTransfer(String, cb)` null-checks its first parameter under the name **`sn`** and forwards it | `sdk/PlaudDeviceAgent.txt:1202-1213` (`"sn"` :1206) |
| NiceBuildSdk | the same value is named **`userId`**; no agent → "WiFi agent not available." → false | `sdk/NiceBuildSdk.txt:3289-3312` (:3294) |
| agent | `WifiAgentImpl.startWifiTransfer`: already active → "WiFi transfer already active", false; else store callback, compute the **token from `resolveHandshakeToken("")`** (not from the argument), launch `LambdaTaskRunner(this, cb, userId)`, return true | `…/wifi/WifiAgentImpl.txt:2254-2338` (:2267, :2278, :2319) |
| use | the String reaches only `WifiConnectionManager.connectToDeviceWifi(ssid, pass, userId)` | `…/wifi/LambdaTaskRunner.txt:138` |
| dead | API ≥ 29: `connectWifiAndroidQ` overwrites local 3 (the userId) with a log string before any use; API < 29: `connectWifiLegacy` passes only ssid/pass to `AriesTaskRelay` | `…/wifi/WifiConnectionManager.txt:40-47`, `:164-179` |

**The argument is null-checked and otherwise unused.** The template passes
`RecordingStore.userId` (`SyncManager.kt:201,208`). At runtime our argument
`SYNTH-WIFI-USER-0001` appears in no SDK log line, system log line or wire
byte (runs 1b, 2, 5). That is an absence check, consistent with the bytecode.

### 2.2 What happens between OpenWiFi and the server

`LambdaTaskRunner.invokeSuspend` (`…/wifi/LambdaTaskRunner.txt:64-220`), in order:

1. `updateState(CONNECTING)` (:64).
2. `checkPrerequisitesInternal`: BLE connected (`p7.f().d().U()`), else
   `onError(1001, "Prerequisites not met")` (:68-72; `WifiAgentImpl.txt:138-150`).
3. `saveEncryptionKeys`: copies BLE `z.J/K/L`, `sendSeq = z.M`, `recvSeq = 0`,
   algorithm AES-GCM iff `z.w()` (:79; `WifiAgentImpl.txt:156-271`).
4. `openDeviceWifi` → `KappaValueObject` → `t3.d(false, …)` → `q.a(false, null)`
   → `new i4(0, null)` = **`01 0a 00 00`** (`KappaValueObject.txt:57`
   `iconst_0`, `:64`; `ALL.txt:45963-45997`). `IotaStateTracker`: `j4.status
   != 0` → "openWiFi failed with status:" → 1002; status 0 → SSID =
   `BleDevice.getWiFiName()`, pass = `getWiFiPwd()`, falling back to
   `calculateWifiName/Password` only when null; **the passphrase in the j4
   answer is ignored** (`IotaStateTracker.txt:27-75`, `:110-136`).
5. **`WifiConnectionManager.connectToDeviceWifi(ssid, pass, userId)`** on
   `Dispatchers.Main` (`WifiConnectionManager.txt:228-241`) →
   `PiscesValueRelay`: `SDK_INT >= 29` → `connectWifiAndroidQ`, else
   `connectWifiLegacy` (`com/plaud/sdk/internalimpl/PiscesValueRelay.txt:35`,
   `:76-124`). `connectWifiAndroidQ`: `WifiNetworkSpecifier.Builder
   .setSsid(ssid)`, `.setWpa2Passphrase(pass)` iff non-empty,
   `NetworkRequest.Builder.addTransportType(TRANSPORT_WIFI)
   .removeCapability(NET_CAPABILITY_INTERNET).setNetworkSpecifier(..)`,
   **`requestNetwork(request, cb, 30000)`** (`WifiConnectionManager.txt:59-104`).
   The callback resumes TRUE on `onAvailable` (stores the `Network`) and FALSE
   on `onUnavailable` ("WiFi network unavailable")
   (`WifiConnectionManager$connectWifiAndroidQ$2$networkCallback$1.txt:19-94`).
   FALSE → `onError(1003, "Failed to connect to device WiFi")` and return
   (`LambdaTaskRunner.txt:150`). The state stays CONNECTING: only the
   exception path sets ERROR (:207).
6. Only after TRUE: CONNECTED, HANDSHAKING (:157-161), then
   `startWebSocketAndHandshake` (:169) → `CapricornusStateRelay`:
   `WebSocketOperation.startServer()` (`CapricornusStateRelay.txt:45-46`), then
   wait for a session id with a 30 s watchdog (`SagittariusProcessorRelay.txt:52`
   `30000l`, :65) → empty → "Handshake timeout" (`CapricornusStateRelay.txt:121`)
   → 1004. Success → `isActive = true` (:181).

**So the join is mandatory and precedes the server; the server is not started
regardless.** On API 34 the join needs the system to find and connect a
network matching the specifier, which on real devices includes the user
approving the Settings "network request" dialog.

### 2.3 Which address and port the server binds

`WebSocketOperation.startServer`: "Starting WebSocket server on port 8081",
`new InetSocketAddress(8081)`, the **wildcard address**, then
`setReuseAddr(true)` and `start()` (`…/wifi/WebSocketOperation.txt:162-220`,
:174, :180-181, :193, :199). Nothing binds the process or the socket to the
joined `Network`: there is no `bindProcessToNetwork`, `Network.bindSocket` or
`getSocketFactory` use on this path anywhere in `ALL.txt` (the one
`getSocketFactory` hit, ALL.txt:71101, is `SSLContext`). Had the server
started, the `adb forward` to tcp:8081 would have reached it, so design (a)
stays valid for a rig where the join can succeed.

### 2.4 How the pen's handshake token is checked

* **The phone does not gate on the pen.** On SayHello (type 2) the phone logs
  "Device says hello: SN=…, Version=…, pVer=…". If the SayHello token is
  non-empty and not `equals(ignoreCase)` to its own token, it only logs
  `"Token mismatch! device=… vs sdk=… —— 握手会被设备拒绝"` ("the device will
  reject the handshake"). Either way, if it has no session yet, it sends
  `HandshakeRequest(padEnd(token, 32, '0'), stamp)` via `EtaConfiguratorNode`
  (`…/wifi/WifiAgentImpl.txt:1376-1417`; `EtaConfiguratorNode.txt:23-56`).
* **The pen decides.** HandshakeResponse status 0 → session id (or the token)
  → READY → `onHandshakeCompleted`; any other status → `onError(1006,
  "Handshake failed with status: N")` (`WifiAgentImpl.txt:1428-1505`, READY
  :1460, `sipush 1006` :1505).
* **The phone's token** is `NiceBuildSdk.resolveHandshakeToken("")`: the JWT
  `sub` of the partner user-access token (prefix `client_user_` and `-`
  removed), else the bind token (here `""`), else empty with "generateToken:
  handshake token is EMPTY" (`WifiAgentImpl.txt:2278-2292`;
  `NiceBuildSdk.txt:61-219`). RUNTIME (all transfer runs): "generateToken:
  using BLE handshake userToken (len=21)" and "resolveHandshakeToken: using JWT
  userId: SYNTHETICHISTORICALID", i.e. derived from our synthetic init JWT.
  **The BLE `k3` token of the same session was `SYNTHHIST0001` (13
  characters)**, because the recovery path uses the historical id
  (`PlaudDeviceAgent: recoveryConnect … handshakeToken=SYNTHHIST0001`), so on
  this path the Wi-Fi token is *not* the BLE token (D3).

## 3. Runtime design

The bytecode rules out option (a) as specified: the SDK does not start its
server without the join. That leaves (b). The join cannot succeed on this AVD
without changing the SDK, the OS, or the simulated radio environment. Scan
results in every probe show only `AndroidWifi` (open, `[ESS]`). The runs
therefore drove the real flow up to the block and recorded the exact
blocking call, its timing and its visible side effects. The pen was still
started (runs 2-5) and dialled the adb-forwarded 8081 throughout, so a server
would have been caught had one appeared.

## 4. Runs

| run | device | driver | outcome |
|---|---|---|---|
| 1 | as shipped (pre-fix) | transfer | Same as 1b. The streamed logcat was not yet in `run.sh`, and `logcat -d` kept only the tail (ring buffer; `logcat-run1-…TRUNCATED.log`). Capture, probes and screenshots are valid: specifier REGISTER 12:57:31.073, RELEASE 12:58:01.186. |
| 1b | as shipped (pre-fix) | transfer | BLE `010a0000` → emulator answered `010a0000` (status 0, **no passphrase**, and **no Wi-Fi device started**: mode 0 read as "off"). SDK: "WiFi opened, name: PLAUD0001" → "Connecting to device WiFi: PLAUD0001" → "WiFi connection request sent for: PLAUD0001"; ConnectivityService `requestNetwork … WifiNetworkSpecifier [SSID LITERAL: PLAUD0001]` 13:00:15.095; Settings `NetworkRequestDialogActivity` started 13:00:15.119; request released 13:00:45.118; "WiFi network unavailable" → `onError(1003)` at +30.124 s. No "Starting WebSocket server" line; no :1F91 socket at either probe. Guards: `getFileList()` → false ("Cannot get file list - WiFi not ready"); `exportAudioViaWiFi` → "WiFi is not ready. Current state: CONNECTING". `endWiFiTransfer()` wrote nothing over BLE (agent not active); `setDeviceWiFi(false)` → `010d00` → `010d0000`, no app callback. |
| 2 | **fixed** | transfer | `010a0000` → `010a0000 3130303030303031` (status 0 + "10000001"); the pen started at once and dialled `ws://127.0.0.1:18081` 31 times from +0.01 s to +30.3 s, each `InvalidMessage('did not receive a valid HTTP response')` (the forward accepts and drops; nothing listens on the AVD). SDK outcome identical: 1003 at +30.051 s, no server. `setDeviceWiFi(false)`'s opcode 13 cancelled the dial (attempt 32, `ble_close_wifi`). |
| 3 | fixed | open | `setDeviceWiFi(true)` → **`010a0001`** (mode 1) → `010a0000 3130303030303031` → `onCallback: 10` → `bleWiFiOpen(0, "", "", "")` 51 ms later. The facade passes empty strings, so neither SSID nor passphrase reaches the app. The Wi-Fi agent stayed NONE: no join, no server. `setDeviceWiFi(false)` → `010d00` → `010d0000`: `cmd type:13` logged, **no callback** (no `bleWiFiOpen`). |
| 4 | fixed + EXPERIMENT (`WIFICAP_CLOSE_RSP_OPCODE=10`, runner only) | open | Open as run 3. CloseWiFi answered with `010a0000` instead: `onCallback: 10` → **`java.lang.Exception: l0 Mismatch`** → `bleWiFiOpen(-1, …)`. |
| 5 | fixed | transfer, NEARBY_WIFI_DEVICES also granted | Identical to run 2 (1003 at +30.155 s, 31 dials, no server): the missing permission was not the cause. |

Hash of what the SDK handed the app: **nothing was handed over.** No
`onFileListReceived`, no `onFileTransferCompleted`, no export output; the
app's files dir held only the SDK's own logs (`appfiles-*.txt`). The served
recording is `0f45367bd1eae540a51f2741e3150ffc705aace3054bfdaa4e134b71cbb48c3d`
(11 271 B). No Wi-Fi PDU exists to capture: `capture-*.json` → `wifi`
contains dial events only, and zero `in`/`out` frames in every run.

Screenshots: from the first run on, a "System UI isn't responding" ANR prompt
(the freshly booted AVD on a loaded host) covered the screen, so the
`screen-*.png` files show that prompt, not the join dialog. The dialog is
evidenced by `dumpsys activity` (`topResumedActivity=…NetworkRequestDialogActivity`)
and the ActivityTaskManager START line. The prompt was left alone: nothing was
tapped.

Timeline of run 1b (AVD clock, IST): 13:00:09.76 `am start` → 13:00:12.5 BLE
connected/bound (protVersion=5, tz=5 facade quirk as R7-S12) → 13:00:13.51
`stopSyncFile` (`011d00` → `011e00`) → 13:00:15.015 `startWifiTransfer` →
+0.011 s CONNECTING → opcode 10 round-trip 18 ms → +0.08 s `requestNetwork` →
+30.10 s released → +30.124 s `onError(1003)` → +31.14 s `endWiFiTransfer`,
`setDeviceWiFi(false)` → opcode 13.

## 5. Discrepancies: real SDK vs our docs/emulator

| # | what the docs/emulator said | what the SDK does | class | action |
|---|---|---|---|---|
| D1 | `profile._open_wifi`: opcode-10 byte = on/off; 0 = "drop the hotspot" (`docs/wifi-transport.md` §8; ledger §7 `u8 onOff`) | `startWifiTransfer` opens with **0**; `setDeviceWiFi(true)` sends 1; both "off" paths use opcode 13 | BYTECODE_PROVEN + RUNTIME_PROVEN (runs 1b, 3) | **fixed** (§6) |
| D2 | SSID = `"Plaud"` + last 4 (`wifi-transport.md` §2, citing `calculateWifiName`) | SSID = `BleDevice.getWiFiName()` (project 881, pv < 20 → `"PLAUD"`+last 4; pv ≥ 20 → `"Plaud"`; 880/888 `PLAUD`, 882 `Plaud`, 712 IzyRec/iZYREC, else the name); `calculateWifiName` is only the null fallback | BYTECODE_PROVEN (`ALL.txt:11094-11210`); RUNTIME: `PLAUD0001` | docs corrected |
| D3 | Wi-Fi token = "the BLE handshake token" | `resolveHandshakeToken("")`; equals the BLE token on `connectBleDevice` (ALL.txt:2620-2631) but **not** on `recoveryConnectBleDevice` | RUNTIME_PROVEN (len 21 vs `SYNTHHIST0001`) | docs corrected |
| D4 | §8: the pen dials "as soon as it has answered opcode 10", `connect_attempts` default 1 | the phone starts its server only after a successful join (≤ 30 s later, plus a user tap on real phones); a pen with the default single dial attempt would always fail against the real SDK | BYTECODE_PROVEN; RUNTIME: 31 failed dials | documented; the runner uses 90 × 1 s (HARNESS_POLICY). Default left unchanged: no real pen timing is known |
| D5 | ledger §7: CloseWiFi "rsp `[u8 status]`" on opcode 13 | `q.P` registers the close request's response bean on opcode **{10}** (`ALL.txt:46011-46026`) while `l0.a()` is 13 and `n.<init>` throws "`<class>` Mismatch" on a type mismatch (`ALL.txt:55388-55410`, `:55767-55796`). An opcode-13 answer never reaches the app; an opcode-10 answer reaches it as status −1 | BYTECODE_PROVEN + RUNTIME_PROVEN (runs 1b, 2, 3, 5; run 4) | SDK-side; emulator keeps 13; noted at `OPCODE_CLOSE_WIFI` and in ledger §7 |
| D6 | (not documented) | After a failed join the agent stays CONNECTING (not ERROR) and inactive, so `endWiFiTransfer()`/`NiceBuildSdk.stopWifiTransfer` sends **no** opcode 13 (it does only while `isTransferActive`, `NiceBuildSdk.txt:3315-3364`); the template compensates with `setDeviceWiFi(false)` (`SyncManager.kt`, `finishWiFiTransfer`) | RUNTIME_PROVEN (no BLE write at `endWiFiTransfer`; one `010d00` after `setDeviceWiFi(false)`) | recorded |
| D7 | R7-S12 "the recovery entry point is cloud-free"; `cloud-endpoint-inventory.md` "none exercised" | `initSDK` with a non-blank token calls `POST …/partner/sdk/gen-key` on `platform-jp.plaud.ai`, once per app start, in R7-S13 and R7-S14 alike | RUNTIME_PROVEN (401 in every R7-S14 run that logged a response; see the 28 Sep note below for R7-S13) | disclosed above; the inventory row and the rig are not mine to change |

Also recorded, but no discrepancy: the facade names the `startWifiTransfer`
parameter `sn` while the SDK names it `userId`, and it is dead (§2.1). The
proto stack has a second Wi-Fi helper, `c8`: at init it issues a plain
WIFI `requestNetwork` without a specifier (ALL.txt:27779-27802; seen at every
app start, `logcat-*.system.log`), and it has a specifier-free connect path
with timeout (ALL.txt:24468, :28825). Neither was on the transfer path here.

## 6. The emulator fix

`emulator/plaudsim/profile.py`: `parse_open_wifi_request` returns
`{"mode", "wifi_pass"}` (was `on_off`), and `_open_wifi` treats **every**
opcode 10 as an open. Busy status 4 still applies while streaming or while
the hotspot is already up; status 0 answers with the passphrase and starts the
Wi-Fi device. Only opcode 13 closes, and the `"ble_open_wifi_off"` close
reason is gone. The byte's meaning stays UNKNOWN: the iOS
`operateWiFi(open:isOTA:hotspotPassword:)` suggests `isOTA`, but that is
orientation only. Tests (`tests/test_wifi_ble_handoff.py`,
`tests/test_wifi_ble_crossover.py`):

* new `test_r7_s14_sdk_fast_transfer_open_mode_zero_starts_the_wifi_device`
  (the SDK's `010a0000` starts the device, answers the passphrase, and a
  second one is busy rather than a close);
* new `test_r7_s14_the_sdks_recorded_ble_sequence_opens_a_pullable_wifi_session`
  (over Bumble GATT: `011d00`, `010a0000` → pen → phone double → file list →
  byte-exact download → `010d00` → WifiClose);
* updated `test_open_wifi_request_layouts`,
  `test_open_wifi_reports_status_and_serial_derived_passphrase`,
  `test_second_open_does_not_start_a_second_wifi_device`.

All four changed or new behavioural tests fail against the pre-fix handler (a
scratch copy of the tree; 4 failed, 21 passed) and pass with the fix. Run 2
confirmed the fix live: the pen started on the SDK's own open and answered
the passphrase. Noted in `docs/wifi-transport.md` (§8 table, new §11) and in
ledger §7.

## 7. What remains unknown

* **Everything after the join, with the real SDK**: server start timing, the
  phone accepting the pen's connection, SayHello/Handshake acceptance, READY,
  `getFileList`, FileSync/FileSyncContent handling, `exportAudioViaWiFi`
  output bytes, WifiClose. All of it is still BYTECODE_PROVEN at best and
  tested against the phone double only.
* The opcode-10 mode byte's meaning; which opcode real firmware uses to answer
  CloseWiFi (and so whether any real app ever sees a close status; the
  template's remark that a second close "can make the device answer with an
  error status", `SyncManager.kt`, *might* be D5's −1, but that is unproven).
* The legacy (< API 29) join path (`AriesTaskRelay`), which was not read.
* Whether a real phone needs the user's tap on the network-request dialog every
  time (likely, per Android behaviour; not observed here).
* Real pen timing between OpenWiFi and dialling, and whether it retries.

**Paths that could get further (not attempted, per the task):** (i) a
simulated radio environment that broadcasts `PLAUD0001`/WPA2 `10000001`. netsimd has a
`--config` option; whether it can set the hostapd SSID or passphrase was not
checked. The Settings dialog would also need a user approval. (ii) A physical
Android phone plus a real SoftAP (`PLAUD0001`, `10000001`) on a host Wi-Fi
adapter, with our WifiDevice dialling the phone's DHCP address on 8081. The
server's wildcard bind (§2.3) means either would reach it.

## 8. Environment and housekeeping

* Memory gate: Colima not running and no `faster_whisper|sherpa|pipeline batch`
  process at 12:50 and again before Gradle (12:54), so there was no wait. The
  AVD booted in about 35 s. No physical phone was attached (`adb devices` showed
  only `emulator-5554`). At the end: forward removed, capture processes
  stopped, `adb -s emulator-5554 emu kill`, `pkill netsimd`.
* `r7/android-app/NOTICE-MODIFICATIONS.md` (the Apache-2.0 §4(b) change list)
  still describes only the R7-S12/S13 changes. It now misses
  `WifiCaptureActivity.kt`, the new activity entry and the
  `NEARBY_WIFI_DEVICES` declaration. It is outside this task's ownership and
  **needs updating by its owner**.
* Evidence: `r7/r7-s14-evidence/` (60 files, `SHA256SUMS`). `run.sh` is
  archival (this machine's paths).

> **28 Sep 2026 note (after a fact-check).** Across the archived R7-S13 and R7-S14 logs, 22 show the
> automatic `gen-key` request and 19 record the server's 401. In R7-S13 runs 11–13 the emulator's DNS
> lookup of `platform-jp.plaud.ai` failed (`UnknownHostException`), so those three requests never
> reached the server. The earlier R4-S3 and R5-S1 runs used the stock template with no token set
> (their logs say `user access token not set`), so they very likely sent nothing. R7-S12's logs were
> filtered and cannot tell.

## Independent checker's notes (25 Sep 2026)

The report was checked against its evidence by a second agent. The main result stands. These six
points qualify it:

1. **Cause of the block.** The logs prove only the 30 s timeout of the network request. Nothing
   approved Android's network-request dialog (a "System UI isn't responding" prompt covered it), so
   on API 34 the request would likely have timed out even if a `PLAUD0001` access point existed. The
   missing access point is sufficient on its own, but the runs do not show it is the only cause.
2. **WPA2 passphrase `10000001`.** The runtime logs show only the SSID. The WPA2 type and passphrase
   come from bytecode (`setWpa2Passphrase` when non-empty; `getWiFiPwd` = last 8 digits of the serial).
3. **Run 1.** Its kept log is 58 lines and shows only `WIFICAP_END state=CONNECTING`; the onError 1003,
   the guard results and the missing server line are not in run 1's evidence. Only five `gen-key`
   requests appear in the logs; the sixth (run 1) is inferred. Run 1's second probe was at +63 s,
   outside the join window, and run 1 used an earlier script than the archived `run.sh`.
4. **Inventory quote.** D7's "none exercised" belongs to the inventory's section C (community web API).
   The line it actually contradicts is the header's "Nothing here was called." The point stands.
5. **"DEBUG-ONLY".** The exported driver activities and the `NEARBY_WIFI_DEVICES` permission live in
   `src/main`, so they ship in any build of this app copy; "DEBUG-ONLY" is a comment, not a build-type
   restriction.
6. **Housekeeping claims without an evidence file** (memory-gate checks, ~35 s boot, no physical phone,
   forward removed) are consistent with the machine state but not archived.
