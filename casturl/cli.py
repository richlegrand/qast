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
    cookies_from_browser: str | None
    screen: bool
    window: bool
    no_cursor: bool


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
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Browser to extract cookies from (e.g. chrome, firefox, brave)",
    )

    group = parser.add_argument_group("screen capture")
    group.add_argument(
        "--screen",
        action="store_true",
        default=False,
        help="Capture screen and cast to device",
    )
    group.add_argument(
        "--window",
        action="store_true",
        default=False,
        help="Capture a specific window (click to select)",
    )
    group.add_argument(
        "--no-cursor",
        action="store_true",
        default=False,
        help="Hide mouse cursor in screen capture",
    )

    ns = parser.parse_args(argv)
    return Args(
        urls=ns.urls,
        device=ns.device,
        verbose=ns.verbose,
        debug=ns.debug,
        cookies_from_browser=ns.cookies_from_browser,
        screen=ns.screen,
        window=ns.window,
        no_cursor=ns.no_cursor,
    )
