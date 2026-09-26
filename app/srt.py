"""SRT (SubRip) export."""

from __future__ import annotations

from typing import Sequence

from .segmenter import SegmentResult


def format_timestamp(ms: int) -> str:
    """Milliseconds -> ``HH:MM:SS,mmm``."""
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def to_srt(segments: Sequence[SegmentResult]) -> str:
    blocks = []
    for seg in segments:
        blocks.append(
            f"{seg.index + 1}\n"
            f"{format_timestamp(seg.start_ms)} --> {format_timestamp(seg.end_ms)}\n"
            f"{seg.text}"
        )
    return "\n\n".join(blocks) + "\n"
