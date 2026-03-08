"""Browser capture segment — renders a URL via Xvfb + x11grab.

Launches Chromium on a virtual X display (Xvfb) and captures the
framebuffer via ffmpeg x11grab at the full pipeline frame rate.

Browser setup (Xvfb, Chromium, navigation) can be separated from
capture via ``prewarm()`` so that the heavy page-load happens in the
background while a previous segment plays.  x11grab must NOT start
until the pipeline is ready to consume stdout — blocking the stdout
pipe causes x11grab frame drops (dup/drop).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from .. import config
from ..log import get_logger
from .segment import SegmentBase

log = get_logger("pipeline.browser")

_CAPTURE_W = 1920
_CAPTURE_H = 1080


class BrowserSegment(SegmentBase):
    """Captures a webpage via Chromium on a virtual X display.

    Starts Xvfb, launches Chromium via Playwright, enters fullscreen
    via CDP (hides all browser chrome), then captures via ffmpeg x11grab.

    Call ``prewarm()`` to load the page in the background, then
    ``start()`` when ready to capture.  Or just call ``start()``
    which does both.

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
        self._display: str | None = None
        self._xvfb_proc: subprocess.Popen | None = None
        self._pw = None       # Playwright instance
        self._browser = None  # Playwright Browser

    @staticmethod
    def _find_free_display() -> int:
        """Find a free X display number."""
        for n in range(99, 200):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
                return n
        raise RuntimeError("No free X display found")

    def _build_cmd(self) -> list[str]:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "x11grab",
            "-framerate", config.VIDEO_FPS,
            "-video_size", f"{_CAPTURE_W}x{_CAPTURE_H}",
            "-draw_mouse", "0",
            "-i", f"{self._display}.0",
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

    def prewarm(self) -> None:
        """Load the page in the background (Xvfb + Chromium + navigate).

        Does NOT start x11grab capture — call ``start()`` for that.
        Keeping ffmpeg off until the pipeline is ready to consume stdout
        prevents the stdout pipe from filling up, which causes x11grab
        frame drops (dup/drop).
        """
        if self._xvfb_proc is not None:
            return  # already prewarmed

        # 1. Start Xvfb on a free display
        display_num = self._find_free_display()
        self._display = f":{display_num}"
        self._xvfb_proc = subprocess.Popen(
            ["Xvfb", self._display, "-screen", "0",
             f"{_CAPTURE_W}x{_CAPTURE_H}x24",
             "+extension", "GLX", "+render", "-noreset"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 3.0
        while not os.path.exists(f"/tmp/.X11-unix/X{display_num}"):
            if time.monotonic() > deadline:
                raise RuntimeError(f"Xvfb failed to start on {self._display}")
            time.sleep(0.05)
        log.info("Xvfb started on %s (%dx%d)", self._display,
                 _CAPTURE_W, _CAPTURE_H)

        # 2. Launch browser on virtual display
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        browser_env = {**os.environ, "DISPLAY": self._display}
        self._browser = self._pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--no-first-run", "--no-default-browser-check",
                f"--window-size={_CAPTURE_W},{_CAPTURE_H}",
                "--window-position=0,0",
            ],
            env=browser_env,
        )
        page = self._browser.new_page(no_viewport=True)

        # 3. Navigate
        log.info("Navigating to %s on %s", self.url, self._display)
        page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        # 4. Wait for initial paint to finish
        try:
            page.evaluate(
                "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
        except Exception:
            pass

        # 5. Enter fullscreen via CDP
        #    Fullscreen hides tabs/address bar even without a window manager.
        try:
            cdp = page.context.new_cdp_session(page)
            window = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"windowState": "fullscreen"},
            })
            cdp.detach()
        except Exception:
            log.debug("CDP fullscreen failed", exc_info=True)

        # Brief settle after fullscreen transition
        time.sleep(0.3)
        log.info("Page ready on %s", self._display)

    def start(self) -> None:
        """Start x11grab capture. Calls prewarm() first if needed."""
        self.prewarm()

        cmd = self._build_cmd()
        log.info("%s: %s", self._log_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True,
        )
        self._stderr_thread.start()

    def _cleanup_browser(self) -> None:
        """Tear down browser and virtual display."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._xvfb_proc:
            self._xvfb_proc.terminate()
            try:
                self._xvfb_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._xvfb_proc.kill()
                self._xvfb_proc.wait()
            self._xvfb_proc = None
            log.debug("Xvfb stopped on %s", self._display)

    def wait(self) -> int:
        rc = super().wait()
        self._cleanup_browser()
        return rc

    def kill(self) -> None:
        super().kill()
        self._cleanup_browser()
