# casturl Screen and Window Capture Specification

## Overview

casturl can capture live screen or window content and stream it to cast devices, turning any display content into a castable source. This enables:

- Presentations without HDMI cables
- Dashboards on lobby TVs
- Browser tabs on the big screen
- Any app cast to any TV

## CLI Interface

### Screen Capture

```bash
# Capture entire screen (primary monitor)
casturl --screen

# List monitors
casturl --screen --list

# Capture specific monitor
casturl --screen 2

# With audio (system audio loopback)
casturl --screen --audio
```

### Window Capture

```bash
# Interactive window selection
casturl --window

# By window title (partial match)
casturl --window "Sales Dashboard"

# By window ID (for scripting)
casturl --window-id 0x4a00004
```

### Options

```bash
--fps 30              # Capture framerate (default: 30, use lower for static content)
--cursor              # Show mouse cursor (default: on)
--no-cursor           # Hide mouse cursor
--cursor-highlight    # Highlight clicks (yellow circle animation)
--follow-cursor       # Pan viewport to follow cursor (for large screens)
--region WxH+X+Y      # Capture region only (e.g., 1920x1080+0+0)
```

### Mixed Playlists

Screen/window sources can be mixed with video sources:

```bash
# 30 sec dashboard, then hype video, repeat
casturl --window "Dashboard" --duration 30 hype.mp4 --repeat
```

Or in a playlist file:

```
# playlist.txt
screen://0?duration=30          # Primary screen for 30 sec
https://youtube.com/watch?v=... # Then a video
window://Sales Dashboard        # Then a window
```

## Implementation

### Platform Detection

```python
import platform

def get_capture_backend():
    system = platform.system()
    if system == 'Linux':
        # Check for Wayland vs X11
        if os.environ.get('WAYLAND_DISPLAY'):
            return 'pipewire'  # or wlroots screencopy
        return 'x11grab'
    elif system == 'Darwin':
        return 'avfoundation'
    elif system == 'Windows':
        return 'gdigrab'
```

### Screen Capture (via ffmpeg)

Let ffmpeg handle capture directly — it's faster and handles edge cases.

**Linux (X11):**
```python
ffmpeg_args = [
    '-f', 'x11grab',
    '-framerate', str(fps),
    '-video_size', f'{width}x{height}',
    '-i', f':0.0+{x},{y}',          # display+offset
    '-draw_mouse', '1' if cursor else '0',
]
```

**Linux (Wayland/PipeWire):**
```python
ffmpeg_args = [
    '-f', 'pipewire',
    '-framerate', str(fps),
    '-i', 'default',
]
# Note: PipeWire capture may trigger a permission dialog
```

**macOS:**
```python
ffmpeg_args = [
    '-f', 'avfoundation',
    '-framerate', str(fps),
    '-capture_cursor', '1' if cursor else '0',
    '-i', f'{screen_index}:none',   # video:audio
]
```

**Windows:**
```python
ffmpeg_args = [
    '-f', 'gdigrab',
    '-framerate', str(fps),
    '-draw_mouse', '1' if cursor else '0',
    '-i', 'desktop',                # or 'title=Window Name'
]
```

### Window Enumeration

**Linux (X11):**
```python
import subprocess

def list_windows_linux():
    """Use wmctrl to list windows."""
    out = subprocess.check_output(['wmctrl', '-l']).decode()
    windows = []
    for line in out.strip().split('\n'):
        parts = line.split(None, 3)
        if len(parts) >= 4:
            windows.append({
                'id': int(parts[0], 16),
                'desktop': parts[1],
                'host': parts[2],
                'title': parts[3],
            })
    return windows

def list_windows_linux_xdotool():
    """Alternative using xdotool."""
    out = subprocess.check_output(['xdotool', 'search', '--name', '']).decode()
    window_ids = [int(wid) for wid in out.strip().split('\n') if wid]
    
    windows = []
    for wid in window_ids:
        try:
            name = subprocess.check_output(['xdotool', 'getwindowname', str(wid)]).decode().strip()
            windows.append({'id': wid, 'title': name})
        except:
            pass
    return windows
```

