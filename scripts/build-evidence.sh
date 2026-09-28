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
# Temurin JDK 17.0.13+11, fetched only when no JDK is found (see "java" below).
# The build is per platform: the macOS x64 one this script used to fetch
# everywhere does not run on Linux, and on Apple silicon only under Rosetta.
JDK_URL_BASE="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.13%2B11"

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
# javap is the ground truth, so a JDK is required (a JRE has no javap). Order:
# $JAVA_HOME; the original analysis machine's JDK; a JDK this script fetched
# into $TOOLS/jdk earlier; else download the Temurin build for this platform
# (macOS or Linux, x64 or arm64) -- anything else is a clear error, never a
# download of a JDK that cannot run here.
jdk_home_under() { # <dir> -> a JAVA_HOME below it with bin/javap (macOS Contents/Home or Linux layout), or nothing
  local javap
  javap="$(find "$1" -maxdepth 5 -path '*/bin/javap' -type f 2>/dev/null | head -1 || true)"
  if [ -n "$javap" ]; then dirname "$(dirname "$javap")"; fi
}
jdk_asset() { # "<uname -s>/<uname -m>" -> the Temurin asset infix, or nothing
  case "$1" in
    Darwin/x86_64)              echo x64_mac ;;
    Darwin/arm64)               echo aarch64_mac ;;
    Linux/x86_64)               echo x64_linux ;;
    Linux/aarch64|Linux/arm64)  echo aarch64_linux ;;
  esac
}
PLATFORM="$(uname -s)/$(uname -m)"
if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/javap" ]; then
  :
elif [ -d /tmp/plaud-analysis/jdk-17.0.20.1+1/Contents/Home ]; then
  export JAVA_HOME=/tmp/plaud-analysis/jdk-17.0.20.1+1/Contents/Home
elif [ -n "$(jdk_home_under "$TOOLS/jdk")" ]; then
  export JAVA_HOME="$(jdk_home_under "$TOOLS/jdk")"
else
  asset="$(jdk_asset "$PLATFORM")"
  if [ -z "$asset" ]; then
    echo "ERROR: no JDK found, and there is no JDK 17 download for this platform ($PLATFORM);" >&2
    echo "  the script fetches Temurin builds for macOS and Linux on x64/arm64 only." >&2
    echo "  Install a JDK 17 (javap is required; a JRE is not enough) and re-run with JAVA_HOME set." >&2
    exit 1
  fi
  echo "==> fetching JDK 17 ($asset) into $TOOLS/jdk"
  mkdir -p "$TOOLS/jdk"
  jdk_url="$JDK_URL_BASE/OpenJDK17U-jdk_${asset}_hotspot_17.0.13_11.tar.gz"
  curl -fsSL "$jdk_url" | tar xz -C "$TOOLS/jdk" || {
    echo "ERROR: could not download/unpack $jdk_url; install a JDK 17 and set JAVA_HOME instead." >&2
    exit 1
  }
  export JAVA_HOME="$(jdk_home_under "$TOOLS/jdk")"
fi
if [ -z "${JAVA_HOME:-}" ] || [ ! -x "$JAVA_HOME/bin/javap" ] || ! "$JAVA_HOME/bin/java" -version >/dev/null 2>&1; then
  echo "ERROR: no usable JDK on $PLATFORM: JAVA_HOME=${JAVA_HOME:-<unset>} has no working bin/java + bin/javap." >&2
  echo "  Set JAVA_HOME to a JDK 17 for this platform (e.g. \$(/usr/libexec/java_home -v 17) on macOS," >&2
  echo "  /usr/lib/jvm/java-17-openjdk-amd64 on Debian/Ubuntu), or remove $TOOLS/jdk to re-download." >&2
  exit 1
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
