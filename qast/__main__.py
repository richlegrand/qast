"""Entry point: python -m qast"""

from __future__ import annotations

import os
import sys
import termios
import time

from . import config
from .capture import ScreenSegment, WebcamSegment, _select_window, _find_window_by_title
from .pipeline.browser import BrowserSegment
from .cast.dispatch import cast_media, stop_device
from .cli import parse_args
from .tty import tty_input
from .controller import Controller
from .discovery.cast import stop_discovery
from .discovery.scan import discover_all, select_device
from .discovery.types import Device
from .log import setup_logging, get_logger
from .pipeline.pipeline import Pipeline
from .pipeline.segment import SegmentFFmpeg
from .progress import ProgressBar
from .queue import PlayQueue
from .resolve.ytdlp import download_audio, probe_duration, resolve

log = get_logger("main")


def _parse_duration(s: str) -> float:
    """Parse '30s', '5m', '1h', or bare seconds."""
    s = s.strip().lower()
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    return float(s)


def _select_device_auto(devices: list[Device], selector: str | None) -> Device:
    """Auto-select a device by name/index, or fall back to interactive menu."""
    if selector is not None and devices:
        # Try as index
        try:
            idx = int(selector)
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        # Try as name substring (case-insensitive)
        lower = selector.lower()
        for dev in devices:
            if lower in dev.name.lower():
                return dev

    return select_device(devices)


