# qast Python API

```python
from qast import discover, cast, Qast, Device, Status
```

## Device discovery

### `discover(timeout=15, show_all=False) -> list[Device]`

Scan the local network for cast-capable devices. Returns a list of `Device` objects. Results are cached internally so subsequent calls to `cast()` or `Qast()` can resolve devices by index or name without re-scanning.

| Parameter  | Type   | Default | Description                                      |
|------------|--------|---------|--------------------------------------------------|
| `timeout`  | `int`  | `15`    | Seconds to wait for SSDP/mDNS responses.         |
| `show_all` | `bool` | `False` | Include devices that may not support video (e.g. audio-only speakers). |

```python
devices = discover()
for i, d in enumerate(devices):
    print(f"  [{i}] {d.name} ({d.protocol})")
```

## Device

```python
@dataclass
class Device:
    name: str               # friendly name ("Living Room TV")
    model: str              # model string from the device
    protocol: str           # "cast" | "dlna" | "roku"
    host: str               # IP address
    port: int               # control port
```

Wherever a function accepts a `device` parameter, you can pass:

- A `Device` object directly
- An `int` index into the most recent `discover()` results
- A `str` substring matched against device names (case-insensitive), or an exact protocol name (`"roku"`, `"dlna"`, `"cast"`)

```python
cast("https://...", device=devices[0])   # Device object
cast("https://...", device=0)            # index
cast("https://...", device="roku")       # protocol match
cast("https://...", device="living")     # name substring
```

If no prior `discover()` call has been made, one is triggered automatically.

## One-shot casting

### `cast(source, *, device, ...) -> None`

Cast one or more sources and block until playback finishes or `KeyboardInterrupt`.

