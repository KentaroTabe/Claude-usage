"""活動の再生画面(cu activity)のデータを作る。

セッションを星、プロジェクトを星雲として、全期間の動きを再生する画面の入力を組み立てる(設計書 §2.15)。
計算(セッションの分類・関係・配置・動きの値)は純粋関数にし、読み込みと書き出しは make_activity にまとめる。
画面は今後の使用に合わせて作り直すので、プロジェクトの位置とセッションの位置は data_dir/activity-layout.json に
残して引き継ぐ(作り直しても、すでにあるものは同じ場所に出る)。星雲は使うほど大きくなり続け、互いに触れないよう押し広げる。
星雲の姿は種類を 1 つに決めず、プロジェクトの特徴を連続した値で持って混ぜ合わせる(全く同じ星雲ができないように):
作るものの比率(フォルダのファイル構成から。名前と拡張子だけを見て、中身は読まない。設定 [activity.project_kinds] で寄せられる)と、
使い方の特徴(並行の作業・休んでいた時間・使い方の波・モデルの使い分け・勢い。会話ログの記録から)。
会話の本文は読まない。画面に出すのはプロジェクト名・時刻・応答数・API 換算額だけで、ブランチ名やファイルのパスは
関係の判定にだけ使い、画面のデータには入れない。

ここにある数値は配置の見た目の定数で、値の定義に関わるものは設定 [activity] に置く。
"""
from __future__ import annotations

import json
import math
import os
import random
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Collection, Iterable, Mapping, Sequence

from .config import ActivityOptions, Config, Weights
from .metrics import weighted
from .projects import ProjectResolver
from .render.activity import render_activity
from .render.svg import WEEKDAYS, time_label
from .sources import SessionLinks, UsageEvent, continued_pairs, read_desktop_sessions, read_session_links, read_usage_events
from .state import load_state, write_atomic
from .weekly import EPOCH, stored_prices
from .weeks import completed_weeks, week_containing

# 関係の種類(強い順)。同じ組に複数の関係があるときは最も強いものを使う
CONTINUED, PR, BRANCH, FILE = "continued", "pr", "branch", "file"
KINDS = (CONTINUED, PR, BRANCH, FILE)
PROJECT = "project"

# 配置の見た目の定数
RADIUS_BASE, RADIUS_SCALE = 34.0, 11.0  # 星雲の半径 = 基本 + 係数 × API 換算額の立方根(使うほど大きくなり続ける)
GAP = 34.0              # 星雲同士の最小の間隔(触れないように)
SPIRAL_STEP = 12.0      # 新しい星雲の置き場所を探す渦の刻み
WIDE, TALL = 1.7, 1.0   # 横長の画面に広がるように、渦を横に伸ばす
SPREAD_ROUNDS = 400     # 育って重なった星雲を押し広げる回数の上限
SESSION_ROUNDS = 240
SESSION_EDGE = 0.82     # セッションの定位置は星雲の半径のこの割合より内側
REST = {CONTINUED: 0.12, PR: 0.16, BRANCH: 0.2, FILE: 0.26}     # 関係のあるセッションの距離(星雲の半径を 1 として)
PULL = {CONTINUED: 0.34, PR: 0.26, BRANCH: 0.2, FILE: 0.14}
# 星雲の形の成分(assets/activity.js のシェーダーの重み w0・w1 の順)
NEBULA_SHAPES = ("emission", "pillars", "ring", "bipolar", "remnant", "reflection", "spiral", "dark")
# 作るもの(フォルダのファイル構成から比率を出す方向)と表示名
DOMAIN_NAMES = {"game": "ゲーム", "app": "アプリ", "web": "Web", "data": "AI・データ", "tool": "道具・自動化",
                "media": "画像・文書"}
