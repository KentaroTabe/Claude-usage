"""作成済みのレポートとプロジェクトの色枠を覚えておく小さな JSON(data_dir/state.json)。

データベースは使わない。週次レポートは毎回、取得元(会話ログ・デスクトップアプリの記録)から直接作る。
ここに残すのは、取得元から消えても困るものだけ:
  - weeks: 週の終わり(UTC の ISO 文字列)→ {"final_pct": その週の最後の週次使用率, "pdf": 保存先}
    (前週比の計算と、二重に作らないための記録)
  - slots: プロジェクト名 → 色枠(週をまたいで同じ色にするため)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def week_key(end: datetime) -> str:
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("weeks", {})
    data.setdefault("slots", {})
    return data


def write_atomic(path: Path, text: str) -> None:
    """一時ファイルに書いてから置き換える(書き込みの途中で止まっても、元のファイルが壊れないように)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_state(path: Path, state: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
