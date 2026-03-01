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
    no_cursor: bool
    show_all: bool
    repeat: bool
    shuffle: bool
    no_placeholder: bool
    placeholder_time: str | None
    preroll: str
    duration: str | None
    playlist: str | None
    aspect: float


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="qast",
        description="Cast URLs to Chromecast devices via an always-transcode pipeline.",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        metavar="SOURCE",
        help="Sources to cast: URLs, files, screen[@duration], webcam[@duration], "
             "browser:<url>[@duration], window[:<title>][@duration] (if none given, prompts interactively)",
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
        help="Browser to extract cookies from (e.g. BROWSER=chrome, firefox, brave, edge, or safari)",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        default=False,
        help="Hide mouse cursor in screen/window capture",
    )
    parser.add_argument(
        "--duration",
        metavar="TIME",
        help="Default duration for sources without @duration (e.g. 30s, 5m, 1h, 5m30s)",
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
        "--placeholder-time",
        default=None,
        metavar="TIME",
        help="Minimum placeholder duration between segments, e.g. 5s, 1m (default: 2s)",
    )
    parser.add_argument(
        "--preroll",
        default="0",
        metavar="TIME",
        help="Preroll duration before the video, e.g. 30s, 1m, 1m15s (helps DLNA TVs that skip the start)",
    )
    parser.add_argument(
        "--playlist",
        metavar="FILE",
        help="Load sources from a playlist file (one per line, '-' for stdin)",
    )
    parser.add_argument(
        "--aspect",
        type=float,
        default=1.0,
        metavar="FACTOR",
        help="Aspect ratio correction: 1.0=no change, >1.0=wider, <1.0=narrower",
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
        no_cursor=ns.no_cursor,
        show_all=ns.show_all,
        repeat=ns.repeat,
        shuffle=ns.shuffle,
        no_placeholder=ns.no_placeholder,
        placeholder_time=ns.placeholder_time,
        preroll=ns.preroll,
        duration=ns.duration,
        playlist=ns.playlist,
        aspect=ns.aspect,
    )
