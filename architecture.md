# casturl Architecture

casturl discovers media-capable devices on the local network and casts content to them. It supports three casting protocols (DLNA, Roku ECP, Chromecast). Every URL — whether a single video or one of many in a queue — flows through the same pipeline: yt-dlp resolves the source, ffmpeg transcodes and muxes to MPEG-TS, a master muxer remuxes to fragmented MP4, and an HTTP server streams it to the TV. Nothing touches disk.

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

## 2. The Pipeline

Every URL flows through the same pipeline. A single video is just a queue of one.

```
                       pipe                              stdout
[segment ffmpeg] ─── MPEG-TS ───→ [master muxer ffmpeg] ── fMP4 ──→ [stream buffer] ──→ [HTTP handler] ──→ TV
  (transcode+mux)                    (remux, -c copy)               (in-memory)
```

### Processes

Three processes at steady state:

**Segment ffmpeg** (short-lived, one per queue item) — Reads the source directly from the network (one `-i` for a muxed source, two `-i` for separate video+audio URLs like YouTube DASH). Transcodes video to H.264 and audio to AAC. Muxes both into MPEG-TS and writes to stdout. Exits when the source is consumed.

```
# Muxed source (HLS, direct URL, etc.)
ffmpeg -i <source_url>
  -c:v libx264 -preset ultrafast -s 1920x1080 -r 30
  -c:a aac -ar 44100 -ac 2 -b:a 128k
  -output_ts_offset <cumulative>
  -f mpegts pipe:1

# Separate video+audio (YouTube DASH)
ffmpeg -i <video_url> -i <audio_url>
  -c:v libx264 -preset ultrafast -s 1920x1080 -r 30
  -c:a aac -ar 44100 -ac 2 -b:a 128k
  -output_ts_offset <cumulative>
  -f mpegts pipe:1
```

ffmpeg handles the input regardless of container format (MP4, WebM, MKV, HLS, whatever). It demuxes, transcodes, and muxes to MPEG-TS — all in one process, fetching from the network itself. For live streams, `-re` is added to read at native frame rate.

**Master muxer ffmpeg** (long-lived, one per session) — Reads MPEG-TS from stdin, remuxes to fragmented MP4, writes to stdout. This is a `-c copy` operation (no re-encoding), so it's nearly zero CPU — just container format conversion.

```
ffmpeg -f mpegts -i pipe:0
  -c copy
  -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof
  pipe:1
```

