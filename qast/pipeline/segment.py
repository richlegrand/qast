"""Segment ffmpeg — transcodes a source to MPEG-TS on stdout."""

from __future__ import annotations

import re
import subprocess
import threading

from .. import config
from ..log import get_logger

log = get_logger("pipeline.segment")

# Regex to extract frame count from ffmpeg's progress output.
# Matches lines like: "frame= 2179 ..."
_FRAME_RE = re.compile(r"frame=\s*(\d+)")

_VIDEO_FPS = int(config.VIDEO_FPS)


class SegmentFFmpeg:
    """Wraps an ffmpeg process that transcodes source URL(s) to MPEG-TS.

    stdout produces MPEG-TS data. stderr is drained by a background thread.
    After wait(), `actual_duration` contains the encoded duration computed
    from the frame count.
    """

    def __init__(
        self,
        source_urls: list[str],
        is_live: bool = False,
    ) -> None:
        self.source_urls = source_urls
        self.is_live = is_live
        self.proc: subprocess.Popen | None = None
        self.actual_duration: float | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]

        if self.is_live:
            cmd += ["-re"]

        for url in self.source_urls:
            cmd += ["-i", url]

        cmd += [
            "-c:v", config.VIDEO_CODEC,
            "-preset", config.VIDEO_PRESET,
            "-b:v", config.VIDEO_BITRATE,
            "-s", config.VIDEO_SIZE,
            "-r", config.VIDEO_FPS,
            "-g", config.VIDEO_GOP,
            "-c:a", config.AUDIO_CODEC,
            "-ar", config.AUDIO_SAMPLE_RATE,
            "-ac", config.AUDIO_CHANNELS,
            "-b:a", config.AUDIO_BITRATE,
        ]

        cmd += [
            "-shortest",
            "-muxdelay", "0", "-muxpreload", "0",
            "-flush_packets", "1",
            "-f", "mpegts", "pipe:1",
        ]
        return cmd

    def start(self) -> None:
        cmd = self._build_cmd()
        log.info("Segment ffmpeg: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            for line in self.proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
        except Exception:
            pass

    def _parse_duration(self) -> float | None:
        """Compute segment duration from the video frame count.

        Uses video_frames/fps only.  The MPEG-TS muxer shifts video PTS
        forward by one AAC frame (~23 ms), so using video frame count keeps
        video PTS continuous across segment boundaries.  Audio may overlap
        by up to one AAC frame at boundaries — non-accumulating and masked
        by placeholder silence between segments.

        ffmpeg -stats uses \\r to overwrite progress in-place, so all
        updates may appear as one long "line".  We find ALL frame=
        matches and take the last.
        """
        for line in reversed(self._stderr_lines):
            matches = _FRAME_RE.findall(line)
            if matches:
                video_frames = int(matches[-1])
                return video_frames / _VIDEO_FPS
        return None

    def wait(self) -> int:
        """Wait for the process to finish. Returns exit code."""
        if self.proc:
            rc = self.proc.wait()
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)
            self.actual_duration = self._parse_duration()
            if self.actual_duration:
                log.info("Segment duration: %.10fs", self.actual_duration)
            if rc != 0 and self._stderr_lines:
                log.warning("Segment ffmpeg exited %d:\n%s", rc, "\n".join(self._stderr_lines[-10:]))
            elif self._stderr_lines:
                log.debug("Segment ffmpeg stderr:\n%s", "\n".join(self._stderr_lines[-5:]))
            return rc
        return -1

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            log.debug("Segment ffmpeg killed")

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else None
