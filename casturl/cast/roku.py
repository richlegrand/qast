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


def play(device: Device, url: str, video_format: str = "mp4") -> None:
    """Cast a stream URL to a Roku device via Media Assistant.

    Media Assistant (channel 782875) is a free community app from the Roku
    Channel Store.  Play on Roku (channel 15985) was disabled in Roku OS 11.5+.
    """
    base = f"http://{device.host}:{device.port}"
    encoded_url = quote(url, safe="")
    params = f"?t=v&u={encoded_url}&videoName=casturl&videoFormat={video_format}"

    ok, status = _post(f"{base}/launch/782875{params}", "Media Assistant")
    if ok:
        time.sleep(2)
        return

    if status == 404:
        raise RuntimeError(
            f"Media Assistant is not installed on {device.name}.\n"
            "  Install it free from the Roku Channel Store:\n"
            "    Search for 'Media Assistant' on your Roku, or visit:\n"
            "    https://channelstore.roku.com/details/782875/media-assistant\n"
            "  Then ensure Settings > System > Advanced System Settings\n"
            "  > Control by mobile apps is set to 'Enabled'."
        )

    raise RuntimeError(f"Could not cast to {device.name} (HTTP {status}).")


def stop(device: Device) -> None:
    """Send Stop keypress to Roku."""
    base = f"http://{device.host}:{device.port}"
    _post(f"{base}/keypress/Home", "Home keypress")


def _post(url: str, label: str) -> tuple[bool, int]:
    """POST to a Roku ECP endpoint. Returns (success, http_status)."""
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        resp = urllib.request.urlopen(req, timeout=10)
        log.info("%s: OK", label)
        return True, resp.status
    except urllib.error.HTTPError as e:
        log.debug("%s: HTTP %s", label, e.code)
        return False, e.code
    except Exception as e:
        log.debug("%s: %s", label, e)
        return False, 0
