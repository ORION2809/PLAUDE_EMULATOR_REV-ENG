# R7-S16: the real SDK's Wi-Fi transfer over the emulated Wi-Fi link (plan; not yet run)

**Status (30 September 2026): not yet run.** This directory holds the run script, the evidence
checker and this plan. No emulator has been started for R7-S16, so none of the evidence below exists yet.

## Why

In R7-S15 the SDK joined a simulated `PLAUD0001` network and completed the transfer byte-exact.
However, our pen reached the SDK's WebSocket server through `adb forward`. adbd inside the phone
connected to its own loopback, and the SDK logged `Device connected: 127.0.0.1`. The bytes never
crossed a Wi-Fi link. R7-S16 makes the pen's connection arrive on the phone's `wlan0`, from the
Wi-Fi network's gateway address.

## What is simulated, and what is not

The Wi-Fi in R7-S16 is **the emulator's own simulated Wi-Fi**, which Google calls "the previous
networking model" (`-feature -WiFiPacketStream`). It works like this:

* the phone's `wlan0` (mac80211_hwsim over virtio-wifi) exchanges 802.11 frames with an access
  point that runs inside the emulator process (`HostapdController`, WPA2-PSK/CCMP);
* that access point passes its data frames to a QEMU user-mode network, the netdev `virtio-wifi`.

It is **not netsim's shared Wi-Fi medium**, the one R7-S15 used. Bluetooth still goes through netsim.

netsimd 37.1.11 has no way for host traffic to enter its Wi-Fi network. Each reason below comes
from the netsim source, with file and line; `run.sh` carries the same citations in its header.

* **No port forwarding.**
  * `rust/daemon/src/args.rs` has no flag for it.
  * The `SlirpOptions` message in `proto/netsim/config.proto` has no field for it.
  * `rust/daemon/src/wifi/libslirp.rs` passes only `http_proxy` and `host_dns` to the network.
  * The slirp command enum has no host forward (`rust/libslirp-rs/src/libslirp.rs:132-138`).
* **`--wifi-tap` is `todo!()`** (`rust/daemon/src/wireless/wifi_manager.rs:46-47`).
* **A second station would break the phone's link.**
  * netsim's access-point driver keeps one pairwise key: `driver_virtio_wifi.c:24-31` in
    wpa_supplicant_8, branch `emu-master-dev`.
  * `set_key` overwrites that key (lines 446-454).
  * hostapd-rs encrypts and decrypts every unicast frame with it (`rust/hostapd-rs/src/hostapd.rs:280-392`).

  So a second WPA2 station joining `PLAUD0001` would replace the phone's key. This is read from the
  source; it was not tested.

A netsim-faithful run would need a netsimd patched with a slirp host forward. This Mac cannot
build one: it has no Xcode and about 7 GB of free disk.

## How the pen gets in

1. **The Wi-Fi netdev gets a host forward.** Inside the qemu process, `-wifi-user-mode-options`
   becomes `-netdev user,id=virtio-wifi,<opts>` (emulator `android-qemu2-glue/main.cpp:3104-3110`).
   `ps` never shows that string, only the requested option; qemu prints the argv it built when the
   emulator runs with `-verbose`, which `run.sh` now passes in every mode. The options are:

   ```
   dhcpstart=10.0.2.16,restrict=on,hostfwd=tcp:127.0.0.1:18081-10.0.2.16:8081
   ```

2. **The pen is unchanged** (`r7/wifi_capture_device.py`). It dials `ws://127.0.0.1:18081`, as it
   did in R7-S15. The difference is who listens on that port: QEMU, not adb.
3. **QEMU carries the connection into the phone.** QEMU connects to `10.0.2.16:8081` over the Wi-Fi
   netdev. libslirp presents a host-loopback source as the gateway (`sotranslate_accept`), so the
   SDK should log `Device connected: 10.0.2.2`.
4. **`restrict=on` blocks egress.** It isolates the phone on that network. DHCP and the host forward
   still work (libslirp `udp.c` and `tcp_input.c:383-391`). Pings to the gateway are still answered,
   by libslirp itself (`ip_icmp.c:206-210`); pings anywhere else are dropped.
5. **The console sets the network.** The command `wifi add PLAUD0001 10000001`
   (`console.cpp:994-1005`) gives the access point the pen's SSID and WPA2 passphrase. The
   `WifiConfigurable` feature must be on for this command (`console.cpp:181-184`). It writes BSSID
   `00:13:10:95:fe:0b` (`HostapdController.cpp:60`). The emulator's template access point uses
   `00:13:10:85:fe:01`, the same BSSID as netsim's, so the BSSID shows whether `wifi add` took effect.
   It does not show which medium carries the Wi-Fi; E2 does that.
