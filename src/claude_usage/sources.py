"""取得元の読み込み。ファイルを読む部分(read_*)と、1 行・1 ファイルの中身を解釈する純粋関数(parse_*)に分ける。

- Claude Code の会話ログ(~/.claude/projects/**/*.jsonl): 応答ごとのトークン数・作業ディレクトリ・モデルと、
  Claude Code が計算した費用の記録(cost-state 行。単価の推定に使う)
- デスクトップアプリの plan-usage-history.json: 5 時間枠・週次の使用率(約 15 分ごと、30 日分)
- デスクトップアプリの config.json: 使用量メニューを最後に開いた時刻だけ(記録が止まる前の通知に使う)
- Claude Code から取得した /usage の値の記録(data_dir/claude-code-usage.jsonl。claude_code.py が書く)
- 活動の再生画面のための、セッション同士の関係の材料: 会話ログのブランチ名・PR 番号・編集したファイルのパスと、
  デスクトップアプリのセッション情報の ID の項目(2026-11-02 ユーザーの了承。設計書 §2.15)
  (プロジェクトの種類の判定に使うフォルダのファイル名・拡張子は activity.scan_folder が読む)
会話の本文は読まない(使用量の数値と付随する情報だけを取り出す)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from .config import CODE_PRODUCT, Observation

SYNTHETIC_MODEL = "<synthetic>"  # Claude Code が内部で作る応答。API を使っていないので数えない


class HistoryFormatError(Exception):
    pass


@dataclass(frozen=True)
class UsageEvent:
    """Claude Code の応答 1 件分の使用量。

    project_dir はセッションを始めたディレクトリ(read_usage_events が求める)。parse_usage_line の時点では応答時の cwd。
    """
    ts: datetime
    session_id: str
    project_dir: str
    model: str
    is_subagent: bool
    input_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output_tokens: int


@dataclass(frozen=True)
class CostRecord:
    """Claude Code が会話ログに残す費用の記録(cost-state 行)の、前の記録からの増分(モデルごと)。

    cost は Claude Code が自分の単価表で計算した API 換算額 [ドル]。cache_write は 5 分と 1 時間の書き込みの合計。
    ts は記録の時刻の近似(cost-state 行に時刻がないため、同じファイルで直前にある応答の時刻)。
    """
    ts: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost: float


@dataclass(frozen=True)
class LimitSample:
    """デスクトップアプリが記録した使用率(%)。"""
    ts: datetime
    five_hour: float | None
    seven_day: float | None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_usage_line(line: str) -> tuple[tuple[str, str], UsageEvent] | None:
    """会話ログの 1 行を解釈する。使用量の行でなければ None。JSON として壊れていれば ValueError。

    同じ応答が複数行に分かれて記録される(途中の行は 0、最後の行が確定値)ため、重複を除くキーも返す。
    """
    if '"usage"' not in line or '"assistant"' not in line:  # 大半の行をここで読み飛ばす
        return None
    rec = json.loads(line)
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    ts = rec.get("timestamp")
    if not isinstance(usage, dict) or not isinstance(ts, str) or msg.get("model") == SYNTHETIC_MODEL:
        return None
    detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    write_1h = _int(detail.get("ephemeral_1h_input_tokens")) if detail else 0
    write_5m = _int(detail.get("ephemeral_5m_input_tokens")) if detail else _int(usage.get("cache_creation_input_tokens"))
    event = UsageEvent(
        ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        session_id=str(rec.get("sessionId") or ""),
        project_dir=str(rec.get("cwd") or ""),
        model=str(msg.get("model") or ""),
        is_subagent=rec.get("isSidechain") is True,
        input_tokens=_int(usage.get("input_tokens")),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_read=_int(usage.get("cache_read_input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
    )
    key = (str(msg.get("id") or rec.get("uuid") or ""), str(rec.get("requestId") or ""))
    return key, event


COST_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "costUSD")


def parse_cost_state(line: str) -> dict[str, tuple[float, ...]] | None:
    """cost-state 行(Claude Code のセッションの累計)を、モデル ID → COST_FIELDS の値に直す。ほかの行なら None。

    モデル ID の末尾の [1m] などの指定は除く(応答の記録のモデル ID と合わせるため)。
    """
    if '"cost-state"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    usage = rec.get("modelUsage") if isinstance(rec, dict) and rec.get("type") == "cost-state" else None
    if not isinstance(usage, dict):
        return None
    out = {}
    for model, u in usage.items():
        if isinstance(u, dict):
            values = tuple(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
                           for v in (u.get(f) for f in COST_FIELDS))
            out[re.sub(r"\[.*\]$", "", str(model))] = values
    return out


def cost_increments(states: Sequence[tuple[datetime, dict[str, tuple[float, ...]]]]) -> list[CostRecord]:
    """1 つのファイルの cost-state(時刻, 累計)の並びを、モデルごとの増分の記録にする。

    累計が減ったモデルは、セッションを再開して数え直したとみなし、その値をそのまま増分にする。費用が増えていない記録は除く
    (Claude Code が単価を知らないモデルは費用が 0 のまま)。
    """
    prev: dict[str, tuple[float, ...]] = {}
    out = []
    for ts, usage in states:
        for model, cur in usage.items():
            before = prev.get(model, (0.0,) * len(COST_FIELDS))
            delta = cur if any(c < b for c, b in zip(cur, before)) else tuple(c - b for c, b in zip(cur, before))
            prev[model] = cur
            if delta[4] > 0:
                out.append(CostRecord(ts, model, int(delta[0]), int(delta[1]), int(delta[2]), int(delta[3]), delta[4]))
    return out


def encode_dir(path: str) -> str:
    """Claude Code が会話ログのフォルダ名にするときの変換(英数字以外を - にする)。"""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def launch_dir(cwd: str, folder: str) -> str:
    """応答時の cwd から、セッションを始めたディレクトリを求める。

    会話ログのフォルダ名(folder)は始めたディレクトリを encode_dir したもの。cwd かその上位のうち、変換すると folder に
    一致するものが始めたディレクトリ。見つからなければ cwd のまま(始めたディレクトリの外へ移動した場合など)。
    """
    path = PurePosixPath(cwd)
    for candidate in (path, *path.parents):
        if encode_dir(str(candidate)) == folder:
            return str(candidate)
    return cwd


@dataclass(frozen=True)
class Transcripts:
    events: list[UsageEvent]   # 応答(重複を除き時刻順)
    unparsed: int              # 使用量の行らしい(usage と assistant を含む)のに JSON として読めなかった行の数
    costs: list[CostRecord]    # 費用の記録(cost-state)の増分


def read_transcripts(root: Path, since: datetime) -> Transcripts:
    """since 以降の応答と費用の記録を読む。

    応答は重複を除いて時刻順に返す。使用量の行らしくない行は解釈せずに読み飛ばすので、読めなくても数えない。
    ログは追記されるだけなので、最終更新が since より前のファイルは読まない(軽量化のため)。
    """
    best: dict[tuple[str, str], UsageEvent] = {}
    dirs: dict[tuple[str, str], str] = {}
    unparsed = 0
    costs: list[CostRecord] = []
    for path in root.rglob("*.jsonl"):
        if path.stat().st_mtime < since.timestamp():
            continue
        folder = path.relative_to(root).parts[0]  # サブエージェントのログは <フォルダ>/<セッション>/subagents/ にある
        states: list[tuple[datetime, dict[str, tuple[float, ...]]]] = []
        last_ts: datetime | None = None
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                state = parse_cost_state(line)
                if state is not None:
                    if last_ts is not None:
                        states.append((last_ts, state))
                    continue
                try:
                    parsed = parse_usage_line(line)
                except ValueError:
                    unparsed += 1
                    continue
                if parsed is None:
                    continue
                key, event = parsed
                last_ts = event.ts
                if event.ts < since:
                    continue
                prev = best.get(key)
                if prev is None or event.output_tokens >= prev.output_tokens:
                    cache_key = (event.project_dir, folder)
                    if cache_key not in dirs:
                        dirs[cache_key] = launch_dir(event.project_dir, folder)
                    best[key] = replace(event, project_dir=dirs[cache_key])
        costs += [c for c in cost_increments(states) if c.ts >= since]
    return Transcripts(sorted(best.values(), key=lambda e: e.ts), unparsed, sorted(costs, key=lambda c: c.ts))


def read_usage_events(root: Path, since: datetime) -> tuple[list[UsageEvent], int]:
    """since 以降の応答(重複を除き時刻順)と、読めなかった使用量の行の数。費用の記録が要らないとき用。"""
    t = read_transcripts(root, since)
    return t.events, t.unparsed


def parse_plan_usage_history(text: str) -> list[LimitSample]:
    """plan-usage-history.json の中身を解釈する(version 1・2 に対応)。知らない形式なら HistoryFormatError。"""
    try:
        data = json.loads(text)
    except ValueError:
        raise HistoryFormatError("JSON として読めません") from None
    if not isinstance(data, dict) or data.get("version") not in (1, 2) or not isinstance(data.get("samples"), list):
        raise HistoryFormatError(f"知らない形式です(version={data.get('version') if isinstance(data, dict) else None})")
    samples = []
    for s in data["samples"]:
        if not isinstance(s, dict) or not isinstance(s.get("t"), (int, float)):
            continue
        values = s.get("u") if data["version"] == 2 else s
        if not isinstance(values, dict):
            continue
        fh, sd = values.get("fh"), values.get("sd")
        samples.append(LimitSample(
            ts=datetime.fromtimestamp(s["t"] / 1000, timezone.utc),
            five_hour=float(fh) if isinstance(fh, (int, float)) and not isinstance(fh, bool) else None,
            seven_day=float(sd) if isinstance(sd, (int, float)) and not isinstance(sd, bool) else None,
        ))
    return sorted(samples, key=lambda s: s.ts)


def read_plan_usage_history(path: Path) -> list[LimitSample]:
    if not path.exists():
        return []
    return parse_plan_usage_history(path.read_text(encoding="utf-8"))


def encode_plan_usage_history(samples: Sequence[LimitSample]) -> str:
    """使用率の記録を plan-usage-history.json の version 1 の形にする(記録のコピー用。parse_plan_usage_history で読める)。"""
    rows = []
    for s in samples:
        row: dict[str, float] = {"t": round(s.ts.timestamp() * 1000)}
        if s.five_hour is not None:
            row["fh"] = s.five_hour
        if s.seven_day is not None:
            row["sd"] = s.seven_day
        rows.append(row)
    return json.dumps({"version": 1, "samples": rows}, separators=(",", ":")) + "\n"


def merge_limit_samples(*groups: Sequence[LimitSample]) -> list[LimitSample]:
    """複数の使用率の記録を 1 つにする(時刻順)。同じ時刻の記録は、後に渡したもの(新しい取得元)を使う。"""
    by_ts: dict[datetime, LimitSample] = {}
    for group in groups:
        for s in group:
            by_ts[s.ts] = s
    return sorted(by_ts.values(), key=lambda s: s.ts)


# /usage の製品別の内訳のキー → 表示名(画面の値 [observed.points] の products と同じ名前にそろえる)。知らないキーはそのまま。
# other は、円グラフでプロジェクトをまとめる「その他」と区別できる名前にする
PRODUCT_NAMES = {"claude_code": CODE_PRODUCT, "chat": "チャット", "cowork": "Cowork", "other": "その他の製品"}


def _pct(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class UsageLog:
    """Claude Code から取得した /usage の値の記録(時刻順)。

    samples: 全モデルの週次%・5 時間枠の%(デスクトップアプリの記録と同じ形)。
    observations: 全モデルの週次%・モデル別の週間上限の%・製品別の内訳(画面の値と同じ形)。取れた回ごとに 1 つ。
    failures: 取れなかった回の時刻。
    """
    samples: list[LimitSample]
    observations: list[Observation]
    failures: list[datetime] = field(default_factory=list)


def parse_usage_log(text: str) -> UsageLog:
    """Claude Code から取得した /usage の値の記録(1 行 1 回の JSON)を読む。

    読めない行は読み飛ばす(書き込みの途中で止まった行など)。製品別の内訳は、Claude Code の割合がなければ使わない
    (Claude Code 以外の割合を出せないため)。取れなかった回の行({"t", "error"})は failures に入れる。
    """
    samples, observations, failures = [], [], []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(rec, dict) or ts.tzinfo is None:
            continue
        if "error" in rec:
            failures.append(ts)
            continue
        all_pct, five_hour = _pct(rec.get("all")), _pct(rec.get("five_hour"))
        models = rec.get("models") if isinstance(rec.get("models"), dict) else {}
        products = rec.get("products") if isinstance(rec.get("products"), dict) else {}
        named = {PRODUCT_NAMES.get(str(k), str(k)): v for k, v in ((k, _pct(v)) for k, v in products.items())
                 if v is not None}
        if all_pct is not None or five_hour is not None:
            samples.append(LimitSample(ts, five_hour, all_pct))
        observations.append(Observation(
            ts=ts,
            all_pct=all_pct,
            models={str(k): v for k, v in ((k, _pct(v)) for k, v in models.items()) if v is not None},
            products=named if CODE_PRODUCT in named else {},
        ))
    return UsageLog(sorted(samples, key=lambda s: s.ts), sorted(observations, key=lambda o: o.ts), sorted(failures))


def read_usage_log(path: Path) -> UsageLog:
    try:
        return parse_usage_log(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UsageLog([], [])


@dataclass(frozen=True)
class SessionLinks:
    """セッション同士の関係の材料。種類ごとに、値(ブランチ名・PR・ファイルのパス)→ その値を持つセッション。"""
    branches: dict[str, set[str]] = field(default_factory=dict)
    prs: dict[str, set[str]] = field(default_factory=dict)
    files: dict[str, set[str]] = field(default_factory=dict)


LINK_MARKS = ('"pr-link"', '"file-history-delta"')
# ブランチ名は多くの行にある。全行を JSON として読むと遅いので、項目だけを取り出す(本文の中の同じ文字列はエスケープされて一致しない)
BRANCH_RE = re.compile(r'"gitBranch":"([^"\\]+)"')
SESSION_RE = re.compile(r'"sessionId":"([^"\\]+)"')


def parse_link_line(line: str, file_session: str) -> tuple[str, str, str] | None:
    """会話ログの 1 行から、関係の材料 (種類, 値, セッション ID) を取り出す。ほかの行や読めない行は None。

    ブランチ名・PR・編集したファイルのパスの項目だけを読み、本文は読まない。ファイルの履歴の行にはセッション ID が
    ないので、ログのファイル名のセッション(file_session)を使う。
    """
    if not any(mark in line for mark in LINK_MARKS):
        branch, session = BRANCH_RE.search(line), SESSION_RE.search(line)
        return ("branch", branch[1], session[1]) if branch and session else None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    kind, session = rec.get("type"), rec.get("sessionId")
    if kind == "pr-link" and session and rec.get("prNumber") is not None:
        return "pr", f"{rec.get('prRepository')}#{rec.get('prNumber')}", session
    if kind == "file-history-delta" and isinstance(rec.get("trackingPath"), str):
        return "file", rec["trackingPath"], file_session
    return None


def read_session_links(root: Path) -> SessionLinks:
    """会話ログから関係の材料を読む。サブエージェントのログは親のセッションの一部なので読まない。"""
    links = SessionLinks()
    groups = {"branch": links.branches, "pr": links.prs, "file": links.files}
    for path in root.rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                found = parse_link_line(line, path.stem)
                if found is not None:
                    groups[found[0]].setdefault(found[1], set()).add(found[2])
    return links


@dataclass(frozen=True)
class DesktopSession:
    """デスクトップアプリのセッション情報のうち、ID の項目だけ。"""
    local_id: str
    cli_id: str                  # 会話ログのセッション ID
    prior_cli_ids: tuple[str, ...]  # 同じ作業の前のセッション(セッションを作り直したとき)
    parent_local_id: str | None  # 分岐・派生の元のセッション


def parse_desktop_session(text: str) -> DesktopSession | None:
    """セッション情報のファイルから ID の項目だけを取り出す。読めなければ None。ほかの項目(タイトルなど)は読まない。"""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cliSessionId"), str):
        return None
    prior = data.get("priorCliSessionIds")
    spawned = data.get("spawnedFrom")
    parent = data.get("forkedFromSessionId") or (spawned.get("sessionId") if isinstance(spawned, dict) else None)
    return DesktopSession(str(data.get("sessionId") or ""), data["cliSessionId"],
                          tuple(p for p in prior if isinstance(p, str)) if isinstance(prior, list) else (),
                          parent if isinstance(parent, str) else None)


def read_desktop_sessions(root: Path) -> list[DesktopSession]:
    found = []
    for path in sorted(root.rglob("*.json")):
        try:
            session = parse_desktop_session(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if session is not None:
            found.append(session)
    return found


def continued_pairs(sessions: Sequence[DesktopSession]) -> list[tuple[str, str]]:
    """デスクトップアプリのセッション情報から、続き・分岐の組 (元のセッション, 後のセッション) を作る(会話ログの ID で)。"""
    cli_of = {s.local_id: s.cli_id for s in sessions}
    pairs = [(prior, s.cli_id) for s in sessions for prior in s.prior_cli_ids]
    pairs += [(cli_of[s.parent_local_id], s.cli_id) for s in sessions if s.parent_local_id in cli_of]
    return pairs


TRAY_OPEN_KEY = "planUsageLastTrayOpenAt"  # デスクトップアプリの設定で、使用量メニューを最後に開いた時刻(ミリ秒)


def parse_last_tray_open(text: str) -> datetime | None:
    """デスクトップアプリの設定(config.json)から、使用量メニューを最後に開いた時刻だけを取り出す。なければ None。

    この設定にはログインの情報も入っているので、ほかの値は取り出さない(表示も保存もしない)。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    ms = data.get(TRAY_OPEN_KEY) if isinstance(data, dict) else None
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def read_last_tray_open(path: Path) -> datetime | None:
    try:
        return parse_last_tray_open(path.read_text(encoding="utf-8"))
    except OSError:
        return None
