"""Progress bar for console output during playback."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline.pipeline import Pipeline
    from .queue import PlayQueue


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    seconds = int(seconds)
    if seconds >= 3600:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


class ProgressBar:
    """Two-line progress display updated in a daemon thread.

    Line 1: ▶ Title  [elapsed / total]  ████░░░░  pct%
    Line 2:   Up next: ... (N pending)
    """

    def __init__(
        self,
        pipeline: Pipeline,
        queue: PlayQueue | None = None,
        interval: float = 0.5,
    ) -> None:
        self._pipeline = pipeline
        self._queue = queue
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lines_written = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Clear the progress lines
        self._clear()

    def _clear(self) -> None:
        if self._lines_written > 0:
            for _ in range(self._lines_written - 1):
                sys.stdout.write("\033[F")  # cursor up
            for _ in range(self._lines_written):
                sys.stdout.write("\033[K\n")  # clear line
            for _ in range(self._lines_written):
                sys.stdout.write("\033[F")  # cursor back up
            sys.stdout.flush()
            self._lines_written = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self._draw()
            self._stop.wait(self._interval)

    def _draw(self) -> None:
        title = self._pipeline.now_playing
        if title is None:
            return

        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80

        start = self._pipeline.segment_start_time
        elapsed = time.monotonic() - start if start else 0.0
        duration = self._pipeline.current_duration

        # Build time string
        if duration and duration > 0:
            pct = min(elapsed / duration, 1.0)
            time_str = f"[{_fmt_time(elapsed)} / {_fmt_time(duration)}]"
        elif duration is None or duration == 0:
            pct = -1  # unknown
            time_str = f"[{_fmt_time(elapsed)}]"
        else:
            pct = -1
            time_str = f"[{_fmt_time(elapsed)}]"

        # Truncate title if needed
        prefix = "\u25b6 "  # ▶
        max_title = cols - len(prefix) - len(time_str) - 4  # padding
        if max_title < 10:
            max_title = 10
        if len(title) > max_title:
            display_title = title[:max_title - 3] + "..."
        else:
            display_title = title

        # Line 1: title + time + optional bar
        if pct >= 0:
            pct_str = f"{int(pct * 100):3d}%"
            # Bar takes remaining space
            used = len(prefix) + len(display_title) + 2 + len(time_str) + 2 + len(pct_str) + 2
            bar_width = cols - used
            if bar_width < 5:
                bar_width = 0

            if bar_width > 0:
                filled = int(bar_width * pct)
                bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
                line1 = f"{prefix}{display_title}  {time_str}  {bar}  {pct_str}"
            else:
                line1 = f"{prefix}{display_title}  {time_str}  {pct_str}"
        else:
            line1 = f"{prefix}{display_title}  {time_str}"

        # Line 2: queue info
        line2 = ""
        if self._queue:
            with self._queue._lock:
                pending_count = len(self._queue._pending)
                # Peek at the next resolved item for "up next"
                next_title = None
                if self._queue._resolved:
                    next_title = self._queue._resolved[0].title
                elif self._queue._pending:
                    qi = self._queue._pending[0]
                    next_title = qi.title or qi.url or qi.capture or "item"

            if next_title:
                if pending_count > 0:
                    line2 = f"  Up next: {next_title} ({pending_count} pending)"
                else:
                    line2 = f"  Up next: {next_title}"

        # Move cursor up to overwrite previous output
        if self._lines_written > 0:
            for _ in range(self._lines_written - 1):
                sys.stdout.write("\033[F")
            sys.stdout.write("\r")

        # Write lines
        lines = [line1[:cols]]
        if line2:
            lines.append(line2[:cols])

        output = ""
        for i, line in enumerate(lines):
            output += f"\033[K{line}"
            if i < len(lines) - 1:
                output += "\n"

        sys.stdout.write(output)
        sys.stdout.flush()
        self._lines_written = len(lines)