6. **The options pass the validator; a refusal would cost restrict.** The emulator checks each
   option against an allowlist that includes `dhcpstart`, `restrict` and `hostfwd`
   (`android/emu/cmdline/src/android/cmdline-option.cpp:433-461`, branch `emu-master-dev`; the
   installed 37.1.11 binary embeds the same file). If it ever refused one, `main.cpp` would replace
   the whole option string with `dhcpstart=10.0.2.16`, so `restrict` would be off for the entire
   boot. The script's fallback adds the forward through the QEMU monitor
   (`hostfwd_add virtio-wifi tcp:127.0.0.1:18081-10.0.2.16:8081`), but it cannot add `restrict`.
   After a refusal, a transfer therefore runs only with `REQUIRE_RESTRICT=0`.

**A side effect on the SDK install, undone by the script.** `wifi add` rewrites the file hostapd
started from, and on a boot from the template that file is the shared
`$ANDROID_SDK/emulator/lib/hostapd.conf` (`HostapdController.cpp:89-93, 135, 157-176, 221-227`).
Left alone, every later boot without `WiFiPacketStream` would start a WPA2 `PLAUD0001` access
point. So `run.sh`:

* refuses to start unless the template has its pristine sha256 (`HOSTAPD_TEMPLATE_SHA256`,
  `ec59f9e5…` for emulator 37.1.11);
* copies it to `hostapd.conf.orig`;
* restores it after the emulator stops, keeping the rewritten copy as
  `hostapd.conf.as-left-by-emulator` (evidence for U3) and recording the result in
  `hostapd-template.txt`.

If the script is killed with SIGKILL before the restore, the next run refuses until the template
is restored from that run's `hostapd.conf.orig`.

`run.sh` creates no `adb forward`.

## Plan

```
r7/r7-s16-evidence/run.sh <out> probe            # boot; settle the six unknowns; no SDK run
r7/r7-s16-evidence/run.sh <out> probe transfer   # then the real SDK's transfer, same boot
FWD=0 r7/r7-s16-evidence/run.sh <out2> control   # control boot without the host forward
```

**Before it starts**, `run.sh` refuses to run in any of these cases:

* an emulator is running, or the console/adb ports 5554/5555 are taken (the emulator must be `emulator-5554`);
* a process named `netsimd` is already running (`KILL_NETSIMD=1` stops it first; only processes with
  that exact name are ever signalled);
* the SDK's `emulator/lib/hostapd.conf` is not pristine;
* swap in use exceeds 4 GB;
* free, inactive and speculative memory together are below 3 GB;
* a heavy job is running (`HEAVY_JOBS`);
* port 18081 or the monitor port is taken;
* an `adb forward` uses `tcp:18081`.

`FORCE_MEMORY=1` overrides the memory and heavy-job checks. One AVD needs about 4 GB of RAM, and
this Mac has 8 GB shared with other jobs.

**When it finishes**, the script, in order:

1. stops the emulator with `emu kill`, then with TERM and KILL to this run's own emulator PIDs if needed;
2. restores the hostapd template;
3. stops the `netsimd` that served this run;
4. runs `check_wifi_pcap.py`.

The checker writes `wifi-<mode>-pcap-check.json` and exits 0 (pass), 1 (fail), 2 (input or internal
error) or 3 (inconclusive). A capture defect, such as a TCP gap (also at the end of a stream, from
the peer's ACKs) or a truncated pcap, turns a mismatch into "inconclusive" rather than "fail".

### The six unknowns (the probe mode, `probe/probe-results.txt`)

| # | unknown | how the probe settles it |
|---|---|---|
| U1 | Does the emulator accept `hostfwd` and `restrict` in `-wifi-user-mode-options`? | The allowlist in the validator's source says yes. The probe confirms it: the `virtio-wifi` netdev qemu built, read from the `-verbose` argv lines in `avd.log` (and the monitor's `info network`), the validator messages, and which process owns port 18081. If the options were refused, the monitor fallback adds the forward (not `restrict`) |
| U2 | Does `-feature -WiFiPacketStream,WifiConfigurable` work? | `avd.log` should lack "Successfully initialized netsim WiFi", netsim should list no Wi-Fi chip, and `wifi add` should answer `OK` |
| U3 | Does `wifi add` work on API 34 in this mode? | Its reply, the scan showing `PLAUD0001` with BSSID `00:13:10:95:fe:0b` (which `wifi add` writes), and `hostapd-template.txt` / `hostapd.conf.as-left-by-emulator` |
| U4 | Does `filter-dump` work on the netdev `virtio-wifi`? | Packets in `wifi-virtio-8023.pcap` (`--summary`) |
| U5 | Does the host forward work while `-http-proxy` is set? | The shell joins `PLAUD0001` (and forgets it afterwards), `toybox nc` listens on 8081, and the host dials 18081. Each side must receive the other's line. Afterwards the guest's `nc` is stopped whether or not the dial reached it (`guest-listeners-after-probe.txt`), and the transfer refuses to start while anything still LISTENs on 8081 in the guest |
| U6 | Does the emulator's own libslirp behave like libslirp master? | The phone must see the dial come from 10.0.2.2 (`/proc/net/tcp`). Every egress probe made while the phone is joined must fail. The pcap's egress record counts answered guest TCP, UDP and ICMP/ICMPv6 echo flows |