**Python orchestrator** — Manages everything else:
- Creates a pipe connecting segment ffmpeg stdout → master muxer stdin
- Holds the write end of the pipe open across segment transitions (so the master muxer doesn't see EOF between items)
- Reads master muxer stdout into the stream buffer (reader thread)
- Runs the HTTP server (server thread)
- Spawns/manages segment ffmpeg processes in sequence
- Runs yt-dlp to resolve and prefetch upcoming URLs
- Tracks cumulative PTS offset, passes `-output_ts_offset` to each segment

### Stream buffer

An in-memory ring buffer sits between the master muxer's output and the HTTP handler. Two configurable parameters:

- **`buffer_max`** (default 32 MB) — Maximum buffer size. When full, back-pressure pauses the master muxer until the TV consumes data. Prevents unbounded memory growth if the TV stalls.
- **`buffer_min`** (default 1 MB) — Minimum fill before serving begins. The HTTP handler blocks until this threshold is reached, ensuring the TV has enough data to start playing smoothly. The cast command is sent to the TV only after this threshold is met.

### A/V sync

MPEG-TS is the intermediate format between the segment ffmpeg and the master muxer because it carries PTS timestamps for both audio and video. The segment ffmpeg muxes audio and video together with proper timing — this is what muxers do. The master muxer reads those timestamps and preserves them when remuxing to fMP4. A/V sync is maintained end to end by the container format, never inferred from frame counts.

This is the same pattern yt-dlp uses when merging separate YouTube video+audio streams: let ffmpeg handle the container, trust the timestamps it produces.

### Continuous timestamps across segments

Each segment ffmpeg receives `-output_ts_offset <cumulative_duration>` so its MPEG-TS timestamps continue where the previous segment left off. Python tracks the running offset. The master muxer sees a continuously increasing timestamp sequence — no discontinuities, no drift.

### Why always-transcode

The continuous stream requires uniform codec parameters. If video0 is 1080p30 and video1 is 720p60, the master muxer's fMP4 output would break mid-stream. Every segment transcodes to the same standard:

- Video: H.264 Main profile, 1080p, 30fps, `-preset ultrafast`
- Audio: AAC stereo, 44100 Hz, 128kbps

This is the same approach Plex uses — transcode for control and compatibility. The tradeoffs are mild:

- **CPU**: `ultrafast` handles 1080p in real-time on any modern machine. Hardware encoding (Intel Quick Sync, VA-API) is a future optimization.
- **Quality**: Generation loss from re-encoding. Imperceptible on a TV at couch distance at 8-10 Mbps.
- **Seeking**: Not supported in the continuous stream. Rarely missed for TV viewing.

What we gain:

- **One code path.** No branching based on source type, file size, or live/VOD.
- **Guaranteed compatibility.** We control exactly what reaches the TV.
- **Seamless queue transitions.** The TV sees one continuous video.
- **No completion detection.** We don't poll the TV. The segment ffmpeg exits when done, and Python starts the next one.
- **No disk I/O.** The entire data path stays in memory.

---

## 3. Segment Lifecycle

### URL resolution

For each queue item, yt-dlp probes the URL and extracts source information. This can run in the background while the previous item is still playing (prefetch). yt-dlp determines:

- Whether the source is live or VOD
- Stream URL(s) — either a single muxed URL, separate video+audio CDN URLs, or an HLS manifest
- Metadata — title, duration, estimated size (used for display, not routing decisions)

The format selector `bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a] / bv[ext=mp4]+ba[ext=m4a] / b[ext=mp4] / b` prefers H.264+AAC sources to minimize the transcoding work, but the pipeline handles any codec since ffmpeg transcodes everything anyway.

If yt-dlp fails (direct URL to an already-playable file), the URL is passed directly to ffmpeg as input. ffmpeg can handle most direct media URLs natively.

### Transcoding and muxing

The segment ffmpeg reads the source from the network, transcodes to H.264+AAC, and muxes into MPEG-TS on stdout. For separate video+audio sources (YouTube DASH), ffmpeg takes two `-i` arguments; for everything else, one `-i`. Either way, the output is interleaved MPEG-TS with proper PTS timestamps.

Each segment ffmpeg naturally starts its output with an IDR frame (keyframe) plus SPS/PPS parameters, which is the default behavior when a new encoding session begins.

### Completion and advancement

When a segment ffmpeg exits (input consumed or live stream ended):

1. Python detects this via `poll()` or `wait()` on the process
2. If the next item is already resolved, Python immediately spawns a new segment ffmpeg — seamless transition
3. If the next item is still resolving, Python spawns a placeholder segment to fill the gap (see Section 4)
4. If the queue is exhausted, Python closes the write end of the pipe. The master muxer reads remaining buffered data and exits. The HTTP handler drains the stream buffer and the TV reaches the natural end of the stream.

---

## 4. Placeholders and Status Display

Between queue items (or while the first item buffers), a placeholder segment ffmpeg writes to the same pipe as any other segment. It uses ffmpeg's `lavfi` virtual inputs — no real media source, no network, no disk.

### Basic text on solid color

```
ffmpeg -f lavfi -i "color=c=0x1a1a2e:s=1920x1080:r=30:d=30"
  -f lavfi -i "anullsrc=r=44100:cl=stereo"
  -vf "drawtext=text='Up next\: Breaking Bad S03E05':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2"
  -c:v libx264 -preset ultrafast -tune stillimage
  -c:a aac -ar 44100 -ac 2 -b:a 128k
  -shortest
  -output_ts_offset <cumulative>
  -f mpegts pipe:1
```

### Multi-line layouts

Multiple `drawtext` filters can be stacked for richer displays:

```
-vf "drawtext=text='Up next':fontsize=36:fontcolor=gray:x=(w-text_w)/2:y=(h/2-80),
     drawtext=text='Breaking Bad S03E05':fontsize=56:fontcolor=white:x=(w-text_w)/2:y=(h/2),
     drawtext=text='2 of 7':fontsize=28:fontcolor=gray:x=(w-text_w)/2:y=(h/2+80)"
```

### Overlay on real content

Placeholders don't have to be plain screens. Options include:

- **Looping background video** — a short ambient clip looped with `-stream_loop -1` as the input instead of `color`
- **Thumbnail of next video** — extract a frame from the upcoming source (`ffmpeg -ss 30 -frames:v 1`) and use it as the background, giving a YouTube-style "up next" preview
- **"Up next" overlay on the current video** — use drawtext's `enable` parameter to show text only in the last N seconds of the current item: `-vf "drawtext=text='Up next\: ...':enable='gte(t,DURATION-10)'"`. While the banner is showing, the next item is already being prepared in the background. This reduces the visible gap to zero when prefetching works.

### Dependency

The `drawtext` filter requires ffmpeg built with `--enable-libfreetype` (standard in most distro packages). Fallback: use a static PNG as the background instead of generated text.

---

## 5. Casting (per protocol)

The TV receives one cast command at session start, pointing to the HTTP handler's URL. The same command works whether the session contains one video or a full queue.

### DLNA (UPnP AVTransport SOAP)

Casting uses two SOAP actions sent to the device's `control_url`:

1. **SetAVTransportURI** — Body includes:
   - `<CurrentURI>` — the HTTP handler URL
   - `<CurrentURIMetaData>` — HTML-escaped DIDL-Lite XML containing:
     - `<dc:title>casturl</dc:title>`
     - `<res protocolInfo="http-get:*:video/mp4:*">URL</res>`
     - `<upnp:class>object.item.videoItem</upnp:class>`
2. **Play** — Sent with `<Speed>1</Speed>`. Retried up to 3 times with 1-second delays because some TVs (LG webOS) return HTTP 500 if Play arrives too soon after SetAVTransportURI.

SOAP envelope format: `SOAPAction` header is `"urn:schemas-upnp-org:service:AVTransport:1#ActionName"`, body is XML with `<InstanceID>0</InstanceID>` plus action-specific arguments.

### Roku (ECP)

Content is sent to Roku Media Player:

- `POST /input/15985?t=v&u=ENCODED_URL&videoName=casturl&videoFormat=mp4` — sends the HTTP handler URL for playback.

Native app launches (e.g. YouTube channel 837) are not used. Launching a native app provides no playback state feedback — `/query/media-player` only tracks the system media player, not in-app players. Native apps also can't be pointed at our HTTP server, so they're incompatible with the pipeline regardless.

### Chromecast

Uses `pychromecast`'s media controller:

```python
cc.media_controller.play_media(url, "video/mp4", stream_type="LIVE")
mc.block_until_active(timeout=60)
```

The `stream_type` is always `"LIVE"` since the continuous stream is forward-only (no seek bar). The underlying Cast protocol uses protobuf messages over a TLS channel — pychromecast handles all of this.

---

## 6. Session Monitoring

The continuous stream means the TV never reaches a "finished" state mid-session — it's always playing. There is no per-protocol polling to detect when a video ends. Queue advancement is driven entirely by the local pipeline (segment ffmpeg exits → Python starts the next one).

The monitoring that remains:

### Ctrl+C (user stop)

Kill the master muxer ffmpeg, which closes the stream. Then send a protocol-specific stop command as a courtesy:

| Protocol | Stop command |
|----------|-------------|
| DLNA | SOAP `Stop` action |
| Roku | `POST /keypress/Stop` |
| Chromecast | `mc.stop()` |

### TV disconnect

The HTTP handler detects a `BrokenPipeError` or `ConnectionResetError` when the TV closes the HTTP connection (user switched input, powered off, etc.). Python uses this as a signal to tear down the pipeline — kill the segment ffmpeg and master muxer.

### Session end

After the last segment ffmpeg exits, Python closes the write end of the pipe. The master muxer reads any remaining buffered data, writes final output to stdout, then exits. The HTTP handler drains the stream buffer and closes the HTTP response. The TV reaches the natural end of the stream, triggering its normal finished state (`STOPPED` on DLNA, `state="close"` on Roku, `IDLE`+`FINISHED` on Chromecast).
