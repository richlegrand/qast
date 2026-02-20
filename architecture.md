# casturl Architecture

casturl discovers media-capable devices on the local network and casts content to them. It supports three casting protocols (DLNA, Roku ECP, Chromecast) and handles four source types (small VOD, large VOD, live stream, direct URL). This document describes how each subsystem works and how they compose.

---

## 1. Discovery

All three discovery protocols run in parallel via `ThreadPoolExecutor` (`discover_all`). Results are merged, deduplicated by `(host, protocol)`, and sorted alphabetically. Every discovered device is normalized into a common dict:

```python
{
    "name":        str,   # friendly name
    "model":       str,   # model or manufacturer
    "protocol":    str,   # "dlna" | "roku" | "cast"
    "host":        str,   # IP address
    "port":        int,   # control port
    "cast_obj":    obj,   # pychromecast object (cast only, else None)
    "control_url": str,   # AVTransport SOAP endpoint (dlna only, else None)
}
```

### SSDP (DLNA and Roku)

DLNA and Roku both use SSDP (Simple Service Discovery Protocol) over UDP multicast at `239.255.255.250:1900`. They differ only in search target:

| Protocol | Search target (`ST:`) |
|----------|-----------------------|
| DLNA     | `urn:schemas-upnp-org:device:MediaRenderer:1` |
| Roku     | `roku:ecp` |

The flow is the same for both:

1. **Send M-SEARCH** — A UDP datagram with `MAN: "ssdp:discover"` and an `MX` header (capped at 5s per UPnP spec, tells devices the max random delay before responding). Sent 3 times upfront because UDP is unreliable.
2. **Collect responses** — Listen until the timeout deadline. Each response contains a `LOCATION:` header pointing to the device's description URL. On socket timeouts, re-send M-SEARCH and keep listening (catches slow responders like LG webOS TVs).
3. **Fetch device description** — HTTP GET the location URL.
   - **DLNA**: Parse UPnP XML (namespace `urn:schemas-upnp-org:device-1-0`) for `friendlyName`, `modelName`, and the `controlURL` of the `AVTransport` service. Devices without AVTransport are discarded.
   - **Roku**: Hit `/query/device-info` instead. Parse XML (no namespace) for `friendly-device-name` and `model-name`. The base URL (e.g. `http://192.168.2.73:8060/`) serves as the ECP endpoint.

### mDNS (Chromecast)

Chromecast and Google TV use mDNS/DNS-SD at `224.0.0.251:5353` — a completely separate multicast group from SSDP. Clients browse for the `_googlecast._tcp.local` service type and get back TXT records with device metadata plus a host/port for the Cast protocol (protobuf over TLS).

casturl delegates this entirely to `pychromecast` (which depends on the `zeroconf` library). If pychromecast is not installed, Cast discovery is silently skipped and DLNA/Roku still work. The `cast_obj` field holds the pychromecast device object needed for later control.

### Reliability

UDP packet loss is the main concern. The code mitigates this by:
- Sending M-SEARCH 3 times on initial burst
- Re-sending on every socket timeout within the deadline window
- Using a generous 15-second overall timeout (important for LG webOS, which is slow and intermittent)

---

## 2. Content Pipeline

When a URL is submitted, it flows through a decision tree that determines how the content is prepared and served. There are four paths.

### Path A: VOD, small file (<=200 MB)

Triggered when yt-dlp succeeds, the stream is not live, and estimated size is at or below `STREAM_THRESHOLD_MB` (200 MB). Typical source: a short YouTube video.

