"""Screen capture segment — captures desktop via ffmpeg x11grab."""

from __future__ import annotations

import re
import subprocess
import threading

from . import config
from .log import get_logger

log = get_logger("capture")

_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_VIDEO_FPS = int(config.VIDEO_FPS)


def _get_primary_monitor() -> tuple[int, int, int, int]:
    """Detect primary monitor geometry via xrandr.

    Returns (width, height, x_offset, y_offset).
    Falls back to 1920x1080+0+0.
    """
    try:
        out = subprocess.check_output(
            ["xrandr", "--query"], stderr=subprocess.DEVNULL, timeout=5,
        ).decode(errors="replace")
        for line in out.splitlines():
            # Match: "HDMI-0 connected primary 1920x1080+0+1080 ..."
            if " connected primary " in line:
                match = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
                if match:
                    return (
                        int(match.group(1)), int(match.group(2)),
                        int(match.group(3)), int(match.group(4)),
                    )
        # No primary found — try first connected monitor
        for line in out.splitlines():
            if " connected " in line:
                match = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
                if match:
                    return (
                        int(match.group(1)), int(match.group(2)),
                        int(match.group(3)), int(match.group(4)),
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    return 1920, 1080, 0, 0


def _select_window() -> tuple[int, int, int]:
    """Prompt user to click a window. Returns (window_id, width, height)."""
    print("Click a window to capture...")
    try:
        wid_str = subprocess.check_output(
            ["xdotool", "selectwindow"], timeout=30,
        ).decode().strip()
        window_id = int(wid_str)
    except FileNotFoundError:
        raise SystemExit("xdotool is required for --window (apt install xdotool)")
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as e:
        raise SystemExit(f"Window selection failed: {e}")

    try:
        geo = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", str(window_id)], timeout=5,
        ).decode()
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        raise SystemExit(f"Failed to get window geometry: {e}")

    width = height = 0
    for line in geo.splitlines():
        if line.startswith("WIDTH="):
            width = int(line.split("=", 1)[1])
        elif line.startswith("HEIGHT="):
            height = int(line.split("=", 1)[1])
    if not width or not height:
        raise SystemExit(f"Could not parse window geometry: {geo}")

    log.info("Selected window %d (%dx%d)", window_id, width, height)
    return window_id, width, height


class ScreenSegment:
    """Captures the screen via ffmpeg x11grab, outputting MPEG-TS on stdout.

    Same interface as SegmentFFmpeg: start(), wait(), kill(), stdout.
    """

    def __init__(
        self,
        cursor: bool = True,
        window_id: int | None = None,
        window_size: tuple[int, int] | None = None,
    ) -> None:
        self.cursor = cursor
        self.window_id = window_id
        self.window_size = window_size
        self.proc: subprocess.Popen | None = None
        self.actual_duration: float | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        draw_mouse = "1" if self.cursor else "0"

        if self.window_id is not None and self.window_size is not None:
            win_w, win_h = self.window_size
            grab_size = f"{win_w}x{win_h}"
            log.info("Capturing window %d: %s", self.window_id, grab_size)
            x11_input = [
                "-f", "x11grab",
                "-framerate", config.VIDEO_FPS,
                "-window_id", str(self.window_id),
                "-video_size", grab_size,
                "-draw_mouse", draw_mouse,
                "-i", ":0.0",
            ]
        else:
            width, height, x_off, y_off = _get_primary_monitor()
            grab_size = f"{width}x{height}"
            log.info("Capturing monitor: %dx%d+%d+%d", width, height, x_off, y_off)
            x11_input = [
                "-f", "x11grab",
                "-framerate", config.VIDEO_FPS,
                "-video_size", grab_size,
                "-draw_mouse", draw_mouse,
                "-i", f":0.0+{x_off},{y_off}",
            ]

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            *x11_input,
            # Silent audio (pipeline expects audio+video)
            "-f", "lavfi", "-i",
            f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
            f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}",
            # Video encoding (x11grab is BGR0, must convert to yuv420p for device compatibility)
            "-pix_fmt", "yuv420p",
            "-c:v", config.VIDEO_CODEC,
            "-preset", config.VIDEO_PRESET,
            "-b:v", config.VIDEO_BITRATE,
            "-s", config.VIDEO_SIZE,
            "-r", config.VIDEO_FPS,
            "-g", config.VIDEO_GOP,
            # Audio encoding
            "-c:a", config.AUDIO_CODEC,
            "-ar", config.AUDIO_SAMPLE_RATE,
            "-ac", config.AUDIO_CHANNELS,
            "-b:a", config.AUDIO_BITRATE,
            # Mux settings
            "-shortest",
            "-muxdelay", "0", "-muxpreload", "0",
            "-flush_packets", "1",
            "-f", "mpegts", "pipe:1",
        ]
        return cmd

    def start(self) -> None:
        cmd = self._build_cmd()
        log.info("Screen capture: %s", " ".join(cmd))
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
        for line in reversed(self._stderr_lines):
            matches = _FRAME_RE.findall(line)
            if matches:
                video_frames = int(matches[-1])
                return video_frames / _VIDEO_FPS
        return None

    def wait(self) -> int:
        if self.proc:
            rc = self.proc.wait()
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)
            self.actual_duration = self._parse_duration()
            if self.actual_duration:
                log.info("Screen capture duration: %.1fs", self.actual_duration)
            if rc != 0 and self._stderr_lines:
                log.warning("Screen capture exited %d:\n%s", rc, "\n".join(self._stderr_lines[-10:]))
            return rc
        return -1

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            log.debug("Screen capture killed")

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else None
