import json
import re
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_usage.config import ReportOptions
from claude_usage.demo import SCENARIOS, build_demo_report
from claude_usage.metrics import axis_for, cumulative, group_projects
from claude_usage.model import FROM_DEFAULT, FROM_OBSERVED, FROM_RECORDS, LimitPoint, ModelFactor, NonCodeUsage, ProjectUsage, WeekReport, WindowBucket
from claude_usage.render.report import breakable, render_report
from claude_usage.render.svg import (
    DONUT_SIZE,
    LIMIT_H,
    N_SLOTS,
    WIDTH,
    WINDOWS_H,
    WINDOWS_PAD_T,
    AXIS_B,
    USAGE_H,
    XScale,
    day_label,
    day_label_positions,
    donut_chart,
    end_label_ys,
    fmt_usd,
    fmt_share,
    limit_chart,
    midnights,
    usage_chart,
    fraction_ticks,
    smooth_path,
    window_label,
    windows_chart,
)

TZ = ZoneInfo("Asia/Tokyo")
END = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)  # 10/27 火 19:00 JST
START = END - timedelta(days=7)
OPTIONS = ReportOptions(max_slices=5, others_min_pct=3.0, marker_max_points=40, models={"Fable": "claude-fable"})
SVG_NS = "{http://www.w3.org/2000/svg}"


def window(hours_from_start, by_project, pct=None, by_model=None):
    s = START + timedelta(hours=hours_from_start)
    return WindowBucket(s, s + timedelta(hours=5), by_project, pct, by_model=by_model or {})


def report(points=(), projects=None, windows=None):
    projects = projects if projects is not None else [
        ProjectUsage("alpha", 60.0, 3, 6.0), ProjectUsage("beta", 40.0, 2, 0.0)]
    windows = windows if windows is not None else [
        window(h, {"alpha": 6.0, "beta": 4.0}, pct=10.0 * (i + 1)) for i, h in enumerate(range(0, 160, 16))]
    return WeekReport(week_start=START, week_end=END, limit_points=list(points), windows=windows, projects=projects)


def parse_svg(svg: str) -> ET.Element:
    # 名前空間なしの断片なので、検証用に付けてから XML として読む(壊れたタグがあればここで例外になる)
    return ET.fromstring(svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1))


def numbers(attr: str) -> list[float]:
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", attr)]


def marks(root: ET.Element, cls: str = "mark") -> list[ET.Element]:
    return [e for e in root.iter() if cls in (e.get("class") or "").split()]


