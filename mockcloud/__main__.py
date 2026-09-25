"""python -m mockcloud --port 8787 [--persist] [...]

Binds to 127.0.0.1 by default. The process performs no outbound network
traffic of any kind; see docs/mockcloud.md for how to point the template app
at it (customDomain / server-domain knobs).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app, default_persist_path
from .settings import MockSettings
from .state import MockState


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mockcloud", description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--persist", nargs="?", const=str(default_persist_path()), default=None,
                   metavar="PATH", help="persist state as JSON (default build/mockcloud/state.json)")
    p.add_argument("--auto-register-sn", action="store_true",
                   help="HARNESS_POLICY: register unknown SNs on first bind instead of 404")
    p.add_argument("--task-step-seconds", type=float, default=1.0,
                   help="seconds per Celery state hop in the transcription worker")
    p.add_argument("--local-file-root", action="append", default=[], metavar="DIR",
                   help="directory a file:// file_url may point into (repeatable)")
    p.add_argument("--ota-image", default=None, metavar="PATH",
                   help="serve this file as the latest firmware from version/latest")
    p.add_argument("--ota-version-code", type=int, default=0)
    p.add_argument("--public-url", default=None,
                   help="absolute base for issued URLs (default: the requesting URL's base)")
    p.add_argument("--chunk-size", type=int, default=None, help="override the documented 5 MiB ChunkSize")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    kwargs = dict(
        auto_register_unknown_sn=args.auto_register_sn,
        task_step_s=args.task_step_seconds,
        local_file_roots=tuple(Path(d) for d in args.local_file_root),
        ota_image_path=Path(args.ota_image) if args.ota_image else None,
        ota_version_code=args.ota_version_code,
        public_base_url=args.public_url,
    )
    if args.chunk_size:
        kwargs["chunk_size"] = args.chunk_size
    settings = MockSettings(**kwargs)
    state = MockState(persist_path=Path(args.persist) if args.persist else None)
    app = create_app(settings, state)
    print(f"plaud-harness mockcloud (SYNTHETIC values only) on http://{args.host}:{args.port} "
          f"persist={args.persist or 'off'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
