"""Thread-safe in-memory ring buffer for streaming data."""

from __future__ import annotations

import threading
import time
from collections import deque

from ..log import get_logger

log = get_logger("pipeline.ringbuf")


class RingBuffer:
    """Thread-safe ring buffer using a deque of byte chunks.

    Back-pressure: write() blocks when the buffer is full.
    read() blocks until data is available or EOF.
    """

    def __init__(self, max_bytes: int, min_bytes: int) -> None:
        self._max = max_bytes
        self._min = min_bytes
        self._buf: deque[bytes] = deque()
        self._size = 0
        self._total_written = 0
        self._closed = False
        self._first_read_time: float | None = None
        self._lock = threading.Lock()
        self._data_available = threading.Condition(self._lock)
        self._space_available = threading.Condition(self._lock)
        self._drained = threading.Condition(self._lock)

    @property
    def min_fill(self) -> int:
        return self._min

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def first_read_time(self) -> float | None:
        with self._lock:
            return self._first_read_time

    def write(self, data: bytes) -> None:
        """Append data to the buffer. Blocks if full (back-pressure)."""
        with self._space_available:
            while self._size + len(data) > self._max and not self._closed:
                self._space_available.wait()

            if self._closed:
                return

            self._buf.append(data)
            self._size += len(data)
            self._total_written += len(data)
            self._data_available.notify_all()

    def read(self, size: int) -> bytes:
        """Read up to `size` bytes. Blocks until data available.

        Returns b'' only when closed AND all buffered data has been drained.
        This ensures the TV receives every byte before seeing EOF.
        """
        with self._data_available:
            while not self._buf and not self._closed:
                self._data_available.wait()

            # EOF only after all data is drained
            if not self._buf:
                return b""

            chunk = self._buf.popleft()
            self._size -= len(chunk)

            if self._first_read_time is None:
                self._first_read_time = time.monotonic()

            # If chunk is larger than requested, put remainder back
            if len(chunk) > size:
                self._buf.appendleft(chunk[size:])
                self._size += len(chunk) - size
                chunk = chunk[:size]

            self._space_available.notify_all()
            self._drained.notify_all()
            return chunk

    def peek(self, size: int) -> bytes:
        """Return up to `size` bytes from the head without consuming them."""
        with self._lock:
            if not self._buf:
                return b""
            result = bytearray()
            for chunk in self._buf:
                remaining = size - len(result)
                if remaining <= 0:
                    break
                result.extend(chunk[:remaining])
            return bytes(result)

    def wait_min_fill(self, timeout: float) -> bool:
        """Block until min_bytes have been written. Returns True if threshold met."""
        with self._data_available:
            deadline_met = self._data_available.wait_for(
                lambda: self._total_written >= self._min or self._closed,
                timeout=timeout,
            )
            if self._closed and self._total_written < self._min:
                return False
            return deadline_met

    def wait_drained(self, timeout: float = 120) -> bool:
        """Block until all buffered data has been read. Call after close()."""
        with self._drained:
            return self._drained.wait_for(
                lambda: self._size == 0,
                timeout=timeout,
            )

    def close(self) -> None:
        """Signal no more writes. Readers will drain remaining data then get EOF."""
        with self._lock:
            self._closed = True
            self._data_available.notify_all()
            self._space_available.notify_all()
        log.debug("Ring buffer closed (total written: %d bytes, remaining: %d bytes)",
                  self._total_written, self._size)
