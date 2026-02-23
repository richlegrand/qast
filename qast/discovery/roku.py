"""Roku device discovery via SSDP (roku:ecp)."""

from __future__ import annotations

import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..log import get_logger
from .types import Device

log = get_logger("discovery.roku")


def discover_roku(timeout: int = 5) -> list[Device]:
    """SSDP M-SEARCH for Roku devices."""
    mx = min(timeout, 5)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        "ST: roku:ecp\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(2)

    for _ in range(3):
        sock.sendto(msg, ("239.255.255.250", 1900))

    locations: set[str] = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="replace")
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if not loc.endswith("/"):
                        loc += "/"
                    locations.add(loc)
        except socket.timeout:
            if time.time() < deadline:
                sock.sendto(msg, ("239.255.255.250", 1900))
                continue
            break
    sock.close()

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
