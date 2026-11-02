import copy
from dataclasses import replace
import json
import os
import stat
import tempfile
import time
import tomllib
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_usage.config import (EXAMPLE_CONFIG_PATH, ClaudeCodeOptions, FactorOptions, NonCodeOptions, Observation,
                                 Price, Weights, parse_config)
from claude_usage.metrics import SourceComparison, model_factor, weighted
from claude_usage.model import (FROM_DEFAULT, FROM_OBSERVED, FROM_RECORDS, Axis, LimitPoint, ModelFactor, NonCodeUsage,
                                 ProjectUsage, WeekReport, WindowBucket)
from claude_usage.sources import LimitSample, UsageEvent, UsageLog
from claude_usage.state import load_state, save_state, week_key
from claude_usage.weekly import (Created, comparison_notes, consolidation_note, consolidation_streak, make_reports,
                                 non_code_usage, price_notes, prices_to_state, report_axes, select_weeks,
                                 source_comparison, source_notes, stored_prices, summary_message, unmet_conditions,
                                 week_ending_on, week_factor, week_notes, week_weights)
from claude_usage.weeks import Week

JST = ZoneInfo("Asia/Tokyo")
ANCHOR = datetime(2026, 10, 27, 19, 0, tzinfo=JST)
CYCLE = timedelta(days=7)


def jst(*args):
    return datetime(*args, tzinfo=JST)


def week_ending(month, day):
    end = jst(2026, month, day, 19, 0)
    return Week(end - CYCLE, end)


class SelectWeeksTest(unittest.TestCase):
    WEEKS = [week_ending(10, 6), week_ending(10, 13), week_ending(10, 20)]

    def test_pending_skips_done(self):
        done = {week_key(self.WEEKS[0].end): {}}
        self.assertEqual(select_weeks(self.WEEKS, done, "pending", None, JST), self.WEEKS[1:])

    def test_all_and_week(self):
        done = {week_key(w.end): {} for w in self.WEEKS}
        self.assertEqual(select_weeks(self.WEEKS, done, "all", None, JST), self.WEEKS)
        self.assertEqual(select_weeks(self.WEEKS, done, "week", date(2026, 10, 13), JST), [self.WEEKS[1]])


class WeekEndingOnTest(unittest.TestCase):
    def test_reset_day(self):
        self.assertEqual(week_ending_on(date(2026, 10, 20), JST, ANCHOR, CYCLE), week_ending(10, 20))

    def test_other_day(self):
        with self.assertRaisesRegex(ValueError, "終わる週はありません"):
            week_ending_on(date(2026, 10, 21), JST, ANCHOR, CYCLE)


class WeekNotesTest(unittest.TestCase):
    def test_reset_and_missing_records(self):
        week = week_ending(10, 6)
        samples = [LimitSample(jst(2026, 10, 4, 14, 35), None, 66.0), LimitSample(jst(2026, 10, 4, 14, 42), 0.0, 0.0)]
        notes = week_notes(week, samples, JST, None)
        self.assertIn("10/4 日 14:35〜10/4 日 14:42 の間に週次の使用率が 66% → 0% に下がりました(手動リセットなど)。", notes)
        self.assertFalse(any("記録がありません" in n for n in notes))
        self.assertTrue(any("記録がありません" in n for n in week_notes(week_ending(10, 13), samples, JST, None)))
        self.assertTrue(any("読み込めませんでした(壊れている)" in n for n in week_notes(week, [], JST, "壊れている")))
        # 仕様として常に同じ説明(5 時間枠・週の区切りの決め方)は載せない
        self.assertFalse(any("5 時間枠の区切り" in n for n in notes))


