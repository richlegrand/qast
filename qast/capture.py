"""Screen capture segment — captures desktop via ffmpeg x11grab."""

from __future__ import annotations

import os
import re
import subprocess

from . import config
from .log import get_logger
from .pipeline.segment import SegmentBase

log = get_logger("capture")


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


def _get_window_geometry(window_id: int) -> tuple[int, int]:
    """Get window dimensions via xdotool. Returns (width, height)."""
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
    return width, height


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

    width, height = _get_window_geometry(window_id)
    log.info("Selected window %d (%dx%d)", window_id, width, height)
    return window_id, width, height


def _find_window_by_title(title: str) -> tuple[int, int, int]:
    """Find a window by title via xdotool search. Returns (window_id, width, height)."""
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", title], timeout=5,
        ).decode().strip()
    except FileNotFoundError:
        raise SystemExit("xdotool is required for --window-title (apt install xdotool)")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"Window search timed out for: {title}")
    except subprocess.CalledProcessError:
        raise SystemExit(f"No window found matching: {title}")

    lines = out.splitlines()
    if not lines:
        raise SystemExit(f"No window found matching: {title}")

    # Pick the first mapped (visible) window — xdotool often returns hidden
    # helper windows first (depth 0, unmapped) that x11grab can't capture.
    window_id = None
    for line in lines:
        wid = int(line)
        try:
            info = subprocess.check_output(
                ["xwininfo", "-id", str(wid)], stderr=subprocess.DEVNULL, timeout=2,
            ).decode(errors="replace")
            if "IsViewable" in info:
                window_id = wid
                break
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            continue

    if window_id is None:
        # Fallback to first result
        window_id = int(lines[0])

    width, height = _get_window_geometry(window_id)
    log.info("Found window %d (%dx%d) matching '%s'", window_id, width, height, title)
    return window_id, width, height


class ScreenSegment(SegmentBase):
    """Captures the screen via ffmpeg x11grab, outputting MPEG-TS on stdout.

    Same interface as SegmentFFmpeg: start(), wait(), kill(), stdout.
    """

    _log_name = "Screen capture"

    def __init__(
        self,
        cursor: bool = True,
        window_id: int | None = None,
        window_size: tuple[int, int] | None = None,
        duration: float | None = None,
        fade_in: float = 0,
        fade_out: float = 0,
    ) -> None:
        super().__init__(fade_in=fade_in, fade_out=fade_out)
        self.cursor = cursor
        self.window_id = window_id
        self.window_size = window_size
        self.duration = duration

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
        ]
        if self.duration is not None:
            cmd += ["-t", str(self.duration)]
        cmd += config.ffmpeg_output_args(pix_fmt="yuv420p")
        cmd = self._append_fade_filters(cmd)
        return cmd


class WebcamSegment(SegmentBase):
    """Captures webcam via ffmpeg v4l2, outputting MPEG-TS on stdout.

    Same interface as ScreenSegment: start(), wait(), kill(), stdout.
    """

    _log_name = "Webcam capture"

    def __init__(
        self,
        device: str = "/dev/video0",
        duration: float | None = None,
        fade_in: float = 0,
        fade_out: float = 0,
    ) -> None:
        super().__init__(fade_in=fade_in, fade_out=fade_out)
        if not os.path.exists(device):
            raise FileNotFoundError(
                f"No webcam found at {device}. Is a camera connected?"
            )
        self.device = device
        self.duration = duration

    def _build_cmd(self) -> list[str]:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "v4l2",
            "-framerate", config.VIDEO_FPS,
            "-i", self.device,
            # Silent audio (pipeline expects audio+video)
            "-f", "lavfi", "-i",
            f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
            f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}",
        ]
        if self.duration is not None:
            cmd += ["-t", str(self.duration)]
        cmd += config.ffmpeg_output_args(pix_fmt="yuv420p")
        cmd = self._append_fade_filters(cmd)
        return cmd
