"""週次レポートの入力となるデータの型。集計(metrics)が作り、描画(render)が読む。

集計の最小単位は 5 時間枠と週。1 時間ごとの使用量は扱わない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping


@dataclass(frozen=True)
class LimitPoint:
    """週次上限の使用率の 1 回の観測。"""
    ts: datetime
    pct: float


@dataclass(frozen=True)
class ProjectUsage:
    name: str
    weighted: float           # 使用量(API 換算額 × モデルの倍率)[ドル]
    sessions: int
    subagent_weighted: float  # うちサブエージェント分
    dev_time: timedelta = timedelta()  # 開発時間(このプロジェクトの応答だけで計算。ほかのプロジェクトと重なることがある)


@dataclass(frozen=True)
class WindowBucket:
    """5 時間枠 1 つ分。

    by_project はプロジェクト名 → 使用量(API 換算額 × 倍率)。
    by_model は追跡するモデルの表示名(例: "Fable")→ 使用量。設定 report.models で決めたものだけ入る。
    five_hour_pct はその枠のリセット直前に記録した 5 時間枠の使用率(記録がなければ None)。
    """
    start: datetime
    end: datetime
    by_project: Mapping[str, float]
    five_hour_pct: float | None = None
    by_model: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Axis:
    """使用量の縦軸の上限(これまでの最大値。グラフの一番上がちょうどこの値になる)。

    week_total: 週の合計(累積グラフの左軸)/ window: 5 時間枠 1 つの合計(棒グラフの左軸)/
    models: モデル別の週の合計(累積グラフの右軸。例: {"Fable": …})。
    最大値が更新されたら、それ以降のレポートから上限を上げる(作成済みのレポートは作り直さない)。
    """
    week_total: float
    window: float
    models: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class NonCodeUsage:
    """Claude Code 以外の利用(Cowork など)が、週全体の上限の消費に占める割合。使用量(トークン)は分からない。"""
    name: str
    share_pct: float
    estimated: bool  # True: 使用率の記録からの推定 / False: 設定に手で入れた値


FROM_RECORDS = "records"    # その週の使用率の記録から推定
FROM_OBSERVED = "observed"  # 画面の値(全モデル%・model%)と Claude Code 以外の割合から計算
FROM_DEFAULT = "default"    # 設定 metric.model_factors の既定値


@dataclass(frozen=True)
class ModelFactor:
    """その週に使ったモデルの倍率(設定 metric.weekly_factor の model)。

    source は FROM_RECORDS / FROM_OBSERVED / FROM_DEFAULT。intervals は記録から推定するのに使えた区間の数。
    """
    model: str
    value: float
    source: str
    intervals: int = 0

    @property
    def estimated(self) -> bool:
        return self.source != FROM_DEFAULT


@dataclass(frozen=True)
class WeekReport:
    week_start: datetime
    week_end: datetime
    limit_points: list[LimitPoint]
    windows: list[WindowBucket]
    projects: list[ProjectUsage]
    prev_final_pct: float | None = None
    unparsed_lines: int = 0
    is_demo: bool = False
    notes: list[str] = field(default_factory=list)
    non_code: list[NonCodeUsage] = field(default_factory=list)
    factor: ModelFactor | None = None
    # モデル別の週間上限の使用率(表示名 → 時刻順の点)。取得元は Claude Code の /usage の記録と画面の値。ない週もある
    model_limit_points: Mapping[str, list[LimitPoint]] = field(default_factory=dict)
    dev_time: timedelta = timedelta()  # 開発時間(全セッションをまとめて計算。並行した分は 1 回に数える)

    @property
    def model_limits(self) -> dict[str, LimitPoint]:
        """モデル別の週間上限の使用率の、それぞれ最後の値。"""
        return {name: max(points, key=lambda p: p.ts) for name, points in self.model_limit_points.items() if points}

    @property
    def total_weighted(self) -> float:
        return sum(p.weighted for p in self.projects)

    @property
    def final_pct(self) -> float | None:
        return max(self.limit_points, key=lambda p: p.ts).pct if self.limit_points else None

    def model_total(self, name: str) -> float:
        return sum(w.by_model.get(name, 0.0) for w in self.windows)

    @property
    def max_window_total(self) -> float:
        return max((sum(w.by_project.values()) for w in self.windows), default=0.0)

    @property
    def limit_hours(self) -> int:
        """週次上限の使用率の記録が何時間分あるか(記録のある「時」の数)。"""
        return len({p.ts.replace(minute=0, second=0, microsecond=0) for p in self.limit_points})