DOMAIN_EXTS = {
    "game": {".cs", ".unity", ".asset", ".prefab", ".shader", ".gd", ".tscn", ".mat", ".anim", ".uasset"},
    "app": {".swift", ".m", ".mm", ".storyboard", ".xib", ".kt", ".plist", ".entitlements"},
    "web": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".html", ".css", ".scss", ".vue", ".svelte"},
    "data": {".csv", ".tsv", ".pth", ".pt", ".onnx", ".ipynb", ".parquet", ".npy", ".wav", ".m4a", ".mp3", ".pcm", ".jsonl"},
    "tool": {".py", ".sh", ".rb", ".go", ".rs", ".toml", ".yaml", ".yml"},
    "media": {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".blend", ".stl", ".md", ".txt"},
}
GAME_MARKS = {"ProjectSettings", "*.unity", "project.godot"}
APP_MARKS = {"*.xcodeproj", "Package.swift"}
WEB_MARKS = {"package.json", "index.html", "vite.config.ts", "vite.config.js", "next.config.js"}
TOOL_MARKS = {"pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod"}
DOMAIN_MARKS = {"game": GAME_MARKS, "app": APP_MARKS, "web": WEB_MARKS, "tool": TOOL_MARKS}
MARK_WEIGHT = 3.0       # 目印 1 つの重み(拡張子の数は対数で数える。目印は種類をはっきり示すので重くする)
OVERRIDE_SHARE = 0.55   # 設定で寄せた作るものに足す割合
SHARPEN = 1.5           # 成分の重みを強調する(主な形が読み取れるように)
MIN_SHAPE = 0.06        # これより小さい成分は描かない(生成の手間を減らす)
MAX_SHAPES = 3
SCAN_SKIP = {"node_modules", ".venv", "venv", "Library", "obj", "bin", "build", "dist", "DerivedData", "Temp", "Logs",
             "logs", "Pods", "__pycache__"}   # 依存物・生成物のフォルダには入らない
SCAN_DEPTH = 3
SCAN_MAX_FILES = 5000
HIGHLIGHT_DAYS = 3      # 見どころとして選べるようにする、応答の多い日の数


@dataclass(frozen=True)
class Edge:
    """主なセッション同士の関係。a < b。kind は最も強い関係、weight はその関係の根拠の数。"""
    a: str
    b: str
    kind: str
    weight: int


def split_sessions(events: Iterable[UsageEvent], min_responses: int) -> tuple[dict[str, list[UsageEvent]],
                                                                               dict[str, list[UsageEvent]]]:
    """応答をセッションごとに分け、(主なセッション, 小さな実行) にする。サブエージェントの応答も親のセッションに含める。"""
    sessions: dict[str, list[UsageEvent]] = defaultdict(list)
    for e in events:
        sessions[e.session_id].append(e)
    major = {k: v for k, v in sessions.items() if len(v) >= min_responses}
    minor = {k: v for k, v in sessions.items() if len(v) < min_responses}
    return major, minor


def session_edges(major: Collection[str], project_of: Mapping[str, str], links: SessionLinks,
                  continued: Iterable[tuple[str, str]], options: ActivityOptions) -> list[Edge]:
    """主なセッション同士の関係を作る。同じ組は最も強い種類にまとめる。

    ブランチ名とファイルのパス(相対パスが多い)は、別のリポジトリでは同じ名前でも関係がないので、同じプロジェクトの中に
    限る。main などのブランチと、多くのセッションが触るファイルは関係の根拠にしない。
    """
    found: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)

    def link(members: Iterable[str], kind: str) -> None:
        ordered = sorted(members)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                found[(a, b)][kind] += 1

    def by_project(members: Iterable[str]) -> Iterable[set[str]]:
        groups: dict[str, set[str]] = defaultdict(set)
        for s in members:
            groups[project_of[s]].add(s)
        return groups.values()

    for a, b in continued:
        if a != b and a in major and b in major:
            link((a, b), CONTINUED)
    for members in links.prs.values():
        link((s for s in members if s in major), PR)
    for branch, members in links.branches.items():
        if branch not in options.default_branches:
            for group in by_project(s for s in members if s in major):
                link(group, BRANCH)
    for members in links.files.values():
        for group in by_project(s for s in members if s in major):
            if len(group) <= options.max_file_sessions:
                link(group, FILE)
    edges = []
    for (a, b), kinds in sorted(found.items()):
        kind = next(k for k in KINDS if k in kinds)
        edges.append(Edge(a, b, kind, kinds[kind]))
    return edges


def nebula_radius(usd: float) -> float:
    """星雲の半径。絶対的な式なので、これからも使うほど大きくなる(今の最大に合わせて縮めたりしない)。"""
    return RADIUS_BASE + RADIUS_SCALE * math.cbrt(usd)


