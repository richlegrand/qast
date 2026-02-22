"""Pipeline orchestrator: segments -> TS rewriter -> master muxer -> ring buffer -> HTTP.

Segments produce MPEG-TS which is piped through the TSRewriter (for PTS and
continuity-counter continuity), then through the master muxer (TS -> fMP4),
into an in-memory ring buffer served over HTTP.

PTS offsets use exact 90 kHz integer ticks — no float-based computation.
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
from .tsrewrite import TSRewriter

if TYPE_CHECKING:
    from ..queue import PlayQueue

log = get_logger("pipeline")

# Placeholder durations
LOADING_DURATION = 10.0
UP_NEXT_DURATION = 5.0

# One video frame in 90 kHz ticks at configured fps
_TICKS_PER_FRAME = 90_000 // int(config.VIDEO_FPS)


class Pipeline:
    """Orchestrates the full streaming pipeline.

    The master muxer stays alive across all segments (one continuous fMP4 stream).
    The bridge thread consumes segments in sequence: placeholder -> real -> ...
    The TSRewriter ensures PTS/DTS/PCR and continuity counter continuity
    across segment boundaries so the master muxer sees one seamless TS input.
    """

    def __init__(self, debug: bool = False) -> None:
        self.ring_buffer = RingBuffer(config.BUFFER_MAX, config.BUFFER_MIN)
        self.master = MasterMuxer()
        self.server = StreamServer(self.ring_buffer)
        self._rewriter = TSRewriter()
        self._debug = debug
        self._current_segment: SegmentFFmpeg | PlaceholderSegment | None = None
        self._bridge_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._skip_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._total_audio_samples: int = 0  # cumulative audio samples across all segments

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

        self._rewriter.set_offset(0)
        try:
            # Show loading placeholder if we have a title
            if title:
                ph = PlaceholderSegment(
                    text=f"Loading: {title}",
                    duration=LOADING_DURATION,
                )
                self._run_segment(ph, master_stdin)
                self._advance_offset()

            # Loop the segment until shutdown/Ctrl+C
            loop = 1
            consecutive_failures = 0
            while not self._shutdown_event.is_set():
                log.info("Playing (loop %d)", loop)
                seg = SegmentFFmpeg(source_urls, is_live=is_live)
                self._run_segment(seg, master_stdin)

                if self._rewriter.max_pts > 0:
                    self._advance_offset()
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        log.error("Segment failed %d times in a row, giving up", consecutive_failures)
                        break

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
            if not self._shutdown_event.is_set():
                log.debug("Bridge single: waiting for master to finish")
                self.master.wait()
                log.debug("Bridge single: waiting for buffer drain")
                self.ring_buffer.wait_drained(timeout=300)
            log.debug("Bridge single finished")
            self._shutdown_event.set()

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
        consecutive_failures = 0
        self._rewriter.set_offset(0)
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

                ph = PlaceholderSegment(
                    text=placeholder_text,
                    duration=ph_duration,
                )
                self._run_segment(ph, master_stdin)
                self._advance_offset()

                # Reset skip event for this item
                self._skip_event.clear()

                # Real segment
                seg = SegmentFFmpeg(
                    item.source_urls,
                    is_live=item.is_live,
                )
                self._current_segment = seg
                self._run_segment(seg, master_stdin)
                self._current_segment = None

                if self._rewriter.max_pts > 0:
                    self._advance_offset()
                    consecutive_failures = 0
                    log.info("Segment done (offset=%d ticks, %.3fs)",
                             self._rewriter._offset,
                             self._rewriter._offset / 90_000)
                else:
                    consecutive_failures += 1
                    log.warning("Segment produced no data (%d consecutive failures)",
                                consecutive_failures)
                    if consecutive_failures >= 3:
                        log.error("Too many consecutive failures, giving up")
                        break

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
            if not self._shutdown_event.is_set():
                log.debug("Bridge queue: waiting for master to finish")
                self.master.wait()
                log.debug("Bridge queue: waiting for buffer drain")
                self.ring_buffer.wait_drained(timeout=300)
            log.debug("Bridge queue finished")
            self._shutdown_event.set()

    def _advance_offset(self) -> None:
        """Advance the rewriter offset based on the measured max PTS.

        Aligns to the next video frame boundary (multiple of TICKS_PER_FRAME)
        and computes a video PTS correction to match the audio master clock.

        Audio is the master clock: the decoder plays audio at 44100 Hz
        regardless of PTS values.  Each AAC frame = 1024 samples = 2089.8
        ticks of real playback time (non-integer).  Over a segment, audio
        real time drifts from video PTS.  We correct video PTS to match
        where audio actually is.
        """
        # Accumulate actual audio render time
        self._total_audio_samples += self._rewriter.audio_frame_count * 1024

        # Audio clock position in 90 kHz ticks (integer math, <=1 tick error)
        # 90000/44100 = 100/49
        audio_clock_ticks = self._total_audio_samples * 100 // 49

        # Base offset from max video PTS (frame-aligned, as before)
        max_pts = self._rewriter.max_pts
        new_offset = ((max_pts // _TICKS_PER_FRAME) + 1) * _TICKS_PER_FRAME

        # Video correction: shift video PTS to match where audio actually is
        video_correction = audio_clock_ticks - new_offset

        log.info("PTS offset: %d -> %d, video_correction=%d (%.1fms), "
                 "audio_clock=%d, audio_frames=%d",
                 self._rewriter._offset, new_offset,
                 video_correction, video_correction / 90,
                 audio_clock_ticks, self._rewriter.audio_frame_count)

        self._rewriter.set_offset(new_offset, video_correction=video_correction)

    def _run_segment(self, segment, master_stdin) -> None:
        """Run a single segment, piping its stdout through the TS rewriter to master stdin."""
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
                    return

                chunk = segment.stdout.read(config.PIPE_CHUNK)
                if not chunk:
                    break
                rewritten = self._rewriter.process(chunk)
                if rewritten:
                    master_stdin.write(rewritten)

            # Discard any partial packet (incomplete 188-byte TS packets)
            self._rewriter.flush()

            segment.wait()

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
