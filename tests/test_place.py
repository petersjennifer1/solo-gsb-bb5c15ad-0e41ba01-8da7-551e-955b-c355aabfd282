"""Unit + API tests for joint segmentation with top/bottom placement.

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.place import segment_and_place
from app.segmenter import SegmentationError
from app.vtt import format_timestamp


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


class TestPlacementCore(unittest.TestCase):
    def test_no_occlusion_places_everything_bottom(self):
        # Bottom wins every placement tie.
        result = segment_and_place(WORDS, [], **OPTS)
        self.assertEqual(len(result.segments), 3)
        self.assertTrue(all(s.position == "bottom" for s in result.segments))
        self.assertAlmostEqual(result.total_cost, 5.96)

    def test_top_blocked_everywhere_still_all_bottom(self):
        result = segment_and_place(WORDS, [(0, 10000, "top")], **OPTS)
        self.assertTrue(all(s.position == "bottom" for s in result.segments))
        self.assertAlmostEqual(result.total_cost, 5.96)

    def test_bottom_blocked_everywhere_places_all_top(self):
        result = segment_and_place(WORDS, [(0, 10000, "bottom")], **OPTS)
        self.assertTrue(all(s.position == "top" for s in result.segments))
        self.assertAlmostEqual(result.total_cost, 5.96)

    def test_forced_area_switch_costs_one(self):
        # First subtitle time span is bottom-blocked; the later spans are
        # top-blocked, so the only feasible order is top, bottom, bottom —
        # exactly one switch on top of the plain 5.96 cost.
        result = segment_and_place(
            WORDS,
            [(0, 1500, "bottom"), (1900, 10000, "top")],
            **OPTS,
        )
        self.assertEqual(
            [s.position for s in result.segments], ["top", "bottom", "bottom"]
        )
        self.assertEqual(
            [(s.word_start, s.word_end) for s in result.segments],
            [(0, 1), (2, 6), (7, 8)],
        )
        self.assertAlmostEqual(result.total_cost, 6.96)

    def test_touching_endpoint_does_not_block(self):
        # [1400, 2000) bottom only fills the gap between subtitles 1 and 2;
        # intersections with every subtitle have zero length.
        result = segment_and_place(WORDS, [(1400, 2000, "bottom")], **OPTS)
        self.assertTrue(all(s.position == "bottom" for s in result.segments))

    def test_zero_length_intersection_does_not_block(self):
        # Occlusion starts exactly when the first subtitle ends.
        result = segment_and_place(WORDS, [(1400, 1900, "bottom")], **OPTS)
        self.assertEqual(
            [s.position for s in result.segments],
            ["bottom", "bottom", "bottom"],
        )

    def test_positive_overlap_even_one_ms_blocks(self):
        result = segment_and_place(WORDS, [(1399, 1400, "bottom")], **OPTS)
        self.assertTrue(all(s.position == "top" for s in result.segments))

    def test_both_areas_blocked_raises(self):
        with self.assertRaisesRegex(SegmentationError, "no legal segmentation"):
            segment_and_place(
                WORDS,
                [(0, 1500, "top"), (0, 1500, "bottom")],
                **OPTS,
            )

    def test_overlapping_same_area_intervals_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "overlap"):
            segment_and_place(
                WORDS,
                [(0, 1500, "bottom"), (1000, 2000, "bottom")],
                **OPTS,
            )

    def test_unsorted_same_area_intervals_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "sorted by start time"):
            segment_and_place(
                WORDS,
                [(3000, 4000, "top"), (1000, 2000, "top")],
                **OPTS,
            )

    def test_touching_same_area_intervals_allowed(self):
        result = segment_and_place(
            WORDS, [(0, 1400, "bottom"), (1400, 2000, "bottom")], **OPTS
        )
        self.assertTrue(all(s.position == "top" for s in result.segments))

    def test_cross_area_overlaps_are_independent(self):
        # Overlapping intervals in different areas are allowed and each only
        # blocks its own area.  With the bottom area blocked for the first
        # subtitle's span and top blocked there as well, no area would be
        # left for it — instead block top for seg1 and bottom for seg2/3:
        result = segment_and_place(
            WORDS,
            [(0, 1399, "top"), (2001, 6400, "bottom")],
            **OPTS,
        )
        self.assertEqual(
            [s.position for s in result.segments], ["bottom", "top", "top"]
        )

    def test_zero_length_occlusion_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "start_ms < end_ms"):
            segment_and_place(WORDS, [(1000, 1000, "top")], **OPTS)

    def test_unknown_area_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "top.*bottom"):
            segment_and_place(WORDS, [(0, 1000, "middle")], **OPTS)

    def test_segmentation_constraints_still_apply(self):
        # A single-word 500 ms subtitle cannot meet the 1000 ms minimum even
        # when a placement rule would otherwise be satisfiable.
        words = [W("a", 0, 500)]
        with self.assertRaisesRegex(SegmentationError, "no legal segmentation"):
            segment_and_place(words, [(0, 600, "bottom")], **OPTS)

    def test_manual_breaks_combine_with_placement(self):
        result = segment_and_place(
            WORDS,
            [(0, 600, "bottom"), (1400, 10000, "top")],
            **{**OPTS, "min_duration_ms": 0, "required_break_after": [0, 6]},
        )
        ends = [s.word_end for s in result.segments]
        self.assertIn(0, ends)
        self.assertIn(6, ends)
        self.assertEqual(result.segments[0].position, "top")
        for s in result.segments[2:]:
            self.assertEqual(s.position, "bottom")

    def test_keep_together_combines_with_placement(self):
        result = segment_and_place(
            WORDS,
            [(0, 10000, "top")],
            **{**OPTS, "keep_together": [[2, 4]]},
        )
        self.assertTrue(all(s.position == "bottom" for s in result.segments))
        span = next(
            s for s in result.segments if s.word_start <= 2 <= s.word_end
        )
        self.assertGreaterEqual(span.word_end, 4)

    def test_max_line_chars_combines_with_placement(self):
        words = [
            W("aaa", 0, 450),
            W("bbb", 450, 900),
            W("ccc", 900, 1350),
            W("ddd", 1350, 1800),
        ]
        result = segment_and_place(
            words,
            [(0, 1800, "bottom")],
            max_chars=42,
            min_duration_ms=0,
            max_duration_ms=7000,
            target_duration_ms=2000,
            pause_threshold_ms=500,
            max_line_chars=7,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, "aaa bbb\nccc ddd")
        self.assertEqual(result.segments[0].position, "top")

    def test_tie_break_bottom_preference_prefix(self):
        # Force the first subtitle to top and the last one to bottom; the
        # middle subtitle is free.  Both (top, top, bottom) and
        # (top, bottom, bottom) then switch exactly once, so the area
        # sequence tie-break must choose bottom for the middle subtitle.
        result = segment_and_place(
            WORDS,
            [(0, 1400, "bottom"), (5199, 6400, "top")],
            **OPTS,
        )
        positions = [s.position for s in result.segments]
        self.assertEqual(positions, ["top", "bottom", "bottom"])
        self.assertAlmostEqual(result.total_cost, 6.96)


def _word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


PLACE_BODY = {
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
    "occlusions": [
        {"start_ms": 0, "end_ms": 1500, "region": "bottom"},
        {"start_ms": 1900, "end_ms": 10000, "region": "top"},
    ],
    "max_chars": 42,
    "min_duration_ms": 1000,
    "max_duration_ms": 7000,
    "target_duration_ms": 3000,
    "pause_threshold_ms": 500,
}


class TestPlaceAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_place_json(self):
        r = self.client.post("/segment/place", json=PLACE_BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["segment_count"], 3)
        self.assertAlmostEqual(data["total_cost"], 6.96)
        positions = [s["position"] for s in data["segments"]]
        self.assertEqual(positions, ["top", "bottom", "bottom"])
        for s in data["segments"]:
            self.assertIn(s["position"], ("top", "bottom"))
            self.assertGreaterEqual(s["duration_ms"], 1000)

    def test_place_without_occlusions_defaults_bottom(self):
        body = {k: v for k, v in PLACE_BODY.items() if k != "occlusions"}
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)  # occlusions required

    def test_empty_occlusions_rejected(self):
        r = self.client.post("/segment/place", json={**PLACE_BODY, "occlusions": []})
        self.assertEqual(r.status_code, 422)

    def test_place_vtt_uses_line_settings(self):
        r = self.client.post("/segment/place/vtt", json=PLACE_BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/vtt"))
        vtt = r.text
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        blocks = vtt.strip().split("\n\n")[1:]
        self.assertEqual(len(blocks), 3)
        self.assertIn("line:0", blocks[0])
        self.assertIn("line:-1", blocks[1])
        self.assertIn("line:-1", blocks[2])
        # dot-separated WebVTT timestamps
        self.assertIn("00:00:00.000 --> 00:00:01.400", blocks[0])

    def test_vtt_and_json_share_the_same_result(self):
        rj = self.client.post("/segment/place", json=PLACE_BODY)
        rv = self.client.post("/segment/place/vtt", json=PLACE_BODY)
        self.assertEqual(rj.status_code, 200)
        self.assertEqual(rv.status_code, 200)
        segs = rj.json()["segments"]
        blocks = rv.text.strip().split("\n\n")[1:]
        self.assertEqual(len(blocks), len(segs))
        for k, (block, seg) in enumerate(zip(blocks, segs), start=1):
            lines = block.split("\n")
            self.assertEqual(lines[0], str(k))
            self.assertIn(
                f"{format_timestamp(seg['start_ms'])} --> "
                f"{format_timestamp(seg['end_ms'])}",
                lines[1],
            )
            self.assertIn(seg["text"], block)
            self.assertIn("line:0" if seg["position"] == "top" else "line:-1", lines[1])

    def test_422_when_no_area_available(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 0, "end_ms": 1500, "region": "bottom"},
                {"start_ms": 0, "end_ms": 1500, "region": "top"},
            ],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("no legal segmentation", r.json()["detail"])

    def test_422_on_overlapping_same_area_occlusions(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 0, "end_ms": 1500, "region": "bottom"},
                {"start_ms": 1000, "end_ms": 2000, "region": "bottom"},
            ],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("overlap", r.json()["detail"])

    def test_422_on_unsorted_same_area_occlusions(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 3000, "end_ms": 4000, "region": "top"},
                {"start_ms": 1000, "end_ms": 2000, "region": "top"},
            ],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("sorted", r.json()["detail"])

    def test_422_on_zero_length_occlusion(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": 1000, "end_ms": 1000, "region": "top"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_unknown_region(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": 0, "end_ms": 100, "region": "middle"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_negative_occlusion_ms(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": -1, "end_ms": 100, "region": "top"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_vtt_422_matches_json_422(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 0, "end_ms": 7000, "region": "top"},
                {"start_ms": 0, "end_ms": 7000, "region": "bottom"},
            ],
        }
        r = self.client.post("/segment/place/vtt", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("no legal segmentation", r.json()["detail"])

    def test_root_lists_place_endpoints(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        endpoints = r.json()["endpoints"]
        self.assertEqual(endpoints["segment_place_json"], "POST /segment/place")
        self.assertEqual(endpoints["segment_place_vtt"], "POST /segment/place/vtt")


class TestVttTimestamps(unittest.TestCase):
    def test_format_timestamp_uses_dot(self):
        self.assertEqual(format_timestamp(0), "00:00:00.000")
        self.assertEqual(format_timestamp(1400), "00:00:01.400")
        self.assertEqual(format_timestamp(3_661_500), "01:01:01.500")


if __name__ == "__main__":
    unittest.main()
