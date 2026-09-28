#!/usr/bin/env bash
# Fetch every open-source repo named in dump.md / PROJECT.md into reference/
# for local inspection, each at the commit pinned for it in
# docs/reference-pins.txt (looked up by URL). Idempotent: skips repos already
# present (it never rewrites an existing tree -- reference/ is immutable
# evidence), but warns when a present tree is not at its pin.
#
# Layout:
#   reference/plaud-org/<repo>     -- all 16 Plaud-AI public repos (shallow, pinned)
#   reference/third-party/<repo>   -- Plaud ecosystem: riffado, plaud-api, ...
#   reference/upstream/<repo>      -- upstream originals for fork diffing (shallow, pinned)
#
# Each repo is fetched as `git fetch --depth 1 origin <pinned sha>` and checked
# out detached at that sha, never at the remote's moving HEAD (the Docker images
# COPY reference/upstream/bumble, and tests/test_compose_topology.py asserts it
# is at the pin). A repo with no pin, or whose pin cannot be fetched, is an
# error: its half-made directory is removed and the script exits non-zero after
# trying the rest.
#
# Everything under reference/ is gitignored (see .gitignore) so nothing
# fetched here is ever committed. AGPL repos (riffado) are safe to *read*
# here; the AGPL only matters if we vendor their code into our own source.
# Re-run any time: ./scripts/fetch-references.sh
# Overrides (for testing into a scratch location; defaults shown):
#   REFERENCE_DIR=<repo>/reference   REFERENCE_PINS=<repo>/docs/reference-pins.txt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="${REFERENCE_DIR:-$ROOT/reference}"
PINS="${REFERENCE_PINS:-$ROOT/docs/reference-pins.txt}"
ORG="$REF/plaud-org"
TP="$REF/third-party"
UP="$REF/upstream"
[ -f "$PINS" ] || { echo "ERROR: pin file not found: $PINS" >&2; exit 1; }
mkdir -p "$ORG" "$TP" "$UP"

FAILED=""   # newline-separated "<url>: <reason>" (a plain string: bash 3.2 + set -u)
DRIFTED=""  # present trees that are not at their pin

pin_for() { # <url> -> the 40-hex sha pinned for it, or nothing
  # (length() rather than a {40} interval: mawk, Debian/Ubuntu's default awk, lacks intervals)
  awk -v url="$1" 'length($1) == 40 && $1 ~ /^[0-9a-f]+$/ && $2 == url { print $1; exit }' "$PINS"
}

clone_shallow() { # <dest-dir> <url>
  local dest="$1" url="$2" pin head
  pin="$(pin_for "$url")"
  if [ -z "$pin" ]; then
    echo "  ERROR: no pin for $url in $PINS" >&2
    FAILED="$FAILED$url: no pin in $PINS"$'\n'
    return 0
  fi
  if [ -d "$dest/.git" ]; then
    head="$(git -C "$dest" rev-parse HEAD 2>/dev/null || true)"
    if [ "$head" = "$pin" ]; then
      echo "  skip  $(basename "$dest") (present, at pin ${pin:0:12})"
    else
      echo "  WARN: $(basename "$dest") is present at ${head:-<no HEAD>}, pin is $pin;" \
           "left untouched -- move it aside and re-run to fetch the pin" >&2
      DRIFTED="$DRIFTED$dest"$'\n'
    fi
    return 0
  fi
  if [ -e "$dest" ]; then
    echo "  ERROR: $dest exists but is not a git checkout; not touching it" >&2
    FAILED="$FAILED$url: $dest exists without .git"$'\n'
    return 0
  fi
  echo "  fetch $url @ $pin -> $dest"
  if git init -q "$dest" \
     && git -C "$dest" remote add origin "$url" \
     && git -C "$dest" fetch -q --depth 1 origin "$pin" \
     && git -C "$dest" -c advice.detachedHead=false checkout -q --detach "$pin" \
     && [ "$(git -C "$dest" rev-parse HEAD)" = "$pin" ]; then
    :
  else
    echo "  ERROR: could not fetch $url at pinned commit $pin" >&2
    rm -rf "$dest"  # made by this call; a re-run must not mistake it for a present repo
    FAILED="$FAILED$url: pinned commit $pin could not be fetched"$'\n'
  fi
}

