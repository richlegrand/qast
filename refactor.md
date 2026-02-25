# Codebase Structural Cleanup

Four structural improvements to reduce code duplication, improve encapsulation, and make the codebase easier to maintain. No behavior changes — purely internal restructuring.

---

## 1. Segment Base Class

**Problem:** 4 segment classes duplicate ~70 lines of identical code (`_drain_stderr`, `kill`, `stdout`, `_parse_duration`, `wait`, `start`) plus constants (`_FRAME_RE`, `_VIDEO_FPS`).

**Files modified:**
- `qast/pipeline/segment.py` — add `SegmentBase` class at top, make `SegmentFFmpeg` inherit
- `qast/pipeline/placeholder.py` — make `PlaceholderSegment` inherit from `SegmentBase`
- `qast/capture.py` — make `ScreenSegment` and `WebcamSegment` inherit from `SegmentBase`

**New class in `pipeline/segment.py`:**
```python
class SegmentBase:
    """Common base for all ffmpeg-based segments."""

    _log_name: str = "Segment"  # overridden by subclasses for log messages

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.actual_duration: float | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        raise NotImplementedError

    def start(self) -> None:
        cmd = self._build_cmd()
        log.info("%s: %s", self._log_name, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        # current shared logic

    def _parse_duration(self) -> float | None:
        # current shared logic (uses _FRAME_RE, _VIDEO_FPS defined once in this module)

    def wait(self) -> int:
        # current shared logic, uses self._log_name for log messages

    def kill(self) -> None:
        # current shared logic, uses self._log_name for log messages

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else None
```

**Per-class changes:**
- `SegmentFFmpeg`: inherits `SegmentBase`, sets `_log_name = "Segment ffmpeg"`, keeps own `__init__` (calls `super().__init__()`), keeps own `_build_cmd()`; remove duplicated methods
- `ScreenSegment`: inherits `SegmentBase`, sets `_log_name = "Screen capture"`, keeps own `__init__` + `_build_cmd()`; remove duplicated methods
- `WebcamSegment`: same pattern, `_log_name = "Webcam capture"`
- `PlaceholderSegment`: inherits `SegmentBase`, sets `_log_name = "Placeholder"`, keeps own `__init__` + `_build_cmd()`, **overrides `start()`** (has drawtext fallback logic); remove duplicated methods

**Constants:** `_FRAME_RE` and `_VIDEO_FPS` defined once in `segment.py`, imported by `placeholder.py` and `capture.py`.

**Note on `wait()` differences:** SegmentFFmpeg has an extra `elif` debug log branch. To handle per-class log format differences cleanly, `wait()` in the base class uses `self._log_name` and a consistent format. The minor logging differences (%.10fs vs %.1fs) are normalized to one format.

---

## 2. Shared FFmpeg Output Args

**Problem:** All 4 segment classes repeat the same ~15 encoding flags.

**File modified:** `qast/config.py` — add helper function

```python
def ffmpeg_output_args(
    preset: str | None = None,
    tune: str | None = None,
    flush_packets: bool = True,
    pix_fmt: str | None = None,
) -> list[str]:
    """Standard ffmpeg output encoding args for all segments."""
    args = []
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    args += [
        "-c:v", VIDEO_CODEC,
        "-preset", preset or VIDEO_PRESET,
    ]
    if tune:
        args += ["-tune", tune]
    args += [
        "-b:v", VIDEO_BITRATE,
        "-s", VIDEO_SIZE,
        "-r", VIDEO_FPS,
        "-g", VIDEO_GOP,
        "-c:a", AUDIO_CODEC,
        "-ar", AUDIO_SAMPLE_RATE,
        "-ac", AUDIO_CHANNELS,
        "-b:a", AUDIO_BITRATE,
        "-shortest",
        "-muxdelay", "0", "-muxpreload", "0",
    ]
    if flush_packets:
        args += ["-flush_packets", "1"]
    args += ["-f", "mpegts", "pipe:1"]
    return args
```