**macOS:**
```python
import Quartz

def list_windows_macos():
    """Use Quartz to list windows."""
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID
    )
    
    windows = []
    for w in window_list:
        windows.append({
            'id': w.get('kCGWindowNumber'),
            'title': w.get('kCGWindowName', ''),
            'owner': w.get('kCGWindowOwnerName', ''),
            'bounds': w.get('kCGWindowBounds'),
        })
    return windows
```

**Windows:**
```python
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

def list_windows_windows():
    """Enumerate visible windows."""
    windows = []
    
    def enum_callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, buf, length)
            if buf.value:
                windows.append({
                    'id': hwnd,
                    'title': buf.value,
                })
        return True
    
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return windows
```

### Window Capture

**Linux (X11) — ffmpeg native:**
```python
def capture_window_linux(window_id, fps, cursor):
    return [
        '-f', 'x11grab',
        '-framerate', str(fps),
        '-window_id', hex(window_id),
        '-draw_mouse', '1' if cursor else '0',
        '-i', ':0',
    ]
```

**macOS — Python capture required:**

ffmpeg's avfoundation can capture screens but not specific windows. Use Python:

```python
import Quartz
from PIL import Image

def capture_window_macos(window_id):
    """Capture a specific window by ID."""
    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming
    )
    
    width = Quartz.CGImageGetWidth(cg_image)
    height = Quartz.CGImageGetHeight(cg_image)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(cg_image)
    
    data_provider = Quartz.CGImageGetDataProvider(cg_image)
    data = Quartz.CGDataProviderCopyData(data_provider)
    
    return Image.frombytes('RGBA', (width, height), data, 'raw', 'BGRA')
```

**Windows — ffmpeg native:**
```python
def capture_window_windows(window_title, fps, cursor):
    return [
        '-f', 'gdigrab',
        '-framerate', str(fps),
        '-draw_mouse', '1' if cursor else '0',
        '-i', f'title={window_title}',
    ]
```

### Mouse Cursor

#### Native cursor (via ffmpeg)

ffmpeg's `-draw_mouse 1` captures the system cursor. This is the simplest approach and works for most cases.

#### Custom cursor rendering (Python capture path)

When using Python capture (macOS windows, or for custom cursor effects):

```python
import Quartz

def get_cursor_info():
    """Get current cursor position and image."""
    # Position
    pos = Quartz.NSEvent.mouseLocation()
    screen_height = Quartz.CGDisplayPixelsHigh(Quartz.CGMainDisplayID())
    x, y = pos.x, screen_height - pos.y  # Flip Y coordinate
    
    return {'x': int(x), 'y': int(y)}

def render_cursor(frame, cursor_pos, cursor_img):
    """Composite cursor onto frame."""
    frame.paste(cursor_img, (cursor_pos['x'], cursor_pos['y']), cursor_img)
    return frame
```

#### Click highlighting

Visual feedback on mouse clicks:

```python
import time
from collections import deque

click_events = deque(maxlen=10)  # Recent clicks

def on_click(x, y, button, pressed):
    if pressed:
        click_events.append({
            'x': x,
            'y': y,
            'time': time.time(),
        })

def render_click_highlights(frame, draw):
    """Draw fading yellow circles at click locations."""
    now = time.time()
    for click in click_events:
        age = now - click['time']
        if age < 0.5:  # Visible for 500ms
            alpha = int(255 * (1 - age / 0.5))
            radius = int(20 + age * 40)  # Expanding circle
            draw.ellipse(
                [click['x'] - radius, click['y'] - radius,
                 click['x'] + radius, click['y'] + radius],
                outline=(255, 255, 0, alpha),
                width=3
            )
```

For click detection, use `pynput`:

```python
from pynput import mouse

listener = mouse.Listener(on_click=on_click)
listener.start()
```

### Audio Capture

