# Wi-Fi bulk transfer — emulator track (`emulator/plaudsim/wifi.py`, `wifi_device.py`)

Status 2026-09-25: **implemented and tested against a phone-side test double;
never run against a real phone or pen.** Since 2026-09-25 a BLE session can
hand over to Wi-Fi: with a `wifi_device_factory`, an accepted opcode 10
starts the Wi-Fi device and opcode 13 closes it (section 8). That hand-over is
harness policy over loopback, not a SoftAP. R7-S14 (2026-09-25,
`r7/r7-s14-wifi-real-sdk.md`, section 11 below) ran the GENUINE SDK's
`startWifiTransfer` against this track on the AVD: it blocks on the SoftAP join
before its server exists, so no Wi-Fi PDU was ever exchanged with the real
SDK; one emulator bug it exposed (opcode 10 mode 0) is fixed. This document records what the
code does, which facts it rests on (with `build/evidence` line numbers), which
choices are the harness's own (`HARNESS_POLICY`) and what remains `UNKNOWN`.
The recovered design it implements is `docs/protocol-ledger.md` §7; where this
document and §7 disagree, this document is the more recent reading of the same
bytecode and says so inline. (Update, 25 September 2026: this paragraph used to
say that two §7 statements were out of date, its "NOT IMPLEMENTED" title and its
"same padding rule as BLE's `k3`" wording. Both are now corrected in the
ledger. §7 is titled "SOURCE-DERIVED; emulated against a phone double only",
and it says the Wi-Fi handshake token matches `k3` only up to 32 characters,
because `padEnd` never truncates and `k3` does
(`test_pad_matches_k3_below_32_and_diverges_above`).)

