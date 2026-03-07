"""yt-dlp URL resolution and source downloading."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from .. import config
from ..log import get_logger

log = get_logger("resolve")


def probe_duration(path: str) -> float | None:
    """Get duration via ffprobe for local files."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        pass
    return None


@dataclass
class ResolvedURL:
    title: str
    duration: float | None          # seconds, None for live
    is_live: bool
    source_urls: list[str]          # 1 for muxed, 2 for DASH video+audio
    capture: str | None = None      # "screen" | "window" | "webcam" for capture items
    window_title: str | None = None # for capture="window"
    show_placeholder: bool = True   # per-item placeholder toggle
    cursor: bool = True             # show cursor in screen/window capture
    has_audio: bool = True           # False when only video stream available
    _original_url: str = ""          # for re-resolution on fallback
    _temp_files: list[str] = field(default_factory=list, repr=False)

    def cleanup(self) -> None:
        """Remove any temp files created by download_audio."""
        for path in self._temp_files:
            try:
                os.unlink(path)
                log.debug("Cleaned up temp file: %s", path)
            except OSError:
                pass
        self._temp_files.clear()


def resolve(url: str, cookies_from_browser: str | None = None) -> ResolvedURL | None:
    """Probe a URL with yt-dlp and return a ResolvedURL, or None on failure.

    Consolidates probe_url, get_live_stream_url, get_vod_stream_urls from
    the original qast.py into one function.
    """
    try:
        cmd = [
            "yt-dlp", "-j", "--no-playlist",
            "--remote-components", "ejs:github",
            "-f", config.YTDLP_FORMAT_DEFAULT if config.YOUTUBE_DEFAULT else config.YTDLP_FORMAT,
        ]
        if cookies_from_browser:
            cmd += ["--cookies-from-browser", cookies_from_browser]
        cmd.append(url)
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=config.YTDLP_TIMEOUT,
        )
    except FileNotFoundError:
        log.error("yt-dlp not found — install with: pip install yt-dlp")
        return None
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timed out after %ds", config.YTDLP_TIMEOUT)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        log.warning("yt-dlp failed: %s", stderr)
        stderr_lower = stderr.lower()
        if "latest version" in stderr_lower or "challenge" in stderr_lower:
            log.warning("yt-dlp may be outdated. Update with: pip install -U yt-dlp")
        return None

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("yt-dlp returned invalid JSON")
        return None

    title = info.get("title", "Unknown")
    is_live = bool(info.get("is_live"))
    duration = info.get("duration") if not is_live else None

    if is_live:
        urls = _extract_live_urls(info)
    else:
        urls = _extract_vod_urls(info)

    if not urls:
        log.warning("Could not extract source URLs from yt-dlp output")
        return None

    log.info("Resolved: %s (live=%s, sources=%d)", title, is_live, len(urls))
    return ResolvedURL(
        title=title,
        duration=duration,
        is_live=is_live,
        source_urls=urls,
        _original_url=url,
    )


def download_audio(resolved: ResolvedURL) -> None:
    """Download the audio stream to a temp file, leaving video as HTTP.

    Works around an ffmpeg bug where multiple HTTP inputs cause audio
    truncation. By making the audio a local file, ffmpeg only has one
    HTTP input (video) and the bug is avoided.
    The audio file is typically small (~1-2MB) so this is fast.

    On timeout/failure, falls back to a single muxed URL by re-resolving
    with format "b" to avoid hanging ffmpeg with two HTTP inputs.
    """
    if resolved.is_live or len(resolved.source_urls) < 2:
        return

    audio_url = resolved.source_urls[1]
    fd, path = tempfile.mkstemp(suffix=".m4a", prefix="qast_")
    os.close(fd)

    log.info("Downloading audio to %s", path)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-i", audio_url, "-c", "copy", path],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        log.warning("Audio download timed out, falling back to muxed stream")
        _cleanup_path(path)
        _fallback_to_muxed(resolved)
        return
    if result.returncode != 0:
        log.warning("Audio download failed: %s", result.stderr[:200])
        _cleanup_path(path)
        _fallback_to_muxed(resolved)
        return

    resolved.source_urls[1] = path
    resolved._temp_files = [path]
    log.info("Audio downloaded (%d bytes)", os.path.getsize(path))


