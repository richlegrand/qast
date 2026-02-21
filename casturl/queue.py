"""Thread-safe play queue with prefetch."""

from __future__ import annotations

import threading
from collections import deque

from .log import get_logger
from .resolve.ytdlp import ResolvedURL, download_audio, resolve

log = get_logger("queue")


class PlayQueue:
    """Thread-safe URL queue with background prefetch.

    URLs are added as raw strings. The queue resolves them via yt-dlp,
    prefetching the next item while the current one plays.
    """

    def __init__(self) -> None:
        self._pending: deque[str] = deque()       # raw URLs not yet resolved
        self._resolved: deque[ResolvedURL] = deque()  # ready to play
        self._lock = threading.Lock()
        self._item_available = threading.Condition(self._lock)
        self._prefetch_thread: threading.Thread | None = None
        self._closed = False

    def add(self, url: str) -> None:
        """Add a raw URL to the queue."""
        with self._item_available:
            self._pending.append(url)
            log.info("Queued: %s (%d pending)", url, len(self._pending))
            self._item_available.notify_all()

    def next(self) -> ResolvedURL | None:
        """Get the next resolved item. Blocks until available. Returns None when done."""
        url: str | None = None
        with self._item_available:
            # Wait until we have a resolved item, a pending item to resolve, or are closed
            while not self._resolved and not self._pending and not self._closed:
                self._item_available.wait()

            if self._resolved:
                item = self._resolved.popleft()
                log.info("Dequeued: %s", item.title)
                return item

            if self._pending:
                url = self._pending.popleft()

        # Resolve outside the lock (this is slow)
        if url:
            log.info("Resolving: %s", url)
            resolved = resolve(url)
            if resolved:
                download_audio(resolved)
                return resolved
            # yt-dlp failed — pass raw URL directly to ffmpeg
            log.warning("Failed to resolve %s, using raw URL", url)
            return ResolvedURL(
                title=url,
                duration=None,
                is_live=False,
                source_urls=[url],
            )

        return None

    def start_prefetch(self) -> None:
        """Start background resolution of the next pending URL."""
        with self._lock:
            if not self._pending or self._resolved:
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
            url = self._pending.popleft()

        log.info("Prefetching: %s", url)
        resolved = resolve(url)
        if resolved:
            download_audio(resolved)
            with self._item_available:
                self._resolved.append(resolved)
                self._item_available.notify_all()
            log.info("Prefetched: %s", resolved.title)
        else:
            # Failed — re-add as a raw-URL fallback
            with self._item_available:
                self._resolved.append(ResolvedURL(
                    title=url,
                    duration=None,
                    is_live=False,
                    source_urls=[url],
                ))
                self._item_available.notify_all()
            log.warning("Prefetch failed for %s, using raw URL", url)

    def close(self) -> None:
        """Signal no more items will be added."""
        with self._item_available:
            self._closed = True
            self._item_available.notify_all()

    @property
    def status(self) -> str:
        with self._lock:
            return (
                f"Queue: {len(self._resolved)} resolved, "
                f"{len(self._pending)} pending"
            )
