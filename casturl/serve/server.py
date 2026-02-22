"""HTTP server lifecycle."""

from __future__ import annotations

import threading
from http.server import HTTPServer

from .. import config
from ..log import get_logger
from ..net import get_local_ip
from ..pipeline.ringbuf import RingBuffer
from .handler import StreamHandler

log = get_logger("serve.server")


class StreamServer:
    """Wraps HTTPServer in a daemon thread. Provides url property and stop()."""

    def __init__(self, ring_buffer: RingBuffer, content_type: str = "video/mp4") -> None:
        self._ring_buffer = ring_buffer
        self._content_type = content_type
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None

    def start(self) -> None:
        # Set class-level attributes on the handler
        StreamHandler.ring_buffer = self._ring_buffer
        StreamHandler.content_type = self._content_type

        self._server = HTTPServer(
            (config.HTTP_BIND, config.HTTP_PORT),
            StreamHandler,
        )
        port = self._server.server_address[1]
        local_ip = get_local_ip()
        self._url = f"http://{local_ip}:{port}/stream"

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        log.info("HTTP server started at %s", self._url)

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("Server not started")
        return self._url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            log.debug("HTTP server stopped")
