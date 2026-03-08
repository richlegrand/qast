"""Browser capture segment — renders a URL via headless Playwright.

Uses CDP Page.captureScreenshot for lower-overhead frame capture than
Playwright's page.screenshot().
"""

from __future__ import annotations

import base64
import subprocess
import threading
import time

from .. import config
from ..log import get_logger
from .segment import SegmentBase

log = get_logger("pipeline.browser")

_CAPTURE_W = 1920
_CAPTURE_H = 1080
_CAPTURE_FPS = 5


class BrowserSegment(SegmentBase):
    """Captures a webpage via headless Chromium, outputting MPEG-TS on stdout.

    Launches ffmpeg expecting JPEG frames on stdin (image2pipe), and a
    background thread that captures frames via CDP Page.captureScreenshot.
    ffmpeg upsamples to the pipeline's VIDEO_FPS via frame duplication.

    Same interface as SegmentBase: start(), wait(), kill(), stdout.
    """

    _log_name = "Browser"

    def __init__(self, url: str, duration: float | None = None,
                 fade_in: float = 0, fade_out: float = 0) -> None:
        super().__init__(fade_in=fade_in, fade_out=fade_out)
        if "://" not in url:
            url = "https://" + url
        self.url = url
        self.duration = duration
        self._capture_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            # JPEG frames on stdin — probesize/analyzeduration minimized so
            # ffmpeg starts encoding after the very first frame arrives.
            "-probesize", "32",
            "-analyzeduration", "0",
            "-f", "image2pipe", "-c:v", "mjpeg",
            "-framerate", str(_CAPTURE_FPS),
            "-i", "pipe:0",
            # Silent audio
            "-f", "lavfi", "-i",
            f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
            f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}",
        ]
        if self.duration is not None:
            cmd += ["-t", str(self.duration)]
        cmd += config.ffmpeg_output_args(pix_fmt="yuv420p")
        cmd = self._append_fade_filters(cmd)
        return cmd

    def start(self) -> None:
        cmd = self._build_cmd()
        log.info("%s: %s", self._log_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def _capture_loop(self) -> None:
        """Capture frames via CDP and pipe JPEG bytes to ffmpeg stdin."""
        assert self.proc and self.proc.stdin
        interval = 1.0 / _CAPTURE_FPS
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": _CAPTURE_W, "height": _CAPTURE_H})

                log.info("Navigating to %s", self.url)
                page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                log.info("Page loaded, capturing at %d fps (%dx%d)",
                         _CAPTURE_FPS, _CAPTURE_W, _CAPTURE_H)

                cdp = page.context.new_cdp_session(page)

                while self.proc.poll() is None:
                    t0 = time.monotonic()
                    try:
                        result = cdp.send("Page.captureScreenshot", {
                            "format": "jpeg",
                            "quality": 80,
                        })
                        jpeg = base64.b64decode(result["data"])
                        self.proc.stdin.write(jpeg)
                    except (BrokenPipeError, OSError):
                        break
                    elapsed = time.monotonic() - t0
                    sleep_time = interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)

                cdp.detach()
                browser.close()
        except Exception:
            log.exception("Browser capture loop error")
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def wait(self) -> int:
        rc = super().wait()
        if self._capture_thread:
            self._capture_thread.join(timeout=5)
        return rc