def stable_number(name: str) -> int:
    """名前から決まる数(実行のたびに変わらない)。"""
    return zlib.crc32(name.encode("utf-8"))


def _touching(a: tuple[float, float], ra: float, b: tuple[float, float], rb: float) -> bool:
    return math.dist(a, b) < ra + rb + GAP


def free_spot(radius: float, placed: Mapping[str, tuple[float, float]], radii: Mapping[str, float]) -> tuple[float, float]:
    """中心に近い、どの星雲にも触れない場所を、横長の渦に沿って探す。"""
    k = 0
    while True:
        d = SPIRAL_STEP * math.sqrt(k)
        spot = (d * math.cos(k * 2.399963) * WIDE, d * math.sin(k * 2.399963) * TALL)
        if not any(_touching(spot, radius, at, radii[name]) for name, at in placed.items()):
            return round(spot[0], 1), round(spot[1], 1)
        k += 1


def spread(placed: dict[str, tuple[float, float]], radii: Mapping[str, float]) -> None:
    """育って近づきすぎた星雲を、触れなくなるまで少しずつ押し広げる(placed を書き換える)。大きいものほど動かない。"""
    names = sorted(placed)
    for _ in range(SPREAD_ROUNDS):
        moved = False
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if not _touching(placed[a], radii[a], placed[b], radii[b]):
                    continue
                (ax, ay), (bx, by) = placed[a], placed[b]
                dx, dy = bx - ax, by - ay
                d = math.hypot(dx, dy) or 1.0
                push = radii[a] + radii[b] + GAP - d + 0.5
                share = radii[b] / (radii[a] + radii[b])     # 小さい方が大きく動く
                placed[a] = (ax - dx / d * push * share, ay - dy / d * push * share)
                placed[b] = (bx + dx / d * push * (1 - share), by + dy / d * push * (1 - share))
                moved = True
        if not moved:
            return