## Evidence list (E1-E10)

| # | evidence | file(s) |
|---|---|---|
| E1 | The Wi-Fi netdev as qemu built it, with `hostfwd`, `restrict=on` and the `filter-dump`: the `-verbose` "QEMU options list" argv lines from `avd.log`, plus (MONITOR=1) the live `info network` / `info usernet`. `qemu-args.txt` (`ps`) holds only the requested launcher options | `qemu-core-args.txt`, `qemu-monitor-info.txt`, `boot-state.txt`; `qemu-args.txt`, `emulator-command.txt` |
| E2 | netsim does not carry Wi-Fi: `avd.log` has no "Successfully initialized netsim WiFi" (R7-S15's did) and netsim lists no Wi-Fi chip; Bluetooth still uses netsim | `avd-wifi-lines.txt`, `netsim-devices.json`, `netsimd-args.txt` |
| E3 | Port 18081 is owned by `qemu-system-aarch64`, and `adb forward --list` has no `tcp:18081` at any point | `hostport-listener.txt`, `adb-forward-*.txt`, `wifi-transfer-adb-forward.txt` |
| E4 | The phone sees `PLAUD0001` (WPA2-PSK-CCMP) with BSSID `00:13:10:95:fe:0b`, which shows that `wifi add` took effect. It does not show the medium (the template's BSSID equals netsim's); E2 does | `wifi-add.txt`, `wifi-scan.txt`, `hostapd-template.txt` |
| E5 | The SDK logs `Device connected: 10.0.2.2`, and no `gen-key` line appears | `wifi-transfer-logcat.log`; check `logcat_device_connected` |
| E6 | The phone's socket table shows `10.0.2.16:8081 <-> 10.0.2.2:x ESTABLISHED`. In hex this is `1002000A:1F91 0202000A:xxxx 01`, or its `::ffff:` form in `tcp6` | `wifi-transfer-proc-net-tcp.txt`; check `proc_net_tcp_peer` |
| E7 | The Wi-Fi-netdev pcap holds the TCP stream 10.0.2.2 → 10.0.2.16:8081. Each WebSocket message (client frames unmasked) must equal the pen's capture JSON, in order, both ways. The `FileSyncContent` payloads must re-hash to `0f45367b…` | `wifi-virtio-8023.pcap`, `wifi-transfer-capture.json`; checks `ws_*`, `pdu_out_match`, `pdu_in_match`, `file_sha256` |
| E8 | The Ethernet netdev (`mynet`, emulator `-tcpdump`) has no port-8081 packet. This is the negative control | `eth0-mynet.pcap`; check `eth_no_port` |
| E9 | The Wi-Fi TX/RX counters rise across the transfer. The egress record shows no answered guest-initiated TCP connection, no answered UDP flow (other than DHCP), and no ICMP/ICMPv6 echo answered from outside the user-mode network | `wifi-transfer-status-{before,after}.txt`; `other_flows` and `egress` in the report; check `egress_none` |
| E10 | Control (`FWD=0`): nothing listens on 18081 and the pen cannot connect. There is no port-8081 payload, no `Device connected` line and no ESTABLISHED row. That absence counts only if the run was live: the Wi-Fi pcap is complete and carries guest traffic, the guest LISTENed on 8081, the SDK logged "Starting WebSocket server on port 8081", the pen logged dial attempts, and the phone was on `PLAUD0001`. Otherwise the result is "inconclusive" | `<out2>/wifi-control-*`; `--expect-none` checks and `control_liveness` |

## Risks and what this cannot show

* **Only the 802.3 side is captured.** The pcap holds the Ethernet frames after the emulated
  access point removed WPA2. No 802.11 capture exists in this mode.
* **Possible egress at boot.** Before `wifi add` runs, the access point is the template's open
  `AndroidWifi`, and the phone may join it at boot. (This holds for every run, because the script
  restores the template.) `restrict=on` normally covers that time. If the validator ever refused
  the options, `restrict` would be off for the whole boot. `wifi-status-boot.txt` records what
  happened.
* **The approval dialog may come back.** Android may show its network-request dialog again because
  the BSSID changed. `approve_dialog` taps it, as in R7-S15.
* **The QEMU monitor has no authentication.** With `MONITOR=1` (the default) it listens on
  `127.0.0.1:45454` until the script has stopped the emulator. `MONITOR=0` disables it, and with it
  the fallback and the `info usernet` evidence; the netdev and `restrict` are still read from `avd.log`.
* **One emulator, one synthetic pen.** As in R7-S15, the handshake token is empty (blank SDK token)
  and the session is unencrypted. Nothing here shows how a real recorder behaves.

## Files

* `run.sh`: the run script (probe, transfer, control).
* `check_wifi_pcap.py`: the evidence checker (standard library only; tests in
  `tests/test_r7_s16_pcap.py`).
* `README.md`: this plan.

Run outputs are to be added after the run, with `SHA256SUMS`.