class FormatTest(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(fmt_usd(2849.3), "$2,849")
        self.assertEqual(fmt_usd(45.2), "$45")
        self.assertEqual(fmt_usd(10), "$10")
        self.assertEqual(fmt_usd(4.56), "$4.6")
        self.assertEqual(fmt_usd(0.123), "$0.12")
        self.assertEqual(fmt_usd(0), "$0")

    def test_share(self):
        self.assertEqual(fmt_share(66.24), "66%")
        self.assertEqual(fmt_share(0.94), "0.9%")

    def test_labels(self):
        self.assertEqual(day_label(START, TZ), "10/20 火")
        self.assertEqual(window_label(window(0, {}), TZ), "10/20 火 19:00–00:00")

    def test_fraction_ticks_top_is_exactly_the_maximum(self):
        self.assertEqual(fraction_ticks(4.94e8), [0, 1.235e8, 2.47e8, 3.705e8, 4.94e8])  # 余白なし
        self.assertEqual(fraction_ticks(0), [0, 0.25, 0.5, 0.75, 1.0])                  # 0 除算を避ける


class DayAxisTest(unittest.TestCase):
    def test_midnights_inside_week(self):
        ms = midnights(START, END, TZ)
        self.assertEqual([m.astimezone(TZ).strftime("%m/%d %H:%M") for m in ms],
                         ["10/21 00:00", "10/22 00:00", "10/23 00:00", "10/24 00:00",
                          "10/25 00:00", "10/26 00:00", "10/27 00:00"])

    def test_midnight_boundaries_are_excluded(self):
        s = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
        ms = midnights(s, s + timedelta(days=2), TZ)
        self.assertEqual([m.astimezone(TZ).day for m in ms], [21])

    def test_short_edge_days_get_no_label(self):
        # 週の端の 10/20(19 時から 5 時間)は短いのでラベルなし。10/27(0 時から 19 時)は 19 時間あるので出す
        days = [d.astimezone(TZ).strftime("%m/%d") for _, d in day_label_positions(START, END, TZ)]
        self.assertEqual(days, ["10/21", "10/22", "10/23", "10/24", "10/25", "10/26", "10/27"])

    def test_every_weekly_chart_has_dates_below_the_plot(self):
        r = report([LimitPoint(START + timedelta(hours=h), h / 2) for h in (3, 30)],
                   windows=[window(10, {"alpha": 4.0}, by_model={"Fable": 2.0})])
        charts = {"limit": (limit_chart(r, TZ, 40), LIMIT_H), "usage": (usage_chart(r, ["Fable"], TZ, 10.0, 5.0), USAGE_H),
                  "windows": (windows_chart(r, group_projects(r.projects, 5, 3.0, N_SLOTS), TZ), WINDOWS_H)}
        for name, (svg, height) in charts.items():
            with self.subTest(chart=name):
                root = parse_svg(svg)
                dates = [e for e in root.iter(f"{SVG_NS}text") if e.get("text-anchor") == "middle"
                         and e.get("class") == "tick"]
                self.assertEqual([e.text for e in dates],
                                 ["10/21 水", "10/22 木", "10/23 金", "10/24 土", "10/25 日", "10/26 月", "10/27 火"])
                plot_bottom = max(float(e.get("y1")) for e in root.iter(f"{SVG_NS}line") if e.get("x1") == e.get("x2"))
                self.assertTrue(all(plot_bottom < float(e.get("y")) <= height for e in dates))  # グラフの下・図の中


class SmoothPathTest(unittest.TestCase):
    @staticmethod
    def segments(d):
        """'M x,y C c1x,c1y c2x,c2y x,y ...' を (始点, 制御点 1, 制御点 2, 終点) の組に分ける。"""
        nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]
        start, rest = (nums[0], nums[1]), nums[2:]
        out = []
        for i in range(0, len(rest), 6):
            c1, c2, end = (rest[i], rest[i + 1]), (rest[i + 2], rest[i + 3]), (rest[i + 4], rest[i + 5])
            out.append((start, c1, c2, end))
            start = end
        return out

    def test_passes_through_points_without_overshoot(self):
        coords = [(0, 100), (10, 100), (20, 60), (30, 55), (40, 55), (50, 10)]  # 画面の y は下向き(累積が増えると小さくなる)
        segs = self.segments(smooth_path(coords))
        self.assertEqual([s[3] for s in segs], [(float(x), float(y)) for x, y in coords[1:]])
        for p0, c1, c2, p1 in segs:
            lo, hi = min(p0[1], p1[1]), max(p0[1], p1[1])
            self.assertTrue(lo - 1e-9 <= c1[1] <= hi + 1e-9 and lo - 1e-9 <= c2[1] <= hi + 1e-9)  # 単調(行き過ぎない)

    def test_flat_stretches_stay_flat(self):
        segs = self.segments(smooth_path([(0, 100), (10, 100), (20, 50), (30, 50)]))
        self.assertEqual({c[1] for c in (segs[0][1], segs[0][2])}, {100.0})
        self.assertEqual({c[1] for c in (segs[2][1], segs[2][2])}, {50.0})

    def test_duplicate_x_and_short_inputs(self):
        self.assertEqual(smooth_path([(0, 5), (0, 3), (10, 1)]).split(" ")[0], "M0,3")
        self.assertEqual(smooth_path([(1, 2)]), "M1,2")
        self.assertEqual(smooth_path([]), "")


    def test_drop_is_drawn_without_overshoot(self):
        # 使用率が下がる区間(手動リセット)をはさんでも、各区間の中で記録の値を越えない
        segs = self.segments(smooth_path([(0, 150), (10, 40), (20, 190), (30, 120)]))
        for p0, c1, c2, p1 in segs:
            lo, hi = min(p0[1], p1[1]), max(p0[1], p1[1])
            self.assertTrue(lo - 1e-9 <= c1[1] <= hi + 1e-9 and lo - 1e-9 <= c2[1] <= hi + 1e-9)


