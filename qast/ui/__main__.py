"""Run the local UI server."""

from __future__ import annotations

import argparse

from .server import run_ui_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="qast-ui",
        description="Serve local site, docs, and admin UI entrypoints.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    args = parser.parse_args()
    run_ui_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
