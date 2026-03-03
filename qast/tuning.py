"""Adaptive runtime tuning for discovery behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_STATE_PATH = Path.home() / ".qast" / "runtime_state.json"


@dataclass
class DiscoveryState:
    consecutive_failures: int = 0


def _read_state() -> DiscoveryState:
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return DiscoveryState(consecutive_failures=int(raw.get("consecutive_failures", 0)))
    except Exception:
        return DiscoveryState()


def _write_state(state: DiscoveryState) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"consecutive_failures": state.consecutive_failures}),
            encoding="utf-8",
        )
    except Exception:
        pass


def adaptive_discovery_timeout(base_timeout: int) -> int:
    """Increase discovery timeout after repeated no-device scans."""
    state = _read_state()
    extra = min(state.consecutive_failures * 2, 10)
    return int(base_timeout + extra)


def record_discovery_result(device_count: int) -> None:
    state = _read_state()
    if device_count > 0:
        state.consecutive_failures = 0
    else:
        state.consecutive_failures = min(state.consecutive_failures + 1, 5)
    _write_state(state)
