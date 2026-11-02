import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_usage.sources import LimitSample
from claude_usage.weeks import Week, completed_weeks, final_pct, resets_within, week_containing

JST = ZoneInfo("Asia/Tokyo")
ANCHOR = datetime(2026, 10, 27, 19, 0, tzinfo=JST)
CYCLE = timedelta(days=7)


def jst(*args):
    return datetime(*args, tzinfo=JST)


def sample(ts, sd, fh=None):
    return LimitSample(ts, fh, sd)


class WeekContainingTest(unittest.TestCase):
    def test_before_and_after_anchor(self):
        w = week_containing(jst(2026, 10, 23, 15, 0), ANCHOR, CYCLE)
        self.assertEqual((w.start, w.end), (jst(2026, 10, 20, 19, 0), jst(2026, 10, 27, 19, 0)))
        w = week_containing(jst(2026, 10, 28, 0, 0), ANCHOR, CYCLE)
        self.assertEqual(w.start, ANCHOR)

    def test_boundary_belongs_to_next_week(self):
        w = week_containing(jst(2026, 10, 20, 19, 0), ANCHOR, CYCLE)
        self.assertEqual(w.start, jst(2026, 10, 20, 19, 0))
        self.assertTrue(w.contains(jst(2026, 10, 20, 19, 0)))
        self.assertFalse(w.contains(jst(2026, 10, 27, 19, 0)))

    def test_far_past(self):
        w = week_containing(jst(2026, 7, 17, 12, 0), ANCHOR, CYCLE)
        self.assertEqual(w.start, jst(2026, 7, 14, 19, 0))


class CompletedWeeksTest(unittest.TestCase):
    def test_only_finished_weeks(self):
        weeks = completed_weeks(jst(2026, 10, 1, 0, 0), jst(2026, 10, 23, 15, 0), ANCHOR, CYCLE)
        self.assertEqual([w.end.astimezone(JST).strftime("%m/%d") for w in weeks], ["10/06", "10/13", "10/20"])

    def test_none_finished(self):
        self.assertEqual(completed_weeks(jst(2026, 10, 21, 0, 0), jst(2026, 10, 23, 0, 0), ANCHOR, CYCLE), [])


class SamplesTest(unittest.TestCase):
    WEEK = Week(jst(2026, 9, 29, 19, 0), jst(2026, 10, 6, 19, 0))
    SAMPLES = [
        sample(jst(2026, 9, 29, 18, 58), 57),   # 前の週
        sample(jst(2026, 9, 29, 19, 13), 0),
        sample(jst(2026, 10, 4, 14, 35), 66),
        sample(jst(2026, 10, 4, 14, 42), 0),    # 手動リセット
        sample(jst(2026, 10, 4, 20, 19), 16),
        sample(jst(2026, 10, 5, 9, 0), None, fh=5),
    ]

    def test_final_pct(self):
        self.assertEqual(final_pct(self.SAMPLES, self.WEEK), 16)
        self.assertIsNone(final_pct(self.SAMPLES, Week(jst(2026, 10, 6, 19, 0), jst(2026, 10, 13, 19, 0))))

    def test_resets_within_week_only(self):
        resets = resets_within(self.SAMPLES, self.WEEK)
        self.assertEqual([(a.seven_day, b.seven_day) for a, b in resets], [(66, 0)])


if __name__ == "__main__":
    unittest.main()
