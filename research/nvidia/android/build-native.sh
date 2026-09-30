#!/bin/bash
# Build the speechbench native layer (docs/nvidia-speech.md) for android (arm64-v8a,
# CPU) or macos (arm64, Metal), against prebuilt NeMo-Speech.cpp, whisper.cpp and
# sherpa-onnx, and gather everything a device needs into one directory:
#
#   research/nvidia/android/build-native.sh android|macos
#     -> $SB_OUT/<target>/  (default build/nvidia/speechbench/<target>, git-ignored)
#
# Inputs (see docs/nvidia-speech.md "Reproduce" for how each was built; defaults are
# the toolchain directory of this machine):
#   TOOLCHAIN     root holding the builds (default ~/mivi-toolchain)
#   NSC_SRC       NeMo-Speech.cpp checkout at 0f706e4 (android: the clean-ggml one)
#   NSC_LIBDIR    its build's bin/ (libnemo_speech_asr_c + deps)
#   WCPP_SRC      whisper.cpp v1.9.4 checkout (a static build is made here)
#   SHERPA_DIR    sherpa-onnx v1.13.8 android archive, unpacked (jniLibs/, include/)
#   ANDROID_NDK   NDK 26.1 (android only)
set -euo pipefail
TARGET=${1:?usage: build-native.sh android|macos}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TC=${TOOLCHAIN:-$HOME/mivi-toolchain}
export PATH="$TC/buildtools-venv/bin:$PATH"   # cmake 3.31 + ninja
WCPP_SRC=${WCPP_SRC:-$TC/whisper.cpp}
SHERPA_DIR=${SHERPA_DIR:-$TC/deps/sherpa-onnx-android}
OUT=${SB_OUT:-$ROOT/build/nvidia/speechbench}/$TARGET
JOBS=${JOBS:-4}
mkdir -p "$OUT"

case "$TARGET" in
  android)
    NSC_SRC=${NSC_SRC:-$TC/NeMo-Speech.cpp-android}
    NSC_LIBDIR=${NSC_LIBDIR:-$NSC_SRC/build/android-arm64/bin}
    NDK=${ANDROID_NDK:-$TC/android-sdk/ndk/26.1.10909125}
    XC=(-DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28)
    GGML=(-DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16 -DGGML_OPENMP=OFF)
    SHERPA=(-DSB_WITH_SHERPA=ON -DSB_SHERPA_INCLUDE="$SHERPA_DIR/include" -DSB_SHERPA_LIBDIR="$SHERPA_DIR/jniLibs/arm64-v8a")
    LIBEXT=so ;;
  macos)
    NSC_SRC=${NSC_SRC:-$TC/NeMo-Speech.cpp}
    NSC_LIBDIR=${NSC_LIBDIR:-$NSC_SRC/build/metal-asr/bin}
    XC=()
    GGML=()
    SHERPA=(-DSB_WITH_SHERPA=OFF)   # the Mac runs sherpa-onnx through the harness's Python package
    LIBEXT=dylib ;;
  *) echo "unknown target $TARGET" >&2; exit 2 ;;
esac

# 1. whisper.cpp, static (its ggml stays inside libsb_whisper, hidden)
WB="$WCPP_SRC/build/$TARGET-static"
cmake -S "$WCPP_SRC" -B "$WB" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_SDL2=OFF \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON ${XC[@]+"${XC[@]}"} ${GGML[@]+"${GGML[@]}"} > "$OUT/whisper-configure.log"
cmake --build "$WB" -j "$JOBS" --target whisper > "$OUT/whisper-build.log"

# 2. speechbench
SB="$OUT/cmake"
cmake -S "$HERE/native" -B "$SB" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSB_NSC_ROOT="$NSC_SRC" -DSB_NSC_LIBDIR="$NSC_LIBDIR" -DSB_WCPP_ROOT="$WCPP_SRC" -DSB_WCPP_BUILD="$WB" \
  ${SHERPA[@]+"${SHERPA[@]}"} ${XC[@]+"${XC[@]}"} > "$OUT/speechbench-configure.log"
cmake --build "$SB" -j "$JOBS" > "$OUT/speechbench-build.log"

# 3. gather the runtime: our libs + NVIDIA's shared libs (+ sherpa-onnx's on android)
LIB="$OUT/lib"
rm -rf "$LIB" && mkdir -p "$LIB"
cp "$SB"/speechbench "$OUT/"
cp "$SB"/libsb_*."$LIBEXT" "$LIB/"
[[ -f "$SB/libspeechbench.$LIBEXT" ]] && cp "$SB/libspeechbench.$LIBEXT" "$LIB/"
for f in "$NSC_LIBDIR"/libnemo_speech_asr*."$LIBEXT"* "$NSC_LIBDIR"/libggml*."$LIBEXT"*; do cp -P "$f" "$LIB/"; done
[[ -x "$NSC_LIBDIR/nemo-speech" ]] && cp "$NSC_LIBDIR/nemo-speech" "$OUT/"
if [[ "$TARGET" == android ]]; then
  cp "$SHERPA_DIR"/jniLibs/arm64-v8a/lib{sherpa-onnx-c-api,onnxruntime}.so "$LIB/"
  WCLI="$WCPP_SRC/build/android-arm64/bin"   # the shared whisper-cli build, with its own lib dir
  if [[ -x "$WCLI/whisper-cli" ]]; then mkdir -p "$OUT/whisper-cli" && cp "$WCLI"/whisper-cli "$WCLI"/*.so "$OUT/whisper-cli/"; fi
fi
( cd "$OUT" && find . -type f ! -name '*.log' ! -path './cmake/*' -print0 | xargs -0 shasum -a 256 | sort -k2 > SHA256SUMS )
echo "[build-native] $TARGET -> $OUT"
ls -la "$OUT" "$LIB" | awk 'NF>=9 {print $5, $NF}'