class LimitChartTest(unittest.TestCase):
    def line_of(self, svg):
        return parse_svg(svg).find(f"{SVG_NS}path[@class='limit-line']")

    @staticmethod
    def line_points(d):
        """なめらかな線の path から、記録点(曲線の始点と各区間の終点)を取り出す。"""
        segs = SmoothPathTest.segments(d)
        return [segs[0][0]] + [s[3] for s in segs]

    def test_points_inside_viewbox(self):
        pts = [LimitPoint(START + timedelta(hours=h), h / 1.68) for h in range(1, 168, 3)]
        d = self.line_of(limit_chart(report(pts), TZ, OPTIONS.marker_max_points)).get("d")
        self.assertIn(" C", d)  # 階段線ではなく、なめらかな曲線
        xs, ys = zip(*self.line_points(d))
        xs, ys = list(xs), list(ys)
        self.assertEqual(len(xs), len(pts))
        self.assertTrue(all(0 <= x <= WIDTH for x in xs))
        self.assertTrue(all(0 <= y <= LIMIT_H for y in ys))
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(ys, sorted(ys, reverse=True))  # 使用率が増えるほど上へ

    def test_no_pace_line(self):
        svg = limit_chart(report([LimitPoint(START + timedelta(hours=3), 12)]), TZ, 40)
        self.assertNotIn("pace", svg)
        self.assertNotIn("均等ペース", svg)

    def test_day_gridlines_at_midnight(self):
        root = parse_svg(limit_chart(report([]), TZ, 40))
        vertical = [e for e in root.iter(f"{SVG_NS}line") if e.get("x1") == e.get("x2")]
        self.assertEqual(len(vertical), 7)

    def test_unsorted_input_is_drawn_in_time_order(self):
        pts = [LimitPoint(START + timedelta(hours=5), 30), LimitPoint(START + timedelta(hours=1), 10)]
        d = self.line_of(limit_chart(report(pts), TZ, 40)).get("d")
        (x0, y0), (x1, y1) = self.line_points(d)
        self.assertLess(x0, x1)
        self.assertGreater(y0, y1)

    def test_markers_only_when_sparse(self):
        many = [LimitPoint(START + timedelta(hours=h), h / 2) for h in range(1, 60)]
        root = parse_svg(limit_chart(report(many), TZ, marker_max_points=40))
        self.assertEqual(len(root.findall(f"{SVG_NS}circle[@class='limit-marker']")), 1)
        root = parse_svg(limit_chart(report(many[:5]), TZ, marker_max_points=40))
        self.assertEqual(len(root.findall(f"{SVG_NS}circle[@class='limit-marker']")), 5)

    def test_single_point_has_marker_but_no_line(self):
        svg = limit_chart(report([LimitPoint(START + timedelta(hours=3), 12)]), TZ, 40)
        self.assertIsNone(self.line_of(svg))
        self.assertIn('class="limit-marker"', svg)

    def test_empty(self):
        svg = limit_chart(report([]), TZ, OPTIONS.marker_max_points)
        parse_svg(svg)
        self.assertIn("記録はありません", svg)
        self.assertNotIn("limit-line", svg)

    def test_points_outside_week_are_ignored(self):
        pts = [LimitPoint(START - timedelta(hours=1), 90), LimitPoint(START + timedelta(hours=1), 5)]
        svg = limit_chart(report(pts), TZ, OPTIONS.marker_max_points)
        self.assertIn(">5%</text>", svg)
        self.assertNotIn(">90%</text>", svg)

    def model_report(self):
        h = lambda n: START + timedelta(hours=n)  # noqa: E731
        return replace(report([LimitPoint(h(1), 10), LimitPoint(h(20), 30), LimitPoint(h(40), 50)]),
                       model_limit_points={"Fable": [LimitPoint(h(2), 4), LimitPoint(h(30), 21),
                                                     LimitPoint(END + timedelta(hours=1), 99)]})  # 週の外は描かない

    def test_model_limit_line(self):
        root = parse_svg(limit_chart(self.model_report(), TZ, 40, ["Fable"]))
        self.assertIsNotNone(root.find(f"{SVG_NS}path[@class='limit-line']"))
        model_line = root.find(f"{SVG_NS}path[@class='model-line']")
        self.assertEqual(len(self.line_points(model_line.get("d"))), 2)
        self.assertEqual(len(root.findall(f"{SVG_NS}circle[@class='model-marker']")), 2)
        ends = {e.get("class"): e.text for e in root.iter(f"{SVG_NS}text") if "end-label" in e.get("class")}
        self.assertEqual(ends, {"end-label": "50%", "end-label secondary": "21%"})
        self.assertIn("・Fable 21%", root.get("aria-label"))
        hover = json.loads(root.find(f"{SVG_NS}rect[@class='hover-target']").get("data-points"))
        # 全体の記録の時刻ごとに、その時点で分かっているモデル別の値を添える(最初の点の時刻はまだ記録がない)
        self.assertEqual([p["v"] for p in hover], ["全体 10%", "全体 30% / Fable 4%", "全体 50% / Fable 21%"])

    def test_model_limit_line_only_for_requested_models(self):
        svg = limit_chart(self.model_report(), TZ, 40)
        self.assertNotIn("model-line", svg)
        self.assertNotIn("Fable", svg)

    def test_end_labels_do_not_overlap(self):
        # SVG の y は下ほど大きい。後の線の終わりが下にあれば値も下へ、上にあれば上へずらす
        self.assertEqual(end_label_ys([(700, 100), (700, 104)]), [105, 121])
        self.assertEqual(end_label_ys([(700, 100), (700, 110)]), [105, 121])
        self.assertEqual(end_label_ys([(700, 100), (700, 98)]), [105, 89])
        self.assertEqual(end_label_ys([(700, 100), (700, 150)]), [105, 155])     # 離れていれば動かさない
        self.assertEqual(end_label_ys([(700, 100), (600, 100)]), [105, 105])     # 横に離れていれば動かさない


