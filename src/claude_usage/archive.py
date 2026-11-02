"""取得元から消える記録のコピーを data_dir に残す。

デスクトップアプリの使用率の記録(plan-usage-history.json)は 30 日分しか残らない。読むたびにコピー
(data_dir/plan-usage-archive.json)に足し、コピーと合わせて使う(2026-10-28 ユーザーの指定。設計書 §2.13)。
"""
from __future__ import annotations

from pathlib import Path

from .sources import (HistoryFormatError, LimitSample, encode_plan_usage_history, merge_limit_samples,
                      read_plan_usage_history)
from .state import write_atomic


def load_limit_samples(history: Path, archive: Path) -> tuple[list[LimitSample], str | None]:
    """デスクトップアプリの記録とコピーを合わせた使用率の記録(時刻順)と、読めなかったときの説明(なければ None)。

    新しい記録があればコピーに足す。同じ時刻の記録はアプリの記録を使う。
    アプリの記録が読めなければコピーだけを使う。コピーが読めなければアプリの記録だけを使い、コピーは書き換えない
    (読めないコピーを上書きして、残してきた記録を失わないように)。
    """
    errors = []
    try:
        live = read_plan_usage_history(history)
    except HistoryFormatError as exc:
        live = []
        errors.append(str(exc))
    try:
        kept = read_plan_usage_history(archive)
    except HistoryFormatError as exc:
        errors.append(f"記録のコピー {archive.name} を読めません: {exc}")
        return live, "・".join(errors)
    merged = merge_limit_samples(kept, live)
    if merged != kept:
        write_atomic(archive, encode_plan_usage_history(merged))
    return merged, "・".join(errors) or None
