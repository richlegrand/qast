"""Operator assistance for setup, discovery, and failure triage.

Provides deterministic guidance locally, with optional Claude augmentation
when credentials are configured.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SetupReport:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _playlist_sources(playlist: str | None) -> list[str]:
    if not playlist or playlist == "-":
        return []
    p = Path(playlist)
    if not p.is_file():
        return []
    out: list[str] = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _has_any_prefix(values: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(v.startswith(prefixes) for v in values)


def analyze_setup(urls: list[str], playlist: str | None = None) -> SetupReport:
    """Inspect requested sources and return setup blockers/warnings."""
    report = SetupReport()
    all_sources = [*urls, *_playlist_sources(playlist)]

    if shutil.which("ffmpeg") is None:
        report.blockers.append("`ffmpeg` is required but not found in PATH.")

    needs_browser = _has_any_prefix(all_sources, ("browser:",))
    needs_window = any(
        s == "window" or s.startswith("window:") or s.startswith("window@")
        for s in all_sources
    )
    needs_webcam = any(s == "webcam" or s.startswith("webcam@") for s in all_sources)
    has_web_urls = any("://" in s for s in all_sources)

    if needs_browser:
        try:
            import playwright  # noqa: F401
        except Exception:
            report.blockers.append(
                "Browser capture requested, but Playwright is not installed. "
                "Install with `pip install playwright` and run `playwright install chromium`."
            )

    if os.name != "nt" and needs_window and shutil.which("xdotool") is None:
        report.blockers.append(
            "Window capture requested, but `xdotool` is missing. Install with `apt install xdotool`."
        )
    if os.name == "nt" and "window" in all_sources:
        report.blockers.append(
            "On Windows, interactive `window` selection is not supported. Use `window:<title>`."
        )

    if os.name != "nt" and needs_webcam and not Path("/dev/video0").exists():
        report.blockers.append("Webcam capture requested, but `/dev/video0` was not found.")
    if os.name == "nt" and needs_webcam:
        report.warnings.append(
            "On Windows, webcam capture uses ffmpeg dshow. "
            "If the default camera is wrong, set `QAST_WEBCAM_DEVICE` to the device name."
        )

    if has_web_urls and shutil.which("yt-dlp") is None:
        report.warnings.append(
            "`yt-dlp` is not installed. Direct URLs can still work, but YouTube and many sites may fail."
        )

    if shutil.which("qast-encoder") is None:
        bin_name = "qast-encoder.exe" if os.name == "nt" else "qast-encoder"
        local_rust = Path(__file__).resolve().parents[1] / "rust" / "qast-encoder" / "target" / "release" / bin_name
        if not local_rust.is_file():
            report.warnings.append(
                "Rust encoder binary not found. Build with "
                "`cargo build --manifest-path rust/qast-encoder/Cargo.toml --release`."
            )

    return report


class Advisor:
    """Optional Claude-backed advisor with deterministic fallback hints."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("QAST_AI_ENABLE", "0") == "1"
        self.api_key = os.environ.get("QAST_ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("QAST_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

    def _claude(self, prompt: str) -> str | None:
        if not self.enabled or not self.api_key:
            return None
        try:
            payload = {
                "model": self.model,
                "max_tokens": 220,
                "system": (
                    "You are a practical ops assistant for a LAN TV casting tool. "
                    "Return 3 concise actionable bullets. No markdown headings."
                ),
                "messages": [{"role": "user", "content": prompt}],
            }
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            content = data.get("content", [])
            if content and isinstance(content, list):
                first = content[0]
                text = first.get("text") if isinstance(first, dict) else None
                if isinstance(text, str) and text.strip():
                    return text.strip()
        except Exception:
            return None
        return None

    @staticmethod
    def _normalize_lines(text: str | None) -> list[str]:
        if not text:
            return []
        out: list[str] = []
        for line in text.splitlines():
            item = line.strip().lstrip("-*0123456789. ").strip()
            if item:
                out.append(item)
        return out[:4]

    def setup_hints(self, report: SetupReport) -> list[str]:
        fallback = [*report.blockers, *report.warnings]
        if not fallback:
            return []
        prompt = "Setup diagnostics:\n" + "\n".join(f"- {m}" for m in fallback)
        ai = self._normalize_lines(self._claude(prompt))
        return ai or fallback

    def discovery_hints(self, timeout: int, show_all: bool) -> list[str]:
        fallback = [
            "Confirm your computer and TV are on the same network/VLAN.",
            "Run with `-v` to inspect discovery behavior and protocol errors.",
            "If using Roku, ensure 'Control by mobile apps' is enabled and Media Assistant is installed.",
            "If devices are still missing, retry with a longer scan timeout.",
        ]
        prompt = (
            "No cast devices discovered. "
            f"timeout={timeout}s show_all={show_all}. "
            "Give concise remediation steps."
        )
        ai = self._normalize_lines(self._claude(prompt))
        return ai or fallback

    def cast_failure_hints(self, error_text: str, protocol: str | None) -> list[str]:
        low = error_text.lower()
        fallback: list[str] = []

        if "yt-dlp" in low or "youtube" in low:
            fallback.append("Update yt-dlp and retry (`pip install -U yt-dlp`).")
            fallback.append("Try `--cookies-from-browser chrome` to bypass bot checks.")
        if protocol == "dlna":
            fallback.append("For DLNA start-cutoff or probe issues, retry with `--preroll 5` (or higher).")
        if protocol == "roku":
            fallback.append("Install Roku Media Assistant and ensure 'Control by mobile apps' is enabled.")
        if "buffer" in low:
            fallback.append("For live capture, reduce source complexity or bitrate to help initial buffering.")

        fallback.append("Re-run with `-v` and inspect ffmpeg/protocol errors from the last 20 lines.")

        prompt = (
            f"Casting failed for protocol={protocol or 'unknown'}. "
            f"error={error_text}. Give 3 concise remediation steps."
        )
        ai = self._normalize_lines(self._claude(prompt))
        return ai or fallback


def safe_doctor_json(url: str) -> dict | None:
    """Best-effort JSON probe helper for future diagnostics.

    Useful for scanner endpoints or device health probes when needed.
    """
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def ffmpeg_is_usable() -> bool:
    """Run a short ffmpeg version probe for setup checks."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=4)
        return result.returncode == 0
    except Exception:
        return False