class WindowsChartTest(unittest.TestCase):
    def render(self, r):
        return parse_svg(windows_chart(r, group_projects(r.projects, 5, 3.0, N_SLOTS), TZ))

    def test_stacked_bars_and_pct_dots(self):
        root = self.render(report())
        bars = [e for e in marks(root) if "hit" not in e.get("class")]
        dots = marks(root, "window-pct")
        hits = marks(root, "hit")
        self.assertEqual(len(bars), 20)  # 10 枠 × 2 プロジェクト
        self.assertEqual(len(dots), 10)
        self.assertEqual(len(hits), 10)
        self.assertTrue(all(float(h.get("r")) >= 12 for h in hits))  # 当たり判定は直径 24 以上
        self.assertTrue(all("mark" in h.get("class") for h in hits))
        for e in root.iter(f"{SVG_NS}rect"):
            self.assertLessEqual(float(e.get("width")), 24)
            self.assertGreaterEqual(float(e.get("y")), 0)
            self.assertLessEqual(float(e.get("y")) + float(e.get("height")), WINDOWS_H)

    def test_dot_height_follows_right_axis(self):
        r = report(windows=[window(10, {"alpha": 1.0}, pct=0), window(30, {"alpha": 1.0}, pct=100)])
        dots = marks(self.render(r), "window-pct")
        top, bottom = WINDOWS_PAD_T, WINDOWS_H - AXIS_B
        self.assertAlmostEqual(float(dots[0].get("cy")), bottom, delta=0.1)
        self.assertAlmostEqual(float(dots[1].get("cy")), top, delta=0.1)

    def test_both_axes_are_labeled(self):
        texts = [e.text for e in self.render(report()).iter(f"{SVG_NS}text")]
        for label in ("換算額", "5時間枠", "0%", "25%", "50%", "75%", "100%", "10/21 水", "10/27 火"):
            self.assertIn(label, texts)
        self.assertNotIn("10/20 火", texts)  # 週の端の短い日はラベルなし

    def test_window_without_pct_has_no_dot(self):
        r = report(windows=[window(10, {"alpha": 1.0}, pct=None)])
        self.assertEqual(marks(self.render(r), "window-pct"), [])

    def test_bar_is_centered_in_its_window(self):
        r = report(windows=[window(10, {"alpha": 1.0}, pct=None)])
        d = marks(self.render(r))[0].get("d")
        # 上端を丸めた棒の path: 左端は M の x、右端は 2 つ目の円弧の終点の x
        x0 = float(re.match(r"M([\d.]+),", d).group(1))
        x1 = float(re.findall(r"A[\d.]+,[\d.]+ 0 0 1 ([\d.]+),", d)[1])
        mid_expected = XScale(START, END)(START + timedelta(hours=12.5))  # 枠(10〜15 時間後)の中央
        self.assertAlmostEqual((x0 + x1) / 2, mid_expected, delta=0.2)

    def test_no_windows(self):
        root = self.render(report(windows=[]))
        self.assertEqual(marks(root), [])


class NonCodeTest(unittest.TestCase):
    """Claude Code 以外の利用(Cowork など)は、円グラフと表にだけ割合で加える(使用量・時刻は分からない)。"""

    def test_pie_and_table_include_non_code_share(self):
        r = replace(report(), non_code=[NonCodeUsage("Cowork", 40.0, estimated=False)])
        html = render_report(r, TZ, OPTIONS)
        donut = parse_svg(re.search(r'<svg class="chart chart-donut".*?</svg>', html, flags=re.S).group(0))
        seg = marks(donut, "fill-noncode")
        self.assertEqual(len(seg), 1)
        self.assertEqual((seg[0].get("data-tip-value"), seg[0].get("data-tip-label")), ("40%", "Cowork"))
        self.assertIn("Claude Code 分", html)
        # 表の割合は週全体に対する値: alpha は Claude Code の 60% × 残り 60% = 36%
        self.assertRegex(html, r'alpha</span> </td><td class="num">36%</td>')
        self.assertRegex(html, r'fill-noncode"></span>Cowork</span></td><td class="num">40%</td><td class="num">--</td>')
        # 時系列のグラフと「最多」の欄は Claude Code だけ
        self.assertIn("最多: alpha(60%)", html)
        body = html.split("</style>", 1)[1]
        self.assertEqual(body.count("fill-noncode"), 2)  # 円グラフと表の 2 か所だけ

    def test_neighbouring_non_code_slices_get_different_shades(self):
        r = replace(report(), non_code=[NonCodeUsage("チャット", 5.0, False), NonCodeUsage("Cowork", 14.0, False)])
        body = render_report(r, TZ, OPTIONS).split("</style>", 1)[1]
        self.assertIn('fill-noncode"></span>チャット', body)
        self.assertIn('fill-noncode-2"></span>Cowork', body)

    def test_estimated_name_and_no_change_without_non_code(self):
        r = replace(report(), non_code=[NonCodeUsage("Cowork など", 25.0, estimated=True)])
        self.assertIn("Cowork など(推定)", render_report(r, TZ, OPTIONS))
        self.assertNotIn('class="mark donut-seg fill-noncode', render_report(report(), TZ, OPTIONS))


