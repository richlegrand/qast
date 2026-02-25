"""Thread-safe play queue with prefetch."""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass

from .log import get_logger
from .resolve.ytdlp import ResolvedURL, download_audio, probe_duration, resolve

log = get_logger("queue")


@dataclass
class QueueItem:
    """A queued item — either a URL or a capture source."""
    url: str | None = None                      # URL to resolve via yt-dlp
    capture: str | None = None                   # "screen" | "window" | "webcam"
    window_title: str | None = None              # for capture="window"
    duration: float | None = None                # per-item duration limit
    show_placeholder: bool = True                # per-item placeholder toggle
    title: str | None = None                     # override title (for display)


class PlayQueue:
    """Thread-safe queue with background prefetch.

    Items are added as raw URL strings or QueueItem objects.
    URL items are resolved via yt-dlp; capture items pass through directly.
    """

    def __init__(self, loop: bool = False, cookies_from_browser: str | None = None) -> None:
        self._pending: deque[QueueItem] = deque()
        self._resolved: deque[ResolvedURL] = deque()  # ready to play
        self._all_items: list[QueueItem] = []         # original items for looping
        self._lock = threading.Lock()
        self._item_available = threading.Condition(self._lock)
        self._prefetch_thread: threading.Thread | None = None
        self._prefetching = False
        self._closed = False
        self._loop = loop
        self._loop_count: int = 0
        self._cookies_from_browser = cookies_from_browser

    def add(self, url: str) -> None:
        """Add a raw URL to the queue (backward-compatible)."""
        self.add_item(QueueItem(url=url))

    def add_item(self, item: QueueItem) -> None:
        """Add a QueueItem to the queue."""
        with self._item_available:
            self._pending.append(item)
            self._all_items.append(item)
            label = item.url or item.capture or "item"
            log.info("Queued: %s (%d pending)", label, len(self._pending))
            self._item_available.notify_all()

    def next(self) -> ResolvedURL | None:
        """Get the next resolved item. Blocks until available. Returns None when done."""
        qi: QueueItem | None = None
        with self._item_available:
            # Wait until we have a resolved item, a pending item to resolve, or are closed
            # Stay alive while a prefetch is in-flight — it will deliver a resolved item
            while not self._resolved and not self._pending:
                if self._closed and not self._prefetching:
                    if self._loop and self._all_items:
                        self._pending.extend(self._all_items)
                        self._loop_count += 1
                        log.info("Looping queue (%d items, loop %d)", len(self._pending), self._loop_count)
                        continue
                    break
                self._item_available.wait()

            if self._resolved:
                item = self._resolved.popleft()
                log.info("Dequeued: %s", item.title)
                return item

            if self._pending:
                qi = self._pending.popleft()

        # Resolve outside the lock (this is slow)
        if qi is not None:
            return self._resolve_item(qi)

        return None

    def _resolve_item(self, qi: QueueItem) -> ResolvedURL:
        """Resolve a QueueItem into a ResolvedURL."""
        if qi.capture:
            # Capture items don't need yt-dlp resolution
            return ResolvedURL(
                title=qi.title or qi.capture.title(),
                duration=qi.duration,
                is_live=False,
                source_urls=[],
                capture=qi.capture,
                window_title=qi.window_title,
                show_placeholder=qi.show_placeholder,
            )

        url = qi.url
        assert url is not None
        log.info("Resolving: %s", url)
        resolved = resolve(url, cookies_from_browser=self._cookies_from_browser)
        if resolved:
            download_audio(resolved)
            resolved.show_placeholder = qi.show_placeholder
            if qi.duration is not None:
                resolved.duration = qi.duration
            return resolved

        # yt-dlp failed — pass raw URL directly to ffmpeg
        if "youtube.com" in url or "youtu.be" in url:
            log.warning("yt-dlp failed for %s — YouTube may be blocking requests. "
                        "Try --cookies-from-browser chrome", url)
        else:
            log.warning("Failed to resolve %s, using raw URL", url)
        duration = probe_duration(url) if os.path.isfile(url) else None
        if qi.duration is not None:
            duration = qi.duration
        return ResolvedURL(
            title=qi.title or url,
            duration=duration,
            is_live=False,
            source_urls=[url],
            show_placeholder=qi.show_placeholder,
        )

    def start_prefetch(self) -> None:
        """Start background resolution of the next pending item."""
        with self._lock:
            if not self._pending or self._resolved:
                return
            # Skip capture items (nothing to prefetch)
            if self._pending[0].capture:
                return
            # Don't start another if one is already running
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                return

        self._prefetch_thread = threading.Thread(target=self._prefetch, daemon=True)
        self._prefetch_thread.start()

    def _prefetch(self) -> None:
        with self._lock:
            if not self._pending:
                return
            qi = self._pending.popleft()
            self._prefetching = True

        resolved = self._resolve_item(qi)
        with self._item_available:
            self._resolved.append(resolved)
            self._prefetching = False
            self._item_available.notify_all()
        log.info("Prefetched: %s", resolved.title)

    def remove(self, index: int) -> str | None:
        """Remove a pending item by index. Returns a label or None."""
        with self._lock:
            if 0 <= index < len(self._pending):
                qi = self._pending[index]
                del self._pending[index]
                return qi.url or qi.title or qi.capture
        return None

    def close(self) -> None:
        """Signal no more items will be added."""
        with self._item_available:
            self._closed = True
            self._item_available.notify_all()

    @property
    def loop_count(self) -> int:
        """Number of times the queue has looped back to the beginning."""
        return self._loop_count

    @property
    def status(self) -> str:
        with self._lock:
            return (
                f"Queue: {len(self._resolved)} resolved, "
                f"{len(self._pending)} pending"
            )
