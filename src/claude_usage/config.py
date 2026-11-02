"""設定ファイルの読み込み。

数値・パスはすべて設定ファイルから取り、コードには既定値を持たない。
設定ファイルの探索順: 環境変数 CLAUDE_USAGE_CONFIG → USER_CONFIG_PATH → リポジトリの config.example.toml
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ENV_CONFIG = "CLAUDE_USAGE_CONFIG"
USER_CONFIG_PATH = Path("~/Library/Application Support/claude-usage/config.toml").expanduser()
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "config.example.toml"
CU_SCRIPT = REPO_ROOT / "scripts" / "cu"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Price:
    """API の単価 [ドル / 100 万トークン]。"""
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class Weights:
    """使用量の重み: モデルごとの API の単価(API 換算額にする)と、モデルごとの倍率。

    prices はモデル ID の先頭 → 単価(長い先頭ほど優先。"claude-opus" のような系統の単価を、知らないモデルの代わりに使う)。
    learned は会話ログの費用の記録から推定した単価: モデル ID → ((この日時から, 単価), …)(日時の古い順)。prices より優先。
    model_factors はモデル ID の先頭 → ((この日時から, 倍率), …)。最初の日時より前と、当てはまらないモデルの倍率は 1。
    """
    prices: Mapping[str, Price]
    model_factors: Mapping[str, tuple[tuple[datetime, float], ...]] = field(default_factory=dict)
    learned: Mapping[str, tuple[tuple[datetime, Price], ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceLearning:
    """会話ログの費用の記録(Claude Code が計算した API 換算額)から単価を推定する条件。"""
    min_records: int    # 単価が合わない記録がこの数以上続いたら、新しい単価を推定する
    tolerance: float    # 記録の費用と単価からの見積もりのずれの許容(割合。0.03 = 3%)


@dataclass(frozen=True)
class ReportOptions:
    max_slices: int
    others_min_pct: float
    marker_max_points: int
    # 使用量を個別にグラフにするモデル。表示名 → モデル ID の先頭(例: {"Fable": "claude-fable"})
    models: Mapping[str, str] = field(default_factory=dict)
    # 週次上限の使用率のグラフを載せるのに必要な記録の時間数
    min_limit_hours: int = 0
    # 開発時間で、続けて作業していたとみなす応答の間隔の上限
    dev_time_gap: timedelta = timedelta()


@dataclass(frozen=True)
class ReminderOptions:
    """デスクトップアプリの使用量の記録が止まったときに、使用量メニューを開くよう促す通知。"""
    enabled: bool
    stale: timedelta          # 最新の記録がこれより古ければ止まっているとみなす
    repeat: timedelta         # 一度知らせたら次に知らせるまで空ける時間
    active_from_hour: int     # 通知を出す時間帯(この時から)
    active_until_hour: int    # (この時の前まで)
    tray_notice: timedelta    # 使用量メニューを最後に開いてからこの時間が経ったら、記録が止まる前に知らせる
    tray_window: timedelta    # アプリが記録を続ける時間(使用量メニューを開いてから)


@dataclass(frozen=True)
class FactorOptions:
    """週ごとのモデルの倍率の推定。

    1. その週の使用率の記録の区間から、model とそれ以外の消費の比を最小二乗で求める。
    2. 1 ができず、画面の値(observed)と Claude Code 以外の割合(non_code.weeks)が分かる週は、その値から求める。
    """
    model: str                # 推定するモデル ID の先頭(例 "claude-fable")
    max_gap: timedelta        # 推定に使う記録の区間の間隔の上限
    min_intervals: int        # 推定に必要な区間(Claude Code を使った区間)の数
    min_model_intervals: int  # そのうち model を使った区間・それ以外を使った区間の、それぞれの最小数
    min_factor: float         # 推定値がこの範囲の外なら使わない(区間が少ない・Claude Code 以外の利用が多いと外れやすい)
    max_factor: float
    model_limit_share: float  # model の週間上限が週次上限の何 % か(画面の model% を全モデルの pt に直すのに使う)


CODE_PRODUCT = "Claude Code"  # 製品別使用状況のうち Claude Code の名前(それ以外の製品が Claude Code 以外の利用)


@dataclass(frozen=True)
class Observation:
    """使用量の画面(スクリーンショットなど)で見た値。

    all は全モデルの週次%、models はモデルの表示名 → 週間上限の %、products は「今週の製品別使用状況」の
    製品名 → 割合 %(Claude Code・チャット・Cowork など。画面にない週は空)。
    """
    ts: datetime
    all_pct: float | None
    models: Mapping[str, float] = field(default_factory=dict)
    products: Mapping[str, float] = field(default_factory=dict)

    @property
    def non_code_pct(self) -> float | None:
        """製品別の割合のうち Claude Code 以外の合計 [%]。製品別の割合がなければ None。"""
        return 100 - self.products[CODE_PRODUCT] if self.products else None


@dataclass(frozen=True)
class ClaudeCodeOptions:
    """Claude Code の /usage と同じ値(使用率)を、定期実行のたびに Claude Code から取得して記録する。"""
    enabled: bool
    executables: tuple[str, ...]  # 実行ファイルの場所(~ と * を使える)。見つかったもののうち版の番号が最も大きいものを使う
    timeout: float                # 応答を待つ時間の上限 [秒]
    compare_gap: timedelta        # デスクトップアプリの記録との照合で、前後の記録とみなす時間の幅
    # 一本化の目安(設計書 §2.13): 次を満たす週が consolidate_weeks 週続いたら、/usage の記録だけにしてよいとする
    consolidate_weeks: int
    consolidate_min_compared: int      # 照合できた記録の最小数
    consolidate_max_failure_pct: float  # 取得の失敗の割合の上限 [%]


@dataclass(frozen=True)
class ActivityOptions:
    """活動の再生画面(cu activity)。見た目の定数は描画側に置き、ここには値の定義に関わるものだけを置く。"""
    min_responses: int            # 点として出すセッションの応答数の下限(残りはプロジェクトの小さな実行にまとめる)
    bin: timedelta                # 再生の時間の刻み
    half_life: timedelta          # 動きの値(応答数の指数減衰)の半減期
    heat_percentile: float        # 全期間のこの百分位の動きの値を、色の上限にする [%]
    fade_level: float             # 最後の応答の後、動きの段階(0〜1)がこれを下回ったらセッションを消す
    default_branches: frozenset[str]  # 関係に使わないブランチ(多くのセッションが共有するもの)
    max_file_sessions: int        # 同じプロジェクトでこれより多くのセッションが触るファイルは、関係とみなさない
    refresh: timedelta            # 再生画面を作り直す最短の間隔(会話ログが更新されたときだけ作り直す)
    mature_usd: float             # 星雲が生まれたての姿から、そのプロジェクトの姿になりきる API 換算額 [ドル]
    dormant: timedelta            # 動きがないと星雲が衰え始めるまでの時間
    dead: timedelta               # 動きがないと星雲が死んだ姿になりきるまでの時間
    categories: Mapping[str, str] = field(default_factory=dict)      # 作るもの → 星雲の形の成分
    project_kinds: Mapping[str, str] = field(default_factory=dict)   # プロジェクト名 → 寄せる作るもの


@dataclass(frozen=True)
class NonCodeOptions:
    """Claude Code 以外(Cowork など)の利用。ローカルに使用量が残らないため、週全体に占める割合 [%] で扱う。"""
    max_gap: timedelta        # 推定に使う記録の間隔の上限(これより長い区間は、いつ使ったか分からないので使わない)
    min_known_pct: float      # 推定に使えた区間の増分が、週の増分のこの割合 [%] 以上のときだけ推定を載せる
    estimate_name: str        # 推定した値の表示名
    # 手で入れた値: 週の終わり(リセット)の日付 → {表示名: 割合 [%]}。推定より優先する
    weeks: Mapping[date, Mapping[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    transcripts_root: Path
    plan_usage_history: Path
    app_config: Path          # デスクトップアプリの設定(使用量メニューを最後に開いた時刻だけを読む)
    desktop_sessions: Path    # デスクトップアプリのセッション情報(ID の項目だけを読む)
    activity_html: Path       # 活動の再生画面の保存先
    data_dir: Path
    demo_dir: Path
    pdf_dir: Path
    timezone: ZoneInfo
    reset_anchor: datetime
    cycle: timedelta
    window: timedelta
    weights: Weights
    price_learning: PriceLearning
    worktree_patterns: tuple[str, ...]
    project_aliases: Mapping[str, str]
    report: ReportOptions
    pdf_timeout_seconds: float
    notify: bool
    notify_active_idle: timedelta  # 最後の操作からこの時間以内なら Mac を操作中とみなし、通知を出す
    schedule_interval: timedelta
    schedule_label: str
    reminder: ReminderOptions
    non_code: NonCodeOptions
    weekly_factor: FactorOptions
    claude_code: ClaudeCodeOptions
    activity: ActivityOptions
    observed: tuple[Observation, ...] = ()  # 時刻順

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def limit_archive_path(self) -> Path:
        """デスクトップアプリの使用率の記録のコピー(アプリは 30 日分しか残さないため)。"""
        return self.data_dir / "plan-usage-archive.json"

    @property
    def activity_layout_path(self) -> Path:
        """活動の再生画面の配置の記憶(作り直しても、すでにあるものを同じ場所に同じ姿で出すため)。"""
        return self.data_dir / "activity-layout.json"

    @property
    def usage_log_path(self) -> Path:
        """Claude Code から取得した /usage の値の記録(1 行 1 回)。"""
        return self.data_dir / "claude-code-usage.jsonl"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "tick.log"

    @property
    def pdf_helper_path(self) -> Path:
        return self.data_dir / "helpers" / "html2pdf"


def _require(table: Mapping[str, Any], section: str, key: str) -> Any:
    try:
        return table[section][key]
    except (KeyError, TypeError):
        raise ConfigError(f"設定 [{section}] {key} がありません") from None


def _number(table: Mapping[str, Any], section: str, key: str, *, integer: bool = False,
            minimum: float = 0) -> float:
    value = _require(table, section, key)
    allowed = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, allowed) or value < minimum:
        kind = "整数" if integer else "数値"
        raise ConfigError(f"[{section}] {key} は {minimum:g} 以上の{kind}にしてください")
    return value


def _bool(table: Mapping[str, Any], section: str, key: str) -> bool:
    value = _require(table, section, key)
    if not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} は true / false にしてください")
    return value


def _str_map(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) and v
                                                 for k, v in value.items()):
        raise ConfigError(f"{where} は「名前 = \"文字列\"」の形で書いてください")
    return dict(value)


def _path(table: Mapping[str, Any], key: str) -> Path:
    return Path(_require(table, "paths", key)).expanduser()


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _prices(value: Any) -> dict[str, Price]:
    """[metric.prices]: "モデル ID の先頭" = [入力, 5 分書き込み, 1 時間書き込み, キャッシュ読み込み, 出力](ドル/100 万トークン)。

    どのモデルにも当てはまるよう、"claude" の行が必要(知らない系統のモデルの代わり)。
    """
    where = "[metric.prices]"
    if not isinstance(value, Mapping) or "claude" not in value:
        raise ConfigError(f"{where} は「\"モデル ID の先頭\" = [5 つの単価]」の形で、\"claude\" の行を含めてください")
    out = {}
    for prefix, row in value.items():
        if (not isinstance(row, list) or len(row) != 5
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in row)):
            raise ConfigError(f"{where} {prefix!r} は [入力, 5 分書き込み, 1 時間書き込み, キャッシュ読み込み, 出力] の"
                              "0 以上の数 5 つにしてください")
        out[prefix] = Price(*(float(v) for v in row))
    return out


def _price_learning(table: Mapping[str, Any]) -> PriceLearning:
    section = _require(table, "metric", "price_learning")
    sub = {"metric.price_learning": section if isinstance(section, Mapping) else {}}
    tolerance = float(_number(sub, "metric.price_learning", "tolerance_pct"))
    if tolerance >= 100:
        raise ConfigError("[metric.price_learning] tolerance_pct は 100 未満にしてください")
    return PriceLearning(
        min_records=int(_number(sub, "metric.price_learning", "min_records", integer=True, minimum=1)),
        tolerance=tolerance / 100,
    )


def _model_factors(value: Any) -> dict[str, tuple[tuple[datetime, float], ...]]:
    """[metric.model_factors]: モデル ID の先頭 = [{ from = "時差付きの日時", factor = 倍率 }, …]"""
    where = "[metric.model_factors]"
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} は「\"モデル ID の先頭\" = [{{ from = …, factor = … }}]」の形で書いてください")
    out = {}
    for prefix, periods in value.items():
        if not prefix or not isinstance(periods, list) or not periods:
            raise ConfigError(f"{where} {prefix!r} には期間の配列を書いてください")
        parsed = []
        for p in periods:
            try:
                start = datetime.fromisoformat(p["from"])
            except (TypeError, ValueError, KeyError):
                raise ConfigError(f"{where} {prefix!r} の from は時差付きの日時で書いてください") from None
            if start.tzinfo is None or not _positive(p.get("factor")):
                raise ConfigError(f"{where} {prefix!r} は時差付きの from と、0 より大きい factor を書いてください")
            parsed.append((start, float(p["factor"])))
        out[prefix] = tuple(sorted(parsed))
    return out


def _weekly_factor(table: Mapping[str, Any]) -> FactorOptions:
    section = _require(table, "metric", "weekly_factor")
    if not isinstance(section, Mapping):
        raise ConfigError("[metric.weekly_factor] がありません")
    sub = {"metric.weekly_factor": section}
    model = _require(sub, "metric.weekly_factor", "model")
    lo = float(_number(sub, "metric.weekly_factor", "min_factor"))
    hi = float(_number(sub, "metric.weekly_factor", "max_factor"))
    if not isinstance(model, str) or not model or not 0 < lo < hi:
        raise ConfigError("[metric.weekly_factor] は model を空でない文字列、0 < min_factor < max_factor にしてください")
    share = float(_number(sub, "metric.weekly_factor", "model_limit_share_pct"))
    if not 0 < share <= 100:
        raise ConfigError("[metric.weekly_factor] model_limit_share_pct は 0 より大きく 100 以下にしてください")
    return FactorOptions(
        model=model,
        max_gap=timedelta(minutes=_number(sub, "metric.weekly_factor", "max_gap_minutes", minimum=1)),
        min_intervals=int(_number(sub, "metric.weekly_factor", "min_intervals", integer=True, minimum=2)),
        min_model_intervals=int(_number(sub, "metric.weekly_factor", "min_model_intervals", integer=True, minimum=1)),
        min_factor=lo,
        max_factor=hi,
        model_limit_share=share,
    )


def _percentages(values: Mapping[str, Any]) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100 for v in values.values())


def _observed(table: Mapping[str, Any]) -> tuple[Observation, ...]:
    """[observed.points]: "時差付きの日時" = { all = 全モデル%, <モデルの表示名> = その週間上限の%,
    products = { "Claude Code" = 割合, "チャット" = 割合, … } }(products は省ける)"""
    points = _require(table, "observed", "points")
    if not isinstance(points, Mapping):
        raise ConfigError("[observed.points] は「\"日時\" = { all = …, モデル名 = … }」の形で書いてください")
    out = []
    for key, values in points.items():
        try:
            ts = datetime.fromisoformat(key)
        except ValueError:
            ts = None
        if ts is None or ts.tzinfo is None:
            raise ConfigError(f"[observed.points] のキーは時差付きの日時にしてください: {key}")
        products = values.get("products", {}) if isinstance(values, Mapping) else None
        numbers = {k: v for k, v in values.items() if k != "products"} if isinstance(values, Mapping) else {}
        if (not numbers or not _percentages(numbers) or not isinstance(products, Mapping)
                or (products and (CODE_PRODUCT not in products or not _percentages(products)))):
            raise ConfigError(f"[observed.points] {key} は「all = 0〜100, モデル名 = 0〜100」の形で書いてください"
                              f"(products を書くときは \"{CODE_PRODUCT}\" を含む 0〜100 の割合)")
        all_pct = numbers.get("all")
        out.append(Observation(ts, None if all_pct is None else float(all_pct),
                               {k: float(v) for k, v in numbers.items() if k != "all"},
                               {k: float(v) for k, v in products.items()}))
    return tuple(sorted(out, key=lambda o: o.ts))


def _non_code(table: Mapping[str, Any]) -> NonCodeOptions:
    min_known = float(_number(table, "non_code", "min_known_pct"))
    name = _require(table, "non_code", "estimate_name")
    if min_known > 100 or not isinstance(name, str) or not name:
        raise ConfigError("[non_code] min_known_pct は 100 以下、estimate_name は空でない文字列にしてください")
    table_weeks = _require(table, "non_code", "weeks")
    if not isinstance(table_weeks, Mapping):
        raise ConfigError("[non_code.weeks] は「\"週の終わりの日付\" = { 表示名 = 割合 }」の形で書いてください")
    weeks = {}
    for key, shares in table_weeks.items():
        try:
            day = date.fromisoformat(key)
        except ValueError:
            raise ConfigError(f"[non_code.weeks] のキーは週の終わりの日付(例 \"2026-10-20\")にしてください: {key}") from None
        if (not isinstance(shares, Mapping) or not all(k and _positive(v) for k, v in shares.items())
                or sum(shares.values()) >= 100):
            raise ConfigError(f"[non_code.weeks] {key} は「表示名 = 割合 [%]」の形で、合計 100 未満にしてください")
        weeks[day] = {k: float(v) for k, v in shares.items()}
    return NonCodeOptions(
        max_gap=timedelta(minutes=_number(table, "non_code", "max_gap_minutes", minimum=1)),
        min_known_pct=min_known,
        estimate_name=name,
        weeks=weeks,
    )


def _claude_code(table: Mapping[str, Any]) -> ClaudeCodeOptions:
    executables = _require(table, "claude_code", "executables")
    if not isinstance(executables, list) or not executables or not all(isinstance(p, str) and p for p in executables):
        raise ConfigError("[claude_code] executables は実行ファイルの場所(文字列)の配列にしてください")
    return ClaudeCodeOptions(
        enabled=_bool(table, "claude_code", "enabled"),
        executables=tuple(executables),
        timeout=float(_number(table, "claude_code", "timeout_seconds", minimum=1)),
        compare_gap=timedelta(minutes=_number(table, "claude_code", "compare_gap_minutes", minimum=1)),
        consolidate_weeks=int(_number(table, "claude_code", "consolidate_weeks", integer=True, minimum=1)),
        consolidate_min_compared=int(_number(table, "claude_code", "consolidate_min_compared", integer=True, minimum=1)),
        consolidate_max_failure_pct=float(_number(table, "claude_code", "consolidate_max_failure_pct")),
    )


def _activity(table: Mapping[str, Any]) -> ActivityOptions:
    percentile = float(_number(table, "activity", "heat_percentile", minimum=1))
    fade = float(_number(table, "activity", "fade_level"))
    branches = _require(table, "activity", "default_branches")
    if percentile > 100 or not 0 < fade < 1:
        raise ConfigError("[activity] は heat_percentile を 100 以下、fade_level を 0 より大きく 1 未満にしてください")
    if not isinstance(branches, list) or not all(isinstance(b, str) for b in branches):
        raise ConfigError("[activity] default_branches は文字列の配列にしてください")
    categories = _str_map(_require(table, "activity", "categories"), "[activity.categories]")
    project_kinds = _str_map(_require(table, "activity", "project_kinds"), "[activity.project_kinds]")
    unknown = sorted(set(project_kinds.values()) - set(categories))
    if unknown:
        raise ConfigError(f"[activity.project_kinds] の種類が [activity.categories] にありません: {', '.join(unknown)}")
    dormant = timedelta(days=_number(table, "activity", "dormant_days", minimum=1))
    dead = timedelta(days=_number(table, "activity", "dead_days", minimum=1))
    if dead <= dormant:
        raise ConfigError("[activity] は dormant_days < dead_days にしてください")
    return ActivityOptions(
        min_responses=int(_number(table, "activity", "min_responses", integer=True, minimum=1)),
        bin=timedelta(minutes=_number(table, "activity", "bin_minutes", minimum=1)),
        half_life=timedelta(minutes=_number(table, "activity", "half_life_minutes", minimum=1)),
        heat_percentile=percentile,
        fade_level=fade,
        default_branches=frozenset(branches),
        max_file_sessions=int(_number(table, "activity", "max_file_sessions", integer=True, minimum=2)),
        refresh=timedelta(minutes=_number(table, "activity", "refresh_minutes", minimum=1)),
        mature_usd=float(_number(table, "activity", "mature_usd", minimum=1)),
        dormant=dormant,
        dead=dead,
        categories=categories,
        project_kinds=project_kinds,
    )


def parse_config(table: Mapping[str, Any]) -> Config:
    try:
        anchor = datetime.fromisoformat(_require(table, "week", "reset_anchor"))
    except (TypeError, ValueError):
        raise ConfigError("[week] reset_anchor は時差付きの日時で書いてください(例 2026-10-27T19:00:00+09:00)") from None
    if anchor.tzinfo is None:
        raise ConfigError("[week] reset_anchor には時差(+09:00 など)を付けてください")
    patterns = _require(table, "projects", "worktree_patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ConfigError("[projects] worktree_patterns は文字列の配列にしてください")
    notify = _bool(table, "notify", "enabled")
    label = _require(table, "schedule", "label")
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise ConfigError("[schedule] label は英数字と . _ - だけで書いてください")
    active_from = int(_number(table, "reminder", "active_from_hour", integer=True))
    active_until = int(_number(table, "reminder", "active_until_hour", integer=True))
    if not active_from < active_until <= 24:
        raise ConfigError("[reminder] は active_from_hour < active_until_hour ≦ 24 にしてください")
    tray_notice = timedelta(hours=_number(table, "reminder", "tray_notice_hours", minimum=1))
    tray_window = timedelta(hours=_number(table, "reminder", "tray_window_hours", minimum=1))
    if not tray_notice < tray_window:
        raise ConfigError("[reminder] は tray_notice_hours < tray_window_hours にしてください")
    return Config(
        transcripts_root=_path(table, "transcripts_root"),
        plan_usage_history=_path(table, "plan_usage_history"),
        app_config=_path(table, "app_config"),
        desktop_sessions=_path(table, "desktop_sessions"),
        activity_html=_path(table, "activity_html"),
        data_dir=_path(table, "data_dir"),
        demo_dir=_path(table, "demo_dir"),
        pdf_dir=_path(table, "pdf_dir"),
        timezone=ZoneInfo(_require(table, "week", "timezone")),
        reset_anchor=anchor,
        cycle=timedelta(days=_number(table, "week", "cycle_days", minimum=1)),
        window=timedelta(hours=_number(table, "week", "window_hours", minimum=1)),
        weights=Weights(prices=_prices(_require(table, "metric", "prices")),
                        model_factors=_model_factors(_require(table, "metric", "model_factors"))),
        price_learning=_price_learning(table),
        worktree_patterns=tuple(patterns),
        project_aliases=_str_map(_require(table, "projects", "aliases"), "[projects.aliases]"),
        report=ReportOptions(
            max_slices=int(_number(table, "report", "max_slices", integer=True, minimum=1)),
            others_min_pct=float(_number(table, "report", "others_min_pct")),
            marker_max_points=int(_number(table, "report", "marker_max_points", integer=True)),
            models=_str_map(_require(table, "report", "models"), "[report.models]"),
            min_limit_hours=int(_number(table, "report", "min_limit_hours", integer=True)),
            dev_time_gap=timedelta(minutes=_number(table, "report", "dev_time_gap_minutes", minimum=1)),
        ),
        pdf_timeout_seconds=float(_number(table, "pdf", "timeout_seconds", minimum=1)),
        notify=notify,
        notify_active_idle=timedelta(minutes=_number(table, "notify", "active_idle_minutes", minimum=1)),
        schedule_interval=timedelta(minutes=_number(table, "schedule", "interval_minutes", minimum=1)),
        schedule_label=label,
        reminder=ReminderOptions(
            enabled=_bool(table, "reminder", "enabled"),
            stale=timedelta(minutes=_number(table, "reminder", "stale_minutes", minimum=1)),
            repeat=timedelta(hours=_number(table, "reminder", "repeat_hours")),
            active_from_hour=active_from,
            active_until_hour=active_until,
            tray_notice=tray_notice,
            tray_window=tray_window,
        ),
        non_code=_non_code(table),
        weekly_factor=_weekly_factor(table),
        claude_code=_claude_code(table),
        activity=_activity(table),
        observed=_observed(table),
    )


def find_config_path(env: Mapping[str, str]) -> Path:
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG]).expanduser()
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    return EXAMPLE_CONFIG_PATH


def load_config(env: Mapping[str, str] | None = None) -> Config:
    path = find_config_path(os.environ if env is None else env)
    with path.open("rb") as fh:
        return parse_config(tomllib.load(fh))
