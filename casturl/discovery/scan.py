"""Parallel discovery orchestration and device selection UI."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

from ..config import DISCOVERY_TIMEOUT
from ..log import get_logger
from .cast import discover_cast
from .roku import discover_roku
from .types import Device

log = get_logger("discovery.scan")

PROTO_TAG = {"cast": "Cast", "dlna": "DLNA", "roku": "Roku"}


def discover_all(timeout: int = DISCOVERY_TIMEOUT) -> list[Device]:
    """Run all discovery protocols in parallel, merge and deduplicate."""
    results: list[Device] = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(discover_cast, timeout),
            pool.submit(discover_roku, timeout),
        ]
        for fut in futures:
            try:
                results.extend(fut.result())
            except Exception:
                log.debug("Discovery future failed", exc_info=True)

    # Deduplicate by (host, protocol)
    seen: set[tuple[str, str]] = set()
    unique: list[Device] = []
    for dev in results:
        key = (dev.host, dev.protocol)
        if key not in seen:
            seen.add(key)
            unique.append(dev)

    unique.sort(key=lambda d: d.name.lower())
    log.info("Discovered %d device(s)", len(unique))
    return unique


def select_device(devices: list[Device]) -> Device:
    """Display devices and let the user pick one."""
    if not devices:
        try:
            import pychromecast  # noqa: F401
            has_pycc = True
        except ImportError:
            has_pycc = False

        msg = "No devices found."
        if not has_pycc:
            msg += " (pychromecast not installed — Chromecast discovery skipped)"
        print(msg)
        sys.exit(1)

    print(f"\nFound {len(devices)} device(s):\n")
    for i, dev in enumerate(devices):
        tag = PROTO_TAG.get(dev.protocol, dev.protocol)
        print(f"  [{i}] {dev.name} ({dev.model}) [{tag}]")

    print()
    while True:
        choice = input("Select device number: ").strip()
        try:
            idx = int(choice)
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        print(f"Enter a number between 0 and {len(devices) - 1}.")
