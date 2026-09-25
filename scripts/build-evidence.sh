#!/usr/bin/env bash
# Rebuild the derived evidence tree from reference/** WITHOUT touching it.
#
# reference/** is immutable evidence. Everything this script writes lands in
# build/evidence/ (gitignored). Inputs:
#   reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar
#   reference/plaud-org/plaud-sdk-public/sdk/ios/*.framework
#
# Outputs (build/evidence/):
#   aar/        unpacked AAR (classes.jar, jni/*.so, proguard.txt, manifest)
#   classes/    unpacked classes.jar (.class files)
#   jadx-out/   jadx decompilation  -- a DECOMPILER'S INTERPRETATION
#   javap/      per-class `javap -p -c -constants` -- ACTUAL BYTECODE, ground truth
#
# When jadx and javap disagree, javap wins. Cite javap for any protocol claim.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AAR="$ROOT/reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar"
OUT="$ROOT/build/evidence"
TOOLS="${PLAUD_TOOLS:-/tmp/plaud-tools}"
JADX_VERSION="1.5.1"
JDK_URL="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.13%2B11/OpenJDK17U-jdk_x64_mac_hotspot_17.0.13_11.tar.gz"

# Pinned input: any change here invalidates every protocol claim in docs/.
EXPECTED_AAR_SHA=041a6f8814d350dbc8bcd4a515487136529bb8bb4368fc88c9b36c9a386aedce
EXPECTED_JAR_SHA=27795d1fcb1165cae2cfd93773949b79d67713e85216670e7d5ad9866f933560

[ -f "$AAR" ] || { echo "missing $AAR -- run ./scripts/fetch-references.sh" >&2; exit 1; }
actual=$(shasum -a 256 "$AAR" | cut -d' ' -f1)
[ "$actual" = "$EXPECTED_AAR_SHA" ] || {
  echo "AAR sha256 mismatch!" >&2
  echo "  expected $EXPECTED_AAR_SHA" >&2
  echo "  actual   $actual" >&2
  echo "The SDK was republished. Protocol claims must be re-derived." >&2
  exit 1
}

mkdir -p "$TOOLS"

# --- java ---------------------------------------------------------------
if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/javap" ]; then
  :
elif [ -d /tmp/plaud-analysis/jdk-17.0.20.1+1/Contents/Home ]; then
  export JAVA_HOME=/tmp/plaud-analysis/jdk-17.0.20.1+1/Contents/Home
elif [ -d "$TOOLS/jdk" ]; then
  export JAVA_HOME="$(find "$TOOLS/jdk" -maxdepth 3 -name Home -type d | head -1)"
else
  echo "==> fetching JDK 17"
  mkdir -p "$TOOLS/jdk"
  curl -sL "$JDK_URL" | tar xz -C "$TOOLS/jdk"
  export JAVA_HOME="$(find "$TOOLS/jdk" -maxdepth 3 -name Home -type d | head -1)"
fi
"$JAVA_HOME/bin/java" -version 2>&1 | head -1

# --- jadx ---------------------------------------------------------------
JADX="$TOOLS/jadx/bin/jadx"
[ -x "$JADX" ] || [ -x /tmp/plaud-analysis/jadx/bin/jadx ] || {
  echo "==> fetching jadx $JADX_VERSION"
  curl -sL -o "$TOOLS/jadx.zip" \
    "https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip"
  mkdir -p "$TOOLS/jadx"; unzip -q -o "$TOOLS/jadx.zip" -d "$TOOLS/jadx"
  chmod +x "$JADX"; rm -f "$TOOLS/jadx.zip"
}
[ -x "$JADX" ] || JADX=/tmp/plaud-analysis/jadx/bin/jadx

# --- unpack -------------------------------------------------------------
rm -rf "$OUT"; mkdir -p "$OUT"
echo "==> unpacking AAR"
unzip -q -o "$AAR" -d "$OUT/aar"
actual=$(shasum -a 256 "$OUT/aar/classes.jar" | cut -d' ' -f1)
[ "$actual" = "$EXPECTED_JAR_SHA" ] || { echo "classes.jar sha mismatch: $actual" >&2; exit 1; }
mkdir -p "$OUT/classes"; (cd "$OUT/classes" && unzip -q -o ../aar/classes.jar)

# --- decompile ----------------------------------------------------------
echo "==> jadx"
"$JADX" -q -d "$OUT/jadx-out" --no-res --show-bad-code "$OUT/aar/classes.jar" || true

# --- bytecode (ground truth) -------------------------------------------
echo "==> javap"
mkdir -p "$OUT/javap"
(cd "$OUT/classes" && find . -name '*.class' | sed 's|^\./||; s|\.class$||' | sort) > "$OUT/classlist.txt"
(cd "$OUT/classes" && "$JAVA_HOME/bin/javap" -p -c -constants -cp . \
   $(tr '/' '.' < "$OUT/classlist.txt" | tr '\n' ' ')) > "$OUT/javap/ALL.txt"
while read -r c; do
  mkdir -p "$OUT/javap/$(dirname "$c")"
  (cd "$OUT/classes" && "$JAVA_HOME/bin/javap" -p -c -constants -cp . "$(echo "$c" | tr '/' '.')") \
    > "$OUT/javap/$c.txt" 2>/dev/null
done < "$OUT/classlist.txt"

echo
echo "ready:"
echo "  bytecode (ground truth)  $OUT/javap/           ($(find "$OUT/javap" -name '*.txt' | wc -l | tr -d ' ') classes)"
echo "  decompiled java          $OUT/jadx-out/sources ($(find "$OUT/jadx-out" -name '*.java' | wc -l | tr -d ' ') files)"
echo "  native libs              $OUT/aar/jni/"
echo "  swift interfaces         reference/plaud-org/plaud-sdk-public/sdk/ios/*.framework/Modules/*/*.swiftinterface"
