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
    "DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)
