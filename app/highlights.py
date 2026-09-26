"""Local highlight-reel planning (``POST /highlights``).

Pure Python (no web dependencies) so it can be unit-tested in isolation.

The request reuses the time-ordered, non-overlapping word-level transcript
of the subtitle endpoints, together with 1..8 *target phrases* (each a
non-empty list of word texts) and a positive per-clip duration cap.

* a phrase *hits* at word ``h`` when the texts of the consecutive words
  ``h .. h + len(phrase) - 1`` equal the phrase element by element; the
  match is exact and case-sensitive.  Repeated hits (the same phrase
  occurring several times) and overlapping hits (e.g. ``["a", "a"]`` inside
  ``a a a``) all count;
* a *clip* is a consecutive run of request words ``[i, j]`` whose only
  timecodes are ``words[i].start_ms`` / ``words[j].end_ms`` and whose
  duration does not exceed the cap; clips may start/end away from a hit;
* a clip *covers* a phrase iff it fully contains one of that phrase's hits;
* the chosen clips must be pairwise non-overlapping in time (clips over
  disjoint word runs merely touch at a word boundary, which is allowed) and
  together cover every target phrase at least once;
* the objective minimises::

      sum of clip durations in milliseconds
      + 1000 points for every chosen clip

  ties are broken in favour of fewer clips, then of the lexicographically
  smaller time-ordered ``(word_start, word_end)`` pair sequence.

If no such selection exists (a phrase never occurs, every clip containing
some hit exceeds the duration cap, or the hits cannot be packed into
pairwise non-overlapping clips), :class:`SegmentationError` is raised and
mapped to HTTP 422 by the endpoint.

A dynamic program over (word position, still-needed phrase bit-mask) solves
this exactly: with at most 100 words and 8 phrases it has 101 * 256
states, each extended over at most 100 clip candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .segmenter import SegmentationError, WordLike

# Fixed per-clip surcharge added on top of the clip's own duration.
PER_CLIP_FEE_MS = 1000

# Request-shape limits (mirrored by the Pydantic model); the core keeps its
# own guards so direct callers get uniform errors.
MAX_WORDS = 100
MAX_PHRASES = 8


@dataclass(frozen=True)
class HighlightClip:
    """One chosen clip, described on both the timeline and the word list."""

    index: int
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int  # index of the clip's first word, inclusive
    word_end: int  # index of the clip's last word, inclusive
    covered_phrases: tuple[int, ...]  # zero-based request phrase indices


@dataclass(frozen=True)
class HighlightPlan:
    clips: tuple[HighlightClip, ...]
    total_score: int


def _validate_words(
    words: Sequence[WordLike], max_clip_duration_ms: int
) -> None:
    """Check the transcript and the cap, mirroring the segmenter's rules."""
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")
    if n > MAX_WORDS:
        raise SegmentationError(
            f"words may contain at most {MAX_WORDS} words (got {n})"
        )
    if max_clip_duration_ms <= 0:
        raise SegmentationError(
            "max_clip_duration_ms must be a positive integer"
        )
    for idx, w in enumerate(words):
        if not w.text or not w.text.strip():
            raise SegmentationError(f"word {idx} has empty text")
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


def _validate_phrases(phrases: Sequence[Sequence[str]]) -> None:
    if not 1 <= len(phrases) <= MAX_PHRASES:
        raise SegmentationError(
            f"phrases must contain between 1 and {MAX_PHRASES} target phrases "
            f"(got {len(phrases)})"
        )
    for p, phrase in enumerate(phrases):
        if len(phrase) == 0:
            raise SegmentationError(f"phrase {p} must contain at least one word")
        for k, text in enumerate(phrase):
            if not isinstance(text, str) or not text:
                raise SegmentationError(
                    f"phrase {p} has an empty word text at position {k}"
                )


def _find_hits(
    words: Sequence[WordLike], phrases: Sequence[Sequence[str]]
) -> tuple[list[list[tuple[int, int]]], list[bool]]:
    """Find every exact consecutive-word hit of every phrase.

    Returns ``hits_ending[j]`` — a list of ``(hit_start, phrase_bit)`` pairs
    for hits whose last word is ``j`` (a fixed phrase length means at most
    one hit per phrase can end at any ``j``) — and a per-phrase "matched at
    least once" flag.
    """
    n = len(words)
    hits_ending: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    matched = [False] * len(phrases)
    for p, phrase in enumerate(phrases):
        length = len(phrase)
        for h in range(0, n - length + 1):
            if all(words[h + k].text == phrase[k] for k in range(length)):
                hits_ending[h + length - 1].append((h, 1 << p))
                matched[p] = True
    return hits_ending, matched