System audio (what's playing through speakers):

**Linux (PulseAudio):**
```python
audio_args = [
    '-f', 'pulse',
    '-i', 'default',  # Or specific sink
]
# List sinks: pactl list short sinks
```

**Linux (PipeWire):**
```python
audio_args = [
    '-f', 'pulse',  # PipeWire has PulseAudio compat
    '-i', 'default',
]
```

**macOS:**

Requires a loopback driver like BlackHole or Soundflower:
```python
audio_args = [
    '-f', 'avfoundation',
    '-i', ':BlackHole 2ch',
]
```

**Windows (WASAPI):**
```python
audio_args = [
    '-f', 'dshow',
    '-i', 'audio=virtual-audio-capturer',  # Or specific device
]
# Or use WASAPI loopback via ffmpeg build with --enable-libwasapi
```

### Integration with casturl Pipeline

Screen/window capture is just another segment source. Same named pipes, same master muxer:

```python
def create_screen_segment(screen_index, fps, cursor, audio):
    """Create ffmpeg args for screen capture segment."""
    
    video_args = get_screen_capture_args(screen_index, fps, cursor)
    
    args = ['ffmpeg'] + video_args
    
    if audio:
        args += get_audio_capture_args()
        args += ['-map', '0:v', '-map', '1:a']
    
    args += [
        '-c:v', 'libx264', '-preset', 'ultrafast',
        '-s', '1920x1080', '-r', '30',
        '-f', 'h264', 'pipe:video',
    ]
    
    if audio:
        args += [
            '-c:a', 'aac', '-ar', '44100', '-ac', '2',
            '-f', 'adts', 'pipe:audio',
        ]
    
    return args
```

The master muxer doesn't know it's receiving screen content vs video file content. It's all just H.264 frames through the pipe.

### Duration Limits

For playlist mixing, screen/window sources need duration limits:

```python
# Capture for 30 seconds then move to next item
args += ['-t', '30']
```

Without a duration, screen capture runs until interrupted.

## Interactive Selection UI

```
casturl --window

Scanning for windows...

Available windows:
  1. Firefox — Q4 Sales Dashboard
  2. Firefox — Gmail
  3. Terminal — ~/projects/casturl
  4. Slack — Anthropic
  5. Code — architecture.md

Select window (1-5): 1
Framerate [30]: 
Show cursor? [Y/n]: y
Highlight clicks? [y/N]: n

Capturing "Firefox — Q4 Sales Dashboard" at 30fps
Casting to: Living Room TV
Press Ctrl+C to stop
```

## Dependencies

**Required:**
- ffmpeg (with x11grab/avfoundation/gdigrab support)

**Optional by platform:**

| Platform | Window listing | Click events |
|----------|---------------|--------------|
| Linux | wmctrl or xdotool | pynput |
| macOS | Quartz (stdlib) | pynput |
| Windows | ctypes (stdlib) | pynput |

```toml
# pyproject.toml
[project.optional-dependencies]
screen = [
    "pynput>=1.7.0",     # Click highlighting
    "pillow>=9.0.0",     # Frame manipulation (macOS window capture)
]
```

## Limitations

- **Wayland**: Limited support. PipeWire capture requires user permission dialog. Individual window capture not well supported.
- **macOS window capture**: Requires Python frame capture path (slower than native ffmpeg).
- **System audio**: Requires platform-specific setup (PulseAudio/PipeWire on Linux, BlackHole on macOS, virtual device on Windows).
- **DRM content**: Protected browser content (Netflix, etc.) will show black.

## Examples

### Office dashboard on lobby TV

```bash
# Open dashboard in browser, then:
casturl --window "Dashboard" --no-cursor --fps 5 --repeat
```

Low framerate since dashboard is mostly static. No cursor for clean look.

### Presentation to conference room

```bash
casturl --screen --cursor --cursor-highlight
```

Show cursor with click highlights so audience can follow along.

### Automated playlist with dashboard breaks

```bash
# playlist.txt
window://Dashboard?duration=60
https://youtube.com/watch?v=hype-video
window://Dashboard?duration=60
https://youtube.com/watch?v=another-video
```

```bash
casturl playlist.txt --repeat
```

### Picture-in-picture (future enhancement)

```bash
# Main video with dashboard overlay in corner
casturl main-video.mp4 --pip "window://Dashboard" --pip-position=bottom-right
```
