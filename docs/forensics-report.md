# Plaud Harness Forensic Report

Inspection date: 2026-09-21. Scope: read-only inspection of the project and all
checked-out repositories under `reference/`. No implementation code was changed.

## 1. PROJECT INVENTORY

The project is a git repository containing the source-of-truth project brief in
`PROJECT.md`, a short README, acquisition scripts, and an evidence directory.
`reference/` contains 16 Plaud-AI repositories, 6 third-party Plaud ecosystem
repositories, and 11 upstream/reference repositories, for 33 repository roots
including the official SDK repository. The checked-out roots and purposes are
listed in [repository-map.md](repository-map.md).

The project-owned directories `emulator/plaudsim`, `generator`, `pipeline`,
`evals`, `tests`, and `data/corpora` contain no implementation or corpus files.
The current worktree has user changes in `PROJECT.md` and untracked investigation
documents/scripts; these were preserved.

## 2. REPOSITORY RELATIONSHIPS

The recorded remotes and shallow pins are in `docs/reference-pins.txt`; the
repositories retain their own remotes. Confirmed relationships include:

- `client-sdk-esp32` follows the `hayden-xiong/client-sdk-esp32` to
  `livekit/client-sdk-esp32` lineage.
- `xiaozhi-esp32` follows `78/xiaozhi-esp32`.
- `live-agent` follows `xinnan-tech/xiaozhi-esp32-server`; it is not a fork of
  `livekit/agents`. The latter was supplied as a comparison target.
- `live-agent-memory` follows `mem0ai/mem0` and is archived.
- `plaud-opik` follows `comet-ml/opik`.
- Plaud's GoReplay copy is compared with `probelabs/goreplay`.
- `riffado` is the renamed/current home of the earlier `openplaud` project.

The official SDK is the only supplied repository with Plaud-specific device
protocol artifacts. Other repositories implement cloud access, sync, audio,
observability, or generic Bluetooth infrastructure.

## 3. EXISTING FUNCTIONALITY

The official SDK repository contains iOS and Android template applications,
precompiled `PlaudBleSDK`, `PlaudWiFiSDK`, and `PlaudDeviceBasicSDK` frameworks,
an Android `plaud-sdk.aar`, public generated Swift interfaces, Objective-C
headers, and native libraries. The templates exercise scan/connect/disconnect,
record start/stop/pause/resume, device state, file list/sync, Wi-Fi fast
transfer, firmware UI, and cloud binding flows.

Its mock managers only simulate UI state and sample files. They do not implement
GATT, packet framing, a BLE peripheral, or a transfer protocol.

The Plaud cloud clients expose recordings, transcription segments, speakers,
summaries, tags, file URLs, and analysis status. Their tests contain representative
JSON fixtures and endpoint expectations.

`riffado` provides a mature sync/transcription application with audio sniffing,
decode/compression, repetition-loop protections, encryption-at-rest, storage,
and many regression tests. `applaud` and `plaud-toolkit` provide additional cloud
sync and workflow implementations.

`bumble` provides the generic GATT server/client, advertising, link, transport,
and testing machinery needed for a software-only peripheral. It contains no
Plaud UUIDs or Plaud packet implementation.

## 4. GOLDEN IMPLEMENTATIONS

| Subsystem | Golden source | Confidence | Reason |
|---|---|---:|---|
| Plaud BLE public API | `plaud-sdk-public/sdk/ios/PlaudBleSDK.framework/Modules/*swiftinterface` and headers | High | Official, unobfuscated public declarations. |
| Plaud protocol constants/layout | `plaud-sdk-public/sdk/android/plaud-sdk.aar`, plus `scripts/extract_protocol.py` output | Medium to high | Constants and class structure survive obfuscation; payload semantics remain incomplete. |
| Plaud device lifecycle | Official iOS/Android template managers and SDK interfaces | High | Real client call paths, though behavior is delegated to binaries. |
| BLE emulator substrate | `reference/upstream/bumble` | High | Generic virtual/emulated Bluetooth and GATT test support. |
| Cloud entity schema | `reference/third-party/plaud-api` | Medium | Explicit Pydantic models and mocked API tests. |
| Cloud sync behavior | `reference/third-party/riffado`, `applaud-rsteckler`, `plaud-toolkit` | Medium | Independent implementations; not official protocol sources. |
| Audio decode/sniffing | `reference/third-party/riffado/src/lib/audio` and transcription modules | Medium | Production-shaped code and regression tests. |
| Synthetic room simulation | `reference/upstream/pyroomacoustics` | High for generic acoustics | Dependency candidate only; no project integration exists. |
| TTS | `reference/upstream/kokoro` | Medium | Candidate dependency only; no local generator exists. |
| Metrics/evals | None locally | None | `evals/` is empty; metric libraries are only named in docs. |

## 5. PROTOCOL RECONSTRUCTION

