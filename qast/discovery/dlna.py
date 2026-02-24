"""DLNA device discovery via SSDP (UPnP MediaRenderer)."""

from __future__ import annotations

import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..log import get_logger
from .types import Device

log = get_logger("discovery.dlna")

_NS = "urn:schemas-upnp-org:device-1-0"


def discover_dlna(timeout: int = 5) -> list[Device]:
    """SSDP M-SEARCH for UPnP MediaRenderer devices."""
    target = "urn:schemas-upnp-org:device:MediaRenderer:1"
    mx = min(timeout, 5)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {target}\r\n"
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
                    locations.add(line.split(":", 1)[1].strip())
        except socket.timeout:
            if time.time() < deadline:
                sock.sendto(msg, ("239.255.255.250", 1900))
                continue
            break
    sock.close()

    devices: list[Device] = []
    for loc in locations:
        dev = _parse_description(loc)
        if dev:
            devices.append(dev)
    log.info("Found %d DLNA device(s)", len(devices))
    return devices


def _xml_text(parent: ET.Element, tag: str) -> str:
    """Safe XML text extraction with UPnP namespace."""
    el = parent.find(f"{{{_NS}}}{tag}")
    return el.text.strip() if el is not None and el.text else ""


def _parse_description(location_url: str) -> Device | None:
    """Fetch a UPnP device description XML and extract useful fields."""
    try:
        req = urllib.request.Request(location_url, headers={"User-Agent": "casturl/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_bytes = resp.read()
    except Exception:
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    device_el = root.find(f"{{{_NS}}}device")
    if device_el is None:
        return None

    friendly_name = _xml_text(device_el, "friendlyName")
    model = _xml_text(device_el, "modelName") or _xml_text(device_el, "manufacturer")

    # Find AVTransport service controlURL
    control_url = None
    for service in device_el.iter(f"{{{_NS}}}service"):
        stype = _xml_text(service, "serviceType")
        if "AVTransport" in stype:
            ctrl = _xml_text(service, "controlURL")
            if ctrl:
                parsed = urlparse(location_url)
                base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"
                control_url = base + ctrl if ctrl.startswith("/") else base + "/" + ctrl
            break

    if not control_url:
        return None

    parsed_loc = urlparse(location_url)
    return Device(
        name=friendly_name or parsed_loc.hostname or "DLNA Renderer",
        model=model or "UPnP Renderer",
        protocol="dlna",
        host=parsed_loc.hostname or "",
        port=parsed_loc.port or 80,
        control_url=control_url,
    )
