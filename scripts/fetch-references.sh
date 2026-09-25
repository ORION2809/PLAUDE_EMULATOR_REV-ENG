#!/usr/bin/env bash
# Fetch every open-source repo named in dump.md / PROJECT.md into reference/
# for local inspection. Idempotent: skips repos already present.
#
# Layout:
#   reference/plaud-org/<repo>     -- all 16 Plaud-AI public repos (shallow)
#   reference/third-party/<repo>   -- Plaud ecosystem: riffado, plaud-api, ...
#   reference/upstream/<repo>      -- upstream originals for fork diffing (shallow)
#
# Everything under reference/ is gitignored (see .gitignore) so nothing
# fetched here is ever committed. AGPL repos (riffado) are safe to *read*
# here; the AGPL only matters if we vendor their code into our own source.
# Re-run any time: ./scripts/fetch-references.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORG="$ROOT/reference/plaud-org"
TP="$ROOT/reference/third-party"
UP="$ROOT/reference/upstream"
mkdir -p "$ORG" "$TP" "$UP"

clone_shallow() { # <dest-dir> <url>
  local dest="$1" url="$2"
  if [ -d "$dest/.git" ]; then
    echo "  skip  $(basename "$dest") (present)"
  else
    echo "  clone $url -> $dest"
    git clone --depth 1 "$url" "$dest" 2>&1 | tail -2 || echo "  WARN: failed $url"
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
du -sh "$ORG"/* 2>/dev/null | sort -k2
du -sh "$TP"/* 2>/dev/null | sort -k2
du -sh "$UP"/* 2>/dev/null | sort -k2
echo
du -sh "$ROOT/reference" 2>/dev/null
echo "done. See docs/SOURCES.md for what each repo is for."
