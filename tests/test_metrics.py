import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from claude_usage.config import Observation, Price, Weights
from claude_usage.metrics import (
    LEARNED,
    OTHERS,
    active_time,
    api_cost,
    assign_slots,
    build_week_report,
    compare_sources,
    estimate_factor,
    estimate_non_code_pct,
    factor_intervals,
    fit_price,
    fit_rates,
    fold_to_slices,
    group_dev_time,
    group_projects,
    infer_windows,
    model_limit_points,
    learn_prices,
    model_factor,
    model_group,
    observed_factor,
    MATCH,
    MISS,
    NEAR,
    price_match,
    price_of,
    split_increments,
    split_tokens,
    weighted,
    with_non_code,
    with_observations,
)
from claude_usage.model import LimitPoint, NonCodeUsage, ProjectUsage
from claude_usage.sources import CostRecord, LimitSample, UsageEvent
from claude_usage.weeks import Week

# 100 万トークンあたり 100 万ドル = 1 トークン 1 ドル。API 換算額が従来の重み(1・1.25・2・0.1・5)と同じ数になる
UNIT = Price(input=1e6, cache_write_5m=1.25e6, cache_write_1h=2e6, cache_read=1e5, output=5e6)
W = Weights(prices={"claude": UNIT})
START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)  # 10/20 19:00 JST
WEEK = Week(START, START + timedelta(days=7))


def event(minutes, project_dir="/r/alpha", model="claude-opus-5", session="s1", sub=False, output=10, read=0):
    return UsageEvent(START + timedelta(minutes=minutes), session, project_dir, model, sub, 0, 0, 0, read, output)


class WeightedTest(unittest.TestCase):
    def test_weights(self):
        e = UsageEvent(START, "s", "/", "m", False, input_tokens=10, cache_write_5m=100, cache_write_1h=1000,
                       cache_read=10000, output_tokens=2)
        self.assertAlmostEqual(weighted(e, W), 10 + 125 + 2000 + 1000 + 10)

    def test_model_factor_by_period(self):
        withdrawn = datetime(2026, 6, 24, 14, 39, 56, tzinfo=timezone.utc)
        factors = {"claude-fable": ((datetime(2026, 6, 9, tzinfo=timezone.utc), 2.0), (withdrawn, 4.0))}
        self.assertEqual(model_factor("claude-fable-5", datetime(2026, 6, 1, tzinfo=timezone.utc), factors), 1.0)
        self.assertEqual(model_factor("claude-fable-5", withdrawn - timedelta(seconds=1), factors), 2.0)
        self.assertEqual(model_factor("claude-fable-5-1", withdrawn, factors), 4.0)   # 境界の時刻から新しい倍率
        self.assertEqual(model_factor("claude-opus-5", withdrawn, factors), 1.0)     # 当てはまらないモデル
        w = replace(W, model_factors=factors)
        self.assertEqual(weighted(event(0, model="claude-fable-5-1", output=10), w), 200)  # 10 × 5 × 4
        self.assertEqual(weighted(event(0, output=10), w), 50)


FABLE51 = Price(10.0, 12.5, 20.0, 0.25, 50.0)
OPUS55 = Price(4.0, 5.0, 8.0, 0.2, 20.0)
OPUS = Price(5.0, 6.25, 10.0, 0.5, 25.0)
PRICES = Weights(prices={"claude": OPUS55, "claude-opus": OPUS55, "claude-opus-5": OPUS, "claude-opus-5-5": OPUS55,
                         "claude-fable-5-1": FABLE51})


def cost_record(minutes, model, price, inp=0, out=0, read=0, write=0, write_1h=True):
    """単価 price のとおりに費用を付けた記録(書き込みは 1 時間か 5 分の単価)。"""
    cost = (inp * price.input + out * price.output + read * price.cache_read
            + write * (price.cache_write_1h if write_1h else price.cache_write_5m)) / 1e6
    return CostRecord(START + timedelta(minutes=minutes), model, inp, out, read, write, cost)


