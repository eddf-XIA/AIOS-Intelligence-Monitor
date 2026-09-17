#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIOS Intelligence Monitor launcher.

    python app.py

Starts the local web UI on http://127.0.0.1:8765 and opens a browser. No
Node.js, no Docker, no external database - everything lives under ``data/``.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aios import __app_name__, __version__  # noqa: E402
from aios.config import DEFAULT_HOST, DEFAULT_PORT, get_paths  # noqa: E402


def port_is_free(host: str, port: int) -> bool:
    """True when nothing is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def print_banner(host: str, port: int, key_configured: str, fresh: bool) -> None:
    url = f"http://{host}:{port}"
    print()
    print(f"  {__app_name__} v{__version__}")
    print("  Starting server...")
    print()
    paths = get_paths()
    print(f"  Database:   {paths.db_path}")
    print(f"  Reports:    {paths.reports_dir}")
    print(f"  Logs:       {paths.logs_dir}")
    print(f"  AI model:   {key_configured}")
    if fresh:
        print("  Seed:       8 default monitor modules created")
    print()
    print(f"  Web UI:     {url}")
    print()
    print("  Press Ctrl+C to stop.")
    print(flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__app_name__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser; also exits quietly if AIOS is already running "
             "(this is how the Windows logon task starts the app)",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload (development)")
    args = parser.parse_args()

    if not port_is_free(args.host, args.port):
        # A Windows logon task can fire while the user already has AIOS open.
        # Exit cleanly instead of stacking a second server on the same data.
        if args.no_browser:
            print(
                f"  AIOS is already running on http://{args.host}:{args.port} - "
                "nothing to do."
            )
            return 0
        print()
        print(f"  ERROR: port {args.port} on {args.host} is already in use.")
        print()
        print("  Another copy of AIOS Intelligence Monitor may already be running -")
        print(f"  try opening http://{args.host}:{args.port} first.")
        print(f"  Otherwise start on a different port:  python app.py --port {args.port + 1}")
        print()
        return 1

    # Initialise before uvicorn so the banner can report real state.
    from aios.database import init_db, session_scope
    from aios.logging_setup import configure_logging
    from aios.repositories import providers as providers_repo

    configure_logging()
    fresh = init_db(get_paths())

    with session_scope() as session:
        default = providers_repo.default_provider(session)
        usable = providers_repo.configured_providers(session)
        if usable and default is not None:
            llm_status = f"{default.display_name} / {default.default_model}"
            if len(usable) > 1:
                llm_status += f"  (+{len(usable) - 1} more configured)"
        else:
            llm_status = "Not configured - open Settings > AI 模型"

    print_banner(args.host, args.port, llm_status, fresh)

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(
        "aios.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
