"""Roku device discovery via SSDP (roku:ecp)."""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..log import get_logger
from .ssdp import ssdp_search
from .types import Device

log = get_logger("discovery.roku")


def _normalize_roku_url(loc: str) -> str:
    """Ensure Roku location URLs end with /."""
    if not loc.endswith("/"):
        loc += "/"
    return loc


def discover_roku(timeout: int = 5) -> list[Device]:
    """SSDP M-SEARCH for Roku devices."""
    locations = ssdp_search(
        "roku:ecp", timeout=timeout,
        normalize_location=_normalize_roku_url,
    )

    devices: list[Device] = []
    for loc in locations:
        dev = _parse_device(loc)
        if dev:
            devices.append(dev)
    log.info("Found %d Roku device(s)", len(devices))
    return devices


def _xml_text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else ""


def _parse_device(base_url: str) -> Device | None:
    """Fetch Roku device-info and build a Device."""
    try:
        req = urllib.request.Request(
            f"{base_url}query/device-info",
            headers={"User-Agent": "qast/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_bytes = resp.read()
    except Exception:
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    name = _xml_text(root, "friendly-device-name") or _xml_text(root, "user-device-name")
    model = _xml_text(root, "model-name") or _xml_text(root, "friendly-model-name")

    parsed = urlparse(base_url)
    return Device(
        name=name or parsed.hostname or "Roku",
        model=model or "Roku",
        protocol="roku",
        host=parsed.hostname or "",
        port=parsed.port or 8060,
    )
