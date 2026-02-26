"""Entry point: python -m qast"""

from __future__ import annotations

import os
import sys
import termios
import time

from . import config
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
from .source import merge_duration_args, parse_source, has_capture_source

log = get_logger("main")


def _parse_duration(s: str) -> float:
    """Parse durations like '30s', '5m', '1h', '1h30m', '5m30s', or bare seconds."""
    import re

    s = s.strip().lower()
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and m.group(0):
        h = float(m.group(1) or 0)
        mins = float(m.group(2) or 0)
        secs = float(m.group(3) or 0)
        return h * 3600 + mins * 60 + secs
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

        if args.urls == ["-"]:
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
            # Source mode — URLs, files, captures, or any mix
            raw_sources = list(args.urls)
            if args.playlist:
                if args.playlist == "-":
                    lines = sys.stdin.read().splitlines()
                else:
                    with open(args.playlist) as f:
                        lines = f.readlines()
                playlist_sources = [
                    l.strip() for l in lines
                    if l.strip() and not l.strip().startswith("#")
                ]
                raw_sources = playlist_sources + raw_sources

            if not raw_sources:
                url = tty_input("Paste URL to cast: ").strip()
                if not url:
                    print("No URL provided.")
                    sys.exit(1)
                raw_sources = [url]

            # Merge standalone @duration args (e.g. ["url", "@5m"] → ["url@5m"])
            raw_sources = merge_duration_args(raw_sources)

            # Parse all sources
            items = [
                parse_source(s, global_duration=duration, cursor=not args.no_cursor)
                for s in raw_sources
            ]
            any_capture = has_capture_source(items)

            if len(items) == 1 and not items[0].capture:
                # Single URL mode — resolve, show placeholder, play
                item = items[0]
                effective_duration = item.duration

                pipeline = Pipeline(save_stream=args.save_stream, raw_ts=raw_ts, verbose=verbose)

                print("Resolving...")
                resolved = resolve(item.url, cookies_from_browser=args.cookies_from_browser)

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
                    if effective_duration is not None:
                        media_duration = effective_duration

                else:
                    if "youtube.com" in item.url or "youtu.be" in item.url:
                        print("  yt-dlp failed to extract video. YouTube may be blocking requests.")
                        print("  Try: qast --cookies-from-browser chrome <url>")
                    elif verbose:
                        print("  yt-dlp failed, passing URL directly to ffmpeg.")
                    source_urls = [item.url]
                    is_live = False
                    if os.path.isfile(item.url):
                        title = os.path.basename(item.url)
                        media_duration = probe_duration(item.url)
                        if effective_duration is not None:
                            media_duration = effective_duration
                        if verbose and media_duration:
                            mins, secs = divmod(int(media_duration), 60)
                            print(f"  Duration: {mins}m{secs:02d}s (via ffprobe)")
                    else:
                        title = None
                        media_duration = effective_duration

                if verbose:
                    print("Starting pipeline...")
                pipeline.start_single(source_urls, is_live=is_live, title=title,
                                      duration=media_duration,
                                      show_placeholder=not args.no_placeholder,
                                      loop=args.repeat)

            else:
                # Queue mode — multiple sources or capture sources
                if args.shuffle:
                    import random
                    random.shuffle(items)

                # Use capture buffer sizes when any item is a capture source
                if any_capture:
                    pipeline = Pipeline(
                        save_stream=args.save_stream, raw_ts=raw_ts,
                        buffer_max=config.CAPTURE_BUFFER_MAX,
                        buffer_min=config.CAPTURE_BUFFER_MIN,
                        verbose=verbose,
                    )
                else:
                    pipeline = Pipeline(save_stream=args.save_stream, raw_ts=raw_ts, verbose=verbose)

                queue = PlayQueue(loop=args.repeat, cookies_from_browser=args.cookies_from_browser)
                for item in items:
                    queue.add_item(item)
                queue.close()

                # Eagerly resolve the first item so the user sees progress
                print("Resolving...")
                queue.resolve_next()

                if verbose:
                    print(f"Starting pipeline with {len(items)} items...")
                pipeline.start_queue(queue, show_placeholder=not args.no_placeholder)

                # Start interactive controller for queue management
                controller = Controller(pipeline, queue)
                controller.start()

        is_capture = args.urls == ["-"]
        if queue:
            is_capture = is_capture or queue.has_capture_items
        min_frames = int(config.VIDEO_GOP) + 1 if is_capture else 0
        if not pipeline.wait_ready(min_frames=min_frames):
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
            print("Commands: <source[@duration]> add | s=skip | q=quit | ?=status\n")

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
