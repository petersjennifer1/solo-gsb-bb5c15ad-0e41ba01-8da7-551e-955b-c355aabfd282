"""Joint subtitle segmentation and vertical placement (top / bottom).

Pure Python (no web dependencies) so it can be unit-tested in isolation.

Besides the usual word-level transcript and segmentation parameters, the
request carries *occlusions* on the video timeline, each tagged with an
area — ``"top"`` or ``"bottom"``.  An area is available for a subtitle
only when the subtitle's own time span has **no positive-length
intersection** with any occlusion tagged with that area (intervals merely
touching at an endpoint do not block).  Every subtitle must therefore lie
in an area that stays clear for its whole duration.

Segmentation and placement are decided *together* by a single dynamic
program.  The candidate subtitles are exactly those of the plain
optimiser — same character limit, duration limits, two-line typesetting,
natural-boundary penalty and optional human-edited boundaries — and the
total cost adds one :data:`~app.config.AREA_SWITCH_PENALTY` point for
every pair of consecutive subtitles placed in different areas::

      segmentation cost (same objective as POST /segment)
      + 1 × number of adjacent subtitles switching between top and bottom

Ties are broken, in order, by:

1. fewer subtitles;
2. the lexicographically smaller area sequence, where **bottom < top**
   (so ``bottom`` wins every tie — the first differing subtitle decides).

If no subtitle of a feasible segmentation has an always-clear area
(including the case where some moment is occluded in both areas), no
solution exists and the caller gets :class:`SegmentationError`
(HTTP 422).

Occlusion intervals of one area must be given sorted by start time and be
pairwise non-overlapping (touching at an endpoint is allowed); top and
bottom intervals are independent and may interleave or overlap freely.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from .config import AREA_SWITCH_PENALTY
from .segmenter import (
    PreparedSegmentation,
    SegmentationError,
    WordLike,
    build_segment_text,
    iter_candidates,
    prepare_segmentation,
)

# Area codes ordered by the tie-break preference: bottom wins lexicographic
# ties, so it must compare smaller than top.
BOTTOM = 0
TOP = 1
AREAS = (BOTTOM, TOP)
AREA_NAMES = {BOTTOM: "bottom", TOP: "top"}
_AREA_CODES = {"bottom": BOTTOM, "top": TOP}

# An occlusion is (start_ms, end_ms, area_name).
Occlusion = tuple[int, int, str]


@dataclass(frozen=True)
class AreaBlocks:
    """Sorted occlusion interval endpoints (``starts``, ``ends``) for an area."""

    starts: tuple[int, ...]
    ends: tuple[int, ...]

    def blocks(self, start_ms: int, end_ms: int) -> bool:
        """True iff [start_ms, end_ms) has a positive-length intersection."""
        k = bisect_right(self.starts, start_ms) - 1
        if k >= 0 and self.ends[k] > start_ms:
            return True
        k += 1
        return k < len(self.starts) and self.starts[k] < end_ms


@dataclass
class PlacedSegment:
    index: int
    text: str
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int
    word_end: int
    crossed_boundaries: int
    position: str  # "top" | "bottom"


@dataclass
class PlacementResult:
    segments: list[PlacedSegment]
    total_cost: float


def _validate_occlusions(
    occlusions: Sequence[Occlusion],
) -> dict[int, AreaBlocks]:
    """Validate occlusions and group them per area into endpoint arrays."""
    per_area: dict[int, list[tuple[int, int]]] = {BOTTOM: [], TOP: []}
    for t, (start, end, area) in enumerate(occlusions):
        if area not in _AREA_CODES:
            raise SegmentationError(
                f"occlusion {t} has unknown area {area!r}; area must be "
                '"top" or "bottom"'
            )
        if start >= end:
            raise SegmentationError(
                f"occlusion {t} [{start}, {end}] in area {area!r} must "
                "satisfy start_ms < end_ms"
            )
        per_area[_AREA_CODES[area]].append((start, end))

    blocks: dict[int, AreaBlocks] = {}
    for area, intervals in per_area.items():
        checked: list[tuple[int, int]] = []
        for start, end in intervals:
            if checked:
                prev_start, prev_end = checked[-1]
                if start < prev_start:
                    raise SegmentationError(
                        f"occlusion intervals in area {AREA_NAMES[area]!r} must "
                        "be sorted by start time: "
                        f"[{start}, {end}] begins before "
                        f"[{prev_start}, {prev_end}]"
                    )
                if start < prev_end:
                    raise SegmentationError(
                        f"occlusion intervals [{prev_start}, {prev_end}] and "
                        f"[{start}, {end}] in area {AREA_NAMES[area]!r} overlap; "
                        "intervals of the same area must be pairwise "
                        "non-overlapping"
                    )
            # Touching endpoints (start == prev_end) are kept apart on
            # purpose: their union has no extra interior, and merging would
            # wrongly mark a subtitle ending / starting at that endpoint as
            # blocked even though the intersection has zero length.
            checked.append((start, end))
        blocks[area] = AreaBlocks(
            starts=tuple(s for s, _ in checked),
            ends=tuple(e for _, e in checked),
        )
    return blocks


@dataclass(frozen=True)
class _State:
    """Best suffix plan for ``dp[i][area]`` (first subtitle in ``area``)."""

    cost: float
    count: int
    seq: tuple[int, ...]  # area codes of the whole suffix, for tie-breaks
    nxt_end: int  # inclusive end index of the first subtitle
    nxt_area: int  # area of the second subtitle (ignored when count == 1)
    nxt_break: int  # first word of line 2 of the first subtitle (-1 = 1 line)


def _better(cand: _State | None, cur: _State | None, eps: float) -> bool:
    """Whether ``cand`` beats the incumbent ``cur`` under the tie order.

    Order: total cost (numeric), then fewer subtitles, then the area
    sequence lexicographically with bottom (0) < top (1).
    """
    if cur is None:
        return True
    if cand.cost < cur.cost - eps:
        return True
    if cand.cost > cur.cost + eps:
        return False
    if cand.count != cur.count:
        return cand.count < cur.count
    return cand.seq < cur.seq


def segment_and_place(
    words: Sequence[WordLike],
    occlusions: Sequence[Occlusion],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
    max_line_chars: int | None = None,
    required_break_after: Sequence[int] | None = None,
    keep_together: Sequence[Sequence[int]] | None = None,
) -> PlacementResult:
    """Segment ``words`` and assign each subtitle a clear top/bottom area.

    Raises :class:`SegmentationError` on illegal occlusions, on any input
    rejected by the plain segmenter, or when no segmentation exists whose
    every subtitle has a fully available area.
    """
    blocks = _validate_occlusions(occlusions)

    prep: PreparedSegmentation = prepare_segmentation(
        words,
        max_chars=max_chars,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        target_duration_ms=target_duration_ms,
        pause_threshold_ms=pause_threshold_ms,
        max_line_chars=max_line_chars,
        required_break_after=required_break_after,
        keep_together=keep_together,
    )
    n = len(words)
    eps = 1e-9

    # Empty suffix: zero cost / count for either preceding area.  The state
    # carries no area code; callers skip the switch term against it.
    terminal = _State(0.0, 0, (), -1, BOTTOM, -1)
    # dp[i][area] = best plan covering words i..n-1 whose first subtitle is
    # placed in ``area``.
    dp: list[list[_State | None]] = [
        [None, None] for _ in range(n + 1)
    ]
    dp[n] = [terminal, terminal]

    for i in range(n - 1, -1, -1):
        # Availability is monotone as the candidate span grows: once an area
        # is blocked, every wider span starting at i stays blocked in it.
        blocked_areas: set[int] = set()
        for cand in iter_candidates(prep, i):
            for area in AREAS:
                if area in blocked_areas:
                    continue
                if blocks[area].blocks(cand.start_ms, cand.end_ms):
                    blocked_areas.add(area)
                    continue
                for next_area in AREAS:
                    suffix = dp[cand.j + 1][next_area]
                    if suffix is None:
                        continue
                    switch = (
                        AREA_SWITCH_PENALTY
                        if suffix.count > 0 and next_area != area
                        else 0.0
                    )
                    state = _State(
                        cost=cand.base_cost + switch + suffix.cost,
                        count=1 + suffix.count,
                        seq=(area,) + suffix.seq,
                        nxt_end=cand.j,
                        nxt_area=next_area,
                        nxt_break=cand.break_word,
                    )
                    if _better(state, dp[i][area], eps):
                        dp[i][area] = state
            if len(blocked_areas) == len(AREAS):
                break  # no wider span can be placed anywhere

    best = None
    for area in AREAS:  # bottom first; both orders are covered by _better
        cand = dp[0][area]
        if cand is not None and _better(cand, best, eps):
            best = cand

    if best is None:
        layout_note = (
            f", max_line_chars={max_line_chars}" if max_line_chars is not None else ""
        )
        raise SegmentationError(
            "no legal segmentation covers all words under the given constraints "
            "and occlusion areas: every subtitle must stay for its whole "
            "duration within an area (top or bottom) that has no positive-"
            "length intersection with a same-area occlusion, while respecting "
            f"max_chars={max_chars}{layout_note}, min_duration_ms="
            f"{min_duration_ms}, max_duration_ms={max_duration_ms}; try "
            "relaxing min_duration_ms / the other limits or shortening the "
            "occluded spans"
        )

    # ---- reconstruct the optimal path --------------------------------------
    segments: list[PlacedSegment] = []
    i = 0
    state: _State | None = best
    while state is not None and state.count > 0:
        j = state.nxt_end
        area = state.seq[0]
        segments.append(
            PlacedSegment(
                index=len(segments),
                text=build_segment_text(words, i, j, state.nxt_break),
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                crossed_boundaries=prep.nat_prefix[j] - prep.nat_prefix[i],
                position=AREA_NAMES[area],
            )
        )
        i = j + 1
        state = dp[i][state.nxt_area]

    return PlacementResult(segments=segments, total_cost=best.cost)
