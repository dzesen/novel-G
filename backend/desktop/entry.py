"""Frozen executable entry point for the local desktop service owner."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import threading


def serve(port: int) -> None:
    import uvicorn
    from main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))

    def watch_parent() -> None:
        # EOF includes a native-window crash. Uvicorn drains requests before lifespan teardown.
        sys.stdin.readline()
        server.should_exit = True

    threading.Thread(target=watch_parent, daemon=True).start()
    server.run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("supervise", "serve"))
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--lang", choices=("zh", "en"), default="zh")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if not args.data_dir.is_absolute():
        parser.error("Data directory must be absolute")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["NOVEL_G_DATA_DIR"] = str(args.data_dir.resolve())
    os.environ["NOVEL_G_BACKEND_HOST"] = "127.0.0.1"
    os.chdir(args.data_dir)
    if args.mode == "serve":
        if args.port is None or not 1024 <= args.port <= 65535:
            parser.error("Invalid backend port")
        serve(args.port)
    else:
        if args.runtime_root is None:
            parser.error("Runtime root is required")
        from backend.desktop.supervisor import Supervisor
        asyncio.run(Supervisor(args.runtime_root.resolve(), args.data_dir.resolve(), args.lang).run())


if __name__ == "__main__":
    main()
