"""Source string parser for CLI and playlist input.

Parses source strings like:
    video.mp4@5m        → URL with 5-minute duration
    screen@30s          → screen capture for 30 seconds
    webcam              → webcam capture
    browser:https://... → headless browser capture
    window:Grafana@1m   → window capture by title
"""

from __future__ import annotations

import re

from .queue import QueueItem

# Duration pattern: combinations of hours, minutes, seconds (e.g. 1h30m, 5m, 30s, 5m30s)
_DURATION_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$"
)


def _parse_duration(s: str) -> float:
    """Parse durations like '30s', '5m', '1h', '1h30m', '5m30s', or bare seconds."""
    s = s.strip().lower()
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and m.group(0):
        h = float(m.group(1) or 0)
        mins = float(m.group(2) or 0)
        secs = float(m.group(3) or 0)
        return h * 3600 + mins * 60 + secs
    return float(s)


def _is_duration_suffix(s: str) -> bool:
    """Check if a string looks like a duration suffix (e.g. '5m', '30s', '1h30m')."""
    if not s:
        return False
    m = _DURATION_RE.fullmatch(s.lower())
    return bool(m and m.group(0))


def _split_duration_suffix(raw: str) -> tuple[str, float | None]:
    """Split a source string on the rightmost '@' if the suffix is a duration.

    Returns (source, duration) where duration is None if no valid suffix found.
    """
    idx = raw.rfind("@")
    if idx <= 0:
        # No '@' or '@' is the first character
        return raw, None

    suffix = raw[idx + 1:]
    if _is_duration_suffix(suffix):
        return raw[:idx], _parse_duration(suffix)
    return raw, None


def merge_duration_args(raw_args: list[str]) -> list[str]:
    """Pre-process arg list, merging standalone @duration args with the preceding source.

    Example: ["url", "@5m"] → ["url@5m"]
             ["url", "@5m", "video.mp4"] → ["url@5m", "video.mp4"]
    """
    result: list[str] = []
    for arg in raw_args:
        if arg.startswith("@") and _is_duration_suffix(arg[1:]) and result:
            result[-1] = result[-1] + arg
        else:
            result.append(arg)
    return result


def parse_source(
    raw: str,
    global_duration: float | None = None,
    cursor: bool = True,
) -> QueueItem:
    """Parse a source string into a QueueItem.

    Detects screen, webcam, browser:, window: prefixes,
    falls back to URL/file.
    """
    source, item_duration = _split_duration_suffix(raw)
    duration = item_duration if item_duration is not None else global_duration

    if source == "screen":
        return QueueItem(capture="screen", duration=duration, cursor=cursor,
                         title="Screen Capture")

    if source == "webcam":
        return QueueItem(capture="webcam", duration=duration, title="Webcam")

    if source.startswith("browser:"):
        url = source[len("browser:"):]
        return QueueItem(capture="browser", url=url, duration=duration,
                         title=url, cursor=cursor)

    if source == "window":
        return QueueItem(capture="window", duration=duration, cursor=cursor,
                         title="Window Capture")

    if source.startswith("window:"):
        title = source[len("window:"):]
        return QueueItem(capture="window", window_title=title,
                         duration=duration, cursor=cursor,
                         title=f"Window: {title}")

    # URL or file path
    return QueueItem(url=source, duration=duration)


def has_capture_source(items: list[QueueItem]) -> bool:
    """True if any item in the list is a capture source."""
    return any(item.capture for item in items)
