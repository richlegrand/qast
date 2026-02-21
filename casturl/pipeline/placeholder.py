"""Placeholder segment — generates text-on-screen MPEG-TS from lavfi."""

from __future__ import annotations

import subprocess
import threading

from .. import config
from ..log import get_logger

log = get_logger("pipeline.placeholder")


class PlaceholderSegment:
    """Generates MPEG-TS from ffmpeg lavfi virtual inputs.

    Produces a solid color background with centered text. No network, no disk.
    Same interface as SegmentFFmpeg: start(), wait(), kill(), stdout.

    If drawtext is unavailable (no libfreetype), falls back to plain color.
    """

    def __init__(
        self,
        text: str,
        duration: float = 10.0,
    ) -> None:
        self.text = text
        self.duration = duration
        self.proc: subprocess.Popen | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _build_cmd(self, use_drawtext: bool = True) -> list[str]:
        video_input = (
            f"color=c=0x1a1a2e:s={config.VIDEO_SIZE}"
            f":r={config.VIDEO_FPS}:d={self.duration}"
        )
        audio_input = (
            f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
            f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}"
        )

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "lavfi", "-i", video_input,
            "-f", "lavfi", "-i", audio_input,
        ]

        if use_drawtext:
            # Escape for ffmpeg drawtext filter syntax.
            # The text is delimited by single quotes in the filter string.
            # Colons and backslashes are special in filter syntax.
            # Single quotes cannot be escaped inside the text — strip them.
            safe_text = self.text
            safe_text = safe_text.replace("\\", "\\\\")
            safe_text = safe_text.replace(":", "\\:")
            safe_text = safe_text.replace("'", "\u2019")  # curly apostrophe
            safe_text = safe_text.replace(";", "\\;")
            safe_text = safe_text.replace("%", "%%")
            drawtext = (
                f"drawtext=text='{safe_text}'"
                ":fontsize=48:fontcolor=white"
                ":x=(w-text_w)/2:y=(h-text_h)/2"
            )
            cmd += ["-vf", drawtext]

        cmd += [
            "-c:v", config.VIDEO_CODEC,
            "-preset", "ultrafast",
            "-tune", "stillimage",
            "-c:a", config.AUDIO_CODEC,
            "-ar", config.AUDIO_SAMPLE_RATE,
            "-ac", config.AUDIO_CHANNELS,
            "-b:a", config.AUDIO_BITRATE,
            "-shortest",
        ]

        cmd += ["-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"]
        return cmd

    def _start_stderr_drain(self) -> None:
        self._stderr_lines = []
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

    def start(self) -> None:
        # Try with drawtext first
        cmd = self._build_cmd(use_drawtext=True)
        log.info("Placeholder: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Quick check: if ffmpeg exits immediately, drawtext probably failed
        try:
            self.proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            # Still running — good, start stderr drain
            self._start_stderr_drain()
            return

        # It exited fast — check stderr and retry without drawtext
        rc = self.proc.returncode
        if rc != 0:
            err = ""
            if self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
            log.warning("Placeholder with drawtext failed (rc=%d): %s", rc, err)
            log.info("Retrying placeholder without drawtext")

            cmd = self._build_cmd(use_drawtext=False)
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._start_stderr_drain()
        else:
            # Exited 0 very fast — that's fine (very short duration?)
            pass

    def wait(self) -> int:
        if self.proc:
            rc = self.proc.wait()
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)
            if rc != 0 and self._stderr_lines:
                log.warning("Placeholder exited %d:\n%s", rc, "\n".join(self._stderr_lines[-10:]))
            return rc
        return -1

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            log.debug("Placeholder killed")

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else None
