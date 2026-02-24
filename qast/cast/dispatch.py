"""Cast dispatch — routes to protocol-specific implementations."""

from __future__ import annotations

from ..discovery.types import Device
from ..log import get_logger
from . import chromecast
from . import dlna
from . import roku

log = get_logger("cast.dispatch")


def cast_media(device: Device, url: str, video_format: str = "mp4") -> None:
    """Cast a media URL to the given device."""
    if device.protocol == "cast":
        chromecast.play(device, url)
    elif device.protocol == "dlna":
        dlna.play(device, url, video_format=video_format)
    elif device.protocol == "roku":
        roku.play(device, url, video_format=video_format)
    else:
        raise ValueError(f"Unsupported protocol: {device.protocol}")


def stop_device(device: Device) -> None:
    """Best-effort stop for any protocol."""
    try:
        if device.protocol == "cast":
            chromecast.stop(device)
        elif device.protocol == "dlna":
            dlna.stop(device)
        elif device.protocol == "roku":
            roku.stop(device)
        else:
            log.warning("No stop implementation for protocol: %s", device.protocol)
    except Exception:
        log.debug("stop_device failed", exc_info=True)