class NonCodeUsageTest(unittest.TestCase):
    WEEK = week_ending(10, 20)
    OPTIONS = NonCodeOptions(max_gap=timedelta(minutes=45), min_known_pct=80, estimate_name="Cowork など",
                             weeks={date(2026, 10, 20): {"Cowork": 40.0, "チャット": 5.0}})

    def samples(self, *minutes_pct):
        return [LimitSample(self.WEEK.start + timedelta(minutes=m), None, p) for m, p in minutes_pct]

    def test_manual_value_wins(self):
        samples = self.samples((15, 10), (30, 30))
        self.assertEqual(non_code_usage(self.WEEK, samples, [], self.OPTIONS, JST),
                         [NonCodeUsage("Cowork", 40.0, False), NonCodeUsage("チャット", 5.0, False)])

    def test_product_shares_from_the_screen_win(self):
        observations = [
            Observation(self.WEEK.start + timedelta(hours=1), 30.0, {}, {"Claude Code": 70.0, "Cowork": 30.0}),
            Observation(self.WEEK.start + timedelta(hours=5), 50.0, {}, {"Claude Code": 82.0, "チャット": 6.0,
                                                                       "Cowork": 12.0, "その他": 0.0}),
            Observation(self.WEEK.end + timedelta(hours=1), 9.0, {}, {"Claude Code": 50.0, "Cowork": 50.0}),  # 次の週
        ]
        self.assertEqual(non_code_usage(self.WEEK, [], [], self.OPTIONS, JST, observations),  # 手で入れた値より優先
                         [NonCodeUsage("チャット", 6.0, False), NonCodeUsage("Cowork", 12.0, False)])

    def test_estimate_when_no_manual_value(self):
        week = week_ending(10, 27)
        samples = [LimitSample(week.start + timedelta(minutes=m), None, p) for m, p in ((15, 10), (30, 40))]
        code_at = [week.start + timedelta(minutes=5)]  # 最初の区間だけ Claude Code を使った
        self.assertEqual(non_code_usage(week, samples, code_at, self.OPTIONS, JST),
                         [NonCodeUsage("Cowork など", 75.0, True)])
        self.assertEqual(non_code_usage(week, [], code_at, self.OPTIONS, JST), [])  # 記録なし


class WeekFactorTest(unittest.TestCase):
    WEEK = week_ending(10, 27)
    OPTIONS = FactorOptions(model="claude-fable", max_gap=timedelta(minutes=45), min_intervals=4, min_model_intervals=2,
                            min_factor=1.0, max_factor=10.0, model_limit_share=50.0)
    UNIT = Price(1e6, 1.25e6, 2e6, 1e5, 5e6)  # 1 トークン 1 ドル(従来の重みと同じ数)
    WEIGHTS = Weights({"claude": UNIT}, model_factors={"claude-fable": ((jst(2026, 6, 24), 4.0),)})

    def event(self, minutes, model, output):
        return UsageEvent(self.WEEK.start + timedelta(minutes=minutes), "s", "/r", model, False, 0, 0, 0, 0, output)

    def history(self, fable_rate, other_rate):
        """15 分ごとの区間で Fable と Opus を交互に使い、その分だけ週次%が増える記録を作る。"""
        events, samples, pct = [], [], 0.0
        for i, (model, out) in enumerate([("claude-fable-5-1", 2), ("claude-opus-5-5", 8), ("claude-fable-5-1", 4),
                                          ("claude-opus-5-5", 4), ("claude-fable-5-1", 1)]):
            events.append(self.event(15 * i + 5, model, out))
            pct += out * 5 * (fable_rate if "fable" in model else other_rate)
            samples.append(LimitSample(self.WEEK.start + timedelta(minutes=15 * (i + 1)), None, pct))
        return events, samples

    def test_estimated_from_records_and_used_for_the_week(self):
        events, samples = self.history(fable_rate=0.3, other_rate=0.1)
        factor = week_factor(self.WEEK, samples, events, self.WEIGHTS, self.OPTIONS)
        self.assertEqual(factor.source, FROM_RECORDS)
        self.assertAlmostEqual(factor.value, 3.0)
        self.assertEqual(factor.intervals, 5)
        w = week_weights(self.WEIGHTS, factor, self.WEEK)
        self.assertAlmostEqual(weighted(self.event(30, "claude-fable-5-1", 1), w), 15.0)  # 1 × 5 × 推定値 3
        self.assertEqual(w.model_factors["claude-fable"][0][0], self.WEEK.start)

    def test_from_observed_values_when_records_are_not_enough(self):
        events, _ = self.history(fable_rate=0.3, other_rate=0.1)
        # Fable 7×5=35、Opus 12×5=60(倍率なし)。画面: 全モデル 50%・Fable 60%(= 30pt)、Claude Code 以外 10%
        obs = Observation(self.WEEK.start + timedelta(hours=2), 50.0, {"Fable": 60.0})
        factor = week_factor(self.WEEK, [], events, self.WEIGHTS, self.OPTIONS, (obs, "Fable"), non_code_pct=10.0)
        self.assertEqual(factor.source, FROM_OBSERVED)
        self.assertAlmostEqual(factor.value, (30 / 35) / ((50 * 0.9 - 30) / 60))
        # Claude Code 以外の割合が分からない週・手動リセットのある週は計算しない(既定値)
        self.assertEqual(week_factor(self.WEEK, [], events, self.WEIGHTS, self.OPTIONS, (obs, "Fable")).source,
                         FROM_DEFAULT)
        reset = [LimitSample(self.WEEK.start + timedelta(hours=1), None, 40.0),
                 LimitSample(self.WEEK.start + timedelta(hours=1, minutes=5), None, 0.0)]
        self.assertEqual(week_factor(self.WEEK, reset, events, self.WEIGHTS, self.OPTIONS, (obs, "Fable"), 10.0).source,
                         FROM_DEFAULT)

    def test_screen_values_with_product_shares_come_before_records(self):
        events, samples = self.history(fable_rate=0.3, other_rate=0.1)  # 記録からは 3.0 と推定できる
        obs = Observation(self.WEEK.start + timedelta(hours=2), 50.0, {"Fable": 60.0},
                          {"Claude Code": 90.0, "チャット": 4.0, "Cowork": 6.0})
        factor = week_factor(self.WEEK, samples, events, self.WEIGHTS, self.OPTIONS, (obs, "Fable"))
        self.assertEqual(factor.source, FROM_OBSERVED)
        self.assertAlmostEqual(factor.value, (30 / 35) / ((50 * 0.9 - 30) / 60))  # Claude Code 以外 10%(チャットを含む)
        # 製品別の割合がない画面の値は、記録からの推定の後
        plain = Observation(obs.ts, 50.0, {"Fable": 60.0})
        self.assertEqual(week_factor(self.WEEK, samples, events, self.WEIGHTS, self.OPTIONS, (plain, "Fable"), 10.0).source,
                         FROM_RECORDS)

    def test_default_at_a_boundary_on_the_reset_uses_the_week_it_belongs_to(self):
        # 倍率の期間の境目が週の終わり(リセット時刻)ちょうどなら、その週は境目の前の倍率
        weights = Weights({"claude": self.UNIT}, model_factors={"claude-fable": (
            (jst(2026, 6, 9), 2.0), (self.WEEK.end, 4.0))})
        self.assertEqual(week_factor(self.WEEK, [], [], weights, self.OPTIONS).value, 2.0)
        self.assertEqual(model_factor("claude-fable-5", self.WEEK.end - timedelta(seconds=1),
                                      weights.model_factors), 2.0)  # 週の中の応答の倍率と一致

    def test_default_when_records_are_not_enough(self):
        events, samples = self.history(fable_rate=0.3, other_rate=0.1)
        factor = week_factor(self.WEEK, samples[:2], events, self.WEIGHTS, self.OPTIONS)
        self.assertEqual(factor, ModelFactor("claude-fable", 4.0, FROM_DEFAULT, intervals=2))
        self.assertIs(week_weights(self.WEIGHTS, factor, self.WEEK), self.WEIGHTS)


