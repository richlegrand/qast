"""Chromecast mDNS discovery via pychromecast."""

from __future__ import annotations

from ..log import get_logger
from .types import Device

log = get_logger("discovery.cast")

_cast_browser = None


def discover_cast(timeout: int = 10) -> list[Device]:
    """Discover Chromecast devices. Returns [] if pychromecast not installed."""
    global _cast_browser
    try:
        import pychromecast
    except ImportError:
        log.debug("pychromecast not installed, skipping Cast discovery")
        return []

    log.debug("Starting Chromecast discovery (timeout=%ds)", timeout)
    chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    _cast_browser = browser

    devices = []
    for cc in chromecasts:
        devices.append(Device(
            name=cc.cast_info.friendly_name,
            model=cc.cast_info.model_name or "Chromecast",
            protocol="cast",
            host=str(cc.cast_info.host),
            port=cc.cast_info.port,
            cast_obj=cc,
        ))
    log.debug("Found %d Chromecast device(s)", len(devices))
    return devices


def stop_discovery() -> None:
    """Stop the mDNS browser if running."""
    global _cast_browser
    if _cast_browser:
        _cast_browser.stop_discovery()
        _cast_browser = None
