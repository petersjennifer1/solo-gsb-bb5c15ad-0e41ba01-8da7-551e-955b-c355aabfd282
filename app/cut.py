"""Re-segmentation of a transcript after cutting ranges from the video.

Pure Python (no web dependencies) so it can be unit-tested in isolation.

The request carries *deletion intervals* on the original video timeline
(milliseconds).  The intervals must be sorted by start time, pairwise
non-overlapping (touching at an endpoint is allowed) and each must
satisfy ``start < end``.  Words fully covered by an interval are dropped;
a word that intersects an interval boundary without being fully covered
is rejected, because the cut would slice through spoken text.

Surviving words are shifted earlier by the total duration of the
intervals preceding them — the timeline of the edited video — and every
deletion interval forces a subtitle edge: words on opposite sides of a
cut may never share a subtitle.  Each maximal run of surviving words (a
*fragment*) is therefore segmented independently with the regular
dynamic-programming optimiser (same character, duration and two-line
typesetting rules), and the resulting subtitles keep the word indices of
the original request.  Deleting every word, or a fragment that admits no
legal segmentation, raises :class:`SegmentationError` (HTTP 422).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .segmenter import (
    SegmentResult,
    SegmentationError,
    SegmentationResult,
    WordLike,
    segment_words,
)


@dataclass
class _KeptWord:
    """A surviving word on the shifted timeline plus its request index."""

    text: str
    start_ms: int
    end_ms: int
    orig_index: int


def _validate_deletions(
    deletions: Sequence[Sequence[int]],
) -> tuple[tuple[int, int], ...]:
    """Check the deletion intervals and return them as ``(start, end)`` pairs.

    Intervals must be sorted by start time, pairwise non-overlapping
    (touching at an endpoint is allowed) and each must satisfy
    ``start < end``.  Raises :class:`SegmentationError` otherwise.
    """
    checked: list[tuple[int, int]] = []
    for pair in deletions:
        start, end = pair
        if start >= end:
            raise SegmentationError(
                f"deletion interval [{start}, {end}] must satisfy "
                "start_ms < end_ms"
            )
        if checked:
            prev_start, prev_end = checked[-1]
            if start < prev_start:
                raise SegmentationError(
                    "deletion intervals must be sorted by start time: "
                    f"[{start}, {end}] begins before "
                    f"[{prev_start}, {prev_end}]"
                )
            if start < prev_end:
                raise SegmentationError(
                    f"deletion intervals [{prev_start}, {prev_end}] and "
                    f"[{start}, {end}] overlap; intervals must be pairwise "
                    "non-overlapping"
                )
        checked.append((start, end))
    return tuple(checked)


def segment_with_deletions(
    words: Sequence[WordLike],
    deletions: Sequence[Sequence[int]],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
    max_line_chars: int | None = None,
) -> SegmentationResult:
    """Segment the transcript that remains after cutting ``deletions``.

    ``words`` is the time-ordered, non-overlapping transcript of the
    *original* video; ``deletions`` holds ``(start_ms, end_ms)`` intervals
    on that same timeline.  Returns subtitles on the edited (shifted)
    timeline whose ``word_start`` / ``word_end`` are indices into the
    original request.  Raises :class:`SegmentationError` on illegal
    intervals, words partially cut by a deletion boundary, an empty
    remainder, or a fragment with no legal segmentation.
    """
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")
    cuts = _validate_deletions(deletions)

    # The original transcript must be time-ordered for the cut arithmetic
    # to make sense; per-word constraint checks (length, duration, ...)
    # happen per fragment below and only concern the surviving words.
    for idx, w in enumerate(words):
        if w.end_ms <= w.start_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) must satisfy end_ms > start_ms "
                f"(got {w.start_ms}..{w.end_ms})"
            )
    for i in range(1, n):
        if words[i].start_ms < words[i - 1].end_ms:
            raise SegmentationError(
                f"words must be sorted by time and non-overlapping: word {i} "
                f"starts at {words[i].start_ms} ms but word {i - 1} ends at "
                f"{words[i - 1].end_ms} ms"
            )

    # ---- drop covered words, shift survivors, split into fragments ------
    # ``di`` is the first interval not entirely before the current word and
    # ``shift`` the total duration of the intervals before it.  Surviving
    # words sharing the same ``di`` form one fragment; every interval
    # crossed along the way starts a new fragment, so the two sides of a
    # deletion always end up in different subtitles.
    fragments: list[list[_KeptWord]] = []
    frag_di: int | None = None  # ``di`` of the current fragment's words
    di = 0
    shift = 0
    for idx, w in enumerate(words):
        while di < len(cuts) and cuts[di][1] <= w.start_ms:
            shift += cuts[di][1] - cuts[di][0]
            di += 1
        if di < len(cuts):
            ds, de = cuts[di]
            # de > w.start_ms here, so the word intersects the interval
            # exactly when it reaches past the interval's start.
            if w.end_ms > ds:
                if ds <= w.start_ms and w.end_ms <= de:
                    continue  # fully inside the cut -> dropped
                raise SegmentationError(
                    f"word {idx} ({w.text!r}) spans {w.start_ms}..{w.end_ms} "
                    f"ms and intersects the deletion interval [{ds}, {de}] "
                    "without being fully covered by it; deletion boundaries "
                    "must not cut through a word"
                )
        kept = _KeptWord(w.text, w.start_ms - shift, w.end_ms - shift, idx)
        if frag_di == di and fragments:
            fragments[-1].append(kept)
        else:
            fragments.append([kept])
            frag_di = di

    if not fragments:
        raise SegmentationError(
            "the deletion intervals cover every word; nothing is left to "
            "segment"
        )

    # ---- optimise each fragment and stitch the results together ---------
    segments: list[SegmentResult] = []
    total_cost = 0.0
    for frag_no, frag_words in enumerate(fragments):
        try:
            res = segment_words(
                frag_words,
                max_chars=max_chars,
                min_duration_ms=min_duration_ms,
                max_duration_ms=max_duration_ms,
                target_duration_ms=target_duration_ms,
                pause_threshold_ms=pause_threshold_ms,
                max_line_chars=max_line_chars,
            )
        except SegmentationError as exc:
            raise SegmentationError(
                f"fragment {frag_no} (kept words {frag_words[0].orig_index}"
                f"..{frag_words[-1].orig_index} of the request) cannot be "
                f"segmented: {exc}"
            ) from exc
        for seg in res.segments:
            segments.append(
                SegmentResult(
                    index=len(segments),
                    text=seg.text,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                    duration_ms=seg.duration_ms,
                    word_start=frag_words[seg.word_start].orig_index,
                    word_end=frag_words[seg.word_end].orig_index,
                    crossed_boundaries=seg.crossed_boundaries,
                )
            )
        total_cost += res.total_cost

    return SegmentationResult(segments=segments, total_cost=total_cost)