class DonutChartTest(unittest.TestCase):
    def test_segments(self):
        r = report()
        slices = group_projects(r.projects, 5, 3.0, N_SLOTS)
        root = parse_svg(donut_chart(slices, r.total_weighted))
        segs = root.findall(f"{SVG_NS}path")
        self.assertEqual(len(segs), 2)
        for seg in segs:
            self.assertTrue(all(0 <= n <= DONUT_SIZE for n in numbers(seg.get("d"))))

    def test_single_slice_is_full_ring(self):
        slices = group_projects([ProjectUsage("only", 10, 1, 0)], 5, 3.0, N_SLOTS)
        root = parse_svg(donut_chart(slices, 10))
        self.assertEqual(len(root.findall(f"{SVG_NS}path")), 1)

    def test_empty(self):
        svg = donut_chart([], 0)
        parse_svg(svg)
        self.assertIn("データなし", svg)


class ModelUsageTest(unittest.TestCase):
    """週の使用量(重み付きトークンの累積)のグラフ。全体と Fable を同じ縦軸に描く。"""

    def fable_report(self):
        return report(windows=[
            window(10, {"alpha": 4_000_000.0}, 30, by_model={"Fable": 2_000_000.0}),
            window(20, {"alpha": 1_000_000.0}, 40, by_model={}),                      # Fable を使っていない枠
            window(40, {"alpha": 5_000_000.0}, 50, by_model={"Fable": 3_000_000.0}),
        ], projects=[ProjectUsage("alpha", 10_000_000.0, 1, 0.0)])

    def test_cumulative_points(self):
        r = self.fable_report()
        h = lambda n: START + timedelta(hours=n)  # noqa: E731
        # 使った枠ごとに、始まり(それまでの累積)と終わり(その枠の分を足した累積)の 2 点。使っていない枠は飛ばす
        self.assertEqual(cumulative(r, "Fable"), [(START, 0.0), (h(10), 0.0), (h(15), 2_000_000.0),
                                                  (h(40), 2_000_000.0), (h(45), 5_000_000.0)])
        self.assertEqual(cumulative(r), [(START, 0.0), (h(10), 0.0), (h(15), 4_000_000.0), (h(20), 4_000_000.0),
                                         (h(25), 5_000_000.0), (h(40), 5_000_000.0), (h(45), 10_000_000.0)])
        self.assertEqual(cumulative(r, "Opus"), [])

    def test_cumulative_back_to_back_windows_share_a_point(self):
        r = report(windows=[window(0, {"a": 1.0}), window(5, {"a": 2.0})], projects=[ProjectUsage("a", 3.0, 1, 0)])
        self.assertEqual(cumulative(r), [(START, 0.0), (START + timedelta(hours=5), 1.0),
                                         (START + timedelta(hours=10), 3.0)])

    def chart(self, total_max=4.94e8, model_max=1.55e8):
        return parse_svg(usage_chart(self.fable_report(), ["Fable"], TZ, total_max, model_max))

    def tick_labels(self, root, anchor_end):
        """縦軸の目盛り(左軸は右寄せ、右軸は左寄せ。中央寄せは横軸の日付)。"""
        return [e.text for e in root.iter(f"{SVG_NS}text") if e.get("class") == "tick"
                and e.get("text-anchor") != "middle" and (e.get("text-anchor") == "end") == anchor_end]

    def test_two_axes_each_topped_by_its_own_maximum(self):
        root = self.chart()
        self.assertEqual(self.tick_labels(root, True), ["$0", "$123,500,000", "$247,000,000", "$370,500,000", "$494,000,000"])   # 左軸 = 全体
        self.assertEqual(self.tick_labels(root, False), ["$0", "$38,750,000", "$77,500,000", "$116,250,000", "$155,000,000"])  # 右軸 = Fable
        titles = [e.text for e in root.iter(f"{SVG_NS}text") if e.get("class") == "axis-title"]
        self.assertEqual(titles, ["全体", "Fable"])

    def test_lines_are_drawn_on_their_own_axes(self):
        root = self.chart(total_max=10_000_000.0, model_max=5_000_000.0)
        total = root.find(f"{SVG_NS}circle[@class='limit-marker']")
        fable = root.find(f"{SVG_NS}circle[@class='model-marker']")
        top_y = min(float(e.get("y1")) for e in root.iter(f"{SVG_NS}line") if e.get("x1") != e.get("x2"))  # 最上段の横線
        # どちらもこれまでの最大値に達しているので、点はそれぞれの軸のいちばん上に来る
        self.assertAlmostEqual(float(total.get("cy")), top_y, delta=0.1)
        self.assertAlmostEqual(float(fable.get("cy")), top_y, delta=0.1)
        hover = json.loads(root.find(f"{SVG_NS}rect[@class='hover-target']").get("data-points"))
        self.assertEqual(hover[-1]["v"], "全体 $10,000,000 / Fable $5,000,000")

    def test_week_above_maximum_is_not_clipped(self):
        root = self.chart(total_max=1.0, model_max=1.0)  # 上限より大きい週は、その週の値を上限にする
        self.assertEqual(self.tick_labels(root, True)[-1], "$10,000,000")

    def test_no_right_axis_without_models(self):
        root = parse_svg(usage_chart(self.fable_report(), [], TZ, 4.94e8, 0.0))
        self.assertEqual(self.tick_labels(root, False), [])
        self.assertIsNone(root.find(f"{SVG_NS}path[@class='model-line']"))

    def test_chart_without_usage(self):
        svg = usage_chart(report(windows=[]), ["Fable"], TZ, 1e8, 1e8)
        parse_svg(svg)
        self.assertIn("この週の Claude Code の使用はありません", svg)

    def test_report_sections(self):
        html = render_report(self.fable_report(), TZ, OPTIONS)
        self.assertIn("<h2>週の使用量(Claude Code)</h2>", html)
        self.assertIn('<span class="line-key secondary"></span>Fable(右軸)', html)  # 凡例
        self.assertIn("Fable の使用量</div><div class=\"value\">$5,000,000</div>", html)
        self.assertIn("<th>Fable</th>", html)  # 5 時間枠ごとの表の列

    def test_no_models_configured(self):
        options = ReportOptions(max_slices=5, others_min_pct=3.0, marker_max_points=40)
        html = render_report(self.fable_report(), TZ, options)
        self.assertNotIn("Fable", html)


