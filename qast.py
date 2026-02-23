#!/usr/bin/env python3
"""Discover Cast/DLNA/Roku devices on the network, select one, and cast a URL."""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import pychromecast
    HAS_PYCHROMECAST = True
except ImportError:
    HAS_PYCHROMECAST = False

_cast_browser = None


def get_local_ip():
    """Get the local IP address that other devices on the LAN can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _xml_text(parent, tag, ns=None):
    """Safe XML text extraction — returns '' if element missing."""
    if ns:
        tag = f"{{{ns}}}{tag}"
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else ""


def _parse_dlna_description(location_url):
    """Fetch a UPnP device description XML and extract useful fields.

    Returns a device dict or None.
    """
    try:
        req = urllib.request.Request(location_url, headers={"User-Agent": "qast/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_bytes = resp.read()
    except Exception:
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    ns = "urn:schemas-upnp-org:device-1-0"
    device_el = root.find(f"{{{ns}}}device")
    if device_el is None:
        return None

    friendly_name = _xml_text(device_el, "friendlyName", ns)
    model = _xml_text(device_el, "modelName", ns) or _xml_text(device_el, "manufacturer", ns)

    # Find AVTransport service controlURL
    control_url = None
    for service in device_el.iter(f"{{{ns}}}service"):
        stype = _xml_text(service, "serviceType", ns)
        if "AVTransport" in stype:
            ctrl = _xml_text(service, "controlURL", ns)
            if ctrl:
                parsed = urlparse(location_url)
                base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"
                control_url = base + ctrl if ctrl.startswith("/") else base + "/" + ctrl
            break

    if not control_url:
        return None

    parsed_loc = urlparse(location_url)
    return {
        "name": friendly_name or parsed_loc.hostname,
        "model": model or "UPnP Renderer",
        "protocol": "dlna",
        "host": parsed_loc.hostname,
        "port": parsed_loc.port or 80,
        "cast_obj": None,
        "control_url": control_url,
    }


def discover_dlna(timeout=5):
    """SSDP M-SEARCH for UPnP MediaRenderer devices."""
    target = "urn:schemas-upnp-org:device:MediaRenderer:1"
    mx = min(timeout, 5)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        f"MX: {mx}\r\n"
        f"ST: {target}\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(2)

    # Send M-SEARCH multiple times — UDP is unreliable
    for _ in range(3):
        sock.sendto(msg, ("239.255.255.250", 1900))

    locations = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="replace")
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    locations.add(line.split(":", 1)[1].strip())
        except socket.timeout:
            # Re-send and keep listening until deadline
            if time.time() < deadline:
                sock.sendto(msg, ("239.255.255.250", 1900))
                continue
            break
    sock.close()

    devices = []
    for loc in locations:
        dev = _parse_dlna_description(loc)
        if dev:
            devices.append(dev)
    return devices


def _parse_roku_device(base_url):
    """Fetch Roku device-info and return a device dict or None."""
    try:
        req = urllib.request.Request(
            f"{base_url}query/device-info",
            headers={"User-Agent": "qast/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_bytes = resp.read()
    except Exception:
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    friendly_name = _xml_text(root, "friendly-device-name") or _xml_text(root, "user-device-name")
    model = _xml_text(root, "model-name") or _xml_text(root, "friendly-model-name")

    parsed = urlparse(base_url)
    return {
        "name": friendly_name or parsed.hostname,
        "model": model or "Roku",
        "protocol": "roku",
        "host": parsed.hostname,
        "port": parsed.port or 8060,
        "cast_obj": None,
        "control_url": None,
    }


def discover_roku(timeout=5):
    """SSDP M-SEARCH for Roku devices (roku:ecp)."""
    mx = min(timeout, 5)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        f"MX: {mx}\r\n"
        "ST: roku:ecp\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(2)

    for _ in range(3):
        sock.sendto(msg, ("239.255.255.250", 1900))

    locations = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="replace")
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if not loc.endswith("/"):
                        loc += "/"
                    locations.add(loc)
        except socket.timeout:
            if time.time() < deadline:
                sock.sendto(msg, ("239.255.255.250", 1900))
                continue
            break
    sock.close()

    devices = []
    for loc in locations:
        dev = _parse_roku_device(loc)
        if dev:
            devices.append(dev)
    return devices


def discover_cast(timeout=10):
    """Discover Chromecast devices via pychromecast. Returns [] if not installed."""
    global _cast_browser
    if not HAS_PYCHROMECAST:
        return []

    chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    _cast_browser = browser

    devices = []
    for cc in chromecasts:
        devices.append({
            "name": cc.cast_info.friendly_name,
            "model": cc.cast_info.model_name or "Chromecast",
            "protocol": "cast",
            "host": str(cc.cast_info.host),
            "port": cc.cast_info.port,
            "cast_obj": cc,
            "control_url": None,
        })
    return devices


def discover_all(timeout=15):
    """Run all discovery protocols in parallel, merge and deduplicate."""
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_dlna = pool.submit(discover_dlna, timeout)
        fut_roku = pool.submit(discover_roku, timeout)
        fut_cast = pool.submit(discover_cast, timeout)

        for fut in [fut_dlna, fut_roku, fut_cast]:
            try:
                results.extend(fut.result())
            except Exception:
                pass

    # Deduplicate by (host, protocol)
    seen = set()
    unique = []
    for dev in results:
        key = (dev["host"], dev["protocol"])
        if key not in seen:
            seen.add(key)
            unique.append(dev)

    unique.sort(key=lambda d: d["name"].lower())
    return unique


# ---------------------------------------------------------------------------
# URL probing / yt-dlp / ffmpeg / HTTP serving (unchanged)
# ---------------------------------------------------------------------------

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
        tmp_dir = tempfile.mkdtemp(prefix="qast_")
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
    tmp_dir = tempfile.mkdtemp(prefix="qast_stream_")
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

    # DLNA content features flag for MP4 (enables streaming on LG/Samsung)
    DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"

    def do_HEAD(self):
        print(f"  [HTTP] HEAD from {self.client_address[0]}")
        self._serve(head_only=True)

    def do_GET(self):
        print(f"  [HTTP] GET from {self.client_address[0]} Range={self.headers.get('Range', 'none')}")
        self._serve(head_only=False)

    def _send_dlna_headers(self):
        self.send_header("contentFeatures.dlna.org", self.DLNA_FLAGS)
        self.send_header("transferMode.dlna.org", "Streaming")

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
                self._send_dlna_headers()
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
                self._send_dlna_headers()
                self.end_headers()

                if not head_only:
                    with open(self.serve_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            print(f"  [HTTP] client disconnected")

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
    tmp_dir = tempfile.mkdtemp(prefix="qast_live_")
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


# ---------------------------------------------------------------------------
# DLNA (UPnP AVTransport) control
# ---------------------------------------------------------------------------

def _dlna_soap_action(control_url, action, args=""):
    """Send a SOAP request to a DLNA AVTransport service."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        f'{args}'
        f'</u:{action}>'
        '</s:Body>'
        '</s:Envelope>'
    ).encode("utf-8")

    req = urllib.request.Request(
        control_url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _cast_media_dlna(device, url, content_type):
    """Cast a URL to a DLNA renderer via AVTransport SOAP."""
    escaped_url = html_escape(url)
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        '<dc:title>qast</dc:title>'
        f'<res protocolInfo="http-get:*:{content_type}:*">{escaped_url}</res>'
        '<upnp:class>object.item.videoItem</upnp:class>'
        '</item>'
        '</DIDL-Lite>'
    )
    metadata = html_escape(didl)
    args = (
        f'<CurrentURI>{escaped_url}</CurrentURI>'
        f'<CurrentURIMetaData>{metadata}</CurrentURIMetaData>'
    )
    _dlna_soap_action(device["control_url"], "SetAVTransportURI", args)
    # Some TVs (e.g. LG webOS) need time to process the URI before Play
    for attempt in range(3):
        time.sleep(1)
        try:
            _dlna_soap_action(device["control_url"], "Play", "<Speed>1</Speed>")
            break
        except Exception:
            if attempt == 2:
                raise
            print(f"  Play not ready, retrying ({attempt + 1}/3)...")
    print(f"Now casting to {device['name']}")