**Per-class `_build_cmd()` changes:**
- `SegmentFFmpeg`: `... + config.ffmpeg_output_args()`
- `PlaceholderSegment`: `... + config.ffmpeg_output_args(preset="ultrafast", tune="stillimage", flush_packets=False)`
- `ScreenSegment`: `... + config.ffmpeg_output_args(pix_fmt="yuv420p")`
- `WebcamSegment`: `... + config.ffmpeg_output_args(pix_fmt="yuv420p")`

---

## 3. Shared SSDP Discovery

**Problem:** `discovery/roku.py` and `discovery/dlna.py` duplicate the SSDP M-SEARCH socket logic.

**New file:** `qast/discovery/ssdp.py`

```python
def ssdp_search(
    service_type: str,
    timeout: int = 5,
    normalize_location: Callable[[str], str] | None = None,
) -> set[str]:
    """SSDP M-SEARCH — returns set of Location URLs."""
    # Socket creation, M-SEARCH message, send/receive loop
    # (extracted from current roku.py/dlna.py)
```

**Files modified:**
- `discovery/roku.py` — replace inline SSDP code with `ssdp_search("roku:ecp", normalize_location=_normalize_roku_url)`
- `discovery/dlna.py` — replace inline SSDP code with `ssdp_search("urn:schemas-upnp-org:device:MediaRenderer:1")`

Each file keeps its own device XML parsing (`_parse_device`, `_parse_description`) — only the SSDP network code is shared.

---

## 4. Queue Encapsulation

**Problem:** `api.py` and `progress.py` access 5 different private attributes of PlayQueue (11 accesses total).

**File modified:** `qast/queue.py` — add public methods:

```python
def set_loop(self, enabled: bool) -> None:
    """Enable or disable queue looping."""
    self._loop = enabled

@property
def has_capture_items(self) -> bool:
    """True if any queued item is a capture source."""
    return any(qi.capture for qi in self._all_items)

def pending_labels(self) -> list[str]:
    """Return labels for all pending items (thread-safe)."""
    with self._lock:
        return [qi.url or qi.title or qi.capture or "item" for qi in self._pending]

def peek_next(self) -> tuple[int, str | None]:
    """Return (pending_count, next_item_title) under lock.

    Used by progress bar to show queue status without exposing internals.
    """
    with self._lock:
        count = len(self._pending) + len(self._resolved)
        if self._resolved:
            title = self._resolved[0].title
        elif self._pending:
            qi = self._pending[0]
            title = qi.title or qi.url or qi.capture
        else:
            title = None
        return count, title
```

**Files modified to use new methods:**
- `api.py` line 202: `self._queue._loop = repeat` → `self._queue.set_loop(repeat)`
- `api.py` line 207: `any(qi.capture for qi in self._queue._all_items)` → `self._queue.has_capture_items`
- `api.py` lines 250-253: `with self._queue._lock: [qi.url ... for qi in self._queue._pending]` → `self._queue.pending_labels()`
- `progress.py` lines 130-137: replace `_lock`/`_pending`/`_resolved` access block with `self._queue.peek_next()`

---

## Execution Order

1. **Segment base class** (item 1) — largest change, do first
2. **FFmpeg output args** (item 2) — builds on item 1, simplifies `_build_cmd()` methods
3. **SSDP discovery** (item 3) — independent, new file + two edits
4. **Queue encapsulation** (item 4) — independent, add methods + update callers

Each step is independently verifiable via import check (`python3 -c "from qast.pipeline.segment import SegmentBase, SegmentFFmpeg; ..."` etc).

## Verification

- `python3 -c "from qast.pipeline.segment import SegmentBase, SegmentFFmpeg; from qast.pipeline.placeholder import PlaceholderSegment; from qast.capture import ScreenSegment, WebcamSegment"` — all imports work
- `python3 -c "from qast.discovery.ssdp import ssdp_search"` — new module imports
- `python3 -c "from qast.config import ffmpeg_output_args"` — helper imports
- `python3 -c "from qast.queue import PlayQueue; q = PlayQueue(); q.set_loop(True); q.pending_labels(); q.peek_next()"` — new queue methods work
- Manual smoke test: `python3 -m qast <url>` still works end-to-end
