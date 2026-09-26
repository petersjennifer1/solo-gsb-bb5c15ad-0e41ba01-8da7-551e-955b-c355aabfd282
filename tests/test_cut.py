"""Unit + API tests for re-segmentation after video deletions.

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.cut import segment_with_deletions
from app.main import app
from app.segmenter import SegmentationError, segment_words
from app.srt import format_timestamp


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


OPTS = dict(
    max_chars=42,
    min_duration_ms=1000,
    max_duration_ms=7000,
    target_duration_ms=3000,
    pause_threshold_ms=500,
)

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


class TestCutAndShift(unittest.TestCase):
    def test_basic_cut_shifts_and_splits(self):
        # [1500, 5000] swallows words 2..6; the survivors form two
        # fragments that are re-timed onto the edited timeline.
        result = segment_with_deletions(WORDS, [[1500, 5000]], **OPTS)
        self.assertEqual(len(result.segments), 2)
        first, second = result.segments
        self.assertEqual(first.text, "Hello world.")
        self.assertEqual((first.start_ms, first.end_ms), (0, 1400))
        self.assertEqual((first.word_start, first.word_end), (0, 1))
        # 3500 ms deleted before "Fine" / "thanks." -> shifted earlier.
        self.assertEqual(second.text, "Fine thanks.")
        self.assertEqual((second.start_ms, second.end_ms), (1700, 2900))
        self.assertEqual((second.word_start, second.word_end), (7, 8))
        self.assertAlmostEqual(result.total_cost, 5.8)

    def test_word_indices_are_request_indices(self):
        result = segment_with_deletions(WORDS, [[1500, 5000]], **OPTS)
        covered = []
        for seg in result.segments:
            covered.extend(range(seg.word_start, seg.word_end + 1))
        # exactly the surviving request indices, in order
        self.assertEqual(covered, [0, 1, 7, 8])

    def test_touching_intervals_are_allowed(self):
        result = segment_with_deletions(
            WORDS, [[2000, 3200], [3200, 4600]], **OPTS
        )
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[-1].start_ms, 5200 - 2600)

    def test_deletion_inside_gap_still_forces_a_split(self):
        # No word is dropped, but the cut must separate the two sides even
        # though the 100 ms gap is below the pause threshold.
        words = [W("a", 0, 400), W("b", 500, 900), W("c", 1000, 1400)]
        opts = {**OPTS, "min_duration_ms": 0}
        uncut = segment_words(words, **opts)
        self.assertEqual(len(uncut.segments), 1)
        result = segment_with_deletions(words, [[450, 480]], **opts)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(
            [(s.word_start, s.word_end) for s in result.segments],
            [(0, 0), (1, 2)],
        )
        # the second fragment is shifted by the 30 ms cut
        self.assertEqual(result.segments[1].start_ms, 470)
        self.assertEqual(result.segments[1].end_ms, 1370)

    def test_leading_deletion_shifts_without_splitting(self):
        words = [W("b", 1000, 1500), W("c", 1500, 2500)]
        result = segment_with_deletions(
            words, [[0, 1000]], **{**OPTS, "min_duration_ms": 0}
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual((result.segments[0].start_ms, result.segments[0].end_ms), (0, 1500))

    def test_trailing_deletion_is_a_no_op(self):
        result = segment_with_deletions(WORDS, [[10000, 12000]], **OPTS)
        plain = segment_words(WORDS, **OPTS)
        self.assertEqual(
            [(s.text, s.start_ms, s.end_ms) for s in result.segments],
            [(s.text, s.start_ms, s.end_ms) for s in plain.segments],
        )

    def test_empty_deletions_behave_like_plain_segmentation(self):
        result = segment_with_deletions(WORDS, [], **OPTS)
        plain = segment_words(WORDS, **OPTS)
        self.assertEqual(
            [(s.text, s.word_start, s.word_end) for s in result.segments],
            [(s.text, s.word_start, s.word_end) for s in plain.segments],
        )

    def test_word_touching_interval_edges_is_kept(self):
        # world. ends exactly at the cut start, How starts exactly at its
        # end: neither intersects the interval, both survive.
        result = segment_with_deletions(WORDS, [[1400, 2000]], **OPTS)
        texts = [s.text for s in result.segments]
        self.assertIn("Hello world.", texts)
        self.assertTrue(any("How" in t for t in texts))

    def test_max_line_chars_applies_per_fragment(self):
        words = [
            W("aaa", 0, 450),
            W("bbb", 450, 900),
            W("ccc", 2000, 2450),
            W("ddd", 2450, 2900),
        ]
        result = segment_with_deletions(
            words,
            [[900, 2000]],
            **{**OPTS, "min_duration_ms": 0, "max_line_chars": 4},
        )
        self.assertEqual([s.text for s in result.segments], ["aaa\nbbb", "ccc\nddd"])
        self.assertEqual(result.segments[1].start_ms, 900)


class TestCutValidation(unittest.TestCase):
    def test_unsorted_intervals_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "sorted"):
            segment_with_deletions(WORDS, [[3000, 4000], [1000, 2000]], **OPTS)

    def test_overlapping_intervals_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "overlap"):
            segment_with_deletions(WORDS, [[1000, 3000], [2000, 4000]], **OPTS)

    def test_same_start_intervals_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "overlap"):
            segment_with_deletions(WORDS, [[1000, 2000], [1000, 1500]], **OPTS)

    def test_empty_interval_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "start_ms < end_ms"):
            segment_with_deletions(WORDS, [[2000, 2000]], **OPTS)

    def test_inverted_interval_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "start_ms < end_ms"):
            segment_with_deletions(WORDS, [[3000, 1000]], **OPTS)

    def test_word_ending_inside_interval_rejected(self):
        # world. (500..1400) is clipped by the cut start at 1000.
        with self.assertRaisesRegex(SegmentationError, "fully covered"):
            segment_with_deletions(WORDS, [[1000, 5000]], **OPTS)

    def test_word_starting_inside_interval_rejected(self):
        # are (2400..2700) sticks out of the cut end at 2500.
        with self.assertRaisesRegex(SegmentationError, "fully covered"):
            segment_with_deletions(WORDS, [[2000, 2500]], **OPTS)

    def test_word_spanning_whole_interval_rejected(self):
        # are (2400..2700) covers the tiny cut [2500, 2600] entirely.
        with self.assertRaisesRegex(SegmentationError, "fully covered"):
            segment_with_deletions(WORDS, [[2500, 2600]], **OPTS)

    def test_word_covered_only_by_union_of_intervals_rejected(self):
        words = [W("a", 0, 1000), W("b", 1000, 3000), W("c", 3000, 4000)]
        with self.assertRaisesRegex(SegmentationError, "fully covered"):
            segment_with_deletions(words, [[500, 1500], [2000, 2500]], **OPTS)

    def test_all_words_deleted_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "cover every word"):
            segment_with_deletions(WORDS, [[0, 10000]], **OPTS)

    def test_fragment_without_legal_segmentation_rejected(self):
        # The cut isolates a 500 ms word that is shorter than the 1000 ms
        # minimum subtitle duration.
        words = [W("a", 0, 500), W("b", 1000, 2000)]
        with self.assertRaisesRegex(SegmentationError, "fragment 0"):
            segment_with_deletions(words, [[600, 900]], **OPTS)

    def test_unsorted_words_rejected(self):
        words = [W("a", 1000, 2000), W("b", 0, 500)]
        with self.assertRaisesRegex(SegmentationError, "non-overlapping"):
            segment_with_deletions(words, [[5000, 6000]], **OPTS)

    def test_no_words_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "at least one word"):
            segment_with_deletions([], [[0, 100]], **OPTS)


def _word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


CUT_BODY = {
    "words": [
        _word("Hello", 0, 500),
        _word("world.", 500, 1400),
        _word("How", 2000, 2400),
        _word("are", 2400, 2700),
        _word("you", 2700, 3200),
        _word("doing", 3200, 3800),
        _word("today?", 3800, 4600),
        _word("Fine", 5200, 5600),
        _word("thanks.", 5600, 6400),
    ],
    "deletions": [[1500, 5000]],
    "max_chars": 42,
    "min_duration_ms": 1000,
    "max_duration_ms": 7000,
    "target_duration_ms": 3000,
    "pause_threshold_ms": 500,
}


class TestCutAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_cut_json(self):
        r = self.client.post("/segment/cut", json=CUT_BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["segment_count"], 2)
        self.assertAlmostEqual(data["total_cost"], 5.8)
        first, second = data["segments"]
        self.assertEqual(first["text"], "Hello world.")
        self.assertEqual((first["start_ms"], first["end_ms"]), (0, 1400))
        self.assertEqual((first["word_start"], first["word_end"]), (0, 1))
        self.assertEqual(second["text"], "Fine thanks.")
        self.assertEqual((second["start_ms"], second["end_ms"]), (1700, 2900))
        self.assertEqual((second["word_start"], second["word_end"]), (7, 8))
        for s in data["segments"]:
            self.assertLessEqual(len(s["text"]), 42)
            self.assertGreaterEqual(s["duration_ms"], 1000)
            self.assertLessEqual(s["duration_ms"], 7000)

    def test_cut_srt_matches_json(self):
        rj = self.client.post("/segment/cut", json=CUT_BODY)
        rs = self.client.post("/segment/cut/srt", json=CUT_BODY)
        self.assertEqual(rj.status_code, 200, rj.text)
        self.assertEqual(rs.status_code, 200, rs.text)
        self.assertTrue(rs.headers["content-type"].startswith("text/plain"))
        segs = rj.json()["segments"]
        blocks = rs.text.strip().split("\n\n")
        self.assertEqual(len(blocks), len(segs))
        for k, (block, seg) in enumerate(zip(blocks, segs), start=1):
            lines = block.split("\n")
            self.assertEqual(lines[0], str(k))
            self.assertIn(seg["text"], block)
            self.assertIn(
                f"{format_timestamp(seg['start_ms'])} --> "
                f"{format_timestamp(seg['end_ms'])}",
                lines[1],
            )

    def test_422_on_unsorted_deletions(self):
        body = {**CUT_BODY, "deletions": [[3000, 4000], [1000, 2000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("sorted", r.json()["detail"])

    def test_422_on_overlapping_deletions(self):
        body = {**CUT_BODY, "deletions": [[1000, 3000], [2000, 4000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("overlap", r.json()["detail"])

    def test_422_on_zero_length_deletion(self):
        body = {**CUT_BODY, "deletions": [[2000, 2000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("start_ms < end_ms", r.json()["detail"])

    def test_422_on_partially_covered_word(self):
        body = {**CUT_BODY, "deletions": [[1000, 5000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("fully covered", r.json()["detail"])

    def test_422_when_all_words_deleted(self):
        body = {**CUT_BODY, "deletions": [[0, 10000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("cover every word", r.json()["detail"])

    def test_422_when_fragment_has_no_legal_segmentation(self):
        body = {
            **CUT_BODY,
            "words": [_word("a", 0, 500), _word("b", 1000, 2000)],
            "deletions": [[600, 900]],
        }
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("fragment 0", r.json()["detail"])

    def test_422_on_empty_deletions(self):
        body = {**CUT_BODY, "deletions": []}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_missing_deletions(self):
        body = {k: v for k, v in CUT_BODY.items() if k != "deletions"}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_negative_deletion_ms(self):
        body = {**CUT_BODY, "deletions": [[-100, 500]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_malformed_deletion(self):
        body = {**CUT_BODY, "deletions": [[1000, 2000, 3000]]}
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_overlapping_words(self):
        body = {
            **CUT_BODY,
            "words": [_word("a", 0, 500), _word("b", 400, 900)],
            "deletions": [[2000, 3000]],
        }
        r = self.client.post("/segment/cut", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("non-overlapping", r.json()["detail"])

    def test_cut_srt_422_matches_json_422(self):
        body = {**CUT_BODY, "deletions": [[0, 10000]]}
        r = self.client.post("/segment/cut/srt", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("cover every word", r.json()["detail"])

    def test_root_lists_cut_endpoints(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        endpoints = r.json()["endpoints"]
        self.assertEqual(endpoints["segment_cut_json"], "POST /segment/cut")
        self.assertEqual(endpoints["segment_cut_srt"], "POST /segment/cut/srt")


if __name__ == "__main__":
    unittest.main()