The following is supported by shipped artifacts and the existing extraction
output, not by a live hardware capture.

### BLE/GATT

- Primary service: `00001910-0000-1000-8000-00805f9b34fb`.
- Custom characteristics: `00002BB0-0000-1000-8000-00805f9b34fb` and
  `00002BB1-0000-1000-8000-00805f9b34fb`.
- CCCD: standard `00002902-0000-1000-8000-00805f9b34fb`.
- Battery service/level: standard `0000180f-...` / `00002a19-...`.
- Decompiled Android code confirms `2BB0` is the device-to-host notification/
  indication path and `2BB1` is the host-to-device write path. The runtime
  property mask and write-with-response mode remain unknown.

### Framing and codec evidence

- Protocol type 1 frames begin with one byte followed by a little-endian u16
  opcode; protocol types 2, 3, and 5 use a one-byte type plus u32 `0xffffffff`.
- Recovered sentinels include `0xFE10`, `0xFE11`, `0xFE12`, and `0xFE20`.
- The JNI utility exposes integer packing/reading at 8/16/24/32/64 bits and
  CRC helpers. The Java helpers establish little-endian encoding.
- Native disassembly confirms CRC-16/CCITT-FALSE with polynomial `0x1021`,
  initial `0xFFFF`, no reflection, and xorout `0x0000`. Control-frame coverage
  and wire byte order remain unknown.
- The extracted tables contain 74 request entries and 98 response entries,
  with obfuscated classes and only partial semantic names.

### Client capability and transport

The public interface exposes common read/set settings, recording scenes and
modes, VAD sensitivity, microphone/VPU gain, battery behavior, auto power-off,
raw-wave options, sync behavior, and Wi-Fi configuration. The iOS SDK exposes
recording controls, file-list/sync callbacks, and a separate Wi-Fi agent with
WebSocket-related APIs. The interface supports a BLE control path and a Wi-Fi
fast-transfer path, but the exact on-wire Wi-Fi handshake is not reconstructed.

The SDK exposes Ogg/Opus decoding and MP3 conversion helpers, so the artifact
supports an Opus/Ogg-to-phone conversion path. A claim that device files are
always Opus bytes with an `.mp3` extension is not yet independently demonstrated
by a local fixture.

## 6. PROTOCOL UNKNOWNs

- Fixed runtime property masks for `2BB0`/`2BB1`, and whether `2BB1` uses write
  with response or write without response.
- Advertising payload, device name rules, manufacturer data, and serial-number
  encoding.
- Pairing/authentication sequence, including RSA key exchange and token fields.
- CRC control-frame coverage and wire byte order.
- Complete request/response opcode semantics and aliases.
- Variable-length field boundaries, sequence/ACK/retry rules, and error frames.
- Whether payloads are protobuf, custom structs, or a mixture; the existing
  report's protobuf hypothesis has not been verified in this pass.
- Recording-list and file-transfer state machines.
- Wi-Fi SSID/password behavior, WebSocket handshake, and transfer framing.
- Device-generation differences between Note Pro, NotePin, and other Tinno-based
  products.
- No real packet capture, hardware trace, or SDK-to-emulator exchange exists yet.

## 7. CODE WE CAN REUSE

- Official SDK public interfaces, headers, and template call paths as the client
  contract: `reference/plaud-org/plaud-sdk-public/sdk/ios/...` and
  `ios/PlaudTemplateApp/Managers/...`.
- `docs/protocol.json` as the machine-readable extraction baseline, regenerated
  by `scripts/extract_protocol.py` when SDK analysis inputs are available.
- Bumble's GATT server/client, virtual transport, and tests under
  `reference/upstream/bumble/bumble` and `reference/upstream/bumble/tests`.
- `plaud-api` models and fixtures for cloud-side recording/transcript/speaker/
  summary/tag contracts.
- Riffado's audio sniff/decode/compression and regression tests as isolated
  references, preserving provenance and its AGPL boundary in project records.
- GoReplay's capture/replay engine for a later local HTTP mock, if needed.

## 8. CODE THAT NEEDS ADAPTATION

- Bumble needs a Plaud-specific GATT profile, advertisement, peripheral state
  machine, frame codec, and fault-injection hooks. No such adapter exists yet.
- The official iOS/Android SDK is binary and platform-specific; it cannot be
  used as a Python test client without a platform bridge. The first client-side
  proof may require a small iOS/Android integration runner or a separately
  verified central implementation.
- `scripts/extract_protocol.py` assumes decompiled AAR source and emits partial
  layouts; it needs more evidence and tests before driving an emulator.
- Cloud clients require token-based network access and should be redirected to a
  local mock before use in deterministic tests.
- Riffado and other workflow repositories are application-scale references, not
  drop-in pipeline modules for the empty project directories.

## 9. EXISTING EMULATOR / MOCKS

