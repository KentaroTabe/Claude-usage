"""週の区切り(純粋関数)。

週は設定の reset_anchor から cycle ごとに区切る(火曜 19:00 JST の 7 日周期)。
手動リセット(例: 2026-10-04)では週を区切らず、週の途中の出来事としてレポートに注記する。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .sources import LimitSample


@dataclass(frozen=True)
class Week:
    start: datetime
    end: datetime

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def week_containing(ts: datetime, anchor: datetime, cycle: timedelta) -> Week:
    k = math.floor((ts - anchor) / cycle)
    start = anchor + k * cycle
    return Week(start, start + cycle)


def completed_weeks(first: datetime, now: datetime, anchor: datetime, cycle: timedelta) -> list[Week]:
    """first を含む週から、now までに終わった週までを古い順に返す。"""
    weeks = []
    week = week_containing(first, anchor, cycle)
    while week.end <= now:
        weeks.append(week)
        week = Week(week.end, week.end + cycle)
    return weeks


def final_pct(samples: Sequence[LimitSample], week: Week) -> float | None:
    """その週の最後に記録された週次の使用率。"""
    values = [s.seven_day for s in samples if week.contains(s.ts) and s.seven_day is not None]
    return values[-1] if values else None


def resets_within(samples: Sequence[LimitSample], week: Week) -> list[tuple[LimitSample, LimitSample]]:
    """週の途中で週次の使用率が下がった区間(手動リセットなど)。(下がる前の記録, 下がった後の記録) の組。"""
    inside = [s for s in samples if week.contains(s.ts) and s.seven_day is not None]
    return [(a, b) for a, b in zip(inside, inside[1:]) if b.seven_day < a.seven_day]
