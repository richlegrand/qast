#!/usr/bin/env python3
"""Discover Cast devices on the network, select one, and cast a URL to it."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import pychromecast


def get_local_ip():
    """Get the local IP address that other devices on the LAN can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def probe_url(url):
    """Use yt-dlp to get info about a URL.

    Returns the info dict or None.
    """
    try:
        result = subprocess.run(
            [
                "yt-dlp", "-j", "--no-playlist",
                "-f", "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  yt-dlp failed: {result.stderr.strip()}")
            return None
        return json.loads(result.stdout)
    except FileNotFoundError:
        print("  yt-dlp not found. Install with: pip install yt-dlp")
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def get_live_stream_url(info):
    """Extract HLS manifest URL for a live stream.

    Returns (manifest_url, title) or None.
    """
    manifest = info.get("manifest_url")
    if manifest:
        return manifest, info.get("title", "Unknown")

    # Fallback: look for an HLS URL in formats
    for fmt in info.get("formats", []):
        if fmt.get("protocol") == "m3u8_native" and fmt.get("url"):
            return fmt["url"], info.get("title", "Unknown")

    return None


def estimate_size(info):
    """Estimate the file size in bytes from yt-dlp info dict."""
    # Check requested_formats (separate video+audio)
    requested = info.get("requested_formats")
    if requested:
        total = 0
        for fmt in requested:
            total += fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if total > 0:
            return total

    # Check top-level
    return info.get("filesize") or info.get("filesize_approx") or 0


# Files larger than this stream instead of downloading first
STREAM_THRESHOLD_MB = 200


def download_video(url):
    """Use yt-dlp to download and merge the best quality video.

    Returns (file_path,) or None.
    """
    try:
        tmp_dir = tempfile.mkdtemp(prefix="casturl_")
        out_path = os.path.join(tmp_dir, "video.mp4")

        print("  Downloading (best quality)...")
        dl = subprocess.run(
            [
                "yt-dlp", "--no-playlist",
                "-f", "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
                "--merge-output-format", "mp4",
                "--postprocessor-args", "ffmpeg:-movflags +faststart",
                "-o", out_path,
                url,
            ],
            timeout=600,
        )
        if dl.returncode != 0 or not os.path.exists(out_path):
            print("  Download failed.")
            return None

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        print(f"  Ready ({size_mb:.1f} MB)")
        return out_path

    except subprocess.TimeoutExpired:
        print("  Download timed out.")
        return None


def get_vod_stream_urls(info):
    """Extract separate video and audio URLs from yt-dlp info.

    Returns (video_url, audio_url) or None.
    """
    requested = info.get("requested_formats")
    if requested and len(requested) >= 2:
        video_url = requested[0].get("url")
        audio_url = requested[1].get("url")
        if video_url and audio_url:
            return video_url, audio_url

    # Fallback: single muxed URL
    return None


def start_vod_stream(video_url, audio_url):
    """Start ffmpeg copy-muxing separate video+audio into a growing fragmented mp4.

    Returns (server, ffmpeg_proc, local_url, tmp_path) or None.
    """
    tmp_dir = tempfile.mkdtemp(prefix="casturl_stream_")
    tmp_path = os.path.join(tmp_dir, "video.mp4")

    ffmpeg_log = os.path.join(tmp_dir, "ffmpeg.log")
    ffmpeg_logf = open(ffmpeg_log, "w")
    ffmpeg_proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-i", video_url,
            "-i", audio_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-reset_timestamps", "1",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            tmp_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=ffmpeg_logf,
    )

    # Wait for ffmpeg to buffer enough data
    print("  Buffering", end="", flush=True)
    MIN_BUFFER = 1024 * 1024  # 1MB before we start casting
    for i in range(150):  # up to 15 seconds
        time.sleep(0.1)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) >= MIN_BUFFER:
            break
        if i % 10 == 9:
            print(".", end="", flush=True)
    print()

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        ffmpeg_proc.kill()
        ffmpeg_logf.close()
        print("  ffmpeg failed to start. Log:")
        with open(ffmpeg_log) as f:
            for line in f.readlines()[-10:]:
                print(f"    {line.rstrip()}")
        return None

    size_kb = os.path.getsize(tmp_path) // 1024
    print(f"  Buffered {size_kb}KB")

    LiveStreamHandler.serve_path = tmp_path
    LiveStreamHandler.ffmpeg_proc = ffmpeg_proc

    server = HTTPServer(("0.0.0.0", 0), LiveStreamHandler)
    port = server.server_address[1]
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}/video.mp4"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ffmpeg_proc, local_url, tmp_path


