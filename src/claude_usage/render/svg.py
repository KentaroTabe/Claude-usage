"""グラフの SVG を文字列で組み立てる。

- 時系列のグラフ(週次上限%・週の使用量・5 時間枠ごとの使用量)は縦に並べ、横軸(時刻)の位置をそろえる。
  日付の区切り線はどれも同じ時刻(タイムゾーンの 0 時)に引き、どのグラフにも横軸の日付を書く。
  線グラフは記録点をなめらかにつなぐ。
- 下段だけは利用者の指定により 2 つの縦軸を持つ(左: API 換算額、右: 5 時間枠の使用率)。
  読み違えを減らすため、左軸の目盛りを 4 等分にして右軸の 0/25/50/75/100% と同じ横線にそろえ、両軸に見出しを付ける。
- 色は CSS のクラス(fill-s0〜s7 / fill-others)で指定し、ライト/ダークの切り替えは CSS 側で行う。
- 文字列はすべて esc() を通す(プロジェクト名はディレクトリ名に由来する外部入力のため)。
- ここにある数値はグラフの見た目(余白・太さ)の定数で、集計結果を左右するパラメータは設定ファイル側に置く。
"""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timedelta, tzinfo
from typing import Sequence

from ..metrics import Slice, cumulative, fold_to_slices
from ..model import LimitPoint, WeekReport, WindowBucket

WEEKDAYS = "月火水木金土日"
N_SLOTS = 8  # CSS に定義した分類色の数

WIDTH = 720
MARGIN_L = 64
MARGIN_R = 76
PAD_T = 16
LIMIT_H = 244        # 週次上限の使用率のグラフ
USAGE_H = 264        # 週の使用量(累積)のグラフ
WINDOWS_H = 236
WINDOWS_PAD_T = 28   # 軸の見出しの分
AXIS_B = 36          # 横軸の日付の分
END_LABEL_GAP = 16   # 線の終わりの値どうしが重ならない縦の間隔
END_LABEL_NEAR_X = 48  # 線の終わりがこれより近い横位置にあるときだけ、値の縦位置をずらす
BAR_MAX_W = 24
BAR_FILL_RATIO = 0.7  # 5 時間枠の幅のうち棒が占める割合
BAR_GAP = 2
BAR_RADIUS = 4
MARKER_R = 4
HIT_R = 12  # 点のツールチップの当たり判定(直径 24 以上)
TICK_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)  # 使用量の目盛り(上限の 4 等分)
MIN_LABEL_HOURS = 12  # 日付ラベルを出す最小の幅(週の端の短い日には出さない)
DONUT_SIZE = 240
DONUT_R_OUT = 108
DONUT_R_IN = 70


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def fmt_usd(n: float) -> str:
    """API 換算額(ドル)を短く表す。例: 2849.3 → $2,849 / 45.2 → $45 / 4.56 → $4.6 / 0.123 → $0.12 / 0 → $0"""
    if n == 0:
        return "$0"
    if n >= 10:
        return f"${n:,.0f}"
    if n >= 1:
        return f"${n:.1f}"
    return f"${n:.2f}"


def fmt_hours(t: timedelta) -> str:
    """開発時間を時間の単位で表す。例: 19.1 時間"""
    return f"{t.total_seconds() / 3600:.1f} 時間"


def fmt_share(pct: float) -> str:
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def day_label(dt: datetime, tz: tzinfo) -> str:
    d = dt.astimezone(tz)
    return f"{d.month}/{d.day} {WEEKDAYS[d.weekday()]}"


def time_label(dt: datetime, tz: tzinfo) -> str:
    d = dt.astimezone(tz)
    return f"{d.month}/{d.day} {WEEKDAYS[d.weekday()]} {d:%H:%M}"


def window_label(w: WindowBucket, tz: tzinfo) -> str:
    return f"{time_label(w.start, tz)}–{w.end.astimezone(tz):%H:%M}"


def fraction_ticks(max_value: float) -> list[float]:
    """上限 max_value(これまでの最大値)をちょうど 4 等分した目盛り。一番上が max_value になる。

    きりのよい値に切り上げない(上限が最大値より上に余白を持たないように)。2 つの縦軸を持つグラフでは、
    左右とも同じ割合(0・1/4・1/2・3/4・1)で目盛りを切ると横線がそろう。
    """
    top = max_value if max_value > 0 else 1.0
    return [top * f for f in TICK_FRACTIONS]