class PriceTest(unittest.TestCase):
    def test_price_of_longest_prefix_and_fallback(self):
        self.assertEqual(price_of("claude-opus-5-5", START, PRICES), (OPUS55, "claude-opus-5-5"))
        self.assertEqual(price_of("claude-opus-5", START, PRICES), (OPUS, "claude-opus-5"))
        self.assertEqual(price_of("claude-opus-6", START, PRICES), (OPUS55, "claude-opus"))  # 表にない Opus は系統の単価
        self.assertEqual(price_of("other-model", START, PRICES), (OPUS55, "claude"))

    def test_learned_price_takes_over_from_its_start(self):
        new = Price(3.0, 3.75, 6.0, 0.15, 15.0)
        w = replace(PRICES, learned={"claude-opus-5-5": ((START + timedelta(hours=1), new),)})
        self.assertEqual(price_of("claude-opus-5-5", START, w)[0], OPUS55)
        self.assertEqual(price_of("claude-opus-5-5", START + timedelta(hours=1), w), (new, LEARNED))

    def test_api_cost(self):
        e = UsageEvent(START, "s", "/", "claude-fable-5-1", False, input_tokens=1_000_000, cache_write_5m=1_000_000,
                       cache_write_1h=1_000_000, cache_read=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(api_cost(e, FABLE51), 10 + 12.5 + 20 + 0.25 + 50)
        self.assertAlmostEqual(weighted(e, replace(PRICES, model_factors={"claude-fable": ((START, 2.0),)})),
                               (10 + 12.5 + 20 + 0.25 + 50) * 2)

    def test_match_near_miss(self):
        r1 = cost_record(0, "m", OPUS55, out=20_000, read=1_000_000, write=10_000, write_1h=True)
        r5 = cost_record(0, "m", OPUS55, out=20_000, read=1_000_000, write=10_000, write_1h=False)
        self.assertEqual(price_match(r1, OPUS55, 0.03), MATCH)  # 書き込みを 1 時間として合う
        self.assertEqual(price_match(r5, OPUS55, 0.03), NEAR)   # 5 分の書き込みなら合う
        self.assertEqual(price_match(r1, OPUS, 0.03), MISS)
        heavy = cost_record(0, "m", OPUS55, out=1000, read=100_000, write=50_000)
        self.assertEqual(price_match(heavy, OPUS, 0.03), NEAR)  # 書き込みが多いと別の単価とも区別できない


class LearnPricesTest(unittest.TestCase):
    def records(self, model, price, start=0, n=4):
        mixes = [(100, 2_000, 400_000, 20_000), (50, 8_000, 900_000, 5_000), (10, 500, 2_000_000, 1_000),
                 (300, 4_000, 100_000, 60_000)]
        return [cost_record(start + 10 * i, model, price, *mixes[i % 4]) for i in range(n)]

    def test_fit_price_recovers_the_official_structure(self):
        price = fit_price(self.records("claude-fable-5-1", FABLE51))
        for got, want in zip(vars(price).values(), vars(FABLE51).values()):
            self.assertAlmostEqual(got, want, places=6)
        no_cache = [cost_record(i, "h", Price(1, 1.25, 2, 0.1, 5), inp=100 * (i + 1), out=50) for i in range(3)]
        self.assertAlmostEqual(fit_price(no_cache).input, 1.0)  # 読み込みがない記録だけでも入力の単価は求まる

    def test_new_model_and_price_change_are_learned(self):
        new_model = Price(6.0, 7.5, 12.0, 0.3, 30.0)
        records = self.records("claude-opus-6", new_model) + self.records("claude-opus-5-5", OPUS55)
        cut = Price(3.0, 3.75, 6.0, 0.15, 15.0)  # 値下げ
        records += self.records("claude-opus-5-5", cut, start=100)
        learned = learn_prices(records, PRICES, min_records=3, tolerance=0.03)
        self.assertEqual(set(learned), {"claude-opus-6", "claude-opus-5-5"})
        (since, price), = learned["claude-opus-6"]
        self.assertEqual(since, START)
        self.assertAlmostEqual(price.output, 30.0, places=6)
        (since, price), = learned["claude-opus-5-5"]
        self.assertEqual(since, START + timedelta(minutes=100))  # 値下げした記録の最初から
        self.assertAlmostEqual(price.cache_read, 0.15, places=6)
        # もう一度学んでも同じ(推定した単価で合うので足さない)
        self.assertEqual(learn_prices(records, replace(PRICES, learned=learned), 3, 0.03), learned)

    def test_needs_enough_records_and_keeps_matching_prices(self):
        few = self.records("claude-opus-6", Price(6.0, 7.5, 12.0, 0.3, 30.0), n=2)
        matching = self.records("claude-fable-5-1", FABLE51)
        self.assertEqual(learn_prices(few + matching, PRICES, min_records=3, tolerance=0.03), {})


class SplitIncrementsTest(unittest.TestCase):
    GAP = timedelta(minutes=45)

    @staticmethod
    def samples(*minutes_pct):
        return [LimitSample(START + timedelta(minutes=m), None, p) for m, p in minutes_pct]

    def test_split_by_code_activity(self):
        samples = self.samples((15, 10), (30, 25), (45, 25), (60, 30), (240, 50))
        code_at = [START + timedelta(minutes=m) for m in (5, 50)]
        # 0→15 分(Code あり)+10 / 15→30 分(なし)+15 / 30→45 分 増分なし / 45→60 分(あり)+5 / 60→240 分(間隔が長い)+20
        self.assertEqual(split_increments(WEEK, samples, code_at, self.GAP), (15.0, 15.0, 20.0))

    def test_reset_counts_value_after_drop_and_other_weeks_are_ignored(self):
        samples = self.samples((15, 60), (30, 5), (60 * 24 * 8, 90)) + [LimitSample(START, None, None)]
        # 30 分の時点で 60% → 5%(手動リセット): 増分は下がった後の 5
        self.assertEqual(split_increments(WEEK, samples, [], self.GAP), (0.0, 65.0, 0.0))

    def test_estimate_needs_enough_known_increments(self):
        self.assertAlmostEqual(estimate_non_code_pct(60.0, 20.0, 20.0, 80), 25.0)   # 分かる区間が 80%
        self.assertIsNone(estimate_non_code_pct(60.0, 10.0, 30.0, 80))               # 70% しかない
        self.assertIsNone(estimate_non_code_pct(0.0, 0.0, 0.0, 80))                  # 記録なし


class FactorEstimateTest(unittest.TestCase):
    """週ごとのモデルの倍率の推定: 区間の取り出し(factor_intervals)と当てはめ(fit_rates / estimate_factor)。"""
    GAP = timedelta(minutes=45)

    def test_intervals(self):
        samples = [LimitSample(START + timedelta(minutes=m), None, p) for m, p in
                   ((15, 2), (30, 5), (45, 5), (60, 1), (75, 3), (300, 9))]
        events = [event(5, output=10), event(20, model="claude-fable-5-1", output=2), event(25, output=4),
                  event(70, output=1), event(200, output=50)]
        rows = factor_intervals(WEEK, samples, events, self.GAP, "claude-fable", W)
        self.assertEqual(rows, [
            (2.0, 0.0, 50.0),    # 0→15 分: 週の始まり(0%)から。Opus 10×5
            (3.0, 10.0, 20.0),   # 15→30 分: Fable 2×5、Opus 4×5
            # 30→45 分は Claude Code を使っていない / 45→60 分は値が下がった(手動リセット)ので除く
            (2.0, 0.0, 5.0),     # 60→75 分
            # 75→300 分は間隔が長いので除く
        ])

    def test_model_factor_is_not_applied_to_interval_tokens(self):
        w = replace(W, model_factors={"claude-fable": ((START - timedelta(days=1), 4.0),)})
        samples = [LimitSample(START + timedelta(minutes=15), None, 1)]
        rows = factor_intervals(WEEK, samples, [event(5, model="claude-fable-5", output=2)], self.GAP, "claude-fable", w)
        self.assertEqual(rows, [(1.0, 10.0, 0.0)])  # 倍率 4 を掛けない

    def test_fit_recovers_rates(self):
        rows = [(2 * x + 0.5 * y, x, y) for x, y in ((1, 0), (0, 4), (3, 2), (1, 5), (2, 2))]
        a, b = fit_rates(rows)
        self.assertAlmostEqual(a, 2.0)
        self.assertAlmostEqual(b, 0.5)
        self.assertIsNone(fit_rates([(1, 1, 2), (2, 2, 4)]))  # x と y が比例していて見分けられない
        self.assertIsNone(fit_rates([(1, 0, 2), (2, 0, 4)]))  # x がずっと 0

    def test_estimate_conditions(self):
        rows = [(4 * x + y, x, y) for x, y in ((1, 0), (0, 3), (2, 1), (1, 4), (3, 2), (0, 1))]
        self.assertAlmostEqual(estimate_factor(rows, 6, 4, 1.0, 10.0), 4.0)
        self.assertIsNone(estimate_factor(rows, 7, 4, 1.0, 10.0))    # 区間が足りない
        self.assertIsNone(estimate_factor(rows, 6, 5, 1.0, 10.0))    # model を使った区間が 4 つしかない
        self.assertIsNone(estimate_factor(rows, 6, 4, 1.0, 3.5))     # 範囲の外
        negative = [(-x + y, x, y) for _, x, y in rows]
        self.assertIsNone(estimate_factor(negative, 6, 4, 0.1, 10.0))  # model の消費が負になる


class ObservationTest(unittest.TestCase):
    """画面で見た値(スクリーンショットなど)の扱い。"""
    ANCHOR = START
    CYCLE = timedelta(days=7)

    def obs(self, minutes, all_pct=None, **models):
        return Observation(START + timedelta(minutes=minutes), all_pct, models)

    def test_added_only_after_the_last_record_of_the_week(self):
        samples = [LimitSample(START + timedelta(minutes=60), 10.0, 30.0)]
        observations = [self.obs(30, 25.0), self.obs(90, 40.0, Fable=50.0), self.obs(120, None, Fable=60.0),
                        self.obs(60 * 24 * 8, 7.0)]  # 次の週(記録がないので足す)
        merged = with_observations(samples, observations, self.ANCHOR, self.CYCLE)
        self.assertEqual([(s.ts - START, s.seven_day) for s in merged], [
            (timedelta(minutes=60), 30.0), (timedelta(minutes=90), 40.0), (timedelta(days=8), 7.0)])

    def test_model_limit_points(self):
        observations = [self.obs(90, 40.0, Fable=50.0), self.obs(30, 25.0, Fable=20.0), self.obs(60 * 24 * 8, Fable=1.0),
                        self.obs(60, 30.0)]  # モデル別の値のない記録は点にならない
        self.assertEqual(model_limit_points(WEEK, observations),  # 週の中の点を時刻順に(次の週の点は除く)
                         {"Fable": [LimitPoint(START + timedelta(minutes=30), 20.0),
                                    LimitPoint(START + timedelta(minutes=90), 50.0)]})
        self.assertEqual(model_limit_points(WEEK, []), {})

    def test_split_tokens(self):
        events = [event(5, model="claude-fable-5-1", output=2), event(10, output=4), event(70, output=100)]
        w = replace(W, model_factors={"claude-fable": ((START, 4.0),)})
        self.assertEqual(split_tokens(events, START, START + timedelta(minutes=60), "claude-fable", w), (10.0, 20.0))

    def test_observed_factor(self):
        # Fable 80% → 40pt、全モデル 70% のうち Claude Code 以外 10% → それ以外 70×0.9−40 = 23pt
        self.assertAlmostEqual(observed_factor(20.0, 46.0, 70.0, 80.0, 50.0, 10.0), (40 / 20) / (23 / 46))
        self.assertIsNone(observed_factor(20.0, 46.0, 50.0, 100.0, 50.0, 0.0))  # それ以外の分が 0
        self.assertIsNone(observed_factor(0.0, 46.0, 70.0, 80.0, 50.0, 10.0))   # Fable を使っていない


class ActiveTimeTest(unittest.TestCase):
    GAP = timedelta(minutes=15)

    @staticmethod
    def at(*minutes):
        return [START + timedelta(minutes=m) for m in minutes]

    def test_gaps_up_to_the_limit_are_added(self):
        self.assertEqual(active_time(self.at(0, 5, 20, 36, 40), self.GAP), timedelta(minutes=24))  # 16 分の間隔は切れ目
        self.assertEqual(active_time(self.at(40, 0, 5), self.GAP), timedelta(minutes=5))           # 順番は問わない

    def test_nothing_to_add(self):
        self.assertEqual(active_time([], self.GAP), timedelta())
        self.assertEqual(active_time(self.at(0), self.GAP), timedelta())


class CompareSourcesTest(unittest.TestCase):
    GAP = timedelta(minutes=20)

    @staticmethod
    def at(minutes, pct):
        return LimitSample(START + timedelta(minutes=minutes), None, pct)

    def test_usage_values_between_neighbouring_desktop_values(self):
        desktop = [self.at(0, 10), self.at(15, 12), self.at(30, 12), self.at(45, 20), self.at(60 * 5, 30)]
        usage = [self.at(20, 12),          # 前 12・後 12 の間: 同じ値
                 self.at(40, 15),          # 前 12・後 20 の間: 収まるが値は違う
                 self.at(50, 19),          # 前 20 より小さい: 食い違い
                 self.at(60 * 3, 25),      # 前後 20 分以内にデスクトップの記録がない: 照合しない
                 self.at(60 * 5 + 10, 30),  # 後ろがない: 前 30 以上なら収まる
                 self.at(-10, 99),         # 前の週は数えない
                 LimitSample(START + timedelta(minutes=70), 50.0, None)]  # 週次% のない記録は照合しない
        failures = [START + timedelta(minutes=90), START - timedelta(minutes=30)]
        c = compare_sources(WEEK, desktop, usage, failures, self.GAP)
        self.assertEqual((c.compared, c.consistent, c.same), (4, 3, 2))
        self.assertEqual(c.mismatches, [(START + timedelta(minutes=50), 19.0, 20.0, None)])
        # 記録のある「時」: デスクトップ 10/20 10 時・15 時、/usage 10 時・13 時・15 時(13 時は /usage だけ)
        self.assertEqual((c.desktop_hours, c.usage_hours, c.usage_only_hours), (2, 3, 1))
        self.assertEqual((c.attempts, c.failures), (7, 1))  # 週の中の取得 6 回 + 失敗 1 回

    def test_without_usage_records(self):
        c = compare_sources(WEEK, [self.at(0, 10)], [], [], self.GAP)
        self.assertEqual((c.compared, c.attempts, c.desktop_hours, c.usage_hours), (0, 0, 1, 0))


class WithNonCodeTest(unittest.TestCase):
    def test_shares_are_scaled_to_the_whole_week(self):
        slices = group_projects([usage("a", 75.0), usage("b", 25.0)], 5, 3.0, 8)
        out = with_non_code(slices, [NonCodeUsage("Cowork", 40.0, False), NonCodeUsage("Cowork など", 10.0, True)])
        self.assertEqual([(s.name, s.share_pct, s.non_code) for s in out], [
            ("a", 37.5, False), ("b", 12.5, False), ("Cowork", 40.0, True), ("Cowork など(推定)", 10.0, True)])
        self.assertEqual(out[2].weighted, 80.0)  # Claude Code 100 が 50% に当たるので、40% は 80 相当
        self.assertEqual(sum(s.share_pct for s in out), 100.0)

    def test_unchanged_without_non_code_or_code_usage(self):
        slices = group_projects([usage("a", 75.0)], 5, 3.0, 8)
        self.assertEqual(with_non_code(slices, []), slices)
        self.assertEqual(with_non_code([], [NonCodeUsage("Cowork", 40.0, False)]), [])
        self.assertEqual(with_non_code(slices, [NonCodeUsage("Cowork など", 100.0, True)]), slices)


class InferWindowsTest(unittest.TestCase):
    def test_windows_start_at_the_hour_and_last_five_hours(self):
        events = [event(m) for m in (130, 10, 290, 320, 700)]  # 順不同で渡す
        windows = infer_windows(events, timedelta(hours=5))
        self.assertEqual([(s - START, e - START, len(m)) for s, e, m in windows], [
            (timedelta(0), timedelta(hours=5), 3),             # 10:10 に使い始め → 10:00〜15:00(10, 130, 290 分)
            (timedelta(hours=5), timedelta(hours=10), 1),      # 320 分 = 15:20 → 15:00〜20:00
            (timedelta(hours=11), timedelta(hours=16), 1),     # 700 分 = 21:40 → 21:00〜
        ])

    def test_empty(self):
        self.assertEqual(infer_windows([], timedelta(hours=5)), [])


class BuildWeekReportTest(unittest.TestCase):
    def test_aggregation(self):
        events = [
            event(10, output=10),                                         # alpha 50
            event(20, project_dir="/r/beta", model="claude-fable-5-1", output=20),  # beta 100(Fable)
            event(30, sub=True, session="s2", output=4),                  # alpha 20(サブエージェント)
            event(60 * 24 * 8, output=1000),                              # 週の外
        ]
        samples = [
            LimitSample(START + timedelta(minutes=50), 30.0, 12.0),
            LimitSample(START + timedelta(minutes=250), 61.0, 15.0),
            LimitSample(START + timedelta(minutes=320), 2.0, 15.0),   # 次の枠(この週には枠がない時間)
            LimitSample(START - timedelta(minutes=5), 90.0, 80.0),    # 前の週
        ]
        r = build_week_report(WEEK, events, samples, lambda d: d.rsplit("/", 1)[-1], W,
                              {"Fable": "claude-fable"}, timedelta(hours=5), prev_final_pct=70.0, unparsed_lines=3,
                              notes=["n"])
        by_name = {p.name: p for p in r.projects}
        self.assertEqual(by_name["alpha"].weighted, 70)
        self.assertEqual(by_name["alpha"].sessions, 2)
        self.assertEqual(by_name["alpha"].subagent_weighted, 20)
        self.assertEqual(by_name["beta"].weighted, 100)
        self.assertEqual(len(r.windows), 1)
        w = r.windows[0]
        self.assertEqual(w.by_project, {"alpha": 70, "beta": 100})
        self.assertEqual(w.by_model, {"Fable": 100})
        self.assertEqual(w.five_hour_pct, 61.0)  # 枠の中で最大(リセット直前)
        self.assertEqual([p.pct for p in r.limit_points], [12.0, 15.0, 15.0])
        self.assertEqual(r.final_pct, 15.0)
        self.assertEqual((r.prev_final_pct, r.unparsed_lines, r.notes), (70.0, 3, ["n"]))
        self.assertEqual(r.model_total("Fable"), 100)

    def test_empty_week(self):
        r = build_week_report(WEEK, [], [], str, W, {}, timedelta(hours=5))
        self.assertEqual((r.windows, r.projects, r.limit_points), ([], [], []))

    def test_dev_time(self):
        events = [event(0), event(10), event(14, project_dir="/r/beta"), event(25, project_dir="/r/beta"),
                  event(100), event(60 * 24 * 8)]  # 離れた応答は作業の切れ目。最後は週の外
        r = build_week_report(WEEK, events, [], lambda d: d.rsplit("/", 1)[-1], W, {}, timedelta(hours=5),
                              dev_time_gap=timedelta(minutes=15))
        # 週の合計: 全セッションをまとめて 0〜10・10〜14・14〜25 分がつながる(並行した分は 1 回)
        self.assertEqual(r.dev_time, timedelta(minutes=25))
        # プロジェクト別: それぞれの応答だけで計算する(alpha は 0〜10 分、beta は 14〜25 分。重なることがある)
        self.assertEqual({p.name: p.dev_time for p in r.projects},
                         {"alpha": timedelta(minutes=10), "beta": timedelta(minutes=11)})
        # 「その他」にまとめたときに数え直せるよう、応答の時刻を週の中の分だけ持つ
        alpha = next(p for p in r.projects if p.name == "alpha")
        self.assertEqual(alpha.times, tuple(START + timedelta(minutes=m) for m in (0, 10, 100)))
        self.assertEqual(group_dev_time(r.projects, timedelta(minutes=15)), r.dev_time)


class GroupDevTimeTest(unittest.TestCase):
    GAP = timedelta(minutes=15)

    def test_times_are_merged_so_parallel_work_counts_once(self):
        a = ProjectUsage("a", 1.0, 1, 0.0, timedelta(minutes=20), (START, START + timedelta(minutes=20)))
        b = ProjectUsage("b", 1.0, 1, 0.0, timedelta(minutes=10),
                         (START + timedelta(minutes=5), START + timedelta(minutes=15)))
        self.assertEqual(group_dev_time([a, b], self.GAP), timedelta(minutes=20))   # 合計の 30 分ではない

    def test_sum_when_times_are_missing(self):
        # ダミーデータなど時刻を持たないものは、各プロジェクトの値の合計にする
        a = ProjectUsage("a", 1.0, 1, 0.0, timedelta(hours=2))
        b = ProjectUsage("b", 1.0, 1, 0.0, timedelta(hours=1), (START,))
        self.assertEqual(group_dev_time([a, b], self.GAP), timedelta(hours=3))
        self.assertEqual(group_dev_time([], self.GAP), timedelta())


def usage(name, weighted):
    return ProjectUsage(name=name, weighted=weighted, sessions=1, subagent_weighted=0.0)


class AssignSlotsTest(unittest.TestCase):
    def test_new_names_take_lowest_slots_in_order(self):
        self.assertEqual(assign_slots(["alpha", "beta", "gamma"], 8), {"alpha": 0, "beta": 1, "gamma": 2})

    def test_preferred_slots_are_kept(self):
        result = assign_slots(["gamma", "alpha", "new"], 8, {"alpha": 0, "gamma": 2, "gone": 1})
        self.assertEqual(result, {"gamma": 2, "alpha": 0, "new": 1})

    def test_survivor_keeps_color_when_others_disappear(self):
        full = assign_slots(["alpha", "beta", "gamma"], 8)
        alone = assign_slots(["gamma"], 8, full)
        self.assertEqual(alone["gamma"], full["gamma"])

    def test_conflicting_or_invalid_preferences(self):
        result = assign_slots(["a", "b", "c"], 8, {"a": 3, "b": 3, "c": 99})
        self.assertEqual(result, {"a": 3, "b": 0, "c": 1})

    def test_more_names_than_slots(self):
        result = assign_slots([f"p{i}" for i in range(10)], 8)
        self.assertEqual(len(result), 8)
        self.assertEqual(sorted(result.values()), list(range(8)))


class GroupProjectsTest(unittest.TestCase):
    def test_top_n_and_others(self):
        projects = [usage("a", 50), usage("b", 30), usage("c", 10), usage("d", 6), usage("e", 4)]
        slices = group_projects(projects, max_slices=3, others_min_pct=3.0, n_slots=8)
        self.assertEqual([s.name for s in slices], ["a", "b", "c", OTHERS])
        self.assertEqual(slices[-1].members, ("d", "e"))
        self.assertIsNone(slices[-1].slot)
        self.assertAlmostEqual(sum(s.share_pct for s in slices), 100.0)

    def test_small_share_goes_to_others_even_within_top_n(self):
        projects = [usage("a", 97), usage("b", 2), usage("c", 1)]
        slices = group_projects(projects, max_slices=5, others_min_pct=3.0, n_slots=8)
        self.assertEqual([s.name for s in slices], ["a", OTHERS])
        self.assertEqual(slices[1].members, ("b", "c"))

    def test_no_others_when_everything_fits(self):
        slices = group_projects([usage("a", 60), usage("b", 40)], max_slices=5, others_min_pct=3.0, n_slots=8)
        self.assertEqual([s.name for s in slices], ["a", "b"])

    def test_empty_and_zero(self):
        self.assertEqual(group_projects([], 5, 3.0, 8), [])
        self.assertEqual(group_projects([usage("a", 0)], 5, 3.0, 8), [])

    def test_ties_are_ordered_by_name(self):
        slices = group_projects([usage("b", 10), usage("a", 10)], 5, 3.0, 8)
        self.assertEqual([s.name for s in slices], ["a", "b"])


class ModelGroupTest(unittest.TestCase):
    MODELS = {"Fable": "claude-fable", "Opus": "claude-opus"}

    def test_prefix_match(self):
        self.assertEqual(model_group("claude-fable-5-1", self.MODELS), "Fable")
        self.assertEqual(model_group("claude-fable-5", self.MODELS), "Fable")
        self.assertEqual(model_group("claude-opus-5-5", self.MODELS), "Opus")

    def test_no_match(self):
        self.assertIsNone(model_group("claude-sonnet-5", self.MODELS))
        self.assertIsNone(model_group("<synthetic>", self.MODELS))
        self.assertIsNone(model_group(None, self.MODELS))
        self.assertIsNone(model_group("claude-fable-5", {}))


class FoldToSlicesTest(unittest.TestCase):
    def test_fold(self):
        projects = [usage("a", 50), usage("b", 30), usage("c", 1), usage("d", 1)]
        slices = group_projects(projects, max_slices=2, others_min_pct=0, n_slots=8)
        self.assertEqual(fold_to_slices({"a": 5, "c": 1, "d": 2, "unknown": 9}, slices), [5, 0, 3])


if __name__ == "__main__":
    unittest.main()
