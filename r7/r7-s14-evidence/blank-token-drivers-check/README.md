# Blank-token re-runs of the K3 and Wi-Fi drivers (28 September 2026)

On 25 Sep the three debug drivers were changed to pass a blank token to
`PlaudDeviceAgent.initSDK`: with a non-blank token the SDK POSTs
`partner/sdk/gen-key` to `platform-jp.plaud.ai` at every app start
(`r7/r7-s14-wifi-real-sdk.md`, D7). Only the pull driver was re-run that way
then (`../blank-token-offline-check/`). This folder re-runs the other two, from
the debug build in which the drivers live in the debug source set
(`r7/android-app/app/src/debug/`).

Setup (`run.sh`, paths anonymised): API-34 AVD `mivi_test_34`, fresh install of
the debug APK, `pm clear` before each run, Bluetooth permissions granted, the
phone's Wi-Fi and mobile data disabled (`svc wifi disable; svc data disable`;
`ping 8.8.8.8` → "Network is unreachable"), our Python peripheral attached through
netsim. The full, unfiltered logcat of each run is archived (gzip).

| run | driver | result (logcat) | `gen-key` lines | full logcat lines |
|---|---|---|---:|---:|
| K3 | `K3CaptureActivity --es id SYNTH-HIST-0001` | legacy handshake `old_protocol_ok`, `K3CAP_BIND status=0` | 0 | 4846 |
| Wi-Fi open | `WifiCaptureActivity --es mode open` | `WIFICAP_BIND status=0`, `WIFICAP_BLE_WIFI_OPEN status=0` | 0 | 1492 |
| Wi-Fi transfer | `WifiCaptureActivity --es mode transfer` | `WIFICAP_BIND status=0`; with the phone's Wi-Fi off the join fails at once: `onError(1003) "Failed to connect to device WiFi"` at +151 ms (with Wi-Fi on, R7-S14 saw a 30 s timeout) | 0 | 1479 |

Each log carries the `PlaudSdk_SourceFile` tag that logged every earlier
`gen-key` request (7, 7 and 8 lines here: base-URL configuration only, e.g.
"Creating Partner API service with base URL: https://platform-jp.plaud.ai").
No run requested any URL.

What this shows: with a blank token, none of the three drivers' SDK
initialisation asks Plaud's server for anything, on this build. The Wi-Fi
transfer run is not a repeat of R7-S14 (the phone was offline); it only shows
the driver runs and ends cleanly.

`SHA256SUMS` covers every file here.
