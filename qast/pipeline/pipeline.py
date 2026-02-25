"""Pipeline orchestrator: segments -> TS rewriter -> [master muxer] -> ring buffer -> HTTP.

Segments produce MPEG-TS which is piped through the TSRewriter (for PTS and
continuity-counter continuity), then optionally through the master muxer
(TS -> fMP4), into an in-memory ring buffer served over HTTP.

When raw_ts=True (e.g. Roku), the master muxer is skipped and rewritten
MPEG-TS is written directly to the ring buffer (served as video/MP2T).

PTS offsets use exact 90 kHz integer ticks — no float-based computation.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from .. import config
from ..capture import ScreenSegment, WebcamSegment, _find_window_by_title
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

    def __init__(
        self,
        save_stream: str | None = None,
        raw_ts: bool = False,
        buffer_max: int | None = None,
        buffer_min: int | None = None,
        verbose: bool = False,
    ) -> None:
        self._raw_ts = raw_ts
        self._verbose = verbose
        self.ring_buffer = RingBuffer(
            buffer_max or config.BUFFER_MAX,
            buffer_min or config.BUFFER_MIN,
        )
        self.master = None if raw_ts else MasterMuxer()
        self._disconnect_event = threading.Event()
        content_type = "video/mpeg" if raw_ts else "video/mp4"
        self.server = StreamServer(
            self.ring_buffer,
            content_type=content_type,
            disconnect_event=self._disconnect_event,
            fake_content_length=raw_ts,
        )
        self._rewriter = TSRewriter()
        self._save_stream: str | None = save_stream
        self._save_file = None  # IO[bytes] | None — for raw TS save
        self._current_segment: SegmentFFmpeg | PlaceholderSegment | None = None
        self._bridge_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._skip_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._total_audio_samples: int = 0  # cumulative audio samples across all segments
        # Public state for progress bar / status API
        self.now_playing: str | None = None
        self.segment_start_time: float | None = None
        self.current_duration: float | None = None

    def start_single(
        self,
        source_urls: list[str],
        is_live: bool = False,
        title: str | None = None,
        loading_duration: float = LOADING_DURATION,
        show_placeholder: bool = True,
    ) -> None:
        """Start pipeline for a single video, optionally with a loading placeholder."""
        if self.master:
            self.master.start(save_path=self._save_stream)
            self.master.start_reader(self.ring_buffer)
        else:
            if self._save_stream:
                self._save_file = open(self._save_stream, "wb")
                log.info("Saving TS stream to %s", self._save_stream)
        self.server.start()

        self._bridge_thread = threading.Thread(
            target=self._bridge_single,
            args=(source_urls, is_live, title, loading_duration, show_placeholder),
            daemon=True,
        )
        self._bridge_thread.start()
        self._start_monitor()

    def _bridge_single(
        self,
        source_urls: list[str],
        is_live: bool,
        title: str | None,
        loading_duration: float = LOADING_DURATION,
        show_placeholder: bool = True,
    ) -> None:
        """Bridge for single-video mode: optional placeholder then real segment."""
        sink = self._get_sink()
        if sink is None:
            log.error("Bridge: no write target")
            return

        self._rewriter.set_offset(0)
        try:
            # Show loading placeholder if we have a title
            if show_placeholder and title:
                ph = PlaceholderSegment(
                    text=f"Loading: {title}",
                    duration=loading_duration,
                )
                self._run_segment(ph, sink)
                self._advance_offset()

            # Loop the segment until shutdown/Ctrl+C
            loop = 1
            consecutive_failures = 0
            self.now_playing = title
            self.current_duration = None
            while not self._shutdown_event.is_set():
                log.info("Playing (loop %d)", loop)
                self.segment_start_time = time.monotonic()
                seg = SegmentFFmpeg(source_urls, is_live=is_live)
                self._run_segment(seg, sink)

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

                self._close_save()  # only save first pass
                loop += 1
        except _PipelineShutdown:
            log.debug("Bridge: shutdown requested")
        finally:
            self._close_sink()
            if not self._shutdown_event.is_set():
                if self.master:
                    log.debug("Bridge single: waiting for master to finish")
                    self.master.wait()
                log.debug("Bridge single: waiting for buffer drain")
                self.ring_buffer.wait_drained(timeout=300)
            log.debug("Bridge single finished")
            self._shutdown_event.set()

    def start_queue(self, queue: PlayQueue, show_placeholder: bool = True) -> None:
        """Start pipeline consuming items from a queue."""
        if self.master:
            self.master.start(save_path=self._save_stream)
            self.master.start_reader(self.ring_buffer)
        else:
            if self._save_stream:
                self._save_file = open(self._save_stream, "wb")
                log.info("Saving TS stream to %s", self._save_stream)
        self.server.start()

        self._bridge_thread = threading.Thread(
            target=self._bridge_queue,
            args=(queue, show_placeholder),
            daemon=True,
        )
        self._bridge_thread.start()
        self._start_monitor()

    def _bridge_queue(self, queue: PlayQueue, show_placeholder: bool = True) -> None:
        """Bridge for queue mode: loop consuming items."""
        sink = self._get_sink()
        if sink is None:
            log.error("Bridge: no write target")
            return

        first = True
        save_closed = False
        consecutive_failures = 0
        self._rewriter.set_offset(0)
        try:
            while not self._shutdown_event.is_set():
                item = queue.next()
                if item is None:
                    log.info("Queue exhausted")
                    break

                # Per-item placeholder (falls back to pipeline-level default)
                item_show_placeholder = show_placeholder and item.show_placeholder
                if item_show_placeholder:
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
                    self._run_segment(ph, sink)
                    self._advance_offset()
                else:
                    first = False

                # Reset skip event for this item
                self._skip_event.clear()

                # Update state for progress bar / status API
                self.now_playing = item.title
                self.current_duration = item.duration
                self.segment_start_time = time.monotonic()

                # Create segment based on item type
                if item.capture:
                    seg = self._create_capture_segment(item)
                else:
                    seg = SegmentFFmpeg(
                        item.source_urls,
                        is_live=item.is_live,
                        duration=item.duration,
                    )
                self._current_segment = seg
                self._run_segment(seg, sink)
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

                # Close save file after first pass through the queue
                if not save_closed and queue.loop_count > 0:
                    self._close_save()
                    save_closed = True

                # Start prefetch for next item
                queue.start_prefetch()

        except _PipelineShutdown:
            log.debug("Bridge queue: shutdown requested")
        finally:
            self._close_sink()
            if not self._shutdown_event.is_set():
                if self.master:
                    log.debug("Bridge queue: waiting for master to finish")
                    self.master.wait()
                log.debug("Bridge queue: waiting for buffer drain")
                self.ring_buffer.wait_drained(timeout=300)
            log.debug("Bridge queue finished")
            self._shutdown_event.set()

    def start_capture(self, segment, title: str | None = None) -> None:
        """Start pipeline for a live capture source (runs until killed)."""
        if self.master:
            self.master.start(save_path=self._save_stream)
            self.master.start_reader(self.ring_buffer)
        else:
            if self._save_stream:
                self._save_file = open(self._save_stream, "wb")
                log.info("Saving TS stream to %s", self._save_stream)
        self.server.start()

        self._bridge_thread = threading.Thread(
            target=self._bridge_capture, args=(segment, title), daemon=True,
        )
        self._bridge_thread.start()
        self._start_monitor()

    def _bridge_capture(self, segment, title: str | None = None) -> None:
        """Bridge for capture mode: single segment, no loop."""
        sink = self._get_sink()
        if sink is None:
            log.error("Bridge capture: no write target")
            return

        self.now_playing = title or "Capture"
        self.current_duration = getattr(segment, 'duration', None)
        self.segment_start_time = time.monotonic()

        self._rewriter.set_offset(0)
        try:
            self._run_segment(segment, sink)
        except _PipelineShutdown:
            log.debug("Bridge capture: shutdown requested")
        finally:
            self._close_sink()
            if not self._shutdown_event.is_set():
                if self.master:
                    log.debug("Bridge capture: waiting for master to finish")
                    self.master.wait()
                log.debug("Bridge capture: waiting for buffer drain")
                self.ring_buffer.wait_drained(timeout=10)
            log.debug("Bridge capture finished")
            self._shutdown_event.set()

    def _create_capture_segment(self, item) -> ScreenSegment | WebcamSegment:
        """Create a capture segment from a ResolvedURL with capture config."""
        if item.capture == "screen":
            return ScreenSegment(duration=item.duration)
        elif item.capture == "window":
            wid, w, h = _find_window_by_title(item.window_title)
            return ScreenSegment(
                window_id=wid,
                window_size=(w, h),
                duration=item.duration,
            )
        elif item.capture == "webcam":
            return WebcamSegment(duration=item.duration)
        else:
            raise ValueError(f"Unknown capture type: {item.capture}")

    def _advance_offset(self) -> None:
        """Advance the rewriter offset based on the measured max PTS.

        Aligns to the next video frame boundary (multiple of TICKS_PER_FRAME).

        For fMP4 (Chromecast): also computes a video PTS correction to match
        the audio master clock.  Chromecast plays audio at 44100 Hz regardless
        of PTS values, so audio real time drifts from video PTS.

        For raw TS (Roku): no correction needed.  The decoder uses PTS for
        both audio and video timing, so there's no clock drift.
        """
        # Base offset from max video PTS (frame-aligned)
        max_pts = self._rewriter.max_pts
        new_offset = ((max_pts // _TICKS_PER_FRAME) + 1) * _TICKS_PER_FRAME

        video_correction = 0
        if not self._raw_ts:
            # Accumulate actual audio render time
            self._total_audio_samples += self._rewriter.audio_frame_count * 1024

            # Audio clock position in 90 kHz ticks (integer math, <=1 tick error)
            # 90000/44100 = 100/49
            audio_clock_ticks = self._total_audio_samples * 100 // 49

            # Video correction: shift video PTS to match where audio actually is
            video_correction = audio_clock_ticks - new_offset

            log.info("PTS offset: %d -> %d, video_correction=%d (%.1fms), "
                     "audio_clock=%d, audio_frames=%d",
                     self._rewriter._offset, new_offset,
                     video_correction, video_correction / 90,
                     audio_clock_ticks, self._rewriter.audio_frame_count)
        else:
            log.info("PTS offset: %d -> %d (raw TS, no correction), "
                     "audio_frames=%d",
                     self._rewriter._offset, new_offset,
                     self._rewriter.audio_frame_count)

        self._rewriter.set_offset(new_offset, video_correction=video_correction)

    def _get_sink(self):
        """Return the write target: master stdin (fMP4 path) or ring buffer (raw TS)."""
        if self.master:
            return self.master.stdin
        return self.ring_buffer

    def _close_sink(self) -> None:
        """Close the write target after all segments are done."""
        if self.master:
            try:
                self.master.stdin.close()
            except OSError:
                pass
        else:
            self.ring_buffer.close()

    def _run_segment(self, segment, sink) -> None:
        """Run a single segment, piping its stdout through the TS rewriter to the sink."""
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
                    sink.write(rewritten)
                    try:
                        sf = self._save_file
                        if sf:
                            sf.write(rewritten)
                    except Exception:
                        pass

            # Discard any partial packet (incomplete 188-byte TS packets)
            self._rewriter.flush()

            segment.wait()

        except (BrokenPipeError, OSError):
            log.debug("Segment bridge: pipe broken")
            raise _PipelineShutdown

    def _close_save(self) -> None:
        """Close save file(s) — called after first pass or on shutdown."""
        if self._save_file:
            try:
                self._save_file.close()
            except Exception:
                pass
            self._save_file = None
        if self.master:
            self.master.close_save_file()

    def _start_monitor(self) -> None:
        if not self._verbose:
            return  # progress bar replaces monitor in non-verbose mode
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
        log.info("Waiting for buffer to fill (%d bytes min)...", self.ring_buffer.min_fill)
        ok = self.ring_buffer.wait_min_fill(timeout)
        if ok:
            log.info("Buffer ready")
        else:
            log.warning("Buffer fill timed out")
        return ok

    @property
    def serve_url(self) -> str:
        return self.server.url

    @property
    def client_disconnected(self) -> bool:
        """True if a client disconnect has been detected since last clear."""
        return self._disconnect_event.is_set()

    def clear_disconnect(self) -> None:
        """Reset the disconnect flag after handling a reconnect."""
        self._disconnect_event.clear()

    def gate_serving(self) -> None:
        """Block HTTP handler from serving buffer data (probe-only mode)."""
        self.server.gate()

    def ungate_serving(self) -> None:
        """Allow HTTP handler to serve buffer data."""
        self.server.ungate()

    def wait_done(self, timeout: float | None = None) -> bool:
        """Block until shutdown is requested. Returns True if shutdown occurred."""
        result = self._shutdown_event.wait(timeout=timeout)
        if result:
            log.debug("Pipeline done")
        return result

    def shutdown(self) -> None:
        """Kill all subprocesses and stop the server."""
        log.info("Shutting down pipeline")
        self._close_save()
        self.now_playing = None
        self.segment_start_time = None
        self.current_duration = None
        self._shutdown_event.set()
        self._skip_event.set()  # unblock any segment reads
        if self._current_segment:
            self._current_segment.kill()
        if self.master:
            self.master.kill()
        self.ring_buffer.close()
        self.server.stop()


class _PipelineShutdown(Exception):
    """Internal signal for pipeline shutdown."""
