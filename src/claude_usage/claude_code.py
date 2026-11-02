"""Claude Code の /usage と同じ値を Claude Code に問い合わせて取り、記録(data_dir/claude-code-usage.jsonl)に足す。

Claude Code の制御要求 get_usage を使う(/usage の画面と同じデータ。Claude Code 2.1.308 で確認、設計書 §2.13)。
モデルには問い合わせないので上限は減らない。セッションは保存せず(--no-session-persistence)、MCP サーバー
(--strict-mcp-config)とユーザー設定のプラグイン・フック(--setting-sources project)も読み込まない。
取得した値のうち、使用率(全モデル・5 時間枠・モデル別の週次)と製品別の内訳だけを残す。
取れなかった回も時刻と理由を残す(デスクトップアプリの記録と照合し、一本化してよいかを判断するため。設計書 §2.13)。
応答の解釈(usage_record・response_body)は純粋関数にし、Claude Code の起動と記録への書き込みと分ける。
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import Config

REQUEST_ID = "claude-usage-get-usage"
ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "project")


class UsageRequestError(Exception):
    pass


def version_key(path: str) -> tuple[int, ...]:
    """パスに含まれる最後の版の番号(…/claude-code/2.1.308/… なら (2, 1, 308))。なければ ()。"""
    found = re.findall(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])", path)
    return tuple(int(x) for x in found[-1]) if found else ()


def find_executable(patterns: Sequence[str]) -> Path | None:
    """patterns(~ と * を使える)に当たる実行ファイルのうち、版の番号が最も大きいもの(同じなら先の patterns のもの)。

    版の番号はリンク先のパスから読む(~/.local/bin/claude は …/versions/<版> へのリンク)。
    """
    found = [Path(p) for pattern in patterns for p in sorted(glob.glob(os.path.expanduser(pattern)))
             if os.path.isfile(p) and os.access(p, os.X_OK)]
    return max(found, key=lambda p: version_key(os.path.realpath(p)), default=None)


def response_body(stdout: str, request_id: str = REQUEST_ID) -> Mapping[str, Any]:
    """Claude Code の出力(1 行 1 つの JSON)から、制御要求への応答の中身を取り出す。なければ UsageRequestError。"""
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = msg.get("response") if isinstance(msg, dict) and msg.get("type") == "control_response" else None
        if not isinstance(resp, dict) or resp.get("request_id") != request_id:
            continue
        if resp.get("subtype") != "success" or not isinstance(resp.get("response"), dict):
            raise UsageRequestError(f"Claude Code がエラーを返しました: {resp.get('error') or resp.get('subtype')}")
        return resp["response"]
    raise UsageRequestError("Claude Code の出力に応答がありません")


def request_usage(executable: Path, cwd: Path, timeout: float) -> Mapping[str, Any]:
    """Claude Code を起動して get_usage を送り、応答の中身を返す。失敗したら UsageRequestError。"""
    request = {"type": "control_request", "request_id": REQUEST_ID,
               "request": {"subtype": "get_usage", "skip_behaviors": True}}  # ローカルの会話ログの分析は要らない
    try:
        proc = subprocess.run([str(executable), *ARGS], input=json.dumps(request) + "\n", capture_output=True,
                              text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise UsageRequestError(f"{timeout:g} 秒以内に応答がありませんでした") from None
    except OSError as exc:
        raise UsageRequestError(f"Claude Code を起動できません: {exc}") from None
    try:
        return response_body(proc.stdout)
    except UsageRequestError as exc:
        err = proc.stderr.strip().splitlines()
        raise UsageRequestError(f"{exc}(終了コード {proc.returncode}{'、' + err[-1][:200] if err else ''})") from None


def _pct(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_record(body: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """get_usage の応答から、記録に残す値を取り出す。使用率がなければ None(API キーでの利用など)。

    all: 全モデルの週次% / five_hour: 5 時間枠の% / models: モデルの表示名 → 週間上限の % /
    products: 製品別の内訳(キー → 割合 %。claude_code・chat・cowork・other)。
    """
    limits = body.get("rate_limits")
    if not isinstance(limits, Mapping):
        return None

    def window(name: str) -> float | None:
        w = limits.get(name)
        return _pct(w.get("utilization")) if isinstance(w, Mapping) else None

    models = {m["display_name"]: _pct(m.get("utilization")) for m in limits.get("model_scoped") or []
              if isinstance(m, Mapping) and isinstance(m.get("display_name"), str)}
    breakdown = limits.get("seven_day_breakdown")
    rows = breakdown.get("rows") if isinstance(breakdown, Mapping) else None
    products = {r["key"]: _pct(r.get("percent")) for r in rows or []
                if isinstance(r, Mapping) and isinstance(r.get("key"), str)}
    record = {"t": now.isoformat(), "all": window("seven_day"), "five_hour": window("five_hour"),
              "models": {k: v for k, v in models.items() if v is not None},
              "products": {k: v for k, v in products.items() if v is not None}}
    if record["all"] is None and not record["models"]:
        return None
    return record


Request = Callable[[Path, Path, float], Mapping[str, Any]]


def record_usage(config: Config, now: datetime, request: Request = request_usage) -> str | None:
    """Claude Code から /usage の値を取り、記録に 1 行足す。

    取れなかったときは、その回を記録に {"t": 時刻, "error": 理由} の 1 行で残し、ログに残す文を返す(通知はしない)。
    """
    options = config.claude_code
    if not options.enabled:
        return None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    executable = find_executable(options.executables)
    if executable is None:
        problem = "Claude Code が見つからないため、/usage の値を取得できませんでした"
    else:
        try:
            record = usage_record(request(executable, config.data_dir, options.timeout), now)
            problem = None if record is not None else "Claude Code の /usage に使用率がありませんでした"
        except UsageRequestError as exc:
            problem = f"Claude Code の /usage の値を取得できませんでした: {exc}"
    if problem is not None:
        record = {"t": now.isoformat(), "error": problem}
    with config.usage_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return problem
