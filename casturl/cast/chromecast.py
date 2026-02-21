"""Chromecast casting via pychromecast."""

from __future__ import annotations

from ..config import CAST_CONNECT_TIMEOUT
from ..discovery.types import Device
from ..log import get_logger

log = get_logger("cast.chromecast")


def play(device: Device, url: str) -> None:
    """Cast a URL to a Chromecast device."""
    cc = device.cast_obj
    if cc is None:
        raise RuntimeError("Device has no cast_obj")

    log.info("Connecting to %s...", device.name)
    cc.wait()

    # Always LIVE — our stream is continuous (loops until stopped)
    mc = cc.media_controller
    mc.play_media(url, "video/mp4", stream_type="LIVE")
    mc.block_until_active(timeout=CAST_CONNECT_TIMEOUT)
    log.info("Now casting to %s", device.name)


def stop(device: Device) -> None:
    """Stop playback on a Chromecast."""
    cc = device.cast_obj
    if cc:
        try:
            cc.media_controller.stop()
        except Exception:
            log.debug("Failed to stop Chromecast", exc_info=True)
