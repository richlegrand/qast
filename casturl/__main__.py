"""Entry point: python -m casturl"""

from __future__ import annotations

import sys

from .cast.dispatch import cast_media, stop_device
from .cli import parse_args
from .controller import Controller
from .discovery.cast import stop_discovery
from .discovery.scan import discover_all, select_device
from .discovery.types import Device
from .log import setup_logging, get_logger
from .pipeline.pipeline import Pipeline
from .queue import PlayQueue
from .resolve.ytdlp import download_audio, resolve

log = get_logger("main")


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
    devices = discover_all()

    pipeline: Pipeline | None = None
    device: Device | None = None
    controller: Controller | None = None
    resolved_obj: object | None = None
    try:
        device = _select_device_auto(devices, args.device)
        print(f"\nSelected: {device.name} [{device.protocol.upper()}]\n")

        # Collect URLs
        urls = list(args.urls)
        if not urls:
            url = input("Paste URL to cast: ").strip()
            if not url:
                print("No URL provided.")
                sys.exit(1)
            urls = [url]

        raw_ts = device.protocol == "roku"
        pipeline = Pipeline(debug=args.debug, raw_ts=raw_ts)

        if len(urls) == 1:
            # Single URL mode — resolve, show placeholder, play
            print("Resolving URL...")
            resolved = resolve(urls[0], cookies_from_browser=args.cookies_from_browser)

            if resolved:
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

            else:
                print("  yt-dlp failed, passing URL directly to ffmpeg.")
                title = None
                source_urls = [urls[0]]
                is_live = False

            print("Starting pipeline...")
            pipeline.start_single(source_urls, is_live=is_live, title=title)

        else:
            # Queue mode — multiple URLs
            queue = PlayQueue(loop=True, cookies_from_browser=args.cookies_from_browser)
            for u in urls:
                queue.add(u)
            queue.close()

            print(f"Starting pipeline with {len(urls)} items...")
            pipeline.start_queue(queue)

            # Start interactive controller for queue management
            controller = Controller(pipeline, queue)
            controller.start()

        if not pipeline.wait_ready():
            print("Failed to buffer enough data. Exiting.")
            sys.exit(1)

        print(f"  Streaming at: {pipeline.serve_url}")
        try:
            video_format = "ts" if raw_ts else "mp4"
            cast_media(device, pipeline.serve_url, video_format=video_format)
        except Exception as e:
            print(f"  Cast failed: {e}")
            sys.exit(1)

        if controller:
            print("Playing. Commands: <URL> add | s=skip | q=quit | ?=status\n")
        else:
            print("Playing. Press Ctrl+C to stop.\n")

        pipeline.wait_done()
        print("\nPlayback finished.")

    except KeyboardInterrupt:
        print("\nStopping...")
        if device:
            stop_device(device)
    finally:
        if controller:
            controller.stop()
        if pipeline:
            pipeline.shutdown()
        if resolved_obj and hasattr(resolved_obj, 'cleanup'):
            resolved_obj.cleanup()
        stop_discovery()


if __name__ == "__main__":
    main()
