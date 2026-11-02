"""集計の純粋関数。

- 週次レポートの組み立て: 使用量(API 換算額 × モデルの倍率)、5 時間枠の推定、プロジェクト別・モデル別の集計
  (build_week_report)、Claude Code 以外の利用の割合の推定(split_increments)、
  週ごとのモデルの倍率の推定(factor_intervals → estimate_factor)
- 週次% の 2 つの記録(デスクトップアプリ・Claude Code の /usage)の照合(compare_sources)
- 描画の前段: 「その他」へのまとめ、色枠の割り当て、Claude Code 以外の区分の追加
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping, Sequence

from .config import Observation, Price, Weights
from .model import Axis, LimitPoint, ModelFactor, NonCodeUsage, ProjectUsage, WeekReport, WindowBucket
from .sources import CostRecord, LimitSample, UsageEvent
from .weeks import Week, week_containing

OTHERS = "その他"


def model_factor(model: str, ts: datetime, factors: Mapping[str, Sequence[tuple[datetime, float]]]) -> float:
    """ts の時点でのモデルの倍率(設定 metric.model_factors)。最初の期間より前と、当てはまらないモデルは 1。"""
    for prefix, periods in factors.items():
        if model.startswith(prefix):
            current = [factor for start, factor in periods if start <= ts]
            return current[-1] if current else 1.0
    return 1.0


LEARNED = "learned"  # price_of の出どころ: 会話ログの費用の記録から推定した単価


def price_of(model: str, ts: datetime, weights: Weights) -> tuple[Price, str]:
    """ts の時点でのモデルの単価と、その出どころ(LEARNED か、一致した単価表の先頭)。

    推定した単価(weights.learned)を優先し、なければ単価表で最も長く一致する先頭の単価。どれにも一致しなければ "claude"。
    """
    current = [p for start, p in weights.learned.get(model, ()) if start <= ts]
    if current:
        return current[-1], LEARNED
    prefix = max((p for p in weights.prices if model.startswith(p)), key=len, default="claude")
    return weights.prices[prefix], prefix


def api_cost(e: UsageEvent, price: Price) -> float:
    """応答 1 件の API 換算額 [ドル]。"""
    return (e.input_tokens * price.input + e.cache_write_5m * price.cache_write_5m
            + e.cache_write_1h * price.cache_write_1h + e.cache_read * price.cache_read
            + e.output_tokens * price.output) / 1e6


def weighted(e: UsageEvent, w: Weights) -> float:
    """応答 1 件の使用量 = API 換算額 × モデルの倍率 [ドル]。"""
    return api_cost(e, price_of(e.model, e.ts, w)[0]) * model_factor(e.model, e.ts, w.model_factors)


def cost_bounds(r: CostRecord, price: Price) -> tuple[float, float]:
    """費用の記録のトークン数と単価から見積もった費用の範囲 [ドル]。

    記録はキャッシュ書き込みを 5 分と 1 時間に分けないので、すべて 5 分 〜 すべて 1 時間の幅で見積もる。
    """
    base = r.input_tokens * price.input + r.output_tokens * price.output + r.cache_read * price.cache_read
    return (base + r.cache_write * price.cache_write_5m) / 1e6, (base + r.cache_write * price.cache_write_1h) / 1e6


MATCH, NEAR, MISS = "match", "near", "miss"


def price_match(r: CostRecord, price: Price, tolerance: float) -> str:
    """費用の記録が単価と合うか。

    MATCH: 書き込みを 1 時間(Claude Code の既定)として tolerance 以内で合う。実際の記録はほぼすべてこれ。
    NEAR: 書き込みが 5 分〜1 時間のどこかなら合う(書き込みの多い記録は幅が広く、別の単価とも合いうるので決め手にしない)。
    MISS: 合わない。
    """
    lo, hi = cost_bounds(r, price)
    if abs(r.cost - hi) <= tolerance * hi:
        return MATCH
    return NEAR if lo * (1 - tolerance) <= r.cost <= hi * (1 + tolerance) else MISS


def fit_price(records: Sequence[CostRecord]) -> Price | None:
    """費用の記録から単価を推定する。

    公式の単価と同じ構造(出力 = 入力×5、5 分書き込み = 入力×1.25、1 時間書き込み = 入力×2)を仮定し、入力の単価 p と
    キャッシュ読み込みの単価 c を最小二乗で求める(書き込みは Claude Code の既定の 1 時間とみなす)。キャッシュ読み込みが
    ない記録だけなら c は求まらないので、入力の 0.1 倍(多くのモデルの比)とする。
    """
    rows = [(r.cost, (r.input_tokens + 5 * r.output_tokens + 2 * r.cache_write) / 1e6, r.cache_read / 1e6)
            for r in records]
    rates = fit_rates(rows)
    if rates is None:
        sxx = sum(x * x for _, x, _ in rows)
        if sxx <= 0 or any(y > 0 for _, _, y in rows):
            return None
        rates = (sum(d * x for d, x, _ in rows) / sxx, None)
    p, c = rates
    if p <= 0 or (c is not None and c < 0):
        return None
    return Price(p, 1.25 * p, 2 * p, 0.1 * p if c is None else c, 5 * p)


def learn_prices(records: Sequence[CostRecord], weights: Weights, min_records: int,
                 tolerance: float) -> dict[str, tuple[tuple[datetime, Price], ...]]:
    """費用の記録と単価を比べ、合わなくなったモデルの単価を推定して weights.learned に足したものを返す。

    モデルごとに時刻順に見て、最後に単価と合った(MATCH の)記録より後の「合わない(MISS の)記録」が min_records 以上あれば、
    それらから単価を推定し(fit_price)、どれも合えば、その最初の記録の時刻からの単価として加える。NEAR の記録は数えない。
    表にない新しいモデル(系統の代わりの単価で計算していたもの)も、単価の変更も、同じ手順で扱う。
    すでに推定した単価で合っていれば何も足さない。
    """
    learned = dict(weights.learned)
    by_model: dict[str, list[CostRecord]] = defaultdict(list)
    for r in records:
        by_model[r.model].append(r)
    for model, recs in by_model.items():
        current = replace(weights, learned=learned)
        run: list[CostRecord] = []
        for r in sorted(recs, key=lambda r: r.ts):
            match = price_match(r, price_of(model, r.ts, current)[0], tolerance)
            if match == MATCH:
                run = []
            elif match == MISS:
                run.append(r)
        if len(run) < min_records:
            continue
        price = fit_price(run)
        if price is not None and all(price_match(r, price, tolerance) != MISS for r in run):
            learned[model] = tuple(sorted([*learned.get(model, ()), (run[0].ts, price)], key=lambda p: p[0]))
    return learned


def split_increments(week: Week, samples: Sequence[LimitSample], event_times: Sequence[datetime],
                     max_gap: timedelta) -> tuple[float, float, float]:
    """週次の使用率の増分 [pt] を、(Claude Code を使った区間, 使っていない区間, 分からない区間) に分ける。

    区間は記録の連続する 2 点(週の始まりは 0%)。間隔が max_gap より長い区間は、いつ使ったか分からない。
    Claude Code の応答が 1 件でもある区間は、同時に Cowork などを使っていても Claude Code の区間に数える
    (そのため、Claude Code 以外の分は少なめに出る)。値が下がった区間(手動リセット)は、下がった後の値を増分とする。
    event_times は時刻順に並べた Claude Code の応答の時刻。
    """
    points = [(week.start, 0.0)] + sorted((s.ts, s.seven_day) for s in samples
                                          if week.contains(s.ts) and s.seven_day is not None)
    code = other = unknown = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        d = p1 - p0 if p1 >= p0 else p1
        if d <= 0:
            continue
        i = bisect_right(event_times, t0)
        if t1 - t0 > max_gap:
            unknown += d
        elif i < len(event_times) and event_times[i] <= t1:
            code += d
        else:
            other += d
    return code, other, unknown


def factor_intervals(week: Week, samples: Sequence[LimitSample], events: Sequence[UsageEvent], max_gap: timedelta,
                     model: str, weights: Weights) -> list[tuple[float, float, float]]:
    """モデルの倍率の推定に使う区間ごとの (週次%の増分, model の API 換算額, それ以外の API 換算額)。

    区間は記録の連続する 2 点(週の始まりは 0%)のうち、間隔が max_gap 以内で値が下がっていない(手動リセットでない)もの。
    Claude Code を使っていない区間は Claude Code 以外の利用の区間なので除く。API 換算額にはモデルの倍率を掛けない。
    events は時刻順に並べておく。
    """
    raw = replace(weights, model_factors={})
    points = [(week.start, 0.0)] + sorted((s.ts, s.seven_day) for s in samples
                                          if week.contains(s.ts) and s.seven_day is not None)
    times = [e.ts for e in events]
    rows = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t1 - t0 > max_gap or p1 < p0:
            continue
        members = events[bisect_right(times, t0):bisect_right(times, t1)]
        if members:
            mine = sum(weighted(e, raw) for e in members if e.model.startswith(model))
            rows.append((p1 - p0, mine, sum(weighted(e, raw) for e in members) - mine))
    return rows


def fit_rates(rows: Sequence[tuple[float, float, float]]) -> tuple[float, float] | None:
    """増分 ≒ a × x + b × y(切片なし)を最小二乗で解き (a, b) を返す。x と y が比例して見分けられなければ None。"""
    sxx = sum(x * x for _, x, _ in rows)
    syy = sum(y * y for _, _, y in rows)
    sxy = sum(x * y for _, x, y in rows)
    sdx = sum(d * x for d, x, _ in rows)
    sdy = sum(d * y for d, _, y in rows)
    det = sxx * syy - sxy * sxy
    if det <= 1e-12 * max(sxx * syy, 1e-300):
        return None
    return (sdx * syy - sdy * sxy) / det, (sdy * sxx - sdx * sxy) / det


def estimate_factor(rows: Sequence[tuple[float, float, float]], min_intervals: int, min_model_intervals: int,
                    min_factor: float, max_factor: float) -> float | None:
    """factor_intervals の区間から、model の倍率(model の API 換算額あたりの消費 ÷ それ以外の API 換算額あたりの消費)を推定する。

    区間が足りない、両方の消費が正にならない、範囲の外になる場合は None(既定値を使う)。
    """
    if (len(rows) < min_intervals or sum(1 for _, x, _ in rows if x > 0) < min_model_intervals
            or sum(1 for _, _, y in rows if y > 0) < min_model_intervals):
        return None
    rates = fit_rates(rows)
    if rates is None or rates[0] <= 0 or rates[1] <= 0:
        return None
    factor = rates[0] / rates[1]
    return factor if min_factor <= factor <= max_factor else None


def with_observations(samples: Sequence[LimitSample], observations: Sequence[Observation], anchor: datetime,
                      cycle: timedelta) -> list[LimitSample]:
    """記録に、画面で見た全モデルの週次%を足す(記録が止まっていた週を補う)。時刻順に並べて返す。

    画面の値は、その週の記録の最後より後のものだけを足す。記録と重なる時間は記録を優先する(画面の値は表示の時点の値で、
    記録と同じ時刻に並べると、手動リセットの前後などで値が前後するため)。
    """
    last: dict[datetime, datetime] = {}
    for s in samples:
        if s.seven_day is not None:
            start = week_containing(s.ts, anchor, cycle).start
            last[start] = max(last.get(start, s.ts), s.ts)
    extra = [LimitSample(o.ts, None, o.all_pct) for o in observations
             if o.all_pct is not None and o.ts > last.get(week_containing(o.ts, anchor, cycle).start, o.ts - cycle)]
    return sorted([*samples, *extra], key=lambda s: s.ts)


def model_limit_points(week: Week, observations: Sequence[Observation]) -> dict[str, list[LimitPoint]]:
    """週の中のモデル別の週間上限の使用率(表示名 → 時刻順の点)。

    observations は Claude Code の /usage の記録と画面の値(どちらも同じ値の表示なので区別しない)。
    """
    out: dict[str, list[LimitPoint]] = defaultdict(list)
    for o in sorted(observations, key=lambda o: o.ts):
        if week.contains(o.ts):
            for name, pct in o.models.items():
                out[name].append(LimitPoint(o.ts, pct))
    return dict(out)


@dataclass(frozen=True)
class SourceComparison:
    """週次% の 2 つの記録(デスクトップアプリ・Claude Code の /usage)の、1 週間分の照合の結果。

    compared: 前後 gap 以内にデスクトップの記録がある /usage の記録の数 / consistent: そのうち値が前後のデスクトップの
    記録の間に収まったもの / same: そのうち最も近いデスクトップの記録と同じ値のもの / mismatches: 収まらなかったもの
    (時刻, /usage の値, 前のデスクトップの値, 後のデスクトップの値)/ *_hours: 記録のある「時」の数(usage_only_hours は
    /usage にだけ記録がある「時」= デスクトップの記録が止まっていた時間)/ attempts・failures: /usage を取りに行った回数と、
    そのうち取れなかった回数。
    """
    compared: int
    consistent: int
    same: int
    mismatches: list[tuple[datetime, float, float | None, float | None]]
    desktop_hours: int
    usage_hours: int
    usage_only_hours: int
    attempts: int
    failures: int


def _hours(times: Iterable[datetime]) -> set[datetime]:
    return {t.replace(minute=0, second=0, microsecond=0) for t in times}


def compare_sources(week: Week, desktop: Sequence[LimitSample], usage: Sequence[LimitSample],
                    failures: Sequence[datetime], gap: timedelta) -> SourceComparison:
    """週の中の /usage の記録(usage)の週次% を、デスクトップアプリの記録(desktop)と照合する。

    同じサーバーの値なので、時刻順に並べれば前後のデスクトップの記録の値の間に収まるはず(週次% は週の中では増える一方。
    手動リセットの前後は例外)。前後 gap 以内にデスクトップの記録がない /usage の記録は照合しない。
    """
    d = sorted((s.ts, s.seven_day) for s in desktop if week.contains(s.ts) and s.seven_day is not None)
    u = sorted((s.ts, s.seven_day) for s in usage if week.contains(s.ts) and s.seven_day is not None)
    times = [t for t, _ in d]
    compared = consistent = same = 0
    mismatches = []
    for t, v in u:
        i = bisect_right(times, t)
        before = d[i - 1] if i > 0 and t - d[i - 1][0] <= gap else None
        after = d[i] if i < len(d) and d[i][0] - t <= gap else None
        if before is None and after is None:
            continue
        compared += 1
        if (before is None or before[1] <= v) and (after is None or v <= after[1]):
            consistent += 1
        else:
            mismatches.append((t, v, before[1] if before else None, after[1] if after else None))
        nearest = min((x for x in (before, after) if x is not None), key=lambda x: abs(x[0] - t))
        same += nearest[1] == v
    d_hours, u_hours = _hours(times), _hours(t for t, _ in u)
    successes = sum(1 for s in usage if week.contains(s.ts))
    failed = sum(1 for t in failures if week.contains(t))
    return SourceComparison(compared, consistent, same, mismatches, len(d_hours), len(u_hours),
                            len(u_hours - d_hours), successes + failed, failed)


def split_tokens(events: Sequence[UsageEvent], start: datetime, end: datetime, model: str,
                 weights: Weights) -> tuple[float, float]:
    """start〜end の Claude Code の API 換算額(倍率なし)を (model, それ以外) に分ける。"""
    raw = replace(weights, model_factors={})
    mine = other = 0.0
    for e in events:
        if start <= e.ts < end:
            if e.model.startswith(model):
                mine += weighted(e, raw)
            else:
                other += weighted(e, raw)
    return mine, other


def observed_factor(model_tokens: float, other_tokens: float, all_pct: float, model_pct: float,
                    model_limit_share: float, non_code_pct: float) -> float | None:
    """画面の値から倍率を計算する。

    model の分 [pt] = model% × model_limit_share / 100(model の週間上限は週次上限の model_limit_share %)
    それ以外の分 [pt] = 全モデル% × (1 − Claude Code 以外の割合) − model の分
    倍率 = (model の分 ÷ model の API 換算額) ÷ (それ以外の分 ÷ それ以外の API 換算額)。計算できなければ None。
    """
    model_pt = model_pct * model_limit_share / 100
    other_pt = all_pct * (1 - non_code_pct / 100) - model_pt
    if model_tokens <= 0 or other_tokens <= 0 or model_pt <= 0 or other_pt <= 0:
        return None
    return (model_pt / model_tokens) / (other_pt / other_tokens)


def estimate_non_code_pct(code: float, other: float, unknown: float, min_known_pct: float) -> float | None:
    """split_increments の結果から、Claude Code 以外の割合 [%] を推定する。分かる区間が少なければ None。"""
    known = code + other
    if known <= 0 or known / (known + unknown) * 100 < min_known_pct:
        return None
    return other / known * 100


def infer_windows(events: Sequence[UsageEvent], length: timedelta) -> list[tuple[datetime, datetime, list[UsageEvent]]]:
    """5 時間枠を推定する。前の枠が終わった後、最初に使った時刻の「時」から length の間を 1 つの枠とする。

    デスクトップアプリの記録で見た枠(13 時台に始まり 18 時に終わる等)に合わせた推定。claude.ai だけで使った時間は分からない。
    """
    windows: list[tuple[datetime, datetime, list[UsageEvent]]] = []
    for e in sorted(events, key=lambda e: e.ts):
        if windows and e.ts < windows[-1][1]:
            windows[-1][2].append(e)
            continue
        start = e.ts.replace(minute=0, second=0, microsecond=0)
        windows.append((start, start + length, [e]))
    return windows


def axis_for(reports: Iterable[WeekReport], models: Iterable[str] = (), base: Axis | None = None) -> Axis:
    """レポート群(と、これまでの上限 base)のうち最大の値を縦軸の上限にする。"""
    reports = list(reports)
    return Axis(
        week_total=max([r.total_weighted for r in reports] + [base.week_total if base else 0.0]),
        window=max([r.max_window_total for r in reports] + [base.window if base else 0.0]),
        models={m: max([r.model_total(m) for r in reports] + [base.models.get(m, 0.0) if base else 0.0])
                for m in models},
    )


def cumulative(report: WeekReport, model: str | None = None) -> list[tuple[datetime, float]]:
    """週内の使用量の累積。週の始まりを 0 とし、使った 5 時間枠ごとに始まりと終わりの 2 点。

    枠の始まりでは直前までの累積、終わりではその枠の分を足した累積。使っていない時間は横ばいになり、
    なめらかにつないでも枠の外で増えて見えない。model を指定するとそのモデルの分だけ(by_model)、
    省くと全体(by_project の合計)。使っていなければ空。
    """
    points = [(report.week_start, 0.0)]
    total = 0.0
    for w in sorted(report.windows, key=lambda w: w.start):
        v = w.by_model.get(model, 0.0) if model else sum(w.by_project.values())
        if v <= 0 or w.end <= report.week_start or w.start >= report.week_end:
            continue
        start = max(w.start, report.week_start)
        if start > points[-1][0]:  # 直前の枠と続いていれば、始まりの点は前の枠の終わりと同じなので足さない
            points.append((start, total))
        total += v
        points.append((min(w.end, report.week_end), total))
    return points if total > 0 else []


def active_time(times: Iterable[datetime], gap: timedelta) -> timedelta:
    """時刻の並びから、続けて作業していた時間の合計を求める。連続する 2 点の間隔が gap 以内なら足す(長い間隔は作業の切れ目)。"""
    ordered = sorted(times)
    return sum((b - a for a, b in zip(ordered, ordered[1:]) if b - a <= gap), timedelta())


def build_week_report(week: Week, events: Sequence[UsageEvent], samples: Sequence[LimitSample],
                      project_of: Callable[[str], str], weights: Weights, models: Mapping[str, str],
                      window: timedelta, prev_final_pct: float | None = None, unparsed_lines: int = 0,
                      notes: Sequence[str] = (), non_code: Sequence[NonCodeUsage] = (),
                      factor: ModelFactor | None = None,
                      model_limit_points: Mapping[str, list[LimitPoint]] | None = None,
                      dev_time_gap: timedelta = timedelta()) -> WeekReport:
    """1 週間分の使用量と使用率の記録から、レポートの入力を組み立てる。

    factor はその週に使ったモデルの倍率(表示用)。使用量には weights の倍率が使われるので、
    推定値を使う週は呼び出し側で weights に反映しておく(weekly.week_weights)。
    """
    in_week = [e for e in events if week.contains(e.ts)]
    totals: dict[str, float] = defaultdict(float)
    subagent: dict[str, float] = defaultdict(float)
    sessions: dict[str, set[str]] = defaultdict(set)
    times: dict[str, list[datetime]] = defaultdict(list)
    windows = []
    for start, end, members in infer_windows(in_week, window):
        by_project: dict[str, float] = defaultdict(float)
        by_model: dict[str, float] = defaultdict(float)
        for e in members:
            value, project = weighted(e, weights), project_of(e.project_dir)
            by_project[project] += value
            totals[project] += value
            sessions[project].add(e.session_id)
            times[project].append(e.ts)
            if e.is_subagent:
                subagent[project] += value
            group = model_group(e.model, models)
            if group is not None:
                by_model[group] += value
        # リセット直前の値 = 枠の中で最も大きい値(枠の中では使用率は増える一方のため)
        pcts = [s.five_hour for s in samples if start <= s.ts < end and s.five_hour is not None]
        windows.append(WindowBucket(start, end, dict(by_project), max(pcts) if pcts else None, dict(by_model)))
    projects = [ProjectUsage(name, totals[name], len(sessions[name]), subagent[name],
                             active_time(times[name], dev_time_gap)) for name in totals]
    limit_points = [LimitPoint(s.ts, s.seven_day) for s in samples if week.contains(s.ts) and s.seven_day is not None]
    return WeekReport(week_start=week.start, week_end=week.end, limit_points=limit_points, windows=windows,
                      projects=projects, prev_final_pct=prev_final_pct, unparsed_lines=unparsed_lines,
                      notes=list(notes), non_code=list(non_code), factor=factor,
                      model_limit_points=dict(model_limit_points or {}),
                      dev_time=active_time((e.ts for e in in_week), dev_time_gap))


@dataclass(frozen=True)
class Slice:
    name: str                  # 表示名。まとめた区分は OTHERS
    weighted: float
    share_pct: float
    slot: int | None           # 色枠(0 始まり)。「その他」と Claude Code 以外は None
    members: tuple[str, ...]   # この区分に含まれるプロジェクト名
    non_code: bool = False     # Claude Code 以外の利用(Cowork など)。weighted は割合からの換算値
    shade: int = 0             # Claude Code 以外の区分の色の濃さ(0・1 を交互に。隣り合う区分を見分けるため)


def with_non_code(slices: Sequence[Slice], non_code: Sequence[NonCodeUsage]) -> list[Slice]:
    """Claude Code のプロジェクト別の区分に、Claude Code 以外の区分を加え、割合を週全体に対する値に直す。

    Claude Code 以外の使用量は分からないため、割合から使用量に換算した値を weighted に入れる。
    Claude Code を使っていない週は、比べる相手がないので加えない。
    """
    code_pct = 100 - sum(n.share_pct for n in non_code)
    code_total = sum(s.weighted for s in slices)
    if not non_code or code_total <= 0 or code_pct <= 0:
        return list(slices)
    out = [replace(s, share_pct=s.share_pct * code_pct / 100) for s in slices]
    for i, n in enumerate(non_code):
        name = f"{n.name}(推定)" if n.estimated else n.name
        out.append(Slice(name, code_total * n.share_pct / code_pct, n.share_pct, None, (), non_code=True, shade=i % 2))
    return out


def assign_slots(names: Sequence[str], n_slots: int, preferred: Mapping[str, int] | None = None) -> dict[str, int]:
    """プロジェクト名ごとに色枠を決める。

    - preferred(過去の週で割り当て済みの枠)にあり、その枠が空いていればそれを使う(週をまたいで同じ色にするため)。
    - それ以外は空いている枠のうち番号の小さいものから使う。分類色は番号順に並べたとき隣同士が見分けやすいよう
      検証された並びなので、大きい順に小さい番号を割り当てると、円グラフで隣り合う区分が見分けやすくなる。
    names は優先順(大きい順)。n_slots を超える名前は割り当てない。
    """
    preferred = preferred or {}
    names = list(names[:n_slots])
    result: dict[str, int] = {}
    taken: set[int] = set()
    for name in names:
        slot = preferred.get(name)
        if slot is not None and 0 <= slot < n_slots and slot not in taken:
            result[name] = slot
            taken.add(slot)
    free = (s for s in range(n_slots) if s not in taken)
    for name in names:
        if name not in result:
            result[name] = next(free)
    return result


def group_projects(projects: Iterable[ProjectUsage], max_slices: int, others_min_pct: float,
                   n_slots: int, preferred_slots: Mapping[str, int] | None = None) -> list[Slice]:
    """大きい順に並べ、上位 max_slices 件かつ others_min_pct 以上のものを個別の区分、残りを「その他」にまとめる。

    preferred_slots は過去の週の色枠の割り当て(state.json に保存したもの)。
    """
    ordered = sorted((p for p in projects if p.weighted > 0), key=lambda p: (-p.weighted, p.name))
    total = sum(p.weighted for p in ordered)
    if total <= 0:
        return []
    keep = [p for p in ordered[:max_slices] if p.weighted / total * 100 >= others_min_pct]
    rest = [p for p in ordered if p not in keep]
    slots = assign_slots([p.name for p in keep], n_slots, preferred_slots)
    slices = [Slice(p.name, p.weighted, p.weighted / total * 100, slots[p.name], (p.name,)) for p in keep]
    if rest:
        w = sum(p.weighted for p in rest)
        slices.append(Slice(OTHERS, w, w / total * 100, None, tuple(p.name for p in rest)))
    return slices


def model_group(model_id: str | None, models: Mapping[str, str]) -> str | None:
    """モデル ID(例: claude-fable-5-1)が、設定 report.models のどの表示名に当たるか。当たらなければ None。"""
    if not model_id:
        return None
    return next((name for name, prefix in models.items() if model_id.startswith(prefix)), None)


def fold_to_slices(by_project: Mapping[str, float], slices: Sequence[Slice]) -> list[float]:
    """1 日分のプロジェクト別の値を、slices と同じ並びの区分ごとの値に直す。"""
    owner = {m: i for i, s in enumerate(slices) for m in s.members}
    values = [0.0] * len(slices)
    for name, v in by_project.items():
        i = owner.get(name)
        if i is not None:
            values[i] += v
    return values
