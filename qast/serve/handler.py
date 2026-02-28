"""HTTP handler that streams from a ring buffer."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler

from .. import config
from ..log import get_logger
from ..pipeline.ringbuf import RingBuffer

log = get_logger("serve.handler")


class StreamHandler(BaseHTTPRequestHandler):
    """Serves media data from a ring buffer as a continuous stream.

    Sends DLNA headers for compatibility.  For DLNA renderers a fake
    Content-Length is included so Samsung TVs (which reject streams
    without one) start playback.
    """

    ring_buffer: RingBuffer | None = None
    content_type: str = "video/mp4"
    disconnect_event: threading.Event | None = None
    fake_content_length: bool = False
    serve_gate: threading.Event | None = None

    def do_HEAD(self) -> None:
        log.debug("HEAD from %s", self.client_address[0])
        self.send_response(200)
        self._send_headers()
        self.end_headers()

    def do_GET(self) -> None:
        log.debug("GET from %s", self.client_address[0])
        gate = self.__class__.serve_gate
        if gate is not None and not gate.is_set():
            log.debug("Gated — holding request from %s", self.client_address[0])
            gate.wait()
            log.debug("Gate opened — dropping held connection from %s",
                       self.client_address[0])
            return
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
            if self.disconnect_event is not None:
                self.disconnect_event.set()
        else:
            log.info("Stream finished, sent %d bytes", total)

    def _send_headers(self) -> None:
        self.send_header("Content-Type", self.content_type)
        if self.fake_content_length:
            # Fake large Content-Length so DLNA renderers (Samsung etc.)
            # accept the stream.  Connection closes when real data ends.
            self.send_header("Content-Length", str(config.DLNA_FAKE_CONTENT_LENGTH))
        self.send_header("Connection", "close")
        self.send_header("Accept-Ranges", "none")
        self.send_header("contentFeatures.dlna.org", config.DLNA_FLAGS)
        self.send_header("transferMode.dlna.org", "Streaming")

    def log_message(self, format, *args) -> None:
        # Suppress default access log — we use our own logger
        pass