class XScale:
    def __init__(self, start: datetime, end: datetime):
        self.start, self.end = start, end
        self.left, self.right = MARGIN_L, WIDTH - MARGIN_R

    def __call__(self, dt: datetime) -> float:
        frac = (dt - self.start) / (self.end - self.start)
        return self.left + min(max(frac, 0.0), 1.0) * (self.right - self.left)


def midnights(start: datetime, end: datetime, tz: tzinfo) -> list[datetime]:
    """start と end の間にある、タイムゾーン tz の 0 時の一覧(start・end ちょうどは含めない)。"""
    local = start.astimezone(tz)
    d = local.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while True:
        d = (d + timedelta(days=1, hours=2)).replace(hour=0)  # 夏時間でも次の日の 0 時に進むように
        if d >= end:
            return out
        if d > start:
            out.append(d)


def day_label_positions(start: datetime, end: datetime, tz: tzinfo) -> list[tuple[datetime, datetime]]:
    """各日の区間(週の端は切り詰める)のうち、ラベルを出せる幅があるもの。(区間の中央, その日の 0 時) を返す。"""
    bounds = [start, *midnights(start, end, tz), end]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= timedelta(hours=MIN_LABEL_HOURS):
            out.append((a + (b - a) / 2, a))
    return out


def _day_gridlines(xs: XScale, tz: tzinfo, top: float, bottom: float) -> list[str]:
    return [f'<line class="grid" x1="{_num(xs(d))}" x2="{_num(xs(d))}" y1="{_num(top)}" y2="{_num(bottom)}"/>'
            for d in midnights(xs.start, xs.end, tz)]


def _day_labels(xs: XScale, tz: tzinfo, height: float) -> list[str]:
    """横軸の日付(例「10/21 水」)。各日の区間の中央に置く(週の端の短い日には出さない)。"""
    return [f'<text class="tick" x="{_num(xs(mid))}" y="{_num(height - AXIS_B + 20)}" text-anchor="middle">'
            f'{esc(day_label(day, tz))}</text>' for mid, day in day_label_positions(xs.start, xs.end, tz)]


def end_label_ys(ends: Sequence[tuple[float, float]]) -> list[float]:
    """線の終わりの値を書く縦位置(文字の基準線)。ends は線ごとの終わりの点 (x, y)。最初の線の値は動かさない。

    後の線の値は、それより前の値と横位置が近く(END_LABEL_NEAR_X 未満)縦に重なる(END_LABEL_GAP 未満)とき、
    自分の線のある側(上か下)へ END_LABEL_GAP 離す。
    """
    out: list[float] = []
    for x, y in ends:
        label = y + 5
        for (px, py), placed in zip(ends, out):
            if abs(x - px) < END_LABEL_NEAR_X and abs(label - placed) < END_LABEL_GAP:
                label = placed - END_LABEL_GAP if y < py or (y == py and label <= placed) else placed + END_LABEL_GAP
        out.append(label)
    return out


def smooth_path(coords: Sequence[tuple[float, float]]) -> str:
    """点をなめらかな曲線でつなぐ(単調な 3 次エルミート補間 = Fritsch–Carlson 法を 3 次ベジェ曲線で描く)。

    点と点の間で行き過ぎたり戻ったりしない補間を使う(累積や使用率が、記録の間で記録より大きく・小さく見えない)。
    横ばいの区間は横ばいのまま描き、値が下がる区間(手動リセット)も行き過ぎずに下がる。
    """
    pts: list[tuple[float, float]] = []
    for x, y in coords:
        if pts and x <= pts[-1][0]:
            pts[-1] = (pts[-1][0], y)  # 同じ横位置の点は後の値を使う
        else:
            pts.append((x, y))
    if len(pts) < 2:
        return f"M{_num(pts[0][0])},{_num(pts[0][1])}" if pts else ""
    n = len(pts)
    secants = [(pts[i + 1][1] - pts[i][1]) / (pts[i + 1][0] - pts[i][0]) for i in range(n - 1)]
    tangents = [secants[0]] + [
        (secants[i - 1] + secants[i]) / 2 if secants[i - 1] * secants[i] > 0 else 0.0 for i in range(1, n - 1)
    ] + [secants[-1]]
    for i, m in enumerate(secants):
        if m == 0:
            tangents[i] = tangents[i + 1] = 0.0
            continue
        a, b = tangents[i] / m, tangents[i + 1] / m
        if a * a + b * b > 9:  # 行き過ぎを防ぐための傾きの制限
            tau = 3 / math.sqrt(a * a + b * b)
            tangents[i], tangents[i + 1] = tau * a * m, tau * b * m
    parts = [f"M{_num(pts[0][0])},{_num(pts[0][1])}"]
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        h = (x1 - x0) / 3
        parts.append(f"C{_num(x0 + h)},{_num(y0 + tangents[i] * h)} {_num(x1 - h)},{_num(y1 - tangents[i + 1] * h)} "
                     f"{_num(x1)},{_num(y1)}")
    return " ".join(parts)


