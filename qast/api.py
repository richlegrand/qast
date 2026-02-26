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
from .resolve.ytdlp import download_audio, resolve

log = get_logger("api")


@dataclass
class Status:
    state: str  # "playing" | "stopped" | "idle"
    now_playing: str | None
    duration: float | None
    queue: list[str]


def discover(timeout: int = 15, show_all: bool = False) -> list[Device]:
    """Discover all cast-capable devices on the network."""
    return discover_all(timeout=timeout, show_all=show_all)


def cast(
    source: str | list[str],
    *,
    device: Device,
    repeat: bool = False,
    shuffle: bool = False,
    no_placeholder: bool = False,
    cookies_from_browser: str | None = None,
) -> None:
    """One-shot cast. Blocks until playback finishes or KeyboardInterrupt.

    Args:
        source: URL string or list of URLs.
        device: Target device (from discover()).
        repeat: Loop queue when all items finish.
        shuffle: Randomize URL order.
        no_placeholder: Skip loading/up-next screens.
        cookies_from_browser: Browser to extract cookies from.
    """
    urls = [source] if isinstance(source, str) else list(source)

    if shuffle and len(urls) > 1:
        import random
        random.shuffle(urls)

    raw_ts = device.protocol in ("roku", "dlna")
    pipeline = Pipeline(raw_ts=raw_ts)
    show_placeholder = not no_placeholder

    try:
        if len(urls) == 1:
            resolved = resolve(urls[0], cookies_from_browser=cookies_from_browser)
            if resolved:
                if len(resolved.source_urls) > 1:
                    download_audio(resolved)
                pipeline.start_single(
                    resolved.source_urls,
                    is_live=resolved.is_live,
                    title=resolved.title,
                    duration=resolved.duration,
                    show_placeholder=show_placeholder,
                    loop=repeat,
                )
            else:
                pipeline.start_single(
                    [urls[0]],
                    show_placeholder=show_placeholder,
                    loop=repeat,
                )
        else:
            queue = PlayQueue(loop=repeat, cookies_from_browser=cookies_from_browser)
            for u in urls:
                queue.add(u)
            queue.close()
            pipeline.start_queue(queue, show_placeholder=show_placeholder)

        if not pipeline.wait_ready():
            raise RuntimeError("Failed to buffer enough data")

        video_format = "ts" if raw_ts else "mp4"
        cast_media(device, pipeline.serve_url, video_format=video_format)
        pipeline.clear_disconnect()
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
        q.add_screen()
        q.play()
        q.skip()
        q.stop()
    """

    def __init__(
        self,
        device: Device,
        cookies_from_browser: str | None = None,
        save_stream: str | None = None,
    ) -> None:
        self._device = device
        self._cookies_from_browser = cookies_from_browser
        self._save_stream = save_stream
        self._raw_ts = device.protocol in ("roku", "dlna")

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
        url: str,
        duration: float | None = None,
        placeholder: bool = True,
    ) -> None:
        """Add a URL to the queue."""
        self._ensure_queue().add_item(QueueItem(
            url=url, duration=duration,
            show_placeholder=placeholder,
        ))

    def add_screen(
        self, duration: float | None = None, placeholder: bool = True,
    ) -> None:
        """Add screen capture to the queue."""
        self._ensure_queue().add_item(QueueItem(
            capture="screen", duration=duration,
            show_placeholder=placeholder, title="Screen Capture",
        ))

    def add_window(
        self, title: str, duration: float | None = None, placeholder: bool = True,
    ) -> None:
        """Add window capture by title to the queue."""
        self._ensure_queue().add_item(QueueItem(
            capture="window", window_title=title,
            duration=duration, show_placeholder=placeholder,
            title=f"Window: {title}",
        ))

    def add_webcam(
        self, duration: float | None = None, placeholder: bool = True,
    ) -> None:
        """Add webcam capture to the queue."""
        self._ensure_queue().add_item(QueueItem(
            capture="webcam", duration=duration,
            show_placeholder=placeholder, title="Webcam",
        ))

    def add_browser(
        self, url: str, duration: float | None = None, placeholder: bool = True,
    ) -> None:
        """Add browser capture (headless Chromium rendering a URL) to the queue."""
        self._ensure_queue().add_item(QueueItem(
            capture="browser", url=url, duration=duration,
            show_placeholder=placeholder, title=url,
        ))

    def play(self, repeat: bool = False, show_placeholder: bool = True) -> None:
        """Start playback. Non-blocking — returns immediately."""
        if self._playing:
            return

        if self._queue is None:
            raise RuntimeError("Nothing to play — call add() or add_screen() first")

        if repeat:
            self._queue.set_loop(repeat)
        self._queue.close()

        # Use larger buffer for capture-only queues
        has_capture = self._queue.has_capture_items
        buffer_max = config.CAPTURE_BUFFER_MAX if has_capture else None
        buffer_min = config.CAPTURE_BUFFER_MIN if has_capture else None

        self._pipeline = Pipeline(
            save_stream=self._save_stream, raw_ts=self._raw_ts,
            buffer_max=buffer_max, buffer_min=buffer_min,
        )
        self._pipeline.start_queue(self._queue, show_placeholder=show_placeholder)

        if not self._pipeline.wait_ready():
            self._pipeline.shutdown()
            raise RuntimeError("Failed to buffer enough data")

        video_format = "ts" if self._raw_ts else "mp4"
        cast_media(self._device, self._pipeline.serve_url, video_format=video_format)
        self._pipeline.clear_disconnect()
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
        if self._queue:
            queue_items = self._queue.pending_labels()

        if not self._playing or not self._pipeline:
            return Status(
                state="idle",
                now_playing=None,
                duration=None,
                queue=queue_items,
            )

        return Status(
            state="playing",
            now_playing=self._pipeline.now_playing,
            duration=self._pipeline.current_duration,
            queue=queue_items,
        )

    def wait(self, timeout: float | None = None) -> bool:
        """Block until playback finishes. Returns True if finished."""
        if self._pipeline:
            return self._pipeline.wait_done(timeout=timeout)
        return True