def main() -> None:
    args = parse_args()
    setup_logging(verbose=args.verbose)

    print("Scanning for devices...")
    devices = discover_all(show_all=args.show_all)

    # Save terminal state — ffmpeg subprocesses can corrupt it on kill
    try:
        _saved_termios = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError):
        _saved_termios = None

    pipeline: Pipeline | None = None
    device: Device | None = None
    controller: Controller | None = None
    resolved_obj: object | None = None
    progress: ProgressBar | None = None
    queue: PlayQueue | None = None
    verbose = args.verbose
    try:
        device = _select_device_auto(devices, args.device)
        print(f"\nSelected: {device.name}\n")

        raw_ts = device.protocol in ("roku", "dlna")

        duration = _parse_duration(args.duration) if args.duration else None

        if args.screen or args.window or args.window_title:
            # Screen/window capture mode — small buffer for low latency
            pipeline = Pipeline(
                save_stream=args.save_stream, raw_ts=raw_ts,
                buffer_max=config.CAPTURE_BUFFER_MAX,
                buffer_min=config.CAPTURE_BUFFER_MIN,
                verbose=verbose,
            )
            if args.window_title:
                window_id, win_w, win_h = _find_window_by_title(args.window_title)
                segment = ScreenSegment(
                    cursor=not args.no_cursor,
                    window_id=window_id,
                    window_size=(win_w, win_h),
                    duration=duration,
                )
            elif args.window:
                window_id, win_w, win_h = _select_window()
                segment = ScreenSegment(
                    cursor=not args.no_cursor,
                    window_id=window_id,
                    window_size=(win_w, win_h),
                    duration=duration,
                )
            else:
                segment = ScreenSegment(cursor=not args.no_cursor, duration=duration)
            if verbose:
                print("Starting screen capture...")
            pipeline.start_capture(segment, title="Screen Capture")

        elif args.webcam:
            # Webcam capture mode
            pipeline = Pipeline(
                save_stream=args.save_stream, raw_ts=raw_ts,
                buffer_max=config.CAPTURE_BUFFER_MAX,
                buffer_min=config.CAPTURE_BUFFER_MIN,
                verbose=verbose,
            )
            segment = WebcamSegment(duration=duration)
            if verbose:
                print("Starting webcam capture...")
            pipeline.start_capture(segment, title="Webcam")

        elif args.browser:
            # Browser capture mode — render URL in headless Chromium
            pipeline = Pipeline(
                save_stream=args.save_stream, raw_ts=raw_ts,
                buffer_max=config.CAPTURE_BUFFER_MAX,
                buffer_min=config.CAPTURE_BUFFER_MIN,
                verbose=verbose,
            )
            if not args.urls:
                url = tty_input("Paste URL to render: ").strip()
                if not url:
                    print("No URL provided.")
                    sys.exit(1)
                args.urls = [url]
            if len(args.urls) != 1:
                print("--browser requires exactly one URL.")
                sys.exit(1)
            segment = BrowserSegment(args.urls[0], duration=duration)
            print(f"Rendering {args.urls[0]}")
            pipeline.start_capture(segment, title=args.urls[0])

        elif args.urls == ["-"]:
            # Stdin pipe mode — read MPEG-TS from stdin
            pipeline = Pipeline(
                save_stream=args.save_stream, raw_ts=raw_ts,
                buffer_max=config.CAPTURE_BUFFER_MAX,
                buffer_min=config.CAPTURE_BUFFER_MIN,
                verbose=verbose,
            )
            segment = SegmentFFmpeg(["pipe:0"])
            if verbose:
                print("Reading from stdin...")
            pipeline.start_capture(segment, title="Stdin")

        else:
            # URL mode
            pipeline = Pipeline(save_stream=args.save_stream, raw_ts=raw_ts, verbose=verbose)

            # Load playlist file if specified
            urls = list(args.urls)
            if args.playlist:
                if args.playlist == "-":
                    lines = sys.stdin.read().splitlines()
                else:
                    with open(args.playlist) as f:
                        lines = f.readlines()
                playlist_urls = [
                    l.strip() for l in lines
                    if l.strip() and not l.strip().startswith("#")
                ]
                urls = playlist_urls + urls

            if not urls:
                url = tty_input("Paste URL to cast: ").strip()
                if not url:
                    print("No URL provided.")
                    sys.exit(1)
                urls = [url]

            if len(urls) == 1:
                # Single URL mode — resolve, show placeholder, play
                print("Resolving...")
                resolved = resolve(urls[0], cookies_from_browser=args.cookies_from_browser)

                media_duration: float | None = None
                if resolved:
                    if verbose:
                        print(f"  Title: {resolved.title}")
                        if resolved.duration:
                            mins, secs = divmod(int(resolved.duration), 60)
                            print(f"  Duration: {mins}m{secs:02d}s")
                    if resolved.is_live:
                        print("  Live stream detected.")
                    elif len(resolved.source_urls) > 1:
                        download_audio(resolved)
                    resolved_obj = resolved
                    title = resolved.title
                    source_urls = resolved.source_urls
                    is_live = resolved.is_live
                    media_duration = resolved.duration

                else:
                    if "youtube.com" in urls[0] or "youtu.be" in urls[0]:
                        print("  yt-dlp failed to extract video. YouTube may be blocking requests.")
                        print("  Try: qast --cookies-from-browser chrome <url>")
                    elif verbose:
                        print("  yt-dlp failed, passing URL directly to ffmpeg.")
                    source_urls = [urls[0]]
                    is_live = False
                    if os.path.isfile(urls[0]):
                        title = os.path.basename(urls[0])
                        media_duration = probe_duration(urls[0])
                        if verbose and media_duration:
                            mins, secs = divmod(int(media_duration), 60)
                            print(f"  Duration: {mins}m{secs:02d}s (via ffprobe)")
                    else:
                        title = None

                if verbose:
                    print("Starting pipeline...")
                pipeline.start_single(source_urls, is_live=is_live, title=title,
                                      duration=media_duration,
                                      show_placeholder=not args.no_placeholder,
                                      loop=args.repeat)

            else:
                # Queue mode — multiple URLs
                if args.shuffle:
                    import random
                    random.shuffle(urls)
                queue = PlayQueue(loop=args.repeat, cookies_from_browser=args.cookies_from_browser)
                for u in urls:
                    queue.add(u)
                queue.close()

                # Eagerly resolve the first item so the user sees progress
                print("Resolving...")
                queue.resolve_next()

                if verbose:
                    print(f"Starting pipeline with {len(urls)} items...")
                pipeline.start_queue(queue, show_placeholder=not args.no_placeholder)

                # Start interactive controller for queue management
                controller = Controller(pipeline, queue)
                controller.start()

        if not pipeline.wait_ready():
            print("Failed to buffer enough data. Exiting.")
            sys.exit(1)

        if verbose:
            print(f"  Streaming at: {pipeline.serve_url}")
        try:
            video_format = "ts" if raw_ts else "mp4"
            cast_media(device, pipeline.serve_url, video_format=video_format)
        except Exception as e:
            print(f"  Cast failed: {e}")
            sys.exit(1)

        # Clear any disconnect events from the TV probing the stream
        # during SetAVTransportURI — these are not real disconnects.
        pipeline.clear_disconnect()

        print(f"Streaming to {device.name}")
        if controller:
            print("Commands: <URL> add | s=skip | q=quit | ?=status\n")

        # Start progress bar (non-verbose mode only)
        if not verbose:
            progress = ProgressBar(pipeline, queue)
            progress.start()

        if device.protocol == "dlna":
            # DLNA renderers manage their own playback state — re-casting
            # is harmful because normal DLNA probe connections trigger
            # false disconnect events, creating a Stop/Play loop.
            pipeline.wait_done()
        else:
            while True:
                if pipeline.wait_done(timeout=5):
                    break
                if pipeline.client_disconnected:
                    pipeline.clear_disconnect()
                    log.info("Device disconnected, re-casting...")
                    time.sleep(3)
                    try:
                        cast_media(device, pipeline.serve_url, video_format=video_format)
                        pipeline.clear_disconnect()
                    except Exception as e:
                        log.error("Re-cast failed: %s", e)
                        break

        print("\nPlayback finished.")

    except KeyboardInterrupt:
        print("\nStopping...")
        if device:
            stop_device(device)
    finally:
        if progress:
            progress.stop()
        if controller:
            controller.stop()
        if pipeline:
            pipeline.shutdown()
        if resolved_obj and hasattr(resolved_obj, 'cleanup'):
            resolved_obj.cleanup()
        stop_discovery()
        # Restore terminal state — ffmpeg can leave echo disabled
        if _saved_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _saved_termios)
            except (termios.error, OSError):
                pass


if __name__ == "__main__":
    main()
