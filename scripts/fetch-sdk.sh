#!/usr/bin/env bash
# Fetch and decompile the Plaud SDK for analysis.
# The SDK binaries are proprietary under a licence separate from the repo's Apache 2.0,
# so they are never vendored here — this script pulls them on demand into reference/,
# which is gitignored.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$ROOT/reference"
JADX_VERSION="1.5.1"

mkdir -p "$REF/tools"

if [ ! -d "$REF/sdk" ]; then
  echo "==> cloning plaud-sdk-public"
  git clone --depth 1 https://github.com/Plaud-AI/plaud-sdk-public.git "$REF/sdk"
else
  echo "==> sdk already present"
fi

if [ ! -x "$REF/tools/jadx/bin/jadx" ]; then
  echo "==> installing jadx $JADX_VERSION"
  curl -sL -o "$REF/tools/jadx.zip" \
    "https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip"
  mkdir -p "$REF/tools/jadx"
  unzip -q -o "$REF/tools/jadx.zip" -d "$REF/tools/jadx"
  chmod +x "$REF/tools/jadx/bin/jadx"
  rm -f "$REF/tools/jadx.zip"
fi

echo "==> unpacking plaud-sdk.aar"
mkdir -p "$REF/aar"
unzip -q -o "$REF/sdk/sdk/android/plaud-sdk.aar" -d "$REF/aar"

echo "==> decompiling classes.jar"
"$REF/tools/jadx/bin/jadx" -q -d "$REF/jadx-out" --no-res "$REF/aar/classes.jar"

echo
echo "ready:"
echo "  decompiled java   $REF/jadx-out/sources"
echo "  swift interfaces  $REF/sdk/sdk/ios/*.framework/Modules/*.swiftmodule/*.swiftinterface"
echo "  native libs       $REF/aar/jni/"
