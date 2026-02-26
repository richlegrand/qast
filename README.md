# qast

qast casts anything to your TV from the command line. 

```bash
qast video.mov                                # Cast local file
qast "https://dropbox.com/abc123/video.mp4"   # Cast video located somewhere on web
qast "https://youtube.com/watch?v=..."        # Cast YouTube video
qast --screen                                 # Cast your computer desktop
qast --window                                 # Cast a window on your desktop
qast --browser "https://grafana.example.com"  # Cast a webpage (via headless Chromium)
qast --webcam                                 # Cast your webcam
cat stream.ts | qast -                        # Cast generic piped data
qast url1 url2 url3 --repeat                  # Cast varied content, queued, and looped
```

## The problem

Almost every TV made in the last decade can receive cast streams. But what they'll *accept* varies:

- Chromecast handles YouTube natively, but won't take an arbitrary URL
- Most DLNA TVs play MP4 files but won't play MKV or WebM
- Roku has varied mechanisms for streaming depending on version/vendor
- Screen mirroring exists on some platforms, not others

In other words, TVs have inconsistent streaming support — content that plays fine on a Samsung may fail on an LG. Codec mismatches (VP9, H.265, DivX/Xvid), uncommon containers (MKV, WebM, FLV, AVI, OGG), and unsupported audio formats (FLAC, Opus, DTS) are common causes of "format not supported" errors. Even when a TV claims to support a format, it may only handle specific codec profiles or resolutions.

## The solution

qast sidesteps the compatibility problem entirely. Practically all TVs accept either MPEG transport stream or fragmented MP4, so qast transcodes everything — URLs, files, screen captures, windows, webcams, piped data — into a single H.264/AAC stream. Input can be anything ffmpeg understands, which is practically every media format in existence. Because everything is transcoded to a common format, qast can play varied content (different sources, different formats, different resolutions) back to back seamlessly. The TV sees one continuous stream with consistent format, resolution and bitrate throughout — content is added dynamically to a continuously-running mux, so there are no gaps or format switches between items. qast basically creates your own TV station from the command line.

## Install

```bash
pip install qast
```

### Requirements

- Python 3.10+
- ffmpeg

```bash
# Ubuntu/Debian
sudo apt install ffmpeg
```

