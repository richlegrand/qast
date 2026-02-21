"""Command-line argument parsing."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class Args:
    urls: list[str]
    device: str | None
    verbose: bool
    debug: bool


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="casturl",
        description="Cast URLs to Chromecast devices via an always-transcode pipeline.",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URLs to cast (if none given, prompts interactively)",
    )
    parser.add_argument(
        "-d", "--device",
        help="Device name or index to auto-select",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save pipeline fMP4 output to /tmp/casturl_debug.mp4",
    )

    ns = parser.parse_args(argv)
    return Args(urls=ns.urls, device=ns.device, verbose=ns.verbose, debug=ns.debug)
