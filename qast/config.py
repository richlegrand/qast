"""All constants in one place."""

# ── Codec parameters ──
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "ultrafast"
VIDEO_SIZE = "1920x1080"
VIDEO_FPS = "30"
VIDEO_GOP = "60"  # keyframe every 2s (at 30fps) — limits fragment buffer lag
AUDIO_CODEC = "aac"
AUDIO_SAMPLE_RATE = "44100"
AUDIO_CHANNELS = "2"
AUDIO_BITRATE = "128k"
VIDEO_BITRATE = "5M"

# ── Buffer sizes ──
BUFFER_MAX = 64 * 1024 * 1024  # 64 MB ring buffer max
BUFFER_MIN = 4 * 1024 * 1024   # 4 MB before casting starts
PIPE_CHUNK = 65536              # 64 KB read/write chunks

# ── Screen capture buffer (low-latency) ──
# Derive byte sizes from bitrate + audio overhead
_VIDEO_BPS = int(VIDEO_BITRATE.replace("M", "000000").replace("k", "000"))
_AUDIO_BPS = int(AUDIO_BITRATE.replace("M", "000000").replace("k", "000"))
_TOTAL_BPS = _VIDEO_BPS + _AUDIO_BPS
CAPTURE_BUFFER_SECONDS_MIN = 4
CAPTURE_BUFFER_SECONDS_MAX = 8
CAPTURE_BUFFER_MIN = CAPTURE_BUFFER_SECONDS_MIN * _TOTAL_BPS // 8
CAPTURE_BUFFER_MAX = CAPTURE_BUFFER_SECONDS_MAX * _TOTAL_BPS // 8

# ── Buffer monitor ──
BUFFER_MONITOR_INTERVAL = 3     # seconds between buffer status prints

# ── Timeouts (seconds) ──
DISCOVERY_TIMEOUT = 15
CAST_CONNECT_TIMEOUT = 60
YTDLP_TIMEOUT = 30
BUFFER_FILL_TIMEOUT = 60

# ── yt-dlp format selector ──
YTDLP_FORMAT = (
    "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
    "/bv[ext=mp4]+ba[ext=m4a]"
    "/b[ext=mp4]"
    "/b"
)

# ── HTTP server ──
HTTP_BIND = "0.0.0.0"
HTTP_PORT = 0  # auto-assign

# ── DLNA headers ──
DLNA_FLAGS = (
    "DLNA.ORG_OP=00;DLNA.ORG_CI=1;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)
# Fake Content-Length for DLNA renderers that refuse streams without one.
# Standard trick from pulseaudio-dlna / Serviio.
DLNA_FAKE_CONTENT_LENGTH = 100 * 1024 * 1024 * 1024  # 100 GB


def ffmpeg_output_args(
    preset: str | None = None,
    tune: str | None = None,
    flush_packets: bool = True,
    pix_fmt: str | None = None,
    scale: bool = True,
) -> list[str]:
    """Standard ffmpeg output encoding args for all segments."""
    args = []
    if scale:
        # Build a -vf filter chain: fit source within VIDEO_SIZE, pad with
        # black bars to preserve aspect ratio (letterbox / pillarbox).
        filters: list[str] = []
        if pix_fmt:
            filters.append(f"format={pix_fmt}")
        filters.append(
            f"scale={VIDEO_SIZE.replace('x', ':')}:"
            "force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={VIDEO_SIZE.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black"
        )
        args += ["-vf", ",".join(filters)]
    else:
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
