"""launchd の LaunchAgent(~/Library/LaunchAgents/<label>.plist)で cu tick を定期実行する設定。

設定の中身を作る部分(build_plist)は純粋関数。書き込みと launchctl の実行は install / uninstall にまとめる。
既定は内容を表示するだけで、--apply / --remove を付けたときだけ変更する(永続的な設定のため)。
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()


def plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def build_plist(label: str, python: str, cu_script: Path, interval_seconds: int, log_path: Path) -> bytes:
    """cu tick を interval_seconds ごとと、ログイン時(読み込み時)に 1 回動かす。負荷を抑えるため低優先度で動かす。"""
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [python, str(cu_script), "tick"],
        "StartInterval": interval_seconds,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(label: str, content: bytes, log_path: Path) -> Path:
    """plist を置き、読み込み直す(すでに読み込まれていれば一度外してから)。"""
    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}")  # 読み込まれていなければ失敗するが、それでよい
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        raise OSError(f"launchctl bootstrap に失敗しました: {result.stderr.strip()}")
    return path


def uninstall(label: str) -> Path | None:
    path = plist_path(label)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    if path.exists():
        path.unlink()
        return path
    return None
