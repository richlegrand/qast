"""Interactive controller — reads stdin during playback."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .log import get_logger

if TYPE_CHECKING:
    from .pipeline.pipeline import Pipeline
    from .queue import PlayQueue

log = get_logger("controller")


class Controller:
    """Daemon thread that reads stdin commands during playback.

    Commands:
        <URL>   — add URL to queue
        s       — skip current item
        q       — quit
        ?       — show queue status
    """

    def __init__(self, pipeline: Pipeline, queue: PlayQueue) -> None:
        self._pipeline = pipeline
        self._queue = queue
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                line = input().strip()
            except EOFError:
                break

            if not line:
                continue

            if line == "q":
                log.info("Quit requested")
                self._pipeline.shutdown()
                break
            elif line == "s":
                log.info("Skip requested")
                self._pipeline.skip_current()
            elif line == "?":
                print(self._queue.status)
            elif line.startswith("r "):
                try:
                    idx = int(line[2:].strip())
                    removed = self._queue.remove(idx)
                    if removed:
                        print(f"Removed: {removed}")
                    else:
                        print(f"Invalid index: {idx}")
                except ValueError:
                    print("Usage: r <index>")
            elif line.startswith(("http://", "https://")):
                self._queue.add(line)
                print(f"Added to queue: {line}")
            else:
                print("Commands: <URL> add | s=skip | r <N>=remove | q=quit | ?=status")

    def stop(self) -> None:
        self._stop.set()