| Parameter             | Type                      | Default | Description                                      |
|-----------------------|---------------------------|---------|--------------------------------------------------|
| `source`              | `str \| list[str]`        | —       | URL, local file path, capture spec, or list of them. |
| `device`              | `Device \| str \| int`    | —       | Target device (see [Device](#device) selectors). |
| `duration`            | `float \| None`           | `None`  | Per-item duration limit in seconds.              |
| `repeat`              | `bool`                    | `False` | Loop the queue when all items finish.            |
| `shuffle`             | `bool`                    | `False` | Randomize source order before playing.           |
| `no_placeholder`      | `bool`                    | `False` | Skip loading/up-next placeholder screens.        |
| `cookies_from_browser`| `str \| None`             | `None`  | Browser name to extract cookies from (e.g. `"chrome"`). |
| `preroll`             | `float`                   | `0`     | Seconds of placeholder to show before the first item. |
| `placeholder_time`    | `float`                   | `0`     | Minimum placeholder duration between segments.   |

Source strings follow the same syntax as the CLI:

| Source                    | Description                          |
|---------------------------|--------------------------------------|
| `"https://youtube.com/..."` | Any URL supported by yt-dlp        |
| `"/path/to/video.mp4"`   | Local file                           |
| `"screen"`                | Screen capture                       |
| `"screen@30s"`            | Screen capture with duration         |
| `"window:Firefox"`        | Window capture by title              |
| `"webcam"`                | Webcam capture                       |
| `"browser:https://..."`  | Headless browser capture of a URL    |

```python
# Single URL
cast("https://www.youtube.com/watch?v=dQw4w9WgXcQ", device="roku")

# Multiple sources with duration
cast(["screen@10s", "https://youtube.com/watch?v=..."], device=0, duration=30)

# Loop a playlist
cast(["a.mp4", "b.mp4", "c.mp4"], device="roku", repeat=True, shuffle=True)
```

## Queue-based casting

### `Qast(device, ...) -> Qast`

Create a programmatic queue. Build up items with `add*()` methods, then call `play()`.

| Parameter             | Type                   | Default | Description                                      |
|-----------------------|------------------------|---------|--------------------------------------------------|
| `device`              | `Device \| str \| int` | —       | Target device.                                   |
| `cookies_from_browser`| `str \| None`          | `None`  | Browser to extract cookies from.                 |
| `save_stream`         | `str \| None`          | `None`  | File path to save raw stream (MPEG-TS).          |
| `preroll`             | `float`                | `0`     | Seconds of placeholder before the first item.    |
| `placeholder_time`    | `float`                | `0`     | Minimum placeholder duration between segments.   |

### Adding items

All `add*()` methods can be called before or after `play()`. Items added after `play()` will be picked up on the next loop iteration (when `repeat=True`).

#### `q.add(url, duration=None, placeholder=True)`

Add a URL or local file path.

```python
q.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ", duration=30)
q.add("/home/user/video.mp4")
```

#### `q.add_screen(duration=None, placeholder=True)`

Add screen capture.

```python
q.add_screen(duration=60)
```

#### `q.add_window(title, duration=None, placeholder=True)`

Add window capture by title (substring match).

```python
q.add_window("Firefox", duration=30)
```

#### `q.add_webcam(duration=None, placeholder=True)`

Add webcam capture.

```python
q.add_webcam(duration=15)
```

#### `q.add_browser(url, duration=None, placeholder=True)`

Add headless browser capture of a URL.

```python
q.add_browser("https://grafana.local/dashboard", duration=60)
```

### Playback control

#### `q.play(repeat=False, show_placeholder=True, verbose=False)`

Start playback. Returns immediately (non-blocking). The pipeline and casting run in background threads.

| Parameter          | Type   | Default | Description                              |
|--------------------|--------|---------|------------------------------------------|
| `repeat`           | `bool` | `False` | Loop the queue indefinitely.             |
| `show_placeholder` | `bool` | `True`  | Show loading/up-next screens.            |
| `verbose`          | `bool` | `False` | Print buffer stats to stdout.            |

#### `q.skip()`

Skip the currently playing item and advance to the next.

#### `q.stop()`

Stop playback, shut down the pipeline, and send a stop command to the device.

#### `q.wait(timeout=None) -> bool`

Block until playback finishes. Returns `True` if playback completed, `False` on timeout.

#### `q.remove(index) -> str | None`

Remove a pending item by index. Returns the item label, or `None` if the index is out of range.

### Status

#### `q.status() -> Status`

```python
@dataclass
class Status:
    state: str              # "playing" | "idle"
    now_playing: str | None # title of the current item
    duration: float | None  # duration limit of the current item (seconds)
    position: float | None  # elapsed content time (seconds)
    queue: list[str]        # upcoming items (circular rotation when repeat=True)
```

When `repeat=True`, `queue` shows all items in play order rotated so the next-up item is first. When `repeat=False`, it shows only remaining pending items.

```python
q.play(repeat=True)
while True:
    s = q.status()
    if s.state != "playing":
        break
    pos = f"{int(s.position)}s" if s.position else "..."
    print(f"  {s.now_playing}  {pos}", end="\r")
    time.sleep(1)
q.stop()
```

## Complete examples

### One-shot

```python
from qast import discover, cast

devices = discover()
print(f"Casting to {devices[0].name}")
cast("https://www.youtube.com/watch?v=dQw4w9WgXcQ", device=devices[0])
```

### Queue with status loop

```python
import time
from qast import Qast

q = Qast(device="roku", cookies_from_browser="chrome")

q.add("https://www.youtube.com/watch?v=PwylW_sUfQY", duration=15)
q.add("https://www.youtube.com/watch?v=aOAzJ37Nxfw", duration=15)
q.add_screen(duration=15)
q.add_window("sublime", duration=15)
q.add_browser("pixycam.com", duration=15)
q.add_webcam(duration=15)

q.play(repeat=True)

while True:
    s = q.status()
    if s.state != "playing":
        break
    pos = f"{int(s.position)}s" if s.position else "..."
    print(f"  {s.now_playing}  {pos}", end="\r")
    time.sleep(1)

q.stop()
```

### Mixed capture and URLs

```python
from qast import cast

cast(
    ["screen@10s", "webcam@10s", "https://youtube.com/watch?v=..."],
    device=0,
    repeat=True,
    preroll=3,
)
```