class PriceNotesTest(unittest.TestCase):
    OPUS55 = Price(4.0, 5.0, 8.0, 0.2, 20.0)
    WEIGHTS = Weights({"claude": OPUS55, "claude-opus": OPUS55, "claude-opus-5-5": OPUS55})

    @staticmethod
    def event(model):
        return UsageEvent(jst(2026, 10, 21, 10, 0), "s", "/r", model, False, 0, 0, 0, 0, 1)

    def test_notes_for_learned_and_family_prices(self):
        learned = Price(6.0, 7.5, 12.0, 0.3, 30.0)
        w = replace(self.WEIGHTS, learned={"claude-sonnet-6": ((jst(2026, 10, 1), learned),)})
        notes = price_notes([self.event(m) for m in ("claude-opus-5-5", "claude-opus-6", "claude-sonnet-6")], w)
        self.assertEqual(notes, [
            "claude-opus-6 は単価表にないため、claude-opus の単価で計算しました: 入力 $4・出力 $20・キャッシュ読み込み $0.2"
            "(100 万トークンあたり)。",
            "claude-sonnet-6 の単価は Claude Code の費用の記録から推定しました: 入力 $6・出力 $30・キャッシュ読み込み $0.3"
            "(100 万トークンあたり)。",
        ])

    def test_state_roundtrip(self):
        learned = {"claude-opus-6": ((jst(2026, 10, 1), Price(6.0, 7.5, 12.0, 0.3, 30.0)),)}
        state = json.loads(json.dumps({"prices": prices_to_state(learned)}))
        self.assertEqual(stored_prices(state), learned)
        self.assertEqual(stored_prices({"prices": {"x": "壊れた値"}}), {})


class SummaryMessageTest(unittest.TestCase):
    def test_single_and_many(self):
        w = week_ending(10, 20)
        r = WeekReport(w.start, w.end, [], [], [ProjectUsage("alpha", 75, 1, 0), ProjectUsage("beta", 25, 1, 0)])
        one = Created(w, Path("/x.pdf"), r)
        self.assertEqual(summary_message([one]), "週次上限 記録なし / 最多 alpha 75%")
        self.assertEqual(summary_message([one, one]), "2 週分のレポートを保存しました。")


