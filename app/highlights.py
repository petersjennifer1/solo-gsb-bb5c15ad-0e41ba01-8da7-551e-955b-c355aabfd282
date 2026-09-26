"""Highlight-reel clip planning over a word-level transcript.

Pure Python (no web dependencies) so it can be unit-tested in isolation.

Besides the usual word-level transcript (at most 100 words), the request
carries 1..8 *target phrases* — each a non-empty list of word texts — and a
positive per-clip duration limit in milliseconds.  A phrase *hits* wherever
its word texts exactly match a run of consecutive transcript words;
repeated and overlapping hits all count.

A *clip* is any span of consecutive words ``i..j``: its timecodes are the
first word's start and the last word's end, and its duration may not exceed
the limit.  A clip *covers* a phrase when it fully contains one of the
phrase's hits.  The planner selects clips that are pairwise non-overlapping
in time (touching at an endpoint is allowed), covers every phrase at least
once, and minimises::

    total clip duration in milliseconds + 1000 per clip

Ties are broken by fewer clips, then by the lexicographically smaller
sequence of ``(word_start, word_end)`` index pairs (compared in time
order).  If no clip set can cover every phrase, the caller gets
:class:`SegmentationError` (HTTP 422).

With at most 8 target phrases, an exact dynamic program over
(word position, still-uncovered phrase bitmask) is cheap: for every phrase
``hit_end[p][i]`` precomputes the earliest end index of a hit starting at
word ``i`` or later, so a clip ``i..j`` covers phrase ``p`` iff
``hit_end[p][i] <= j``.  Each state keeps only ``(score, clip count)`` plus
its first clip, because the lexicographic tie-break decomposes locally: on
a score/count tie a *take* always beats a *skip* (its first index ``i`` is
smaller than anything a skip can produce), and between two takes the
smaller end index wins regardless of the suffixes.  The optimal clip
sequence is then rebuilt by walking the stored decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import HIGHLIGHT_CLIP_PENALTY
from .segmenter import SegmentationError, WordLike

# Request-size limits mirrored by the API schema (checked again here so the
# pure function stays safe to call directly).
MAX_WORDS = 100
MAX_PHRASES = 8


@dataclass
class ClipResult:
    index: int
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int  # index of the first word, inclusive
    word_end: int  # index of the last word, inclusive
    phrases: list[int]  # indices of the target phrases this clip covers


@dataclass
class HighlightsResult:
    clips: list[ClipResult]
    total_score: int


def _validate(
    words: Sequence[WordLike],
    phrases: Sequence[Sequence[str]],
    max_clip_ms: int,
) -> None:
    """Check the request shape; raises :class:`SegmentationError` (422)."""
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")
    if n > MAX_WORDS:
        raise SegmentationError(
            f"words must contain at most {MAX_WORDS} words (got {n})"
        )
    if not 1 <= len(phrases) <= MAX_PHRASES:
        raise SegmentationError(
            f"phrases must contain between 1 and {MAX_PHRASES} target "
            f"phrases (got {len(phrases)})"
        )
    if max_clip_ms <= 0:
        raise SegmentationError(
            f"max_clip_ms must be a positive integer (got {max_clip_ms})"
        )
    for p, phrase in enumerate(phrases):
        if not phrase:
            raise SegmentationError(
                f"phrase {p} must contain at least one word text"
            )
        for t in phrase:
            if not t:
                raise SegmentationError(
                    f"phrase {p} contains an empty word text"
                )
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


def plan_highlights(
    words: Sequence[WordLike],
    phrases: Sequence[Sequence[str]],
    *,
    max_clip_ms: int,
) -> HighlightsResult:
    """Pick non-overlapping clips that cover every target phrase.

    ``words`` is the time-ordered, non-overlapping transcript; ``phrases``
    holds 1..8 target phrases, each a non-empty list of word texts matched
    exactly against consecutive words.  Returns the optimal clips in time
    order plus the total score (sum of clip durations in milliseconds plus
    1000 per clip).  Raises :class:`SegmentationError` on illegal input or
    when no clip set within ``max_clip_ms`` covers every phrase.
    """
    _validate(words, phrases, max_clip_ms)
    n = len(words)
    m = len(phrases)

    # ---- earliest hit end --------------------------------------------------
    # hit_end[p][i] = smallest inclusive end index of a hit of phrase p that
    # starts at word i or later (None when no such hit exists).  A clip
    # spanning words i..j covers phrase p iff hit_end[p][i] <= j.
    hit_end: list[list[int | None]] = [[None] * (n + 1) for _ in range(m)]
    for p, phrase in enumerate(phrases):
        plen = len(phrase)
        row = hit_end[p]
        for i in range(n - 1, -1, -1):
            best_end = row[i + 1]
            if i + plen <= n and all(
                words[i + k].text == phrase[k] for k in range(plen)
            ):
                end = i + plen - 1
                if best_end is None or end < best_end:
                    best_end = end
            row[i] = best_end
        if row[0] is None:
            raise SegmentationError(
                f"phrase {p} ({list(phrase)!r}) never occurs as consecutive "
                "words in the transcript, so no clip plan can cover every "
                "target phrase"
            )

    # ---- candidate clips per start word ------------------------------------
    # clips_from[i] = [(end j, duration + per-clip penalty, coverage mask)]
    # for every clip starting at word i that fits the limit and covers at
    # least one phrase (a clip covering nothing can never be part of an
    # optimal plan: dropping it keeps the coverage and lowers the score).
    clips_from: list[list[tuple[int, int, int]]] = [[] for _ in range(n)]
    for i in range(n):
        cov = 0
        for j in range(i, n):
            dur = words[j].end_ms - words[i].start_ms
            if dur > max_clip_ms:
                break  # only grows as j grows
            for p in range(m):
                if hit_end[p][i] == j:
                    cov |= 1 << p
            if cov:
                clips_from[i].append((j, dur + HIGHLIGHT_CLIP_PENALTY, cov))

    # ---- dynamic programming -----------------------------------------------
    # dp[i][mask] = (score, clip count, first clip's end word) of the best
    # plan covering the still-uncovered phrases in ``mask`` with clips that
    # start at word i or later; the end word is -1 when the plan starts no
    # clip at i (word i is skipped).  None when impossible.  Scores and
    # counts are integers; on a tie a take (end word >= 0) beats the skip
    # and, clips being enumerated by ascending end, the first take reaching
    # a tie keeps the smallest end index -- exactly the lexicographic
    # tie-break on the (word_start, word_end) sequence.
    full = (1 << m) - 1
    dp: list[list[tuple[int, int, int] | None]] = [
        [None] * (1 << m) for _ in range(n + 1)
    ]
    dp[n][0] = (0, 0, -1)

    for i in range(n - 1, -1, -1):
        row = dp[i]
        skip_row = dp[i + 1]
        clips_i = clips_from[i]
        for mask in range(1 << m):
            skip = skip_row[mask]  # word i starts no clip
            best = None if skip is None else (skip[0], skip[1], -1)
            best_key = None if best is None else (best[0], best[1])
            for j, add, cov in clips_i:
                if cov & mask == 0:
                    continue  # covers nothing still needed
                sub = dp[j + 1][mask & ~cov]
                if sub is None:
                    continue
                key = (add + sub[0], 1 + sub[1])
                if (
                    best_key is None
                    or key < best_key
                    or (key == best_key and best[2] == -1)
                ):
                    best = (key[0], key[1], j)
                    best_key = key
            row[mask] = best

    if dp[0][full] is None:
        raise SegmentationError(
            "no set of non-overlapping clips with duration <= "
            f"max_clip_ms={max_clip_ms} covers all {m} target phrase(s): "
            "every clip must fully contain a hit of each phrase it covers, "
            "so try raising max_clip_ms or shortening the phrases"
        )

    # ---- rebuild the clips by walking the stored decisions ------------------
    clips: list[ClipResult] = []
    total_score = 0
    i, mask = 0, full
    while mask:
        state = dp[i][mask]
        assert state is not None  # dp[0][full] was reachable
        _score, _count, j = state
        if j == -1:
            i += 1
            continue
        covered = [
            p
            for p in range(m)
            if hit_end[p][i] is not None and hit_end[p][i] <= j
        ]
        clips.append(
            ClipResult(
                index=len(clips),
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                phrases=covered,
            )
        )
        total_score += clips[-1].duration_ms + HIGHLIGHT_CLIP_PENALTY
        for p in covered:
            mask &= ~(1 << p)
        i = j + 1
    return HighlightsResult(clips=clips, total_score=total_score)
