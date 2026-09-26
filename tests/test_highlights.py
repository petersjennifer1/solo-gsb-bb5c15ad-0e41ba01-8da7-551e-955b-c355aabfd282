"""Unit + API tests for highlight-reel clip planning.

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.highlights import plan_highlights
from app.main import app
from app.segmenter import SegmentationError


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


WORDS = [
    W("Hello", 0, 500),
    W("world.", 500, 1400),
    W("How", 2000, 2400),
    W("are", 2400, 2700),
    W("you", 2700, 3200),
    W("doing", 3200, 3800),
    W("today?", 3800, 4600),
    W("Fine", 5200, 5600),
    W("thanks.", 5600, 6400),
]

PHRASES = [["How", "are", "you"], ["Fine"], ["world."]]


class TestHighlightPlanning(unittest.TestCase):
    def test_single_phrase_single_clip(self):
        result = plan_highlights(WORDS, [["How", "are", "you"]], max_clip_ms=3000)
        self.assertEqual(len(result.clips), 1)
        clip = result.clips[0]
        self.assertEqual((clip.word_start, clip.word_end), (2, 4))
        self.assertEqual((clip.start_ms, clip.end_ms), (2000, 3200))
        self.assertEqual(clip.duration_ms, 1200)
        self.assertEqual(clip.phrases, [0])
        self.assertEqual(result.total_score, 1200 + 1000)

    def test_merges_phrases_into_one_clip_when_cheaper(self):
        # One clip [1..4] (2700 ms) covers phrases 0 and 2; merging beats
        # two separate clips (900 + 1200 ms + an extra 1000).
        result = plan_highlights(WORDS, PHRASES, max_clip_ms=3000)
        self.assertEqual(result.total_score, 2700 + 400 + 2000)
        self.assertEqual(
            [(c.word_start, c.word_end) for c in result.clips], [(1, 4), (7, 7)]
        )
        self.assertEqual(result.clips[0].phrases, [0, 2])
        self.assertEqual(result.clips[1].phrases, [1])

    def test_separate_clips_when_merging_exceeds_limit(self):
        # max_clip_ms=2000 forbids the merged [1..4] clip (2700 ms).
        result = plan_highlights(WORDS, PHRASES, max_clip_ms=2000)
        self.assertEqual(
            [(c.word_start, c.word_end) for c in result.clips],
            [(1, 1), (2, 4), (7, 7)],
        )
        self.assertEqual(result.total_score, 900 + 1200 + 400 + 3000)

    def test_overlapping_hits_all_count(self):
        # "a a" hits at words 0..1 and 1..2; both clips cost the same, so
        # the lexicographically smaller (0, 1) wins.
        words = [W("a", 0, 400), W("a", 400, 800), W("a", 800, 1200)]
        result = plan_highlights(words, [["a", "a"]], max_clip_ms=1000)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual((result.clips[0].word_start, result.clips[0].word_end), (0, 1))
        self.assertEqual(result.total_score, 800 + 1000)

    def test_repeated_hits_pick_earliest_on_tie(self):
        words = [
            W("x", 0, 400),
            W("a", 400, 900),
            W("b", 900, 1400),
            W("x", 1400, 1800),
        ]
        result = plan_highlights(words, [["x"]], max_clip_ms=1000)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual((result.clips[0].word_start, result.clips[0].word_end), (0, 0))
        self.assertEqual(result.total_score, 400 + 1000)

    def test_tie_prefers_fewer_clips(self):
        # One 2000 ms clip scores 2000 + 1000 = 3000, exactly the same as
        # two 500 ms clips (500 + 500 + 2000); the single clip wins.
        words = [
            W("a", 0, 500),
            W("b", 500, 1000),
            W("c", 1000, 1500),
            W("d", 1500, 2000),
        ]
        result = plan_highlights(words, [["a"], ["d"]], max_clip_ms=2000)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual((result.clips[0].word_start, result.clips[0].word_end), (0, 3))
        self.assertEqual(result.clips[0].phrases, [0, 1])
        self.assertEqual(result.total_score, 3000)

    def test_clip_lists_every_phrase_it_covers(self):
        words = [W("a", 0, 400), W("b", 400, 800)]
        result = plan_highlights(words, [["a"], ["a", "b"]], max_clip_ms=1000)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual(result.clips[0].phrases, [0, 1])
        self.assertEqual(result.total_score, 800 + 1000)

    def test_duplicate_phrases_are_covered_together(self):
        words = [W("a", 0, 400), W("b", 400, 800)]
        result = plan_highlights(words, [["a"], ["a"]], max_clip_ms=1000)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual(result.clips[0].phrases, [0, 1])

    def test_touching_clips_are_allowed(self):
        # Clip 1 ends where clip 2 starts (word 1 ends at 500 = word 2 start).
        words = [W("a", 0, 500), W("b", 500, 1000)]
        result = plan_highlights(words, [["a"], ["b"]], max_clip_ms=500)
        self.assertEqual(
            [(c.word_start, c.word_end) for c in result.clips], [(0, 0), (1, 1)]
        )
        self.assertEqual(result.total_score, 500 + 500 + 2000)

    def test_exact_matching_is_case_sensitive(self):
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [["hello"]], max_clip_ms=3000)

    def test_422_when_phrase_never_occurs(self):
        with self.assertRaises(SegmentationError) as ctx:
            plan_highlights(WORDS, [["How", "are"], ["nope"]], max_clip_ms=3000)
        self.assertIn("never occurs", str(ctx.exception))

    def test_422_when_hit_is_longer_than_the_clip_limit(self):
        # The only hit of the phrase spans 1800 ms > max_clip_ms.
        with self.assertRaises(SegmentationError) as ctx:
            plan_highlights(
                WORDS, [["How", "are", "you", "doing"]], max_clip_ms=1000
            )
        self.assertIn("no set of non-overlapping clips", str(ctx.exception))

    def test_422_when_covering_clips_would_have_to_overlap(self):
        # "a b" hits [0..1], "b a" hits [1..2]; one clip covering both
        # (1500 ms) exceeds the limit and the two single-hit clips overlap.
        words = [W("a", 0, 500), W("b", 500, 1000), W("a", 1000, 1500)]
        with self.assertRaises(SegmentationError):
            plan_highlights(words, [["a", "b"], ["b", "a"]], max_clip_ms=1000)

    def test_unsorted_or_overlapping_words_rejected(self):
        bad = [W("a", 0, 500), W("b", 400, 900)]
        with self.assertRaises(SegmentationError) as ctx:
            plan_highlights(bad, [["a"]], max_clip_ms=1000)
        self.assertIn("non-overlapping", str(ctx.exception))

    def test_invalid_request_shapes_rejected(self):
        with self.assertRaises(SegmentationError):
            plan_highlights([], [["a"]], max_clip_ms=1000)
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [], max_clip_ms=1000)
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [["a"]] * 9, max_clip_ms=1000)
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [["Hello"]], max_clip_ms=0)
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [[]], max_clip_ms=1000)
        with self.assertRaises(SegmentationError):
            plan_highlights(WORDS, [[""]], max_clip_ms=1000)
        too_many = [W("a", k * 100, k * 100 + 50) for k in range(101)]
        with self.assertRaises(SegmentationError):
            plan_highlights(too_many, [["a"]], max_clip_ms=1000)


class TestHighlightsAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @staticmethod
    def _body(**overrides):
        body = {
            "words": [
                {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                for w in WORDS
            ],
            "phrases": PHRASES,
            "max_clip_ms": 3000,
        }
        body.update(overrides)
        return body

    def test_highlights_ok(self):
        r = self.client.post("/highlights", json=self._body())
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["clip_count"], 2)
        self.assertEqual(data["total_score"], 5100)
        clips = data["clips"]
        self.assertEqual(len(clips), 2)
        # clips are in time order with word indices and covered phrases
        self.assertEqual(clips[0]["start_ms"], 500)
        self.assertEqual(clips[0]["end_ms"], 3200)
        self.assertEqual(clips[0]["duration_ms"], 2700)
        self.assertEqual((clips[0]["word_start"], clips[0]["word_end"]), (1, 4))
        self.assertEqual(clips[0]["phrases"], [0, 2])
        self.assertEqual((clips[1]["word_start"], clips[1]["word_end"]), (7, 7))
        self.assertEqual(clips[1]["phrases"], [1])
        for a, b in zip(clips, clips[1:]):
            self.assertLessEqual(a["end_ms"], b["start_ms"])
        # total score = total clip milliseconds + 1000 per clip
        self.assertEqual(
            data["total_score"],
            sum(c["duration_ms"] for c in clips) + 1000 * len(clips),
        )

    def test_422_when_no_plan_covers_every_phrase(self):
        r = self.client.post(
            "/highlights", json=self._body(phrases=[["How", "are", "you", "doing"]],
                                           max_clip_ms=1000)
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("no set of non-overlapping clips", r.json()["detail"])

    def test_422_when_phrase_never_occurs(self):
        r = self.client.post("/highlights", json=self._body(phrases=[["nope"]]))
        self.assertEqual(r.status_code, 422)
        self.assertIn("never occurs", r.json()["detail"])

    def test_422_on_overlapping_words(self):
        body = self._body(
            words=[
                {"text": "a", "start_ms": 0, "end_ms": 500},
                {"text": "b", "start_ms": 400, "end_ms": 900},
            ],
            phrases=[["a"]],
        )
        r = self.client.post("/highlights", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("non-overlapping", r.json()["detail"])

    def test_422_on_schema_violations(self):
        cases = [
            self._body(phrases=[]),                      # needs 1..8 phrases
            self._body(phrases=[["a"]] * 9),             # too many phrases
            self._body(phrases=[[]]),                    # empty phrase
            self._body(phrases=[[""]]),                  # empty word text
            self._body(max_clip_ms=0),                   # must be positive
            self._body(max_clip_ms=-5),
            self._body(words=[]),                        # needs >= 1 word
            self._body(
                words=[
                    {"text": "a", "start_ms": k * 100, "end_ms": k * 100 + 50}
                    for k in range(101)
                ]
            ),                                           # at most 100 words
        ]
        for body in cases:
            with self.subTest(body=body):
                r = self.client.post("/highlights", json=body)
                self.assertEqual(r.status_code, 422)

    def test_root_lists_highlights(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["endpoints"]["highlights"], "POST /highlights")


if __name__ == "__main__":
    unittest.main()
