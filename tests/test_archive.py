import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_usage.archive import load_limit_samples
from claude_usage.sources import LimitSample


def ms(day, hour):
    return int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def sample(day, hour, sd):
    return LimitSample(datetime(2026, 10, day, hour, tzinfo=timezone.utc), None, float(sd))


class LoadLimitSamplesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.history = root / "plan-usage-history.json"
        self.archive = root / "data" / "plan-usage-archive.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_history(self, *rows):
        self.history.write_text(json.dumps({"version": 2, "samples": [
            {"t": ms(day, hour), "org": "o", "u": {"sd": sd}} for day, hour, sd in rows]}))

    def test_keeps_samples_the_app_has_dropped(self):
        self.write_history((1, 0, 10), (1, 1, 11))
        self.assertEqual(load_limit_samples(self.history, self.archive), ([sample(1, 0, 10), sample(1, 1, 11)], None))
        self.assertTrue(self.archive.exists())
        self.write_history((1, 1, 11), (2, 0, 20))  # アプリが古い記録を消した後
        samples, error = load_limit_samples(self.history, self.archive)
        self.assertEqual(samples, [sample(1, 0, 10), sample(1, 1, 11), sample(2, 0, 20)])
        self.assertIsNone(error)

    def test_archive_is_not_rewritten_without_new_samples(self):
        self.write_history((1, 0, 10))
        load_limit_samples(self.history, self.archive)
        before = self.archive.stat().st_mtime_ns
        self.archive.write_text(self.archive.read_text())  # 内容は同じ
        written = self.archive.stat().st_mtime_ns
        load_limit_samples(self.history, self.archive)
        self.assertEqual(self.archive.stat().st_mtime_ns, written)
        self.assertGreaterEqual(written, before)

    def test_unreadable_history_uses_the_archive(self):
        self.write_history((1, 0, 10))
        load_limit_samples(self.history, self.archive)
        self.history.write_text("{broken")
        samples, error = load_limit_samples(self.history, self.archive)
        self.assertEqual(samples, [sample(1, 0, 10)])
        self.assertIn("JSON", error)

    def test_unreadable_archive_is_kept_as_it_is(self):
        self.write_history((1, 0, 10))
        self.archive.parent.mkdir(parents=True)
        self.archive.write_text("{broken")
        samples, error = load_limit_samples(self.history, self.archive)
        self.assertEqual(samples, [sample(1, 0, 10)])
        self.assertIn("plan-usage-archive.json を読めません", error)
        self.assertEqual(self.archive.read_text(), "{broken")  # 上書きして残した記録を失わない

    def test_no_files(self):
        self.assertEqual(load_limit_samples(self.history, self.archive), ([], None))
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