def place_projects(radii: Mapping[str, float], previous: Mapping[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """星雲の中心を決める。残っている位置はそのまま使い、新しいものだけ空いた場所に置き、育って触れたら押し広げる。"""
    placed = {name: tuple(previous[name]) for name in sorted(radii) if name in previous}
    for name in sorted((n for n in radii if n not in placed), key=lambda n: (-radii[n], n)):
        placed[name] = free_spot(radii[name], placed, radii)
    spread(placed, radii)
    return {name: (round(x, 1), round(y, 1)) for name, (x, y) in placed.items()}


def composition(marks: set[str], exts: Mapping[str, int], override: str | None = None) -> dict[str, float]:
    """作るものの比率(合計 1)。拡張子の数(対数)と目印から出す。override は設定で寄せる作るもの。フォルダが空なら空。"""
    score = {d: sum(math.log1p(exts.get(e, 0)) for e in names) + MARK_WEIGHT * len(marks & DOMAIN_MARKS.get(d, set()))
             for d, names in DOMAIN_EXTS.items()}
    total = sum(score.values())
    mix = {d: v / total for d, v in score.items()} if total else {}
    if override:
        mix = {d: mix.get(d, 0.0) * (1 - OVERRIDE_SHARE) + (OVERRIDE_SHARE if d == override else 0.0) for d in DOMAIN_EXTS}
        total = sum(mix.values())
        mix = {d: v / total for d, v in mix.items() if v > 0}   # フォルダがなくても合計 1 にする
    return mix


@dataclass(frozen=True)
class Traits:
    """使い方の特徴(どれも 0〜1)。"""
    parallel: float   # サブエージェントの分の割合(並行の作業が多い)
    idle: float       # 最初から最後までのうち、使わなかった日の割合
    burst: float      # 日ごとの使用量のばらつき(変動係数)。波があるほど大きい
    warmth: float     # 温かい色に寄せるモデル(既定は Fable)の分の割合
    intensity: float  # 使った日の 1 日あたりの API 換算額(対数)
    sessions: float   # セッションの数(対数)


def usage_traits(events: Sequence[UsageEvent], weights: Weights, tz: tzinfo, warm_model: str) -> Traits:
    """そのプロジェクトの応答から、使い方の特徴を出す。"""
    usd = [weighted(e, weights) for e in events]
    total = sum(usd) or 1.0
    daily: Counter = Counter()
    for e, v in zip(events, usd):
        daily[e.ts.astimezone(tz).date()] += v
    days = sorted(daily)
    span = (days[-1] - days[0]).days + 1 if days else 1
    values = list(daily.values())
    mean = sum(values) / len(values) if values else 0.0
    spread_ = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) if values else 0.0
    return Traits(
        parallel=round(sum(v for e, v in zip(events, usd) if e.is_subagent) / total, 3),
        idle=round(1 - len(days) / span if span >= 3 else 0.0, 3),
        burst=round(min(1.0, (spread_ / mean if mean else 0.0) / 2), 3),
        warmth=round(sum(v for e, v in zip(events, usd) if e.model.startswith(warm_model)) / total, 3),
        intensity=round(min(1.0, math.log1p(mean) / math.log1p(500)), 3),
        sessions=round(min(1.0, math.log1p(len({e.session_id for e in events})) / math.log1p(60)), 3),
    )


def nebula_shape(mix: Mapping[str, float], traits: Traits, shapes: Mapping[str, str]) -> list[float]:
    """星雲の形の成分の重み(NEBULA_SHAPES の順、合計 1)。作るものの比率と、使い方の特徴(並行 → 細い筋、休み → 暗黒星雲)から。

    重みを強調して主な形が読み取れるようにし、小さすぎる成分と 4 番目以降は描かない。作るものが分からなければ散光星雲と反射星雲を混ぜる。
    """
    raw = dict.fromkeys(NEBULA_SHAPES, 0.0)
    for domain, share in mix.items():
        raw[shapes[domain]] += share
    if not mix:                       # フォルダがなく作るものが分からない
        raw["emission"] = raw["reflection"] = 0.35
    raw["remnant"] += 0.5 * traits.parallel
    raw["dark"] += 0.3 * traits.idle   # 今の衰えは別に描くので、休みの多さは控えめな影にとどめる
    sharp = {k: v ** SHARPEN for k, v in raw.items()}
    kept = dict(sorted(sharp.items(), key=lambda kv: -kv[1])[:MAX_SHAPES])
    total = sum(kept.values())
    kept = {k: v for k, v in kept.items() if v / total >= MIN_SHAPE}
    total = sum(kept.values())
    return [round(kept.get(k, 0.0) / total, 3) for k in NEBULA_SHAPES]


def describe_mix(mix: Mapping[str, float]) -> str:
    """作るものの比率の説明(例「AI・データ 6 割・Web 3 割」)。1 割未満は省く。"""
    parts = [f"{DOMAIN_NAMES[d]} {round(v * 10)} 割" for d, v in sorted(mix.items(), key=lambda kv: -kv[1]) if v >= 0.1]
    return "・".join(parts) or "不明"


def scan_folder(root: Path | None) -> tuple[set[str], Counter[str]]:
    """プロジェクトのフォルダの目印と拡張子の数を数える。名前だけを見て、ファイルの中身は読まない。"""
    marks: set[str] = set()
    exts: Counter[str] = Counter()
    if root is None or not root.is_dir():
        return marks, exts
    names = GAME_MARKS | APP_MARKS | WEB_MARKS | TOOL_MARKS
    seen = 0
    for base, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in SCAN_SKIP and not d.startswith(".")]
        if len(Path(base).relative_to(root).parts) >= SCAN_DEPTH:
            subdirs[:] = []
        for name in [*subdirs, *files]:
            suffix = Path(name).suffix.lower()
            if name in names:
                marks.add(name)
            elif f"*{suffix}" in names:
                marks.add(f"*{suffix}")
        for name in files:
            if Path(name).suffix:
                exts[Path(name).suffix.lower()] += 1
        seen += len(files)
        if seen > SCAN_MAX_FILES:
            break
    return marks, exts


def project_roots(events: Iterable[UsageEvent], project_name: Callable[[str], str]) -> dict[str, Path | None]:
    """プロジェクトごとのフォルダ: 最も多く使った作業ディレクトリから、リポジトリの一番上(.git がある所)までさかのぼる。"""
    used: dict[str, Counter[str]] = defaultdict(Counter)
    for e in events:
        used[project_name(e.project_dir)][e.project_dir] += 1
    roots: dict[str, Path | None] = {}
    for name, dirs in used.items():
        root = next((Path(d) for d, _ in dirs.most_common() if Path(d).is_dir()), None)
        while root is not None and root != root.parent and not (root / ".git").exists() and root.name != name:
            root = root.parent
        roots[name] = root if root is not None and root != root.parent else None
    return roots


