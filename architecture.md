# qast Architecture

qast discovers media-capable devices on the local network and casts content to them. It supports three casting protocols (DLNA, Roku ECP, Chromecast). Every URL — whether a single video or one of many in a queue — flows through the same pipeline: yt-dlp resolves the source, ffmpeg transcodes to MPEG-TS, a TS rewriter ensures PTS/DTS continuity across segments, and for Chromecast a master muxer remuxes to fragmented MP4. An HTTP server streams the result to the TV. The main data path stays in memory — the one exception is YouTube DASH audio, which is downloaded to a small temp file to work around an ffmpeg bug (see YouTube notes in the README).

---

## 1. Discovery

All three discovery protocols run in parallel via `ThreadPoolExecutor` (`discover_all`). Results are merged, deduplicated by `(host, protocol)`, and sorted alphabetically. When a host appears on both Cast and DLNA, the DLNA entry is dropped (Cast is more robust). Every discovered device is normalized into a `Device` dataclass:

```python
@dataclass
class Device:
    name: str               # friendly name
    model: str              # model or manufacturer
    protocol: str           # "dlna" | "roku" | "cast"
    host: str               # IP address
    port: int               # control port
    cast_obj: Any = None    # pychromecast object (cast only)
    control_url: str = None # AVTransport SOAP endpoint (dlna only)
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

qast delegates this entirely to `pychromecast` (which depends on the `zeroconf` library). If pychromecast is not installed, Cast discovery is silently skipped and DLNA/Roku still work. The `cast_obj` field holds the pychromecast device object needed for later control.

### Reliability

UDP packet loss is the main concern. The code mitigates this by:
- Sending M-SEARCH 3 times on initial burst
- Re-sending on every socket timeout within the deadline window
- Using a generous 15-second overall timeout (important for LG webOS, which is slow and intermittent)

---

## 2. The Pipeline

Every URL flows through the same pipeline. A single video is just a queue of one.

```
Chromecast path:
[segment ffmpeg] → MPEG-TS → [TS rewriter] → [master muxer ffmpeg] → fMP4 → [ring buffer] → [HTTP handler] → TV

DLNA / Roku path:
[segment ffmpeg] → MPEG-TS → [TS rewriter] → [ring buffer] → [HTTP handler] → TV
```

For Chromecast, a master muxer remuxes MPEG-TS to fragmented MP4. For DLNA and Roku (`raw_ts=True`), the master muxer is skipped and rewritten MPEG-TS is written directly to the ring buffer — DLNA/Roku renderers handle MPEG-TS natively and this avoids issues with LG TVs rejecting fMP4.

### Processes

**Segment ffmpeg** (short-lived, one per queue item) — Reads the source directly from the network (one `-i` for a muxed source, two `-i` for separate video+audio URLs like YouTube DASH). Transcodes video to H.264 and audio to AAC. Outputs MPEG-TS to stdout. Exits when the source is consumed.

```
# Muxed source (HLS, direct URL, etc.)
ffmpeg -i <source_url>
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"
  -c:v libx264 -preset ultrafast -b:v 5M -r 30 -g 60
  -c:a aac -ar 44100 -ac 2 -b:a 128k
  -shortest -muxdelay 0 -muxpreload 0 -flush_packets 1
  -f mpegts pipe:1

# Separate video+audio (YouTube DASH)
ffmpeg -i <video_url> -i <audio_url>
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"
  -c:v libx264 -preset ultrafast -b:v 5M -r 30 -g 60
  -c:a aac -ar 44100 -ac 2 -b:a 128k
  -shortest -muxdelay 0 -muxpreload 0 -flush_packets 1
  -f mpegts pipe:1
