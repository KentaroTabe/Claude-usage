"""作業ディレクトリ(cwd)からプロジェクト名を決める。

順に: 別名(設定)→ worktree 部分を除く → .git を持つ最も近いディレクトリの名前 → cwd の末尾。
linked worktree(.git がファイル)は、その中の gitdir から本体のリポジトリ名を求める。
ファイルシステムを見る部分(probe)は引数で差し替えられるようにして、判定自体は外部環境なしでテストできるようにする。
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

UNKNOWN = "(不明)"
GITDIR_WORKTREES = "/.git/worktrees/"

# probe(ディレクトリ) → "dir"(.git がディレクトリ)/ gitdir の文字列(.git がファイル)/ None(.git がない)
Probe = Callable[[PurePosixPath], "str | None"]


def fs_probe(path: PurePosixPath) -> str | None:
    git = Path(path) / ".git"
    try:
        if git.is_dir():
            return "dir"
        if git.is_file():
            text = git.read_text(encoding="utf-8", errors="replace").strip()
            return text.removeprefix("gitdir:").strip() or None
    except OSError:
        return None
    return None


def resolve_project(cwd: str, aliases: Mapping[str, str], worktree_patterns: Sequence[str], probe: Probe) -> str:
    for pattern, name in aliases.items():
        if re.search(pattern, cwd):
            return name
    stripped = cwd
    for pattern in worktree_patterns:
        stripped = re.sub(pattern, "", stripped)
    path = PurePosixPath(stripped)
    for candidate in (path, *path.parents):
        if len(candidate.parts) <= 1:  # ルート(/)までは上がらない
            break
        found = probe(candidate)
        if found == "dir":
            return candidate.name
        if found:
            main = found.split(GITDIR_WORKTREES)[0] if GITDIR_WORKTREES in found else str(candidate)
            return PurePosixPath(main).name
    return path.name or UNKNOWN


class ProjectResolver:
    """resolve_project の結果を cwd ごとに覚えておく(同じ cwd の応答が大量にあるため)。"""

    def __init__(self, aliases: Mapping[str, str], worktree_patterns: Sequence[str], probe: Probe = fs_probe):
        self._aliases, self._patterns, self._probe = aliases, worktree_patterns, probe
        self._cache: dict[str, str] = {}

    def __call__(self, cwd: str) -> str:
        if cwd not in self._cache:
            self._cache[cwd] = resolve_project(cwd, self._aliases, self._patterns, self._probe) if cwd else UNKNOWN
        return self._cache[cwd]
