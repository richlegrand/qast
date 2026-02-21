"""HTTP handler that streams from a ring buffer."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from .. import config
from ..log import get_logger
from ..pipeline.ringbuf import RingBuffer

log = get_logger("serve.handler")


class StreamHandler(BaseHTTPRequestHandler):
    """Serves fMP4 data from a ring buffer as a continuous stream.

    No Content-Length (continuous stream). Sends DLNA headers for compatibility.
    The ring_buffer class attribute must be set before requests arrive.
    """

    ring_buffer: RingBuffer | None = None

    def do_HEAD(self) -> None:
        log.debug("HEAD from %s", self.client_address[0])
        self.send_response(200)
        self._send_headers()
        self.end_headers()

    def do_GET(self) -> None:
        log.debug("GET from %s", self.client_address[0])
        buf = self.ring_buffer
        if buf is None:
            self.send_error(503, "No stream available")
            return

        self.send_response(200)
        self._send_headers()
        self.end_headers()

        total = 0
        try:
            while True:
                chunk = buf.read(config.PIPE_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            log.info("Client disconnected after %d bytes", total)
        else:
            log.info("Stream finished, sent %d bytes", total)

    def _send_headers(self) -> None:
        self.send_header("Content-Type", "video/mp4")
        # No Content-Length (unknown size) and no Transfer-Encoding.
        # Connection close signals end-of-data (HTTP/1.0 style).
        self.send_header("Connection", "close")
        self.send_header("contentFeatures.dlna.org", config.DLNA_FLAGS)
        self.send_header("transferMode.dlna.org", "Streaming")

    def log_message(self, format, *args) -> None:
        # Suppress default access log — we use our own logger
        pass