def value_at(points: Sequence[tuple[datetime, float]], ts: datetime) -> float | None:
    """ts 時点で分かっている値(ts 以前の最後の点の値)。"""
    before = [v for t, v in points if t <= ts]
    return before[-1] if before else None


def limit_chart(report: WeekReport, tz: tzinfo, marker_max_points: int, models: Sequence[str] = ()) -> str:
    """週次上限の使用率(0–100%)を、記録した点をなめらかにつないだ線で描く。記録点が marker_max_points 以下なら点も描く。

    models に挙げたモデルの週間上限の使用率(report.model_limit_points)があれば、薄い線で重ねる
    (どちらもそれぞれの上限に対する % なので、同じ軸で読める)。
    """
    xs = XScale(report.week_start, report.week_end)
    top, bottom = PAD_T, LIMIT_H - AXIS_B

    def y(pct: float) -> float:
        return bottom - min(max(pct, 0.0), 100.0) / 100 * (bottom - top)

    def in_week(pts: Sequence[LimitPoint]) -> list[tuple[datetime, float]]:
        return sorted((p.ts, p.pct) for p in pts if report.week_start <= p.ts <= report.week_end)

    points = in_week(report.limit_points)
    model_pts = {m: pts for m in models if (pts := in_week(report.model_limit_points.get(m, [])))}
    label = f"週次上限の使用率。最終 {points[-1][1]:.0f}%" if points else "週次上限の使用率。記録なし"
    label += "".join(f"・{m} {pts[-1][1]:.0f}%" for m, pts in model_pts.items())
    out = [f'<svg class="chart chart-limit" viewBox="0 0 {WIDTH} {LIMIT_H}" role="img" aria-label="{esc(label)}">']
    out += _day_gridlines(xs, tz, top, bottom)
    out += _day_labels(xs, tz, LIMIT_H)
    for pct in (0, 25, 50, 75, 100):
        out.append(f'<line class="{"axis" if pct == 0 else "grid"}" x1="{xs.left}" x2="{xs.right}" '
                   f'y1="{_num(y(pct))}" y2="{_num(y(pct))}"/>')
        out.append(f'<text class="tick" x="{xs.left - 8}" y="{_num(y(pct) + 4)}" text-anchor="end">{pct}%</text>')
    if not points:
        out.append(f'<text class="empty" x="{_num((xs.left + xs.right) / 2)}" y="{_num((top + bottom) / 2)}" '
                   'text-anchor="middle">この週の上限%の記録はありません</text></svg>')
        return "".join(out)

    lines = [("limit", points)] + [("model", pts) for pts in model_pts.values()]
    for kind, pts in reversed(lines):  # 全体の線を手前に描く
        if len(pts) >= 2:
            out.append(f'<path class="{kind}-line" d="{smooth_path([(xs(t), y(v)) for t, v in pts])}"/>')
    for kind, pts in reversed(lines):
        for t, v in (pts if len(pts) <= marker_max_points else pts[-1:]):
            out.append(f'<circle class="{kind}-marker" cx="{_num(xs(t))}" cy="{_num(y(v))}" r="{MARKER_R}"/>')
    ends = [pts[-1] for _, pts in lines]
    for (kind, _), (t, v), label_y in zip(lines, ends, end_label_ys([(xs(t), y(v)) for t, v in ends])):
        cls = "end-label" if kind == "limit" else "end-label secondary"
        out.append(f'<text class="{cls}" x="{_num(xs(t) + 10)}" y="{_num(label_y)}">{v:.0f}%</text>')

    hover = []
    for t, v in points:
        values = [f"{v:.0f}%"] if not model_pts else [f"全体 {v:.0f}%"] + [
            f"{m} {known:.0f}%" for m, pts in model_pts.items() if (known := value_at(pts, t)) is not None]
        hover.append({"x": round(xs(t), 1), "y": round(y(v), 1), "v": " / ".join(values), "t": time_label(t, tz)})
    out.append(f'<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{bottom}"/>')
    out.append(f'<circle class="crosshair-dot" cx="0" cy="0" r="{MARKER_R}"/>')
    out.append(f'<rect class="hover-target" x="{xs.left}" y="{top}" width="{xs.right - xs.left}" '
               f'height="{bottom - top}" data-points="{esc(json.dumps(hover, ensure_ascii=False))}"/>')
    out.append("</svg>")
    return "".join(out)


