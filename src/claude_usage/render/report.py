"""週次レポートの HTML を組み立てる。CSS と JS は assets/ から読み込んでファイルに埋め込み、1 ファイルで完結させる。"""
from __future__ import annotations

from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Mapping, Sequence

from ..config import ReportOptions
from ..metrics import OTHERS, Slice, axis_for, fold_to_slices, group_dev_time, group_projects, with_non_code
from ..model import FROM_DEFAULT, FROM_OBSERVED, FROM_RECORDS, Axis, WeekReport
from .svg import (
    N_SLOTS,
    WEEKDAYS,
    donut_chart,
    esc,
    fill_class,
    fmt_hours,
    fmt_usd,
    fmt_share,
    limit_chart,
    time_label,
    usage_chart,
    window_label,
    windows_chart,
)

ASSETS = Path(__file__).resolve().parent / "assets"


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def breakable(name: str) -> str:
    """_ と - の後に幅のない改行位置(U+200B)を入れる。長いプロジェクト名が欄からはみ出さないようにするため。"""
    return name.replace("_", "_​").replace("-", "-​")


FACTOR_SOURCES = {FROM_RECORDS: "この週の記録から", FROM_OBSERVED: "画面の値から", FROM_DEFAULT: "既定値"}


def factor_label(report: WeekReport, prefix: str) -> str:
    """モデルの倍率の表示(例「倍率 3.9(この週の記録から)」)。そのモデルの倍率がなければ空。"""
    f = report.factor
    if f is None or f.model != prefix:
        return ""
    value = f"{f.value:.1f}".rstrip("0").rstrip(".")
    return f"倍率 {value}({FACTOR_SOURCES.get(f.source, f.source)})"


def _kpis(report: WeekReport, slices: Sequence[Slice], tz: tzinfo, models: Mapping[str, str]) -> str:
    final = report.final_pct
    if final is None:
        hero_value, hero_delta = "--", "この週の記録なし"
    else:
        hero_value = f"{final:.0f}%"
        last = max(report.limit_points, key=lambda p: p.ts)
        prev = ("前週の記録なし" if report.prev_final_pct is None
                else f"前週 {report.prev_final_pct:.0f}% から {final - report.prev_final_pct:+.0f}pt")
        hero_delta = f"{time_label(last.ts, tz)} の記録・{prev}"
    # 最も使ったプロジェクトは、長い名前でも収まるよう独立した欄にせず、合計の欄の補足に書く
    top = next((s for s in slices if s.slot is not None), None)
    top_delta = f"最多: {breakable(top.name)}({fmt_share(top.share_pct)})" if top else "Claude Code の利用なし"
    tiles = [("hero long" if len(hero_value) >= 4 else "hero", "週次上限の使用率(最後の記録)", hero_value, hero_delta),
             ("", "API 換算額(Claude Code)", fmt_usd(report.total_weighted), top_delta)]
    for name, prefix in models.items():
        total = report.model_total(name)
        share = f"全体の {fmt_share(total / report.total_weighted * 100)}" if report.total_weighted else ""
        limit = report.model_limits.get(name)
        used = f"週間上限 {limit.pct:.0f}%" if limit else ""
        delta = "・".join(p for p in (used, share, factor_label(report, prefix)) if p) or "API 換算額"
        tiles.append(("", f"{name} の使用量", fmt_usd(total), delta))
    longest = max(report.projects, key=lambda p: p.dev_time, default=None)
    tiles.append(("", "開発時間", fmt_hours(report.dev_time),
                  f"最多: {breakable(longest.name)}({fmt_hours(longest.dev_time)})" if longest else "Claude Code の利用なし"))
    cells = "".join(f'<div class="kpi {cls}"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
                    f'<div class="delta">{esc(delta)}</div></div>' for cls, label, value, delta in tiles)
    return f'<section class="kpis">{cells}</section>'


def _legend(slices: Sequence[Slice]) -> str:
    items = "".join(f'<li><span class="sw {fill_class(s)}"></span>{esc(s.name)}</li>' for s in slices)
    items += '<li><span class="dot-key"></span>5時間枠の使用率(右軸)</li>'
    return f'<ul class="legend" aria-label="凡例">{items}</ul>'


