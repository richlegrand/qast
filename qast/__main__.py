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
from .resolve.ytdlp import resolve_source
from .source import merge_duration_args, parse_duration, parse_source, has_capture_source

log = get_logger("main")


def _stdin_peer_name() -> str:
    """Try to find the process piping into our stdin (Linux only)."""
    try:
        link = os.readlink("/proc/self/fd/0")
        if not link.startswith("pipe:"):
            return os.path.basename(link)
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or entry == str(os.getpid()):
                continue
            try:
                if os.readlink(f"/proc/{entry}/fd/1") == link:
                    with open(f"/proc/{entry}/cmdline") as f:
                        return f.read().replace("\0", " ").strip()
            except OSError:
                continue
    except OSError:
        pass
    return "Stdin"


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
    config.ASPECT = args.aspect

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

        duration = parse_duration(args.duration) if args.duration else None
        preroll = parse_duration(args.preroll) if args.preroll else 0
        placeholder_time = parse_duration(args.placeholder_time) if args.placeholder_time else 0

        if args.urls == ["-"]:
            # Stdin pipe mode — read MPEG-TS from stdin
            pipeline = Pipeline(
                save_stream=args.save_stream, raw_ts=raw_ts,
                buffer_max=config.CAPTURE_BUFFER_MAX,
                buffer_min=config.CAPTURE_BUFFER_MIN,
                verbose=verbose, preroll=preroll,
                placeholder_time=placeholder_time,
            )
            segment = SegmentFFmpeg(["pipe:0"], aspect=config.ASPECT)
            if verbose:
                print("Reading from stdin...")
            pipeline.start_capture(segment, title=_stdin_peer_name())

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
                pipeline = Pipeline(save_stream=args.save_stream, raw_ts=raw_ts, verbose=verbose,
                                    preroll=preroll, placeholder_time=placeholder_time)

                print("Resolving...")
                resolved = resolve_source(
                    item.url,
                    duration=item.duration,
                    cookies_from_browser=args.cookies_from_browser,
                )
                resolved_obj = resolved

                if verbose:
                    print(f"  Title: {resolved.title}")
                    if resolved.duration:
                        mins, secs = divmod(int(resolved.duration), 60)
                        print(f"  Duration: {mins}m{secs:02d}s")
                if resolved.is_live:
                    print("  Live stream detected.")

                if verbose:
                    print("Starting pipeline...")
                pipeline.start_single(resolved.source_urls, is_live=resolved.is_live,
                                      title=resolved.title, duration=resolved.duration,
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
                        verbose=verbose, preroll=preroll,
                        placeholder_time=placeholder_time,
                    )
                else:
                    pipeline = Pipeline(save_stream=args.save_stream, raw_ts=raw_ts, verbose=verbose,
                                        preroll=preroll, placeholder_time=placeholder_time)

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
            if device.protocol == "dlna":
                pipeline.gate_serving()
            try:
                cast_media(device, pipeline.serve_url, video_format=video_format)
            finally:
                if device.protocol == "dlna":
                    pipeline.ungate_serving()
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
