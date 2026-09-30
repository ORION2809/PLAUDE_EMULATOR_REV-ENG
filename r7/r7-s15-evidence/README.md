# R7-S15: the real SDK's Wi-Fi transfer, completed (29 September 2026)

**Result.** On an Android emulator, Plaud's unmodified Android SDK (the debug
build of `r7/android-app`, blank SDK token) bound to the BLE emulator and opened
the pen's Wi-Fi over Bluetooth. It then joined a simulated `PLAUD0001` network,
accepted the WebSocket connection of our Wi-Fi device emulator, and completed the
Wi-Fi handshake. Over Wi-Fi it listed the recording and downloaded it: 11 271
bytes with sha256 `0f45367bd1eae540a51f2741e3150ffc705aace3054bfdaa4e134b71cbb48c3d`,
identical to the served file (`tests/fixtures/r6s2_16k_mono.ogg`), both as
downloaded and as exported to OPUS. No log line of either run shows a `gen-key`
request. This is the step R7-S14 (`r7/r7-s14-wifi-real-sdk.md`) could not reach,
because the emulated phone had no network to join.

## What changed since R7-S14

The Android emulator's simulated Wi-Fi comes from `netsimd`, which normally offers
one network, `AndroidWifi`. `netsimd --wifi <ssid> <password>` offers a chosen one
instead. `run.sh` starts the emulator with `-netsim-args "--wifi PLAUD0001
10000001 --pcap"`, the SSID and WPA2 passphrase the SDK asks Android to join
(bytecode; `r7/r7-s14-wifi-real-sdk.md` D2 for the SSID, §2.2 and checker note 2
for the passphrase). It also approves Android's network-request dialog
("Connect to device") with a uiautomator tap on its Connect button; R7-S14
approved nothing. From run 2 on, the emulator starts with `-dns-server 127.0.0.1
-http-proxy 127.0.0.1:9` (the egress guard below). The driver passes a blank SDK
token. Otherwise the rig is R7-S14's: `r7/wifi_capture_device.py` as the pen,
reached through `adb forward tcp:18081 tcp:8081`, and `WifiCaptureActivity --es
mode transfer`.

## Runs

| run | netsim / emulator flags | what happened |
|---|---|---|
| run1 | `--wifi PLAUD0001 10000001 --debug-no-network --pcap` | The phone listed `PLAUD0001` (WPA2-PSK). Android showed its "Connect to device" dialog for the SDK's network request; `run.sh` tapped its Connect button (uiautomator, 16:42:19). The phone associated and completed the WPA2 4-way handshake with the SDK's passphrase. It then got no DHCP answer (`--debug-no-network` also stops netsim's DHCP), and the SDK reported `onError(1003)` "Failed to connect to device WiFi" at +30.095 s. The pen dialled 31 times; nothing listened |
| run2 | `--wifi PLAUD0001 10000001 --pcap`; emulator `-dns-server 127.0.0.1 -http-proxy 127.0.0.1:9` | Android bypassed the dialog: "Approved network found" (the approval of run 1 is remembered per app). Joined at +3.9 s after the request (address 10.0.2.16). The SDK started its WebSocket server on port 8081 once the network was available, and our device connected at the 6th dial. Handshake done at +5.2 s after `startWifiTransfer`; file list; `FileSync` 0-11271; three `FileSyncContent` frames (4096 + 4096 + 3079, the last marked last); download and OPUS export byte-exact; heartbeats; our device closed after idle heartbeats |

Run 2's Wi-Fi PDUs, both directions, are in `run2/wifi-transfer-capture.json`
(`wifi`): `SayHello` (device) → `Handshake` (phone; token `"0" * 32`, a stamp) →
`Handshake` status 0 (device) → `GetFileList` twice → `FileSync {session, scene 2,
start 0, end 11271}` → three `FileSyncContent` → three `Heartbeat`s from the device (the
phone answered two; the third answer was cancelled at close) → `WifiClose` (device,
`idle_heartbeats`).

## Egress guard

Run 1's `--debug-no-network` also removed DHCP, so run 2 cut egress another way:
the emulated DNS server is 127.0.0.1 on the host, where nothing answers DNS (a
host-side `dig @127.0.0.1` timed out; not archived), and every TCP connection goes
through an HTTP proxy at 127.0.0.1:9, where nothing listened (checked with `nc`;
not archived). The SDK's network is app-private: from the
shell, 8.8.8.8, `platform-jp.plaud.ai` and 1.1.1.1:443 were unreachable while the
phone was on `PLAUD0001` (`run2/wifi-transfer-egress.txt`). Android did not probe
the network ("would not satisfy default request, not validating"). The only
`plaud.ai` lines in either logcat are the SDK's base-URL setup, as in every earlier
run. Not shown: the guard from inside the SDK's own process, and a packet capture
(netsim discarded its capture when it stopped).

## What this does not show

* **The data did not travel over the simulated radio.** The phone joined the
  simulated `PLAUD0001` network, and the SDK started its server only after that
  network was available. But our device reached the server through
  `adb forward` to the phone's port 8081 (the SDK logged "Device connected:
  127.0.0.1"), not across the simulated Wi-Fi link. A real pen reaches the
  phone over its own access point.
* **The handshake token was empty.** With the blank SDK token (the rule since
  25 Sep), the SDK logged `generateToken: handshake token is EMPTY (partnerToken
  未就绪?), WiFi 握手将失败` ("partnerToken not ready? the Wi-Fi handshake will
  fail") and sent 32 zeros. Our emulator accepts any token (HARNESS_POLICY); a real pen
  may not.
* **No encryption.** The SDK saved no session key (`hasKey=false`), so the
  session was plain.
* **Run 2 depended on run 1's approval** of Android's dialog, which a script
  tapped. A user would tap it once.
* One successful run, on one AVD (API 34, emulator 37.1.11.0), with our
  emulator as the device. Nothing here shows how a real recorder behaves.

## Files

`run.sh` (run 2's commands; run 1 had `NETSIM_ARGS` with `--debug-no-network`,
no `-dns-server`/`-http-proxy` and no egress check; after run 2 it gained an EXIT
cleanup trap, a refusal to start while any emulator is running (it kills and
restarts netsimd, which all emulators share), and a corrected comment on what the
egress probes test). Per run: `run.log`,
`avd.log`, `netsimd-args.txt`, `wifi-scan.txt`, `ping.txt`,
`wifi-transfer-logcat.log.gz` (the full logcat: 1 935 and 4 736 lines),
`wifi-transfer-capture.json` (BLE and Wi-Fi traffic at the pen),
`wifi-transfer-peripheral.log`, `wifi-transfer-dialog.log`,
`wifi-transfer-wifi-status.txt`; run 1 also `wifi-transfer-dialog-dialog.xml` (the
UI dump of the approved dialog); run 2 also `wifi-transfer-egress.txt`.
`SHA256SUMS` covers every file.
