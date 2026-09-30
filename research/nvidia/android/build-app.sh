#!/bin/bash
# Build the SpeechBench APK (docs/nvidia-speech.md): copies the android native layer
# from build-native.sh into the app's jniLibs (git-ignored) and runs Gradle offline
# with the same plugin versions as r7/android-app.
#   research/nvidia/android/build-app.sh   -> build/nvidia/speechbench/SpeechBench-debug.apk
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TC=${TOOLCHAIN:-$HOME/mivi-toolchain}
NATIVE=${SB_OUT:-$ROOT/build/nvidia/speechbench}/android
[[ -f "$NATIVE/lib/libspeechbench.so" ]] || { echo "run build-native.sh android first" >&2; exit 2; }
export JAVA_HOME=${JAVA_HOME:-$TC/jdk17/Contents/Home}
SDK=${ANDROID_SDK:-$TC/android-sdk}
APP="$HERE/app"
JNI="$APP/app/src/main/jniLibs/arm64-v8a"
rm -rf "$JNI" && mkdir -p "$JNI"
for f in "$NATIVE"/lib/*.so; do cp -L "$f" "$JNI/"; done
echo "sdk.dir=$SDK" > "$APP/local.properties"
( cd "$APP" && ./gradlew :app:assembleDebug --offline -q )
mkdir -p "$(dirname "$NATIVE")"
cp "$APP/app/build/outputs/apk/debug/app-debug.apk" "$(dirname "$NATIVE")/SpeechBench-debug.apk"
ls -la "$(dirname "$NATIVE")/SpeechBench-debug.apk"