```

ffmpeg handles the input regardless of container format (MP4, WebM, MKV, HLS, whatever). It demuxes, transcodes, and muxes to MPEG-TS — all in one process, fetching from the network itself. For live streams, `-re` is added to read at native frame rate. The `-vf` filter chain scales the source to fit within 1920x1080 while preserving aspect ratio (letterbox/pillarbox with black bars). An optional `setsar` filter handles aspect ratio correction (`--aspect` flag).

**TS rewriter** (Python, in-process) — Processes raw MPEG-TS packets (188 bytes each), rewriting PTS/DTS/PCR timestamps and continuity counters to ensure seamless continuity across segment boundaries. Operates at the TS packet level using exact 90 kHz integer ticks — no float-based computation. This replaces the old approach of passing `-output_ts_offset` to each segment ffmpeg.

**Master muxer ffmpeg** (long-lived, Chromecast only) — Reads rewritten MPEG-TS from stdin, remuxes to fragmented MP4, writes to stdout. This is a `-c copy` operation (no re-encoding), so it's nearly zero CPU — just container format conversion. Skipped entirely for DLNA/Roku.

```
ffmpeg -f mpegts -i pipe:0
  -c copy -bsf:a aac_adtstoasc
  -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof
  pipe:1
```

**Python orchestrator** — Manages everything else:
- Pipes segment ffmpeg stdout through the TS rewriter to the sink (master muxer stdin for Chromecast, ring buffer for DLNA/Roku)
- Runs the HTTP server (server thread)
- Spawns/manages segment ffmpeg processes in sequence
- Runs yt-dlp to resolve and prefetch upcoming URLs
- Tracks cumulative PTS offset via the TS rewriter
- Applies video PTS correction (Chromecast only) to compensate for audio clock drift in fMP4

### Stream buffer

An in-memory ring buffer sits between the pipeline output and the HTTP handler. The ring buffer has byte-based bounds (`buffer_max` = 64 MB, `buffer_min` = 4 MB) as safety limits, but the primary flow control is time-based.

### Frame counting

Byte counts are a poor proxy for time — a static title card compresses to almost nothing while a complex scene may spike. Instead, the pipeline counts video frames as they flow through the TS rewriter.

The TS rewriter parses every 188-byte MPEG-TS packet. When it encounters a PES header with a video stream ID (`0xE0`–`0xEF`) and the payload-unit-start flag set, that's one video frame. The rewriter exposes `video_frame_count` (per-segment) and the pipeline accumulates these into `_total_video_frames` across the session. Dividing by fps (30) gives content-time in seconds — a measurement that's exact regardless of compression ratio or encoding speed.

This frame count drives several things:

**Steady-state throttle**: The bridge thread compares content-time (`total_video_frames / fps`) against wall-time (elapsed since the TV's first read). When content-time leads wall-time by more than `MAX_BUFFER_LEAD` (10 seconds), the bridge blocks until the TV catches up. ffmpeg with `-preset ultrafast` encodes much faster than real-time for most sources, so without the throttle the buffer would grow unboundedly.

**Placeholder duration**: Placeholders (loading screens, "up next" cards) encode extremely fast since they're near-static frames. Byte count would give inconsistent display times. Instead, the pipeline pumps exactly N frames (e.g. `min_seconds * fps`) through the rewriter, then stops — guaranteeing a precise content-time duration. Placeholder frames are tracked separately (`_placeholder_frames`) so they don't inflate the throttle's content-time calculation.

**Readiness**: For capture sources (screen, webcam, window), the pipeline waits for at least one GOP's worth of video frames before casting, which gives a reliable time guarantee. For URL/file sources, readiness falls back to byte-based (`buffer_min`).

**Elapsed time**: The `elapsed` property (used by the status API and console) is `_item_video_frames / fps` — per-item frame count reset at each segment boundary.

**Capture buffers**: Screen/webcam capture uses smaller, time-derived ring buffer sizes (4 seconds min, 8 seconds max, converted to bytes via bitrate) for lower latency.

### A/V sync

MPEG-TS carries PTS timestamps for both audio and video. The segment ffmpeg muxes audio and video together with proper timing — this is what muxers do. A/V sync is maintained end to end by the container format, never inferred from frame counts.

For Chromecast (fMP4 path), the master muxer preserves timestamps when remuxing. However, Chromecast plays audio at a fixed 44100 Hz sample rate regardless of PTS values, which causes cumulative audio/video drift across segment boundaries. The pipeline compensates by computing a video PTS correction at each segment boundary based on the actual audio sample count vs video PTS position.

For DLNA/Roku (raw TS path), no correction is needed — the decoder uses PTS for both audio and video timing, so there's no clock drift.

### Continuous timestamps across segments

The TS rewriter maintains a running PTS/DTS offset across all segments. At each segment boundary, it advances the offset to the next video frame boundary (aligned to 90 kHz ticks). Each new segment's timestamps are rewritten on the fly to continue where the previous segment left off. The downstream consumer (master muxer or ring buffer) sees a continuously increasing timestamp sequence — no discontinuities, no drift.

### Why always-transcode

The continuous stream requires uniform codec parameters. If video0 is 1080p30 and video1 is 720p60, the master muxer's fMP4 output would break mid-stream. Every segment transcodes to the same standard:

- Video: H.264 Main profile, 1080p, 30fps, `-preset ultrafast`
- Audio: AAC stereo, 44100 Hz, 128kbps

This is the same approach Plex uses — transcode for control and compatibility. The tradeoffs are mild:

- **CPU**: `ultrafast` handles 1080p in real-time on any modern machine. Hardware encoding (Intel Quick Sync, VA-API) is a future optimization.
- **Quality**: Generation loss from re-encoding. Imperceptible on a TV at couch distance at 5 Mbps.
- **Seeking**: Not supported in the continuous stream. Rarely missed for TV viewing.

What we gain:

- **One code path.** No branching based on source type, file size, or live/VOD.
- **Guaranteed compatibility.** We control exactly what reaches the TV.
- **Seamless queue transitions.** The TV sees one continuous video.
- **No completion detection.** We don't poll the TV. The segment ffmpeg exits when done, and Python starts the next one.
- **Minimal disk I/O.** The main data path stays in memory. The only disk write is the YouTube DASH audio temp file (~1-2 MB, cleaned up automatically), and optionally `--save-stream`.

---

## 3. Segment Lifecycle

### URL resolution

For each queue item, yt-dlp probes the URL and extracts source information. This can run in the background while the previous item is still playing (prefetch). yt-dlp determines:

- Whether the source is live or VOD
- Stream URL(s) — either a single muxed URL, separate video+audio CDN URLs, or an HLS manifest
- Metadata — title, duration, estimated size (used for display, not routing decisions)

The default format selector `bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a] / bv[ext=mp4]+ba[ext=m4a] / b[ext=mp4] / b` prefers separate DASH H.264+AAC streams to maximize quality. When separate streams are returned, the audio is downloaded to a temp file first to work around an ffmpeg bug with multiple HTTP inputs (see YouTube notes in the README). With `--youtube-default`, the simpler selector `b[ext=mp4] / b` is used instead, which returns a single muxed stream (lower latency, no audio download, but typically capped at 720p).

If yt-dlp fails (direct URL to an already-playable file), the URL is passed directly to ffmpeg as input. ffmpeg can handle most direct media URLs natively.

### Transcoding and muxing

The segment ffmpeg reads the source from the network, transcodes to H.264+AAC, and muxes into MPEG-TS on stdout. For separate video+audio sources (YouTube DASH), ffmpeg takes two `-i` arguments; for everything else, one `-i`. Either way, the output is interleaved MPEG-TS with proper PTS timestamps.

Each segment ffmpeg naturally starts its output with an IDR frame (keyframe) plus SPS/PPS parameters, which is the default behavior when a new encoding session begins.

### Completion and advancement

When a segment ffmpeg exits (input consumed or live stream ended):

1. The bridge thread detects this when `stdout.read()` returns empty
2. The TS rewriter flushes any partial packets and the PTS offset is advanced to the next frame boundary
3. If the next item is already resolved, Python immediately spawns a new segment ffmpeg — seamless transition
4. If the next item is still resolving, Python pumps a placeholder segment to fill the gap (see Section 4)
5. If the queue is exhausted, Python closes the sink. For Chromecast, this closes the master muxer's stdin, which reads remaining data and exits. For DLNA/Roku, the ring buffer is closed directly. Either way, the HTTP handler drains the buffer and the TV reaches the natural end of the stream.

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

Casting uses three SOAP actions sent to the device's `control_url`:

1. **Stop** (best-effort) — Reset transport state. LG webOS returns 701 "Transition not available" on SetAVTransportURI if the renderer is still PLAYING from a previous session.
2. **SetAVTransportURI** — Body includes:
   - `<CurrentURI>` — the HTTP handler URL
   - `<CurrentURIMetaData>` — HTML-escaped DIDL-Lite XML containing:
     - `<dc:title>qast</dc:title>`
     - `<res protocolInfo="http-get:*:video/mpeg:DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=...">URL</res>`
     - `<upnp:class>object.item.videoItem.videoBroadcast</upnp:class>`
3. **Poll GetTransportInfo + Play** — Poll the TV's transport state every 2 seconds and attempt Play. Up to 8 attempts (16 seconds total). Some TVs (LG webOS) need time to transition through TRANSITIONING state after probing the stream. Some TVs auto-start playback without an explicit Play command — detected by checking for `PLAYING` state.

SOAP envelope format: `SOAPAction` header is `"urn:schemas-upnp-org:service:AVTransport:1#ActionName"`, body is XML with `<InstanceID>0</InstanceID>` plus action-specific arguments.

### Roku (ECP)

Content is sent via Media Assistant (channel 782875), a free community app from the Roku Channel Store:

- `POST /launch/782875?t=v&u=ENCODED_URL&videoName=qast&videoFormat=ts` — sends the HTTP handler URL for playback.

The older hidden system channel "Play on Roku" (channel 15985, via `/input/15985`) was disabled in newer Roku firmware. Native app launches (e.g. YouTube channel 837) are not used — they provide no playback state feedback and can't be pointed at our HTTP server.

### Chromecast

Uses `pychromecast`'s media controller:

```python
mc = cc.media_controller
mc.play_media(url, "video/mp4", stream_type="LIVE")
mc.block_until_active(timeout=CAST_CONNECT_TIMEOUT)  # 60s
```

The `stream_type` is always `"LIVE"` since the continuous stream is forward-only (no seek bar). The underlying Cast protocol uses protobuf messages over a TLS channel — pychromecast handles all of this.

---

## 6. Session Monitoring

The continuous stream means the TV never reaches a "finished" state mid-session — it's always playing. There is no per-protocol polling to detect when a video ends. Queue advancement is driven entirely by the local pipeline (segment ffmpeg exits → Python starts the next one).

The monitoring that remains:

### Ctrl+C (user stop)

Shut down the pipeline — kill the current segment and master muxer (if present), close the ring buffer, stop the HTTP server. Then send a protocol-specific stop command as a courtesy:

| Protocol | Stop command |
|----------|-------------|
| DLNA | SOAP `Stop` action |
| Roku | `POST /keypress/Home` |
| Chromecast | `mc.stop()` |

### TV disconnect

The HTTP handler detects a `BrokenPipeError` or `ConnectionResetError` when the TV closes the HTTP connection (user switched input, powered off, etc.). This sets a disconnect event. For Chromecast, the main loop detects this and re-casts after a brief delay. For DLNA, the disconnect event is ignored during playback because normal DLNA probe connections trigger false disconnects — DLNA renderers manage their own playback state.

### Session end

After the last segment ffmpeg exits, Python closes the sink. For Chromecast, the master muxer reads any remaining buffered data and exits; for DLNA/Roku, the ring buffer is closed directly. The HTTP handler drains the buffer and closes the HTTP response. The TV reaches the natural end of the stream, triggering its normal finished state (`STOPPED` on DLNA, `state="close"` on Roku, `IDLE`+`FINISHED` on Chromecast).