class SharedAxisTest(unittest.TestCase):
    def test_windows_chart_uses_shared_axis(self):
        r = report()  # 枠の合計は 10 だけ
        slices = group_projects(r.projects, 5, 3.0, N_SLOTS)
        texts = [e.text for e in parse_svg(windows_chart(r, slices, TZ, axis_max=6.3e7)).iter(f"{SVG_NS}text")]
        self.assertIn("$63,000,000", texts)      # 一番上はこれまでの最大値ちょうど
        self.assertNotIn("$80,000,000", texts)   # きりのよい値への切り上げはしない
        self.assertIn("100%", texts)         # 右軸(5 時間枠の %)の横線と 4 等分でそろう

    def test_axis_from_report_when_not_given(self):
        r = report()
        a = render_report(r, TZ, OPTIONS)
        b = render_report(r, TZ, OPTIONS, axis=axis_for([r], OPTIONS.models))
        self.assertEqual(a, b)


class LimitSectionTest(unittest.TestCase):
    def points(self, hours):
        return [LimitPoint(START + timedelta(hours=h), h / 2) for h in range(1, hours + 1)]

    def test_shown_only_with_enough_records(self):
        options = ReportOptions(5, 3.0, 40, {"Fable": "claude-fable"}, min_limit_hours=24)
        enough = render_report(report(self.points(24)), TZ, options)
        self.assertIn("<h2>週次上限の使用率</h2>", enough)
        self.assertIn('class="chart chart-limit"', enough)
        few = render_report(report(self.points(5)), TZ, options)
        self.assertNotIn("<h2>週次上限の使用率</h2>", few)
        self.assertIn("5 時間分しかないため、% のグラフは載せていません", few)
        none = render_report(report([]), TZ, options)
        self.assertNotIn("<h2>週次上限の使用率</h2>", none)
        self.assertNotIn("しかないため", none)  # 記録が 0 の週は別の注記(weekly 側)に任せる

    def test_limit_chart_comes_first_with_model_legend_and_table(self):
        r = replace(report(self.points(24)), model_limit_points={"Fable": [LimitPoint(START + timedelta(hours=2), 1),
                                                                          LimitPoint(START + timedelta(minutes=30), 0)]})
        html = render_report(r, TZ, OPTIONS)
        order = [html.index(s) for s in ("<h2>週次上限の使用率</h2>", "<h2>週の使用量(Claude Code)</h2>",
                                         "<h2>5時間枠ごとの使用量(Claude Code)</h2>")]
        self.assertEqual(order, sorted(order))  # 使用率のグラフが一番上
        self.assertIn('<li><span class="line-key"></span>全体</li><li><span class="line-key secondary"></span>Fable</li>',
                      html)
        self.assertIn("<th>記録時刻</th><th>全体</th><th>Fable</th>", html)
        self.assertIn('<td class="num">1%</td><td class="num">1%</td>', html)  # 同じ時刻に両方の記録
        self.assertIn('<td class="num">--</td>', html)                         # 片方しかない時刻
        plain = render_report(report(self.points(24)), TZ, OPTIONS)  # モデル別の記録がない週は従来どおり
        self.assertNotIn('<span class="line-key"></span>全体</li>', plain)
        self.assertIn("<th>記録時刻</th><th>使用率</th>", plain)

    def test_model_limits_are_the_latest_points(self):
        r = replace(report(), model_limit_points={"Fable": [LimitPoint(START + timedelta(hours=2), 30),
                                                             LimitPoint(START + timedelta(hours=1), 10)], "Opus": []})
        self.assertEqual(r.model_limits, {"Fable": LimitPoint(START + timedelta(hours=2), 30)})

    def test_limit_hours_counts_distinct_hours(self):
        pts = [LimitPoint(START + timedelta(minutes=m), 1) for m in (0, 15, 30, 70, 130)]
        self.assertEqual(report(pts).limit_hours, 3)


