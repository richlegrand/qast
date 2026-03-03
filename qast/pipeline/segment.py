"""Segment base class and SegmentFFmpeg — transcodes sources to MPEG-TS."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

from .. import config
from ..log import get_logger

log = get_logger("pipeline.segment")

# Regex to extract frame count from ffmpeg's progress output.
# Matches lines like: "frame= 2179 ..."
_FRAME_RE = re.compile(r"frame=\s*(\d+)")

_VIDEO_FPS = int(config.VIDEO_FPS)
_RUST_FALLBACK_WARNED = False


def _find_rust_encoder() -> str | None:
    """Locate the Rust encoder binary, if available."""
    suffix = ".exe" if os.name == "nt" else ""
    env_bin = os.environ.get("QAST_ENCODER_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    local_release = os.path.join(repo_root, "rust", "qast-encoder", "target", "release", f"qast-encoder{suffix}")
    local_debug = os.path.join(repo_root, "rust", "qast-encoder", "target", "debug", f"qast-encoder{suffix}")
    for candidate in (local_release, local_debug):
        if os.path.isfile(candidate):
            return candidate

    path_bin = shutil.which("qast-encoder")
    if path_bin:
        return path_bin
    return None


class SegmentBase:
    """Common base for all ffmpeg-based segments.

    Subclasses must implement _build_cmd() and may override start().
    Provides shared stderr draining, duration parsing, wait/kill, and stdout.
    """

    _log_name: str = "Segment"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.actual_duration: float | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        raise NotImplementedError

    def start(self) -> None:
        cmd = self._build_cmd()
        log.info("%s: %s", self._log_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
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
                log.info("%s duration: %.10fs", self._log_name, self.actual_duration)
            if rc != 0 and self._stderr_lines:
                log.warning("%s exited %d:\n%s", self._log_name, rc, "\n".join(self._stderr_lines[-10:]))
            elif self._stderr_lines:
                log.debug("%s stderr:\n%s", self._log_name, "\n".join(self._stderr_lines[-5:]))
            return rc
        return -1

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            log.debug("%s killed", self._log_name)

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else None


class SegmentFFmpeg(SegmentBase):
    """Wraps an ffmpeg process that transcodes source URL(s) to MPEG-TS.

    stdout produces MPEG-TS data. stderr is drained by a background thread.
    After wait(), `actual_duration` contains the encoded duration computed
    from the frame count.
    """

    _log_name = "Segment ffmpeg"

    def __init__(
        self,
        source_urls: list[str],
        is_live: bool = False,
        duration: float | None = None,
        aspect: float = 1.0,
        has_audio: bool = True,
    ) -> None:
        super().__init__()
        self.source_urls = source_urls
        self.is_live = is_live
        self.duration = duration
        self.aspect = aspect
        self.has_audio = has_audio

    def start(self) -> None:
        if "pipe:0" in self.source_urls:
            # Must inherit parent's stdin so ffmpeg can read piped data.
            # -nostdin in the command prevents ffmpeg's interactive reader
            # from competing with the demuxer for stdin bytes.
            cmd = self._build_cmd()
            log.info("%s: %s", self._log_name, " ".join(cmd))
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True,
            )
            self._stderr_thread.start()
        else:
            super().start()

    def _build_cmd(self) -> list[str]:
        encoder = _find_rust_encoder()
        if encoder:
            cmd = [encoder]
            for url in self.source_urls:
                cmd += ["--source", url]
            if self.is_live:
                cmd += ["--live"]
            if self.duration is not None:
                cmd += ["--duration", str(self.duration)]
            cmd += ["--aspect", str(self.aspect)]
            if not self.has_audio:
                cmd += ["--no-audio"]
            return cmd

        global _RUST_FALLBACK_WARNED
        if not _RUST_FALLBACK_WARNED:
            log.warning(
                "Rust encoder not found, falling back to ffmpeg subprocess path. "
                "Build it with: cargo build --manifest-path rust/qast-encoder/Cargo.toml --release"
            )
            _RUST_FALLBACK_WARNED = True

        return self._build_ffmpeg_cmd()

    def _build_ffmpeg_cmd(self) -> list[str]:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "warning", "-stats"]

        if self.is_live:
            cmd += ["-re"]

        for url in self.source_urls:
            cmd += ["-i", url]

        if not self.has_audio:
            cmd += [
                "-f", "lavfi", "-i",
                f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
                f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}",
            ]

        if self.duration is not None:
            cmd += ["-t", str(self.duration)]

        cmd += config.ffmpeg_output_args(aspect=self.aspect)
        return cmd