| item | where |
|---|---|
| pure codecs: PDU envelope, 17 message types (both phone stacks' JSON spellings), 10-byte file records, token padding, `>200` classifier, `WifiSealer` (AES-GCM and ChaCha20-Poly1305) | `emulator/plaudsim/wifi.py` |
| device side: asyncio WebSocket **client** with the pen's state machine, handlers, heartbeat / exit timers, chunked `FileSyncContent` streaming, stop / replace, `WifiClose` | `emulator/plaudsim/wifi_device.py` |
| phone side (TEST DOUBLE ONLY): WebSocket **server** behaving as `sdk.ble.wifi.WifiAgentImpl` + `WebSocketOperation` | `tests/wifi_support.py` |
| BLE handoff opcodes 10 / 13 / 16 / 17 in `profile.py`, incl. status 4 while streaming / already open | `tests/test_wifi_ble_handoff.py` (18) |
| one session across both transports: BLE (Bumble) opcode 10 → `WifiDevice` → phone double → opcode 13 | `tests/test_wifi_ble_crossover.py` (7) |
| codec tests, hand-computed bytes and KATs | `tests/test_wifi_codec.py` (29) |
| javap pins: every type number, JSON key, guard string, port, timeout read from the dumps | `tests/test_wifi_javap_pins.py` (75) |
| loopback sessions device ↔ phone double | `tests/test_wifi_session.py` (27, about 5.5 s) |
| dependencies (nothing new was installed) | `requirements/wifi.txt` |

Run:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_wifi_codec.py tests/test_wifi_javap_pins.py \
    tests/test_wifi_ble_handoff.py tests/test_wifi_session.py tests/test_wifi_ble_crossover.py \
    -q -p no:cacheprovider --timeout=60
```

There is no CLI. A session is `await WifiDevice(host, port, store=WifiFileStore.from_files({...})).run()`
against any WebSocket server that speaks the phone's protocol; the device dials
out, so the server must already be listening (as the phone's does). From a BLE
session: `PlaudPeripheral.for_real_sdk(device, ..., wifi_device_factory=phone_dialer(host, port))`
(section 8).

## 1. Roles and scope

The roles are inverted between the two layers (ledger §7): the **pen** raises
the SoftAP, but the **phone** runs the `java-websocket` server on TCP 8081 and
the **pen** is the WebSocket client. The emulator therefore *dials out*; it
does not listen.

Out of scope, deliberately: the SoftAP/DHCP layer (IP connectivity is assumed),
the OTA types 20/21/22 (codecs exist, the device logs them as unsupported), any
real credential material, and the iOS `PlaudWiFiSDK` stack (its swiftinterface
was orientation only; nothing here is pinned to it).

**BLE finding that does NOT carry over.** R7-S13 (`r7/r7-s13-recording-pull.md`
§4.1, RUNTIME_PROVEN) showed the BLE transfer is closed by an `EMPTY_PACKAGE`
sentinel frame before TAIL. That is a property of the BLE `syncFile` path. The
Wi-Fi path has its own terminator: `FileSyncContent.last == 1`
(`WifiMessageProcessor$FileSyncContentResponse.isFinished`, checked at
`WifiAgentImpl.txt:1847-1848`, `:1864-1865`, `:1974-1975`). No sentinel frame
was imported into the Wi-Fi design and none is emitted.

## 2. Evidence table

Classes: **BYTECODE_PROVEN** = read from `build/evidence/javap/**` (line numbers
are into `build/evidence/javap/sdk/ble/wifi/<file>` unless another path is given;
`ALL.txt` = `build/evidence/javap/ALL.txt`); **DIRECT** = a name or enum read
verbatim; **CLAIM** = an assertion in a template-app comment, not verified;
**DERIVED** = arithmetic on a proven rule; **HARNESS_POLICY** = the harness's
own choice; **UNKNOWN** = the evidence is silent.

| fact | class | citation |
|---|---|---|
| The phone binds `new InetSocketAddress(8081)`, `setReuseAddr(true)`, `start()` | BYTECODE_PROVEN | `WebSocketOperation.txt:177-189` (`sipush 8081` at :180); proto `w7` likewise (`test_proto_w7_send_and_receive_semantics`) |
| On start: `setConnectionLostTimeout(0)`, `setReuseAddr(true)`, `setTcpNoDelay(true)` | BYTECODE_PROVEN | `WebSocketOperation$startServer$1.txt:15-30` (:25) |
| `onOpen` only records the socket and fires `onClientConnected()`; the phone sends nothing | BYTECODE_PROVEN | `WebSocketOperation$startServer$1.txt:32-67` (:65) |
| Text frames are only logged; binary frames go to `onClientMessage(byte[])` | BYTECODE_PROVEN | `WebSocketOperation$startServer$1.txt:118-138` (text), `:140-181` (binary, :181) |
| A failed `sendMessage` is logged ("Failed to send message:") and returns false; it does not tear the session down | BYTECODE_PROVEN | `WebSocketOperation.txt:301-422` (:406) |
| `stopServer()`: close the client socket, then stop the server | BYTECODE_PROVEN | `WebSocketOperation.txt:222-283` |
| PDU envelope `[u24le total][u8 pduVersion][s16le type][s16le jsonSize][JSON][tail]`, little-endian, host stamps version **1** | BYTECODE_PROVEN | `WifiMessageProcessor$BaseWifiRequest.txt:33-85` (:73-75); proto `x7.t = 1` (`ALL.txt`, `test_proto_envelope_and_tail_sizing`) |
| Decoder guards: "Data too short for header" (<8), "Type mismatch", "Data too short for JSON content"; a JSON parse failure leaves every getter at its default | BYTECODE_PROVEN | `WifiMessageProcessor$BaseWifiResponse.txt:77-233` (:197-206) |
| Inbound: `len < 8` → "Message too short"; `u16le@4 > 200` **and keys present** → decrypt; else parse as plaintext | BYTECODE_PROVEN | `WifiAgentImpl.txt:828` (string), `:850-866` (`sipush 200; if_icmple` at :865) |
| Type numbers 0,1,2,3,4,5,11,12,13,14,15,16,20,21,22,100,101 and every JSON key, on both stacks | BYTECODE_PROVEN | `tests/test_wifi_javap_pins.py` (`KOTLIN_TYPES`, `KOTLIN_KEYS`, `PROTO_TABLE`); per-type ranges in `wifi.MESSAGE_EVIDENCE` |
| Types 5, 15, 16, 20-22, 100, 101 exist only on the proto (`com.plaud.sdk.proto`) stack; the Kotlin stack merely names 15 | BYTECODE_PROVEN | `WebSocketOperation.txt:353-367`; `wifi.MESSAGE_EVIDENCE` |
| Handshake token = `padEnd(token, 32, '0')` (no truncation) | BYTECODE_PROVEN | `WifiMessageProcessor$HandshakeRequest.txt:13-25` |
| The token is `NiceBuildSdk.resolveHandshakeToken("")` (JWT `sub` of the partner user-access token, else empty). R7-S14: this equals the BLE `k3` token only on the `connectBleDevice` path; on `recoveryConnectBleDevice` the BLE token is the historical id, so the two DIFFER (RUNTIME_PROVEN: Wi-Fi token len 21 vs BLE `SYNTHHIST0001`) | BYTECODE_PROVEN | `WifiAgentImpl.txt:2278-2307`; `ALL.txt:2620-2631` (connectBleDevice) vs `:2653-2761` (recovery) |
| SayHello (type 2) is the only trigger of the phone's HandshakeRequest, and only while `currentSessionId` is empty | BYTECODE_PROVEN | `WifiAgentImpl.txt:1353` (SayHelloResponse), `:1417` (EtaConfiguratorNode); `EtaConfiguratorNode.txt:23-56`; log "Device says hello" `:808`; proto `x7.a(p5)` builds `m3` (`test_phone_sends_handshake_only_from_the_say_hello_handler`) |
| HandshakeResponse status 0 → `sessionId = session or token`, state READY, `onHandshakeCompleted` | BYTECODE_PROVEN | `WifiAgentImpl.txt:1428-1485` (READY :1460, callback :1485) |
| Any other handshake status → `onError(1006, "Handshake failed with status: N")` | BYTECODE_PROVEN | `WifiAgentImpl.txt:800-802` (string), `:1505` (`sipush 1006`) |
| The phone waits 30 s for a session id after starting the server ("Handshake timeout") | BYTECODE_PROVEN | `build/evidence/javap/com/plaud/sdk/internalimpl/CapricornusStateRelay.txt:121`; `SagittariusProcessorRelay.txt:52` (`long 30000l`) |
| Every request bean has a 30 s timeout and no per-request response callback; replies are matched by **type** | BYTECODE_PROVEN | `WifiRequestBean.txt:55`; `ZetaAsyncManager.txt:69-78` |
| HeartbeatPing → `onDeviceBatteryUpdate` and a HeartbeatPong (`{"stamp"}`; proto `p3` sends `{}`) | BYTECODE_PROVEN | `WifiAgentImpl.txt:1297-1347` (:1333); `ThetaEventRouter.txt:23-68`; `WifiMessageProcessor$HeartbeatPongRequest.txt` |
| WifiClose → cancel transfers, `stopServer()`, clear keys, DISCONNECTED, "Device closed WiFi" | BYTECODE_PROVEN | `WifiAgentImpl.txt:1191-1292` (:1222 stopServer, :1271 string) |
| `getFileList()` sends `GetFileListRequest(0, 0, false)` | BYTECODE_PROVEN | `ZetaAsyncManager.txt:44-51` |
| The **first** type-11 frame resumes the file-list continuation; status ≠ 0 → empty list | BYTECODE_PROVEN | `WifiAgentImpl.txt:1149-1186` (getStatus :1164, getSessions :1167, continuation :1173-1186; field `register_event_listener` = `fileListContinuation`, `:411-421`) |
| File record = `[u32le sessionId][u32le fileSize][u16le scene]`, loop `i + 10 <= len` | BYTECODE_PROVEN | `WifiMessageProcessor$FileListResponse.txt:83-123` (`bipush 10` :118) |
| Phone file name: `log<id>` when id < 100, else `yyyy-MM-dd_HH-mm-ss` of `id*1000` + `.opus` | BYTECODE_PROVEN | `WifiMessageProcessor$FileListResponse.txt:219-256` |
| Download sends `FileSyncRequest(session, scene, start=0, end=fileSize)` | BYTECODE_PROVEN | `EpsilonDataStream.txt:139-143` |
| FileSyncStatus: status ≠ 0 → "File sync failed: status=N"; `total` sets the expected size | BYTECODE_PROVEN | `WifiAgentImpl.txt:1048-1130` (getStatus :1061/:1097, getTotalSize :1110/:1127); string `:816` |
| FileSyncContent is routed by **its own session id** to a transfer context; none → "No transfer context for session:" and return | BYTECODE_PROVEN | `WifiAgentImpl.txt:1777-1801` (:1795) |
| Content bytes are appended in arrival order; `offset` is never used for placement | BYTECODE_PROVEN | `WifiAgentImpl.txt:1777-2080` (`OutputStream.write` :1823, :1862) |
| An empty data frame skips straight to the `isFinished` check; `last == 1` completes the transfer | BYTECODE_PROVEN | `WifiAgentImpl.txt:1811-1815` → `:1974-1975` |
| `getFileData` returns the tail only when `len(tail) == length` and `> 0` | BYTECODE_PROVEN | `WifiMessageProcessor$FileSyncContentResponse.txt:81-115` |
| `deleteFiles` sends one FileDeleteRequest per id inside `withTimeout(30000)` | BYTECODE_PROVEN | `BetaComponentHandler.txt:102-162` |
| Outbound frames are sealed iff the three key arrays are present; algorithm names "AES-GCM" / "ChaCha20-Poly1305" | BYTECODE_PROVEN | `WifiAgentImpl.txt:489-503`, `:505-698` (:280-283, :638-641) |
| Sealed frame = AEAD over `[u32le seq][PDU]` with the BLE (key, nonce, aad); AES-GCM iff capability bit 3 else ChaCha20-Poly1305 | BYTECODE_PROVEN | `build/evidence/javap/com/plaud/sdk/proto/w7.txt:31-464` (send path `a(e8)`: `z.p()+1` :151, `q5.e` :227, `q5.d` :236), `q5.txt` (`test_proto_w7_send_and_receive_semantics`, `test_aes_gcm_parameters_in_q5`); ledger §4.3 |
| Kotlin receive path stores the sequence **without** a replay check | BYTECODE_PROVEN | `WifiAgentImpl.txt:2084-2196` (`"WiFi received seq="` :2084, `", expected >"` :2179; `test_kotlin_receive_stores_sequence_without_replay_check`) |
| Proto receive path drops a frame when `rx >= seq` | BYTECODE_PROVEN | `build/evidence/javap/com/plaud/sdk/proto/w7.txt:562-1476` (receive path `c(byte[])`: `q5.a(...,boolean)` :663, serial-prefix `"881"` counter split :639) |
| Phone connection states NONE, CONNECTING, CONNECTED, HANDSHAKING, READY, DISCONNECTED, ERROR | DIRECT | `IWifiTransferAgent$WifiConnectionState.txt:49-98` |
| SoftAP SSID = `BleDevice.getWiFiName()` (project 881: `"PLAUD"`+last 4 when portVersion < 20, `"Plaud"`+last 4 when >= 20; 880/888 `"PLAUD"`, 882 `"Plaud"`, 712 IzyRec/iZYREC, else the name); `WifiAgentImpl.calculateWifiName` (`"Plaud"`+last 4) is only the null fallback. Passphrase = `getWiFiPwd()` = last 8 of SN. R7-S14: the SDK asked Android for `PLAUD0001` (RUNTIME_PROVEN) | BYTECODE_PROVEN | `IotaStateTracker.txt:43-68`; `ALL.txt:11094-11226`; fallback `WifiAgentImpl.txt:1676-1703` |
| With a constant nonce the classifier misfires on exactly 201 of 65 536 low-16-bit `totalSize` values (0.31 %) | DERIVED | from the `>200` rule + nonce reuse; demonstrated by `test_sealed_frame_that_defeats_the_size_heuristic_*` |
| After READY the app must call `getFileList()` or the device "just heartbeats and closes" | CLAIM | `docs/product-evidence/agent-findings-2026-09-23.json` (Wi-Fi fast-transfer flow entry) |
| The pen refuses to open its hotspot while still streaming a BLE transfer: openWiFi status 4 (or Wi-Fi connect 1003) | CLAIM | `reference/plaud-org/plaud-sdk-public/android/app/src/main/java/com/plaud/template/managers/SyncManager.kt:31-32`, `:173-176` |
| A second openWiFi while a Wi-Fi session is already opening or running is rejected with status 4 ("WiFi fast transfer already in progress") | CLAIM | same file `:142-148` |
| During Wi-Fi fast transfer the pen drops BLE; a second closeWiFi can answer an error status | CLAIM | same file `:184-187`, `:531-532` (neither is modelled, section 8) |
| The device self-closes after ~3 heartbeats and otherwise times out in ~2-2.5 min | CLAIM | same file, entries "device self-disconnects the WiFi session after 3 heartbeats..." and "Wi-Fi teardown is device-led" |

## 3. The phone-server sequence (reconstructed from `WebSocketOperation` / `WifiAgentImpl`)

1. `startServer()` binds 8081 with reuse-addr and starts; `onStart` disables the
   connection-lost timer and sets TCP_NODELAY (`WebSocketOperation.txt:177-189`,
   `$startServer$1.txt:15-30`). A 30 s "Handshake timeout" starts
   (`CapricornusStateRelay.txt:121`, `SagittariusProcessorRelay.txt:52`).
2. `onOpen`: the socket is remembered, `onClientConnected()` fires, **nothing is
   sent** (`$startServer$1.txt:32-67`).
3. Binary frame → `handleIncomingMessage`: `< 8` bytes dropped; `u16le@4 > 200`
   with keys → decrypt; then a switch on the type (`WifiAgentImpl.txt:790-1669`).
4. Type 2 SayHello → if `currentSessionId` is empty, `EtaConfiguratorNode` queues
   `HandshakeRequest(padEnd(token,32,'0'), now)` (`:1353-1422`,
   `EtaConfiguratorNode.txt:23-56`). Hence **the pen must speak first**.
5. Type 1 HandshakeResponse: status 0 → session id, READY, `onHandshakeCompleted`
   (`:1428-1485`); else `onError(1006, …)` (`:1505`). The template then calls
   `getFileList()` (CLAIM; the SDK itself does not).
6. Type 3 HeartbeatPing → battery callback + HeartbeatPong (`:1297-1347`).
7. Requests (`GetFileList`, `FileSync`, `FileDelete`) are queued
   `WifiRequestBean`s with a 30 s timeout; the reply of the same **type** resumes
   the continuation (`WifiRequestBean.txt:55`, `:1149-1186`, `:1048-1130`).
   Type 13 frames are routed by session id and appended until `last == 1`
   (`:1777-2080`).
8. Type 4 WifiClose → transfers cancelled, `stopServer()` (client socket
   `close()`, server `stop(1000)`), keys cleared, DISCONNECTED (`:1191-1292`,
   `WebSocketOperation.txt:222-283`).

`tests/wifi_support.py` reproduces exactly this and nothing more (its request
timeout is shortened to 5 s for the tests; the constants
`SDK_REQUEST_TIMEOUT_MS`/`SDK_HANDSHAKE_TIMEOUT_MS` keep the real values).

## 4. HARNESS_POLICY items (device side unless stated)

None of these is a device claim. Each is labelled at its definition.

| # | policy | where |
|---|---|---|
| P1 | `token` in SayHello echoes the configured handshake token; `sn`/`version`/`pVer` are synthetic (`8810000001`, `V0001`, 7) | `WifiDevice.__init__`, `wifi.SayHello` |
| P2 | Handshake token check: accept anything unless `expected_token` is set; mismatch → status **1** and empty session | `WifiStatusPolicy`, `_on_handshake` |
| P3 | All non-zero status values (`1` for unknown session on sync/delete, token mismatch) — only `0 = success` is proven on the phone | `WifiStatusPolicy` |
| P4 | Session id string `SYN-WIFI-SESSION-0001` in the HandshakeResponse | `DEFAULT_SESSION_ID` |
| P5 | Heartbeat cadence 1 s (tests: 50 ms); idle-heartbeat self-close after 3 pings with no request in between (models the CLAIM) | `DEFAULT_HEARTBEAT_INTERVAL`, `DEFAULT_IDLE_HEARTBEATS`, `_heartbeat_loop` |
| P6 | Exit timeout 120 s, refreshed by ExtendExitTime (models the CLAIM) | `DEFAULT_EXIT_TIMEOUT`, `_exit_timer`, `_on_extend_exit_time` |
| P7 | Chunk size 4096 B per FileSyncContent, no pacing (`chunk_delay=0`) | `DEFAULT_CHUNK_SIZE`, `_stream_file` |
| P8 | `end <= 0` or beyond EOF means "to EOF"; an empty range yields one empty `last=1` frame | `_stream_file` |
| P9 | A second FileSync replaces the active stream: the old task is cancelled **before** the new FileSyncStatus is sent | `_on_file_sync` |
| P10 | FileSyncStop → status 0 whether or not a stream was active | `_on_file_sync_stop` |
| P11 | One FileList frame, `offset 0`, all records; `start`/`single` parsed and ignored (the only shape both phone stacks consume whole) | `_on_get_file_list`, `wifi.FileListResponse` |
| P12 | JSON superset: the device emits **both** stacks' key spellings (`battery`+`volume`, `reason`+`status`, `code/msg`+`cmd/status`) | `wifi.HeartbeatPing`, `wifi.WifiClose`, `wifi.UniversalError` |
| P13 | The pen classifies inbound frames with the phone's own `>200` rule and parses plaintext even when keyed | `WifiDevice._on_frame` |
| P14 | `pdu_version` stamped by the pen defaults to the host's 1 | `PDU_VERSION_HOST`, `WifiDevice(pdu_version=)` |
| P15 | SpeedTest answers 3 frames of `pack_size` zero bytes; GetDeviceLog answers one frame of a synthetic log; `operate == 0` is a no-op | `_on_speed_test`, `_on_get_device_log` |
| P16 | OTA types 20/21/22 are parsed and logged `unsupported_ota`, never answered | `_on_ota` |
| P17 | The pen's TX sequence starts wherever the given `SealedSession` is (2 for a fresh one); `replay_check=True` by default (proto-phone semantics) | `wifi.WifiSealer` |
| P18 | `ws.close()` after WifiClose is the pen's own; `ping_interval=None` mirrors the phone's `setConnectionLostTimeout(0)` | `WifiDevice.run/close` |
| P19 | (phone double) request timeout 5 s, ephemeral port, `auto_handshake`/`auto_pong` switches | `PhoneWifiServer.__init__` |
| P20 | A `WifiDevice` is single-use: a second `run()` raises; one device per session | `WifiDevice.run` |
| P21 | `close()` works in every state: before `run()` → CLOSED at once and `run()` returns immediately; while dialling → the dial is cancelled, nothing is sent; connected → WifiClose, then the socket closes. Nothing moves the state back out of CLOSING | `WifiDevice.close`, `_dial`, `_set_state` |
| P22 | A request handler that raises is logged `handler_error` and the session keeps serving (the phone's request times out); a failing transfer task is logged `transfer_failed` | `WifiDevice._on_frame`, `_on_transfer_done` |
| P23 | Non-finite JSON numbers (`1e400`, `NaN`) read as the field's default, like a missing key | `wifi._opt_int` |
| P24 | Inputs the wire cannot carry are refused up front: session id / file size outside u32, scene outside u16 (`WifiFileStore`), `chunk_size` above `MAX_CHUNK_SIZE` = 0xFFFFFF − 8 − 256 (u24 `totalSize`) | `WifiFileStore._check`, `WifiDevice.__init__` |
| P25 | Dial attempts (`connect_attempts`, default 1) and the retry interval; a close() during the retry wait ends the dial | `WifiDevice._dial` |
| P26 | The BLE hook: see section 8 | `PlaudPeripheral._start_wifi_device`, `wifi_device.phone_dialer` |

## 5. Sealed sessions (portVersion ≥ 20)

Both AEADs are implemented and exercised end-to-end
(`test_sealed_session_end_to_end[chacha20-poly1305|aes-gcm]`): every frame in
both directions is sealed, opened, and classified by the `>200` rule; the
device's sequence numbers are strictly increasing from 2. Key mismatch is an
`InvalidTag` drop on the phone; a sealed pen against a keyless phone never
handshakes (its frames parse as garbage); a plaintext pen against a keyed phone
gets a sealed Handshake it cannot open.

**Hazard (DERIVED, deterministic).** Because the nonce is reused per frame, the
keystream is constant and sealed byte *i* = plaintext byte *i* XOR *k[i]*.
Plaintext bytes 4..5 are PDU bytes 0..1 = `totalSize & 0xFFFF`, so for a given
(key, nonce) exactly 201 low-16-bit sizes seal to a frame with `u16le@4 ≤ 200`.
The phone then parses ciphertext as a PDU and drops or mis-dispatches it; if
that frame carried `last=1` the download stalls until the 30 s request
timeout. `test_sealed_frame_that_defeats_the_size_heuristic_is_dropped_and_stalls_the_transfer`
computes the colliding size for the synthetic keys and shows exactly that. What
real firmware does about it is UNKNOWN (it may never emit such a size, or may
not care).

## 6. Test harness notes

* **The deadlock the previous implementer hit.** `PhoneWifiServer` handled
  WifiClose by awaiting `stop_server()` → `websockets.Server.wait_closed()` from
  inside the connection handler. `Server._close()` waits for every handler task
  to return before resolving `handlers_waiter`, and the handler was waiting on
  that future: a cycle with no timer, so the loop sat in `select(None)` forever.
  Fix: inside the handler only *schedule* the shutdown (`server.close()`, which
  is idempotent) and let `__aexit__` await `wait_closed()` once the handler has
  unwound (`stop_server(wait=False)`). Every session test sent a WifiClose, so
  the whole file hung.
* **Stream replacement.** With chunks already on the wire, a phone that appended
  any type-13 frame would corrupt the second file. The double now keys frames
  by session id as the phone does (`:1777-1801`), and the device cancels the old
  stream before answering the new FileSync (P9).
* **Determinism.** The sealed tests use fixed synthetic keys; the classifier
  outcome depends only on `totalSize`, and no test frame size collides (checked
  in the scratch script that produced §5's numbers and by the tests passing
  repeatedly). Timings in the tests (50 ms heartbeats, 250 ms exit timeout,
  3 ms chunk pacing) are TEST POLICY, not hardware cadence.

## 7. UNKNOWN (evidence silent; not modelled or modelled as policy)

* **pduVersion** the pen stamps. The host stamps 1; the proto stack overwrites
  its static with whatever the pen's SayHello carried and never checks it.
* **SayHello content**: what real firmware puts in `token` (the Kotlin phone only
  logs a mismatch; the proto phone requires `upper(md5hex(x7.l))`), the format
  of `version`, and the value of `pVer` (the BLE portVersion is the obvious
  candidate, unproven).
* **Tips (type 5)** values and when a pen sends them; **UniversalErr (type 0)**
  codes; **HelloAck** (type 2 phone→pen) has no caller in this build.
* **Status code semantics** beyond `0 = success` for every response.
* **Real timings**: heartbeat cadence, the "3 heartbeats" and "~2 min" figures
  (CLAIMs), chunk size and pacing, whether the pen paces on TCP back-pressure.
* **Semantics** of `GetFileList.start/single` (every caller sends 0/false), of
  `FileSync.end <= 0` (iOS default), of `FileSyncStop`/`ExtendExitTime` on the
  Kotlin stack (no request class), and of a second FileSync while one streams.
* Whether the pen applies the same `>200` classifier inbound, whether it checks
  the Handshake token at all, and its sealed TX counter start.
* The WebSocket close code the pen uses and whether it always precedes the
  socket close with a WifiClose frame.
* Whether real firmware ever emits a frame size that defeats the classifier (§5).

## 8. One session across BLE and Wi-Fi (the opcode-10 hook)

`PlaudPeripheral(wifi_device_factory=f)` connects the two transports. The BLE
side is recovered: opcode 10 `i4`/`j4` and opcode 13 `k0`/`l0` layouts are
BYTECODE_PROVEN (ledger §7; `ALL.txt` ranges at the constants in
`plaudsim/profile.py`), and `NiceBuildSdk.stopWifiTransfer` sends opcode 13
over BLE when BLE is up (`build/evidence/javap/sdk/NiceBuildSdk.txt:3315-3364`,
the "BLE unavailable" branch string at :3346). **Everything that
connects the two is HARNESS_POLICY:**

| policy | behaviour |
|---|---|
| when the device starts | on any opcode 10 answered with status 0, whatever its mode byte (R7-S14 fix: the SDK's own fast-transfer open sends mode **0**); `f(peripheral)` must return a NEW `WifiDevice`, which runs as a task |
| where it dials | wherever the factory says. `phone_dialer(host, port, ...)` dials `ws://host:port`; the real pen dials the phone that joined its SoftAP. IP connectivity is assumed and the phone's server is expected to be listening (`connect_attempts` allows a retry window) |
| what it serves | `store_from_peripheral`: one Wi-Fi record per BLE file-table entry, with the bytes the BLE y6 would stream (`file_bytes_for`) and the entry's scene |
| who it says it is | SayHello `sn` = the advertised serial, `version` = versionType + 4-digit versionCode, `pVer` = the BLE portVersion (that `pVer` is the portVersion is UNKNOWN) |
| busy | opcode 10 answers `wifi_busy_status` (4) while a y6 transfer is still being emitted (CLAIM `SyncManager.kt:31-32`) or while the hotspot is already up (CLAIM `:142-148`); no second device is started |
| stop | opcode 13 closes the device (`close("ble_close_wifi")`): WifiClose if connected, a cancelled dial if not. Opcode 10 never closes (before R7-S14, mode 0 did: `"ble_open_wifi_off"`, removed) |
| end of session | when the device's session ends for any reason (closed over BLE, idle heartbeats, exit timeout, failed dial) the pen drops its hotspot flag, so the next opcode 10 is accepted and gets a new device. `wifi_session_log` records `started`/`ended` |
| BLE during Wi-Fi | stays up; a BLE disconnect does not stop Wi-Fi. The template claims the pen drops BLE while its hotspot is up (`SyncManager.kt:184`) -- not modelled, so that opcode 13 can reach it |

`tests/test_wifi_ble_crossover.py` drives the whole flow over a Bumble link
brought up the way the Android SDK does it: file list over BLE → a paced BLE
transfer → opcode 10 refused with 4 → stopSync → opcode 10 accepted → the pen
dials the phone double → handshake → Wi-Fi file list equals the BLE one →
byte-exact Wi-Fi pull → opcode 13 → the pen announces WifiClose. Other tests
cover a second open (no second device), opcode 13 during the dial, a failed
dial followed by a successful re-open, and opcode 13 immediately followed by
opcode 10 (the new open gets its own device while the old one winds down).

## 9. Review fixes (2026-09-25)

An independent review found these in the Wi-Fi track and its BLE handoff.
Each fix has a test that failed before it (T11: a test that fails against a
deliberately broken implementation the old assertion accepted).

| finding | what was wrong | fix | test |
|---|---|---|---|
| T1 | opcode 10's busy check read `transfer.done`, which `TransferSession.frames()` sets before the first frame leaves, so status 4 could never be answered | `PlaudPeripheral.transfer_streaming` (inline emission counter, live stream task) | `test_open_wifi_mid_stream_is_refused_with_busy_status[inline/task]`, `test_open_wifi_over_bumble_is_busy_mid_stream_and_accepted_after_stop_sync` |
| T3 | `close()` before the socket was open raised AttributeError and did not stop the dial; `run()` then went on to CONNECTED/HANDSHAKED | P21; `send()` raises `WifiNotConnected` without a socket | `test_close_before_run_is_a_clean_no_op_session`, `test_close_while_dialling_cancels_the_session`, `test_close_wifi_while_the_pen_is_still_dialling` |
| T4 | a second `run()` reused set Events (`wait_handshaked()` returned on a phone that never handshook) | P20 | `test_device_is_single_use` |
| T5 | a handler exception ended the session unlogged; a stream exception vanished; `{"start": 1e400}` raised OverflowError; invalid store entries failed inside GetFileList | P22, P23, P24 | `test_a_failing_handler_is_logged_and_the_session_keeps_serving`, `test_non_finite_json_numbers_do_not_crash_the_session`, `test_a_failing_stream_is_logged_as_transfer_failed`, `test_store_and_chunk_size_are_validated_against_the_wire_fields`, `test_opt_int_treats_non_finite_numbers_as_absent` |
| T8 | a second opcode 10 was accepted although the cited template line describes status 4 for exactly that case; the citation mixed the two status-4 claims | busy "already_open"; the constant cites `:31-32`/`:173-176` and `:142-148` separately | `test_a_second_open_while_the_hotspot_is_up_is_refused_with_busy_status`, `test_second_open_does_not_start_a_second_wifi_device` |
| T9 | `EpsilonDataStream.txt:205-235` quoted bytecode offsets as file lines | `:130-143` in `wifi.FileSyncRequest` and `PhoneWifiServer.download` | `test_file_sync_start_end_citations_point_at_the_constructor_call` |
| T11 | three assertions could not fail: the mute-phone test did not check that the pen speaks first; `looks_encrypted(x) or not looks_encrypted(x)`; `all(...)` over a possibly empty drop list | the pen's SayHello must be the first frame on the wire of a phone that never answers (`auto_pong=False`); the sealed bytes are recomputed with the AEAD primitive and the classifier verdict pinned; a stale stream-1 chunk is forced and must be dropped | `test_pen_speaks_first_and_waits_for_the_phones_handshake`, `test_seal_frames_seq_then_pdu_and_opens_across_endpoints`, `test_stale_chunks_of_a_replaced_stream_are_dropped_by_session` |

Also changed: `send()` seals under the send lock, so the logged `seq` is the
frame's own. BLE-side stream fixes (T6/T7) are listed in
`docs/v2-fault-matrix.md` section 6; `PlaudPeripheral.for_real_sdk()` (T2)
is the R7-S13 configuration by name.

## 10. What is NOT done

* No SoftAP / DHCP emulation. The opcode-10 hook dials a configured address
  over loopback; the pen does not drop BLE while Wi-Fi is up.
* No OTA behaviour (types 20-22), no Tips/UniversalErr emission, no paged file
  list, no resume verification beyond `start > 0` slicing.
* The pen does not tell the phone when a transfer task fails
  (`transfer_failed` is logged only); the phone's request times out.
* A second closeWiFi is answered with status 0; the template claims real
  firmware may answer an error status (`SyncManager.kt:531-532`).
* Not validated against a real phone or pen; every phone-side behaviour comes
  from bytecode, every pen-side behaviour is policy constrained by that bytecode.
* The Kotlin phone's permissive receive (`replay_check=False`) is implemented in
  the sealer and covered by the codec tests, not by a session test.

## 11. R7-S14: the genuine SDK against this track (2026-09-25)

Report and evidence: `r7/r7-s14-wifi-real-sdk.md`, `r7/r7-s14-evidence/`
(runs 1-5 on the API-34 AVD; driver `WifiCaptureActivity.kt`, device
`r7/wifi_capture_device.py`). Summary of what changes here:

* **How far the real SDK gets.** `startWifiTransfer` → CONNECTING → BLE
  opcode 10 (`01 0a 00 00`) answered → `WifiConnectionManager.connectToDeviceWifi`
  → `ConnectivityManager.requestNetwork(WifiNetworkSpecifier{SSID PLAUD0001,
  WPA2 10000001}, cb, 30000)` → the AVD has no such network → `onUnavailable`
  after 30.0 s → `onError(1003, "Failed to connect to device WiFi")`. The
  WebSocket server is started only AFTER a successful join
  (`LambdaTaskRunner.txt:138-169`), so port 8081 never listened and the pen
  (dialling through `adb forward`) never connected: **zero Wi-Fi PDUs** with the
  real SDK. Everything in sections 2-5 about the phone's server remains
  BYTECODE_PROVEN only.
* **Fix (emulator bug the real SDK exposed).** `profile._open_wifi` read the
  opcode-10 byte as on/off and treated 0 as "drop the hotspot"; the SDK's
  fast-transfer open sends 0 (`KappaValueObject.txt:57`, `:64`; runtime write
  `010a0000`), so the pen never started. Every opcode 10 now opens; the byte is
  logged as `mode` (meaning UNKNOWN; iOS `operateWiFi(open:isOTA:…)` hints
  `isOTA`). Regression tests:
  `test_r7_s14_sdk_fast_transfer_open_mode_zero_starts_the_wifi_device`,
  `test_r7_s14_the_sdks_recorded_ble_sequence_opens_a_pullable_wifi_session`
  (plus the updated `test_open_wifi_request_layouts`,
  `test_open_wifi_reports_status_and_serial_derived_passphrase`,
  `test_second_open_does_not_start_a_second_wifi_device`); all four
  changed/new ones fail against the pre-fix handler.
* **Close path (SDK side, not changed here).** `q.P` registers the CloseWiFi
  bean on opcode `{10}` (`ALL.txt:46011-46026`) while `l0.a()` is 13
  (`ALL.txt:55388-55410`): an opcode-13 answer never reaches the callback
  (runs 1b-3, 5); an opcode-10 answer reaches it and `l0` throws "l0 Mismatch"
  → status -1 (run 4, experiment). The emulator keeps answering 13; what real
  firmware sends is UNKNOWN.
* The `startWifiTransfer` String argument (facade name `sn`, SDK name
  `userId`) is dead: it reaches `connectToDeviceWifi`'s third parameter and is
  overwritten (`WifiConnectionManager.txt:47`).