def _monitor_dlna(device):
    """Poll DLNA transport state until stopped."""
    try:
        while True:
            time.sleep(3)
            try:
                resp = _dlna_soap_action(device["control_url"], "GetTransportInfo")
                root = ET.fromstring(resp)
                # Find CurrentTransportState in response
                state = ""
                for el in root.iter():
                    if el.tag.endswith("CurrentTransportState") and el.text:
                        state = el.text.strip()
                        break
                if state in ("STOPPED", "NO_MEDIA_PRESENT"):
                    print("\nPlayback finished.")
                    break
                print(f"  State: {state}  ", end="\r")
            except Exception:
                print("\nDevice disconnected.")
                break
    except KeyboardInterrupt:
        print("\nStopping playback...")
        try:
            _dlna_soap_action(device["control_url"], "Stop")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Roku ECP control
# ---------------------------------------------------------------------------

def _extract_youtube_id(url):
    """Extract YouTube video ID from a URL, or return None."""
    parsed = urlparse(url)
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        qs = parse_qs(parsed.query)
        vid = qs.get("v")
        if vid:
            return vid[0]
    if parsed.hostname in ("youtu.be",):
        path = parsed.path.lstrip("/")
        if path:
            return path.split("/")[0]
    return None


def _cast_media_roku(device, url):
    """Launch content on a Roku device via ECP."""
    base = f"http://{device['host']}:{device['port']}"
    yt_id = _extract_youtube_id(url)
    if yt_id:
        launch_url = f"{base}/launch/837?contentId={yt_id}&MediaType=live"
        req = urllib.request.Request(launch_url, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=10)
        device["_roku_app_id"] = "837"
        print(f"Now casting YouTube ({yt_id}) to {device['name']}")
        return

    # Fallback: try Roku Media Player input
    print("  Warning: Non-YouTube URLs may not work on Roku.")
    from urllib.parse import quote
    launch_url = f"{base}/input/15985?t=v&u={quote(url, safe='')}&k=(null)&videoName=qast&videoFormat=mp4"
    req = urllib.request.Request(launch_url, method="POST", data=b"")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"Now casting to {device['name']}")
    except Exception as e:
        print(f"  Roku launch failed: {e}")


