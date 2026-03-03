"""Local UI router for public site, docs, and local admin app."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class UIRoots:
    site: Path
    docs: Path
    admin: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ui_roots() -> UIRoots:
    root = _repo_root() / "ui"
    if not (root / "site").is_dir() or not (root / "docs").is_dir() or not (root / "admin").is_dir():
        root = Path(__file__).resolve().parent / "assets"
    return UIRoots(site=root / "site", docs=root / "docs", admin=root / "admin")


def _safe_path(root: Path, rel: str) -> Path | None:
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


class _UIHandler(BaseHTTPRequestHandler):
    server_version = "qast-ui/0.1"
    roots = _ui_roots()

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": "qast-ui"})
            return

        if path == "/api/ui-targets":
            self._send_json(
                200,
                {
                    "site": "/",
                    "docs": "/docs",
                    "admin": "/admin",
                },
            )
            return

        if path in ("/", "/index.html"):
            self._serve_file(self.roots.site / "index.html")
            return

        if path in ("/docs", "/docs/"):
            self._serve_file(self.roots.docs / "index.html")
            return

        if path.startswith("/docs/"):
            rel = path[len("/docs/") :]
            fpath = _safe_path(self.roots.docs, rel)
            if fpath is None:
                self.send_error(400, "Invalid path")
                return
            self._serve_file(fpath)
            return

        if path.startswith("/admin"):
            rel = path[len("/admin") :].lstrip("/")
            if not rel:
                rel = "index.html"
            fpath = _safe_path(self.roots.admin, rel)
            if fpath is None:
                self.send_error(400, "Invalid path")
                return
            self._serve_file(fpath)
            return

        if path.startswith("/site/"):
            rel = path[len("/site/") :]
            fpath = _safe_path(self.roots.site, rel)
            if fpath is None:
                self.send_error(400, "Invalid path")
                return
            self._serve_file(fpath)
            return

        self.send_error(404, "Not found")


def run_ui_server(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run local UI server that hosts site, docs, and admin UI."""
    server = ThreadingHTTPServer((host, port), _UIHandler)
    print(f"UI server listening on http://{host}:{port}")
    print(f"  Public website: http://{host}:{port}/")
    print(f"  Documentation:  http://{host}:{port}/docs")
    print(f"  Local admin:    http://{host}:{port}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