def place_sessions(session_project: Mapping[str, str], edges: Sequence[Edge],
                   previous: Mapping[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """セッションの定位置を、星雲の中の相対位置(半径を 1 とする)で決める。

    残っている位置はそのまま使う。新しいセッションだけ動かし、関係のあるセッションに引き寄せる。乱数の種は名前から決める。
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for s in sorted(session_project):
        groups[session_project[s]].append(s)
    links: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        if session_project[e.a] == session_project[e.b]:
            links[e.a].append((e.b, e.kind))
            links[e.b].append((e.a, e.kind))
    out: dict[str, tuple[float, float]] = {}
    for project, members in sorted(groups.items()):
        rng = random.Random(stable_number(project))
        at = {}
        for s in members:
            angle, reach = rng.uniform(0, math.tau), SESSION_EDGE * math.sqrt(rng.random())
            at[s] = list(previous[s]) if s in previous else [math.cos(angle) * reach, math.sin(angle) * reach]
        free = [s for s in members if s not in previous]
        for round_ in range(SESSION_ROUNDS if free else 0):
            cool = 1 - round_ / SESSION_ROUNDS
            for s in free:
                fx = fy = 0.0
                for other in members:
                    if other != s:
                        dx, dy = at[s][0] - at[other][0], at[s][1] - at[other][1]
                        d2 = max(dx * dx + dy * dy, 1e-4)
                        fx += 0.004 * dx / d2
                        fy += 0.004 * dy / d2
                for other, kind in links[s]:
                    dx, dy = at[other][0] - at[s][0], at[other][1] - at[s][1]
                    d = math.hypot(dx, dy) or 1e-4
                    fx += PULL[kind] * (d - REST[kind]) * dx / d
                    fy += PULL[kind] * (d - REST[kind]) * dy / d
                at[s][0] += max(-0.05, min(0.05, fx)) * cool
                at[s][1] += max(-0.05, min(0.05, fy)) * cool
                reach = math.hypot(*at[s])
                if reach > SESSION_EDGE:
                    at[s] = [at[s][0] / reach * SESSION_EDGE, at[s][1] / reach * SESSION_EDGE]
        out.update({s: (round(at[s][0], 3), round(at[s][1], 3)) for s in members})
    return out


def bin_of(ts: datetime, start: datetime, step: timedelta) -> int:
    return int((ts - start) / step)


def binned(members: Iterable[UsageEvent], start: datetime, step: timedelta, weights: Weights) -> list[list[float]]:
    """応答を刻みごとにまとめる。[刻み, 応答数, API 換算額] の時刻順。"""
    counts: Counter[int] = Counter()
    usd: dict[int, float] = defaultdict(float)
    for e in members:
        b = bin_of(e.ts, start, step)
        counts[b] += 1
        usd[b] += weighted(e, weights)
    return [[b, counts[b], round(usd[b], 2)] for b in sorted(counts)]


def heat_values(rows: Sequence[Sequence[float]], decay: float) -> list[float]:
    """動きの値: 応答のあった刻みごとの、応答数の指数減衰の合計。decay は 1 刻みあたりの減衰率。"""
    values = []
    heat, prev = 0.0, None
    for b, n, *_ in rows:
        heat = (heat * decay ** (b - prev) if prev is not None else 0.0) + n
        values.append(heat)
        prev = b
    return values


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))] if ordered else 0.0


def day_rows(start: datetime, end: datetime, step: timedelta, rows: Iterable[Sequence[float]],
             tz: tzinfo) -> list[dict[str, object]]:
    """その地域の日付ごとの行(時間軸の棒に使う)。応答のない日も入れる。"""
    days: list[dict[str, object]] = []
    day = start.astimezone(tz).date()
    index = {}
    while day <= end.astimezone(tz).date():
        begin = datetime(day.year, day.month, day.day, tzinfo=tz)
        index[day] = len(days)
        days.append({"bin": max(bin_of(begin, start, step), 0), "date": day.isoformat(),
                     "label": f"{day.month}/{day.day}({WEEKDAYS[day.weekday()]})", "n": 0, "usd": 0.0})
        day += timedelta(days=1)
    for b, n, usd in rows:
        row = days[index[(start + step * int(b)).astimezone(tz).date()]]
        row["n"] += int(n)
        row["usd"] = round(float(row["usd"]) + usd, 2)
    return days


def activity_payload(events: Sequence[UsageEvent], links: SessionLinks, continued: Iterable[tuple[str, str]],
                     project_name: Callable[[str], str], weights: Weights, options: ActivityOptions, tz: tzinfo,
                     tz_name: str, anchor: datetime, cycle: timedelta, now: datetime,
                     memory: Mapping[str, Mapping[str, object]] | None = None,
                     folders: Mapping[str, tuple[set[str], Mapping[str, int]]] | None = None,
                     warm_model: str = "claude-fable") -> tuple[dict[str, object], dict[str, object]]:
    """再生画面に埋め込むデータと、次の回に引き継ぐ配置の記憶。events は時刻順。

    画面のデータにはプロジェクト名・種類・時刻・応答数・API 換算額だけを入れる。memory は前の回の配置の記憶、
    folders はプロジェクト名 → フォルダの (目印, 拡張子の数)(作るものの比率に使う)。warm_model は温かい色に寄せるモデル。
    """
    folders = folders or {}
    memory = memory or {}
    step = options.bin
    start = events[0].ts.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % max(int(step.total_seconds() // 60), 1))
    major, minor = split_sessions(events, options.min_responses)
    all_sessions = {**major, **minor}
    project_of = {sid: project_name(v[0].project_dir) for sid, v in all_sessions.items()}
    session_project = {sid: project_of[sid] for sid in major}
    projects = sorted(set(project_of.values()))
    edges = session_edges(major, session_project, links, continued, options)
    project_usd: Counter[str] = Counter()
    for sid, members in all_sessions.items():
        project_usd[project_of[sid]] += sum(weighted(e, weights) for e in members)
    radii = {name: nebula_radius(project_usd[name]) for name in projects}
    project_at = place_projects(radii, memory.get("projects", {}))
    by_project: dict[str, list[UsageEvent]] = defaultdict(list)
    for sid, members in all_sessions.items():
        by_project[project_of[sid]].extend(members)
    mixes = {name: composition(*folders.get(name, (set(), {})), options.project_kinds.get(name)) for name in projects}
    traits = {name: usage_traits(by_project[name], weights, tz, warm_model) for name in projects}
    session_at = place_sessions(session_project, edges, memory.get("sessions", {}))

    decay = 0.5 ** (step / options.half_life)
    rows = {sid: binned(major[sid], start, step, weights) for sid in major}
    top = percentile([h for r in rows.values() for h in heat_values(r, decay)], options.heat_percentile)
    order = sorted(major, key=lambda sid: major[sid][0].ts)
    number = {sid: i for i, sid in enumerate(order)}
    project_index = {p: i for i, p in enumerate(projects)}
    minor_by_project: dict[str, list[UsageEvent]] = defaultdict(list)
    for sid, members in minor.items():
        minor_by_project[project_of[sid]].extend(members)

    end = events[-1].ts
    total_bins = bin_of(end, start, step) + 1
    all_rows = [r for sid in major for r in rows[sid]]
    tiny = {p: binned(minor_by_project[p], start, step, weights) for p in projects}
    all_rows += [r for p in projects for r in tiny[p]]
    days = day_rows(start, end, step, all_rows, tz)
    weeks = []
    for w in [*completed_weeks(start, now, anchor, cycle), week_containing(now, anchor, cycle)]:
        first, last = w.start.astimezone(tz), (w.end - timedelta(seconds=1)).astimezone(tz)
        if w.start <= end:
            weeks.append({"label": f"{first.month}/{first.day}〜{last.month}/{last.day}",
                          "from": max(bin_of(w.start, start, step), 0),
                          "to": min(bin_of(w.end, start, step), total_bins)})
    busiest = sorted((d for d in days if d["n"]), key=lambda d: -d["n"])[:HIGHLIGHT_DAYS]
    remembered = {"projects": project_at, "sessions": session_at}
    return {
        "version": now.astimezone(timezone.utc).isoformat(),
        "radius": [RADIUS_BASE, RADIUS_SCALE],
        "matureUsd": options.mature_usd,
        "dormantBins": options.dormant / step,
        "deadBins": options.dead / step,
        "binMinutes": step.total_seconds() / 60,
        "bins": total_bins,
        "start": start.astimezone(timezone.utc).isoformat(),
        "timeZone": tz_name,
        "decay": decay,
        "top": top,
        "fade": options.fade_level,
        "projects": [{"name": p, "x": project_at[p][0], "y": project_at[p][1], "r": round(radii[p], 1),
                      "shape": nebula_shape(mixes[p], traits[p], options.categories),
                      "look": {"turb": round(0.35 + 0.65 * traits[p].burst, 3), "dust": round(0.2 + 0.8 * traits[p].idle, 3),
                               "fil": traits[p].parallel, "warm": traits[p].warmth, "stars": traits[p].sessions,
                               "core": traits[p].intensity},
                      "mix": describe_mix(mixes[p]),
                      "seed": stable_number(p) % 100000, "b": tiny[p]} for p in projects],
        "sessions": [{"p": project_index[session_project[sid]], "ux": session_at[sid][0], "uy": session_at[sid][1],
                      "label": time_label(major[sid][0].ts, tz), "b": rows[sid]} for sid in order],
        "edges": [{"a": number[e.a], "b": number[e.b], "k": e.kind} for e in edges],
        "days": days,
        "weeks": weeks,
        "highlights": [{"label": d["label"], "from": d["bin"],
                        "to": min(int(d["bin"]) + int(timedelta(days=1) / step), total_bins)}
                       for d in sorted(busiest, key=lambda d: d["bin"])],
        "smallRuns": len(minor),
    }, remembered


def load_memory(path: Path) -> dict[str, object]:
    """前の回の配置の記憶。なければ、読めなければ空(最初から配置する)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def newest_log(root: Path) -> datetime | None:
    """会話ログの最終更新のうち最も新しい時刻(全部読まずに、作り直しが要るかを判断するため)。"""
    times = [path.stat().st_mtime for path in root.rglob("*.jsonl")]
    return datetime.fromtimestamp(max(times), timezone.utc) if times else None


def activity_due(built_at: str | None, newest: datetime | None, week_end: datetime | None) -> bool:
    """再生画面を作り直すか。週 1 回、週次レポートを作った最新の週の終わり(リセット)が前回より後なら作り直す。

    一度も作っていなければ作る。会話ログがなければ作らない。作り直しに失敗したら、前回のままなので次の回にまた作る。
    (2026-11-04 ユーザーの指定。それまでは会話ログが更新されていれば 60 分ごと)
    """
    if newest is None:
        return False
    if built_at is None:
        return True
    return week_end is not None and week_end > datetime.fromisoformat(built_at)


def version_file(html: Path) -> Path:
    """画面の版の小さなファイル。開いている画面が、これを読んで新しい記録に気づく(file:// でも読める形式にする)。"""
    return html.with_suffix(".version.js")


def make_activity(config: Config, now: datetime | None = None, out: Path | None = None) -> Path | None:
    """再生画面の HTML を書き出す。会話ログに応答がなければ何もせず None。"""
    now = now or datetime.now(timezone.utc)
    events, _ = read_usage_events(config.transcripts_root, EPOCH)
    if not events:
        return None
    weights = replace(config.weights, learned=stored_prices(load_state(config.state_path)))
    resolver = ProjectResolver(config.project_aliases, config.worktree_patterns)
    payload, memory = activity_payload(events, read_session_links(config.transcripts_root),
                                       continued_pairs(read_desktop_sessions(config.desktop_sessions)), resolver, weights,
                                       config.activity, config.timezone, config.timezone.key, config.reset_anchor,
                                       config.cycle, now, load_memory(config.activity_layout_path),
                                       {name: scan_folder(root) for name, root in project_roots(events, resolver).items()},
                                       config.weekly_factor.model)
    path = out or config.activity_html
    payload["versionFile"] = version_file(path).name
    write_atomic(path, render_activity(payload))
    write_atomic(version_file(path), f'window.activityVersion = "{payload["version"]}";\n')
    write_atomic(config.activity_layout_path, json.dumps(memory, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return path