def usage_chart(report: WeekReport, models: Sequence[str], tz: tzinfo, total_max: float, model_max: float) -> str:
    """週の使用量(API 換算額の累積)のなめらかな線。全体(濃い線・左軸)とモデル別(薄い線・右軸)を同じ図に描く。

    - 2 つの縦軸は利用者の指定。読み違いを減らすため、横線は左右とも上限の 4 等分にそろえ、両軸の上に見出しを付ける。
    - 各軸の上限は、これまでの最大値ちょうど(total_max / model_max。グラフの一番上がその値になる)。
    - モデルが複数あるときは右軸を共有する(上限はモデル別の最大値のうち最も大きいもの)。
    """
    xs = XScale(report.week_start, report.week_end)
    top, bottom = WINDOWS_PAD_T, USAGE_H - AXIS_B
    total_pts = cumulative(report)
    model_pts = {m: cumulative(report, m) for m in models}
    total_max = max(total_max, total_pts[-1][1] if total_pts else 0.0) or 1.0
    model_max = max([model_max] + [p[-1][1] for p in model_pts.values() if p])
    right = bool(models) and model_max > 0

    def y_left(v: float) -> float:
        return bottom - min(max(v, 0.0), total_max) / total_max * (bottom - top)

    def y_right(v: float) -> float:
        return bottom - min(max(v, 0.0), model_max) / model_max * (bottom - top)

    label = f"週の使用量(API 換算額の累積)。合計 {fmt_usd(report.total_weighted)}"
    out = [f'<svg class="chart chart-usage" viewBox="0 0 {WIDTH} {USAGE_H}" role="img" aria-label="{esc(label)}">']
    out += _day_gridlines(xs, tz, top, bottom)
    out += _day_labels(xs, tz, USAGE_H)
    for i, frac in enumerate(TICK_FRACTIONS):
        yy = bottom - frac * (bottom - top)
        out.append(f'<line class="{"axis" if i == 0 else "grid"}" x1="{xs.left}" x2="{xs.right}" y1="{_num(yy)}" y2="{_num(yy)}"/>')
        out.append(f'<text class="tick" x="{xs.left - 8}" y="{_num(yy + 4)}" text-anchor="end">'
                   f'{esc(fmt_usd(total_max * frac))}</text>')
        if right:
            out.append(f'<text class="tick" x="{xs.right + 8}" y="{_num(yy + 4)}">{esc(fmt_usd(model_max * frac))}</text>')
    out.append(f'<text class="axis-title" x="{xs.left - 8}" y="{top - 12}" text-anchor="end">全体</text>')
    if right:
        out.append(f'<text class="axis-title" x="{xs.right + 8}" y="{top - 12}">{esc("・".join(models))}</text>')
    if not total_pts:
        out.append(f'<text class="empty" x="{_num((xs.left + xs.right) / 2)}" y="{_num((top + bottom) / 2)}" '
                   'text-anchor="middle">この週の Claude Code の使用はありません</text></svg>')
        return "".join(out)

    lines = [("limit", total_pts, y_left)] + [("model", p, y_right) for p in model_pts.values() if p and right]
    for kind, pts, y in lines:
        out.append(f'<path class="{kind}-line" d="{smooth_path([(xs(t), y(v)) for t, v in pts])}"/>')
        t, v = pts[-1]
        out.append(f'<circle class="{kind}-marker" cx="{_num(xs(t))}" cy="{_num(y(v))}" r="{MARKER_R}"/>')

    hover = []
    for t, v in total_pts:
        values = [f"全体 {fmt_usd(v)}"] + [f"{m} {fmt_usd(value_at(p, t) or 0.0)}"
                                              for m, p in model_pts.items() if right]
        hover.append({"x": round(xs(t), 1), "y": round(y_left(v), 1), "v": " / ".join(values), "t": time_label(t, tz)})
    out.append(f'<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{bottom}"/>')
    out.append(f'<circle class="crosshair-dot" cx="0" cy="0" r="{MARKER_R}"/>')
    out.append(f'<rect class="hover-target" x="{xs.left}" y="{top}" width="{xs.right - xs.left}" '
               f'height="{bottom - top}" data-points="{esc(json.dumps(hover, ensure_ascii=False))}"/>')
    out.append("</svg>")
    return "".join(out)


