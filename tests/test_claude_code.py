import copy
import json
import stat
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_usage.claude_code import (REQUEST_ID, UsageRequestError, find_executable, record_usage, response_body,
                                      usage_record, version_key)
from claude_usage.config import EXAMPLE_CONFIG_PATH, parse_config
from claude_usage.sources import read_usage_log

NOW = datetime(2026, 10, 28, 1, 40, tzinfo=timezone.utc)

# Claude Code 2.1.308 の get_usage の応答(rate_limits のうち使う部分と、使わない欄の例)
BODY = {
    "subscription_type": "max",
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 37, "resets_at": "2026-10-28T02:29:59Z"},
        "seven_day": {"utilization": 4, "resets_at": "2026-11-03T09:59:59Z"},
        "seven_day_opus": None,
        "model_scoped": [{"display_name": "Fable", "utilization": 0, "resets_at": "2026-11-03T10:00:00Z"}],
        "seven_day_breakdown": {"as_of": "2026-10-28T01:39:13Z", "rows": [
            {"key": "claude_code", "display_name": "Claude Code", "percent": 99},
            {"key": "chat", "display_name": "Chats", "percent": 1},
            {"key": "cowork", "display_name": "Cowork", "percent": 0}]},
        "weekly_scoped_shares": [{"limit_index": 2, "used_percent_of_weekly": 0, "allowance_percent_of_weekly": 50}],
    },
}


class UsageRecordTest(unittest.TestCase):
    def test_full_answer(self):
        self.assertEqual(usage_record(BODY, NOW), {
            "t": "2026-10-28T01:40:00+00:00", "all": 4.0, "five_hour": 37.0, "models": {"Fable": 0.0},
            "products": {"claude_code": 99.0, "chat": 1.0, "cowork": 0.0}})

    def test_without_per_model_windows(self):
        limits = {k: v for k, v in BODY["rate_limits"].items() if k not in ("model_scoped", "seven_day_breakdown")}
        record = usage_record({"rate_limits": limits}, NOW)
        self.assertEqual((record["all"], record["models"], record["products"]), (4.0, {}, {}))

    def test_no_rate_limits(self):
        self.assertIsNone(usage_record({"rate_limits_available": False, "rate_limits": None}, NOW))  # API キーなど
        self.assertIsNone(usage_record({"rate_limits": {"seven_day": None, "model_scoped": []}}, NOW))


class ResponseBodyTest(unittest.TestCase):
    @staticmethod
    def response(request_id=REQUEST_ID, **resp):
        return json.dumps({"type": "control_response", "response": {"request_id": request_id, **resp}})

    def test_finds_the_answer_among_other_lines(self):
        stdout = "\n".join(["not json", json.dumps({"type": "system"}), self.response("other", subtype="success",
                            response={"x": 1}), self.response(subtype="success", response=BODY)])
        self.assertEqual(response_body(stdout), BODY)

    def test_error_and_missing(self):
        with self.assertRaisesRegex(UsageRequestError, "エラー"):
            response_body(self.response(subtype="error", error="unknown subtype"))
        with self.assertRaisesRegex(UsageRequestError, "応答がありません"):
            response_body("")


class FindExecutableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, rel, executable=True):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def test_newest_version_wins(self):
        self.make("app/2.1.99/h1/claude")
        newest = self.make("app/2.1.308/h2/claude")
        self.make("app/2.1.400/h3/claude", executable=False)  # 実行できないものは使わない
        target = self.make("versions/2.1.280")
        (self.root / "bin").mkdir()
        (self.root / "bin" / "claude").symlink_to(target)  # 版の番号はリンク先から読む
        patterns = [str(self.root / "app/*/*/claude"), str(self.root / "bin/claude")]
        self.assertEqual(find_executable(patterns), newest)
        self.assertEqual(find_executable([str(self.root / "bin/claude")]), self.root / "bin" / "claude")
        self.assertIsNone(find_executable([str(self.root / "missing/*")]))

    def test_version_key(self):
        self.assertEqual(version_key("/x/claude-code/2.1.308/a93ded4b1b47/claude.app"), (2, 1, 308))
        self.assertEqual(version_key("/x/.local/share/claude/versions/2.1.280"), (2, 1, 280))
        self.assertEqual(version_key("/usr/local/bin/claude"), ())


class RecordUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        exe = root / "bin" / "claude"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        with EXAMPLE_CONFIG_PATH.open("rb") as fh:
            self.table = copy.deepcopy(tomllib.load(fh))
        self.table["paths"]["data_dir"] = str(root / "data")
        self.table["claude_code"]["executables"] = [str(exe)]
        self.config = parse_config(self.table)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, answer):
        def fake(executable, cwd, timeout):
            self.calls.append((executable.name, cwd, timeout))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return fake

    def test_appends_one_line_per_call(self):
        self.assertIsNone(record_usage(self.config, NOW, self.request(BODY)))
        self.assertIsNone(record_usage(self.config, NOW.replace(minute=50), self.request(BODY)))
        self.assertEqual(self.calls[0], ("claude", self.config.data_dir, 30.0))
        log = read_usage_log(self.config.usage_log_path)
        self.assertEqual([(o.ts.minute, o.all_pct, dict(o.models)) for o in log.observations],
                         [(40, 4.0, {"Fable": 0.0}), (50, 4.0, {"Fable": 0.0})])
        self.assertEqual([(s.five_hour, s.seven_day) for s in log.samples], [(37.0, 4.0), (37.0, 4.0)])

    def test_problems_are_reported_not_raised(self):
        self.assertIn("取得できませんでした: 30 秒",
                      record_usage(self.config, NOW, self.request(UsageRequestError("30 秒以内に応答がありませんでした"))))
        self.assertIn("使用率がありませんでした", record_usage(self.config, NOW, self.request({"rate_limits": None})))
        self.table["claude_code"]["executables"] = [str(Path(self.tmp.name) / "missing")]
        self.assertIn("見つからない", record_usage(parse_config(self.table), NOW, self.request(BODY)))
        # 取れなかった回も時刻と理由を残す(2 つの記録の照合で、取得の手間・確かさを判断するため。2026-10-28 ユーザーの指定)
        lines = [json.loads(line) for line in self.config.usage_log_path.read_text().splitlines()]
        self.assertEqual([sorted(r) for r in lines], [["error", "t"]] * 3)
        self.assertIn("30 秒以内", lines[0]["error"])
        log = read_usage_log(self.config.usage_log_path)
        self.assertEqual((log.samples, log.observations, log.failures), ([], [], [NOW] * 3))

    def test_disabled(self):
        self.table["claude_code"]["enabled"] = False
        self.assertIsNone(record_usage(parse_config(self.table), NOW, self.request(BODY)))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
