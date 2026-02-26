"""Placeholder segment — generates text-on-screen MPEG-TS from lavfi."""

from __future__ import annotations

import subprocess
import textwrap

from .. import config
from ..log import get_logger
from .segment import SegmentBase

log = get_logger("pipeline.placeholder")


class PlaceholderSegment(SegmentBase):
    """Generates MPEG-TS from ffmpeg lavfi virtual inputs.

    Produces a solid color background with centered text. No network, no disk.
    Same interface as SegmentFFmpeg: start(), wait(), kill(), stdout.

    If drawtext is unavailable (no libfreetype), falls back to plain color.
    """

    _log_name = "Placeholder"

    def __init__(
        self,
        text: str,
        duration: float = 10.0,
    ) -> None:
        super().__init__()
        self.text = text
        self.duration = duration

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
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "lavfi", "-i", video_input,
            "-f", "lavfi", "-i", audio_input,
        ]

        if use_drawtext:
            # Escape for ffmpeg drawtext filter syntax.
            # The text is delimited by single quotes in the filter string.
            # Colons and backslashes are special in filter syntax.
            # Single quotes cannot be escaped inside the text — strip them.
            safe_text = textwrap.fill(self.text, width=50)
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

        cmd += config.ffmpeg_output_args(
            preset="ultrafast", tune="stillimage", flush_packets=False,
            scale=False,
        )
        return cmd

    def start(self) -> None:
        # Try with drawtext first
        cmd = self._build_cmd(use_drawtext=True)
        log.info("%s: %s", self._log_name, " ".join(cmd))
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
            self._stderr_thread = __import__("threading").Thread(
                target=self._drain_stderr, daemon=True,
            )
            self._stderr_thread.start()
            return

        # It exited fast — check stderr and retry without drawtext
        rc = self.proc.returncode
        if rc != 0:
            err = ""
            if self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
            log.warning("%s with drawtext failed (rc=%d): %s", self._log_name, rc, err)
            log.info("Retrying %s without drawtext", self._log_name.lower())

            cmd = self._build_cmd(use_drawtext=False)
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._stderr_thread = __import__("threading").Thread(
                target=self._drain_stderr, daemon=True,
            )
            self._stderr_thread.start()
