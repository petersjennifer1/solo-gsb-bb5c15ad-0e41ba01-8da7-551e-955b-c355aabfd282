"""WebVTT export with cue vertical positions (top / bottom)."""

from __future__ import annotations

from typing import Sequence

from .place import PlacedSegment


def format_timestamp(ms: int) -> str:
    """Milliseconds -> ``HH:MM:SS.mmm`` (WebVTT uses a dot, not a comma)."""
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _cue_settings(position: str) -> str:
    # WebVTT line numbers are counted from the top; negative numbers count
    # from the bottom.  line:0 hugs the top edge, line:-1 the bottom edge.
    return "line:0" if position == "top" else "line:-1"


def to_vtt(segments: Sequence[PlacedSegment]) -> str:
    blocks = ["WEBVTT"]
    for seg in segments:
        blocks.append(
            f"{seg.index + 1}\n"
            f"{format_timestamp(seg.start_ms)} --> {format_timestamp(seg.end_ms)} "
            f"{_cue_settings(seg.position)}\n"
            f"{seg.text}"
        )
    return "\n\n".join(blocks) + "\n"
