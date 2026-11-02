import tempfile
import unittest
from pathlib import Path, PurePosixPath

from claude_usage.projects import UNKNOWN, ProjectResolver, fs_probe, resolve_project

PATTERNS = [r"/\.claude/worktrees/[^/]+", r"-worktrees/[^/]+$"]
REPOS = {
    "/Users/me/GitHub/alpha": "dir",
    "/Users/me/GitHub/civitas": "dir",
    "/Users/me/GitHub/beta-plan": "gitdir: /Users/me/GitHub/beta/.git/worktrees/beta-plan",
}


def probe(path: PurePosixPath):
    return REPOS.get(str(path))


def resolve(cwd, aliases=None):
    return resolve_project(cwd, aliases or {}, PATTERNS, probe)


class ResolveProjectTest(unittest.TestCase):
    def test_repo_root_and_subdirectory(self):
        self.assertEqual(resolve("/Users/me/GitHub/alpha"), "alpha")
        self.assertEqual(resolve("/Users/me/GitHub/civitas/unity/Packages/core/Runtime"), "civitas")

    def test_worktree_patterns_for_deleted_worktrees(self):
        self.assertEqual(resolve("/Users/me/GitHub/alpha/.claude/worktrees/feat+x"), "alpha")
        self.assertEqual(resolve("/Users/me/GitHub/alpha-worktrees/fix-roster"), "alpha")

    def test_linked_worktree_resolves_to_main_repo(self):
        self.assertEqual(resolve("/Users/me/GitHub/beta-plan/src"), "beta")

    def test_alias_wins(self):
        self.assertEqual(resolve("/Users/me/Library", {"^/Users/me/Library": "(その他)"}), "(その他)")

    def test_no_repo_falls_back_to_last_part(self):
        self.assertEqual(resolve("/Users/me/work/notes"), "notes")

    def test_resolver_caches_and_handles_empty(self):
        calls = []

        def counting(path):
            calls.append(path)
            return probe(path)

        r = ProjectResolver({}, PATTERNS, counting)
        self.assertEqual(r("/Users/me/GitHub/alpha"), "alpha")
        self.assertEqual(r("/Users/me/GitHub/alpha"), "alpha")
        self.assertEqual(len(calls), 1)
        self.assertEqual(r(""), UNKNOWN)


class FsProbeTest(unittest.TestCase):
    def test_dir_file_and_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / ".git").mkdir(parents=True)
            (root / "wt").mkdir()
            (root / "wt" / ".git").write_text("gitdir: /x/main/.git/worktrees/wt\n")
            (root / "plain").mkdir()
            self.assertEqual(fs_probe(PurePosixPath(root / "repo")), "dir")
            self.assertEqual(fs_probe(PurePosixPath(root / "wt")), "/x/main/.git/worktrees/wt")
            self.assertIsNone(fs_probe(PurePosixPath(root / "plain")))
            self.assertEqual(resolve_project(str(root / "wt" / "src"), {}, [], fs_probe), "main")


if __name__ == "__main__":
    unittest.main()
