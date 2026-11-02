"""毎週のレポートを作る: 取得元の読み込み → 集計 → HTML → PDF → state.json の更新。

週を選ぶ・注記を作るといった判断は純粋関数にし、ファイルの読み書きは make_reports にまとめる。
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .archive import load_limit_samples
from .config import CODE_PRODUCT, ClaudeCodeOptions, Config, FactorOptions, NonCodeOptions, Observation, Price, Weights
from .metrics import (LEARNED, SourceComparison, axis_for, build_week_report, compare_sources, estimate_factor,
                      estimate_non_code_pct, factor_intervals, group_projects, learn_prices, model_factor,
                      model_limit_points, observed_factor, price_of, split_increments, split_tokens, with_observations)
from .model import FROM_DEFAULT, FROM_OBSERVED, FROM_RECORDS, Axis, ModelFactor, NonCodeUsage, WeekReport
from .projects import ProjectResolver
from .render.pdf import build_helper, html_to_pdf, report_pdf_path
from .render.report import render_report
from .render.svg import N_SLOTS, fmt_share, time_label
from .sources import LimitSample, UsageEvent, UsageLog, merge_limit_samples, read_transcripts, read_usage_log
from .state import load_state, save_state, week_key
from .weeks import Week, completed_weeks, final_pct, resets_within, week_containing

MODES = ("pending", "all", "week")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Created:
    week: Week
    pdf: Path
    report: WeekReport


def select_weeks(weeks: Sequence[Week], done: Mapping[str, object], mode: str,
                 target: date | None, tz: tzinfo) -> list[Week]:
    if mode == "all":
        return list(weeks)
    if mode == "week":
        return [w for w in weeks if w.end.astimezone(tz).date() == target]
    return [w for w in weeks if week_key(w.end) not in done]


def week_notes(week: Week, samples: Sequence[LimitSample], tz: tzinfo, history_error: str | None) -> list[str]:
    notes = []
    if history_error:
        notes.append(f"デスクトップアプリの使用量の記録を読み込めませんでした({history_error})。")
    elif not any(week.contains(s.ts) for s in samples):
        notes.append("この週は上限%の記録がありません。")
    for a, b in resets_within(samples, week):
        notes.append(f"{time_label(a.ts, tz)}〜{time_label(b.ts, tz)} の間に週次の使用率が "
                     f"{a.seven_day:.0f}% → {b.seven_day:.0f}% に下がりました(手動リセットなど)。")
    return notes


MAX_LISTED_MISMATCHES = 3  # 注記に時刻と値を書く、食い違った記録の数(残りは件数だけ)


def comparison_notes(c: SourceComparison, gap: timedelta, tz: tzinfo) -> list[str]:
    """週次% の 2 つの記録の照合の結果の注記。/usage を取りに行っていない週は空。

    しばらくは 2 つの記録を両方取って照合し、/usage が正確で手間なく取れると判断できたら一本化する
    (2026-10-28 ユーザーの指定)。その判断の材料として毎週のレポートに載せる。
    """
    if c.attempts == 0:
        return []
    minutes = int(gap.total_seconds() // 60)
    notes = [f"週次%の 2 つの記録の照合(デスクトップアプリ・Claude Code の /usage): 前後 {minutes} 分以内にデスクトップの"
             f"記録がある /usage の記録 {c.compared} 件のうち、前後の値の間に収まったもの {c.consistent} 件"
             f"(同じ値 {c.same} 件)。",
             f"記録のある時間: デスクトップ {c.desktop_hours} 時間・/usage {c.usage_hours} 時間"
             f"(/usage だけ {c.usage_only_hours} 時間)。/usage の取得 {c.attempts} 回のうち失敗 {c.failures} 回。"]
    if c.mismatches:
        def pct(v: float | None) -> str:
            return "--" if v is None else f"{v:.0f}%"

        listed = "、".join(f"{time_label(t, tz)} {v:.0f}%(前 {pct(b)}・後 {pct(a)})"
                          for t, v, b, a in c.mismatches[:MAX_LISTED_MISMATCHES])
        rest = len(c.mismatches) - MAX_LISTED_MISMATCHES
        notes.append(f"値が前後に収まらなかった /usage の記録: {listed}" + (f" ほか {rest} 件" if rest > 0 else "") + "。")
    return notes


def unmet_conditions(c: SourceComparison, options: ClaudeCodeOptions) -> list[str]:
    """一本化の目安(設計書 §2.13)のうち、その週に満たしていないもの。すべて満たせば空。"""
    unmet = []
    if c.compared < options.consolidate_min_compared:
        unmet.append(f"照合できた記録が {c.compared} 件で {options.consolidate_min_compared} 件に足りない")
    if c.mismatches:
        unmet.append(f"値が前後に収まらなかった記録が {len(c.mismatches)} 件ある")
    if c.attempts and c.failures / c.attempts * 100 > options.consolidate_max_failure_pct:
        unmet.append(f"取得の失敗が {c.failures / c.attempts * 100:.0f}% ある")
    if c.usage_hours < c.desktop_hours:
        unmet.append(f"記録のある時間が /usage {c.usage_hours} 時間で、デスクトップの {c.desktop_hours} 時間より短い")
    return unmet


def consolidation_streak(week: Week, compare: Callable[[Week], SourceComparison], options: ClaudeCodeOptions,
                         cycle: timedelta) -> int:
    """week から過去へ、一本化の目安を満たす週が何週続いているか。

    /usage の記録が始まる前の週は照合できた記録が 0 件で目安を満たさないので、そこで止まる(最小数は 1 以上)。
    """
    count = 0
    while not unmet_conditions(compare(week), options):
        count += 1
        week = Week(week.start - cycle, week.start)
    return count


def consolidation_note(unmet: Sequence[str], streak: int, needed: int, finished: bool) -> str:
    """一本化の目安の判定の注記。finished は週が終わっているか(途中の週は確定しない)。"""
    if unmet:
        return f"一本化の目安: この週は満たしていません({'・'.join(unmet)})。"
    if not finished:
        return "一本化の目安: ここまでは満たしています(週が終わるまで確定しません)。"
    if streak >= needed:
        return f"一本化の目安: {streak} 週続けて満たしました。/usage の記録に一本化できます。"
    return f"一本化の目安: この週は満たしました(あと {needed - streak} 週)。"


def source_notes(week: Week, desktop: Sequence[LimitSample], usage_log: UsageLog, options: ClaudeCodeOptions,
                 cycle: timedelta, tz: tzinfo, finished: bool = True) -> list[str]:
    """週次% の 2 つの記録の照合の注記と、一本化の目安の判定。/usage を取りに行っていない週は空。"""
    def compare(w: Week) -> SourceComparison:
        return compare_sources(w, desktop, usage_log.samples, usage_log.failures, options.compare_gap)

    c = compare(week)
    notes = comparison_notes(c, options.compare_gap, tz)
    if not notes:
        return []
    return notes + [consolidation_note(unmet_conditions(c, options), consolidation_streak(week, compare, options, cycle),
                                       options.consolidate_weeks, finished)]


def week_factor(week: Week, samples: Sequence[LimitSample], events: Sequence[UsageEvent], weights: Weights,
                options: FactorOptions, observed: tuple[Observation, str] | None = None,
                non_code_pct: float | None = None) -> ModelFactor:
    """その週のモデルの倍率。次の順に決める。events は時刻順。

    observed はその週の画面の値(Claude Code の /usage の記録を含む)と model の表示名、
    non_code_pct は設定に手で入れた Claude Code 以外の割合。
    1. 画面の値に製品別使用状況(products)まであれば、画面の値から計算した値(Anthropic の表示だけで決まり、
       最も直接的なため。2026-10-23 ユーザーの指定。/usage の記録は 2026-10-28 から)。
    2. 使用率の記録から推定できればその値(記録が揃う今後の週のため)。
    3. 画面の値と、手で入れた Claude Code 以外の割合があれば、画面の値から計算した値(記録が足りない過去の週のため)。
    4. どれもできなければ、設定 metric.model_factors の、週の最後の時点の値(週の終わりのリセット時刻ちょうどは次の週
       なので含めない。期間の境目をリセット時刻に置いたとき、表示と集計の倍率がずれないように)。
    画面の値から計算するのは手動リセットのない週だけ(リセットの前後で使用量の数え方が分かれるため)。
    """
    rows = factor_intervals(week, samples, events, options.max_gap, options.model, weights)

    def from_screen(non_code: float | None) -> ModelFactor | None:
        if observed is None or non_code is None or resets_within(samples, week):
            return None
        obs, name = observed
        if obs.all_pct is None or name not in obs.models:
            return None
        mine, other = split_tokens(events, week.start, obs.ts, options.model, weights)
        value = observed_factor(mine, other, obs.all_pct, obs.models[name], options.model_limit_share, non_code)
        if value is None or not options.min_factor <= value <= options.max_factor:
            return None
        return ModelFactor(options.model, value, FROM_OBSERVED, len(rows))

    if observed is not None and (factor := from_screen(observed[0].non_code_pct)) is not None:
        return factor
    value = estimate_factor(rows, options.min_intervals, options.min_model_intervals, options.min_factor,
                            options.max_factor)
    if value is not None:
        return ModelFactor(options.model, value, FROM_RECORDS, len(rows))
    if (factor := from_screen(non_code_pct)) is not None:
        return factor
    last_moment = week.end - timedelta(microseconds=1)
    return ModelFactor(options.model, model_factor(options.model, last_moment, weights.model_factors), FROM_DEFAULT,
                       len(rows))


def price_notes(events: Sequence[UsageEvent], weights: Weights) -> list[str]:
    """その週の応答のうち、単価表のモデル別の単価以外で計算したものの注記。

    - 会話ログの費用の記録から推定した単価(新しいモデル・単価の変更)
    - 単価表にないため、系統の代わりの単価(先頭に版の数字を含まない行。"claude-opus" など)で計算したもの
    """
    used: dict[str, tuple[Price, str]] = {}
    for e in events:
        used.setdefault(e.model, price_of(e.model, e.ts, weights))
    notes = []
    for model, (price, source) in sorted(used.items()):
        unit = (f"入力 ${price.input:g}・出力 ${price.output:g}・キャッシュ読み込み ${price.cache_read:g}"
                f"(100 万トークンあたり)")
        if source == LEARNED:
            notes.append(f"{model} の単価は Claude Code の費用の記録から推定しました: {unit}。")
        elif not any(ch.isdigit() for ch in source):
            notes.append(f"{model} は単価表にないため、{source} の単価で計算しました: {unit}。")
    return notes


def stored_prices(state: Mapping[str, object]) -> dict[str, tuple[tuple[datetime, Price], ...]]:
    """state.json に保存した、推定した単価。形が違うものは読み飛ばす。"""
    data = state.get("prices")
    out: dict[str, tuple[tuple[datetime, Price], ...]] = {}
    if not isinstance(data, Mapping):
        return out
    for model, periods in data.items():
        try:
            out[str(model)] = tuple((datetime.fromisoformat(ts), Price(*(float(v) for v in values)))
                                    for ts, values in periods)
        except (TypeError, ValueError):
            continue
    return out


def prices_to_state(learned: Mapping[str, Sequence[tuple[datetime, Price]]]) -> dict[str, list]:
    return {model: [[ts.isoformat(), [p.input, p.cache_write_5m, p.cache_write_1h, p.cache_read, p.output]]
                     for ts, p in periods] for model, periods in learned.items()}


def week_weights(weights: Weights, factor: ModelFactor, week: Week) -> Weights:
    """推定した倍率を、その週の API 換算額に使う重みにする(既定値の週はそのまま)。"""
    if not factor.estimated:
        return weights
    return replace(weights, model_factors={**weights.model_factors, factor.model: ((week.start, factor.value),)})


def non_code_usage(week: Week, samples: Sequence[LimitSample], event_times: Sequence[datetime],
                   options: NonCodeOptions, tz: tzinfo, observations: Sequence[Observation] = ()) -> list[NonCodeUsage]:
    """Claude Code 以外の利用。次の順に決める。どれもなければ空。

    1. 画面の製品別使用状況と Claude Code の /usage の製品別の内訳(その週の最後のもの)の、
       Claude Code 以外の製品(チャット・Cowork など。割合 0 は除く)。
    2. 設定に手で入れた値(覚えている値。記録が足りない過去の週を補うため)。
    3. 使用率の記録からの推定(記録が十分な週のため)。
    """
    shown = [o for o in observations if week.contains(o.ts) and o.products]
    if shown:
        latest = max(shown, key=lambda o: o.ts)
        return [NonCodeUsage(name, pct, estimated=False) for name, pct in latest.products.items()
                if name != CODE_PRODUCT and pct > 0]
    manual = options.weeks.get(week.end.astimezone(tz).date())
    if manual:
        return [NonCodeUsage(name, pct, estimated=False) for name, pct in manual.items()]
    pct = estimate_non_code_pct(*split_increments(week, samples, event_times, options.max_gap), options.min_known_pct)
    return [NonCodeUsage(options.estimate_name, pct, estimated=True)] if pct else []


def summary_message(created: Sequence[Created]) -> str:
    if len(created) != 1:
        return f"{len(created)} 週分のレポートを保存しました。"
    r = created[0].report
    pct = "記録なし" if r.final_pct is None else f"{r.final_pct:.0f}%"
    top = max(r.projects, key=lambda p: p.weighted, default=None)
    share = f" / 最多 {top.name} {fmt_share(top.weighted / r.total_weighted * 100)}" if top and r.total_weighted else ""
    return f"週次上限 {pct}{share}"


def week_ending_on(day: date, tz: tzinfo, anchor: datetime, cycle: timedelta) -> Week:
    """その日(tz の日付)に終わる週。なければ ValueError。"""
    last_moment = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)
    week = week_containing(last_moment, anchor, cycle)
    if week.start.astimezone(tz).date() == day:  # その日のうちに次の週が始まっている
        week = Week(week.start - cycle, week.start)
    if week.end.astimezone(tz).date() != day:
        raise ValueError(f"{day} に終わる週はありません(週は {anchor.astimezone(tz):%a %H:%M} に区切ります)")
    return week


def _reading_start(mode: str, target_week: Week | None, done: Mapping[str, object]) -> datetime:
    """読み込む期間の始まり。未作成の週だけ作るときは、作成済みの最後の週より後だけを読む(軽量化のため)。"""
    if mode == "week" and target_week is not None:
        return target_week.start
    if mode == "pending" and done:
        return max(datetime.fromisoformat(k.replace("Z", "+00:00")) for k in done)
    return EPOCH


def make_reports(config: Config, mode: str = "pending", target: date | None = None,
                 now: datetime | None = None) -> list[Created]:
    if mode not in MODES:
        raise ValueError(f"mode は {', '.join(MODES)} のいずれか: {mode}")
    now = now or datetime.now(timezone.utc)
    tz = config.timezone
    state = load_state(config.state_path)
    target_week = week_ending_on(target, tz, config.reset_anchor, config.cycle) if mode == "week" else None
    since = _reading_start(mode, target_week, state["weeks"])

    # 使用率: デスクトップアプリの記録とそのコピー(アプリは 30 日分しか残さない)+ Claude Code の /usage の記録
    #   + 記録が止まっていた週の画面の値
    desktop, history_error = load_limit_samples(config.plan_usage_history, config.limit_archive_path)
    usage_log = read_usage_log(config.usage_log_path)
    samples = with_observations(merge_limit_samples(desktop, usage_log.samples), config.observed,
                                config.reset_anchor, config.cycle)
    # 画面の値と同じ形の値(モデル別の週間上限の %・製品別の内訳): Claude Code の /usage の記録 + 画面の値
    observed = sorted([*usage_log.observations, *config.observed], key=lambda o: o.ts)
    transcripts = read_transcripts(config.transcripts_root, since)
    events, unparsed = transcripts.events, transcripts.unparsed
    # 単価: 設定の単価表 + これまでに推定した単価(state.json)+ 今回読んだ費用の記録からの推定
    weights = replace(config.weights, learned=stored_prices(state))
    weights = replace(weights, learned=learn_prices(transcripts.costs, weights, config.price_learning.min_records,
                                                    config.price_learning.tolerance))
    state["prices"] = prices_to_state(weights.learned)
    firsts = [t for t in (events[0].ts if events else None, samples[0].ts if samples else None) if t is not None]
    if not firsts:
        return []
    weeks = completed_weeks(max(min(firsts), since), now, config.reset_anchor, config.cycle)
    project_of = ProjectResolver(config.project_aliases, config.worktree_patterns)
    events.sort(key=lambda e: e.ts)
    event_times = [e.ts for e in events]
    factor_name = next((n for n, p in config.report.models.items() if p == config.weekly_factor.model), None)
    reports = []
    for week in select_weeks(weeks, state["weeks"], mode, target, tz):
        if not any(week.contains(e.ts) for e in events) and not any(week.contains(s.ts) for s in samples):
            continue  # 使っていない週は作らない
        prev = Week(week.start - config.cycle, week.start)
        prev_final = final_pct(samples, prev)
        if prev_final is None:
            prev_final = (state["weeks"].get(week_key(prev.end)) or {}).get("final_pct")
        # 画面の値(Claude Code の /usage の記録を含む): その週の最後のもの(製品別使用状況まであるものを優先)
        screens = [o for o in observed if week.contains(o.ts) and o.all_pct is not None and factor_name in o.models]
        screen = max(screens, key=lambda o: (bool(o.products), o.ts), default=None)
        manual = config.non_code.weeks.get(week.end.astimezone(tz).date())
        factor = week_factor(week, samples, events, weights, config.weekly_factor,
                             (screen, factor_name) if screen else None,
                             sum(manual.values()) if manual is not None else None)
        reports.append((week, build_week_report(week, events, samples, project_of,
                                                week_weights(weights, factor, week),
                                                config.report.models, config.window, prev_final, unparsed,
                                                week_notes(week, samples, tz, history_error)
                                                + price_notes([e for e in events if week.contains(e.ts)], weights)
                                                + source_notes(week, desktop, usage_log, config.claude_code,
                                                               config.cycle, tz),
                                                non_code_usage(week, samples, event_times, config.non_code, tz, observed),
                                                factor, model_limit_points(week, observed),
                                                config.report.dev_time_gap)))
    if not reports:
        return []

    axes = report_axes([r for _, r in reports], list(config.report.models), mode, stored_axis(state))
    created: list[Created] = []
    helper = build_helper(config.pdf_helper_path)
    for (week, report), axis in zip(reports, axes):
        pdf = report_pdf_path(config.pdf_dir, week.end, tz)
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            html.write_text(render_report(report, tz, config.report, state["slots"], axis), encoding="utf-8")
            html_to_pdf(html, pdf, helper, config.pdf_timeout_seconds)
        slices = group_projects(report.projects, config.report.max_slices, config.report.others_min_pct,
                                N_SLOTS, state["slots"])
        state["slots"].update({s.name: s.slot for s in slices if s.slot is not None})
        state["weeks"][week_key(week.end)] = {"final_pct": report.final_pct, "pdf": str(pdf)}
        state["axis"] = {"week_total": axis.week_total, "window": axis.window, "models": dict(axis.models)}
        save_state(config.state_path, state)
        created.append(Created(week, pdf, report))
    return created


def source_comparison(config: Config, week: Week, now: datetime) -> list[str]:
    """その週(途中でもよい)の週次% の 2 つの記録の照合の結果(cu compare-sources)。記録のコピーも更新する。"""
    desktop, history_error = load_limit_samples(config.plan_usage_history, config.limit_archive_path)
    lines = source_notes(week, desktop, read_usage_log(config.usage_log_path), config.claude_code, config.cycle,
                         config.timezone, finished=now >= week.end)
    if history_error:
        lines.append(f"デスクトップアプリの使用量の記録を読み込めませんでした({history_error})。")
    return lines or ["この週は Claude Code の /usage の記録がありません。"]


def report_axes(reports: Sequence[WeekReport], models: Sequence[str], mode: str, stored: Axis | None) -> list[Axis]:
    """各レポートの使用量の縦軸の上限(これまでの最大値)。

    - all(すべて作り直す): 全週の最大値で、すべて同じ上限にそろえる。
    - pending / week: 保存してある上限から始め、古い週から順に最大値を更新しながら使う。最大値を超える週が出ても、
      作成済みのレポートは作り直さず、その週以降の上限が上がる。
    """
    if mode == "all":
        return [axis_for(reports, models)] * len(reports)
    axes, axis = [], stored
    for r in reports:
        axis = axis_for([r], models, axis)
        axes.append(axis)
    return axes


def stored_axis(state: Mapping[str, object]) -> Axis | None:
    data = state.get("axis")
    if not isinstance(data, Mapping):
        return None
    try:
        models = data.get("models") or {}
        return Axis(float(data["week_total"]), float(data["window"]), {str(k): float(v) for k, v in models.items()})
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