class FileHandler(BaseHTTPRequestHandler):
    """Serves a local file with range request support for seeking."""

    serve_path = None

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only=False):
        if not self.serve_path or not os.path.exists(self.serve_path):
            self.send_error(404)
            return

        try:
            file_size = os.path.getsize(self.serve_path)
            range_header = self.headers.get("Range")

            if range_header:
                range_spec = range_header.replace("bytes=", "")
                start_str, _, end_str = range_spec.partition("-")
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                if not head_only:
                    with open(self.serve_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                if not head_only:
                    with open(self.serve_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass  # Client disconnected

    def log_message(self, format, *args):
        pass


class LiveStreamHandler(BaseHTTPRequestHandler):
    """Serves a growing file written by ffmpeg."""

    serve_path = None
    ffmpeg_proc = None

    def do_HEAD(self):
        print(f"  [HTTP] HEAD request from {self.client_address[0]}")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()

    def do_GET(self):
        print(f"  [HTTP] GET request from {self.client_address[0]}")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()

            # Stream from the growing file as ffmpeg writes to it
            total_sent = 0
            with open(self.serve_path, "rb") as f:
                stall_count = 0
                while True:
                    chunk = f.read(65536)
                    if chunk:
                        self.wfile.write(chunk)
                        total_sent += len(chunk)
                        stall_count = 0
                    else:
                        # No data yet — wait for ffmpeg to write more
                        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is not None:
                            print(f"  [HTTP] ffmpeg exited, sent {total_sent} bytes")
                            break  # ffmpeg exited
                        stall_count += 1
                        if stall_count > 300:  # ~30s with no data
                            print(f"  [HTTP] stalled, sent {total_sent} bytes")
                            break
                        time.sleep(0.1)
        except (ConnectionResetError, BrokenPipeError):
            print(f"  [HTTP] client disconnected after {total_sent} bytes")

    def log_message(self, format, *args):
        pass


def start_live_stream(manifest_url):
    """Start ffmpeg reading HLS and outputting fragmented mp4, serve it.

    Returns (server, ffmpeg_proc, local_url, tmp_path).
    """
    tmp_dir = tempfile.mkdtemp(prefix="casturl_live_")
    tmp_path = os.path.join(tmp_dir, "live.mp4")

    # Start ffmpeg: read HLS, output fragmented mp4 (streamable from byte 0)
    ffmpeg_log = os.path.join(tmp_dir, "ffmpeg.log")
    ffmpeg_logf = open(ffmpeg_log, "w")
    ffmpeg_proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-re",
            "-i", manifest_url,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-c:a", "aac", "-b:a", "128k",
            "-reset_timestamps", "1",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            tmp_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=ffmpeg_logf,
    )

    # Wait for ffmpeg to buffer enough data
    print("  Buffering live stream", end="", flush=True)
    MIN_BUFFER = 512 * 1024  # 512KB before we start casting
    for _ in range(150):  # up to 15 seconds
        time.sleep(0.1)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) >= MIN_BUFFER:
            break
        if _ % 10 == 9:
            print(".", end="", flush=True)
    print()

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        ffmpeg_proc.kill()
        ffmpeg_logf.close()
        print("  ffmpeg failed to start. Log:")
        with open(ffmpeg_log) as f:
            for line in f.readlines()[-10:]:
                print(f"    {line.rstrip()}")
        return None

    size_kb = os.path.getsize(tmp_path) // 1024
    print(f"  Buffered {size_kb}KB")
    print(f"  ffmpeg log: {ffmpeg_log}")

    LiveStreamHandler.serve_path = tmp_path
    LiveStreamHandler.ffmpeg_proc = ffmpeg_proc

    server = HTTPServer(("0.0.0.0", 0), LiveStreamHandler)
    port = server.server_address[1]
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}/live.mp4"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ffmpeg_proc, local_url, tmp_path


