"""Pipeline orchestrator: segments -> master muxer -> ring buffer -> HTTP server.

Supports placeholders between queue items and sequential segment playback
through a single long-lived master muxer.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .. import config
from ..log import get_logger
from ..serve.server import StreamServer
from .master import MasterMuxer
from .placeholder import PlaceholderSegment
from .ringbuf import RingBuffer
from .segment import SegmentFFmpeg

if TYPE_CHECKING:
    from ..queue import PlayQueue

log = get_logger("pipeline")

# Placeholder durations
LOADING_DURATION = 10.0
UP_NEXT_DURATION = 5.0


class Pipeline:
    """Orchestrates the full streaming pipeline.

    The master muxer stays alive across all segments (one continuous fMP4 stream).
    The bridge thread consumes segments in sequence: placeholder -> real -> placeholder -> real...
    """

    def __init__(self, debug: bool = False) -> None:
        self.ring_buffer = RingBuffer(config.BUFFER_MAX, config.BUFFER_MIN)
        self.master = MasterMuxer()
        self.server = StreamServer(self.ring_buffer)
        self._debug = debug
        self._current_segment: SegmentFFmpeg | PlaceholderSegment | None = None
        self._bridge_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._ts_offset: float = 0.0
        self._skip_event = threading.Event()
        self._shutdown_event = threading.Event()

    def start_single(
        self,
        source_urls: list[str],
        is_live: bool = False,
        title: str | None = None,
    ) -> None:
        """Start pipeline for a single video, optionally with a loading placeholder."""
        debug_path = "/tmp/casturl_debug.mp4" if self._debug else None
        self.master.start(debug_path=debug_path)
        self.master.start_reader(self.ring_buffer)
        self.server.start()

        self._bridge_thread = threading.Thread(
            target=self._bridge_single,
            args=(source_urls, is_live, title),
            daemon=True,
        )
        self._bridge_thread.start()
        self._start_monitor()

    def _bridge_single(
        self,
        source_urls: list[str],
        is_live: bool,
        title: str | None,
    ) -> None:
        """Bridge for single-video mode: optional placeholder then real segment."""
        master_stdin = self.master.stdin
        if not master_stdin:
            log.error("Bridge: no master stdin")
            return

        try:
            # Show loading placeholder if we have a title
            if title:
                actual = self._run_segment(
                    PlaceholderSegment(
                        text=f"Loading: {title}",
                        duration=LOADING_DURATION,
                        ts_offset=self._ts_offset,
                    ),
                    master_stdin,
                )
                self._ts_offset += actual

            # Loop the segment until shutdown/Ctrl+C
            loop = 1
            while not self._shutdown_event.is_set():
                log.info("Playing (loop %d, ts_offset=%.3f)", loop, self._ts_offset)
                seg = SegmentFFmpeg(source_urls, ts_offset=self._ts_offset, is_live=is_live)
                actual = self._run_segment(seg, master_stdin)
                self._ts_offset += actual
                log.info("Segment done (actual=%.3fs, new ts_offset=%.3f)", actual, self._ts_offset)

                if is_live:
                    break  # live streams don't loop

                loop += 1
        except _PipelineShutdown:
            log.debug("Bridge: shutdown requested")
        finally:
            try:
                master_stdin.close()
            except OSError:
                pass
            log.debug("Bridge single finished")

    def start_queue(self, queue: PlayQueue) -> None:
        """Start pipeline consuming items from a queue."""
        debug_path = "/tmp/casturl_debug.mp4" if self._debug else None
        self.master.start(debug_path=debug_path)
        self.master.start_reader(self.ring_buffer)
        self.server.start()

        self._bridge_thread = threading.Thread(
            target=self._bridge_queue,
            args=(queue,),
            daemon=True,
        )
        self._bridge_thread.start()
        self._start_monitor()

    def _bridge_queue(self, queue: PlayQueue) -> None:
        """Bridge for queue mode: loop consuming items."""
        master_stdin = self.master.stdin
        if not master_stdin:
            log.error("Bridge: no master stdin")
            return

        first = True
        try:
            while not self._shutdown_event.is_set():
                item = queue.next()
                if item is None:
                    log.info("Queue exhausted")
                    break

                # Show placeholder
                if first:
                    placeholder_text = f"Loading: {item.title}"
                    ph_duration = LOADING_DURATION
                    first = False
                else:
                    placeholder_text = f"Up next: {item.title}"
                    ph_duration = UP_NEXT_DURATION

                actual = self._run_segment(
                    PlaceholderSegment(
                        text=placeholder_text,
                        duration=ph_duration,
                        ts_offset=self._ts_offset,
                    ),
                    master_stdin,
                )
                self._ts_offset += actual

                # Reset skip event for this item
                self._skip_event.clear()

                # Real segment
                seg = SegmentFFmpeg(
                    item.source_urls,
                    ts_offset=self._ts_offset,
                    is_live=item.is_live,
                )
                self._current_segment = seg
                actual = self._run_segment(seg, master_stdin)
                self._current_segment = None
                self._ts_offset += actual
                log.info("Segment done (actual=%.3fs, new ts_offset=%.3f)", actual, self._ts_offset)

                # Clean up temp files from this item
                item.cleanup()

                # Start prefetch for next item
                queue.start_prefetch()

        except _PipelineShutdown:
            log.debug("Bridge queue: shutdown requested")
        finally:
            try:
                master_stdin.close()
            except OSError:
                pass
            log.debug("Bridge queue finished")

    def _run_segment(self, segment, master_stdin) -> float:
        """Run a single segment, piping its stdout to master stdin.

        Returns the actual duration of the segment (for ts_offset tracking).
        """
        if self._shutdown_event.is_set():
            raise _PipelineShutdown

        segment.start()
        try:
            while True:
                if self._shutdown_event.is_set():
                    segment.kill()
                    raise _PipelineShutdown
                if self._skip_event.is_set():
                    segment.kill()
                    log.info("Segment skipped")
                    return 0.0

                chunk = segment.stdout.read(config.PIPE_CHUNK)
                if not chunk:
                    break
                master_stdin.write(chunk)

            segment.wait()

            # Return actual duration for ts_offset tracking.
            if isinstance(segment, PlaceholderSegment):
                return segment.duration
            if isinstance(segment, SegmentFFmpeg) and segment.actual_duration:
                return segment.actual_duration
            return 0.0

        except (BrokenPipeError, OSError):
            log.debug("Segment bridge: pipe broken")
            raise _PipelineShutdown

    def _start_monitor(self) -> None:
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

    def _monitor(self) -> None:
        """Periodically print buffer status."""
        while not self._shutdown_event.is_set():
            buf = self.ring_buffer
            if buf.closed and buf.size == 0:
                break
            size_mb = buf.size / (1024 * 1024)
            total_mb = buf._total_written / (1024 * 1024)
            pct = (buf.size / config.BUFFER_MAX) * 100
            print(f"  Buffer: {size_mb:.1f}MB ({pct:.0f}%) | Total streamed: {total_mb:.1f}MB", end="\r", flush=True)
            self._shutdown_event.wait(config.BUFFER_MONITOR_INTERVAL)
        print()  # clear the \r line

    def skip_current(self) -> None:
        """Skip the currently playing segment."""
        self._skip_event.set()

    def wait_ready(self, timeout: float = config.BUFFER_FILL_TIMEOUT) -> bool:
        """Block until ring buffer has enough data to start casting."""
        log.info("Waiting for buffer to fill (%d bytes min)...", config.BUFFER_MIN)
        ok = self.ring_buffer.wait_min_fill(timeout)
        if ok:
            log.info("Buffer ready")
        else:
            log.warning("Buffer fill timed out")
        return ok

    @property
    def serve_url(self) -> str:
        return self.server.url

    def wait_done(self) -> None:
        """Block until shutdown is requested (streams loop until stopped)."""
        self._shutdown_event.wait()
        log.debug("Pipeline done")

    def shutdown(self) -> None:
        """Kill all subprocesses and stop the server."""
        log.info("Shutting down pipeline")
        self._shutdown_event.set()
        self._skip_event.set()  # unblock any segment reads
        if self._current_segment:
            self._current_segment.kill()
        self.master.kill()
        self.ring_buffer.close()
        self.server.stop()


class _PipelineShutdown(Exception):
    """Internal signal for pipeline shutdown."""
