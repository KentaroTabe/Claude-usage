"""見た目の確認用のダミーデータ。乱数の種を固定しているので、毎回同じ内容になる。

シナリオ:
  dense  上限%が 1 時間ごとに記録できた週(5 時間枠の使用率も全枠で記録)
  sparse 数回だけ上限%が記録できた週(5 時間枠の使用率もその時間を含む枠だけ)
  none   上限%の記録が 1 点もない週
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .model import FROM_DEFAULT, FROM_RECORDS, LimitPoint, ModelFactor, NonCodeUsage, ProjectUsage, WeekReport, WindowBucket

SCENARIOS = ("dense", "sparse", "none")

# 架空のプロジェクト名と、週の中での使用量の比率。最後の 1 件は長い名前での表示崩れを確かめるためのもの
PROJECTS = [
    ("alpha-web", 34.0), ("beta-api", 22.0), ("gamma-ml", 16.0), ("delta-tools", 10.0),
    ("epsilon", 7.0), ("zeta-notes", 2.5), ("eta-sandbox", 1.5),
    ("a-very-long-project-name_for-layout-check", 7.0),
]
WEEK_END = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)  # 実際に観測した次回のリセット時刻
FINAL_PCT = 78.0
PREV_FINAL_PCT = 64.0
FABLE_SHARE_RANGE = (0.2, 0.7)  # 各時間の使用量のうち Fable モデルが占める割合の範囲
TOTAL_WEIGHTED = 1.31e8
JST = timezone(timedelta(hours=9))
ACTIVE_HOURS_JST = set(range(9, 24)) | {0}  # 9 時〜翌 0 時台に使う想定
SPARSE_HOURS = (30, 31, 77, 118, 119, 120, 150)  # 週の始まりからの経過時間。上限%を記録できた時間帯の想定
WINDOW_HOURS = 5
FIVE_HOUR_CAPACITY = TOTAL_WEIGHTED / 12  # 5 時間枠を使い切る量の想定(ダミー)
# Claude Code 以外の利用。dense は記録からの推定、sparse は手で入れた値の想定
NON_CODE = {
    "dense": [NonCodeUsage("Cowork など", 18.0, estimated=True)],
    "sparse": [NonCodeUsage("Cowork", 40.0, estimated=False)],
}
# Fable の倍率。dense は記録からの推定、それ以外は既定値の想定
FACTORS = {"dense": ModelFactor("claude-fable", 3.92, FROM_RECORDS, intervals=42)}
DEFAULT_FACTOR = ModelFactor("claude-fable", 4.0, FROM_DEFAULT)


def _window_ranges(hours: list[dict[str, float]]) -> list[tuple[int, int]]:
    """5 時間枠の区間(時間の添字)。前の枠が終わった後、最初に使った時刻から 5 時間。"""
    ranges, h = [], 0
    while h < len(hours):
        if sum(hours[h].values()) > 0:
            ranges.append((h, min(h + WINDOW_HOURS, len(hours))))
            h += WINDOW_HOURS
        else:
            h += 1
    return ranges


def _hourly_usage(rng: random.Random, start: datetime) -> list[dict[str, float]]:
    """週の 168 時間それぞれのプロジェクト別使用量。1 日だけほとんど使わない日を作る。"""
    idle_day = rng.randrange(7)
    hours: list[dict[str, float]] = []
    for h in range(7 * 24):
        jst_hour = (start + timedelta(hours=h)).astimezone(JST).hour
        active = jst_hour in ACTIVE_HOURS_JST and h // 24 != idle_day and rng.random() < 0.7
        hours.append({name: (share * rng.uniform(0.2, 1.8) if active and rng.random() < 0.55 else 0.0)
                      for name, share in PROJECTS})
    return hours


def build_demo_report(scenario: str, seed: int = 7) -> WeekReport:
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario は {', '.join(SCENARIOS)} のいずれか: {scenario}")
    rng = random.Random(seed)
    start = WEEK_END - timedelta(days=7)
    hours = _hourly_usage(rng, start)
    raw_total = sum(sum(h.values()) for h in hours) or 1.0
    scale = TOTAL_WEIGHTED / raw_total
    hours = [{k: v * scale for k, v in h.items()} for h in hours]
    # 各時間の使用量のうち、ランダムな割合が Fable モデルだったとする
    fable_hours = [sum(u.values()) * rng.uniform(*FABLE_SHARE_RANGE) for u in hours]

    windows = []
    for a, b in _window_ranges(hours):
        by_project = {name: sum(h[name] for h in hours[a:b]) for name, _ in PROJECTS}
        recorded = scenario == "dense" or (scenario == "sparse" and any(a <= h < b for h in SPARSE_HOURS))
        pct = min(100.0, round(sum(by_project.values()) / FIVE_HOUR_CAPACITY * 100)) if recorded else None
        windows.append(WindowBucket(start + timedelta(hours=a), start + timedelta(hours=b), by_project, pct,
                                    by_model={"Fable": sum(fable_hours[a:b])}))
    projects = []
    for name, _ in PROJECTS:
        weighted = sum(h[name] for h in hours)
        projects.append(ProjectUsage(name=name, weighted=weighted, sessions=rng.randint(1, 14),
                                     subagent_weighted=weighted * rng.uniform(0.05, 0.35),
                                     dev_time=timedelta(hours=sum(1 for h in hours if h[name] > 0))))

    # 上限%は累積の使用量に比例すると仮定する(実際の消費の式は非公表)
    cumulative, points = 0.0, []
    for h, usage in enumerate(hours):
        cumulative += sum(usage.values())
        ts = start + timedelta(hours=h + 1)
        if (scenario == "dense" and sum(usage.values()) > 0) or (scenario == "sparse" and h in SPARSE_HOURS):
            points.append(LimitPoint(ts, round(FINAL_PCT * cumulative / TOTAL_WEIGHTED, 1)))

    return WeekReport(week_start=start, week_end=WEEK_END, limit_points=points, windows=windows, projects=projects,
                      prev_final_pct=PREV_FINAL_PCT, unparsed_lines=0, is_demo=True,
                      notes=[f"ダミーデータ(シナリオ: {scenario}、乱数の種: {seed})"],
                      non_code=NON_CODE.get(scenario, []), factor=FACTORS.get(scenario, DEFAULT_FACTOR),
                      dev_time=timedelta(hours=sum(1 for h in hours if any(h.values()))))
