"""Runtime configuration.

Defaults for the segmentation parameters can be overridden via environment
variables; every request may still override them per-call.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_MAX_CHARS = _int_env("DEFAULT_MAX_CHARS", 42)
DEFAULT_MIN_DURATION_MS = _int_env("DEFAULT_MIN_DURATION_MS", 1000)
DEFAULT_MAX_DURATION_MS = _int_env("DEFAULT_MAX_DURATION_MS", 7000)
DEFAULT_TARGET_DURATION_MS = _int_env("DEFAULT_TARGET_DURATION_MS", 3000)
DEFAULT_PAUSE_THRESHOLD_MS = _int_env("DEFAULT_PAUSE_THRESHOLD_MS", 500)

# Penalty added to the total cost for every sentence-ending-punctuation or
# long-pause boundary that ends up *inside* a subtitle instead of on its edge.
CROSSED_BOUNDARY_PENALTY = 4.0

# Penalty added whenever two consecutive subtitles are placed in different
# vertical areas (one top, one bottom).
AREA_SWITCH_PENALTY = 1.0

# Fixed score added to the highlight-planning objective for every selected
# clip, on top of its duration in milliseconds.
HIGHLIGHT_CLIP_PENALTY = 1000