echo "==> Plaud-AI org (16 public repos, shallow)"
clone_shallow "$ORG/plaud-sdk-public"        "https://github.com/Plaud-AI/plaud-sdk-public.git"
clone_shallow "$ORG/client-sdk-esp32"        "https://github.com/Plaud-AI/client-sdk-esp32.git"
clone_shallow "$ORG/xiaozhi-esp32"           "https://github.com/Plaud-AI/xiaozhi-esp32.git"
clone_shallow "$ORG/live-agent"              "https://github.com/Plaud-AI/live-agent.git"
clone_shallow "$ORG/live-agent-memory"       "https://github.com/Plaud-AI/live-agent-memory.git"
clone_shallow "$ORG/plaud-memU-server"       "https://github.com/Plaud-AI/plaud-memU-server.git"
clone_shallow "$ORG/plaud-memU-ui"           "https://github.com/Plaud-AI/plaud-memU-ui.git"
clone_shallow "$ORG/langfuse"                "https://github.com/Plaud-AI/langfuse.git"
clone_shallow "$ORG/plaud-opik"              "https://github.com/Plaud-AI/plaud-opik.git"
clone_shallow "$ORG/goreplay"                "https://github.com/Plaud-AI/goreplay.git"
clone_shallow "$ORG/plaud-embedded-skills"   "https://github.com/Plaud-AI/plaud-embedded-skills.git"
clone_shallow "$ORG/embedded-capacitor"      "https://github.com/Plaud-AI/embedded-capacitor.git"
clone_shallow "$ORG/embedded-react-native"   "https://github.com/Plaud-AI/embedded-react-native.git"
clone_shallow "$ORG/embedded-flutter"        "https://github.com/Plaud-AI/embedded-flutter.git"
clone_shallow "$ORG/vite-react-template"     "https://github.com/Plaud-AI/vite-react-template.git"
clone_shallow "$ORG/yt-DeepResearch-Backend" "https://github.com/Plaud-AI/yt-DeepResearch-Backend.git"

echo "==> third-party Plaud ecosystem"
clone_shallow "$TP/riffado"          "https://github.com/riffado/riffado.git"
clone_shallow "$TP/plaud-api"        "https://github.com/arbuzmell/plaud-api.git"
clone_shallow "$TP/python-plaud-ai"  "https://github.com/DmytroLitvinov/python-plaud-ai.git"
clone_shallow "$TP/applaud-rsteckler" "https://github.com/rsteckler/applaud.git"
clone_shallow "$TP/plaud-toolkit"    "https://github.com/sergivalverde/plaud-toolkit.git"
clone_shallow "$TP/applaud-landoncrabtree" "https://github.com/landoncrabtree/applaud.git"

echo "==> upstream originals (for fork diffing)"
clone_shallow "$UP/livekit-client-sdk-esp32" "https://github.com/livekit/client-sdk-esp32.git"
clone_shallow "$UP/xiaozhi-esp32"            "https://github.com/78/xiaozhi-esp32.git"
clone_shallow "$UP/xiaozhi-esp32-server"     "https://github.com/xinnan-tech/xiaozhi-esp32-server.git"
clone_shallow "$UP/livekit-agents"           "https://github.com/livekit/agents.git"
clone_shallow "$UP/mem0"                     "https://github.com/mem0ai/mem0.git"
clone_shallow "$UP/opik"                     "https://github.com/comet-ml/opik.git"
clone_shallow "$UP/goreplay"                 "https://github.com/probelabs/goreplay.git"
clone_shallow "$UP/bumble"                   "https://github.com/google/bumble.git"
clone_shallow "$UP/pyroomacoustics"          "https://github.com/LCAV/pyroomacoustics.git"
clone_shallow "$UP/kokoro"                   "https://github.com/hexgrad/kokoro.git"
clone_shallow "$UP/piper"                    "https://github.com/OHF-Voice/piper1-gpl.git"
# NOTE: langfuse/langfuse, Jununn/memU-* are intentionally NOT cloned here:
#  - langfuse upstream is ~1GB+; diff against the Plaud fork on demand with:
#      git ls-remote https://github.com/langfuse/langfuse.git HEAD
#  - Jununn/memU-server|ui are small; add them if the memU migration becomes load-bearing.

echo
echo "==> inventory"
du -sh "$ORG"/* 2>/dev/null | sort -k2 || true
du -sh "$TP"/* 2>/dev/null | sort -k2 || true
du -sh "$UP"/* 2>/dev/null | sort -k2 || true
echo
du -sh "$REF" 2>/dev/null
if [ -n "$DRIFTED" ]; then
  echo "WARN: present trees not at their pin (left untouched):" >&2
  printf '%s' "$DRIFTED" | sed 's/^/  /' >&2
fi
if [ -n "$FAILED" ]; then
  echo "FAILED: these repos could not be fetched at their pinned commit:" >&2
  printf '%s' "$FAILED" | sed 's/^/  /' >&2
  exit 1
fi
echo "done. See docs/SOURCES.md for what each repo is for."
