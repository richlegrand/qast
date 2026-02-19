#!/usr/bin/env python3
"""Discover Cast devices on the network, select one, and cast a URL to it."""

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request

import pychromecast


def get_local_ip():
    """Get the local IP address that other devices on the LAN can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def resolve_url(url):
    """Use yt-dlp to extract a direct stream URL if possible.

    Returns (stream_url, content_type, title) or None.
    """
    try:
        result = subprocess.run(
            [
                "yt-dlp", "-j", "--no-playlist",
                "-f", "b[ext=mp4]/b",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  yt-dlp failed: {result.stderr.strip()}")
            return None

        info = json.loads(result.stdout)
        stream_url = info.get("url")
        if not stream_url:
            return None

        ext = info.get("ext", "mp4")
        content_type = {
            "mp4": "video/mp4",
            "webm": "video/webm",
            "m4a": "audio/mp4",
            "mp3": "audio/mp3",
        }.get(ext, "video/mp4")

        title = info.get("title", "")
        return stream_url, content_type, title
    except FileNotFoundError:
        print("  yt-dlp not found. Install with: pip install yt-dlp")
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


class ProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler that proxies requests to the upstream URL."""

    upstream_url = None
    content_type = "video/mp4"

    def do_GET(self):
        headers = {}
        # Forward range requests for seeking
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        req = Request(self.upstream_url, headers=headers)
        try:
            resp = urlopen(req, timeout=30)
            self.send_response(resp.status)
            self.send_header("Content-Type", self.content_type)
            cl = resp.headers.get("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            cr = resp.headers.get("Content-Range")
            if cr:
                self.send_header("Content-Range", cr)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception as e:
            self.send_error(502, str(e))

    def do_HEAD(self):
        req = Request(self.upstream_url, method="HEAD")
        try:
            resp = urlopen(req, timeout=10)
            self.send_response(resp.status)
            self.send_header("Content-Type", self.content_type)
            cl = resp.headers.get("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        except Exception as e:
            self.send_error(502, str(e))

    def log_message(self, format, *args):
        pass  # Suppress request logs


def start_proxy(upstream_url, content_type):
    """Start a local HTTP proxy server, return (server, local_url)."""
    ProxyHandler.upstream_url = upstream_url
    ProxyHandler.content_type = content_type

    server = HTTPServer(("0.0.0.0", 0), ProxyHandler)
    port = server.server_address[1]
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}/stream"

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


def cast_url(cc, url, content_type="video/mp4"):
    """Cast a media URL to the selected device."""
    mc = cc.media_controller
    mc.play_media(url, content_type)
    mc.block_until_active(timeout=30)
    print(f"Now casting to {cc.cast_info.friendly_name}")


def monitor_playback(cc):
    """Monitor playback until finished or Ctrl+C."""
    mc = cc.media_controller
    buffering_since = None
    BUFFERING_TIMEOUT = 30
    try:
        while True:
            time.sleep(3)
            mc.update_status()
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
        mc.stop()


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
    proxy_server = None

    try:
        cc = select_device(chromecasts)

        print(f"\nSelected: {cc.cast_info.friendly_name}\n")

        url = input("Paste URL to cast: ").strip()
        if not url:
            print("No URL provided.")
            sys.exit(1)

        # Try yt-dlp to resolve the URL to a direct stream
        print("Resolving URL...")
        resolved = resolve_url(url)
        if resolved:
            stream_url, content_type, title = resolved
            if title:
                print(f"  Title: {title}")
            print(f"  Format: {content_type}")

            # Proxy through local server so Chromecast can access it
            print("Starting local proxy...")
            proxy_server, local_url = start_proxy(stream_url, content_type)
            print(f"  Proxy: {local_url}")
            cast_target = local_url
        else:
            print("  Using URL directly.")
            cast_target = url
            content_type = guess_content_type(url)
            override = input(f"Content type [{content_type}]: ").strip()
            if override:
                content_type = override

        print("Connecting to device...")
        cc.wait()
        cast_url(cc, cast_target, content_type)
        monitor_playback(cc)
    finally:
        if proxy_server:
            proxy_server.shutdown()
        browser.stop_discovery()


if __name__ == "__main__":
    main()