1. **yt-dlp download** — Downloads and merges the best H.264+AAC MP4 (format selector: `bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a] / bv[ext=mp4]+ba[ext=m4a] / b[ext=mp4] / b`). The `--merge-output-format mp4` flag ensures a single output file. The `--postprocessor-args "ffmpeg:-movflags +faststart"` flag relocates the moov atom to the front so the TV can start playing before the full file is buffered.
2. **File server** (`FileHandler`) — Serves the downloaded file over HTTP with:
   - `Content-Type: video/mp4`
   - `Content-Length` (known, file is complete)
   - `Accept-Ranges: bytes` with full HTTP 206 Partial Content support (TVs use Range requests for seeking)
   - DLNA headers: `contentFeatures.dlna.org` (flags `DLNA.ORG_OP=01` etc.) and `transferMode.dlna.org: Streaming`. Without these, LG and Samsung TVs may silently refuse to play.
3. **What reaches the TV**: A standard MP4 file (H.264 video, AAC audio, moov-at-front) served over HTTP with Range support. Fully seekable.

### Path B: VOD, large file (>200 MB)

Triggered when yt-dlp succeeds, the stream is not live, and estimated size exceeds 200 MB. Typical source: a long YouTube video, a large Dropbox-hosted file.

1. **yt-dlp extracts stream URLs** — `get_vod_stream_urls` pulls separate video and audio URLs from `info["requested_formats"]`. These are CDN URLs that yt-dlp would normally download and merge.
2. **ffmpeg copy-mux** (`start_vod_stream`) — ffmpeg reads both URLs in parallel and remuxes them into a single fragmented MP4 with no transcoding (`-c copy`). Key flags:
   - `-bsf:a aac_adtstoasc` — converts raw ADTS AAC headers to the format MP4 containers expect
   - `-movflags frag_keyframe+empty_moov+default_base_moof` — produces a fragmented MP4 that is playable from byte 0 (no moov atom to wait for)
3. **Buffer wait** — The code waits up to 15 seconds for ffmpeg to produce at least 1 MB of output before starting the cast.
4. **Stream server** (`LiveStreamHandler`) — Serves the growing file. Unlike `FileHandler`, this handler does not set `Content-Length` (the file is still growing) and does not support Range requests. It reads from the file and follows ffmpeg's writes, sleeping 100ms when it catches up, with a 30-second stall timeout.
5. **Fallback** — If separate stream URLs aren't available (single muxed format), or if ffmpeg fails, falls back to Path A (full download).
6. **What reaches the TV**: A fragmented MP4 (H.264 video, AAC audio) streamed over HTTP without Content-Length. Not seekable, but starts playing within seconds instead of waiting for a multi-GB download.

### Path C: Live stream

Triggered when `info["is_live"]` is true. Typical source: YouTube Live, Twitch.

