# R4-S2 transport spike (EXPERIMENTAL, disposable)

Goal: prove Android runtime <-> netsim <-> Bumble BLE transport with a
ping/pong payload. Generic test client, no Plaud code.

## Layout

- `android-app/` — minimal Kotlin app: scan -> connect -> discover ->
  subscribe -> write "ping" -> expect "pong" -> disconnect. Logs only.
- `bumble_pingpong.py` — Bumble peripheral on netsim transport: one
  service, one write char, one notify char. Write "ping" -> notify "pong".
- `run.sh` — exact reproduce commands (order matters).

## Results (2026-09-22 — VERIFIED)

Android logcat (`adb logcat | grep PingPong`):

```text
PINGPONG_APP_START
PINGPONG_ADAPTER enabled=true le=true
PINGPONG_SCAN_START
PINGPONG_SCAN_SEEN addr=FB:9C:46:88:E5:57 uuids=[0000feed-...]
PINGPONG_SCAN_MATCH
PINGPONG_CONNECT
PINGPONG_CONN status=0 state=2
PINGPONG_SERVICES status=0
PINGPONG_SERVICE found=true
PINGPONG_CCCD status=0
PINGPONG_WRITE_PING queued=true
PINGPONG_NOTIFIED value=pong
PINGPONG_SUCCESS
PINGPONG_WRITE_DONE status=0
PINGPONG_DISCONNECTED
```

Bumble log (`/tmp/r4s2-bumble.log`):

```text
PINGPONG_WRITE_RX bytes=70696e67   (= "ping")
PINGPONG_NOTIF_TX pong
```

Verdict: `Android write: ping → Bumble received: ping → Bumble
notification: pong → Android received: pong`, over
`Android Bluetooth API → virtual controller → netsim → Bumble`.

## Environment versions

- Android SDK + build-tools 34.0.0, JDK Temurin 17.0.20.1, Gradle 8.9, AGP 8.5.2
- AVD `mivi_test_34` (API 34, arm64) + `hw.bluetooth=yes`, netsimd bundled
  with the emulator (gRPC endpoint published via `$TMPDIR/netsim.ini`)
- Bumble at `reference/upstream/bumble` pin `d371a27` + grpcio/protobuf
- Test APK: `android-app/app/build/outputs/apk/debug/app-debug.apk`