class RenderReportTest(unittest.TestCase):
    def test_kpis(self):
        html = render_report(report([LimitPoint(START + timedelta(hours=3), 41)]), TZ, OPTIONS)
        tiles = re.findall(r'<div class="label">([^<]+)</div><div class="value">([^<]+)</div>'
                           r'<div class="delta">([^<]*)</div>', html)
        self.assertEqual([t[0] for t in tiles],
                         ["週次上限の使用率(最後の記録)", "API 換算額(Claude Code)", "Fable の使用量", "開発時間"])
        self.assertEqual(tiles[0][1], "41%")
        self.assertEqual(tiles[1][2], "最多: alpha(60%)")  # 長い名前でも収まるよう、補足の行に書く

    def test_dev_time(self):
        projects = [ProjectUsage("alpha", 60.0, 3, 6.0, timedelta(hours=3, minutes=6)),
                    ProjectUsage("beta", 40.0, 2, 0.0, timedelta(hours=8, minutes=12))]
        r = replace(report(projects=projects), dev_time=timedelta(hours=9, minutes=30),
                    non_code=[NonCodeUsage("Cowork", 10.0, estimated=False)])
        html = render_report(r, TZ, OPTIONS)
        self.assertIn('<div class="label">開発時間</div><div class="value">9.5 時間</div>'
                      '<div class="delta">最多: beta(8.2 時間)</div>', html)
        self.assertIn("<th>API 換算額</th><th>開発時間</th><th>セッション</th>", html)
        self.assertRegex(html, r'alpha</span> </td><td class="num">54%</td><td class="num">\$60</td>'
                               r'<td class="num">3\.1 時間</td><td class="num">3</td>')
        # Claude Code 以外の区分は時間が分からない(5 列とも割合のほかは --)
        self.assertRegex(html, r'Cowork</span></td><td class="num">10%</td>(<td class="num">--</td>){4}</tr>')
        empty = render_report(report(projects=[], windows=[]), TZ, OPTIONS)
        self.assertIn('<div class="value">0.0 時間</div><div class="delta">Claude Code の利用なし</div>', empty)

    def test_long_hero_value_gets_smaller_font(self):
        # 「100%」が「100」と「%」の 2 行に分かれないよう、4 文字以上は小さい字にする(2026-11-04)
        full = render_report(report([LimitPoint(START + timedelta(hours=3), 100)]), TZ, OPTIONS)
        self.assertIn('<div class="kpi hero long"><div class="label">週次上限の使用率(最後の記録)</div>'
                      '<div class="value">100%</div>', full)
        self.assertIn('<div class="kpi hero"><div class="label">', render_report(report([LimitPoint(START, 41)]), TZ, OPTIONS))
        self.assertIn(".kpi.hero .value { font-size: 48px; line-height: 1.15; white-space: nowrap; }", full)

    def test_table_folds_the_rest_into_one_row(self):
        # 円グラフで個別の区分にしたプロジェクトだけ 1 行ずつ、残りは「その他」の 1 行にまとめる(2026-11-04 ユーザーの了承)
        t0 = START + timedelta(hours=1)
        big = [ProjectUsage(f"p{i}", 100.0 - i, 2, 0.0, timedelta(hours=1)) for i in range(5)]
        small = [ProjectUsage("s1", 4.0, 3, 2.0, timedelta(minutes=20), (t0, t0 + timedelta(minutes=20))),
                 ProjectUsage("s2", 1.0, 1, 0.0, timedelta(minutes=20),
                              (t0 + timedelta(minutes=10), t0 + timedelta(minutes=30))),
                 ProjectUsage("zero", 0.0, 4, 0.0, timedelta(), (t0 + timedelta(minutes=60),))]  # 使用量 0 もまとめる
        options = replace(OPTIONS, dev_time_gap=timedelta(minutes=15))
        html = render_report(report(projects=big + small, windows=[]), TZ, options)
        table = re.search(r"<th>プロジェクト</th>.*?<tbody>(.*?)</tbody>", html, re.S).group(1)
        self.assertEqual(table.count("<tr>"), 6)
        for name in ("s1", "s2", "zero"):
            self.assertNotIn(f"{name}</span>", table)
        # 割合 5/495・換算額の合計・開発時間は並行した分を 1 回(0〜30 分。合計の 40 分ではない)・セッションの合計・サブエージェント 2/5
        self.assertIn(f'fill-others"></span>その他</span> 3 件</td><td class="num">1.0%</td>'
                      f'<td class="num">{fmt_usd(5.0)}</td><td class="num">0.5 時間</td><td class="num">8</td>'
                      f'<td class="num">40%</td></tr>', table)
        self.assertRegex(table, r'fill-s\d"></span>p0</span> </td>')

    def test_fable_tile_shows_the_week_factor(self):
        fable = report(projects=[ProjectUsage("alpha", 10.0, 1, 0.0)],
                       windows=[window(10, {"alpha": 10.0}, by_model={"Fable": 4.0})])
        cases = [(ModelFactor("claude-fable", 3.92, FROM_RECORDS, intervals=31), "倍率 3.9(この週の記録から)"),
                 (ModelFactor("claude-fable", 4.0, FROM_DEFAULT), "倍率 4(既定値)"),
                 (ModelFactor("claude-fable", 6.7, FROM_OBSERVED), "倍率 6.7(画面の値から)")]
        for factor, label in cases:
            with self.subTest(label=label):
                html = render_report(replace(fable, factor=factor), TZ, OPTIONS)
                self.assertIn(f'<div class="delta">全体の 40%・{label}</div>', html)
        with_limit = replace(fable, factor=ModelFactor("claude-fable", 4.0, FROM_DEFAULT),
                             model_limit_points={"Fable": [LimitPoint(START + timedelta(hours=5), 94.0),
                                                           LimitPoint(START + timedelta(hours=2), 40.0)]})
        self.assertIn('<div class="delta">週間上限 94%・全体の 40%・倍率 4(既定値)</div>',
                      render_report(with_limit, TZ, OPTIONS))
        other_model = replace(fable, factor=ModelFactor("claude-opus", 2.0, FROM_DEFAULT))
        self.assertNotIn("倍率", render_report(other_model, TZ, OPTIONS))  # 表示するモデルと違う倍率は出さない

    def test_no_explanations_that_are_always_true(self):
        html = render_report(report(), TZ, OPTIONS)
        self.assertNotIn('class="sub"', html)  # グラフの説明文
        for text in ("claude.ai などの利用も含む", "API の単価比", "階段状", "上位 5 件までを個別に表示"):
            self.assertNotIn(text, html)
        self.assertIn("上限%の記録点数: 0", html)  # 週ごとに変わる事実は残す

    def test_breakable_names(self):
        self.assertEqual(breakable("PokemonChampioons_Adviser"), "PokemonChampioons_​Adviser")
        self.assertEqual(breakable("oshikatsu-3dprint"), "oshikatsu-​3dprint")

    def test_print_styles_are_embedded(self):
        html = render_report(report(), TZ, OPTIONS)
        self.assertIn("@page { size: A4", html)
        self.assertIn("@media print", html)

    def test_escapes_project_names(self):
        evil = '<script>alert("x")</script>'
        r = report(projects=[ProjectUsage(evil, 10, 1, 0)], windows=[window(10, {evil: 10.0}, 50)])
        html = render_report(r, TZ, OPTIONS)
        self.assertNotIn(evil, html)
        self.assertIn("&lt;script&gt;", html)

    def test_demo_scenarios_render(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                html = render_report(build_demo_report(scenario), TZ, OPTIONS)
                self.assertTrue(html.startswith("<!doctype html>"))
                self.assertIn("ダミーデータ", html)
                self.assertIn("その他", html)  # 8 件のうち上位 5 件を超える分がまとめられる
                self.assertNotIn("均等ペース", html)
                for svg in re.findall(r"<svg .*?</svg>", html, flags=re.S):
                    parse_svg(svg)


class DemoTest(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(build_demo_report("dense"), build_demo_report("dense"))

    def test_scenarios(self):
        dense, sparse, none = (build_demo_report(s) for s in SCENARIOS)
        self.assertGreater(len(dense.limit_points), 40)
        self.assertEqual(len(sparse.limit_points), 7)
        self.assertEqual(none.limit_points, [])
        self.assertAlmostEqual(dense.final_pct, 78.0, places=0)
        self.assertAlmostEqual(dense.total_weighted, 1.31e8, delta=1)
        self.assertTrue(all(w.five_hour_pct is not None for w in dense.windows))
        self.assertTrue(any(w.five_hour_pct is None for w in sparse.windows))
        self.assertTrue(any(w.five_hour_pct is not None for w in sparse.windows))
        self.assertTrue(all(w.five_hour_pct is None for w in none.windows))
        fable = dense.model_total("Fable")
        self.assertGreater(fable, 0.2 * dense.total_weighted)  # Fable の割合は各時間 20〜70%
        self.assertLess(fable, 0.7 * dense.total_weighted)
        self.assertTrue(all(set(w.by_model) == {"Fable"} for w in dense.windows))

    def test_windows_are_five_hours_and_do_not_overlap(self):
        windows = build_demo_report("dense").windows
        self.assertGreater(len(windows), 10)
        for a, b in zip(windows, windows[1:]):
            self.assertLessEqual(a.end, b.start)
        for w in windows[:-1]:
            self.assertEqual(w.end - w.start, timedelta(hours=5))
        self.assertTrue(all(0 <= w.five_hour_pct <= 100 for w in windows))
        total = sum(sum(w.by_project.values()) for w in windows)
        self.assertAlmostEqual(total, 1.31e8, delta=1)

    def test_unknown_scenario(self):
        with self.assertRaises(ValueError):
            build_demo_report("nope")


if __name__ == "__main__":
    unittest.main()
