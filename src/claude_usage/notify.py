"""macOS の通知(osascript)。失敗してもレポート作成は止めない。"""
from __future__ import annotations

import json
import subprocess


def applescript_string(text: str) -> str:
    """AppleScript の文字列リテラル。改行は空白にし、" と \\ をエスケープする。"""
    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def notify(title: str, message: str) -> bool:
    script = f"display notification {applescript_string(message)} with title {applescript_string(title)}"
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0
    except OSError:
        return False
