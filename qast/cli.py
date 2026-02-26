"""Command-line argument parsing."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class Args:
    urls: list[str]
    device: str | None
    verbose: bool
    save_stream: str | None
    cookies_from_browser: str | None
    screen: bool
    window: bool
    no_cursor: bool
    show_all: bool
    repeat: bool
    shuffle: bool
    no_placeholder: bool
    window_title: str | None
    duration: str | None
    webcam: bool
    browser: bool
    playlist: str | None


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="qast",
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
        "--save-stream",
        metavar="FILE",
        help="Save the served stream (fMP4 or TS) to a file",
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

    group.add_argument(
        "--window-title",
        metavar="TITLE",
        help="Capture a window matching TITLE (via xdotool search)",
    )
    group.add_argument(
        "--webcam",
        action="store_true",
        default=False,
        help="Capture webcam and cast to device",
    )
    group.add_argument(
        "--browser",
        action="store_true",
        default=False,
        help="Render a URL in headless Chromium and cast the result",
    )
    group.add_argument(
        "--duration",
        metavar="TIME",
        help="Limit capture duration (e.g. 30s, 5m, 1h)",
    )

    parser.add_argument(
        "--repeat",
        action="store_true",
        default=False,
        help="Loop the queue when all items finish",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        default=False,
        help="Shuffle URL order before playing",
    )
    parser.add_argument(
        "--no-placeholder",
        action="store_true",
        default=False,
        help="Skip loading/up-next placeholder screens",
    )
    parser.add_argument(
        "--playlist",
        metavar="FILE",
        help="Load URLs from a playlist file (one per line, '-' for stdin)",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        default=False,
        help="Show all protocols per device (e.g. both Cast and DLNA)",
    )

    ns = parser.parse_args(argv)
    return Args(
        urls=ns.urls,
        device=ns.device,
        verbose=ns.verbose,
        save_stream=ns.save_stream,
        cookies_from_browser=ns.cookies_from_browser,
        screen=ns.screen,
        window=ns.window,
        no_cursor=ns.no_cursor,
        show_all=ns.show_all,
        repeat=ns.repeat,
        shuffle=ns.shuffle,
        no_placeholder=ns.no_placeholder,
        window_title=ns.window_title,
        duration=ns.duration,
        webcam=ns.webcam,
        browser=ns.browser,
        playlist=ns.playlist,
    )