@dataclass(frozen=True)
class _State:
    """Best suffix plan for a ``dp[i][needed-mask]`` state."""

    score: int
    count: int
    # Time-ordered (word_start, word_end) pairs of the whole suffix, kept
    # explicitly for the final lexicographic tie-break.
    seq: tuple[tuple[int, int], ...]
    take_end: int  # inclusive end of the clip starting at i; -1 = skip i


def _better(cand: _State, cur: _State | None) -> bool:
    """Total score, then fewer clips, then the smaller index sequence."""
    if cur is None:
        return True
    if cand.score != cur.score:
        return cand.score < cur.score
    if cand.count != cur.count:
        return cand.count < cur.count
    return cand.seq < cur.seq


def plan_highlights(
    words: Sequence[WordLike],
    phrases: Sequence[Sequence[str]],
    *,
    max_clip_duration_ms: int,
) -> HighlightPlan:
    """Pick the minimum-score set of non-overlapping clips covering all phrases.

    Raises :class:`SegmentationError` (HTTP 422) on illegal input or when no
    clip selection covers every target phrase.
    """
    _validate_words(words, max_clip_duration_ms)
    _validate_phrases(phrases)
    n = len(words)
    m = len(phrases)
    size = 1 << m
    full_mask = size - 1

    hits_ending, matched = _find_hits(words, phrases)

    # ---- feasible clip candidates starting at each word -------------------
    # cover[i] holds (j, mask, duration_ms) for every legal clip [i, j]: the
    # duration grows monotonically with j (words are sorted, non-overlapping)
    # so enumeration stops at the first clip past the cap.  The coverage mask
    # gains a phrase bit exactly when the span starts fully containing a hit;
    # clips that cover no phrase are omitted (they can never improve a plan:
    # dropping one keeps full coverage while removing a strictly positive
    # duration + surcharge).
    cover = [[0] * n for _ in range(n)]
    candidates: list[list[tuple[int, int, int]]] = [[] for _ in range(n)]
    for i in range(n):
        mask = 0
        for j in range(i, n):
            duration_ms = words[j].end_ms - words[i].start_ms
            if duration_ms > max_clip_duration_ms:
                break
            for hit_start, bit in hits_ending[j]:
                if hit_start >= i:
                    mask |= bit
            cover[i][j] = mask
            if mask:
                candidates[i].append((j, mask, duration_ms))

    # ---- dynamic program ---------------------------------------------------
    # dp[i][need] = best plan using clips wholly inside words i..n-1 that
    # covers every phrase bit in ``need`` (covering extra bits is allowed).
    # Transitions: skip word i, or take a clip [i, j] and recurse on
    # words j+1..n-1 with need & ~mask.
    dp: list[list[_State | None]] = [
        [None] * size for _ in range(n + 1)
    ]
    dp[n][0] = _State(0, 0, (), -1)

    for i in range(n - 1, -1, -1):
        for need in range(size):
            best: _State | None = None
            skip = dp[i + 1][need]  # first clip starts somewhere after i
            if skip is not None:
                best = _State(skip.score, skip.count, skip.seq, -1)
            for j, mask, duration_ms in candidates[i]:
                suffix = dp[j + 1][need & ~mask]
                if suffix is None:
                    continue
                cand = _State(
                    score=duration_ms + PER_CLIP_FEE_MS + suffix.score,
                    count=1 + suffix.count,
                    seq=((i, j),) + suffix.seq,
                    take_end=j,
                )
                if _better(cand, best):
                    best = cand
            dp[i][need] = best

    if dp[0][full_mask] is None:
        unmatched = [p for p, ok in enumerate(matched) if not ok]
        if unmatched:
            shown = ", ".join(str(p) for p in unmatched)
            raise SegmentationError(
                f"no feasible highlight plan: target phrase(s) {shown} have "
                "no exact consecutive-word match in the transcript (matching "
                "is exact and case-sensitive on word text)"
            )
        raise SegmentationError(
            "no feasible highlight plan: no set of pairwise time-"
            f"non-overlapping clips, each spanning consecutive words with "
            "end_ms - start_ms <= "
            f"{max_clip_duration_ms} ms, covers all {m} target phrase(s); a "
            "clip containing a hit needs at least the hit span's own "
            "duration, and hits of different phrases must either share one "
            "clip or be packed into clips that do not overlap"
        )

    # ---- reconstruct the chosen clips --------------------------------------
    clips: list[HighlightClip] = []
    i = 0
    need = full_mask
    while need:
        state = dp[i][need]
        assert state is not None
        j = state.take_end
        if j < 0:  # skip this word, first clip starts later
            i += 1
            continue
        mask = cover[i][j]
        clips.append(
            HighlightClip(
                index=len(clips),
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                covered_phrases=tuple(
                    p for p in range(m) if mask & (1 << p)
                ),
            )
        )
        i = j + 1
        need &= ~mask

    return HighlightPlan(clips=tuple(clips), total_score=dp[0][full_mask].score)
