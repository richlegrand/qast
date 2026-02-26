"""MPEG-TS packet rewriter.

Rewrites PTS/DTS/PCR timestamps and continuity counters in a raw MPEG-TS
byte stream.  Ensures seamless concatenation of independently-produced
segments into a single continuous transport stream.

- Timestamps: adds an integer offset (90 kHz ticks) to all PTS/DTS/PCR
- Continuity counters: maintains per-PID CC across segment boundaries
- Tracks the maximum PTS seen per segment for offset advancement
- Video correction: adjusts video PTS to match the audio master clock,
  preventing A/V drift from accumulating across segments

All arithmetic is in 90 kHz integer ticks — no floats.
"""

from __future__ import annotations

from ..log import get_logger

log = get_logger("pipeline.tsrewrite")

_SYNC = 0x47
_PKT = 188


class TSRewriter:
    """Rewrites MPEG-TS packets for seamless segment concatenation.

    Handles three things that break when you naively concatenate TS segments:
      1. PTS/DTS timestamps — adds an offset so they increase monotonically
      2. PCR — same offset applied to the program clock reference
      3. Continuity counters — maintained per-PID across segments

    Additionally applies a per-segment video PTS correction to keep video
    aligned with the audio master clock.  The decoder plays audio at 44100 Hz
    regardless of PTS values; video sync is controlled by the PTS difference
    between video and audio frames.  Each AAC frame renders 1024/44100 seconds
    (non-integer in 90 kHz ticks), so audio real time drifts from video PTS
    at each segment boundary.  The video correction compensates for this
    cumulative drift.
    """

    def __init__(self) -> None:
        self._offset: int = 0            # base offset (90 kHz ticks), applied to audio PTS/DTS and PCR
        self._video_correction: int = 0  # additional offset for video PTS/DTS only
        self._max_pts: int = 0           # highest video PTS seen (includes correction)
        self._audio_frame_count: int = 0 # audio PES headers seen in current segment
        self._video_frame_count: int = 0 # video PES headers seen in current segment
        self._buf = bytearray()          # partial-packet buffer
        self._cc: dict[int, int] = {}    # PID -> last continuity counter

    # ── public API ───────────────────────────────────────────────

    def set_offset(self, ticks: int, video_correction: int = 0) -> None:
        """Set the offset for the next segment and reset per-segment state.

        Does NOT reset continuity counters — those span segments.
        """
        self._offset = ticks
        self._video_correction = video_correction
        self._max_pts = 0
        self._audio_frame_count = 0
        self._video_frame_count = 0
        log.debug("TS rewriter: offset=%d, video_correction=%d (%.3fms)",
                  ticks, video_correction, video_correction / 90)

    @property
    def max_pts(self) -> int:
        return self._max_pts

    @property
    def audio_frame_count(self) -> int:
        return self._audio_frame_count

    @property
    def video_frame_count(self) -> int:
        return self._video_frame_count

    def process(self, data: bytes, max_video_frames: int = 0) -> bytes:
        """Rewrite all packets in *data* and return the modified bytes.

        When *max_video_frames* > 0, stop processing after the video frame
        count (within the current segment) reaches that limit.  Unprocessed
        data remains in the internal buffer for later calls or flush().
        """
        self._buf.extend(data)
        out = bytearray()

        while len(self._buf) >= _PKT:
            if max_video_frames and self._video_frame_count >= max_video_frames:
                break

            # Find sync byte
            if self._buf[0] != _SYNC:
                idx = self._buf.find(_SYNC)
                if idx == -1 or idx + _PKT > len(self._buf):
                    break
                del self._buf[:idx]
                continue

            pkt = bytearray(self._buf[:_PKT])
            del self._buf[:_PKT]
            self._rewrite_packet(pkt)
            out.extend(pkt)

        return bytes(out)

    def flush(self) -> bytes:
        """Return any remaining buffered bytes (partial packets)."""
        result = bytes(self._buf)
        self._buf.clear()
        return result

    # ── packet handling ──────────────────────────────────────────

    def _rewrite_packet(self, pkt: bytearray) -> None:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 0x03  # adaptation_field_control
        payload_start = 4

        # Rewrite continuity counter (except for null packets)
        if pid != 0x1FFF and afc in (1, 3):  # has payload
            expected = (self._cc.get(pid, -1) + 1) & 0x0F
            pkt[3] = (pkt[3] & 0xF0) | expected
            self._cc[pid] = expected

        # Adaptation field — may contain PCR
        if afc in (2, 3):
            af_len = pkt[4]
            if af_len > 0 and (pkt[5] & 0x10):  # PCR flag set
                self._rewrite_pcr(pkt, 6)
            payload_start = 5 + af_len

        # No payload → nothing more to do
        if afc not in (1, 3):
            return

        # PES header lives in packets with payload_unit_start_indicator
        if (pkt[1] & 0x40) and payload_start + 9 <= _PKT:
            self._rewrite_pes(pkt, payload_start)

    def _rewrite_pes(self, pkt: bytearray, pos: int) -> None:
        """Rewrite PTS and optional DTS in a PES header starting at *pos*."""
        if pkt[pos:pos + 3] != b'\x00\x00\x01':
            return

        stream_id = pkt[pos + 3]
        is_video = 0xE0 <= stream_id <= 0xEF
        flags = pkt[pos + 7]
        ts_pos = pos + 9

        if flags & 0x80:  # PTS present
            if ts_pos + 5 > _PKT:
                return
            pts = _read_ts(pkt, ts_pos)
            if is_video:
                self._video_frame_count += 1
                new_pts = (pts + self._offset + self._video_correction) & 0x1_FFFF_FFFF
                # Track uncorrected PTS for offset computation — avoids
                # feedback loop where correction inflates max_pts
                uncorrected = (pts + self._offset) & 0x1_FFFF_FFFF
                if uncorrected > self._max_pts:
                    self._max_pts = uncorrected
            else:
                new_pts = (pts + self._offset) & 0x1_FFFF_FFFF
                self._audio_frame_count += 1
            marker = 0x30 if (flags & 0x40) else 0x20
            _write_ts(pkt, ts_pos, new_pts, marker)
            ts_pos += 5

        if flags & 0x40:  # DTS present
            if ts_pos + 5 > _PKT:
                return
            dts = _read_ts(pkt, ts_pos)
            if is_video:
                new_dts = (dts + self._offset + self._video_correction) & 0x1_FFFF_FFFF
            else:
                new_dts = (dts + self._offset) & 0x1_FFFF_FFFF
            _write_ts(pkt, ts_pos, new_dts, 0x10)

    def _rewrite_pcr(self, pkt: bytearray, pos: int) -> None:
        """Rewrite the 6-byte PCR field at *pos* in the adaptation field."""
        if pos + 6 > _PKT:
            return

        base = (
            pkt[pos] << 25
            | pkt[pos + 1] << 17
            | pkt[pos + 2] << 9
            | pkt[pos + 3] << 1
            | pkt[pos + 4] >> 7
        )
        ext = ((pkt[pos + 4] & 0x01) << 8) | pkt[pos + 5]

        new_base = (base + self._offset) & 0x1_FFFF_FFFF

        pkt[pos]     = (new_base >> 25) & 0xFF
        pkt[pos + 1] = (new_base >> 17) & 0xFF
        pkt[pos + 2] = (new_base >> 9) & 0xFF
        pkt[pos + 3] = (new_base >> 1) & 0xFF
        pkt[pos + 4] = ((new_base & 1) << 7) | 0x7E | ((ext >> 8) & 0x01)
        pkt[pos + 5] = ext & 0xFF


# ── timestamp read / write helpers (module-level for speed) ──────

def _read_ts(pkt: bytearray, pos: int) -> int:
    """Read a 33-bit PTS or DTS from the 5-byte encoding at *pos*."""
    return (
        ((pkt[pos] >> 1) & 0x07) << 30
        | pkt[pos + 1] << 22
        | (pkt[pos + 2] >> 1) << 15
        | pkt[pos + 3] << 7
        | pkt[pos + 4] >> 1
    )


def _write_ts(pkt: bytearray, pos: int, val: int, marker: int) -> None:
    """Write a 33-bit PTS or DTS into the 5-byte encoding at *pos*."""
    pkt[pos]     = marker | ((val >> 29) & 0x0E) | 0x01
    pkt[pos + 1] = (val >> 22) & 0xFF
    pkt[pos + 2] = ((val >> 14) & 0xFE) | 0x01
    pkt[pos + 3] = (val >> 7) & 0xFF
    pkt[pos + 4] = ((val << 1) & 0xFE) | 0x01
