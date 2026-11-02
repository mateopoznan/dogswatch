"""Run inside the container: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import app as pies  # noqa: E402


class BarkSegmentsTest(unittest.TestCase):
    def setUp(self):
        self.threshold = pies.bark_score_threshold
        self.gap = pies.merge_gap
        self.padding = pies.bark_padding
        pies.bark_score_threshold = 0.3
        pies.merge_gap = 15.0
        pies.bark_padding = 3.0

    def tearDown(self):
        pies.bark_score_threshold = self.threshold
        pies.merge_gap = self.gap
        pies.bark_padding = self.padding

    @staticmethod
    def scores(total_seconds, hits):
        values = [0.0] * int(total_seconds / pies.hop_seconds)
        for second in hits:
            values[int(second / pies.hop_seconds)] = 0.6
        return values

    def test_no_detection_gives_no_recording(self):
        self.assertEqual(pies.bark_segments(self.scores(60, []), 60.0), [])

    def test_short_gap_stays_in_one_recording(self):
        segments = pies.bark_segments(self.scores(60, [10, 20]), 60.0)
        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0]["windows"]), 2)

    def test_long_gap_makes_separate_recordings(self):
        segments = pies.bark_segments(self.scores(120, [10, 60]), 120.0)
        self.assertEqual(len(segments), 2)
        self.assertLess(segments[0]["end"], segments[1]["start"])

    def test_padding_surrounds_detection_and_stays_inside_audio(self):
        segments = pies.bark_segments(self.scores(60, [1, 59]), 60.0)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[1]["end"], 60.0)
        middle = pies.bark_segments(self.scores(60, [30]), 60.0)[0]
        self.assertAlmostEqual(middle["bark_start"] - middle["start"], 3.0)
        self.assertAlmostEqual(middle["end"] - middle["bark_end"], 3.0)

    def test_bark_seconds_counts_only_detected_windows(self):
        segment = pies.bark_segments(self.scores(60, [10, 20]), 60.0)[0]
        self.assertAlmostEqual(segment["bark_seconds"], round(2 * pies.hop_seconds, 1))
        self.assertGreater(segment["end"] - segment["start"], segment["bark_seconds"])

    def test_hours_split_at_clock_hour(self):
        start = datetime(2026, 11, 1, 16, 59, 59, tzinfo=ZoneInfo("Europe/Warsaw"))
        hours = pies.bark_hours(start, [0, 10])
        self.assertEqual(set(hours), {"2026-11-01T16", "2026-11-01T17"})


class WeekHeatmapTest(unittest.TestCase):
    def test_counts_exclude_false_positives_and_split_hours(self):
        rows = [
            {"file": "a.mp3", "started": "2026-10-28T14:59:00+01:00", "ended": "2026-10-28T15:01:00+01:00",
             "duration_seconds": 120, "bark_seconds": 30, "bark_score": 0.6, "manual_confirmed": True,
             "bark_hours": {"2026-10-28T14": 10.0, "2026-10-28T15": 20.0}},
            {"file": "b.mp3", "started": "2026-10-28T15:30:00+01:00", "ended": "2026-10-28T15:31:00+01:00",
             "duration_seconds": 60, "bark_seconds": 5, "bark_score": 0.6, "bark_hours": {"2026-10-28T15": 5.0}},
            {"file": "c.mp3", "started": "2026-10-28T15:40:00+01:00", "ended": "2026-10-28T15:41:00+01:00",
             "duration_seconds": 60, "bark_seconds": 50, "bark_score": 0.6, "false_positive": True,
             "bark_hours": {"2026-10-28T15": 50.0}},
        ]
        original = pies.get_events
        pies.get_events = lambda: rows
        try:
            result = pies.week_heatmap(datetime(2026, 10, 30).date())
        finally:
            pies.get_events = original
        self.assertEqual(result["monday"], "2026-10-26")
        cells = {(cell["day"], cell["hour"]): cell for cell in result["cells"]}
        self.assertEqual(cells[(2, 15)], {"day": 2, "hour": 15, "seconds": 25.0, "events": 2, "confirmed": 1})
        self.assertEqual(cells[(2, 14)]["confirmed"], 1)
        self.assertEqual(result["total_seconds"], 35.0)
        self.assertEqual(result["total_events"], 2)
        self.assertEqual(result["total_confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
