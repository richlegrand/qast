"""Device dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Device:
    name: str
    model: str
    protocol: str           # "cast" | "dlna" | "roku"
    host: str
    port: int
    cast_obj: Any = field(default=None, repr=False)
    control_url: str | None = None
