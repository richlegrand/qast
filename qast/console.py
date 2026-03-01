"""Interactive console — REPL prompt with status updates on segment changes."""

from __future__ import annotations

import select
import sys
import termios
import threading
import tty
from typing import TYPE_CHECKING

from .log import get_logger
from .source import parse_source

if TYPE_CHECKING:
    from .pipeline.pipeline import Pipeline
    from .queue import PlayQueue

log = get_logger("console")


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


class Console:
    """REPL-style console with status updates on segment changes.

    Uses cbreak mode for character-by-character input so status updates
    can print above the prompt without losing the user's partially-typed
    input.
    """

    def __init__(self, pipeline: Pipeline, queue: PlayQueue | None = None) -> None:
        self._pipeline = pipeline
        self._queue = queue
        self._input_buf = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_termios = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restore_termios()

    def _restore_termios(self) -> None:
        if self._old_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)
            except (termios.error, OSError):
                pass

    # ── status printing ──────────────────────────────────────────

    def on_segment_change(self) -> None:
        """Called by the pipeline when the playing segment changes."""
        lines = self._build_status()
        if lines:
            self._print_above(lines)

    def _build_status(self) -> list[str]:
        lines = []
        title = self._pipeline.now_playing
        if title:
            duration = self._pipeline.current_duration
            if duration and duration > 0:
                lines.append(f"Playing: {title}  ({_fmt_time(duration)})")
            else:
                lines.append(f"Playing: {title}")

        if self._queue:
            pending_count, next_title = self._queue.peek_next()
            if next_title:
                extra = f"  [{pending_count} pending]" if pending_count > 1 else ""
                lines.append(f"Up next: {next_title}{extra}")
        return lines

    def _print_above(self, lines: list[str]) -> None:
        """Print lines above the current prompt, preserving user input."""
        with self._lock:
            sys.stdout.write("\r\033[K")
            for line in lines:
                sys.stdout.write(f"{line}\n")
            sys.stdout.write(f"> {self._input_buf}")
            sys.stdout.flush()

    # ── input loop ───────────────────────────────────────────────

    def _run(self) -> None:
        fd = sys.stdin.fileno()
        try:
            self._old_termios = termios.tcgetattr(fd)
        except (termios.error, OSError):
            self._run_fallback()
            return

        try:
            tty.setcbreak(fd)
            sys.stdout.write("> ")
            sys.stdout.flush()

            while not self._stop.is_set():
                # Poll stdin with timeout so we can check _stop
                if not select.select([sys.stdin], [], [], 0.2)[0]:
                    continue
                ch = sys.stdin.read(1)
                if not ch:
                    break

                if ch in ("\n", "\r"):
                    with self._lock:
                        line = self._input_buf
                        self._input_buf = ""
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    if line:
                        self._handle_command(line)
                    else:
                        sys.stdout.write("> ")
                        sys.stdout.flush()
                elif ch in ("\x7f", "\x08"):  # backspace
                    with self._lock:
                        if self._input_buf:
                            self._input_buf = self._input_buf[:-1]
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                elif ch == "\x03":  # Ctrl-C
                    sys.stdout.write("\n")
                    self._pipeline.shutdown()
                    break
                elif ch >= " ":  # printable
                    with self._lock:
                        self._input_buf += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        finally:
            self._restore_termios()

    def _run_fallback(self) -> None:
        """Fallback when cbreak mode isn't available."""
        while not self._stop.is_set():
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if line:
                self._handle_command(line)

    # ── command handling ─────────────────────────────────────────

    def _handle_command(self, line: str) -> None:
        if line == "q":
            self._print_above(["Stopping..."])
            self._pipeline.shutdown()
            self._stop.set()
        elif line == "s":
            self._print_above(["Skipping..."])
            self._pipeline.skip_current()
        elif line == "?":
            self._print_above(self._build_full_status())
        elif line.startswith("r "):
            self._cmd_remove(line)
        else:
            self._cmd_add(line)

    def _build_full_status(self) -> list[str]:
        lines = []
        title = self._pipeline.now_playing
        if title:
            duration = self._pipeline.current_duration
            elapsed = self._pipeline.elapsed
            if duration and duration > 0:
                lines.append(f"Playing: {title}  {_fmt_time(elapsed)} / {_fmt_time(duration)}")
            elif elapsed > 0:
                lines.append(f"Playing: {title}  {_fmt_time(elapsed)}")
            else:
                lines.append(f"Playing: {title}")
        if self._queue:
            rotation = self._queue.rotation_labels(current_title=title)
            if rotation:
                for i, label in enumerate(rotation):
                    lines.append(f"  [{i}] {label}")
            else:
                lines.append("Queue: empty")
        return lines or ["Nothing playing"]

    def _cmd_remove(self, line: str) -> None:
        if not self._queue:
            self._print_above(["No queue"])
            return
        try:
            idx = int(line[2:].strip())
            removed = self._queue.remove(idx)
            if removed:
                self._print_above([f"Removed: {removed}"])
            else:
                self._print_above([f"Invalid index: {idx}"])
        except ValueError:
            self._print_above(["Usage: r <index>"])

    def _cmd_add(self, line: str) -> None:
        if not self._queue:
            self._print_above([f"Unknown command: {line}"])
            return
        item = parse_source(line)
        self._queue.add_item(item)
        label = item.title or item.url or item.capture or line
        if item.duration:
            label += f"  ({_fmt_time(item.duration)})"
        self._print_above([f"Added: {label}"])