def start_file_server(file_path):
    """Start HTTP server for a local file, return (server, url)."""
    FileHandler.serve_path = file_path

    server = HTTPServer(("0.0.0.0", 0), FileHandler)
    port = server.server_address[1]
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}/video.mp4"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, local_url


def select_device(chromecasts):
    """Display devices and let the user pick one."""
    if not chromecasts:
        print("No Cast devices found.")
        sys.exit(1)

    print(f"\nFound {len(chromecasts)} device(s):\n")
    for i, cc in enumerate(chromecasts):
        print(f"  [{i}] {cc.cast_info.friendly_name} ({cc.cast_info.model_name})")

    print()
    while True:
        choice = input("Select device number: ").strip()
        try:
            idx = int(choice)
            if 0 <= idx < len(chromecasts):
                return chromecasts[idx]
        except ValueError:
            pass
        print(f"Enter a number between 0 and {len(chromecasts) - 1}.")


def cast_media(cc, url, content_type="video/mp4", live=False):
    """Cast a media URL to the selected device."""
    mc = cc.media_controller
    stream_type = "LIVE" if live else "BUFFERED"
    mc.play_media(url, content_type, stream_type=stream_type)
    mc.block_until_active(timeout=60)
    print(f"Now casting to {cc.cast_info.friendly_name}")


def monitor_playback(cc):
    """Monitor playback until finished or Ctrl+C."""
    mc = cc.media_controller
    buffering_since = None
    BUFFERING_TIMEOUT = 60
    try:
        while True:
            time.sleep(3)
            try:
                mc.update_status()
            except Exception:
                print("\nDevice disconnected.")
                break
            status = mc.status
            if not status:
                continue
            state = status.player_state
            if state == "IDLE" and status.idle_reason == "FINISHED":
                print("\nPlayback finished.")
                break
            if state == "IDLE" and status.idle_reason == "ERROR":
                print("\nPlayback error on device.")
                break
            if state == "BUFFERING":
                if buffering_since is None:
                    buffering_since = time.time()
                elif time.time() - buffering_since > BUFFERING_TIMEOUT:
                    print(f"\nBuffering for over {BUFFERING_TIMEOUT}s, giving up.")
                    mc.stop()
                    break
            else:
                buffering_since = None
            print(f"  State: {state}  ", end="\r")
    except KeyboardInterrupt:
        print("\nStopping playback...")
        try:
            mc.stop()
        except Exception:
            pass


def guess_content_type(url):
    """Make a rough guess at content type from the URL."""
    lower = url.lower().split("?")[0]
    if lower.endswith(".mp3"):
        return "audio/mp3"
    if lower.endswith(".mp4"):
        return "video/mp4"
    if lower.endswith(".mkv"):
        return "video/x-matroska"
    if lower.endswith(".webm"):
        return "video/webm"
    if lower.endswith(".m3u8"):
        return "application/x-mpegURL"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    return "video/mp4"