1. **yt-dlp extracts HLS manifest** — `get_live_stream_url` looks for `manifest_url` or falls back to scanning formats for `m3u8_native` protocol entries.
2. **ffmpeg transcode** (`start_live_stream`) — Unlike the VOD copy-mux, live streams are transcoded:
   - `-c:v libx264 -preset ultrafast -tune zerolatency` — re-encodes video to H.264 (source may be VP9, AV1, etc. which TVs don't support)
   - `-c:a aac -b:a 128k` — re-encodes audio to AAC at 128kbps
   - `-re` — reads input at native frame rate (prevents ffmpeg from consuming the HLS feed faster than real-time)
   - Same fragmented MP4 movflags as Path B
3. **Buffer wait** — Waits for 512 KB of output (less than VOD because live latency matters).
4. **Stream server** — Same `LiveStreamHandler` as Path B.
5. **What reaches the TV**: A fragmented MP4 (H.264 video, AAC audio) streamed in real-time. Not seekable.

### Path D: Direct URL

Triggered when yt-dlp fails to recognize the URL (returns None). Typical source: a direct `.mp4` link, a Dropbox direct link, any URL that's already a playable media file.

1. **No local processing** — The URL is passed directly to the TV. No yt-dlp, no ffmpeg, no local HTTP server.
2. **Content type** — Guessed from the URL extension (`guess_content_type`), with a manual override prompt. Defaults to `video/mp4`.
3. **What reaches the TV**: Whatever the remote server provides. Seeking, codec support, etc. depend on the remote server and the TV's native capabilities.

### Summary table

| Path | Trigger | Processing | Server | Seekable | Codec guarantee |
|------|---------|-----------|--------|----------|-----------------|
| A: Small VOD | yt-dlp ok, <=200 MB | yt-dlp download, faststart | `FileHandler` (Range support) | Yes | H.264/AAC MP4 |
| B: Large VOD | yt-dlp ok, >200 MB | ffmpeg copy-mux (no transcode) | `LiveStreamHandler` (growing file) | No | H.264/AAC fMP4 |
| C: Live | yt-dlp ok, `is_live` | ffmpeg transcode (libx264/aac) | `LiveStreamHandler` (growing file) | No | H.264/AAC fMP4 |
| D: Direct | yt-dlp fails | None | None (pass-through) | Depends | Depends |

---

## 3. Casting (per protocol)

### DLNA (UPnP AVTransport SOAP)

Casting uses two SOAP actions sent to the device's `control_url`:

1. **SetAVTransportURI** — Body includes:
   - `<CurrentURI>` — the HTTP URL of the media
   - `<CurrentURIMetaData>` — HTML-escaped DIDL-Lite XML containing:
     - `<dc:title>casturl</dc:title>`
     - `<res protocolInfo="http-get:*:video/mp4:*">URL</res>`
     - `<upnp:class>object.item.videoItem</upnp:class>`
2. **Play** — Sent with `<Speed>1</Speed>`. Retried up to 3 times with 1-second delays because some TVs (LG webOS) return HTTP 500 if Play arrives too soon after SetAVTransportURI.

SOAP envelope format: `SOAPAction` header is `"urn:schemas-upnp-org:service:AVTransport:1#ActionName"`, body is XML with `<InstanceID>0</InstanceID>` plus action-specific arguments.

### Roku (ECP)

Two casting modes (though native app launch is currently disabled — see Section 5):

- **Native app launch** (disabled): `POST /launch/837?contentId=VIDEO_ID&MediaType=live` — launches the YouTube channel. Empty POST body, no XML.
- **Media Player input**: `POST /input/15985?t=v&u=ENCODED_URL&videoName=casturl&videoFormat=mp4` — sends a URL to Roku Media Player for direct playback.

### Chromecast

Uses `pychromecast`'s media controller:

```python
cc.media_controller.play_media(url, content_type, stream_type="LIVE"|"BUFFERED")
mc.block_until_active(timeout=60)
```

The `stream_type` parameter tells the Chromecast whether the content is live (no seek bar) or buffered (seek bar shown). The underlying Cast protocol uses protobuf messages over a TLS channel to the device — pychromecast handles all of this.

---

## 4. Playback Monitoring & Completion Detection

Each protocol has its own polling loop. All three share the same structure: poll in a loop, detect finished/error states, handle Ctrl+C for manual stop.

### DLNA

- **Poll method**: SOAP `GetTransportInfo` every 3 seconds
- **State field**: `CurrentTransportState` in the response XML
- **Finished**: `STOPPED` or `NO_MEDIA_PRESENT`
- **Error**: Connection failure to control URL (logged as "Device disconnected")
- **Normal states**: `PLAYING`, `TRANSITIONING`, `PAUSED_PLAYBACK`
- **Ctrl+C**: Sends SOAP `Stop` action, then exits
- **Reliability**: Solid. DLNA gives clear finished signals for locally-served content.

### Roku

Two sub-modes depending on how the cast was initiated:

**System media player** (used when serving our own content):
- **Poll method**: `GET /query/media-player` every 3 seconds
- **State field**: `state` attribute on the root XML element
- **Finished**: `state="close"`
- **Normal states**: `play`, `pause`, `buffer`
- **Error**: Connection failure (logged as "Device disconnected")
- **Ctrl+C**: `POST /keypress/Stop`, then exits
- **Reliability**: Works well for content served via our pipeline.

**Native app** (currently disabled — see Section 5):
- **Poll method**: `GET /query/active-app` every 5 seconds
- **State field**: `id` attribute on the `<app>` element
- **Finished**: Active app ID no longer matches the launched app ID (means user navigated away)
- **Limitation**: Cannot detect when a video within the app finishes playing. Only detects when the entire app is exited. This is the fundamental reason native app launches are disabled.
- **Ctrl+C**: `POST /keypress/Stop`, then exits

### Chromecast

- **Poll method**: `mc.update_status()` every 3 seconds (updates pychromecast's internal state)
- **State field**: `mc.status.player_state`
- **Finished**: `player_state == "IDLE"` and `idle_reason == "FINISHED"`
- **Error**: `player_state == "IDLE"` and `idle_reason == "ERROR"`, or connection failure
- **Buffering timeout**: If the device stays in `BUFFERING` state for over 60 seconds, playback is stopped (prevents hanging on broken streams)
- **Normal states**: `PLAYING`, `BUFFERING`, `PAUSED`
- **Ctrl+C**: `mc.stop()`, then exits
- **Reliability**: Very reliable. The Cast protocol provides explicit finished/error signals.

### Summary table

| Protocol | Poll endpoint | Interval | Finished signal | Error signal |
|----------|--------------|----------|-----------------|-------------|
| DLNA | SOAP GetTransportInfo | 3s | `STOPPED` / `NO_MEDIA_PRESENT` | Connection failure |
| Roku (media player) | GET /query/media-player | 3s | `state="close"` | Connection failure |
| Roku (native app) | GET /query/active-app | 5s | App ID changed (not a true "finished") | Connection failure |
| Chromecast | mc.update_status() | 3s | `IDLE` + `FINISHED` | `IDLE` + `ERROR` or connection failure |

---

## 5. Queue Architecture

### Two modes

casturl operates in two modes with different pipelines:

- **Single-item mode** (current): Uses the multi-path pipeline described in Section 2. Paths A-D give the best tradeoffs for one-off casting — copy-mux preserves quality, faststart enables seeking, direct URLs avoid unnecessary processing.
- **Queue mode** (planned): Uses a single always-transcode pipeline that produces one continuous stream per queue. This is a fundamentally different architecture, described below.

### The continuous composite stream

In queue mode, the TV receives a single cast command pointing to a single URL. Behind that URL is one continuous fragmented MP4 stream that contains all queue items with placeholder messages between them:

```
[video0] → [placeholder: "Getting xyz ready..."] → [video1] → [placeholder] → [video2] → ...
```

From the TV's perspective, it is playing one never-ending video. There are no stop/start boundaries, no re-casting, no protocol-specific "replace current media" logic. The stream just keeps going until the queue is exhausted or the user hits Ctrl+C.

### Why always-transcode

The continuous stream requires that every segment — real video and placeholder — produces compatible fMP4 fragments. Fragmented MP4 locks in codec parameters (H.264 SPS/PPS, AAC sample rate) in the initial `moov` box, and all subsequent `moof`+`mdat` fragments must conform. If video0 is 1080p30 and video1 is 720p60, concatenating their fragments breaks the container.

The solution: transcode everything to a uniform output format.

**Standard output parameters:**
- Video: H.264 Main profile, 1080p, 30fps, `-preset ultrafast`
- Audio: AAC stereo, 128kbps
- Container: fragmented MP4 (`-movflags frag_keyframe+empty_moov+default_base_moof`)

This is the same approach Plex uses — transcode for control and compatibility, regardless of source format. The existing live stream path (`start_live_stream`) already does exactly this; the queue pipeline generalizes it to all content.

**Tradeoffs vs. copy-mux:**
- **CPU**: Real work, but `ultrafast` preset handles 1080p in real-time on any modern machine. Hardware encoding (Intel Quick Sync via `h264_qsv`, VA-API via `h264_vaapi`) is available as a future optimization.
- **Quality**: Generation loss from re-encoding H.264→H.264. Imperceptible on a TV at couch distance at 8-10 Mbps. Sources that are already H.264/AAC (most YouTube content) see minimal degradation.
- **Seeking**: Not supported within the continuous stream. Acceptable for playlist playback.
- **Startup latency**: Slightly higher than copy-mux, but masked by the placeholder and the fragmented MP4's ability to start playback after the first MB of output.

**What we gain:**
- **No completion detection needed.** The hardest problem in Section 4 — per-protocol polling, the Roku native app limitation, the "every URL must go through our pipeline" constraint — disappears entirely. We don't poll the TV to know when video0 finishes. ffmpeg knows: when its input is consumed, we feed it the next segment.
- **One code path.** Paths A-D collapse into one: yt-dlp resolves → ffmpeg transcodes → fMP4 → `LiveStreamHandler`.
- **Seamless transitions.** No flicker between items, no risk of the TV dropping to its home screen.
- **Guaranteed compatibility.** We control exactly what codec profile, level, and parameters reach the TV. No more worrying about whether the TV handles the source's H.264 High@5.1 or 10-bit color.
- **Placeholder messages are free.** Just more frames in the same stream, generated via ffmpeg's `lavfi` input (`color` + `drawtext` filter).

### Queue pipeline

For each queue item:

1. **yt-dlp** resolves the URL → extracts stream URL(s) or HLS manifest
2. **ffmpeg** transcodes to the standard output parameters → fragmented MP4 appended to the growing output file
3. Between items, **ffmpeg** generates placeholder frames ("Getting xyz ready...") → same output file
4. **`LiveStreamHandler`** serves the growing file over one persistent HTTP connection to the TV

The TV is cast to once at the start. The stream continues until the queue ends.

### Concatenation mechanism

The technical challenge is appending fragments from separate ffmpeg runs into one valid fMP4 file. Since all segments share identical codec parameters, the approach is:

1. First segment writes the full fMP4 header (`ftyp` + `moov` with `empty_moov`) followed by `moof`+`mdat` fragments
2. Subsequent segments are transcoded to fMP4 separately
3. Strip the `ftyp`/`moov` boxes (they're redundant — codec params are identical) and append only the `moof`+`mdat` fragments to the output file

The MP4 box format is simple (4-byte big-endian size + 4-byte type + payload), so the "parser" to skip headers and extract fragments is minimal.

### Placeholder generation

Between queue items, ffmpeg generates a status video using virtual inputs:

```
ffmpeg -f lavfi -i "color=c=0x1a1a2e:s=1920x1080:d=5"
  -vf "drawtext=text='Getting xyz ready...':fontsize=60:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2"
  -c:v libx264 -preset ultrafast -tune stillimage
  -c:a aac -ar 44100 -ac 2
  -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof
  placeholder.mp4
```

No real media input needed. The `drawtext` filter requires ffmpeg built with `--enable-libfreetype` (standard in most distro packages). Fallback: bake a static PNG into the project and wrap it in a video container.

### Why not the continuous stream for single items

Single-item casting keeps the multi-path pipeline from Section 2 because:

- **Seeking matters.** Path A downloads the full file and serves it with Range support. The continuous stream is forward-only.
- **Copy-mux is faster.** No CPU spent on transcoding when it's not needed.
- **No queue = no concatenation problem.** There's only one video, so codec parameter uniformity is irrelevant.

### Why native app launches are avoided

The Roku YouTube shortcut is disabled in `main()` (commented out with an explicit note). Launching YouTube natively on a Roku (`POST /launch/837`) gives no playback-finished signal — `/query/media-player` only tracks the system media player, not in-app players. In queue mode this is doubly irrelevant since the TV never sees individual items, but even in single-item mode native app launches are avoided to maintain uniform pipeline behavior.