### Optional

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — for YouTube and [1000+ sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) (strongly recommended)
- [pychromecast](https://github.com/home-assistant-libs/pychromecast) — for Chromecast/Google TV support
- [Playwright](https://playwright.dev/python/) — for `--browser` mode (`pip install playwright && playwright install chromium`)
- xdotool — for `--window` mode on Linux (`apt install xdotool`)

## Quick start

```bash
# Cast a YouTube video
qast "https://youtube.com/watch?v=dQw4w9WgXcQ"

# Cast a local file
qast video.mov

# Cast your screen
qast --screen

# Pick a device by name
qast -d "Samsung" video.mp4
```

## What can you cast?

### Anything on the internet

```bash
qast "https://youtube.com/watch?v=..."
qast "https://vimeo.com/..."
qast "https://twitch.tv/..."
```

YouTube, Vimeo, Twitch, TikTok, Twitter/X, Dropbox, Google Drive, PBS, BBC, and [1000+ sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) via yt-dlp.

### Live TV streams

HLS and IPTV streams work directly. Many international broadcasters stream free online but getting those streams onto your TV is a pain and sometimes requires a paid app. qast handles the HLS fetching and transcoding — you just give it the URL. See [iptv-org](https://iptv-org.github.io/) for a directory of free streams.

```bash
# HLS streams (URLs are examples — check broadcaster sites for current links)
qast "https://live-hls-web-aje.getaj.net/AJE/index.m3u8"
qast "https://ott.grani.me/fr24/index.m3u8"
```

### Any file on your computer

```bash
qast video.mp4
qast ~/Videos/*.mp4
```

MP4, MKV, AVI, WebM, FLV, OGG, WMV — anything ffmpeg can read.

### Your entire screen

```bash
qast --screen              # primary monitor
qast --screen --no-cursor  # hide mouse cursor
```

Works even if your TV doesn't support Miracast or AirPlay. Note, Chromecast works best for live streaming. See [Live streaming](#live-streaming) below. 


### A single window

```bash
qast --window                        # click to select
qast --window --window-title Grafana  # by title
```

Note, Chromecast works best for live streaming. See [Live streaming](#live-streaming) below. 

### A webpage

```bash
qast --browser "https://grafana.example.com/dashboard"   # render any URL
qast --browser "https://example.com" --duration 5m       # stop after 5 minutes
```

Renders a URL in headless Chromium and casts the result to your TV. Great for dashboards, status pages, or any content that's best viewed as a live webpage rather than a video. Requires [Playwright](https://playwright.dev/python/) (`pip install playwright && playwright install chromium`).

### Your webcam

```bash
qast --webcam              # default camera
```

Note, Chromecast works best for live streaming. See [Live streaming](#live-streaming) below.

### Piped data

```bash
cat stream.ts | qast -
ffmpeg -i input.avi -f mpegts - | qast -
```

The `-` tells qast to read from stdin. This makes qast composable with any tool that outputs video.

**Audio visualizer from system audio:**
```bash
ffmpeg -f pulse -i $(pactl get-default-sink).monitor \
  -filter_complex "showwaves=s=1920x1080:mode=cline:colors=white" \
  -f mpegts - | qast -
```
Your music as a real-time waveform on your TV.

**Security cam grid:**
```bash
ffmpeg -i rtsp://cam1 -i rtsp://cam2 -i rtsp://cam3 -i rtsp://cam4 \
  -filter_complex "[0:v][1:v]hstack[top];[2:v][3:v]hstack[bottom];[top][bottom]vstack" \
  -f mpegts - | qast -
```
4 cameras on 1 TV. No NVR required.

**Generative art from Python:**
```bash
python my_visualizer.py | ffmpeg -f rawvideo -pix_fmt rgb24 -s 1920x1080 -r 30 -i - \
  -f mpegts - | qast -
```
Your code generates frames, ffmpeg encodes them, qast puts them on TV. No file ever touches disk.

**Test pattern:**
```bash
ffmpeg -f lavfi -i "testsrc2=size=1920x1080:rate=30" -f mpegts - | qast -
```

## Queue mode

Pass multiple sources to play them back to back as one continuous stream:

```bash
qast \
  "https://youtube.com/watch?v=morning-news" \
  ~/Videos/workout.mp4 \
  "https://youtube.com/watch?v=lofi-beats"
```

During playback, you can type commands:

```
<URL>   add a URL to the queue
s       skip current item
?       show queue status
q       quit
```

### Playlist files

Plain text, one source per line:

```
# morning.txt
https://youtube.com/watch?v=VIDEO1
https://youtube.com/watch?v=VIDEO2
~/Videos/workout.mp4
```

```bash
qast --playlist morning.txt
qast --playlist morning.txt --repeat
```

Comments start with `#`. Blank lines are ignored.

```bash
# Play a YouTube playlist
yt-dlp --flat-playlist --print url "https://youtube.com/playlist?list=PLxyz" | qast --playlist -

# Random files from a directory
ls ~/Videos/*.mp4 | shuf | qast --playlist -
```

## Supported devices

**Chromecast** — Chromecast, Chromecast with Google TV, Android TV

**DLNA** — Samsung, LG, Sony, and most smart TVs

**Roku** — Requires the free [Media Assistant](https://channelstore.roku.com/details/782875) app, but conveniently, this app only needs to be installed -- it doesn't need to be selected and "running" for qast to stream and render to your Roku device/TV.

qast auto-discovers devices on your network. If multiple are found, it presents a menu:

```
$ qast video.mp4
Scanning for devices...
  [0] Living Room TV (chromecast)
  [1] Bedroom Samsung (dlna)
  [2] Kitchen Roku (roku)
Select device:
```

Or specify directly:

```bash
qast -d "Samsung" video.mp4         # by name (substring match)
qast -d 0 video.mp4                 # by index
```

## Live streaming

Roku and DLNA TVs tend to have larger starup times and large buffers, which leads to larger latencies. If you're viewing your webcam or computer desktop, you might see a 10 second lag from when you move your mouse and it showing up on the TV (for example). TVs that support Chromecast tend to have only a few seconds of latency. 

For this reason Chromecast is better for live streaming if latency is important. 

## How it works
 
```
[source] → [yt-dlp resolve] → [ffmpeg transcode] → [TS rewriter] → [muxer] → [ring buffer] → [HTTP server] → [TV]
```

1. **Resolve** — yt-dlp extracts direct video URLs from YouTube etc. Local files and pipes skip this step.
2. **Transcode** — ffmpeg normalizes everything to H.264/AAC in MPEG-TS. This is the lowest common denominator that every TV accepts.
3. **Rewrite** — A TS rewriter ensures PTS/DTS continuity across segment boundaries, so the TV sees one seamless stream even when sources change.
4. **Mux** — A continuously-running muxer accepts rewritten TS segments and produces the output format. For DLNA and Roku, the rewritten MPEG-TS is used directly. For Chromecast, the master muxer remuxes to fragmented MP4.
5. **Buffer** — An in-memory ring buffer decouples the muxer from the HTTP server, absorbing bitrate variations.
6. **Cast** — Protocol-specific signaling (DLNA SOAP, Roku ECP, or Chromecast protobuf) tells the TV to stream from a local URL which points to qast's HTTP server.
7. **Serve** — The TV connects and qast streams the buffer contents over HTTP.

See [architecture.md](architecture.md) for details.

## CLI reference

```
qast [OPTIONS] [SOURCE...]

Sources:
  <file>                    Local video file
  <url>                     YouTube, Vimeo, etc. (via yt-dlp)
  --screen                  Capture primary screen
  --window                  Capture window (click to select)
  --window-title TITLE      Select window by title (use with --window)
  --browser                 Render a URL in headless Chromium and cast
  --webcam                  Capture default webcam
  -                         Read from stdin

Device:
  -d, --device NAME|INDEX   Select device by name (substring) or index

Queue:
  --playlist FILE           Load sources from a file (- for stdin)
  --repeat                  Loop the queue indefinitely
  --shuffle                 Shuffle queue order
  --no-placeholder          Disable "up next" placeholder screens

Capture:
  --no-cursor               Hide mouse cursor in screen capture
  --duration TIME            Stop capture after TIME (e.g., 30s, 5m, 1h)

Other:
  --cookies-from-browser B  Extract cookies from browser (chrome, firefox, brave) — helps when
                            YouTube blocks yt-dlp extraction (uses your logged-in session)
  --save-stream FILE        Save the served stream to a file (fMP4 or TS, matching device format)
  -v, --verbose             Debug logging
  -h, --help                Show help
```

## Python API

qast can be given detailed instructions via custom Python code.

### One-shot casting

For simple cases — cast something and block until it finishes:

```python
from qast import discover, cast

# Discover devices on the network
devices = discover()
for i, d in enumerate(devices):
    print(f"  [{i}] {d.name} ({d.protocol})")

# Cast a file (blocks until done or Ctrl+C)
cast("video.mp4", device="Living Room TV")

# Select by index
cast("https://youtube.com/watch?v=...", device=0)

# Cast your screen
cast(screen=True, device="Samsung")
```

### Queue-based playback

Build a queue, control playback, add and remove items on the fly:

```python
from qast import Qast

q = Qast(device="Living Room TV")

q.add("https://youtube.com/watch?v=VIDEO1")
q.add("https://youtube.com/watch?v=VIDEO2")
q.add("~/Videos/workout.mp4", placeholder=False)
q.add_screen(duration=30)
q.add_window("Grafana", duration=60)
q.add_browser("https://grafana.example.com/dashboard", duration=60)
q.add_webcam(duration=120)

q.play()                              # starts casting (non-blocking)
q.add("another.mp4", duration=300)    # add with 5-minute limit
q.remove(2)                           # remove item by index
q.skip()                              # skip to next item
q.stop()                              # stop and disconnect

s = q.status()
s.state               # "playing" | "stopped" | "idle"
s.now_playing         # "Never Gonna Give You Up"
s.duration            # 212.0 (seconds, None for live)
s.position            # 45.3 (seconds elapsed)
s.queue               # ["workout.mp4", "another.mp4"]
```

### Example: morning TV schedule

```python
from qast import Qast
import schedule, time

q = Qast(device="Office TV")

def morning():
    q.stop()
    q.add("https://youtube.com/watch?v=morning-news")
    q.add("https://youtube.com/watch?v=lofi-beats")
    q.play(repeat=True)

def afternoon():
    q.stop()
    q.add_screen()
    q.play()

schedule.every().day.at("08:00").do(morning)
schedule.every().day.at("13:00").do(afternoon)

while True:
    schedule.run_pending()
    time.sleep(60)
```

## Use cases

- **Screen share to any TV** — works even if your TV doesn't support Miracast or AirPlay
- **Security cam grid** — compose RTSP feeds with ffmpeg, pipe to TV
- **Office background** — queue up news, lo-fi streams, conference talks
- **Social gathering** — queue up varied sources from Youtube, Vimeo, Google Drive, Slideshare, and play on a loop
- **Movie marathon** — queue up the LOTR trilogy, watch without having to lift a finger. Put it on repeat: LOTR channel
- **Curated kids content** — queue up YouTube Kids, PBS, etc.
- **Digital signage** — Show "live" data, sales figures, number of users, household/company news, etc.
- **MagicMirror cast** — cast your [MagicMirror]https://github.com/MagicMirrorOrg/MagicMirror to any screen 
- **Etc** — pipe frames from your custom video source, e.g. art, AI generated content, etc.

## FAQ

**Why transcode everything?**

Compatibility. TVs are picky about codecs, containers, and parameters. A Samsung might play your MKV; an LG might not. By normalizing to H.264 + AAC in MPEG-TS (or fragmented MP4 for Chromecast), qast hits the lowest common denominator that every TV accepts. It also enables seamless queue transitions — uniform codec parameters mean no discontinuities between sources.

**Can I seek within a video?**

No. qast streams forward-only — it's designed for lean-back viewing. If you need seeking, consider using a casting app such as YouTube, which is supported on most TVs.

**What about DRM content?**

If yt-dlp can't extract it, qast can't play it. Netflix, Disney+, etc. use DRM that prevents this.

**My TV isn't discovered. What do I do?**

Make sure your TV and computer are on the same network/VLAN. Try `qast -v` to see discovery traffic. Some TVs need DLNA/casting enabled in settings. Roku requires "Control by mobile apps" to be enabled under Settings > System > Advanced.

**Does Roku require anything extra?**

Yes — install the free [Media Assistant](https://channelstore.roku.com/details/782875) app from the Roku Channel Store, and enable "Control by mobile apps" in Settings > System > Advanced.

## Upcoming features

- **Multi-device casting** — cast the same stream to multiple TVs simultaneously (`qast -d "Living Room" -d "Kitchen" video.mp4`)
- **Subtitles** — burn subtitles into the video stream via ffmpeg
- **Pause/resume** — pause and resume playback on the TV
- ~~**Webpage rendering** — cast any webpage to TV via headless Chromium~~ (done — `qast --browser URL`)
- **Scripting** — a simple script format for automated playback sequences with loops, durations, and mixed sources (`qast --script morning-tv.qast`)
- **Overlay/watermark** — add a visible overlay (aka watermark) to the video stream
- **Windows support**
- **macOS support (screen capture to come later)**

## License

MIT

## Why I built this

Our office has TVs of various types. During the Winter Olympics I had mixed results casting the live feed from my browser — sometimes it would work, sometimes not, and some TVs were completely undiscoverable. In the past our business has sought ways to display live numbers on TVs — user counts, sales figures, that sort of thing. We have Raspberry Pis, and that's a solution, but the pain factor is high.

Why can't I just "play this video" or "cast this window" to a given TV from the command line (and most importantly expect it to work)?

Looking into it more, I found that screen casting is often a paid service for businesses (Yodeck, Screenly, UPshow, many more). These solutions typically use Raspberry Pis coupled to a cloud backend. The technical hurdles are solved but it requires a paid subscription. Of course being a big ol nerd, it got me thinking about how easy it would be to just... (and thus qast was born.) I hope others find this tool useful. 

qast is pronounced "cast". The q is for queue — qast can play a queue of varied content back to back as one continuous stream. (And everyone knows replacing a c with q makes anything sound cooler.) 

## Related projects

qast leans heavily on existing projects.

- [ffmpeg](https://www.ffmpeg.org/) — transcoding, muxing, screen capture, window capture, placeholder video generation
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video extraction
- [pychromecast](https://github.com/home-assistant-libs/pychromecast) — Chromecast protocol

## See also

- [go2tv](https://github.com/alexballas/go2tv) — DLNA casting (single files)
- [catt](https://github.com/skorokithakis/catt) — Chromecast CLI
- [MagicMirror](https://github.com/MagicMirrorOrg/MagicMirror) — Configurable/programmable smart information display