def _rounded_top(x0: float, x1: float, y_top: float, y_bottom: float, r: float) -> str:
    r = min(r, (x1 - x0) / 2, y_bottom - y_top)
    return (f"M{_num(x0)},{_num(y_bottom)} V{_num(y_top + r)} A{_num(r)},{_num(r)} 0 0 1 {_num(x0 + r)},{_num(y_top)} "
            f"H{_num(x1 - r)} A{_num(r)},{_num(r)} 0 0 1 {_num(x1)},{_num(y_top + r)} V{_num(y_bottom)} Z")


def fill_class(s: Slice) -> str:
    if s.non_code:
        return "fill-noncode" if s.shade == 0 else "fill-noncode-2"
    return "fill-others" if s.slot is None else f"fill-s{s.slot}"


def windows_chart(report: WeekReport, slices: Sequence[Slice], tz: tzinfo, axis_max: float | None = None) -> str:
    """5 時間枠ごとの API 換算額(左軸、プロジェクト別の積み上げ棒)と、5 時間枠の使用率(右軸、点)。

    axis_max は左軸の上限(全レポート共通にするための、これまでの 5 時間枠の最大値)。省くとこの週の最大値。
    """
    xs = XScale(report.week_start, report.week_end)
    top, bottom = WINDOWS_PAD_T, WINDOWS_H - AXIS_B
    windows = [w for w in report.windows if w.end > report.week_start and w.start < report.week_end]
    per_window = [(w, fold_to_slices(w.by_project, slices)) for w in windows]
    ticks = fraction_ticks(max([sum(v) for _, v in per_window] + [axis_max or 0.0]))
    y_max = ticks[-1]

    def y_tok(v: float) -> float:
        return bottom - v / y_max * (bottom - top)

    def y_pct(p: float) -> float:
        return bottom - min(max(p, 0.0), 100.0) / 100 * (bottom - top)

    out = [f'<svg class="chart chart-windows" viewBox="0 0 {WIDTH} {WINDOWS_H}" role="img" '
           f'aria-label="5 時間枠ごとの API 換算額(左軸)と 5 時間枠の使用率(右軸)">']
    out += _day_gridlines(xs, tz, top, bottom)
    for i, t in enumerate(ticks):
        cls = "axis" if i == 0 else "grid"
        yy = _num(y_tok(t))
        out.append(f'<line class="{cls}" x1="{xs.left}" x2="{xs.right}" y1="{yy}" y2="{yy}"/>')
        out.append(f'<text class="tick" x="{xs.left - 8}" y="{_num(y_tok(t) + 4)}" text-anchor="end">'
                   f'{esc(fmt_usd(t))}</text>')
        out.append(f'<text class="tick" x="{xs.right + 8}" y="{_num(y_tok(t) + 4)}">{i * 25}%</text>')
    out.append(f'<text class="axis-title" x="{xs.left - 8}" y="{top - 12}" text-anchor="end">換算額</text>')
    out.append(f'<text class="axis-title" x="{xs.right + 8}" y="{top - 12}">5時間枠</text>')

    out += _day_labels(xs, tz, WINDOWS_H)

    dots = []
    for w, values in per_window:
        x0w, x1w = xs(w.start), xs(w.end)
        cx = (x0w + x1w) / 2
        width = min(BAR_MAX_W, (x1w - x0w) * BAR_FILL_RATIO)
        x0, x1 = cx - width / 2, cx + width / 2
        label = window_label(w, tz)
        segs = [(s, v) for s, v in zip(slices, values) if v > 0]
        cum = 0.0
        for j, (s, v) in enumerate(segs):
            y_bottom = y_tok(cum) - (BAR_GAP if j > 0 else 0)
            y_top = y_tok(cum + v)
            cum += v
            if y_bottom - y_top < 0.5:
                continue  # 細すぎて描けない区間は表とツールチップに任せる
            tip = f'data-tip-value="{esc(fmt_usd(v))}" data-tip-label="{esc(s.name)} · {esc(label)}"'
            cls = f'mark {fill_class(s)}'
            if j == len(segs) - 1:
                out.append(f'<path class="{cls}" d="{_rounded_top(x0, x1, y_top, y_bottom, BAR_RADIUS)}" '
                           f'tabindex="0" {tip}/>')
            else:
                out.append(f'<rect class="{cls}" x="{_num(x0)}" y="{_num(y_top)}" width="{_num(width)}" '
                           f'height="{_num(y_bottom - y_top)}" tabindex="0" {tip}/>')
        if w.five_hour_pct is not None:
            cy = _num(y_pct(w.five_hour_pct))
            dots.append(f'<circle class="window-pct" cx="{_num(cx)}" cy="{cy}" r="{MARKER_R}"/>')
            # 点は小さいので、ツールチップ用に透明な大きめの当たり判定を重ねる
            dots.append(f'<circle class="mark hit" cx="{_num(cx)}" cy="{cy}" r="{HIT_R}" tabindex="0" '
                        f'data-tip-value="{w.five_hour_pct:.0f}%" '
                        f'data-tip-label="5時間枠の使用率 · {esc(label)} · {esc(fmt_usd(sum(values)))}"/>')
    out += dots  # 点は棒より手前に描く
    out.append("</svg>")
    return "".join(out)