def _monitor_roku(device):
    """Poll Roku state until playback ends."""
    base = f"http://{device['host']}:{device['port']}"
    app_id = device.get("_roku_app_id")
    try:
        if app_id:
            # App launch (e.g. YouTube) — monitor via active-app
            print("  Playing via Roku app. Press Ctrl+C to stop.")
            while True:
                time.sleep(5)
                try:
                    req = urllib.request.Request(f"{base}/query/active-app")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        root = ET.fromstring(resp.read())
                    active = root.find("app")
                    active_id = active.get("id", "") if active is not None else ""
                    if active_id != app_id:
                        print("\nApp closed.")
                        break
                except Exception:
                    print("\nDevice disconnected.")
                    break
        else:
            # Direct media — monitor via media-player
            while True:
                time.sleep(3)
                try:
                    req = urllib.request.Request(f"{base}/query/media-player")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        root = ET.fromstring(resp.read())
                    state = root.get("state", "close")
                    if state == "close":
                        print("\nPlayback finished.")
                        break
                    print(f"  State: {state}  ", end="\r")
                except Exception:
                    print("\nDevice disconnected.")
                    break
    except KeyboardInterrupt:
        print("\nStopping playback...")
        try:
            req = urllib.request.Request(
                f"{base}/keypress/Stop", method="POST", data=b"",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Chromecast control (wraps pychromecast)
# ---------------------------------------------------------------------------

def _cast_media_chromecast(device, url, content_type, live):
    """Cast via pychromecast media controller."""
    cc = device["cast_obj"]
    mc = cc.media_controller
    stream_type = "LIVE" if live else "BUFFERED"
    mc.play_media(url, content_type, stream_type=stream_type)
    mc.block_until_active(timeout=60)
    print(f"Now casting to {device['name']}")


def _monitor_chromecast(device):
    """Monitor Chromecast playback until finished or Ctrl+C."""
    cc = device["cast_obj"]
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


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------

def cast_media(device, url, content_type="video/mp4", live=False):
    """Cast a media URL to the selected device."""
    proto = device["protocol"]
    if proto == "dlna":
        _cast_media_dlna(device, url, content_type)
    elif proto == "roku":
        _cast_media_roku(device, url)
    elif proto == "cast":
        _cast_media_chromecast(device, url, content_type, live)


def monitor_playback(device):
    """Monitor playback until finished or Ctrl+C."""
    proto = device["protocol"]
    if proto == "dlna":
        _monitor_dlna(device)
    elif proto == "roku":
        _monitor_roku(device)
    elif proto == "cast":
        _monitor_chromecast(device)


def stop_device(device):
    """Best-effort stop for any protocol."""
    try:
        proto = device["protocol"]
        if proto == "dlna":
            _dlna_soap_action(device["control_url"], "Stop")
        elif proto == "roku":
            base = f"http://{device['host']}:{device['port']}"
            req = urllib.request.Request(f"{base}/keypress/Stop", method="POST", data=b"")
            urllib.request.urlopen(req, timeout=5)
        elif proto == "cast" and device["cast_obj"]:
            device["cast_obj"].media_controller.stop()
    except Exception:
        pass


def select_device(devices):
    """Display devices and let the user pick one."""
    if not devices:
        msg = "No devices found."
        if not HAS_PYCHROMECAST:
            msg += " (Chromecast discovery skipped — pychromecast not installed)"
        print(msg)
        sys.exit(1)

    proto_tag = {"cast": "Cast", "dlna": "DLNA", "roku": "Roku"}
    print(f"\nFound {len(devices)} device(s):\n")
    for i, dev in enumerate(devices):
        tag = proto_tag.get(dev["protocol"], dev["protocol"])
        print(f"  [{i}] {dev['name']} ({dev['model']}) [{tag}]")

    print()
    while True:
        choice = input("Select device number: ").strip()
        try:
            idx = int(choice)
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        print(f"Enter a number between 0 and {len(devices) - 1}.")


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
    print("Scanning for devices (15s)...")
    devices = discover_all(timeout=15)
    server = None
    tmp_path = None
    ffmpeg_proc = None

    try:
        device = select_device(devices)
        print(f"\nSelected: {device['name']} [{device['protocol'].upper()}]\n")

        url = input("Paste URL to cast: ").strip()
        if not url:
            print("No URL provided.")
            sys.exit(1)

        # NOTE: Roku YouTube shortcut disabled — launching the native YouTube
        # app gives no playback-finished signal, which breaks queue support.
        # All URLs now go through yt-dlp/ffmpeg/serve so we get uniform
        # media-player state polling across all protocols.
        #
        # if device["protocol"] == "roku" and _extract_youtube_id(url):
        #     print("Launching YouTube on Roku...")
        #     cast_media(device, url)
        #     monitor_playback(device)
        #     return

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

        if device["protocol"] == "cast":
            print("Connecting to device...")
            device["cast_obj"].wait()
        cast_media(device, cast_target, content_type, live=is_live)
        monitor_playback(device)
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
        if _cast_browser:
            _cast_browser.stop_discovery()


if __name__ == "__main__":
    main()
