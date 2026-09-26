"""Core subtitle segmentation logic.

Pure Python (no web dependencies) so it can be unit-tested in isolation.

Given a time-ordered, non-overlapping list of words, find a segmentation
into subtitles that:

* covers every word exactly once and keeps the original order;
* gives every subtitle at most ``max_chars`` characters and a duration
  within ``[min_duration_ms, max_duration_ms]`` (hard constraints);
* when ``max_line_chars`` is given, every subtitle must fit on at most two
  lines of that width; lines may only break *between* words and each line
  (including joining spaces) must be no longer than ``max_line_chars``;
* minimises the total cost::

      sum over subtitles of (duration_s - target_s) ** 2
      + 4 * (number of crossed natural boundaries)
      + line-layout cost (only when max_line_chars is given)

  where a *natural boundary* sits between two consecutive words when the
  first ends with sentence-ending punctuation or the gap between them is
  at least ``pause_threshold_ms``.  A boundary is "crossed" when it falls
  inside a subtitle rather than on its edge.
* optional human-edited boundaries, both hard constraints:

  - ``required_break_after``: word indices ``k`` (``0 <= k <= n-2``); a
    subtitle edge *must* sit between word ``k`` and word ``k + 1``;
  - ``keep_together``: closed word-index intervals ``[lo, hi]`` with at
    least two words; no subtitle edge may fall strictly inside any of
    them (the words may still be split between subtitles at the
    interval's outside edges).

  Intervals must be pairwise disjoint (they may touch at an endpoint,
  e.g. ``[0, 1]`` and ``[2, 3]``), and no required break may fall inside
  a keep-together interval.  Illegal or mutually conflicting inputs raise
  a :class:`SegmentationError` (HTTP 422); the optimiser then searches
  globally for the minimum-cost segmentation that satisfies both kinds of
  edited boundaries alongside the other hard constraints, and if none
  exists the caller gets an error.

  The *line-layout cost* of a subtitle is 0 when its text fits on one line;
  otherwise every legal two-line break is considered and the minimum of
  ``(len(line1) - len(line2)) ** 2 / max_line_chars ** 2`` is taken (the
  earlier breakpoint wins ties).  A subtitle that admits no legal layout
  (e.g. its words cannot be split into two width-respecting lines) is not
  a valid candidate; if no segmentation remains, the caller gets an error.
* ties (equal total cost) are broken in favour of fewer subtitles.

A classic O(n^2) dynamic program over word positions solves this exactly.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Protocol, Sequence

from .config import CROSSED_BOUNDARY_PENALTY

SENTENCE_ENDING_CHARS = frozenset(".!?。！？…")
# Closing quotes / brackets that may trail the actual punctuation mark.
TRAILING_CLOSERS = frozenset("\"'”’»』」）)]】》〉")


class WordLike(Protocol):
    text: str
    start_ms: int
    end_ms: int


class SegmentationError(Exception):
    """Raised when the input cannot be segmented legally (mapped to HTTP 422)."""


def _validate_manual_edits(
    n: int,
    required_break_after: Sequence[int] | None,
    keep_together: Sequence[Sequence[int]] | None,
) -> tuple[frozenset[int], tuple[tuple[int, int], ...]]:
    """Validate human-edited boundaries for a transcript of ``n`` words.

    Returns the required-break indices as a sorted, deduplicated set and
    the keep-together ranges as sorted, pairwise-disjoint closed
    intervals ``[lo, hi]``.  Raises :class:`SegmentationError` on any
    illegal or mutually conflicting input.

    * a required break index ``k`` must satisfy ``0 <= k <= n - 2`` (there
      is no boundary after the last word);
    * a keep-together range must contain at least two words, so
      ``0 <= lo < hi <= n - 1``;
    * keep-together ranges may touch at an endpoint but may not overlap;
    * a required break may fall on a range's right outside edge (``hi``)
      but not at its left outside edge (``lo``) nor strictly inside: a
      break at ``lo`` would end word ``lo``'s subtitle while words
      ``lo`` and ``lo + 1`` must stay together, which is contradictory.
    """
    required: set[int] = set()
    for k in required_break_after or ():
        if not 0 <= k <= n - 2:
            raise SegmentationError(
                f"required_break_after index {k} is out of range for {n} "
                f"word(s); each must be between 0 and {n - 2}"
            )
        required.add(k)

    ranges: list[tuple[int, int]] = []
    for pair in keep_together or ():
        lo, hi = pair
        if not 0 <= lo <= n - 1 or not 0 <= hi <= n - 1:
            raise SegmentationError(
                f"keep_together interval [{lo}, {hi}] is out of range for "
                f"{n} word(s); indices must be between 0 and {n - 1}"
            )
        if lo >= hi:
            raise SegmentationError(
                f"keep_together interval [{lo}, {hi}] must contain at least "
                "two distinct words (lo < hi)"
            )
        ranges.append((lo, hi))

    ranges.sort()
    for (lo1, hi1), (lo2, hi2) in zip(ranges, ranges[1:]):
        if lo2 <= hi1:
            raise SegmentationError(
                f"keep_together intervals [{lo1}, {hi1}] and [{lo2}, {hi2}] "
                "overlap; intervals must be pairwise disjoint"
            )

    for lo, hi in ranges:
        # A required break at ``lo`` (the interval's left outside edge) is
        # also impossible: word ``lo`` would end the previous subtitle while
        # staying in the same subtitle as word ``lo + 1``.  Breaks at the
        # right outside edge ``hi`` are fine, as are breaks before ``lo``.
        inside = [k for k in required if lo <= k < hi]
        if inside:
            k = inside[0]
            where = "at the left edge of" if k == lo else "inside"
            raise SegmentationError(
                f"required_break_after index {k} falls {where} the "
                f"keep_together interval [{lo}, {hi}]; a mandatory break "
                "cannot be forbidden and required at the same boundary"
            )

    return frozenset(required), tuple(ranges)


@dataclass
class SegmentResult:
    index: int
    text: str
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int  # index of first word, inclusive
    word_end: int  # index of last word, inclusive
    crossed_boundaries: int


@dataclass
class SegmentationResult:
    segments: list[SegmentResult]
    total_cost: float


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK unified ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK extension A
        or 0x3040 <= cp <= 0x30FF  # hiragana / katakana
        or 0xAC00 <= cp <= 0xD7AF  # hangul syllables
        or 0x3000 <= cp <= 0x303F  # CJK symbols and punctuation
        or 0xFF00 <= cp <= 0xFFEF  # fullwidth forms
    )


def _needs_space(prev: str, nxt: str) -> bool:
    """Heuristic: no space between tokens when either side is CJK."""
    if not prev or not nxt:
        return False
    return not (_is_cjk(prev[-1]) or _is_cjk(nxt[0]))


def join_word_texts(texts: Sequence[str]) -> str:
    parts: list[str] = []
    for i, text in enumerate(texts):
        if i and _needs_space(texts[i - 1], text):
            parts.append(" ")
        parts.append(text)
    return "".join(parts)


def ends_with_sentence_punctuation(text: str) -> bool:
    stripped = text.strip().rstrip("".join(TRAILING_CLOSERS))
    return bool(stripped) and stripped[-1] in SENTENCE_ENDING_CHARS


def _best_layout(
    i: int,
    j: int,
    cum_len: Sequence[int],
    space_before: Sequence[int],
    width: int,
) -> tuple[int, float] | None:
    """Best two-line (or one-line) layout for the words spanning ``i..j``.

    ``cum_len`` is a prefix-length array built as
    ``cum_len[k + 1] = cum_len[k] + len(word_k) + space_before[k]`` (the
    joining space sits *before* its word), and ``space_before[k]`` is 1 when
    a joining space precedes word ``k``.  Words ``a..b`` joined normally have
    length ``cum_len[b + 1] - cum_len[a] - space_before[a]`` (the space
    before the first word is not part of the span).

    Returns ``(first_word_of_line_2, cost)`` (``-1`` marks a single line),
    or ``None`` when no legal layout exists.  A break before word ``m``
    (``i < m <= j``) puts words ``i..m-1`` on line 1 and ``m..j`` on line 2:

    * line 1 length = ``cum_len[m] - cum_len[i] - space_before[i]``
    * line 2 length = ``cum_len[j + 1] - cum_len[m] - space_before[m]``

    the joining space at the break being replaced by the newline.  The cost
    is 0 for a single line; otherwise it is the minimum over legal breaks of
    ``(line1_len - line2_len) ** 2 / width ** 2``, with the earlier break
    winning ties.
    """
    total = cum_len[j + 1] - cum_len[i] - space_before[i]
    if total <= width:
        return -1, 0.0

    # ---- legal break range via binary search ------------------------------
    # Line 1 fits: cum_len[m] <= cum_len[i] + space_before[i] + width
    # Line 2 fits: cum_len[m] + space_before[m] >= cum_len[j+1] - width
    target_lo = cum_len[j + 1] - width
    p = bisect_left(cum_len, target_lo, i + 1, j + 1)
    lo = p
    # When word m starts a Latin line (space_before[m] == 1), line 2 gets
    # one character back, so the position one step before the bisect point
    # may also satisfy the lower bound.
    if p - 1 >= i + 1 and cum_len[p - 1] + space_before[p - 1] >= target_lo:
        lo = p - 1
    hi = bisect_right(
        cum_len, cum_len[i] + space_before[i] + width, i + 1, j + 1
    ) - 1
    if lo > hi:
        return None

    # D(m) = line1 - line2 = 2*cum_len[m] + space_before[m]
    #        - cum_len[i] - space_before[i] - cum_len[j+1]
    # is strictly increasing in m, so the minimum absolute difference is at
    # the one or two legal breaks straddling D = 0.
    balance = (cum_len[i] + space_before[i] + cum_len[j + 1]) / 2.0
    q = bisect_left(cum_len, balance, lo, hi + 1)

    def _diff(m: int) -> int:
        line1 = cum_len[m] - cum_len[i] - space_before[i]
        line2 = cum_len[j + 1] - cum_len[m] - space_before[m]
        return line1 - line2

    best_m = None
    best_diff: int | None = None
    for m in (q - 1, q):  # earlier break first -> it wins an exact tie
        if lo <= m <= hi:
            d = abs(_diff(m))
            if best_diff is None or d < best_diff:
                best_diff = d
                best_m = m
    assert best_m is not None and best_diff is not None
    return best_m, (best_diff**2) / width**2


@dataclass(frozen=True)
class PreparedSegmentation:
    """Validated transcript plus the precomputed tables shared by the DP runs.

    Built once by :func:`prepare_segmentation`; both the plain optimiser and
    the placement-aware optimiser (``place`` module) enumerate candidates
    over the same prepared tables so their character, duration and two-line
    rules never drift apart.
    """

    words: Sequence[WordLike]
    max_chars: int
    min_duration_ms: int
    max_duration_ms: int
    target_s: float
    max_line_chars: int | None
    nat_prefix: Sequence[int]
    cum_len: Sequence[int]
    space_before: Sequence[int]
    required_prefix: Sequence[int]
    blocked_edge: Sequence[bool]
    has_manual_edits: bool


@dataclass(frozen=True)
class SegmentCandidate:
    """A legal subtitle spanning the words ``i..j``.

    ``base_cost`` is the candidate's own segmentation cost (duration
    deviation, crossed natural boundaries, two-line layout) excluding any
    placement-area terms; ``break_word`` is the first word of the second
    line (``-1`` when the text fits on one line).
    """

    i: int
    j: int
    start_ms: int
    end_ms: int
    duration_ms: int
    crossed: int
    break_word: int
    base_cost: float


def prepare_segmentation(
    words: Sequence[WordLike],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
    max_line_chars: int | None = None,
    required_break_after: Sequence[int] | None = None,
    keep_together: Sequence[Sequence[int]] | None = None,
) -> PreparedSegmentation:
    """Validate the request and build the shared prefix tables.

    Mirrors every input check of :func:`segment_words`; raises
    :class:`SegmentationError` (HTTP 422) on any illegal word, ordering or
    human-edited boundary.
    """
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")

    # ---- human-edited boundary validation ---------------------------------
    required_breaks, keep_ranges = _validate_manual_edits(
        n, required_break_after, keep_together
    )
    # required_prefix[t] = number of required breaks on boundaries < t,
    # i.e. among indices 0..t-1.  The breaks strictly inside a segment
    # i..j are those with i <= k < j = required_prefix[j] - required_prefix[i].
    required_prefix = [0] * (n + 1)
    for t in range(1, n + 1):
        required_prefix[t] = required_prefix[t - 1] + (
            1 if t - 1 in required_breaks else 0
        )
    # blocked_edge[k] marks the boundary between words k-1 and k as a
    # forbidden segment edge because it lies strictly inside a keep-together
    # interval (its outside edges stay splittable).
    blocked_edge = [False] * (n + 1)
    for lo, hi in keep_ranges:
        for k in range(lo + 1, hi + 1):
            blocked_edge[k] = True

    # ---- input validation -------------------------------------------------
    for idx, w in enumerate(words):
        if not w.text or not w.text.strip():
            raise SegmentationError(f"word {idx} has empty text")
        if w.end_ms <= w.start_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) must satisfy end_ms > start_ms "
                f"(got {w.start_ms}..{w.end_ms})"
            )
        if len(w.text) > max_chars:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) is {len(w.text)} characters and can "
                f"never fit within max_chars={max_chars}"
            )
        if max_line_chars is not None and len(w.text) > max_line_chars:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) is {len(w.text)} characters and can "
                f"never fit within max_line_chars={max_line_chars}"
            )
        word_dur = w.end_ms - w.start_ms
        if word_dur > max_duration_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) lasts {word_dur} ms and can never "
                f"fit within max_duration_ms={max_duration_ms}"
            )
    for i in range(1, n):
        if words[i].start_ms < words[i - 1].end_ms:
            raise SegmentationError(
                f"words must be sorted by time and non-overlapping: word {i} "
                f"starts at {words[i].start_ms} ms but word {i - 1} ends at "
                f"{words[i - 1].end_ms} ms"
            )

    # ---- natural break points between consecutive words -------------------
    natural = [False] * max(n - 1, 0)
    for k in range(n - 1):
        gap = words[k + 1].start_ms - words[k].end_ms
        natural[k] = (
            gap >= pause_threshold_ms
            or ends_with_sentence_punctuation(words[k].text)
        )
    # nat_prefix[k] = number of natural boundaries among indices 0..k-1, so
    # the count crossed by a segment spanning words i..j is prefix[j]-prefix[i].
    nat_prefix = [0] * n
    for k in range(n - 1):
        nat_prefix[k + 1] = nat_prefix[k] + (1 if natural[k] else 0)

    space_before = [0] * n
    for k in range(1, n):
        space_before[k] = 1 if _needs_space(words[k - 1].text, words[k].text) else 0

    # cum_len[k+1] = length of the joined words 0..k with each word's
    # joining space stored in front of it.  Strictly increasing; the joined
    # length of an interior span a..b is cum_len[b+1] - cum_len[a] -
    # space_before[a] (the space before the span's first word is excluded).
    cum_len = [0] * (n + 1)
    for k in range(n):
        cum_len[k + 1] = cum_len[k] + len(words[k].text) + space_before[k]

    return PreparedSegmentation(
        words=words,
        max_chars=max_chars,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        target_s=target_duration_ms / 1000.0,
        max_line_chars=max_line_chars,
        nat_prefix=nat_prefix,
        cum_len=cum_len,
        space_before=space_before,
        required_prefix=required_prefix,
        blocked_edge=blocked_edge,
        has_manual_edits=bool(required_breaks or keep_ranges),
    )


def iter_candidates(
    prep: PreparedSegmentation,
    i: int,
):
    """Yield every legal subtitle candidate starting at word ``i``.

    Enumerates end positions ``j >= i`` and applies the hard constraints
    shared by all optimisers: character limit, duration limit, mandatory
    breaks, keep-together edges and two-line layout.  Spans shorter than
    ``min_duration_ms`` are skipped.  Reachability of the suffix (and any
    placement-area constraint) is the caller's concern and intentionally
    not checked here.  The ``break`` / ``continue`` pruning mirrors the
    original single DP: character and duration overflow, a swallowed
    mandatory break and an unlayoutable span are monotone in ``j`` (break),
    while a blocked end edge or a too-short span is a local skip
    (continue).
    """
    words = prep.words
    n = len(words)
    chars = 0
    for j in range(i, n):
        chars += len(words[j].text) + (
            prep.space_before[j] if j > i else 0
        )
        if chars > prep.max_chars:
            break  # only grows as j grows
        dur_ms = words[j].end_ms - words[i].start_ms
        if dur_ms > prep.max_duration_ms:
            break  # only grows as j grows
        # A mandatory break inside the segment is illegal, and once the
        # span has swallowed one, every wider span starting at i does too.
        if prep.required_prefix[j] - prep.required_prefix[i] > 0:
            break
        # The segment's end edge sits between words j and j+1; a
        # keep-together interval forbids edges strictly inside itself.
        if prep.blocked_edge[j + 1]:
            continue
        if prep.max_line_chars is not None:
            layout = _best_layout(
                i,
                j,
                prep.cum_len,
                prep.space_before,
                prep.max_line_chars,
            )
            if layout is None:
                break  # wider spans starting at i stay unlayoutable
            break_word, line_cost = layout
        else:
            break_word, line_cost = -1, 0.0
        if dur_ms < prep.min_duration_ms:
            continue
        crossed = prep.nat_prefix[j] - prep.nat_prefix[i]
        dur_s = dur_ms / 1000.0
        base_cost = (
            (dur_s - prep.target_s) ** 2
            + CROSSED_BOUNDARY_PENALTY * crossed
            + line_cost
        )
        yield SegmentCandidate(
            i=i,
            j=j,
            start_ms=words[i].start_ms,
            end_ms=words[j].end_ms,
            duration_ms=dur_ms,
            crossed=crossed,
            break_word=break_word,
            base_cost=base_cost,
        )


def build_segment_text(
    words: Sequence[WordLike],
    i: int,
    j: int,
    break_word: int,
) -> str:
    """Join words ``i..j`` into one or two lines (``break_word`` = -1 → 1)."""
    texts = [w.text for w in words[i : j + 1]]
    if break_word > 0:
        return (
            join_word_texts(texts[: break_word - i])
            + "\n"
            + join_word_texts(texts[break_word - i :])
        )
    return join_word_texts(texts)


def segment_words(
    words: Sequence[WordLike],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
    max_line_chars: int | None = None,
    required_break_after: Sequence[int] | None = None,
    keep_together: Sequence[Sequence[int]] | None = None,
) -> SegmentationResult:
    prep = prepare_segmentation(
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

    # ---- dynamic programming ----------------------------------------------
    INF = float("inf")
    EPS = 1e-9
    dp_cost = [INF] * (n + 1)
    dp_count = [0] * (n + 1)
    dp_cost[n] = 0.0
    nxt = [-1] * (n + 1)  # nxt[i] = inclusive end index of the segment starting at i
    # nxt_break[i] = first word of the chosen segment's second line (-1 = one line)
    nxt_break = [-1] * (n + 1)

    for i in range(n - 1, -1, -1):
        for cand in iter_candidates(prep, i):
            if dp_cost[cand.j + 1] == INF:
                continue
            cost = cand.base_cost + dp_cost[cand.j + 1]
            count = 1 + dp_count[cand.j + 1]
            if cost < dp_cost[i] - EPS or (
                abs(cost - dp_cost[i]) <= EPS and count < dp_count[i]
            ):
                dp_cost[i] = cost
                dp_count[i] = count
                nxt[i] = cand.j
                nxt_break[i] = cand.break_word

    if dp_cost[0] == INF:
        layout_note = (
            f", max_line_chars={max_line_chars}" if max_line_chars is not None else ""
        )
        manual_note = ""
        if prep.has_manual_edits:
            manual_note = (
                " and the required_break_after / keep_together boundaries"
            )
        raise SegmentationError(
            "no legal segmentation covers all words under the given constraints "
            f"(max_chars={max_chars}{layout_note}, min_duration_ms="
            f"{min_duration_ms}, max_duration_ms={max_duration_ms}); every "
            "subtitle — including the last one — must satisfy the duration "
            "limits (and, when set, fit on at most two lines of "
            f"max_line_chars){manual_note}, so try relaxing min_duration_ms or "
            "the other limits"
        )

    # ---- reconstruct the optimal path --------------------------------------
    segments: list[SegmentResult] = []
    i = 0
    while i < n:
        j = nxt[i]
        segments.append(
            SegmentResult(
                index=len(segments),
                text=build_segment_text(words, i, j, nxt_break[i]),
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                crossed_boundaries=prep.nat_prefix[j] - prep.nat_prefix[i],
            )
        )
        i = j + 1

    return SegmentationResult(segments=segments, total_cost=dp_cost[0])