class StateTest(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "state.json"
            self.assertEqual(load_state(path), {"weeks": {}, "slots": {}})
            save_state(path, {"weeks": {"2026-10-20T10:00:00Z": {"final_pct": 69}}, "slots": {"alpha": 0}})
            self.assertEqual(load_state(path)["slots"], {"alpha": 0})
        self.assertEqual(week_key(jst(2026, 10, 20, 19, 0)), "2026-10-20T10:00:00Z")


class ComparisonNotesTest(unittest.TestCase):
    def comparison(self, mismatches=(), attempts=48, failures=1):
        t = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
        return SourceComparison(compared=40, consistent=40 - len(mismatches), same=38,
                                mismatches=[(t, 44.0 + i, 45.0, None) for i in range(len(mismatches))],
                                desktop_hours=60, usage_hours=90, usage_only_hours=35, attempts=attempts,
                                failures=failures)

    def test_summary(self):
        notes = comparison_notes(self.comparison(), timedelta(minutes=20), JST)
        self.assertEqual(notes, [
            "週次%の 2 つの記録の照合(デスクトップアプリ・Claude Code の /usage): 前後 20 分以内にデスクトップの記録がある"
            " /usage の記録 40 件のうち、前後の値の間に収まったもの 40 件(同じ値 38 件)。",
            "記録のある時間: デスクトップ 60 時間・/usage 90 時間(/usage だけ 35 時間)。/usage の取得 48 回のうち失敗 1 回。"])

    def test_mismatches_are_listed_up_to_three(self):
        notes = comparison_notes(self.comparison(mismatches=range(5)), timedelta(minutes=20), JST)
        self.assertEqual(notes[-1], "値が前後に収まらなかった /usage の記録: 10/29 木 12:00 44%(前 45%・後 --)、"
                                    "10/29 木 12:00 45%(前 45%・後 --)、10/29 木 12:00 46%(前 45%・後 --) ほか 2 件。")

    def test_nothing_before_the_usage_records_start(self):
        self.assertEqual(comparison_notes(self.comparison(attempts=0, failures=0), timedelta(minutes=20), JST), [])


class ConsolidationTest(unittest.TestCase):
    """一本化の目安: 照合 30 件以上・食い違い 0 件・失敗 5% 以下・/usage の時間 ≧ デスクトップ を 2 週続けて満たす。"""

    OPTIONS = ClaudeCodeOptions(True, ("claude",), 30.0, timedelta(minutes=20), consolidate_weeks=2,
                                consolidate_min_compared=30, consolidate_max_failure_pct=5.0)
    END = jst(2026, 11, 10, 19, 0)
    CYCLE = timedelta(days=7)

    @staticmethod
    def comparison(compared=69, mismatches=(), desktop_hours=47, usage_hours=97, attempts=183, failures=2):
        return SourceComparison(compared=compared, consistent=compared - len(mismatches), same=63,
                                mismatches=list(mismatches), desktop_hours=desktop_hours, usage_hours=usage_hours,
                                usage_only_hours=54, attempts=attempts, failures=failures)

    def test_all_conditions_met(self):
        self.assertEqual(unmet_conditions(self.comparison(), self.OPTIONS), [])
        self.assertEqual(unmet_conditions(self.comparison(attempts=100, failures=5), self.OPTIONS), [])  # ちょうど 5%

    def test_each_condition(self):
        t = jst(2026, 11, 1, 12, 0)
        cases = [(self.comparison(compared=29), "照合できた記録が 29 件で 30 件に足りない"),
                 (self.comparison(mismatches=[(t, 40.0, 41.0, None)]), "値が前後に収まらなかった記録が 1 件ある"),
                 (self.comparison(attempts=100, failures=6), "取得の失敗が 6% ある"),
                 (self.comparison(desktop_hours=98), "記録のある時間が /usage 97 時間で、デスクトップの 98 時間より短い")]
        for c, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(unmet_conditions(c, self.OPTIONS), [reason])

    def test_streak_stops_at_the_first_week_that_fails(self):
        weeks = [Week(self.END - self.CYCLE * (i + 1), self.END - self.CYCLE * i) for i in range(3)]
        good = {weeks[0], weeks[1]}
        compare = lambda w: self.comparison() if w in good else self.comparison(compared=0)  # noqa: E731
        self.assertEqual(consolidation_streak(weeks[0], compare, self.OPTIONS, self.CYCLE), 2)
        self.assertEqual(consolidation_streak(weeks[2], compare, self.OPTIONS, self.CYCLE), 0)

    def test_notes(self):
        self.assertEqual(consolidation_note(["理由 1", "理由 2"], 0, 2, True),
                         "一本化の目安: この週は満たしていません(理由 1・理由 2)。")
        self.assertEqual(consolidation_note([], 1, 2, True), "一本化の目安: この週は満たしました(あと 1 週)。")
        self.assertEqual(consolidation_note([], 2, 2, True),
                         "一本化の目安: 2 週続けて満たしました。/usage の記録に一本化できます。")
        self.assertEqual(consolidation_note([], 1, 2, False),
                         "一本化の目安: ここまでは満たしています(週が終わるまで確定しません)。")

    def test_source_notes_from_records(self):
        week = Week(self.END - self.CYCLE, self.END)
        times = [week.start + timedelta(minutes=30 * i) for i in range(1, 31)]
        desktop = [LimitSample(t + timedelta(minutes=5), None, float(i)) for i, t in enumerate(times)]
        usage = [LimitSample(t, None, float(i)) for i, t in enumerate(times)]
        notes = source_notes(week, desktop, UsageLog(usage, [], []), self.OPTIONS, self.CYCLE, JST)
        self.assertEqual(notes[-1], "一本化の目安: この週は満たしました(あと 1 週)。")
        self.assertEqual(source_notes(week, desktop, UsageLog([], [], []), self.OPTIONS, self.CYCLE, JST), [])


class MakeReportsTest(unittest.TestCase):
    """取得元からレポート作成・state.json の更新までを通す(PDF 変換は偽のヘルパーで代用)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        with EXAMPLE_CONFIG_PATH.open("rb") as fh:
            table = copy.deepcopy(tomllib.load(fh))
        table["paths"].update(transcripts_root=str(root / "projects"), plan_usage_history=str(root / "history.json"),
                              data_dir=str(root / "data"), demo_dir=str(root / "demo"), pdf_dir=str(root / "pdf"))
        # 設定ファイルの例にある利用者の実データ向けの値(モデルの倍率・手で入れた週の値・画面の値)に左右されないよう、
        # 空にする。これらを使う場合は test_model_factor_and_manual_non_code・test_observed_values で個別に確かめる
        table["metric"]["model_factors"] = {}
        table["non_code"]["weeks"] = {}
        table["observed"]["points"] = {}
        table["metric"]["prices"] = {"claude": [1e6, 1.25e6, 2e6, 1e5, 5e6]}  # 1 トークン 1 ドル(数を追いやすく)
        self.table = table
        self.config = parse_config(table)
        repo = root / "GitHub" / "alpha"
        (repo / ".git").mkdir(parents=True)
        (root / "projects" / "p").mkdir(parents=True)
        lines = [self.line("m1", jst(2026, 10, 14, 10, 0), str(repo)),
                 self.line("m2", jst(2026, 10, 21, 10, 0), str(repo), model="claude-fable-5-1")]
        (root / "projects" / "p" / "s.jsonl").write_text("\n".join(lines) + "\n")
        (root / "history.json").write_text(json.dumps({"version": 2, "samples": [
            {"t": int(jst(2026, 10, 14, 11, 0).timestamp() * 1000), "org": "o", "u": {"fh": 40, "sd": 20}},
            {"t": int(jst(2026, 10, 21, 11, 0).timestamp() * 1000), "org": "o", "u": {"fh": 10, "sd": 5}},
        ]}))
        helper = self.config.pdf_helper_path
        helper.parent.mkdir(parents=True)
        helper.write_text('#!/bin/sh\nprintf "%%PDF fake" > "$2"\n')
        helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
        future = time.time() + 3600
        os.utime(helper, (future, future))  # ビルド済みとみなさせる

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def line(msg_id, ts, cwd, model="claude-opus-5"):
        return json.dumps({"type": "assistant", "timestamp": ts.astimezone(timezone.utc).isoformat(), "sessionId": "s",
                           "cwd": cwd, "requestId": msg_id, "message": {"id": msg_id, "model": model,
                                                                         "usage": {"input_tokens": 1, "output_tokens": 2}}})

    def test_pending_then_nothing_left(self):
        now = jst(2026, 10, 23, 15, 0)
        created = make_reports(self.config, "pending", now=now)
        self.assertEqual([c.pdf.name for c in created], ["Claude_Usage_2026-10-20.pdf"])
        self.assertTrue(created[0].pdf.read_text().startswith("%PDF"))
        report = created[0].report
        self.assertEqual([p.name for p in report.projects], ["alpha"])
        self.assertEqual(report.final_pct, 20)
        self.assertIsNone(report.prev_final_pct)
        state = load_state(self.config.state_path)
        self.assertEqual(state["weeks"]["2026-10-20T10:00:00Z"]["final_pct"], 20)
        self.assertEqual(state["slots"], {"alpha": 0})
        self.assertEqual(make_reports(self.config, "pending", now=now), [])

    def test_week_mode_uses_previous_final_from_state(self):
        now = jst(2026, 10, 30, 0, 0)
        big_axis = {"week_total": 1e12, "window": 1e12, "models": {"Fable": 1e12}}  # 保存済みの上限のほうが大きい
        save_state(self.config.state_path, {"weeks": {"2026-10-20T10:00:00Z": {"final_pct": 55}}, "slots": {},
                                            "axis": big_axis})
        created = make_reports(self.config, "week", date(2026, 10, 27), now=now)
        self.assertEqual([c.pdf.name for c in created], ["Claude_Usage_2026-10-27.pdf"])
        self.assertEqual(created[0].report.prev_final_pct, 20)   # 前の週の記録が履歴にあればそれを使う
        self.assertEqual(created[0].report.model_total("Fable"), 1 + 2 * 5)
        self.assertEqual(load_state(self.config.state_path)["axis"], big_axis)

    def test_new_maximum_raises_axis_only_from_that_week(self):
        make_reports(self.config, "pending", now=jst(2026, 10, 23, 15, 0))  # 10/20 の週だけ
        self.assertEqual(load_state(self.config.state_path)["axis"],
                         {"week_total": 11.0, "window": 11.0, "models": {"Fable": 0.0}})
        before = (Path(self.tmp.name) / "pdf" / "Claude_Usage_2026-10-20.pdf").stat().st_mtime_ns
        # 10/27 の週に出力 100 トークン(重み付き 500)の応答を足し、これまでの最大値(11)を超えさせる
        path = Path(self.tmp.name) / "projects" / "p" / "s.jsonl"
        path.write_text(path.read_text() + json.dumps({
            "type": "assistant", "timestamp": jst(2026, 10, 22, 10, 0).astimezone(timezone.utc).isoformat(),
            "sessionId": "s", "cwd": "/x", "requestId": "m3",
            "message": {"id": "m3", "model": "claude-opus-5", "usage": {"input_tokens": 0, "output_tokens": 100}}}) + "\n")
        created = make_reports(self.config, "pending", now=jst(2026, 10, 30, 0, 0))
        self.assertEqual([c.pdf.name for c in created], ["Claude_Usage_2026-10-27.pdf"])  # 作成済みの週は作り直さない
        self.assertEqual((Path(self.tmp.name) / "pdf" / "Claude_Usage_2026-10-20.pdf").stat().st_mtime_ns, before)
        # 足した応答と Fable の応答は別の 5 時間枠なので、5 時間枠の最大は 500
        self.assertEqual(load_state(self.config.state_path)["axis"],
                         {"week_total": 511.0, "window": 500.0, "models": {"Fable": 11.0}})

    def test_model_factor_and_manual_non_code(self):
        self.table["metric"]["model_factors"] = {"claude-fable": [{"from": "2026-06-24T14:39:56Z", "factor": 4.0}]}
        self.table["non_code"]["weeks"] = {"2026-10-27": {"Cowork": 40}}
        created = make_reports(parse_config(self.table), "pending", now=jst(2026, 10, 30, 0, 0))
        by_pdf = {c.pdf.name: c.report for c in created}
        r = by_pdf["Claude_Usage_2026-10-27.pdf"]
        self.assertEqual(r.model_total("Fable"), (1 + 2 * 5) * 4)
        self.assertEqual(r.factor, ModelFactor("claude-fable", 4.0, FROM_DEFAULT))  # 記録が足りず既定値
        self.assertEqual(r.non_code, [NonCodeUsage("Cowork", 40.0, estimated=False)])
        # 10/20 の週は手で入れた値がなく、使用率の記録が 1 点だけで推定もできない
        self.assertEqual(by_pdf["Claude_Usage_2026-10-20.pdf"].non_code, [])

    def test_price_of_a_new_model_is_learned_from_cost_records(self):
        # 単価表にないモデルの応答と、Claude Code の費用の記録(単価: 入力 $3・出力 $15・読み込み $0.3・書き込み 1 時間 $6)
        increments = [(2000, 400_000, 20_000), (8000, 900_000, 5000), (500, 2_000_000, 1000)]  # 出力・読み込み・書き込み
        lines, out, read, write = [], 0, 0, 0
        for i, (d_out, d_read, d_write) in enumerate(increments):
            out, read, write = out + d_out, read + d_read, write + d_write  # cost-state は累計
            lines.append(self.line(f"n{i}", jst(2026, 10, 21, 10 + i, 0), "/x", model="claude-sonnet-9"))
            lines.append(json.dumps({"type": "cost-state", "modelUsage": {"claude-sonnet-9": {
                "inputTokens": 0, "outputTokens": out, "cacheReadInputTokens": read, "cacheCreationInputTokens": write,
                "costUSD": (out * 15 + read * 0.3 + write * 6) / 1e6}}}))
        (Path(self.tmp.name) / "projects" / "p" / "s2.jsonl").write_text("\n".join(lines) + "\n")
        created = make_reports(self.config, "pending", now=jst(2026, 10, 30, 0, 0))
        r = {c.pdf.name: c.report for c in created}["Claude_Usage_2026-10-27.pdf"]
        self.assertTrue(any(n.startswith("claude-sonnet-9 の単価は Claude Code の費用の記録から推定しました: 入力 $3・出力 $15")
                            for n in r.notes))
        saved = load_state(self.config.state_path)["prices"]["claude-sonnet-9"]
        self.assertEqual(len(saved), 1)
        self.assertAlmostEqual(saved[0][1][4], 15.0, places=6)  # 出力の単価

    def test_observed_values(self):
        self.table["observed"]["points"] = {
            "2026-10-14T10:30:00+09:00": {"all": 5},                 # 記録(10/14 11:00 の 20%)より前なので使わない
            "2026-10-20T18:00:00+09:00": {"all": 88, "Fable": 70},  # 記録の後: 週の最後の値になる
            "2026-10-10T12:00:00+09:00": {"all": 30, "Fable": 10},  # 記録も会話ログもない週を補う
        }
        created = make_reports(parse_config(self.table), "pending", now=jst(2026, 10, 23, 15, 0))
        by_pdf = {c.pdf.name: c.report for c in created}
        r = by_pdf["Claude_Usage_2026-10-20.pdf"]
        self.assertEqual([p.pct for p in r.limit_points], [20.0, 88.0])
        self.assertEqual(r.final_pct, 88.0)
        self.assertEqual(r.model_limits, {"Fable": LimitPoint(jst(2026, 10, 20, 18, 0), 70.0)})
        self.assertEqual(r.prev_final_pct, 30.0)  # 前の週の値も画面の値から
        self.assertEqual(by_pdf["Claude_Usage_2026-10-13.pdf"].final_pct, 30.0)
        self.assertEqual(load_state(self.config.state_path)["weeks"]["2026-10-20T10:00:00Z"]["final_pct"], 88.0)


    def test_limit_records_survive_the_apps_30_day_deletion(self):
        history = Path(self.tmp.name) / "history.json"
        make_reports(self.config, "pending", now=jst(2026, 10, 16, 15, 0))  # 週はまだ終わっていないが、記録はコピーする
        self.assertTrue(self.config.limit_archive_path.exists())
        history.write_text(json.dumps({"version": 2, "samples": [  # アプリが 10/14 の記録を消した後
            {"t": int(jst(2026, 10, 21, 11, 0).timestamp() * 1000), "org": "o", "u": {"fh": 10, "sd": 5}}]}))
        by_pdf = {c.pdf.name: c.report for c in make_reports(self.config, "pending", now=jst(2026, 10, 23, 15, 0))}
        self.assertEqual(by_pdf["Claude_Usage_2026-10-20.pdf"].final_pct, 20)  # コピーに残った記録

    def test_claude_code_usage_is_used_like_screen_values(self):
        # 2026-10-28 ユーザーの指定: /usage の記録の全体%は使用率の記録に足し、モデル別の%・製品別の内訳は画面の値と同じに使う
        root = Path(self.tmp.name)
        transcript = root / "projects" / "p" / "s.jsonl"
        transcript.write_text(transcript.read_text()
                              + self.line("m3", jst(2026, 10, 15, 10, 0), str(root / "GitHub" / "alpha"),
                                          model="claude-fable-5-1") + "\n")
        log = self.config.usage_log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(json.dumps(r) for r in [
            {"t": jst(2026, 10, 15, 11, 0).isoformat(), "all": 30, "five_hour": 50, "models": {"Fable": 8},
             "products": {}},
            {"t": jst(2026, 10, 16, 9, 0).isoformat(), "all": 40, "five_hour": 0, "models": {"Fable": 40},
             "products": {"claude_code": 90, "chat": 6, "cowork": 4, "other": 0}},
        ]) + "\n")
        self.table["observed"]["points"] = {"2026-10-20T18:00:00+09:00": {"all": 88, "Fable": 70}}
        r = {c.pdf.name: c.report for c in make_reports(parse_config(self.table), "pending",
                                                        now=jst(2026, 10, 23, 15, 0))}["Claude_Usage_2026-10-20.pdf"]
        # 全体%: デスクトップアプリの記録(10/14 20%)+ /usage の記録 + その後の画面の値
        self.assertEqual([p.pct for p in r.limit_points], [20.0, 30.0, 40.0, 88.0])
        # モデル別の%: /usage の記録と画面の値を時刻順に
        self.assertEqual(r.model_limit_points, {"Fable": [LimitPoint(jst(2026, 10, 15, 11, 0), 8.0),
                                                          LimitPoint(jst(2026, 10, 16, 9, 0), 40.0),
                                                          LimitPoint(jst(2026, 10, 20, 18, 0), 70.0)]})
        # Claude Code 以外: 製品別の内訳のある最後の値(割合 0 は除く)
        self.assertEqual(r.non_code, [NonCodeUsage("チャット", 6.0, estimated=False),
                                      NonCodeUsage("Cowork", 4.0, estimated=False)])
        # 倍率: 製品別の内訳のある 10/16 の値から。Fable の分 = 40 × 50% = 20pt、それ以外 = 40 × 90% − 20 = 16pt、
        # それまでの API 換算額は Fable・それ以外ともに $11 なので (20 / 11) ÷ (16 / 11) = 1.25
        self.assertEqual(r.factor.source, FROM_OBSERVED)
        self.assertAlmostEqual(r.factor.value, 1.25)
        # 5 時間枠の使用率にも使う(10/14 10:00 の枠はデスクトップの 40%、10/15 10:00 の枠は /usage の 50%)
        self.assertEqual([w.five_hour_pct for w in r.windows], [40.0, 50.0])
        # 2 つの記録の照合を注記に載せる(この週の /usage の記録の前後 20 分にはデスクトップの記録がない)
        self.assertIn("記録のある時間: デスクトップ 1 時間・/usage 2 時間(/usage だけ 2 時間)。/usage の取得 2 回のうち失敗 0 回。",
                      r.notes)

    def test_source_comparison_for_any_week(self):
        log = self.config.usage_log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(json.dumps(r) for r in [
            {"t": jst(2026, 10, 14, 11, 10).isoformat(), "all": 20, "models": {}, "products": {}},
            {"t": jst(2026, 10, 14, 11, 40).isoformat(), "error": "30 秒以内に応答がありませんでした"},
        ]) + "\n")
        week = week_ending_on(date(2026, 10, 20), JST, self.config.reset_anchor, self.config.cycle)
        lines = source_comparison(self.config, week, jst(2026, 10, 23, 15, 0))
        self.assertIn("/usage の記録 1 件のうち、前後の値の間に収まったもの 1 件(同じ値 1 件)", lines[0])
        self.assertIn("/usage の取得 2 回のうち失敗 1 回", lines[1])
        self.assertIn("一本化の目安: この週は満たしていません(", lines[2])
        self.assertIn("照合できた記録が 1 件で 30 件に足りない", lines[2])
        other = week_ending_on(date(2026, 10, 27), JST, self.config.reset_anchor, self.config.cycle)
        self.assertEqual(source_comparison(self.config, other, jst(2026, 10, 23, 15, 0)),
                         ["この週は Claude Code の /usage の記録がありません。"])

    def test_dev_time_uses_the_configured_gap(self):
        transcript = Path(self.tmp.name) / "projects" / "p" / "s.jsonl"
        repo = str(Path(self.tmp.name) / "GitHub" / "alpha")
        transcript.write_text(transcript.read_text() + self.line("m3", jst(2026, 10, 14, 10, 12), repo) + "\n"
                              + self.line("m4", jst(2026, 10, 14, 11, 0), repo) + "\n")
        r = make_reports(self.config, "pending", now=jst(2026, 10, 23, 15, 0))[0].report
        self.assertEqual(r.dev_time, timedelta(minutes=12))  # 15 分以内の間隔だけ足す
        self.assertEqual([p.dev_time for p in r.projects], [timedelta(minutes=12)])

    def test_no_data(self):
        (Path(self.tmp.name) / "projects" / "p" / "s.jsonl").unlink()
        (Path(self.tmp.name) / "history.json").unlink()
        self.assertEqual(make_reports(self.config, "pending", now=jst(2026, 10, 23, 0, 0)), [])


class ReportAxesTest(unittest.TestCase):
    @staticmethod
    def week_report(total, fable):
        w = week_ending(10, 20)
        return WeekReport(w.start, w.end, [], [WindowBucket(w.start, w.start + timedelta(hours=5), {"a": total},
                                                            by_model={"Fable": fable})],
                          [ProjectUsage("a", total, 1, 0)])

    def test_all_mode_uses_one_shared_maximum(self):
        reports = [self.week_report(10, 4), self.week_report(30, 2), self.week_report(20, 9)]
        axes = report_axes(reports, ["Fable"], "all", Axis(1000, 1000, {"Fable": 1000}))  # 保存済みの上限は使わない
        self.assertEqual(axes, [Axis(30, 30, {"Fable": 9})] * 3)

    def test_pending_raises_the_maximum_in_order(self):
        reports = [self.week_report(10, 4), self.week_report(30, 2), self.week_report(20, 9)]
        axes = report_axes(reports, ["Fable"], "pending", Axis(15, 15, {"Fable": 5}))
        self.assertEqual([(a.week_total, a.models["Fable"]) for a in axes], [(15, 5), (30, 5), (30, 9)])


if __name__ == "__main__":
    unittest.main()
