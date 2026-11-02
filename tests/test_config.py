import copy
import tomllib
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from claude_usage.config import EXAMPLE_CONFIG_PATH, ConfigError, Observation, Price, find_config_path, parse_config

with EXAMPLE_CONFIG_PATH.open("rb") as fh:
    EXAMPLE = tomllib.load(fh)


def changed(section, key, value):
    table = copy.deepcopy(EXAMPLE)
    if value is KeyError:
        del table[section][key]
    else:
        table[section][key] = value
    return table


class ParseConfigTest(unittest.TestCase):
    def test_example_file(self):
        c = parse_config(EXAMPLE)
        self.assertEqual(c.reset_anchor, datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(c.cycle, timedelta(days=7))
        self.assertEqual(c.window, timedelta(hours=5))
        self.assertEqual(c.weights.prices["claude-fable-5-1"], Price(10.0, 12.5, 20.0, 0.25, 50.0))
        self.assertEqual(c.weights.prices["claude-opus-5-5"], Price(4.0, 5.0, 8.0, 0.2, 20.0))
        self.assertIn("claude", c.weights.prices)  # どのモデルにも当てはまる代わりの単価
        self.assertEqual((c.price_learning.min_records, c.price_learning.tolerance), (3, 0.03))
        self.assertEqual(c.report.models, {"Fable": "claude-fable"})
        self.assertEqual(c.project_aliases, {})
        self.assertEqual(c.pdf_dir, Path("~/Documents/Claude_Usage").expanduser())
        self.assertEqual(c.state_path, c.data_dir / "state.json")
        self.assertTrue(c.notify)
        self.assertEqual(c.notify_active_idle, timedelta(minutes=10))
        self.assertEqual((c.schedule_interval, c.schedule_label), (timedelta(minutes=30), "local.claude-usage.tick"))
        self.assertEqual((c.reminder.stale, c.reminder.repeat), (timedelta(minutes=60), timedelta(hours=6)))
        self.assertEqual((c.reminder.active_from_hour, c.reminder.active_until_hour), (8, 23))
        self.assertEqual((c.reminder.tray_notice, c.reminder.tray_window), (timedelta(hours=20), timedelta(hours=24)))
        self.assertEqual(c.app_config, Path("~/Library/Application Support/Claude/config.json").expanduser())
        self.assertEqual(c.log_path, c.data_dir / "logs" / "tick.log")
        self.assertEqual(c.limit_archive_path, c.data_dir / "plan-usage-archive.json")
        self.assertEqual(c.usage_log_path, c.data_dir / "claude-code-usage.jsonl")
        self.assertTrue(c.claude_code.enabled)
        self.assertEqual(c.claude_code.executables, (
            "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude", "~/.local/bin/claude"))
        self.assertEqual(c.claude_code.timeout, 30.0)
        self.assertEqual(c.claude_code.compare_gap, timedelta(minutes=20))
        self.assertEqual((c.claude_code.consolidate_weeks, c.claude_code.consolidate_min_compared,
                          c.claude_code.consolidate_max_failure_pct), (2, 30, 5.0))
        self.assertEqual(c.report.dev_time_gap, timedelta(minutes=15))
        self.assertEqual(c.activity.min_responses, 10)
        self.assertEqual((c.activity.bin, c.activity.half_life), (timedelta(minutes=15), timedelta(minutes=30)))
        self.assertEqual((c.activity.heat_percentile, c.activity.fade_level, c.activity.max_file_sessions), (99.0, 0.05, 8))
        self.assertEqual(c.activity.default_branches, {"main", "master", "HEAD", "develop"})
        self.assertEqual(c.activity.refresh, timedelta(minutes=60))
        self.assertEqual(c.activity.mature_usd, 300)
        self.assertEqual((c.activity.dormant, c.activity.dead), (timedelta(days=7), timedelta(days=30)))
        self.assertEqual(c.activity.categories["game"], "spiral")
        self.assertEqual(c.activity.project_kinds["PokemonChampioons_Adviser"], "data")   # 2026-11-03 ユーザーの決定
        self.assertEqual(c.activity_layout_path, c.data_dir / "activity-layout.json")
        self.assertEqual(c.activity_html, Path("~/Documents/Claude_Usage/Claude_Usage_Activity.html").expanduser())
        self.assertEqual(c.desktop_sessions, Path("~/Library/Application Support/Claude/claude-code-sessions").expanduser())
        # Fable は 9/1 に終わる週(9/1 19:00 JST のリセット)まで単価に対して 1 倍(公表の 2 倍は単価の比と同じ)、
        # それ以降は記録からの推定の 2 倍(2026-10-23 ユーザーの決定と、単価ベースへの切り替え)
        self.assertEqual(c.weights.model_factors, {"claude-fable": (
            (datetime(2026, 6, 9, 16, 15, 44, tzinfo=timezone.utc), 1.0),
            (datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc), 2.0))})
        self.assertEqual((c.non_code.max_gap, c.non_code.min_known_pct), (timedelta(minutes=45), 80))
        self.assertEqual(c.non_code.weeks, {date(2026, 10, 20): {"Cowork": 40.0}})
        f = c.weekly_factor
        self.assertEqual((f.model, f.max_gap, f.min_intervals, f.min_model_intervals, f.min_factor, f.max_factor,
                          f.model_limit_share), ("claude-fable", timedelta(minutes=45), 10, 4, 0.5, 10.0, 50.0))
        # スクリーンショットの値(13 枚分 + 10/27 に終わる週の途中と最終の値)。時刻順に並べ、all・モデル別・製品別に分ける
        self.assertEqual(len(c.observed), 15)
        sep22 = next(o for o in c.observed if o.ts.date() == date(2026, 9, 22))
        self.assertEqual(dict(sep22.products), {"Claude Code": 98.0, "チャット": 1.0, "Cowork": 1.0})
        self.assertEqual(sep22.non_code_pct, 2.0)  # チャットも Claude Code 以外に数える
        self.assertEqual(c.observed[0], Observation(datetime(2026, 7, 28, 9, 48, 17, tzinfo=timezone.utc), 65.0,
                                                    {"Fable": 100.0}))
        self.assertEqual([o.ts for o in c.observed], sorted(o.ts for o in c.observed))

    def test_observed_points(self):
        c = parse_config(changed("observed", "points", {"2026-10-27T18:00:00+09:00": {"Fable": 30},
                                                         "2026-10-26T18:00:00+09:00": {"all": 50}}))
        self.assertEqual([(o.all_pct, dict(o.models)) for o in c.observed], [(50.0, {}), (None, {"Fable": 30.0})])
        self.assertIsNone(c.observed[0].non_code_pct)  # 製品別の割合がない

    def test_model_factor_periods_are_sorted(self):
        c = parse_config(changed("metric", "model_factors", {"claude-x": [
            {"from": "2026-07-01T00:00:00Z", "factor": 3}, {"from": "2026-06-01T00:00:00+09:00", "factor": 2.5}]}))
        self.assertEqual([f for _, f in c.weights.model_factors["claude-x"]], [2.5, 3.0])

    def test_missing_key(self):
        with self.assertRaisesRegex(ConfigError, "reset_anchor"):
            parse_config(changed("week", "reset_anchor", KeyError))

    def test_invalid_values(self):
        cases = [
            ("week", "reset_anchor", "2026-10-27T19:00:00"),   # 時差なし
            ("week", "reset_anchor", "火曜 19 時"),
            ("week", "cycle_days", 0),
            ("week", "window_hours", True),
            ("metric", "prices", KeyError),
            ("metric", "prices", {"claude-opus": [4, 5, 8, 0.2, 20]}),   # "claude" の行がない
            ("metric", "prices", {"claude": [4, 5, 8, 0.2]}),
            ("metric", "prices", {"claude": [4, 5, 8, -0.2, 20]}),
            ("metric", "price_learning", {"min_records": 0, "tolerance_pct": 3}),
            ("metric", "price_learning", {"min_records": 3, "tolerance_pct": 100}),
            ("projects", "worktree_patterns", "/x"),
            ("projects", "aliases", {"a": 1}),
            ("report", "max_slices", 0),
            ("report", "models", {"Fable": ""}),
            ("pdf", "timeout_seconds", 0),
            ("notify", "enabled", "yes"),
            ("notify", "active_idle_minutes", 0),
            ("schedule", "interval_minutes", 0),
            ("schedule", "label", "bad label/../x"),
            ("reminder", "enabled", 1),
            ("reminder", "stale_minutes", 0),
            ("reminder", "active_from_hour", 23),    # active_until_hour(23)以上
            ("reminder", "active_until_hour", 25),
            ("reminder", "tray_notice_hours", 24),   # tray_window_hours(24)以上
            ("reminder", "tray_window_hours", KeyError),
            ("paths", "app_config", KeyError),
            ("metric", "model_factors", KeyError),
            ("metric", "model_factors", {"claude-fable": 4}),
            ("metric", "model_factors", {"claude-fable": [{"from": "2026-06-24T14:39:56", "factor": 4}]}),  # 時差なし
            ("metric", "model_factors", {"claude-fable": [{"from": "2026-06-24T14:39:56Z", "factor": 0}]}),
            ("metric", "model_factors", {"claude-fable": [{"factor": 4}]}),
            ("non_code", "max_gap_minutes", 0),
            ("non_code", "min_known_pct", 101),
            ("non_code", "estimate_name", ""),
            ("non_code", "weeks", {"10/20": {"Cowork": 40}}),
            ("non_code", "weeks", {"2026-10-20": {"Cowork": "40"}}),
            ("non_code", "weeks", {"2026-10-20": {"Cowork": 60, "チャット": 40}}),  # 合計 100 以上
            ("claude_code", "enabled", "yes"),
            ("claude_code", "executables", []),
            ("claude_code", "executables", "~/.local/bin/claude"),
            ("claude_code", "executables", [""]),
            ("claude_code", "timeout_seconds", 0),
            ("claude_code", "compare_gap_minutes", 0),
            ("claude_code", "consolidate_weeks", 0),
            ("claude_code", "consolidate_min_compared", 0),
            ("claude_code", "consolidate_max_failure_pct", -1),
            ("report", "dev_time_gap_minutes", 0),
            ("paths", "desktop_sessions", KeyError),
            ("paths", "activity_html", KeyError),
            ("activity", "min_responses", 0),
            ("activity", "bin_minutes", 0),
            ("activity", "half_life_minutes", 0),
            ("activity", "heat_percentile", 101),
            ("activity", "fade_level", 0),
            ("activity", "fade_level", 1),
            ("activity", "default_branches", "main"),
            ("activity", "max_file_sessions", 1),
            ("activity", "refresh_minutes", 0),
            ("activity", "dead_days", 7),                                 # dormant_days(7)以下
            ("activity", "project_kinds", {"x": "unknown-kind"}),         # categories にない種類
            ("activity", "categories", KeyError),
            ("metric", "weekly_factor", KeyError),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "model": ""}),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "min_factor": 5, "max_factor": 2}),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "min_intervals": 1}),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "max_gap_minutes": 0}),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "model_limit_share_pct": 0}),
            ("metric", "weekly_factor", {**EXAMPLE["metric"]["weekly_factor"], "model_limit_share_pct": 120}),
            ("observed", "points", KeyError),
            ("observed", "points", {"2026-10-27T18:00:00": {"all": 50}}),       # 時差なし
            ("observed", "points", {"10/27 18:00": {"all": 50}}),
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {"all": 150}}),
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {}}),
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {"all": "50"}}),
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {"all": 50, "products": {"Cowork": 10}}}),  # Code がない
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {"all": 50, "products": {"Claude Code": 120}}}),
            ("observed", "points", {"2026-10-27T18:00:00+09:00": {"all": 50, "products": 80}}),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                with self.assertRaises(ConfigError):
                    parse_config(changed(section, key, value))

    def test_env_override(self):
        self.assertEqual(find_config_path({"CLAUDE_USAGE_CONFIG": "/x/config.toml"}), Path("/x/config.toml"))


if __name__ == "__main__":
    unittest.main()
