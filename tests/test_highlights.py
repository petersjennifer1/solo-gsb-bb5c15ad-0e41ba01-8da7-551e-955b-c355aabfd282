"""Unit + API tests for the local highlight-reel planner (POST /highlights).

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.highlights import PER_CLIP_FEE_MS, plan_highlights
from app.main import app
from app.segmenter import SegmentationError


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


def word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


class TestHighlightCore(unittest.TestCase):
    def test_single_phrase_takes_minimum_clip(self):
        # Slack words before/after the hit add duration but no coverage, so
        # the optimal clip is exactly the hit span.
        words = [
            W("x", 0, 100),
            W("a", 100, 300),
            W("b", 300, 600),
            W("y", 600, 900),
        ]
        plan = plan_highlights(words, [["a", "b"]], max_clip_duration_ms=5000)
        self.assertEqual(len(plan.clips), 1)
        clip = plan.clips[0]
        self.assertEqual((clip.word_start, clip.word_end), (1, 2))
        self.assertEqual((clip.start_ms, clip.end_ms), (100, 600))
        self.assertEqual(clip.duration_ms, 500)
        self.assertEqual(clip.covered_phrases, (0,))
        self.assertEqual(plan.total_score, 500 + PER_CLIP_FEE_MS)

    def test_overlapping_hits_of_one_phrase(self):
        # ["go", "go"] hits at positions 0 and 1 inside "go go go"; one clip
        # covering either occurrence suffices, and the earlier (lex smaller)
        # minimum span wins.
        words = [W("go", 0, 100), W("go", 100, 200), W("go", 200, 300)]
        plan = plan_highlights(words, [["go", "go"]], max_clip_duration_ms=5000)
        self.assertEqual(len(plan.clips), 1)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 1)
        )
        self.assertEqual(plan.total_score, 200 + PER_CLIP_FEE_MS)

    def test_repeated_phrase_needs_only_one_clip(self):
        words = [W("z", 0, 100), W("q", 100, 4000), W("z", 4000, 4100)]
        plan = plan_highlights(words, [["z"]], max_clip_duration_ms=500)
        self.assertEqual(len(plan.clips), 1)
        # earlier occurrence wins on the lexicographic tie
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 0)
        )

    def test_two_phrases_share_one_clip_when_cheaper(self):
        words = [
            W("a", 0, 100),
            W("b", 100, 200),
            W("x", 200, 300),
            W("y", 300, 400),
        ]
        plan = plan_highlights(
            words, [["a", "b"], ["x", "y"]], max_clip_duration_ms=5000
        )
        self.assertEqual(len(plan.clips), 1)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 3)
        )
        self.assertEqual(plan.clips[0].covered_phrases, (0, 1))
        self.assertEqual(plan.total_score, 400 + PER_CLIP_FEE_MS)

    def test_cap_forces_two_non_overlapping_clips(self):
        # words: a m m e g x y ; hit "e" at 300..400, hit "x y" at 500..700;
        # the 300 ms cap cannot fit one clip spanning both.
        words = [
            W("a", 0, 100),
            W("m", 100, 200),
            W("m", 200, 300),
            W("e", 300, 400),
            W("g", 400, 500),
            W("x", 500, 600),
            W("y", 600, 700),
        ]
        plan = plan_highlights(
            words, [["e"], ["x", "y"]], max_clip_duration_ms=300
        )
        self.assertEqual(len(plan.clips), 2)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (3, 3)
        )
        self.assertEqual(
            (plan.clips[1].word_start, plan.clips[1].word_end), (5, 6)
        )
        # clips merely touch time boundaries: clip0 ends at 400 <= clip1 start
        self.assertLessEqual(
            plan.clips[0].end_ms, plan.clips[1].start_ms
        )
        self.assertEqual(
            plan.total_score,
            100 + 200 + 2 * PER_CLIP_FEE_MS,
        )

    def test_phrase_hit_twice_picks_cheaper_occurrence(self):
        # "p" occurs at 0..100 and at 400..700; the short first occurrence
        # is cheaper on duration.
        words = [W("p", 0, 100), W("q", 100, 400), W("p", 400, 700)]
        plan = plan_highlights(words, [["p"]], max_clip_duration_ms=5000)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 0)
        )
        self.assertEqual(plan.total_score, 100 + PER_CLIP_FEE_MS)

    def test_lexicographic_tie_on_equal_score(self):
        # Two equal-length single-word hits of the only phrase: same duration,
        # one clip either way; the earlier word index wins.
        words = [W("z", 0, 200), W("q", 200, 500), W("z", 500, 700)]
        plan = plan_highlights(words, [["z"]], max_clip_duration_ms=5000)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 0)
        )

    def test_clip_may_extend_beyond_hit_to_bundle_phrases(self):
        # One hit ends early, a second starts later; extending the clip (still
        # under the cap) merges both phrases and saves a 1000-point fee.
        words = [
            W("a", 0, 100),
            W("gap", 100, 800),
            W("b", 800, 900),
        ]
        plan = plan_highlights(
            words, [["a"], ["b"]], max_clip_duration_ms=1000
        )
        self.assertEqual(len(plan.clips), 1)
        self.assertEqual(
            (plan.clips[0].word_start, plan.clips[0].word_end), (0, 2)
        )
        self.assertEqual(plan.clips[0].covered_phrases, (0, 1))

    def test_matching_is_exact_and_case_sensitive(self):
        words = [W("Go", 0, 100), W("home", 100, 200)]
        with self.assertRaises(SegmentationError):
            plan_highlights(words, [["go", "home"]], max_clip_duration_ms=5000)

    def test_422_when_phrase_never_matches(self):
        words = [W("a", 0, 100), W("b", 100, 200)]
        with self.assertRaises(SegmentationError) as ctx:
            plan_highlights(words, [["a"], ["zzz"]], max_clip_duration_ms=5000)
        self.assertIn("no exact consecutive-word match", str(ctx.exception))

    def test_422_when_hit_span_exceeds_cap(self):
        words = [W("a", 0, 100), W("b", 100, 300)]
        with self.assertRaises(SegmentationError):
            plan_highlights(words, [["a", "b"]], max_clip_duration_ms=100)

    def test_422_when_overlapping_hits_cannot_share_a_clip(self):
        # "a b c" spans 0..500, "b c d" spans 100..700; every clip covering
        # one hit overlaps the other's containing clip, and neither the 600 ms
        # cap nor a longer single span can cover both.
        words = [
            W("a", 0, 100),
            W("b", 100, 300),
            W("c", 300, 500),
            W("d", 500, 700),
        ]
        with self.assertRaises(SegmentationError):
            plan_highlights(
                words,
                [["a", "b", "c"], ["b", "c", "d"]],
                max_clip_duration_ms=600,
            )

    def test_422_on_empty_or_oversized_input(self):
        with self.assertRaises(SegmentationError):
            plan_highlights([], [["a"]], max_clip_duration_ms=5000)
        with self.assertRaises(SegmentationError):
            plan_highlights(
                [W("a", i, i + 1) for i in range(101)],
                [["a"]],
                max_clip_duration_ms=5000,
            )
        with self.assertRaises(SegmentationError):
            plan_highlights([W("a", 0, 100)], [], max_clip_duration_ms=5000)
        with self.assertRaises(SegmentationError):
            plan_highlights(
                [W("a", 0, 100)], [[]], max_clip_duration_ms=5000
            )
        with self.assertRaises(SegmentationError):
            plan_highlights(
                [W("a", 0, 100)], [["a"]], max_clip_duration_ms=0
            )

    def test_422_on_unsorted_or_invalid_words(self):
        with self.assertRaises(SegmentationError):
            plan_highlights(
                [W("a", 0, 500), W("b", 400, 900)],
                [["a"]],
                max_clip_duration_ms=5000,
            )
        with self.assertRaises(SegmentationError):
            plan_highlights(
                [W("a", 500, 400)],
                [["a"]],
                max_clip_duration_ms=5000,
            )

    def test_same_phrase_listed_twice_is_covered_together(self):
        # Duplicate targets are distinct phrase indices but share every hit.
        words = [W("a", 0, 100), W("b", 100, 200)]
        plan = plan_highlights(
            words, [["a", "b"], ["a", "b"]], max_clip_duration_ms=5000
        )
        self.assertEqual(len(plan.clips), 1)
        self.assertEqual(plan.clips[0].covered_phrases, (0, 1))

    def test_clips_time_ordered_and_timecodes_from_word_edges(self):
        words = [
            W("a", 0, 100),
            W("m", 100, 2000),
            W("b", 2000, 2100),
            W("n", 2100, 5000),
            W("c", 5000, 5200),
        ]
        plan = plan_highlights(
            words, [["a"], ["c"]], max_clip_duration_ms=500
        )
        self.assertEqual(len(plan.clips), 2)
        starts = [c.start_ms for c in plan.clips]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(plan.clips[0].start_ms, words[0].start_ms)
        self.assertEqual(plan.clips[-1].end_ms, words[-1].end_ms)


class TestHighlightAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    BODY = {
        "words": [
            word("Hello", 0, 300),
            word("wonderful", 300, 900),
            word("world", 900, 1200),
            word("Goodbye", 1400, 1800),
            word("now", 1800, 2000),
        ],
        "phrases": [["wonderful", "world"], ["Goodbye"]],
        "max_clip_duration_ms": 2000,
    }

    def test_highlights_200(self):
        r = self.client.post("/highlights", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["clip_count"], 1)
        clip = data["clips"][0]
        self.assertEqual(clip["word_start"], 1)
        self.assertEqual(clip["word_end"], 3)
        self.assertEqual(clip["start_ms"], 300)
        self.assertEqual(clip["end_ms"], 1800)
        self.assertEqual(clip["duration_ms"], 1500)
        self.assertEqual(clip["covered_phrases"], [0, 1])
        self.assertEqual(data["total_score"], 2500)

    def test_highlights_two_clips(self):
        body = {**self.BODY, "max_clip_duration_ms": 900}
        r = self.client.post("/highlights", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["clip_count"], 2)
        clips = data["clips"]
        self.assertLessEqual(clips[0]["end_ms"], clips[1]["start_ms"])
        self.assertEqual(clips[0]["covered_phrases"], [0])
        self.assertEqual(clips[1]["covered_phrases"], [1])
        # 900 + 1000 + 400 + 1000
        self.assertEqual(data["total_score"], 3300)

    def test_highlights_422_when_no_match(self):
        body = {**self.BODY, "phrases": [["nope"]]}
        r = self.client.post("/highlights", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("no exact consecutive-word match", r.json()["detail"])

    def test_highlights_422_when_cap_too_tight(self):
        body = {**self.BODY, "max_clip_duration_ms": 10}
        r = self.client.post("/highlights", json=body)
        self.assertEqual(r.status_code, 422)

    def test_highlights_schema_limits(self):
        too_many_words = {
            "words": [word("a", i, i + 1) for i in range(101)],
            "phrases": [["a"]],
            "max_clip_duration_ms": 1000,
        }
        self.assertEqual(
            self.client.post("/highlights", json=too_many_words).status_code,
            422,
        )
        too_many_phrases = {
            "words": [word("a", 0, 100)],
            "phrases": [["a"]] * 9,
            "max_clip_duration_ms": 1000,
        }
        self.assertEqual(
            self.client.post("/highlights", json=too_many_phrases).status_code,
            422,
        )
        empty_phrase = {
            "words": [word("a", 0, 100)],
            "phrases": [[]],
            "max_clip_duration_ms": 1000,
        }
        self.assertEqual(
            self.client.post("/highlights", json=empty_phrase).status_code,
            422,
        )
        zero_cap = {**self.BODY, "max_clip_duration_ms": 0}
        self.assertEqual(
            self.client.post("/highlights", json=zero_cap).status_code, 422
        )

    def test_highlights_invalid_word_times_422(self):
        body = {
            **self.BODY,
            "words": [word("a", 500, 100)],  # end < start
        }
        self.assertEqual(
            self.client.post("/highlights", json=body).status_code, 422
        )

    def test_root_lists_highlights(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["endpoints"]["highlights"], "POST /highlights")


if __name__ == "__main__":
    unittest.main()
