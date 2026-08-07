"""CLI entry point.

Binds the listening socket ourselves so the chosen port is known *before*
uvicorn starts. That lets `--port 0` work: we print a one-line JSON handshake on
stdout that the Electron shell reads to learn the port and auth token, which
avoids the race you'd get from pre-picking a "free" port and hoping it stays free.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
import webbrowser

from .config import Settings
from .engine import Engine
from .gitutil import GitError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeviz",
        description="Live structure & flow visualizer for AI-agent code changes.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="repository or directory to open (omit to start on the Open dialog)",
    )
    parser.add_argument("--port", type=int, default=8770, help="0 picks a free port")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback only)")
    parser.add_argument("--scope", help="limit analysis to this subdirectory")
    parser.add_argument(
        "--mode",
        default="live",
        choices=["live", "commit", "branch", "range"],
        help="what to diff against",
    )
    parser.add_argument("--ref", help="commit sha, branch name, or 'a..b' for --mode")
    parser.add_argument("--model", help="Anthropic model for optional AI insights")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = Settings()
    if args.model:
        settings.model = args.model

    engine = Engine()
    if args.path:
        try:
            engine.open(args.path, args.scope)
            if args.mode != "live":
                engine.set_mode(args.mode, args.ref)
        except (GitError, OSError) as exc:
            print(f"codeviz: {exc}", file=sys.stderr)
            return 2

    token = secrets.token_urlsafe(24)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print(f"codeviz: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    port = sock.getsockname()[1]
    url = f"http://{args.host}:{port}/?token={token}"

    # The Electron shell parses exactly this line from stdout.
    print(
        json.dumps({"event": "ready", "port": port, "token": token, "url": url}),
        flush=True,
    )

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import uvicorn  # imported late so --help stays fast

    from .server import create_app

    app = create_app(engine, settings, token)
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
