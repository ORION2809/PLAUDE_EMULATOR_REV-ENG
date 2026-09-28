#!/usr/bin/env bash
# Replay the mock cloud's GoReplay export with GoReplay itself (review finding MC-3).
#
#   ./scripts/crosscheck-goreplay.sh [MEETING_DIR]
#
# 1. fetches Go (pinned version and sha256, macOS/Linux, arm64/amd64) into data/tools/go;
# 2. builds `gor` from reference/upstream/goreplay at its pinned commit, from a `git archive`
#    copy in a temp dir (nothing is written under reference/);
# 3. starts the mock cloud, drives it with docker/cloud_roundtrip.py (MEETING_DIR, default:
#    a fresh generator smoke meeting), exports GET /_mock/log?format=gor;
# 4. replays the file with `gor --input-file … --output-http` into scripts/tools/http_recorder.py;
# 5. compares, as multisets, every logged (method, target, body) with what arrived.
# Exit 0 when they are identical.  Needs a C toolchain for cgo (Xcode CLT / gcc).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
GO_VERSION=go1.27.1
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) GO_OS=darwin-arm64; GO_SHA=ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12 ;;
  *) GO_OS=""; GO_SHA="" ;;
esac
WORK="$(mktemp -d)"
trap 'kill ${MP:-} ${RP:-} 2>/dev/null || true; rm -rf "$WORK"' EXIT
GO="$ROOT/data/tools/go/bin/go"
if [[ ! -x "$GO" ]]; then
  [[ -n "$GO_OS" ]] || { echo "no pinned Go archive for $(uname -s)-$(uname -m); put a Go toolchain at data/tools/go" >&2; exit 3; }
  mkdir -p "$ROOT/data/tools"
  curl -fsSL -o "$WORK/go.tgz" "https://go.dev/dl/$GO_VERSION.$GO_OS.tar.gz"
  [[ "$(shasum -a 256 "$WORK/go.tgz" | cut -d' ' -f1)" == "$GO_SHA" ]] || { echo "Go archive sha256 mismatch" >&2; exit 3; }
  tar xzf "$WORK/go.tgz" -C "$ROOT/data/tools"
fi
GOR="$ROOT/data/tools/gor-$(git -C "$ROOT/reference/upstream/goreplay" rev-parse --short=12 HEAD)"
if [[ ! -x "$GOR" ]]; then
  mkdir -p "$WORK/src"
  git -C "$ROOT/reference/upstream/goreplay" archive HEAD | tar x -C "$WORK/src"
  (cd "$WORK/src" && GOPATH="$ROOT/data/tools/gopath" GOCACHE="$ROOT/data/tools/gocache" GOFLAGS=-mod=mod GOTOOLCHAIN=local \
     CGO_ENABLED=1 "$GO" build -o "$GOR" ./cmd/gor)
fi
MEETING="${1:-}"
if [[ -z "$MEETING" ]]; then
  MEETING="$("$PY" -c 'import sys; from pathlib import Path; from generator.testing import fixture_meeting; d, m = fixture_meeting(Path(sys.argv[1]), preset="smoke", seed=3); print(d)' "$WORK/meet")"
fi
free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
MPORT=$(free_port); RPORT=$(free_port)
"$PY" -m mockcloud --host 127.0.0.1 --port "$MPORT" --chunk-size 20000 --task-step-seconds 0.05 \
  --local-file-root "$(dirname "$MEETING")" >"$WORK/mock.log" 2>&1 & MP=$!
"$PY" "$ROOT/scripts/tools/http_recorder.py" "$RPORT" "$WORK/replayed.jsonl" & RP=$!
for i in $(seq 1 50); do curl -sf "http://127.0.0.1:$MPORT/" >/dev/null && break; sleep 0.2; done
"$PY" "$ROOT/docker/cloud_roundtrip.py" --url "http://127.0.0.1:$MPORT" --meeting-dir "$MEETING" --out "$WORK/hyp" | grep CLOUD_OK
curl -sf "http://127.0.0.1:$MPORT/_mock/log?format=gor" >"$WORK/export.gor"
curl -sf "http://127.0.0.1:$MPORT/_mock/log" >"$WORK/log.json"
(cd "$WORK" && "$GOR" --input-file "export.gor|10000%" --output-http "http://127.0.0.1:$RPORT" --exit-after 15s >"$WORK/gor.out" 2>&1) || true
"$PY" - "$WORK" "$GOR" <<'PYEOF'
import json, sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
work = Path(sys.argv[1])
log = json.loads((work / "log.json").read_text())["entries"]
rep = [json.loads(l) for l in (work / "replayed.jsonl").read_text().splitlines()] if (work / "replayed.jsonl").exists() else []
def target(e):
    q = e.get("query")
    if isinstance(q, dict): q = urlencode(q, doseq=True)
    return e["path"] + (f"?{q}" if q else "")
want = Counter((e["method"], target(e), "" if e.get("body_truncated") else (e.get("body") or "")) for e in log)
got = Counter((r["method"], r["target"], r["body"]) for r in rep)
ok = want == got and sum(want.values()) > 0
print(f"goreplay ({Path(sys.argv[2]).name}): logged {sum(want.values())}, replayed {sum(got.values())}, identical: {ok}")
sys.exit(0 if ok else 1)
PYEOF
