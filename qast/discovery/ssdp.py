"""Shared SSDP M-SEARCH logic for device discovery."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

from ..log import get_logger

log = get_logger("discovery.ssdp")


def ssdp_search(
    service_type: str,
    timeout: int = 5,
    normalize_location: Callable[[str], str] | None = None,
) -> set[str]:
    """SSDP M-SEARCH — returns set of Location URLs.

    Args:
        service_type: The ST (search target) value, e.g. "roku:ecp".
        timeout: How long to listen for responses.
        normalize_location: Optional callable to normalize/fix Location URLs
                           before adding to the result set.
    """
    mx = min(timeout, 5)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {service_type}\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(2)

    for _ in range(3):
        sock.sendto(msg, ("239.255.255.250", 1900))

    locations: set[str] = set()
    deadline = time.time() + timeout
    last_response = 0.0
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="replace")
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if normalize_location:
                        loc = normalize_location(loc)
                    locations.add(loc)
                    last_response = time.time()
        except socket.timeout:
            # Stop early once we have responses and the network has gone quiet
            if locations and last_response and (time.time() - last_response) >= 3:
                break
            if time.time() < deadline:
                sock.sendto(msg, ("239.255.255.250", 1900))
                continue
            break
    sock.close()

    log.debug("SSDP %s: %d location(s)", service_type, len(locations))
    return locations