def _cleanup_path(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fallback_to_muxed(resolved: ResolvedURL) -> None:
    """Re-resolve with yt-dlp to get a single muxed stream (video+audio).

    The muxed stream may be lower resolution than the separate DASH streams,
    but it includes audio and avoids the multi-HTTP-input ffmpeg bug.
    """
    log.info("Re-resolving for muxed stream")
    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-playlist", "-f", "b[ext=mp4]/b",
             resolved._original_url],
            capture_output=True, text=True, timeout=config.YTDLP_TIMEOUT,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            url = info.get("url")
            if url:
                resolved.source_urls = [url]
                log.info("Falling back to muxed stream (may be lower resolution)")
                return
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.warning("Muxed re-resolve failed: %s", e)

    # Last resort: video-only
    resolved.source_urls = [resolved.source_urls[0]]
    resolved.has_audio = False
    log.warning("Using video-only stream (no audio)")


def resolve_source(
    url: str,
    duration: float | None = None,
    title: str | None = None,
    cookies_from_browser: str | None = None,
) -> ResolvedURL:
    """Resolve a URL or file path into a ResolvedURL, with fallback.

    Tries yt-dlp first. On failure, falls back to passing the raw URL
    directly to ffmpeg (probing duration via ffprobe for local files).
    Always returns a ResolvedURL — never None.
    """
    log.info("Resolving: %s", url)

    # Local files go straight to ffmpeg — no yt-dlp needed.
    if os.path.isfile(url):
        media_duration = probe_duration(url)
        if duration is not None:
            media_duration = duration
        fallback_title = title or os.path.basename(url)
        return ResolvedURL(
            title=fallback_title,
            duration=media_duration,
            is_live=False,
            source_urls=[url],
            has_audio=False,
        )

    resolved = resolve(url, cookies_from_browser=cookies_from_browser)
    if resolved:
        download_audio(resolved)
        if duration is not None:
            resolved.duration = duration
        return resolved

    # yt-dlp failed — pass raw URL directly to ffmpeg
    if "youtube.com" in url or "youtu.be" in url:
        hint = ""
        if not cookies_from_browser:
            hint = " Try --cookies-from-browser chrome"
        log.warning("yt-dlp failed for %s — YouTube may be blocking requests.%s",
                    url, hint)
    else:
        log.warning("Failed to resolve %s, using raw URL", url)
    media_duration = duration
    return ResolvedURL(
        title=title or url,
        duration=media_duration,
        is_live=False,
        source_urls=[url],
    )


def _extract_live_urls(info: dict) -> list[str]:
    """Extract stream URL(s) for a live source."""
    # Prefer manifest URL
    manifest = info.get("manifest_url")
    if manifest:
        return [manifest]

    # Fallback: HLS URL from formats
    for fmt in info.get("formats", []):
        if fmt.get("protocol") == "m3u8_native" and fmt.get("url"):
            return [fmt["url"]]

    # Last resort: direct url field
    if info.get("url"):
        return [info["url"]]

    return []


def _extract_vod_urls(info: dict) -> list[str]:
    """Extract stream URL(s) for a VOD source."""
    # Prefer separate video+audio (YouTube DASH)
    requested = info.get("requested_formats")
    if requested and len(requested) >= 2:
        video_url = requested[0].get("url")
        audio_url = requested[1].get("url")
        if video_url and audio_url:
            return [video_url, audio_url]

    # Single muxed URL
    if info.get("url"):
        return [info["url"]]

    # From requested_formats with only one entry
    if requested and len(requested) == 1:
        u = requested[0].get("url")
        if u:
            return [u]

    return []
