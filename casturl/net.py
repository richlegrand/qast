"""Network utilities."""

import socket

from .log import get_logger

log = get_logger("net")


def get_local_ip() -> str:
    """Get the local IP address that other devices on the LAN can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        log.debug("Local IP: %s", ip)
        return ip
    finally:
        s.close()
