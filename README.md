# qast

qast casts anything to any TV from the command line. 

```bash
qast video.mov                                # Cast local file
qast "https://dropbox.com/abc123/video.mp4"   # Cast video located somewhere on web
qast "https://youtube.com/watch?v=..."        # Cast YouTube video
qast screen                                   # Cast your computer desktop
qast window                                   # Cast a window on your desktop (select via mouse click)
qast "browser:https://grafana.example.com"    # Cast a webpage (via headless Chromium)
qast webcam                                   # Cast your webcam
cat stream.ts | qast -                        # Cast generic piped data
qast url1 url2 url3 --repeat                  # Cast varied content, queued, and looped
```

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
<!-- run: npx doctoc /home/rich/qast/README.md --title '## Contents' -->
## Contents

- [The problem](#the-problem)
- [The solution](#the-solution)
- [Install](#install)
  - [Requirements](#requirements)
  - [Optional extras](#optional-extras)
- [Quick start](#quick-start)
- [What can you cast?](#what-can-you-cast)
  - [Anything on the internet](#anything-on-the-internet)
  - [Any file on your computer](#any-file-on-your-computer)
  - [Your screen/desktop](#your-screendesktop)
  - [A single window](#a-single-window)
  - [A webpage](#a-webpage)
  - [Your webcam](#your-webcam)
  - [Live TV streams](#live-tv-streams)
  - [Piped data](#piped-data)
- [Queue mode](#queue-mode)
  - [Per-item duration](#per-item-duration)
  - [Playlist files](#playlist-files)
- [Supported devices](#supported-devices)
- [CLI reference](#cli-reference)
- [Python API](#python-api)
  - [One-shot casting](#one-shot-casting)
  - [Queue-based playback](#queue-based-playback)
  - [Example: morning TV schedule](#example-morning-tv-schedule)
- [Use cases](#use-cases)
- [FAQ](#faq)
- [How it works](#how-it-works)
- [YouTube notes](#youtube-notes)
  - [YouTube blocking yt-dlp](#youtube-blocking-yt-dlp)
  - [Non-YouTube sites](#non-youtube-sites)
- [Upcoming features](#upcoming-features)
- [License](#license)
- [How did this come about?](#how-did-this-come-about)
- [Related projects](#related-projects)
- [See also](#see-also)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## The problem

Almost every TV made in the last decade can receive cast streams. But what they'll *accept* varies:

- Chromecast handles YouTube natively, but won't take an arbitrary URL
- Most DLNA TVs play MP4 files but won't play MKV or WebM
- Roku has varied mechanisms for streaming depending on version/vendor
- Screen mirroring exists on some platforms, not others

In other words, TV's streaming features differ. Straightforward "play this stream" reveals inconsistencies
 — content that plays fine on a Samsung may fail on an LG. Codec mismatches (VP9, H.265, DivX/Xvid), uncommon containers (MKV, WebM, FLV, AVI, OGG), and unsupported audio formats (FLAC, Opus, DTS) are common causes of "format not supported" errors. Even when a TV claims to support a format, it may only handle specific codec profiles or resolutions. 

## The solution

qast sidesteps the compatibility problem entirely. Practically all TVs accept either MPEG transport stream or fragmented MP4, so qast transcodes everything — URLs, files, screen captures, windows, webcams, piped data — into a single H.264/AAC stream. Input can be anything ffmpeg understands, which is practically every media format in existence. Because everything is transcoded to a common format, qast can play varied content (different sources, formats, and resolutions) back-to-back seamlessly. The TV sees one continuous stream with consistent format, resolution and bitrate throughout — content is added dynamically to a continuously-running mux, so there are no gaps or format switches between items. qast basically creates your own TV station from the command line.

## Install

```bash
pip install qast[all]   # recommended: includes yt-dlp, Chromecast, and browser capture
pip install qast        # core only: local files, screen/webcam/window capture, piped data
```

### Requirements

- Python 3.8+
- [ffmpeg](https://www.ffmpeg.org/) — transcoding and capture
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — for YouTube and [1000+ sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) (strongly recommended)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg
pip install yt-dlp      # or: included in qast[all]
```

### Optional extras

| Extra        | What it adds                                                        |
|--------------|---------------------------------------------------------------------|
| `qast[ytdlp]`      | [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube and [1000+ sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) |
| `qast[chromecast]`  | [pychromecast](https://github.com/home-assistant-libs/pychromecast) — Chromecast/Google TV support |
| `qast[browser]`     | [Playwright](https://playwright.dev/python/) — `browser:` capture (also run `playwright install chromium`) |
| `qast[all]`         | All of the above                                                    |

Optional system packages:
- xdotool — for `window` source on Linux (`apt install xdotool`)

## Quick start

```bash
# Cast a YouTube video
qast "https://youtube.com/watch?v=dQw4w9WgXcQ"

# Cast a local file
qast video.mov

# Cast your screen
qast screen

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

### Any file on your computer

```bash
qast video.mp4
qast ~/Videos/*.mp4
```

MP4, MKV, AVI, WebM, FLV, OGG, WMV — anything ffmpeg can read.

### Your screen/desktop

```bash
qast screen                    # primary monitor
qast screen --no-cursor        # hide mouse cursor
qast screen@5m                 # capture for 5 minutes
```

Works if your TV doesn't support Miracast or AirPlay.

### A single window

```bash
qast window                    # click to select
qast window:Grafana          # by title
qast window:Grafana@1m       # by title, 1 minute
```

### A webpage

```bash
qast browser:https://grafana.example.com/dashboard       # render any URL
qast browser:https://example.com@5m                      # stop after 5 minutes
```

Renders a URL in headless Chromium and casts the result to your TV. Great for dashboards, status pages, or any content that's best viewed as a live webpage rather than a video. Requires [Playwright](https://playwright.dev/python/) (`pip install playwright && playwright install chromium`).

### Your webcam

```bash
qast webcam                    # default camera
qast webcam@2m                 # capture for 2 minutes
```

### Live TV streams

HLS and IPTV streams work directly. Many international broadcasters stream free online but getting those streams onto your TV is a pain and sometimes requires a paid app. qast handles the HLS fetching and transcoding — you just give it the URL. See [iptv-org](https://iptv-org.github.io/) for a directory of free streams. Note, many of these streams are geo-blocked. Also note, aspect ratio often needs to be tweaked, see `--aspect` arg.  

```bash
# HLS streams (URLs are examples — check broadcaster sites for current links)
qast "https://tv-trtworld.medya.trt.com.tr/master.m3u8"
qast "https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8"
```

### Piped data

```bash
cat stream.ts | qast -
ffmpeg -i input.avi -f mpegts - | qast -
```

The `-` tells qast to read from stdin. This makes qast composable with any tool that outputs video.

**Audio visualizer from system audio:**
```bash
ffmpeg -loglevel quiet -f pulse -i $(pactl get-default-sink).monitor \
-filter_complex "[0:a]showcqt=s=1920x1080:axis_h=0:bar_g=2:count=6[v]" \
-map "[v]" -f mpegts - | qast -
```

Displays your music as a real-time frequency-based waterfall graph on your TV. Note, ffmpeg has lots and lots of these kinds of [visualizations](https://gist.github.com/yradunchev/1790b8aeffc784debe6479a53613e422). (But we're still waiting on --flight-simulator and --play-chess... C'mon ffmpeg!)

**Security cam grid:**
```bash
ffmpeg -loglevel quiet -i rtsp://cam1 -i rtsp://cam2 -i rtsp://cam3 -i rtsp://cam4 \
  -filter_complex "[0:v][1:v]hstack[top];[2:v][3:v]hstack[bottom];[top][bottom]vstack" \
  -f mpegts - | qast -
```
4 cameras on 1 TV, no NVR needed.

**Test pattern:**

```bash
ffmpeg -loglevel quiet -f lavfi -i "testsrc2=size=1920x1080:rate=30" -f mpegts - | qast -
```

**Generative art from Python:**
```bash
python my_visualizer.py | ffmpeg -f rawvideo -pix_fmt rgb24 -s 1920x1080 -r 30 -i - \
  -f mpegts - | qast -
```

## Queue mode

Pass multiple sources to play them back-to-back as one continuous stream:

```bash
qast \
  "https://youtube.com/watch?v=morning-news" \
  ~/Videos/workout.mp4 \
  "https://youtube.com/watch?v=lofi-beats"
```

### Per-item duration

Append `@duration` to any source to limit how long it plays:

```bash
qast video.mp4@5m                            # play for 5 minutes
qast screen@30s video.mp4@1m webcam@20s      # mixed sources with durations
qast "browser:https://grafana.example.com"@5m video.mp4     # browser capture then video
qast --duration 1m video1.mp4 video2.mp4@30s # global default + per-item override
```

The `@duration` can be a separate argument, useful for URLs containing `@`:

```bash
qast "https://user@host.com/video" @5m       # separate — unambiguous
```

Source syntax:
- `screen[@duration]` — screen capture
- `webcam[@duration]` — webcam capture
- `browser:<url>[@duration]` — headless browser capture
- `window:<title>[@duration]` — window capture by title
- `<url-or-file>[@duration]` — URL or local file


### Playlist files

Plain text, one source per line. Supports the same source syntax including `@duration`:

```
# morning.txt
https://youtube.com/watch?v=VIDEO1
https://youtube.com/watch?v=VIDEO2@10m
~/Videos/workout.mp4
screen@30s
browser:https://grafana.example.com@5m
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
  [0] Living Room TV (Chromecast)
  [1] Bedroom Samsung (DLNA)
  [2] Kitchen Roku (Roku)
Select device:
```

Or specify directly:

```bash
qast -d "Samsung" video.mp4         # by name (substring match)
qast -d 0 video.mp4                 # by index
```

## CLI reference

```
qast [OPTIONS] [SOURCE...]

Sources:
  <file>[@duration]                  Local video file
  <url>[@duration]                   YouTube, Vimeo, etc. (via yt-dlp)
  screen[@duration]                  Capture primary screen
  webcam[@duration]                  Capture default webcam
  browser:<url>[@duration]           Render a URL in headless Chromium and cast
  window:<title>[@duration]          Capture a window by title
  -                                  Read from stdin

  @duration can be attached (video.mp4@5m) or separate (video.mp4 @5m).

Device:
  -d, --device NAME|INDEX   Select device by name (substring) or index

Queue:
  --playlist FILE           Load sources from a file (- for stdin)
  --repeat                  Loop the queue indefinitely
  --shuffle                 Shuffle queue order
  --no-placeholder          Disable "up next" placeholder screens
  --preroll TIME            Preroll video by the specified time. This is useful for some 
                            TVs that either cut off the beginning of the first segment 
                            or show wait icon because of insufficient buffering.
  --placeholder-time TIME   Specify amount of time to show placeholders (2s default)                          
  --duration TIME           Default duration for sources without @duration 
                            (e.g., 30s, 5m, 1h, 5m30s)  

Capture:
  --no-cursor               Hide mouse cursor in screen capture

Other:
  --youtube-default         Use YouTube's default muxed stream instead of DASH
                            (lower latency, may be lower quality)
  --aspect                  Squish or stretch content. 1.0 default, >1.0 stretches,
                            <1.0 squishes
  --cookies-from-browser B  Extract cookies from browser (B=chrome, firefox, brave, edge,
                            or safari) — helps when YouTube blocks yt-dlp extraction
                            (uses your logged-in session)
  --save-stream FILE        Save the served stream to a file (fMP4 or TS, matching device
                            format)
  -v, --verbose             Debug logging
  -h, --help                Show help
```

During playback, you can type commands (with <CR>):

```
<source[@duration]>   add a source to the queue (URLs, screen, webcam, etc.)
s                     skip current item
r <N>                 remove item by index
?                     show queue status
q                     quit
```

## Python API

Full API reference: [api.md](api.md)

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

- **Screen share to any TV** — works even if your TV doesn't support Miracast or AirPlay.
- **Security cam grid** — compose RTSP feeds with ffmpeg, pipe to TV.
- **Social gathering** — queue up varied sources from Youtube, Vimeo, Google Drive, Slideshare, and play on a loop.
- **Movie marathon** — e.g. queue up the LOTR trilogy.
- **Curated kids content** — queue up appropriate kid content -- YouTube Kids, PBS, etc.
- **Digital signage** — Show "live" data, sales figures, number of users, company news, promotions, etc.
- **MagicMirror** — cast your [MagicMirror](https://github.com/MagicMirrorOrg/MagicMirror) screen wherever.
- **Etc** — pipe frames from your custom video source -- art, AI generated content, etc.

## FAQ

**Why transcode everything?**

Compatibility. TVs are picky about codecs, containers, and parameters. A Samsung might play your MKV; an LG might not. By normalizing to H.264 + AAC in MPEG-TS (or fragmented MP4 for Chromecast), qast hits the lowest common denominator that every TV accepts. It also enables seamless queue transitions — uniform codec parameters mean no discontinuities between sources. Modern PCs typically have hardware encode support, so CPU usage is kept reasonably low.  

**Can I seek within a video?**

No. qast streams forward-only — it's designed for lean-back viewing. If you need seeking, consider using a casting app such as YouTube, which is supported on most TVs.

**What about DRM content?**

If yt-dlp can't extract it, qast can't play it. Netflix, Disney+, etc. use DRM that prevents this. 

**My TV isn't discovered. What do I do?**

Make sure your TV and computer are on the same network/VLAN. Try `qast -v` to see discovery traffic. Some TVs need DLNA/casting enabled in settings. Roku requires "Control by mobile apps" to be enabled under Settings > System > Advanced.

**Does Roku require anything extra?**

Yes — install the free [Media Assistant](https://channelstore.roku.com/details/782875) app from the Roku Channel Store.

**My TV cuts off the beginning of the stream**

Some DLNA TVs consume and discard the first chunk of data when they connect — probing the format before they start rendering. This means the first few seconds of your video get eaten, and it can also disrupt the audio/video sync that follows. The `--preroll` flag works around this by inserting a placeholder video (a title card) at the start of the stream. The TV chews through the placeholder instead of your content. Start with `--preroll 5` and increase until you see the placeholder appear on screen — that means the TV is past its probe phase and your real content will play from the beginning. Some TVs need 30 seconds or more. Once you know how much preroll your TV needs, you can add it to all qast calls.

```bash
qast --preroll 30 "https://youtube.com/watch?v=..."
```

**Why am I seeing several seconds of latency?**

Practically all TVs want to buffer a few seconds of data before starting to render frames, which leads to latencies. For live streams such as webcam or computer desktop where latency matters most, you might see up to a 10 second lag from when you move your mouse and when you see it on your TV (for example). I'd like to improve this.


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

## YouTube notes

By default, qast asks yt-dlp for separate DASH video and audio streams when resolving YouTube URLs. DASH streams are higher quality — YouTube serves its best resolutions and bitrates this way, while muxed (combined) streams typically cap at 720p. The downside is that ffmpeg receives two HTTP inputs (one video, one audio), and there's a long-standing ffmpeg bug where multiple HTTP inputs can cause audio truncation or desync.

To work around this, qast downloads the audio stream to a small temp file (~1-2 MB) before handing it to ffmpeg. This way ffmpeg only has one HTTP input (video) and one local file (audio), which avoids the bug. The audio download is fast and the temp file is cleaned up automatically. If the download times out or fails, qast falls back to a single muxed stream automatically.

If you'd rather skip the DASH path entirely and use YouTube's default muxed stream (lower latency, simpler, but potentially lower resolution), use:

```bash
qast --youtube-default "https://youtube.com/watch?v=..."
```

Or via the Python API:

```python
cast("https://youtube.com/watch?v=...", device=0, youtube_default=True)
```

### YouTube blocking yt-dlp

YouTube periodically changes its player internals to break yt-dlp extraction. When this happens you'll see errors like "Sign in to confirm you're not a bot" or "Unable to extract" in yt-dlp's output. Two things help:

1. **Update yt-dlp** — the yt-dlp maintainers typically push fixes within days. Run `pip install -U yt-dlp`.
2. **Use browser cookies** — passing `--cookies-from-browser chrome` (or `firefox`, `brave`, `edge`, `safari`) lets yt-dlp use your logged-in YouTube session, which bypasses most bot detection. This is often the only fix until yt-dlp pushes an update.

```bash
qast --cookies-from-browser chrome "https://youtube.com/watch?v=..."
```

Note that cookie extraction reads from your browser's cookie store — it does not modify anything.

### Non-YouTube sites

yt-dlp supports [1000+ sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). For most of these, qast receives a single muxed URL and no audio download is needed. The DASH splitting behavior is specific to YouTube (and a few other sites that use DASH). If yt-dlp fails entirely for a given URL, qast passes the raw URL directly to ffmpeg as a last resort — this works surprisingly often for direct video links.


## Upcoming features

- **Multi-device casting** — cast the same stream to multiple TVs simultaneously (`qast -d "Living Room" -d "Kitchen" video.mp4`)
- **Subtitles** — burn subtitles into the video stream via ffmpeg
- **Scripting** — a simple script format for automated playback sequences with loops, durations, and mixed sources (`qast --script morning-tv.qast`)
- **Audio with visualization** — render audio only files with graphical visualization
- **Overlay/watermark** — add a visible overlay (aka watermark) to the video stream
- **Windows support**
- **macOS support (screen capture to come later)**

## License

MIT

## How did this come about?

Our office has TVs of various types. During the Winter Olympics I had mixed results casting the live feed from my browser — sometimes it would work, sometimes not, and some TVs were invisible to Chrome despite being stream capable. In the past our business has sought ways to display live numbers on TVs — user counts, sales figures, that sort of thing. We have Raspberry Pis, and that's a solution, but the pain factor with setting up is high.

Why can't I just "play this video" or "cast this window" to a given TV from the command line (and most importantly expect it to work)?

Looking into it more, I found that screen casting is often a paid service for businesses (Yodeck, Screenly, UPshow, many more). These solutions typically use Raspberry Pis coupled to a cloud backend. The technical hurdles are solved but it requires a paid subscription. Being a big ol nerd, it got me thinking -- could you make a streamer that's agnostic to both the video source/format and the TV type? And thus qast was born. I hope others find this tool useful. 

qast is pronounced "cast". The q is for queue -- play a queue of varied content back-to-back. (And everyone knows replacing a c with q makes anything sound cooler.) 

## Related projects

qast leans heavily on existing projects.

- [ffmpeg](https://www.ffmpeg.org/) — transcoding, muxing, screen capture, window capture, audio visualization, placeholder video encoding
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video extraction
- [pychromecast](https://github.com/home-assistant-libs/pychromecast) — Chromecast protocol
- [Playwright](https://playwright.dev/) — headless Chromium (browser) capture

## See also
- [Mkchromecast](https://github.com/muammar/mkchromecast) — Chromecast CLI utility, casts audio and video files
- [catt](https://github.com/skorokithakis/catt) — Chromecast CLI utility, casts urls and web pages
- [go2tv](https://github.com/alexballas/go2tv) — DLNA casting (single files)
- [MagicMirror](https://github.com/MagicMirrorOrg/MagicMirror) — Configurable/programmable smart information display