No Plaud BLE emulator or protocol mock was found. The official SDK mocks are
`MockDeviceManager`, `MockRecordingManager`, and `MockSyncManager` in the iOS
template, with Kotlin equivalents. They emit canned state, IDs, files, and
delays for UI development; they never advertise, discover GATT services, parse
packets, transfer bytes, or inject link faults.

Bumble is a generic emulator substrate, not an existing target implementation.
Therefore the project must adapt Bumble rather than reuse an existing Plaud
emulator.

## 10. DATA INVENTORY

No audio corpus or generated fixture is present in `data/`. The supplied code
contains small cloud JSON fixtures in `plaud-api` tests and a sample MP3 fixture
in Riffado tests. The official template contains mock metadata and file paths,
but not usable device packet captures or a complete recording corpus.

The upstream directory contains source code and generic Bluetooth test assets,
not AMI/LibriCSS/ICSI/VoxConverse/MUSAN/RIRS audio. Dataset acquisition is
documented in `scripts/fetch-datasets.sh` and should wait until the local
protocol/data requirements are settled.

## 11. TEST INVENTORY

The supplied repositories contain substantial upstream tests: official SDK
template mocks are not protocol tests; `plaud-api` has model and API tests with
recording, transcription, speaker, tag, and summary fixtures; Riffado has audio,
transcription, recording-route, and regression tests; Bumble has GATT, device,
link, transport, and profile tests; GoReplay has capture/replay tests.

There are no project-owned tests, packet captures, golden BLE frames, emulator
tests, audio evaluation tests, or CI gates yet.

## 12. CRITICAL DISCOVERIES

1. The supplied material has no ready-made Plaud emulator. The central deliverable
   remains to be built after protocol gaps are reduced.
2. The official SDK is not source-only: Android AAR and iOS framework binaries
   are present, while Swift interfaces and template source expose the usable
   public surface.
3. The earlier identification of `live-agent` as LiveKit Agents is wrong; its
   recorded upstream is the Xiaozhi server.
4. Plaud-specific BLE evidence is concentrated in the official SDK; cloud/audio
   repositories cannot substitute for it.
5. Android decompilation now supplies the complete handshake message chain,
  including chunk serializers, SSN response parsing, second handshake, and time
  synchronization. The strongest hardware-free first test can target this chain
  after the account/RSA inputs are made deterministic.

## 13. GAPS

The project lacks a protocol codec implementation, GATT profile implementation,
advertiser, state machine, transfer model, fault injector, SDK integration runner,
packet fixtures, local cloud mock, synthetic generator, metrics harness, and CI.
It lacks a confirmed runtime response sample and authentication trace; the
characteristic roles and CRC algorithm are now source-confirmed, but hardware
compatibility remains unvalidated.

## 14. PROPOSED FIRST MILESTONE

The smallest useful milestone is **R1a: a deterministic Bumble virtual-link test
that exposes the recovered service and characteristics, performs discovery and
subscription, sends the recovered iOS `getState` request `01 03 00`, and asserts
the opcode-3 response structure**. The response values should come from a
deterministic emulator state, while the fixture records that those values are
test-controlled rather than hardware-observed. Do not claim SDK compatibility
until the same exchange is driven by a real official SDK client path.

## 15. EXACT FILES TO MODIFY

No implementation files should be modified during this first pass. For the next
approved milestone, the narrow modification surface is:

- `docs/protocol.json`: retain the verified post-bind exchange metadata.
- `docs/protocol-spec.md`: retain the verified exchange and R1a reuse plan.
- `docs/fixtures/post-bind-get-state.json`: exact request and structural response
  fixture for the selected exchange.
- `emulator/plaudsim/`: add the Plaud-specific Bumble adapter only after the
  protocol fixture and characteristic direction are confirmed.
- `tests/`: add the single virtual-link discovery/command-response test and its
  fixture.
- `PROJECT.md`: update status and decision log after the milestone passes.

Do not modify `reference/` repositories, generated SDK binaries, or empty audio
pipeline directories for that milestone.

## 16. RISKS / UNCERTAINTIES

Facts are the checked-out artifacts, repository remotes/pins, public interfaces,
template source, extracted constants, and existing tests described above.
Assumptions are fixed characteristic masks, write mode, CRC coverage/serialization,
protobuf presence, RSA handshake details, and the exact file-transfer transport.
Platform risk is material: the official client SDK is iOS/Android
binary code, while CI-friendly Bumble is Python. A passing generic Bumble test
would validate only the emulator substrate until a real SDK/client exchange is
performed.

## 17. NEXT ACTION

Implement the single R1a Bumble virtual-link test from
`docs/fixtures/post-bind-get-state.json`, reusing Bumble's existing GATT server,
advertising, subscription, and `LocalLink` components. This report intentionally
stops before that implementation.