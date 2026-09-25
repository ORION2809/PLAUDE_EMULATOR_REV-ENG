#!/usr/bin/env bash
# Dataset manifest for the synthetic-meeting generator (Layer 2).
# Full corpora are tens of GB: this script NEVER bulk-downloads by default.
#   ./scripts/fetch-datasets.sh --list    # show URLs, licences, sizes
#   ./scripts/fetch-datasets.sh --sample  # tiny smoke-test slice (~50 MB)
#   ./scripts/fetch-datasets.sh --all     # everything (~50 GB free needed)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data/corpora"
mkdir -p "$DATA"

list() {
cat <<'EOF'
AMI Meeting Corpus   | CC BY 4.0        | ~100h | https://groups.inf.ed.ac.uk/ami/corpus/  (+ mirrors, HuggingFace: edinburghcoglearn/ami)
LibriCSS             | research-only    | ~10h  | https://github.com/chenzhehuai/libricss (+ OpenSLR SLR104 for LibriSpeech source)
ICSI Meeting Corpus  | LDC licence      | ~72h  | https://groups.inf.ed.ac.uk/ami/icsi/ (LDC2004S02)
VoxConverse          | CC BY 4.0        | ~50h  | https://github.com/joonson/voxconverse (+ HuggingFace mirrors)
MUSAN (SLR17)        | permissive-research | ~109h | https://www.openslr.org/17/
RIRS_NOISES (SLR28)  | CC BY 4.0        | ~2GB  | https://www.openslr.org/28/
EOF
}

case "${1:---list}" in
  --list) list;;
  --sample)
    echo "sampling MUSAN + RIRS headers only (no audio) ..."
    mkdir -p "$DATA/samples"
    curl -sL -o "$DATA/samples/SLR17-README" "https://www.openslr.org/17/" &
    curl -sL -o "$DATA/samples/SLR28-README" "https://www.openslr.org/28/" &
    wait
    echo "wrote $DATA/samples/ (verify URLs, then extend for --all)";;
  --all)
    echo "ERROR: refusing to bulk-download without a checksum manifest." >&2
    echo "Add verified per-file URLs + sha256 to this script first (see docs/SOURCES.md §5)." >&2
    exit 1;;
  *) echo "usage: $0 [--list|--sample|--all]" >&2; exit 2;;
esac
