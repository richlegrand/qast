"""Progress bar for console output during playback."""

from __future__ import annotations

import os
import sys
import threading
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

    Line 1: ▶ Title  (duration)
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

        duration = self._pipeline.current_duration
        elapsed = self._pipeline.elapsed

        # Build time string: elapsed / duration or just elapsed
        if duration and duration > 0:
            time_str = f"({_fmt_time(elapsed)} / {_fmt_time(duration)})"
        elif elapsed > 0:
            time_str = f"({_fmt_time(elapsed)})"
        else:
            time_str = ""

        # Truncate title if needed
        prefix = "\u25b6 "  # ▶
        max_title = cols - len(prefix) - len(time_str) - 2
        if max_title < 10:
            max_title = 10
        if len(title) > max_title:
            display_title = title[:max_title - 3] + "..."
        else:
            display_title = title

        # Line 1: title + time
        if time_str:
            line1 = f"{prefix}{display_title}  {time_str}"
        else:
            line1 = f"{prefix}{display_title}"

        # Line 2: queue info
        line2 = ""
        if self._queue:
            pending_count, next_title = self._queue.peek_next()
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
