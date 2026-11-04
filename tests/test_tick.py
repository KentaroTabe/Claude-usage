import copy
from dataclasses import replace
import json
import os
import plistlib
import stat
import tempfile
import time
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_usage.config import EXAMPLE_CONFIG_PATH, ReminderOptions, parse_config
from claude_usage.schedule import build_plist, plist_path
from claude_usage.state import load_state
from claude_usage.tick import (app_running, format_log, parse_hid_idle, reminder_due, reminder_message, run_tick,
                               throttled, tray_notice_at, tray_reminder_due, tray_reminder_message, user_active,
                               window_lapsed)

JST = ZoneInfo("Asia/Tokyo")
R = ReminderOptions(enabled=True, stale=timedelta(minutes=60), repeat=timedelta(hours=6),
                    active_from_hour=8, active_until_hour=23, tray_notice=timedelta(hours=20),
                    tray_window=timedelta(hours=24))


def jst(*args):
    return datetime(*args, tzinfo=JST)


class ReminderDueTest(unittest.TestCase):
    """記録が止まった通知。24 時間の期限切れ(lapsed)か、アプリが起動していない(running でない)ときだけ。"""
    NOW = jst(2026, 10, 24, 10, 0)

    def due(self, latest, now=None, last=None, r=R, running=True, lapsed=True):
        return reminder_due(latest, now or self.NOW, last, r, JST, running, lapsed)

    def test_fresh_records(self):
        self.assertFalse(self.due(self.NOW - timedelta(minutes=59)))

    def test_stale_records(self):
        self.assertTrue(self.due(self.NOW - timedelta(minutes=60)))
        self.assertTrue(self.due(None))  # 記録がひとつもない

    def test_only_when_lapsed_or_app_not_running(self):
        stale = self.NOW - timedelta(hours=13)
        self.assertFalse(self.due(stale, lapsed=False))                 # スリープ・通信の失敗: 起きれば再開する
        self.assertTrue(self.due(stale, lapsed=False, running=False))   # アプリが起動していない
        self.assertTrue(self.due(stale, lapsed=True))                   # 24 時間の期限切れ

    def test_not_repeated_within_interval(self):
        stale = self.NOW - timedelta(hours=3)
        self.assertFalse(self.due(stale, last=(self.NOW - timedelta(hours=5)).isoformat()))
        self.assertTrue(self.due(stale, last=(self.NOW - timedelta(hours=6)).isoformat()))

    def test_quiet_hours_and_disabled(self):
        stale = self.NOW - timedelta(hours=3)
        self.assertFalse(self.due(stale, jst(2026, 10, 24, 7, 59)))
        self.assertTrue(self.due(stale, jst(2026, 10, 24, 8, 0)))
        self.assertFalse(self.due(stale, jst(2026, 10, 24, 23, 0)))
        self.assertFalse(self.due(stale, r=replace(R, enabled=False)))

    def test_window_lapsed(self):
        opened = jst(2026, 10, 24, 12, 0)
        self.assertFalse(window_lapsed(opened, jst(2026, 10, 25, 11, 59), R))
        self.assertTrue(window_lapsed(opened, jst(2026, 10, 25, 12, 0), R))
        self.assertTrue(window_lapsed(None, self.NOW, R))  # 時刻が読めない


class IdleTest(unittest.TestCase):
    def test_parse_and_active(self):
        out = '+-o IOHIDSystem  <class IOHIDSystem>\n    {\n      "HIDIdleTime" = 29418462958\n    }\n'
        self.assertEqual(parse_hid_idle(out), timedelta(seconds=29, microseconds=418462))
        self.assertIsNone(parse_hid_idle("壊れた出力"))
        ten = timedelta(minutes=10)
        self.assertTrue(user_active(timedelta(minutes=9), ten))
        self.assertFalse(user_active(timedelta(minutes=11), ten))
        self.assertTrue(user_active(None, ten))  # 読めなければ通知を止めない


