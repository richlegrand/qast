"""Master muxer ffmpeg — remuxes MPEG-TS from stdin to fMP4 on stdout."""

from __future__ import annotations

import subprocess
import threading

from .. import config
from ..log import get_logger
from .ringbuf import RingBuffer

log = get_logger("pipeline.master")


class MasterMuxer:
    """Long-lived ffmpeg that reads MPEG-TS from stdin and outputs fMP4.

    This is a -c copy operation (no re-encoding).
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_output: list[str] = []
        self._save_file = None  # IO[bytes] | None, set by start()

    def start(self, save_path: str | None = None) -> None:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "mpegts", "-i", "pipe:0",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]
        log.info("Master muxer: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if save_path:
            self._save_file = open(save_path, "wb")
            log.info("Saving fMP4 stream to %s", save_path)

    def start_reader(self, ring_buffer: RingBuffer) -> None:
        """Start daemon threads for stdout (into ring buffer) and stderr (log capture)."""

        def _reader():
            assert self.proc and self.proc.stdout
            try:
                while True:
                    chunk = self.proc.stdout.read(config.PIPE_CHUNK)
                    if not chunk:
                        break
                    ring_buffer.write(chunk)
                    try:
                        sf = self._save_file
                        if sf:
                            sf.write(chunk)
                    except Exception:
                        pass
            except Exception:
                log.debug("Master reader exception", exc_info=True)
            finally:
                ring_buffer.close()
                log.debug("Master reader finished")

        def _stderr_reader():
            assert self.proc and self.proc.stderr
            try:
                for line in self.proc.stderr:
                    text = line.decode(errors="replace").rstrip()
                    if text:
                        self._stderr_output.append(text)
                        log.debug("Master muxer: %s", text)
            except Exception:
                pass

        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
        self._stderr_thread.start()

    def close_save_file(self) -> None:
        """Close the save file (if open). Thread-safe."""
        f = self._save_file
        self._save_file = None
        if f:
            try:
                f.close()
            except Exception:
                pass

    @property
    def stdin(self):
        return self.proc.stdin if self.proc else None

    def wait(self) -> int:
        """Wait for the process to finish. Returns exit code."""
        if self.proc:
            rc = self.proc.wait()
            if self._reader_thread:
                self._reader_thread.join(timeout=5)
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)
            if rc != 0 and self._stderr_output:
                log.warning("Master muxer exited %d:\n%s", rc, "\n".join(self._stderr_output[-10:]))
            return rc
        return -1

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            log.debug("Master muxer killed")
