#!/usr/bin/env bash
# DEPRECATED. Use ./scripts/fetch-references.sh then ./scripts/build-evidence.sh.
#
# This script used to clone the SDK into reference/sdk and decompile into
# reference/jadx-out. That was wrong in two ways:
#   1. reference/** is immutable evidence. Nothing may write into it.
#   2. fetch-references.sh already clones plaud-sdk-public, so reference/sdk
#      was a second, unpinned copy that could silently diverge.
#
# build-evidence.sh reads the AAR from the pinned clone, verifies its sha256,
# and writes every derived artifact to build/ instead.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "fetch-sdk.sh is deprecated." >&2
echo >&2
echo "  ./scripts/fetch-references.sh   # clones plaud-sdk-public into reference/ (pinned)" >&2
echo "  ./scripts/build-evidence.sh     # decompiles it into build/evidence/" >&2
echo >&2
exec "$ROOT/scripts/build-evidence.sh" "$@"
