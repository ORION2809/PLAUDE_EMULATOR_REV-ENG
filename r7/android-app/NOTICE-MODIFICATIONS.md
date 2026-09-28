# Notice of modifications

`r7/android-app/` is derived from the Android template app (the `android/`
directory) of **Plaud-AI/plaud-sdk-public**
(<https://github.com/Plaud-AI/plaud-sdk-public>) at commit
`8d7541e503cb96043e8629624aa6cee0241daa03`, the pin recorded in
`docs/reference-pins.txt`. Only `android/` was copied; the upstream `ios/` and
`sdk/` directories were not.

The original work is Copyright 2026 Plaud Inc. and licensed under the Apache
License, Version 2.0. `LICENSE` in this directory is a byte-identical copy of
the upstream repository's licence file, including its closing note that the
Plaud SDK binaries are proprietary. The upstream repository has no `NOTICE`
file. This directory, with the changes listed below, stays under Apache-2.0
(repository `README.md`, "Licence"); the rest of this repository is licensed
separately.

This file is the statement of changes required by section 4(b) of the
licence.

## How the list was produced

Every file in the pinned commit's `android/` tree (148 files, read with
`git ls-tree` / `git show` from the reference checkout) was compared by
sha256 with the file at the same path here. Local build outputs and caches
(`.gradle/`, `app/build/`) were excluded. Result (re-checked 25 Sep 2026): 147 files are identical,
1 is modified, 3 source files were added, and 1 local file is not
distributed.

## Modified

* `app/src/main/AndroidManifest.xml`: three `<activity>` entries added,
  `.debug.K3CaptureActivity`, `.debug.PullCaptureActivity` and
  `.debug.WifiCaptureActivity`, all `android:exported="true"`, plus a
  `NEARBY_WIFI_DEVICES` permission declaration used only by the Wi-Fi driver.
  Each addition is preceded by a `DEBUG-ONLY` comment naming its R7 stage.
  Nothing else in the file changed.

## Added

* `app/src/main/java/com/plaud/template/debug/K3CaptureActivity.kt`: the
  R7-S12 driver. It connects through the public SDK entry point
  `recoveryConnectBleDevice` with a synthetic historical identifier, so the
  unmodified SDK writes its k3 handshake to the harness's Bumble emulator. It
  mirrors stage callbacks to logcat (`K3CAP` prefix). See
  `r7/r7-s12-k3-runtime-capture.md`.
* `app/src/main/java/com/plaud/template/debug/PullCaptureActivity.kt`: the
  R7-S13 driver. It makes the same connection, calls `getFileList()`, then
  pulls the first listed file through `syncFile` (raw collector) and/or
  `exportAudio(..., OPUS, ...)`. It hashes what the SDK delivers and logs
  with the `PULLCAP` prefix. See `r7/r7-s13-recording-pull.md`.

* `app/src/main/java/com/plaud/template/debug/WifiCaptureActivity.kt`: the
  R7-S14 driver. It makes the same Bluetooth connection, then drives the SDK's
  Wi-Fi fast-transfer path (`startWifiTransfer`, `setDeviceWiFi`) and logs
  with the `WIFICAP` prefix. See `r7/r7-s14-wifi-real-sdk.md`.

All three drivers use only public SDK API and are not wired into the
template's UI. Since 25 September 2026 all three pass a blank token to
`initSDK`: with a non-blank token the SDK automatically requests an RSA key
pair from Plaud's partner server, which these drivers must not do (R7-S14,
finding D7).

## Local only, not distributed

* `local.properties`: the upstream README ("Configure Credentials") tells
  users to create `android/local.properties` with `sdk.dir`,
  `PLAUD_USER_ACCESS_TOKEN`, `PLAUD_CLIENT_ID` and `PLAUD_API_KEY`. The
  unchanged `app/build.gradle` reads them into `BuildConfig`. The copy here
  has the same four keys. Its token values are synthetic placeholders, not
  Plaud credentials (`r7/README.md`), and `sdk.dir` is machine-local. The
  file is git-ignored (`**/local.properties`), so it is not part of what
  this repository distributes.

## Unchanged, under a separate licence

* `app/libs/plaud-sdk.aar` is Plaud's proprietary SDK binary and is **not**
  covered by Apache-2.0 (see the note at the end of `LICENSE`). The local copy
  is unmodified (sha256 `041a6f8814d350dbc8bcd4a515487136529bb8bb4368fc88c9b36c9a386aedce`,
  identical to `android/app/libs/plaud-sdk.aar` at the pinned commit). It is
  not committed: `*.aar` is git-ignored. To build, copy it from the pinned
  reference checkout (`reference/plaud-org/plaud-sdk-public`, fetched by
  `scripts/fetch-references.sh`) to `app/libs/plaud-sdk.aar`.

## Trademarks

The template's code and resources are reproduced as upstream ships them.
Examples include `app_name` "Plaud Template" and the NotePin text in
`app/src/main/res/values/strings.xml`, the NotePin illustration
`app/src/main/res/drawable/icon_notepin.xml`, and the other drawables and
launcher icons. Any Plaud names, product names and logos among them remain
the trademarks of Plaud. Apache-2.0 section 6 grants no trademark rights.
This harness is not affiliated with Plaud, and no endorsement by Plaud is
implied. None of the drawables were removed or altered.