class TrayReminderTest(unittest.TestCase):
    """使用量メニューを最後に開いてから 20 時間で、記録が止まる(24 時間)前に知らせる。"""

    def test_notice_time(self):
        self.assertEqual(tray_notice_at(jst(2026, 10, 24, 12, 0), R, JST), jst(2026, 10, 25, 8, 0))   # 20 時間後(昼)
        # 20 時間後が夜で、朝 8 時より前に記録が止まる → その夜 22 時に早める
        self.assertEqual(tray_notice_at(jst(2026, 10, 24, 6, 0), R, JST), jst(2026, 10, 24, 22, 0))   # 20 時間後 2 時
        self.assertEqual(tray_notice_at(jst(2026, 10, 24, 3, 30), R, JST), jst(2026, 10, 24, 22, 0))  # 20 時間後 23:30
        # 20 時間後が夜でも、止まるのが朝 8 時より後なら、そのまま(通知できる 8 時に出る)
        self.assertEqual(tray_notice_at(jst(2026, 10, 24, 10, 0), R, JST), jst(2026, 10, 25, 6, 0))

    def test_due_once_per_open_and_not_after_stop(self):
        opened = jst(2026, 10, 24, 12, 0)
        self.assertFalse(tray_reminder_due(opened, jst(2026, 10, 25, 7, 59), None, R, JST))  # 20 時間前
        self.assertTrue(tray_reminder_due(opened, jst(2026, 10, 25, 8, 0), None, R, JST))
        self.assertFalse(tray_reminder_due(opened, jst(2026, 10, 25, 9, 0), opened.isoformat(), R, JST))  # 通知済み
        self.assertFalse(tray_reminder_due(opened, jst(2026, 10, 25, 12, 0), None, R, JST))  # 止まった後
        self.assertFalse(tray_reminder_due(None, jst(2026, 10, 25, 9, 0), None, R, JST))      # 時刻が読めない
        self.assertFalse(tray_reminder_due(opened, jst(2026, 10, 25, 8, 0), None, replace(R, enabled=False), JST))

    def test_quiet_hours(self):
        opened = jst(2026, 10, 24, 10, 0)  # 20 時間後は 6 時(夜)、止まるのは 10 時 → 8 時に出す
        self.assertFalse(tray_reminder_due(opened, jst(2026, 10, 25, 6, 30), None, R, JST))
        self.assertTrue(tray_reminder_due(opened, jst(2026, 10, 25, 8, 0), None, R, JST))
        early = jst(2026, 10, 24, 6, 0)    # 止まるのが 6 時 → 前の夜 22 時から出す
        self.assertTrue(tray_reminder_due(early, jst(2026, 10, 24, 22, 0), None, R, JST))
        self.assertFalse(tray_reminder_due(early, jst(2026, 10, 24, 23, 0), None, R, JST))

    def test_message(self):
        msg = tray_reminder_message(jst(2026, 10, 24, 12, 0), jst(2026, 10, 25, 8, 10), R, JST)
        self.assertIn("最後に開いてから 20 時間", msg)
        self.assertIn("10/25 12:00 に使用量の記録が止まる", msg)


class MessageTest(unittest.TestCase):
    def test_messages(self):
        now = jst(2026, 10, 24, 10, 0)
        msg = reminder_message(now - timedelta(hours=3, minutes=40), now, True, R)
        self.assertIn("最後に開いてから 24 時間を過ぎたため、使用量の記録が 3 時間止まっています", msg)
        self.assertIn("起動していない", reminder_message(now, now, False, R))
        self.assertIn("見つかりません", reminder_message(None, now, True, R))

    def test_app_running(self):
        self.assertTrue(app_running("/usr/sbin/cfprefsd\n/Applications/Claude.app/Contents/MacOS/Claude\n"))
        self.assertFalse(app_running("/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/x\n"))

    def test_throttled_and_log(self):
        now = jst(2026, 10, 24, 10, 0)
        self.assertFalse(throttled(None, now, timedelta(hours=1)))
        self.assertTrue(throttled((now - timedelta(minutes=30)).isoformat(), now, timedelta(hours=1)))
        self.assertEqual(format_log(["a", "b"], now, JST), "2026-10-24 10:00:00 a\n2026-10-24 10:00:00 b\n")
        self.assertEqual(format_log([], now, JST), "")


