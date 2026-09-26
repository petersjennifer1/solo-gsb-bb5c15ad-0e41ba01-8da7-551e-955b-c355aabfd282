"""Unit tests for the pure segmentation logic.

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from typing import Sequence

from app.segmenter import (
    SegmentationError,
    ends_with_sentence_punctuation,
    join_word_texts,
    segment_words,
)
from app.srt import format_timestamp, to_srt


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


def _segment(texts: Sequence[str], dur_ms: int = 450, **kwargs):
    words = [W(t, i * dur_ms, (i + 1) * dur_ms) for i, t in enumerate(texts)]
    opts = dict(
        max_chars=100,
        min_duration_ms=0,
        max_duration_ms=100000,
        target_duration_ms=2000,
        pause_threshold_ms=1000,
    )
    opts.update(kwargs)
    return segment_words(words, **opts)


def assert_coverage(testcase: unittest.TestCase, result, n: int) -> None:
    """Every word covered exactly once, in order; total cost sane."""
    testcase.assertEqual(result.segments[0].word_start, 0)
    testcase.assertEqual(result.segments[-1].word_end, n - 1)
    for a, b in zip(result.segments, result.segments[1:]):
        testcase.assertEqual(b.word_start, a.word_end + 1)
    testcase.assertTrue(math.isfinite(result.total_cost))
    testcase.assertGreaterEqual(result.total_cost, 0.0)


class TestPunctuationAndJoining(unittest.TestCase):
    def test_sentence_endings(self):
        self.assertTrue(ends_with_sentence_punctuation("Hello."))
        self.assertTrue(ends_with_sentence_punctuation("真的吗？"))
        self.assertTrue(ends_with_sentence_punctuation('"Wow!"'))
        self.assertFalse(ends_with_sentence_punctuation("hello"))
        self.assertFalse(ends_with_sentence_punctuation("word,"))

    def test_join_spaces(self):
        self.assertEqual(join_word_texts(["Hello", "world"]), "Hello world")
        self.assertEqual(join_word_texts(["你好", "世界"]), "你好世界")
        self.assertEqual(join_word_texts(["你好", "world"]), "你好world")
        self.assertEqual(join_word_texts(["hello", "世界"]), "hello世界")


class TestSegmentation(unittest.TestCase):
    def _check_coverage(self, result, n):
        assert_coverage(self, result, n)

    def test_covers_all_words_in_order_and_respects_max_chars(self):
        texts = ["a", "b", "c", "d", "e", "f"]
        result = _segment(texts, max_chars=3)
        for seg in result.segments:
            self.assertLessEqual(len(seg.text), 3)
        self._check_coverage(result, len(texts))
        self.assertEqual(
            "".join(seg.text.replace(" ", "") for seg in result.segments),
            "abcdef",
        )

    def test_respects_duration_limits(self):
        texts = [f"w{i}" for i in range(10)]
        result = _segment(
            texts,
            dur_ms=400,
            min_duration_ms=800,
            max_duration_ms=1600,
            target_duration_ms=1200,
        )
        for seg in result.segments:
            self.assertGreaterEqual(seg.duration_ms, 800)
            self.assertLessEqual(seg.duration_ms, 1600)
        self._check_coverage(result, len(texts))

    def test_sentence_punctuation_discourages_crossing(self):
        # Target duration would make the single 2 s segment optimal on
        # duration alone (cost 0); crossing the sentence boundary costs +4,
        # so splitting into two 1 s segments (cost 2) must win.
        words = [W("Hi.", 0, 1000), W("there", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 2)
        self.assertAlmostEqual(result.total_cost, 2.0, places=9)

    def test_long_pause_discourages_crossing(self):
        # Two 1.0 s words with a 300 ms gap -> single subtitle spans 2.3 s.
        # Target 2.3 s makes one segment perfect but it crosses the pause
        # (+4). Splitting costs 2 * (1.0 - 2.3)^2 = 3.38, so split wins.
        words = [W("Hi", 0, 1000), W("there", 1300, 2300)]  # 300 ms gap
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2300,
            pause_threshold_ms=200,
        )
        self.assertEqual(len(result.segments), 2)
        self.assertAlmostEqual(result.total_cost, 3.38, places=9)

    def test_penalty_charged_when_crossed(self):
        # Big max_chars / perfect one-segment duration but only one subtitle
        # possible: crossing cost must appear in total_cost.
        # min_duration_ms=1500 makes single-word (1 s) subtitles illegal, so
        # the only legal solution crosses the sentence boundary (+4).
        words = [W("Hi.", 0, 1000), W("there", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=1500,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertAlmostEqual(result.total_cost, 4.0, places=9)
        self.assertEqual(result.segments[0].crossed_boundaries, 1)

    def test_tie_break_prefers_fewer_subtitles(self):
        # One segment of 1.5 s vs two segments of 1.0 s + 0.5 s, target 1.0 s:
        # both cost exactly 0.25. Fewer subtitles (1) must win.
        words = [W("a", 0, 1000), W("b", 1000, 1500)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=1000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertAlmostEqual(result.total_cost, 0.25, places=9)

    def test_target_deviation_measured_in_seconds(self):
        words = [W("a", 0, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=3000,
            pause_threshold_ms=1000,
        )
        self.assertAlmostEqual(result.total_cost, 1.0, places=9)  # (2-3)^2

    def test_word_too_long_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("abcdef", 0, 100)],
                max_chars=3,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("max_chars", str(ctx.exception))

    def test_word_exceeding_max_duration_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 9000)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=5000,
                target_duration_ms=3000,
                pause_threshold_ms=1000,
            )
        self.assertIn("max_duration_ms", str(ctx.exception))

    def test_overlapping_words_raise(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 1000), W("b", 900, 1900)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("non-overlapping", str(ctx.exception))

    def test_no_legal_segmentation_due_to_min_duration(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 400)],
                max_chars=100,
                min_duration_ms=1000,
                max_duration_ms=5000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("no legal segmentation", str(ctx.exception))

    def test_empty_words_raise(self):
        with self.assertRaises(SegmentationError):
            segment_words(
                [],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )


class TestLineLayout(unittest.TestCase):
    def _layout(self, texts, width, **kwargs):
        words = [W(t, i * 450, (i + 1) * 450) for i, t in enumerate(texts)]
        opts = dict(
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
            max_line_chars=width,
        )
        opts.update(kwargs)
        return segment_words(words, **opts)

    def test_short_subtitle_stays_single_line_with_zero_layout_cost(self):
        result = self._layout(["aaa", "bbb"], 7)
        seg = result.segments[0]
        self.assertEqual(seg.text, "aaa bbb")  # exactly 7 chars, one line
        # duration 0.9 s vs target 2.0 s -> 1.21; layout cost 0
        self.assertAlmostEqual(result.total_cost, 1.21, places=9)

    def test_two_line_break_balances_lines(self):
        result = self._layout(["aaa", "bbb", "ccc", "ddd"], 7)
        seg = result.segments[0]
        self.assertEqual(seg.text, "aaa bbb\nccc ddd")
        line1, line2 = seg.text.split("\n")
        self.assertEqual(len(line1), 7)
        self.assertEqual(len(line2), 7)
        # perfectly balanced -> layout cost 0; duration 1.8 s vs 2.0 s
        self.assertAlmostEqual(result.total_cost, 0.04, places=9)

    def test_layout_cost_is_squared_diff_over_squared_width(self):
        # "aaa bbb ccc": total 11 > 7; legal breaks are before "bbb" (lines
        # 3 / 7) and before "ccc" (7 / 3) — both diff 4, cost 16/49, and the
        # earlier break wins.
        result = self._layout(["aaa", "bbb", "ccc"], 7)
        seg = result.segments[0]
        self.assertEqual(seg.text, "aaa\nbbb ccc")
        duration_cost = (1.35 - 2.0) ** 2
        self.assertAlmostEqual(
            result.total_cost, duration_cost + 16 / 49, places=9
        )

    def test_tie_on_line_cost_prefers_earlier_break(self):
        # CJK carries no joining spaces: "你好世界再见" is 6 chars, width 5.
        # Break after word 0 -> lines 2 / 4 (diff 2); break after word 1 ->
        # 4 / 2 (diff 2): equal cost, the earlier break wins.
        result = self._layout(["你好", "世界", "再见"], 5)
        seg = result.segments[0]
        self.assertEqual(seg.text, "你好\n世界再见")
        self.assertAlmostEqual(result.total_cost, (1.35 - 2.0) ** 2 + 4 / 25)

    def test_every_line_respects_width_and_breaks_only_between_words(self):
        result = self._layout(
            ["one", "two", "three", "four", "five", "six"], 9
        )
        for seg in result.segments:
            self.assertLessEqual(len(seg.text.split("\n")), 2)
            for line in seg.text.split("\n"):
                self.assertLessEqual(len(line), 9)
            # no word is ever split across lines
            joined = seg.text.replace("\n", " ")
            for word in ["one", "two", "three", "four", "five", "six"]:
                if word in joined:
                    self.assertIn(word, seg.text)

    def test_cjk_break_has_no_space_around_newline(self):
        result = self._layout(["你好", "世界"], 3)
        self.assertEqual(result.segments[0].text, "你好\n世界")

    def test_max_chars_still_enforced_with_layout(self):
        # Each pair joins to 9 chars; max_chars=8 forces pairs apart even
        # though two lines of width 9 could hold them.
        result = self._layout(
            ["aaaa", "bbbb", "cccc", "dddd"], 9, max_chars=8
        )
        for seg in result.segments:
            # displayed length counts the newline, not the space it replaces
            self.assertLessEqual(len(seg.text), 8)

    def test_duration_limits_still_enforced_with_layout(self):
        words = [W(t, i * 400, (i + 1) * 400) for i, t in enumerate(
            ["aaaa", "bbbb", "cccc", "dddd"]
        )]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=800,
            max_duration_ms=1600,
            target_duration_ms=1200,
            pause_threshold_ms=1000,
            max_line_chars=9,
        )
        for seg in result.segments:
            self.assertGreaterEqual(seg.duration_ms, 800)
            self.assertLessEqual(seg.duration_ms, 1600)
            for line in seg.text.split("\n"):
                self.assertLessEqual(len(line), 9)

    def test_word_longer_than_line_width_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("abcdef", 0, 100)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
                max_line_chars=3,
            )
        self.assertIn("max_line_chars", str(ctx.exception))

    def test_no_legal_layout_returns_error(self):
        # 400 ms words require >= 1000 ms (3 words) per subtitle, but width 4
        # allows at most one 4-char Latin word per line, i.e. two per
        # subtitle -> no legal segmentation.
        words = [W(t, i * 400, (i + 1) * 400) for i, t in enumerate(
            ["aaaa", "bbbb", "cccc"]
        )]
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                words,
                max_chars=100,
                min_duration_ms=1000,
                max_duration_ms=100000,
                target_duration_ms=2000,
                pause_threshold_ms=1000,
                max_line_chars=4,
            )
        self.assertIn("no legal segmentation", str(ctx.exception))
        self.assertIn("max_line_chars=4", str(ctx.exception))

    def test_omitting_max_line_chars_keeps_single_line_output(self):
        result = _segment(["aaa", "bbb", "ccc", "ddd"])
        for seg in result.segments:
            self.assertNotIn("\n", seg.text)


class TestManualBoundaries(unittest.TestCase):
    def test_required_break_forces_edge_even_when_costlier(self):
        # 10 unpunctuated words of 200 ms; the duration optimum groups them
        # in tens around the 2 s target. Forcing a break after word 0 makes
        # word 0 a (costly) standalone subtitle it would never otherwise be.
        texts = [f"w{i}" for i in range(10)]
        forced = _segment(texts, dur_ms=200, required_break_after=[0])
        ends = [s.word_end for s in forced.segments]
        self.assertIn(0, ends)
        self.assertEqual(forced.segments[0].word_end, 0)

        plain = _segment(texts, dur_ms=200)
        self.assertNotIn(0, [s.word_end for s in plain.segments])
        # the constrained optimum is no cheaper than the free one
        self.assertGreaterEqual(forced.total_cost, plain.total_cost - 1e-12)
        # every word still covered once, in order
        assert_coverage(self, forced, len(texts))

    def test_multiple_required_breaks(self):
        texts = [f"w{i}" for i in range(8)]
        result = _segment(
            texts, dur_ms=200, required_break_after=[1, 4], max_chars=100
        )
        ends = {s.word_end for s in result.segments}
        self.assertIn(1, ends)
        self.assertIn(4, ends)
        assert_coverage(self, result, len(texts))

    def test_duplicate_required_breaks_are_accepted(self):
        result = _segment(
            ["a", "b", "c"], dur_ms=500, required_break_after=[1, 1]
        )
        self.assertEqual(
            [s.word_end for s in result.segments], [1, 2]
        )

    def test_keep_together_forbids_internal_edges(self):
        # The free optimum breaks after words 2 and 5. Locking [2, 4] moves
        # both edges away from the locked span.
        texts = [f"w{i}" for i in range(6)]
        result = _segment(texts, dur_ms=500, keep_together=[[2, 4]])
        ends = [s.word_end for s in result.segments]
        # neither boundary 2 nor 3 (strictly inside [2, 4]) may be an edge
        self.assertNotIn(2, ends)
        self.assertNotIn(3, ends)
        # words 2, 3, 4 must all live in the same subtitle
        spans = [(s.word_start, s.word_end) for s in result.segments]
        self.assertTrue(any(a <= 2 and 4 <= b for a, b in spans))
        assert_coverage(self, result, len(texts))

    def test_keep_together_allows_its_right_outside_edge(self):
        # [1, 2] locks only boundary 1; the edge after word 2 (the interval's
        # right edge) stays available, and a required break there is legal.
        result = _segment(
            ["a", "b", "c", "d"],
            dur_ms=500,
            keep_together=[[1, 2]],
            required_break_after=[2],
        )
        ends = [s.word_end for s in result.segments]
        self.assertNotIn(1, ends)
        self.assertIn(2, ends)

    def test_combined_edits_are_respected_and_optimal(self):
        texts = [f"w{i}" for i in range(6)]
        result = _segment(
            texts,
            dur_ms=500,
            required_break_after=[0],
            keep_together=[[2, 3]],
        )
        ends = [s.word_end for s in result.segments]
        self.assertIn(0, ends)
        self.assertNotIn(2, ends)
        spans = [(s.word_start, s.word_end) for s in result.segments]
        self.assertTrue(any(a <= 2 and 3 <= b for a, b in spans))
        assert_coverage(self, result, len(texts))

    def test_touching_intervals_are_disjoint_and_allowed(self):
        result = _segment(
            ["a", "b", "c", "d"],
            dur_ms=500,
            keep_together=[[0, 1], [2, 3]],
        )
        assert_coverage(self, result, 4)

    def test_required_break_index_after_last_word_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(["a", "b", "c"], required_break_after=[2])
        self.assertIn("required_break_after", str(ctx.exception))
        self.assertIn("0 and 1", str(ctx.exception))

    def test_required_break_on_single_word_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(["a"], required_break_after=[0])
        self.assertIn("required_break_after", str(ctx.exception))

    def test_negative_required_break_index_raises(self):
        with self.assertRaises(SegmentationError):
            _segment(["a", "b"], required_break_after=[-1])

    def test_keep_interval_must_contain_two_words(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(["a", "b", "c"], keep_together=[[1, 1]])
        self.assertIn("at least two distinct words", str(ctx.exception))

    def test_keep_interval_out_of_range_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(["a", "b"], keep_together=[[0, 5]])
        self.assertIn("out of range", str(ctx.exception))

    def test_overlapping_keep_intervals_raise(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(
                ["a", "b", "c", "d", "e"],
                keep_together=[[0, 2], [2, 4]],
            )
        self.assertIn("overlap", str(ctx.exception))

    def test_required_break_inside_keep_interval_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            _segment(
                ["a", "b", "c", "d"],
                required_break_after=[2],
                keep_together=[[1, 3]],
            )
        self.assertIn("required_break_after", str(ctx.exception))

    def test_required_break_at_keep_left_edge_raises(self):
        # Break after word 1 AND keep words 1..2 together is contradictory.
        with self.assertRaises(SegmentationError) as ctx:
            _segment(
                ["a", "b", "c", "d"],
                required_break_after=[1],
                keep_together=[[1, 2]],
            )
        self.assertIn("required_break_after", str(ctx.exception))

    def test_no_legal_solution_due_to_required_break_raises(self):
        # The mandatory first subtitle (word 0, 500 ms) cannot reach the
        # 1000 ms minimum duration.
        with self.assertRaises(SegmentationError) as ctx:
            _segment(
                ["a", "b", "c"],
                dur_ms=500,
                required_break_after=[0],
                min_duration_ms=1000,
            )
        msg = str(ctx.exception)
        self.assertIn("no legal segmentation", msg)
        self.assertIn("required_break_after", msg)

    def test_no_legal_solution_due_to_keep_together_raises(self):
        # Keeping the two 5-char words together exceeds max_chars=9.
        words = [W("aaaaa", 0, 500), W("bbbbb", 500, 1000), W("c", 1000, 1500)]
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                words,
                max_chars=9,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
                keep_together=[[0, 1]],
            )
        self.assertIn("no legal segmentation", str(ctx.exception))

    def test_edits_work_under_two_line_width(self):
        words = [
            W(t, i * 400, (i + 1) * 400)
            for i, t in enumerate(
                ["aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff"]
            )
        ]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=1200,
            pause_threshold_ms=1000,
            max_line_chars=9,
            required_break_after=[1],
            keep_together=[[3, 4]],
        )
        ends = [s.word_end for s in result.segments]
        self.assertIn(1, ends)
        self.assertNotIn(3, ends)
        for seg in result.segments:
            for line in seg.text.split("\n"):
                self.assertLessEqual(len(line), 9)

    def test_empty_edit_lists_equal_default(self):
        texts = [f"w{i}" for i in range(6)]
        plain = _segment(texts)
        edited = _segment(texts, required_break_after=[], keep_together=[])
        self.assertAlmostEqual(plain.total_cost, edited.total_cost, places=12)
        self.assertEqual(
            [s.word_end for s in plain.segments],
            [s.word_end for s in edited.segments],
        )


class TestSRT(unittest.TestCase):
    def test_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00:00,000")
        self.assertEqual(format_timestamp(3_661_500), "01:01:01,500")

    def test_srt_document(self):
        result = _segment(["Hello", "world", "Bye"], dur_ms=500)
        srt = to_srt(result.segments)
        lines = srt.splitlines()
        self.assertEqual(lines[0], "1")
        self.assertIn(" --> ", lines[1])
        self.assertIn("Hello", srt)
        self.assertIn("Bye", srt)

    def test_srt_document_keeps_line_break(self):
        result = _segment(
            ["aaa", "bbb", "ccc", "ddd"],
            max_line_chars=7,
        )
        srt = to_srt(result.segments)
        # exactly one two-line block; blocks are still separated by a blank line
        self.assertEqual(
            srt,
            "1\n00:00:00,000 --> 00:00:01,800\naaa bbb\nccc ddd\n",
        )


if __name__ == "__main__":
    unittest.main()
