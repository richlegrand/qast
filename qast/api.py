"""Public Python API for qast.

Usage:
    from qast import discover, cast, Qast

    # One-shot
    devices = discover()
    cast("https://youtube.com/watch?v=...", device=devices[0])

    # Programmatic queue
    q = Qast(device=devices[0])
    q.add("https://youtube.com/watch?v=...")
    q.add("https://youtube.com/watch?v=...")
    q.play()
    q.stop()
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import config
from .cast.dispatch import cast_media, stop_device
from .discovery.scan import discover_all
from .discovery.types import Device
from .log import get_logger
from .pipeline.pipeline import Pipeline
from .queue import PlayQueue, QueueItem
from .source import parse_source, has_capture_source

log = get_logger("api")

_cached_devices: list[Device] | None = None


def _create_pipeline(
    device: Device,
    has_capture: bool,
    *,
    save_stream: str | None = None,
    verbose: bool = False,
    preroll: float = 0,
    placeholder_time: float = 0,
) -> Pipeline:
    """Build a Pipeline with the right buffer sizes for *device*."""
    raw_ts = device.protocol in ("roku", "dlna")
    kw: dict = dict(
        save_stream=save_stream, raw_ts=raw_ts,
        verbose=verbose, preroll=preroll,
        placeholder_time=placeholder_time,
    )
    if has_capture:
        kw["buffer_max"] = config.CAPTURE_BUFFER_MAX
        kw["buffer_min"] = config.CAPTURE_BUFFER_MIN
    return Pipeline(**kw)


def _wait_and_cast(pipeline: Pipeline, device: Device, has_capture: bool) -> None:
    """Wait for the pipeline buffer, then cast to *device*.

    Raises RuntimeError if the buffer never fills.
    """
    min_frames = int(config.VIDEO_GOP) + 1 if has_capture else 0
    if not pipeline.wait_ready(min_frames=min_frames):
        raise RuntimeError("Failed to buffer enough data")

    raw_ts = device.protocol in ("roku", "dlna")
    video_format = "ts" if raw_ts else "mp4"
    if device.protocol == "dlna":
        pipeline.gate_serving()
    try:
        cast_media(device, pipeline.serve_url, video_format=video_format)
    finally:
        if device.protocol == "dlna":
            pipeline.ungate_serving()
    pipeline.clear_disconnect()


@dataclass
class Status:
    state: str  # "playing" | "idle"
    now_playing: str | None
    duration: float | None
    position: float | None  # elapsed content time in seconds
    queue: list[str]


def discover(timeout: int = 15, show_all: bool = False) -> list[Device]:
    """Discover all cast-capable devices on the network."""
    global _cached_devices
    _cached_devices = discover_all(timeout=timeout, show_all=show_all)
    return _cached_devices


def _resolve_device(device: Device | str | int) -> Device:
    """Resolve a device selector to a Device object.

    Accepts a Device, an int index, or a string (name substring or protocol).
    Uses cached results from discover(), or discovers automatically if needed.
    """
    if isinstance(device, Device):
        return device

    devices = _cached_devices if _cached_devices is not None else discover()

    if isinstance(device, int):
        if 0 <= device < len(devices):
            return devices[device]
        raise ValueError(f"Device index {device} out of range (0-{len(devices) - 1})")

    if isinstance(device, str):
        lower = device.lower()
        # Try name substring match
        for dev in devices:
            if lower in dev.name.lower():
                return dev
        # Try protocol match
        for dev in devices:
            if lower == dev.protocol:
                return dev
        raise ValueError(f"No device matching '{device}'")

    raise TypeError(f"Expected Device, str, or int, got {type(device).__name__}")


def cast(
    source: str | list[str],
    *,
    device: Device | str | int,
    duration: float | None = None,
    repeat: bool = False,
    shuffle: bool = False,
    no_placeholder: bool = False,
    cookies_from_browser: str | None = None,
    preroll: float = 0,
    placeholder_time: float = 0,
    youtube_default: bool = False,
) -> None:
    """One-shot cast. Blocks until playback finishes or KeyboardInterrupt.

    Args:
        source: URL string or list of URLs.
        device: Target Device, index (int), or name/protocol substring (str).
        duration: Per-item duration limit in seconds.
        repeat: Loop queue when all items finish.
        shuffle: Randomize URL order.
        no_placeholder: Skip loading/up-next screens.
        cookies_from_browser: Browser to extract cookies from.
        placeholder_time: Minimum placeholder duration between segments.
        youtube_default: Use YouTube's default muxed stream instead of DASH.
    """
    config.YOUTUBE_DEFAULT = youtube_default
    device = _resolve_device(device)
    urls = [source] if isinstance(source, str) else list(source)

    if shuffle and len(urls) > 1:
        import random
        random.shuffle(urls)

    show_placeholder = not no_placeholder

    items = [parse_source(u, global_duration=duration) for u in urls]
    any_capture = has_capture_source(items)

    pipeline = _create_pipeline(
        device, any_capture, preroll=preroll, placeholder_time=placeholder_time,
    )

    try:
        queue = PlayQueue(loop=repeat, cookies_from_browser=cookies_from_browser)
        for item in items:
            queue.add_item(item)
        queue.close()
        queue.resolve_next()
        pipeline.start_queue(queue, show_placeholder=show_placeholder)

        _wait_and_cast(pipeline, device, any_capture)
        pipeline.wait_done()

    except KeyboardInterrupt:
        stop_device(device)
    finally:
        pipeline.shutdown()


class Qast:
    """Programmatic queue-based casting interface.

    Usage:
        q = Qast(device=devices[0])
        q.add("https://...")
        q.add("screen")
        q.play()
        q.skip()
        q.stop()
    """

    def __init__(
        self,
        device: Device | str | int,
        cookies_from_browser: str | None = None,
        save_stream: str | None = None,
        preroll: float = 0,
        placeholder_time: float = 0,
        youtube_default: bool = False,
    ) -> None:
        config.YOUTUBE_DEFAULT = youtube_default
        self._device = _resolve_device(device)
        self._cookies_from_browser = cookies_from_browser
        self._save_stream = save_stream
        self._preroll = preroll
        self._placeholder_time = placeholder_time

        self._pipeline: Pipeline | None = None
        self._queue: PlayQueue | None = None
        self._playing = False
        self._lock = threading.Lock()

    def _ensure_queue(self) -> PlayQueue:
        if self._queue is None:
            self._queue = PlayQueue(
                loop=False,
                cookies_from_browser=self._cookies_from_browser,
            )
        return self._queue

    def add(
        self,
        source: str,
        duration: float | None = None,
        placeholder: bool = True,
    ) -> None:
        """Add a source to the queue.

        Uses the same source syntax as the CLI: URLs, file paths,
        ``screen``, ``webcam``, ``window:Title``, ``browser:https://...``.
        Inline durations (``screen@30s``) are supported; the *duration*
        parameter acts as a fallback when no inline duration is present.
        """
        item = parse_source(source, global_duration=duration)
        item.show_placeholder = placeholder
        self._ensure_queue().add_item(item)

    def play(self, repeat: bool = False, show_placeholder: bool = True,
             verbose: bool = False) -> None:
        """Start playback.

        Returns after initial buffering and cast handoff are complete.
        """
        if self._playing:
            return

        if self._queue is None:
            raise RuntimeError("Nothing to play — call add() first")

        if repeat:
            self._queue.set_loop(repeat)
        self._queue.close()

        has_capture = self._queue.has_capture_items

        self._pipeline = _create_pipeline(
            self._device, has_capture,
            save_stream=self._save_stream, verbose=verbose,
            preroll=self._preroll, placeholder_time=self._placeholder_time,
        )
        self._pipeline.start_queue(self._queue, show_placeholder=show_placeholder)

        try:
            _wait_and_cast(self._pipeline, self._device, has_capture)
        except RuntimeError:
            self._pipeline.shutdown()
            raise
        self._playing = True

    def skip(self) -> None:
        """Skip the currently playing item."""
        if self._pipeline:
            self._pipeline.skip_current()

    def stop(self) -> None:
        """Stop playback and clean up."""
        if self._pipeline:
            self._pipeline.shutdown()
            self._pipeline = None
        stop_device(self._device)
        self._playing = False

    def remove(self, index: int) -> str | None:
        """Remove a pending item from the queue by index."""
        if self._queue:
            return self._queue.remove(index)
        return None

    def status(self) -> Status:
        """Get current playback status."""
        queue_items: list[str] = []
        now_playing = self._pipeline.now_playing if self._pipeline else None
        if self._queue:
            queue_items = self._queue.rotation_labels(current_title=now_playing)

        if not self._playing or not self._pipeline:
            return Status(
                state="idle",
                now_playing=None,
                duration=None,
                position=None,
                queue=queue_items,
            )

        return Status(
            state="playing",
            now_playing=self._pipeline.now_playing,
            duration=self._pipeline.current_duration,
            position=self._pipeline.elapsed,
            queue=queue_items,
        )

    def wait(self, timeout: float | None = None) -> bool:
        """Block until playback finishes. Returns True if finished."""
        if self._pipeline:
            return self._pipeline.wait_done(timeout=timeout)
        return True