def _project_table(report: WeekReport, slices: Sequence[Slice], gap: timedelta) -> str:
    """slices は with_non_code の結果。割合は Claude Code 以外も含めた週全体に対する値。

    円グラフで個別の区分にしたプロジェクトを 1 行ずつ、残り(「その他」と使用量 0 のもの)を 1 行にまとめる。
    プロジェクトが多い週に表が長くなり、PDF でページが空くのを防ぐため(2026-11-04 ユーザーの了承)。
    """
    non_code = [s for s in slices if s.non_code]
    code_pct = 100 - sum(s.share_pct for s in non_code)
    total = report.total_weighted or 1
    by_name = {p.name: p for p in report.projects}
    shown = [s for s in slices if s.slot is not None]
    rest = [p for p in report.projects if p.name not in {s.name for s in shown}]
    others = next((s for s in slices if s.slot is None and not s.non_code), None)
    groups = [(fill_class(s), s.name, "", [by_name[s.name]]) for s in shown if s.name in by_name]
    if rest:
        groups.append((fill_class(others) if others else "fill-others", OTHERS, f"{len(rest)} 件", rest))
    rows = []
    for fill, label, extra, members in groups:
        weighted = sum(p.weighted for p in members)
        sub_weighted = sum(p.subagent_weighted for p in members)
        sub = f"{sub_weighted / weighted * 100:.0f}%" if weighted else "--"
        dev = members[0].dev_time if len(members) == 1 else group_dev_time(members, gap)
        rows.append(f'<tr><td><span class="name"><span class="sw {fill}"></span>{esc(label)}</span> {esc(extra)}</td>'
                    f'<td class="num">{fmt_share(weighted / total * code_pct)}</td>'
                    f'<td class="num">{esc(fmt_usd(weighted))}</td><td class="num">{esc(fmt_hours(dev))}</td>'
                    f'<td class="num">{sum(p.sessions for p in members)}</td><td class="num">{sub}</td></tr>')
    for s in non_code:  # 使用量・セッションは分からない
        rows.append(f'<tr><td><span class="name"><span class="sw {fill_class(s)}"></span>{esc(s.name)}</span></td>'
                    f'<td class="num">{fmt_share(s.share_pct)}</td><td class="num">--</td><td class="num">--</td>'
                    f'<td class="num">--</td><td class="num">--</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>プロジェクト</th><th>割合</th><th>API 換算額</th>'
            '<th>開発時間</th><th>セッション</th><th>サブエージェント</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _windows_table(report: WeekReport, slices: Sequence[Slice], tz: tzinfo, models: Sequence[str]) -> str:
    head = "".join(f'<th><span class="sw {fill_class(s)}"></span>{esc(s.name)}</th>' for s in slices)
    head += "".join(f"<th>{esc(name)}</th>" for name in models)
    rows = []
    for w in sorted(report.windows, key=lambda w: w.start):
        values = fold_to_slices(w.by_project, slices)
        pct = "--" if w.five_hour_pct is None else f"{w.five_hour_pct:.0f}%"
        cells = "".join(f'<td class="num">{esc(fmt_usd(v))}</td>' for v in values)
        cells += "".join(f'<td class="num">{esc(fmt_usd(w.by_model.get(name, 0.0)))}</td>' for name in models)
        rows.append(f'<tr><td>{esc(window_label(w, tz))}</td><td class="num">{esc(fmt_usd(sum(values)))}</td>'
                    f'<td class="num">{pct}</td>{cells}</tr>')
    if not rows:
        rows.append(f'<tr><td>記録なし</td><td colspan="{len(slices) + len(models) + 2}"></td></tr>')
    return (f'<div class="table-wrap"><table><thead><tr><th>5時間枠</th><th>合計</th><th>使用率</th>{head}</tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table></div>')


def _limit_models(report: WeekReport, models: Sequence[str]) -> list[str]:
    """使用率のグラフに重ねるモデル(その週にモデル別の週間上限の % の記録があるもの)。"""
    return [m for m in models if report.model_limit_points.get(m)]


def _limit_table(report: WeekReport, tz: tzinfo, models: Sequence[str]) -> str:
    """使用率の記録の表。モデル別の週間上限の % があれば列を足す(記録の時刻が違うので、ない欄は --)。"""
    shown = _limit_models(report, models)
    columns = [{p.ts: p.pct for p in report.limit_points}] + [{p.ts: p.pct for p in report.model_limit_points[m]}
                                                             for m in shown]
    rows = "".join(f'<tr><td>{esc(time_label(ts, tz))}</td>'
                   + "".join(f'<td class="num">{"--" if ts not in c else f"{c[ts]:.0f}%"}</td>' for c in columns)
                   + '</tr>' for ts in sorted(set().union(*columns)))
    if not rows:
        rows = f'<tr><td>記録なし</td><td colspan="{len(columns)}"></td></tr>'
    head = "<th>使用率</th>" if not shown else "<th>全体</th>" + "".join(f"<th>{esc(m)}</th>" for m in shown)
    return (f'<div class="table-wrap"><table><thead><tr><th>記録時刻</th>{head}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _usage_section(report: WeekReport, tz: tzinfo, models: Sequence[str], axis: Axis) -> str:
    model_max = max(axis.models.values(), default=0.0)
    keys = "".join(f'<li><span class="line-key secondary"></span>{esc(m)}(右軸)</li>' for m in models)
    legend = f'<ul class="legend"><li><span class="line-key"></span>全体(左軸)</li>{keys}</ul>' if models else ""
    return (f'<div class="keep"><h2>週の使用量(Claude Code)</h2>'
            f'<div class="chart-scroll">{usage_chart(report, models, tz, axis.week_total, model_max)}</div>'
            f'{legend}</div>')


def _limit_section(report: WeekReport, tz: tzinfo, options: ReportOptions) -> str:
    """週次上限の使用率のグラフ(全体と、記録があればモデル別)。全体の記録が min_limit_hours 時間分以上ある週だけ載せる。"""
    if report.limit_hours < options.min_limit_hours or not report.limit_points:
        return ""
    shown = _limit_models(report, list(options.models))
    keys = "".join(f'<li><span class="line-key secondary"></span>{esc(m)}</li>' for m in shown)
    legend = f'<ul class="legend"><li><span class="line-key"></span>全体</li>{keys}</ul>' if shown else ""
    return (f'<div class="keep"><h2>週次上限の使用率</h2>'
            f'<div class="chart-scroll">{limit_chart(report, tz, options.marker_max_points, shown)}</div>'
            f'{legend}</div><div class="chart-gap"></div>')


def render_report(report: WeekReport, tz: tzinfo, options: ReportOptions,
                  preferred_slots: Mapping[str, int] | None = None, axis: Axis | None = None) -> str:
    """preferred_slots は過去の週で使ったプロジェクトの色枠(週をまたいで同じ色にするため)。

    axis は使用量の縦軸の上限(これまでの最大値)。省くとこの週の値から決める(ダミーデータなど)。
    """
    axis = axis or axis_for([report], options.models)
    slices = group_projects(report.projects, options.max_slices, options.others_min_pct, N_SLOTS, preferred_slots)
    shares = with_non_code(slices, report.non_code)  # 円グラフと表は Claude Code 以外も含めた週全体の割合
    start = report.week_start.astimezone(tz)
    end = report.week_end.astimezone(tz)
    period = (f"{start:%Y-%m-%d}({WEEKDAYS[start.weekday()]}){start:%H:%M} → "
              f"{end:%Y-%m-%d}({WEEKDAYS[end.weekday()]}){end:%H:%M}")
    title = f"Claude 使用量 週次レポート {start:%m/%d}–{end:%m/%d}"
    banner = ('<p class="banner">ダミーデータによる表示確認用のレポートです。数値は実際の使用量ではありません。</p>'
              if report.is_demo else "")
    models = list(options.models)
    notes = [
        f"上限%の記録点数: {len(report.limit_points)} / 解釈できなかったログの行: {report.unparsed_lines}",
        *report.notes,
    ]
    if 0 < report.limit_hours < options.min_limit_hours:
        notes.append(f"この週は上限%の記録が {report.limit_hours} 時間分しかないため、% のグラフは載せていません。")
    notes_html = "".join(f"<li>{esc(n)}</li>" for n in notes)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{_asset("report.css")}</style>
</head>
<body>
<main>
<h1>Claude 使用量 週次レポート</h1>
<p class="period">{esc(period)}</p>
{banner}
{_kpis(report, slices, tz, options.models)}
<section class="card">
{_limit_section(report, tz, options)}
{_usage_section(report, tz, models, axis)}
<div class="chart-gap"></div>
<div class="keep">
<h2>5時間枠ごとの使用量(Claude Code)</h2>
<div class="chart-scroll">{windows_chart(report, slices, tz, axis.window)}</div>
{_legend(slices)}
</div>
<details><summary>5時間枠ごとの表を見る</summary>{_windows_table(report, slices, tz, models)}</details>
<details><summary>上限%の記録を見る</summary>{_limit_table(report, tz, models)}</details>
</section>
<section class="card keep">
<h2>プロジェクト別</h2>
<div class="split">
<div class="donut-wrap">{donut_chart(shares, report.total_weighted)}</div>
{_project_table(report, shares, options.dev_time_gap)}
</div>
</section>
<section class="card keep">
<h2>注記</h2>
<ul class="notes">{notes_html}</ul>
</section>
</main>
<div id="tooltip" role="status" hidden><div class="tip-value"></div><div class="tip-label"></div></div>
<script>{_asset("report.js")}</script>
</body>
</html>
"""