def main():
    print("Scanning for Cast devices (10s)...")
    chromecasts, browser = pychromecast.get_chromecasts(timeout=10)
    server = None
    tmp_path = None
    ffmpeg_proc = None

    try:
        cc = select_device(chromecasts)
        print(f"\nSelected: {cc.cast_info.friendly_name}\n")

        url = input("Paste URL to cast: ").strip()
        if not url:
            print("No URL provided.")
            sys.exit(1)

        # Probe the URL with yt-dlp
        print("Resolving URL...")
        info = probe_url(url)

        if info and info.get("is_live"):
            # Live stream — ffmpeg converts HLS to fragmented mp4
            title = info.get("title", "Unknown")
            print(f"  Title: {title}")
            print("  Live stream detected.")

            live = get_live_stream_url(info)
            if live:
                manifest_url, _ = live
                result = start_live_stream(manifest_url)
                if not result:
                    sys.exit(1)
                server, ffmpeg_proc, local_url, tmp_path = result
                print(f"  Streaming: {local_url}")
                cast_target = local_url
                content_type = "video/mp4"
                is_live = True
            else:
                print("  Could not extract live stream URL.")
                sys.exit(1)

        elif info:
            # VOD — decide: download first (seekable) vs stream (fast start)
            is_live = False
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0)
            est_bytes = estimate_size(info)
            est_mb = est_bytes / (1024 * 1024) if est_bytes else 0
            print(f"  Title: {title}")
            if duration:
                mins, secs = divmod(int(duration), 60)
                print(f"  Duration: {mins}m{secs:02d}s")
            if est_mb:
                print(f"  Estimated size: {est_mb:.0f} MB")

            if est_mb > STREAM_THRESHOLD_MB:
                # Large file — stream it
                print(f"  Large file (>{STREAM_THRESHOLD_MB}MB), streaming instead of downloading.")
                vod_urls = get_vod_stream_urls(info)
                if vod_urls:
                    result = start_vod_stream(*vod_urls)
                    if result:
                        server, ffmpeg_proc, local_url, tmp_path = result
                        print(f"  Streaming: {local_url}")
                        cast_target = local_url
                        content_type = "video/mp4"
                    else:
                        print("  Streaming failed, falling back to download.")
                        tmp_path = download_video(url)
                        if tmp_path:
                            server, local_url = start_file_server(tmp_path)
                            print(f"  Serving: {local_url}")
                            cast_target = local_url
                            content_type = "video/mp4"
                        else:
                            print("  Download also failed.")
                            cast_target = url
                            content_type = guess_content_type(url)
                else:
                    print("  Could not extract stream URLs, downloading instead.")
                    tmp_path = download_video(url)
                    if tmp_path:
                        server, local_url = start_file_server(tmp_path)
                        print(f"  Serving: {local_url}")
                        cast_target = local_url
                        content_type = "video/mp4"
                    else:
                        cast_target = url
                        content_type = guess_content_type(url)
            else:
                # Small file — download first for seeking support
                if est_mb:
                    print(f"  Small file (<={STREAM_THRESHOLD_MB}MB), downloading for seek support.")
                tmp_path = download_video(url)
                if tmp_path:
                    server, local_url = start_file_server(tmp_path)
                    print(f"  Serving: {local_url}")
                    cast_target = local_url
                    content_type = "video/mp4"
                else:
                    print("  Download failed, trying URL directly.")
                    cast_target = url
                    content_type = guess_content_type(url)

        else:
            # yt-dlp couldn't handle it — cast URL directly
            is_live = False
            print("  Using URL directly.")
            cast_target = url
            content_type = guess_content_type(url)
            override = input(f"Content type [{content_type}]: ").strip()
            if override:
                content_type = override

        print("Connecting to device...")
        cc.wait()
        cast_media(cc, cast_target, content_type, live=is_live)
        monitor_playback(cc)
    finally:
        if ffmpeg_proc:
            ffmpeg_proc.kill()
            ffmpeg_proc.wait()
        if server:
            server.shutdown()
        if tmp_path:
            tmp_dir = os.path.dirname(tmp_path)
            # Print ffmpeg log if it exists
            ffmpeg_log = os.path.join(tmp_dir, "ffmpeg.log")
            if os.path.exists(ffmpeg_log):
                print(f"\n  ffmpeg log (last 15 lines):")
                with open(ffmpeg_log) as f:
                    lines = f.readlines()
                    for line in lines[-15:]:
                        print(f"    {line.rstrip()}")
            for f in os.listdir(tmp_dir):
                os.unlink(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        browser.stop_discovery()


if __name__ == "__main__":
    main()