class SchedulePlistTest(unittest.TestCase):
    def test_build_plist(self):
        content = build_plist("local.claude-usage.tick", "/opt/homebrew/bin/python3.12", Path("/repo/scripts/cu"),
                              1800, Path("/data/logs/tick.log"))
        d = plistlib.loads(content)
        self.assertEqual(d["Label"], "local.claude-usage.tick")
        self.assertEqual(d["ProgramArguments"], ["/opt/homebrew/bin/python3.12", "/repo/scripts/cu", "tick"])
        self.assertEqual((d["StartInterval"], d["RunAtLoad"], d["ProcessType"]), (1800, True, "Background"))
        self.assertEqual(d["StandardOutPath"], "/data/logs/tick.log")
        self.assertEqual(plist_path("x").name, "x.plist")


class RunTickTest(unittest.TestCase):
    """レポート作成と通知までを通す(PDF 変換と通知・ps は偽物で代用)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        with EXAMPLE_CONFIG_PATH.open("rb") as fh:
            table = copy.deepcopy(tomllib.load(fh))
        table["paths"].update(transcripts_root=str(root / "projects"), plan_usage_history=str(root / "history.json"),
                              app_config=str(root / "app-config.json"),
                              data_dir=str(root / "data"), demo_dir=str(root / "demo"), pdf_dir=str(root / "pdf"))
        # 設定ファイルの例にある利用者の実データ(画面の値・手で入れた週の値)で、過去の週のレポートが作られないようにする
        table["observed"]["points"] = {}
        table["non_code"]["weeks"] = {}
        self.config = parse_config(table)
        self.app_config = root / "app-config.json"
        (root / "projects" / "p").mkdir(parents=True)
        line = json.dumps({"type": "assistant", "timestamp": "2026-10-21T01:00:00Z", "sessionId": "s", "cwd": "/x/alpha",
                           "requestId": "r", "message": {"id": "m", "model": "claude-opus-5",
                                                         "usage": {"input_tokens": 1, "output_tokens": 2}}})
        (root / "projects" / "p" / "s.jsonl").write_text(line + "\n")
        self.history = root / "history.json"
        self.write_history(jst(2026, 10, 21, 10, 0))
        helper = self.config.pdf_helper_path
        helper.parent.mkdir(parents=True)
        helper.write_text('#!/bin/sh\nprintf "%%PDF fake" > "$2"\n')
        helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
        future = time.time() + 3600
        os.utime(helper, (future, future))
        self.sent = []
        self.usage_calls = []
        self.usage_problem = None
        self.activity_calls = []
        self.activity_error = None

    def tearDown(self):
        self.tmp.cleanup()

    def fake_usage(self, config, now):
        """Claude Code を起動する代わり(呼ばれた時刻を残し、取得の失敗を装える)。"""
        self.usage_calls.append(now)
        return self.usage_problem

    def fake_activity(self, config, now):
        """再生画面を作る代わり(全期間の会話ログを読まない)。"""
        self.activity_calls.append(now)
        if self.activity_error is not None:
            raise self.activity_error

    def write_history(self, latest):
        self.history.write_text(json.dumps({"version": 2, "samples": [
            {"t": int(latest.timestamp() * 1000), "org": "o", "u": {"fh": 10, "sd": 30}}]}))

    def tick(self, now, idle=timedelta(seconds=5)):
        return run_tick(self.config, now, notifier=lambda t, m: self.sent.append((t, m)),
                        ps=lambda: "/Applications/Claude.app/Contents/MacOS/Claude\n", idle=lambda: idle,
                        usage=self.fake_usage, activity=self.fake_activity)

    def test_report_after_reset_then_reminder_when_records_stop(self):
        log = self.tick(jst(2026, 10, 27, 19, 30))     # 週が終わった直後: レポート + 記録が止まっている通知
        self.assertTrue(any(l.startswith("作成: ") and l.endswith("Claude_Usage_2026-10-27.pdf") for l in log))
        self.assertEqual([t for t, _ in self.sent], ["Claude 使用量 週次レポート", "Claude 使用量"])
        self.assertIn("止まっています", self.sent[1][1])

        self.sent.clear()
        self.assertEqual(self.tick(jst(2026, 10, 27, 20, 0)), [])   # 6 時間以内は繰り返さない
        self.assertEqual(self.sent, [])

        self.write_history(jst(2026, 10, 28, 9, 50))                 # メニューを開いて記録が再開
        self.assertEqual(self.tick(jst(2026, 10, 28, 10, 0)), [])
        self.assertEqual(self.sent, [])
        self.assertIn("reminded_at", load_state(self.config.state_path))

    def test_claude_code_usage_every_tick_problems_only_logged(self):
        self.write_history(jst(2026, 10, 23, 11, 55))  # デスクトップアプリの記録は動いている
        self.app_config.write_text(json.dumps({"planUsageLastTrayOpenAt": int(jst(2026, 10, 23, 8, 0).timestamp() * 1000)}))
        self.usage_problem = "Claude Code の /usage の値を取得できませんでした: 30 秒以内に応答がありませんでした"
        log = self.tick(jst(2026, 10, 23, 12, 0))
        self.assertEqual(self.usage_calls, [jst(2026, 10, 23, 12, 0)])
        self.assertEqual(log, [self.usage_problem])
        self.assertEqual(self.sent, [])  # 取れなくても通知はしない(次の回にまた取る)
        self.usage_problem = None
        self.assertEqual(self.tick(jst(2026, 10, 23, 12, 30)), [])
        self.assertEqual(len(self.usage_calls), 2)

    def test_activity_screen_is_rebuilt_weekly_with_the_report(self):
        # 2026-11-04 ユーザーの指定: 画面は週 1 回、週次リセットの後に週次レポートを作る回に作り直す
        #(それまでは会話ログが更新されていれば 60 分ごと)。一度も作っていなければすぐ作る
        log_file = Path(self.tmp.name) / "projects" / "p" / "s.jsonl"
        first = jst(2026, 10, 23, 12, 0)
        self.tick(first)
        self.assertEqual(self.activity_calls, [first])
        self.assertEqual(load_state(self.config.state_path)["activity_built_at"], first.isoformat())
        later = first + timedelta(hours=3)
        os.utime(log_file, (later.timestamp() - 60, later.timestamp() - 60))
        self.tick(later)                                        # ログが更新されても、週の途中は作らない
        self.assertEqual(self.activity_calls, [first])
        reset = jst(2026, 10, 27, 19, 30)
        log = self.tick(reset)                                  # 週次レポートを作った回に作り直す
        self.assertTrue(any(l.startswith("作成: ") for l in log))
        self.assertEqual(self.activity_calls, [first, reset])
        self.tick(reset + timedelta(minutes=30))                # その週の分は作り終えた
        self.assertEqual(self.activity_calls, [first, reset])

    def test_activity_screen_retried_after_failing_on_the_report_tick(self):
        self.tick(jst(2026, 10, 23, 12, 0))
        self.activity_error = OSError("disk full")
        reset = jst(2026, 10, 27, 19, 30)
        self.assertIn("活動の再生画面を作れませんでした: OSError: disk full", self.tick(reset))
        self.activity_error = None
        retry = reset + timedelta(minutes=30)
        self.tick(retry)                                        # レポートは作り済みでも、画面は次の回に作り直す
        self.assertEqual(self.activity_calls, [jst(2026, 10, 23, 12, 0), reset, retry])

    def test_activity_screen_failure_is_only_logged(self):
        self.activity_error = OSError("disk full")
        log = self.tick(jst(2026, 10, 23, 12, 0))
        self.assertIn("活動の再生画面を作れませんでした: OSError: disk full", log)
        self.assertNotIn("activity_built_at", load_state(self.config.state_path))  # 次の回にまた作る

    def test_reminder_before_records_stop(self):
        self.write_history(jst(2026, 10, 25, 9, 55))  # 記録は動いている
        opened = jst(2026, 10, 24, 12, 0)
        self.app_config.write_text(json.dumps({"planUsageLastTrayOpenAt": int(opened.timestamp() * 1000),
                                               "oauth:tokenCache": "読まない値"}))
        self.assertEqual(self.tick(jst(2026, 10, 24, 20, 0)), [])  # まだ 8 時間
        log = self.tick(jst(2026, 10, 25, 10, 0))                   # 22 時間: 記録が止まる前の通知
        self.assertEqual(len(self.sent), 1)
        self.assertIn("10/25 12:00 に使用量の記録が止まる", self.sent[0][1])
        self.assertTrue(any(line.startswith("通知: 使用量メニューを最後に開いてから") for line in log))
        self.write_history(jst(2026, 10, 25, 10, 25))
        self.tick(jst(2026, 10, 25, 10, 30))
        self.assertEqual(len(self.sent), 1)  # 同じ回(最後に開いた時刻が同じ)では 1 回だけ

    def test_no_stopped_notice_while_within_the_window(self):
        # 最後にメニューを開いてから 24 時間以内なら、記録が古くても(スリープなど)通知しない
        self.app_config.write_text(json.dumps({"planUsageLastTrayOpenAt": int(jst(2026, 10, 27, 8, 0).timestamp() * 1000)}))
        self.write_history(jst(2026, 10, 27, 9, 0))
        self.tick(jst(2026, 10, 27, 19, 30))  # 記録は 10 時間止まっているが、期限は 10/28 8:00
        self.assertEqual([t for t, _ in self.sent], ["Claude 使用量 週次レポート"])

    def test_notices_wait_until_the_mac_is_in_use(self):
        away = timedelta(hours=2)
        log = self.tick(jst(2026, 10, 27, 19, 30), idle=away)  # レポート作成 + 記録が止まった通知の条件(期限切れ)
        self.assertTrue(any(line.startswith("作成: ") for line in log))
        self.assertEqual(self.sent, [])                                        # 操作していないので出さない
        self.assertEqual(len(load_state(self.config.state_path)["pending_notices"]), 1)  # レポートの通知は持ち越し
        self.tick(jst(2026, 10, 27, 20, 0), idle=away)
        self.assertEqual(self.sent, [])
        self.tick(jst(2026, 10, 27, 20, 30))                                   # 操作を始めた次の回に出す
        self.assertEqual([t for t, _ in self.sent], ["Claude 使用量 週次レポート", "Claude 使用量"])
        self.assertEqual(load_state(self.config.state_path)["pending_notices"], [])

    def test_pdf_failure_is_logged_and_notified_once(self):
        self.config.pdf_helper_path.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
        future = time.time() + 3600
        os.utime(self.config.pdf_helper_path, (future, future))
        self.write_history(jst(2026, 10, 27, 19, 20))  # 通知の判定に関係しないよう、記録は新しくしておく
        log = self.tick(jst(2026, 10, 27, 19, 30))
        self.assertTrue(any("レポートの作成に失敗" in l and "boom" in l for l in log))
        self.assertEqual(len(self.sent), 1)
        self.tick(jst(2026, 10, 27, 20, 0))
        self.assertEqual(len(self.sent), 1)  # 同じ失敗の通知は繰り返さない


if __name__ == "__main__":
    unittest.main()
