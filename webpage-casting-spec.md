# Webpage Casting Spec

Cast any URL to a TV by rendering it in a headless browser and encoding the output as video. Targeted at dashboards, scoreboards, status pages — anything you'd put on a TV in an office or lobby.

## Usage

### CLI

```bash
# Cast a webpage
qast --webpage https://grafana.local/dashboard

# Auto-refresh every 30 seconds (re-screenshot, not browser reload)
qast --webpage https://status.example.com --refresh 30

# Reload the page every 5 minutes (full browser navigation)
qast --webpage https://status.example.com --reload 300

# With duration limit
qast --webpage https://grafana.local/dashboard --duration 1h

# Mix with other sources in a queue
qast --webpage https://status.example.com video.mp4 https://youtube.com/watch?v=...
```

### Python API

```python
from qast import Qast, discover

devices = discover()
q = Qast(device=devices[0])
q.add_webpage("https://grafana.local/dashboard", refresh=30)
q.add("https://youtube.com/watch?v=...")
q.play()
```

## How It Works

1. Playwright launches headless Chromium, navigates to the URL
2. A loop takes periodic screenshots (`page.screenshot(type="png")`)
3. Screenshots are piped to ffmpeg as image2pipe input
4. ffmpeg encodes to MPEG-TS with silent audio (anullsrc), same as ScreenSegment
5. Pipeline consumes the MPEG-TS stdout like any other segment

### Why screenshot loop instead of Playwright's record_video

Playwright's `record_video` outputs VP8 WebM and gives no control over frame timing. A screenshot loop is simpler, doesn't need a re-encode step, and for static/dashboard content a low FPS (1-5) is fine — less CPU, same visual result.

### Frame rate

Default: **1 fps**. Dashboards don't need 30fps. This keeps CPU usage minimal. The ffmpeg command uses `-framerate 1` on input and outputs at `VIDEO_FPS` (30) via frame duplication — the TV sees a normal 30fps stream.

If `--refresh` is set, screenshots happen at that interval. Otherwise 1/sec.

## Architecture

```
[Playwright]  -->  png bytes  -->  [ffmpeg image2pipe -> mpegts]  -->  stdout
                                        + anullsrc audio
```

Same segment interface as ScreenSegment/WebcamSegment: `start()`, `wait()`, `kill()`, `stdout`.

## Implementation

### New file: `qast/web.py`

```python
class WebpageSegment(SegmentBase):  # or standalone, same interface
    """Renders a webpage via headless Chromium, outputs MPEG-TS on stdout."""

    _log_name = "Webpage capture"

    def __init__(
        self,
        url: str,
        duration: float | None = None,
        refresh: float = 1.0,       # screenshot interval in seconds
        reload: float | None = None, # full page reload interval (None = never)
        viewport: tuple[int, int] = (1920, 1080),
    ) -> None:
        super().__init__()
        self.url = url
        self.duration = duration
        self.refresh = refresh
        self.reload = reload
        self.viewport = viewport
```

**`start()`** overrides the base class:

1. Import playwright (lazy — fail with clear message if not installed)
2. Launch headless Chromium with viewport matching VIDEO_SIZE
3. Navigate to URL, wait for load
4. Start ffmpeg subprocess with image2pipe input + anullsrc audio
5. Start a daemon thread that:
   - Takes screenshots at `self.refresh` interval
   - Writes PNG bytes to ffmpeg's stdin
   - If `self.reload` is set, calls `page.reload()` at that interval
   - Stops when duration expires or `kill()` is called

**`_build_cmd()`**:

```python
def _build_cmd(self) -> list[str]:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
        # Image input (PNGs piped to stdin)
        "-f", "image2pipe", "-framerate", str(1 / self.refresh), "-i", "pipe:0",
        # Silent audio
        "-f", "lavfi", "-i",
        f"anullsrc=r={config.AUDIO_SAMPLE_RATE}"
        f":cl={'stereo' if config.AUDIO_CHANNELS == '2' else 'mono'}",
    ]
    if self.duration is not None:
        cmd += ["-t", str(self.duration)]
    cmd += config.ffmpeg_output_args(pix_fmt="yuv420p")  # if refactor lands
    # or inline the encoding args like ScreenSegment does today
    return cmd
```

**`kill()`** overrides base to also close browser:

```python
def kill(self) -> None:
    self._stop_event.set()
    super().kill()
    # Close browser in finally/cleanup
```

### Modified files

| File | Change |
|------|--------|
| `qast/queue.py` | Add `webpage_url: str \| None = None` and `refresh: float \| None = None` and `reload: float \| None = None` to `QueueItem` |
| `qast/resolve/ytdlp.py` | Add `webpage_url: str \| None = None`, `refresh: float \| None = None`, `reload: float \| None = None` to `ResolvedURL` |
| `qast/queue.py` | In `_resolve_item()`, pass `webpage_url`/`refresh`/`reload` through to `ResolvedURL` when `capture == "webpage"` |
| `qast/pipeline/pipeline.py` | Add `elif item.capture == "webpage": return WebpageSegment(...)` in `_create_capture_segment()` |
| `qast/cli.py` | Add `--webpage`, `--refresh`, `--reload` args; add fields to `Args` |
| `qast/__main__.py` | Add `elif args.webpage:` block to create WebpageSegment and start capture pipeline |
| `qast/api.py` | Add `add_webpage(url, duration, refresh, reload, placeholder)` to `Qast` class |

### Optional dependency

Playwright is heavy (~150MB browser download). Make it optional:

```python
# In web.py, at the top of start()
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    raise SystemExit(
        "Webpage capture requires playwright.\n"
        "Install it with: pip install playwright && playwright install chromium"
    )
```

When packaging is added later, this becomes `pip install qast[web]`.

## Edge Cases

- **Playwright not installed** — clear error message with install instructions
- **Page load timeout** — default 30s timeout, log warning, continue with whatever rendered
- **Page with animations/video** — 1fps screenshot will look choppy, but that's fine for the dashboard use case. Users who want smooth video should use `--screen` instead
- **Auth-protected pages** — out of scope for v1. Could later support `--cookies-from-browser` for Playwright too
- **Dark/blank pages** — not our problem, render what the browser renders
- **Very long durations** — browser memory could grow. The `--reload` option helps since it forces a fresh page load

## Not in scope (future)

- Browser cookie/session injection
- JavaScript interaction (clicking, scrolling)
- Multiple pages in rotation (use queue for that: `qast --webpage url1 --webpage url2`)
- Custom CSS injection
- PDF rendering (separate feature)
