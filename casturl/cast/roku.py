"""Roku ECP casting — play media via Play on Roku or Media Assistant.

Channel 15985 ("Play on Roku") is a hidden system channel that accepts
video URLs via /input/15985 (NOT /launch). Roku OS 11.5+ may block it.

Channel 782875 ("Media Assistant") is a free community replacement from
the Roku Channel Store that accepts the same parameters via /launch.

Channel 2213 ("Roku Media Player") is a file browser — NOT useful for
URL-based casting.
"""

from __future__ import annotations

import time
import urllib.request
from urllib.parse import quote

from ..discovery.types import Device
from ..log import get_logger

log = get_logger("cast.roku")


def play(device: Device, url: str) -> None:
    """Cast a stream URL to a Roku device.

    Tries, in order:
      1. /input/15985  — Play on Roku (hidden system casting API)
      2. /launch/782875 — Media Assistant (community replacement)
    """
    base = f"http://{device.host}:{device.port}"
    encoded_url = quote(url, safe="")
    params = f"?t=v&u={encoded_url}&videoName=casturl&videoFormat=mp4"

    # 1) Play on Roku — must use /input, not /launch
    if _post(f"{base}/input/15985{params}", "Play on Roku (/input/15985)"):
        return

    # 2) Media Assistant — /launch auto-plays with parameters
    if _post(f"{base}/launch/782875{params}", "Media Assistant (/launch/782875)"):
        # Give the channel a moment to start before it fetches the URL
        time.sleep(2)
        return

    raise RuntimeError(
        f"Could not cast to {device.name}.\n"
        "  Play on Roku (built-in) may be disabled on your firmware.\n"
        "  Fix: Install the free 'Media Assistant' app from the Roku Channel Store,\n"
        "  then try again. Also ensure Settings > System > Advanced System Settings\n"
        "  > Control by mobile apps is set to 'Enabled'."
    )


def stop(device: Device) -> None:
    """Send Stop keypress to Roku."""
    base = f"http://{device.host}:{device.port}"
    _post(f"{base}/keypress/Stop", "Stop keypress")


def _post(url: str, label: str) -> bool:
    """POST to a Roku ECP endpoint. Returns True on HTTP 2xx."""
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=10)
        log.info("%s: OK", label)
        return True
    except urllib.error.HTTPError as e:
        log.debug("%s: HTTP %s", label, e.code)
    except Exception as e:
        log.debug("%s: %s", label, e)
    return False
