"""Segment ffmpeg — transcodes a source to MPEG-TS on stdout."""

from __future__ import annotations

import re
import subprocess
import threading

from .. import config
from ..log import get_logger

log = get_logger("pipeline.segment")

# Regex to extract the final encoding time from ffmpeg's progress output.
# Matches lines like: "frame= 2179 ... time=00:01:12.63 ..."
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


class SegmentFFmpeg:
    """Wraps an ffmpeg process that transcodes source URL(s) to MPEG-TS.

    stdout produces MPEG-TS data. stderr is drained by a background thread.
    After wait(), `actual_duration` contains the real encoded duration parsed
    from ffmpeg's progress output (or None if unavailable).
    """

    def __init__(
        self,
        source_urls: list[str],
        ts_offset: float = 0.0,
        is_live: bool = False,
    ) -> None:
        self.source_urls = source_urls
        self.ts_offset = ts_offset
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

        if self.ts_offset > 0:
            cmd += ["-output_ts_offset", str(self.ts_offset)]

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
        """Parse the actual encoded duration from ffmpeg's last progress line.

        ffmpeg -stats uses \\r to overwrite progress in-place, so all updates
        may appear as one long "line". We find ALL time= matches and take the last.
        """
        for line in reversed(self._stderr_lines):
            matches = _TIME_RE.findall(line)
            if matches:
                h, mins, secs = int(matches[-1][0]), int(matches[-1][1]), float(matches[-1][2])
                return h * 3600.0 + mins * 60.0 + secs
        return None

    def wait(self) -> int:
        """Wait for the process to finish. Returns exit code."""
        if self.proc:
            rc = self.proc.wait()
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)
            self.actual_duration = self._parse_duration()
            if self.actual_duration:
                log.info("Segment actual duration: %.3fs", self.actual_duration)
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