def _polar(r: float, deg: float) -> tuple[float, float]:
    c = DONUT_SIZE / 2
    rad = math.radians(deg)
    return c + r * math.cos(rad), c + r * math.sin(rad)


def _sector(a0: float, a1: float) -> str:
    large = 1 if a1 - a0 > 180 else 0
    (ox0, oy0), (ox1, oy1) = _polar(DONUT_R_OUT, a0), _polar(DONUT_R_OUT, a1)
    (ix1, iy1), (ix0, iy0) = _polar(DONUT_R_IN, a1), _polar(DONUT_R_IN, a0)
    return (f"M{_num(ox0)},{_num(oy0)} A{DONUT_R_OUT},{DONUT_R_OUT} 0 {large} 1 {_num(ox1)},{_num(oy1)} "
            f"L{_num(ix1)},{_num(iy1)} A{DONUT_R_IN},{DONUT_R_IN} 0 {large} 0 {_num(ix0)},{_num(iy0)} Z")


def _full_ring() -> str:
    c, ro, ri = DONUT_SIZE / 2, DONUT_R_OUT, DONUT_R_IN
    return (f"M{c + ro},{c} A{ro},{ro} 0 1 1 {c - ro},{c} A{ro},{ro} 0 1 1 {c + ro},{c} Z "
            f"M{c + ri},{c} A{ri},{ri} 0 1 0 {c - ri},{c} A{ri},{ri} 0 1 0 {c + ri},{c} Z")


def donut_chart(slices: Sequence[Slice], total: float) -> str:
    """区分ごとの割合のドーナツ図。中央は Claude Code の API 換算額の合計 total。

    Claude Code 以外の区分(Cowork など)は使用量が分からないので、ツールチップには割合だけを出す。
    """
    c = DONUT_SIZE / 2
    out = [f'<svg class="chart chart-donut" viewBox="0 0 {DONUT_SIZE} {DONUT_SIZE}" role="img" '
           f'aria-label="プロジェクト別の割合">']
    if not slices:
        out.append(f'<path class="donut-track" d="{_full_ring()}" fill-rule="evenodd"/>')
        out.append(f'<text class="empty" x="{c}" y="{c + 5}" text-anchor="middle">データなし</text></svg>')
        return "".join(out)
    angle = -90.0
    for s in slices:
        sweep = s.share_pct / 100 * 360
        d = _full_ring() if len(slices) == 1 else _sector(angle, angle + sweep)
        label = s.name if s.slot is not None or s.non_code else f"{s.name}({'・'.join(s.members)})"
        value = fmt_share(s.share_pct) if s.non_code else f"{fmt_share(s.share_pct)} · {fmt_usd(s.weighted)}"
        out.append(f'<path class="mark donut-seg {fill_class(s)}" d="{d}" fill-rule="evenodd" tabindex="0" '
                   f'data-tip-value="{esc(value)}" data-tip-label="{esc(label)}"/>')
        angle += sweep
    caption = "Claude Code 分" if any(s.non_code for s in slices) else "API 換算額"
    out.append(f'<text class="donut-total" x="{c}" y="{c + 4}" text-anchor="middle">{esc(fmt_usd(total))}</text>')
    out.append(f'<text class="donut-caption" x="{c}" y="{c + 26}" text-anchor="middle">{caption}</text>')
    out.append("</svg>")
    return "".join(out)
