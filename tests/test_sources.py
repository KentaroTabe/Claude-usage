import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_usage.config import Observation
from claude_usage.sources import (
    CostRecord,
    DesktopSession,
    HistoryFormatError,
    LimitSample,
    UsageLog,
    continued_pairs,
    cost_increments,
    encode_dir,
    encode_plan_usage_history,
    launch_dir,
    merge_limit_samples,
    parse_cost_state,
    parse_desktop_session,
    parse_last_tray_open,
    parse_link_line,
    parse_plan_usage_history,
    parse_usage_line,
    parse_usage_log,
    read_desktop_sessions,
    read_last_tray_open,
    read_plan_usage_history,
    read_session_links,
    read_transcripts,
    read_usage_events,
    read_usage_log,
)


def assistant_line(msg_id="m1", req="r1", ts="2026-10-21T01:00:00.000Z", output=10, model="claude-fable-5-1",
                   cwd="/Users/me/GitHub/alpha", sidechain=False, cache=None, **usage_extra):
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 400,
             "output_tokens": output, **usage_extra}
    if cache is not None:
        usage["cache_creation"] = cache
    return json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "s1", "cwd": cwd, "requestId": req,
                       "isSidechain": sidechain, "message": {"id": msg_id, "model": model, "usage": usage}})


class ParseUsageLineTest(unittest.TestCase):
    def test_assistant_with_usage(self):
        key, e = parse_usage_line(assistant_line(cache={"ephemeral_5m_input_tokens": 10,
                                                        "ephemeral_1h_input_tokens": 20}))
        self.assertEqual(key, ("m1", "r1"))
        self.assertEqual(e.ts, datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc))
        self.assertEqual((e.input_tokens, e.cache_write_5m, e.cache_write_1h, e.cache_read, e.output_tokens),
                         (2, 10, 20, 400, 10))
        self.assertEqual((e.model, e.project_dir, e.session_id, e.is_subagent),
                         ("claude-fable-5-1", "/Users/me/GitHub/alpha", "s1", False))

    def test_without_cache_detail_counts_all_writes_as_5m(self):
        _, e = parse_usage_line(assistant_line())
        self.assertEqual((e.cache_write_5m, e.cache_write_1h), (30, 0))

    def test_skips_other_lines(self):
        self.assertIsNone(parse_usage_line(json.dumps({"type": "user", "message": {"content": "hi"}})))
        self.assertIsNone(parse_usage_line(assistant_line(model="<synthetic>")))
        self.assertIsNone(parse_usage_line('{"type":"assistant","message":{"usage":{}}}'))  # 時刻なし

    def test_broken_json_raises(self):
        with self.assertRaises(ValueError):
            parse_usage_line('{"type":"assistant","usage": ')

    def test_subagent(self):
        _, e = parse_usage_line(assistant_line(sidechain=True))
        self.assertTrue(e.is_subagent)


class LaunchDirTest(unittest.TestCase):
    def test_encode_matches_claude_code_folders(self):
        self.assertEqual(encode_dir("/Users/kenta/GitHub/PokemonChampioons_Adviser"),
                         "-Users-kenta-GitHub-PokemonChampioons-Adviser")
        self.assertEqual(encode_dir("/Users/kenta/GitHub/oshikatsu-3dprint/.claude/worktrees/feat+ar-room-preview"),
                         "-Users-kenta-GitHub-oshikatsu-3dprint--claude-worktrees-feat-ar-room-preview")

    def test_subdirectory_resolves_to_launch_dir(self):
        self.assertEqual(launch_dir("/Users/me/GitHub/watch_only/8bar-digger", "-Users-me-GitHub-watch-only"),
                         "/Users/me/GitHub/watch_only")
        self.assertEqual(launch_dir("/Users/me/GitHub/watch_only", "-Users-me-GitHub-watch-only"),
                         "/Users/me/GitHub/watch_only")

    def test_outside_launch_dir_keeps_cwd(self):
        # worktree で始めたセッションが本体のリポジトリに移った場合など
        self.assertEqual(launch_dir("/Users/me/GitHub/alpha", "-Users-me-GitHub-alpha--claude-worktrees-x"),
                         "/Users/me/GitHub/alpha")


class ReadUsageEventsTest(unittest.TestCase):
    def test_project_dir_is_launch_dir_including_subagents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sub = root / "-Users-me-GitHub-alpha" / "sess" / "subagents"
            sub.mkdir(parents=True)
            (sub / "agent-1.jsonl").write_text(assistant_line(cwd="/Users/me/GitHub/alpha/pkg/x", sidechain=True) + "\n")
            events, _ = read_usage_events(root, datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertEqual([(e.project_dir, e.is_subagent) for e in events], [("/Users/me/GitHub/alpha", True)])

    def test_dedupe_since_and_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "p").mkdir()
            current = root / "p" / "s1.jsonl"
            current.write_text("\n".join([
                assistant_line(output=0),                       # 途中の行(0)
                assistant_line(output=50),                      # 確定値
                assistant_line(msg_id="m2", ts="2026-10-19T00:00:00Z"),  # since より前
                assistant_line(msg_id="m3", ts="2026-10-22T00:00:00Z"),
                '{"type":"assistant","message":{"usage":{"input_tokens":',  # 書きかけの使用量の行 → 数える
                "{broken",                                                    # 使用量の行でない → 読み飛ばす
            ]) + "\n")
            old = root / "p" / "old.jsonl"
            old.write_text(assistant_line(msg_id="m9", ts="2026-10-22T05:00:00Z") + "\n")
            past = time.time() - 30 * 86400
            os.utime(old, (past, past))  # 最終更新が since より前のファイルは読まない
            since = datetime(2026, 10, 20, tzinfo=timezone.utc)
            events, unparsed = read_usage_events(root, since)
        self.assertEqual([e.output_tokens for e in events], [50, 10])
        self.assertEqual(unparsed, 1)


def cost_state_line(**models):
    """cost-state 行。models はモデル ID → (入力, 出力, 読み込み, 書き込み, 費用) の累計。"""
    usage = {m: {"inputTokens": v[0], "outputTokens": v[1], "cacheReadInputTokens": v[2],
                 "cacheCreationInputTokens": v[3], "thinkingTokens": 0, "costUSD": v[4]} for m, v in models.items()}
    return json.dumps({"type": "cost-state", "sessionId": "s1", "totalCostUSD": sum(v[4] for v in models.values()),
                       "modelUsage": usage, "hasUnknownModelCost": False})


class CostStateTest(unittest.TestCase):
    """Claude Code が会話ログに残す費用の記録(cost-state 行)。単価の推定に使う。"""

    def test_parse(self):
        line = cost_state_line(**{"claude-opus-5[1m]": (1, 2, 3, 4, 0.5)})
        self.assertEqual(parse_cost_state(line), {"claude-opus-5": (1.0, 2.0, 3.0, 4.0, 0.5)})  # [1m] を除く
        self.assertIsNone(parse_cost_state(assistant_line()))
        self.assertIsNone(parse_cost_state('{"type":"cost-state",'))  # 壊れた行

    def test_increments(self):
        t = [datetime(2026, 10, 21, h, tzinfo=timezone.utc) for h in range(4)]
        states = [
            (t[0], {"a": (10, 20, 30, 40, 1.0), "b": (0, 0, 0, 0, 0.0)}),
            (t[1], {"a": (15, 30, 30, 40, 1.5), "b": (5, 5, 0, 0, 0.0)}),  # b は費用 0(単価を知らないモデル)
            (t[2], {"a": (3, 4, 5, 6, 0.2)}),                              # 累計が減った = 数え直し
        ]
        self.assertEqual(cost_increments(states), [
            CostRecord(t[0], "a", 10, 20, 30, 40, 1.0),
            CostRecord(t[1], "a", 5, 10, 0, 0, 0.5),
            CostRecord(t[2], "a", 3, 4, 5, 6, 0.2),
        ])

    def test_read_with_time_of_preceding_response(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "-r-a").mkdir()
            (root / "-r-a" / "s.jsonl").write_text("\n".join([
                assistant_line(ts="2026-10-21T01:00:00.000Z"),
                cost_state_line(**{"claude-fable-5-1": (2, 10, 400, 30, 0.0006)}),
                cost_state_line(**{"claude-fable-5-1": (4, 20, 800, 60, 0.0012)}),
            ]) + "\n")
            t = read_transcripts(root, datetime(2026, 10, 20, tzinfo=timezone.utc))
            self.assertEqual(len(t.events), 1)
            self.assertEqual([(c.ts, c.output_tokens, c.cost) for c in t.costs],
                             [(datetime(2026, 10, 21, 1, tzinfo=timezone.utc), 10, 0.0006)] * 2)
            self.assertEqual(read_transcripts(root, datetime(2026, 10, 22, tzinfo=timezone.utc)).costs, [])


class LastTrayOpenTest(unittest.TestCase):
    def test_parse(self):
        text = json.dumps({"planUsageLastTrayOpenAt": 1792799682567, "oauth:tokenCache": "x"})
        self.assertEqual(parse_last_tray_open(text), datetime(2026, 10, 23, 23, 54, 42, 567000, tzinfo=timezone.utc))
        for bad in ('{"other": 1}', '{"planUsageLastTrayOpenAt": "1792799682567"}', "{壊れた", "[]"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_last_tray_open(bad))

    def test_missing_file(self):
        self.assertIsNone(read_last_tray_open(Path("/nonexistent/config.json")))


class PlanUsageHistoryTest(unittest.TestCase):
    def test_version_2(self):
        text = json.dumps({"version": 2, "samples": [
            {"t": 1790000600000, "org": "o", "u": {"fh": 26, "sd": 48}},
            {"t": 1790000000000, "org": "o", "u": {"sd": 47}},
            {"t": "bad", "u": {}},
        ]})
        samples = parse_plan_usage_history(text)
        self.assertEqual([(s.five_hour, s.seven_day) for s in samples], [(None, 47.0), (26.0, 48.0)])
        self.assertEqual(samples[0].ts, datetime.fromtimestamp(1790000000, timezone.utc))

    def test_version_1(self):
        text = json.dumps({"version": 1, "samples": [{"t": 1790000000000, "fh": None, "sd": 5}]})
        self.assertEqual([(s.five_hour, s.seven_day) for s in parse_plan_usage_history(text)], [(None, 5.0)])

    def test_unknown_format(self):
        for text in ("{bad", json.dumps({"version": 3, "samples": []}), json.dumps([1])):
            with self.subTest(text=text):
                with self.assertRaises(HistoryFormatError):
                    parse_plan_usage_history(text)

    def test_missing_file(self):
        self.assertEqual(read_plan_usage_history(Path("/nonexistent/plan-usage-history.json")), [])

    def test_encode_round_trip(self):
        live = parse_plan_usage_history(json.dumps({"version": 2, "samples": [
            {"t": 1790000000123, "org": "o", "u": {"fh": 26.5, "sd": 48}},
            {"t": 1790000600000, "org": "o", "u": {"sd": 49}}]}))
        self.assertEqual(parse_plan_usage_history(encode_plan_usage_history(live)), live)  # ミリ秒まで同じ時刻に戻る
        self.assertEqual(parse_plan_usage_history(encode_plan_usage_history([])), [])

    def test_merge_prefers_later_source_and_sorts(self):
        t = [datetime(2026, 10, d, tzinfo=timezone.utc) for d in (1, 2, 3)]
        kept = [LimitSample(t[0], 1.0, 10.0), LimitSample(t[1], 2.0, 20.0)]
        live = [LimitSample(t[2], 3.0, 30.0), LimitSample(t[1], 9.0, 21.0)]
        self.assertEqual(merge_limit_samples(kept, live),
                         [LimitSample(t[0], 1.0, 10.0), LimitSample(t[1], 9.0, 21.0), LimitSample(t[2], 3.0, 30.0)])


class SessionLinksTest(unittest.TestCase):
    """活動の再生画面のための、セッション同士の関係の材料。本文は読まない。"""

    def test_parse_link_line(self):
        cases = [
            ('{"type":"user","sessionId":"s1","gitBranch":"feature/x","message":{"content":"text"}}',
             ("branch", "feature/x", "s1")),
            ('{"type":"pr-link","sessionId":"s1","prRepository":"me/repo","prNumber":12}', ("pr", "me/repo#12", "s1")),
            ('{"type":"file-history-delta","trackingPath":"src/a.py","backup":{}}', ("file", "src/a.py", "from-file")),
            ('{"type":"user","sessionId":"s1","gitBranch":""}', None),
            ('{"type":"assistant","sessionId":"s1"}', None),
            ('{"type":"pr-link","sessionId":"s1"}', None),
            ('{broken "pr-link"', None),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(parse_link_line(line, "from-file"), expected)

    def test_text_in_a_message_is_not_a_branch(self):
        line = json.dumps({"type": "user", "sessionId": "s1", "message": {"content": '"gitBranch":"not-a-branch"'}})
        self.assertIsNone(parse_link_line(line, "s1"))

    def test_read_session_links_skips_subagent_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p" / "s1" / "subagents").mkdir(parents=True)
            (root / "p" / "s1.jsonl").write_text('{"sessionId":"s1","gitBranch":"x"}\n'
                                                 '{"type":"file-history-delta","trackingPath":"a.py"}\n')
            (root / "p" / "s1" / "subagents" / "a.jsonl").write_text('{"sessionId":"s1","gitBranch":"sub"}\n')
            links = read_session_links(root)
        self.assertEqual(links.branches, {"x": {"s1"}})
        self.assertEqual(links.files, {"a.py": {"s1"}})
        self.assertEqual(links.prs, {})


class DesktopSessionsTest(unittest.TestCase):
    def test_parse_reads_only_the_id_fields(self):
        text = json.dumps({"sessionId": "local_1", "cliSessionId": "c1", "priorCliSessionIds": ["c0", 3],
                           "spawnedFrom": {"sessionId": "local_0", "title": "読まない"}, "title": "読まない"})
        self.assertEqual(parse_desktop_session(text), DesktopSession("local_1", "c1", ("c0",), "local_0"))
        forked = json.dumps({"sessionId": "local_2", "cliSessionId": "c2", "forkedFromSessionId": "local_1"})
        self.assertEqual(parse_desktop_session(forked), DesktopSession("local_2", "c2", (), "local_1"))
        for bad in ("{broken", "[]", json.dumps({"sessionId": "x"})):
            with self.subTest(text=bad):
                self.assertIsNone(parse_desktop_session(bad))

    def test_continued_pairs_use_transcript_ids(self):
        sessions = [DesktopSession("local_1", "c1", ("c0",), None), DesktopSession("local_2", "c2", (), "local_1"),
                    DesktopSession("local_3", "c3", (), "local_gone")]   # 元の情報が残っていなければ組にしない
        self.assertEqual(continued_pairs(sessions), [("c0", "c1"), ("c1", "c2")])

    def test_read_skips_unreadable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "good.json").write_text(json.dumps({"sessionId": "l", "cliSessionId": "c"}))
            (root / "a" / "bad.json").write_text("{broken")
            self.assertEqual([s.cli_id for s in read_desktop_sessions(root)], ["c"])
        self.assertEqual(read_desktop_sessions(Path("/nonexistent/sessions")), [])


class UsageLogTest(unittest.TestCase):
    def test_parse(self):
        lines = [
            json.dumps({"t": "2026-10-28T02:00:00+00:00", "all": 5, "five_hour": 40, "models": {"Fable": 3},
                        "products": {"claude_code": 94, "chat": 4, "other": 2, "new_product": 0}}),
            "{broken",
            json.dumps({"t": "2026-10-28T01:30:00+00:00", "all": None, "models": {"Fable": 1, "Bad": "x"},
                        "products": {"chat": 10}}),  # Claude Code の割合がない内訳は使わない
            json.dumps({"t": "2026-10-28T03:00:00", "all": 6}),  # 時差がない行は読み飛ばす
            json.dumps({"t": "2026-10-28T02:30:00+00:00", "error": "30 秒以内に応答がありませんでした"}),
        ]
        log = parse_usage_log("\n".join(lines))
        t1, t2 = (datetime(2026, 10, 28, h, m, tzinfo=timezone.utc) for h, m in ((1, 30), (2, 0)))
        self.assertEqual(log.samples, [LimitSample(t2, 40.0, 5.0)])  # 使用率のない行は使用率の記録にしない
        self.assertEqual(log.observations, [  # 時刻順
            Observation(t1, None, {"Fable": 1.0}, {}),
            Observation(t2, 5.0, {"Fable": 3.0},
                        {"Claude Code": 94.0, "チャット": 4.0, "その他の製品": 2.0, "new_product": 0.0}),
        ])
        self.assertEqual(log.observations[1].non_code_pct, 6.0)
        self.assertEqual(log.failures, [datetime(2026, 10, 28, 2, 30, tzinfo=timezone.utc)])  # 取れなかった回

    def test_missing_file(self):
        self.assertEqual(read_usage_log(Path("/nonexistent/claude-code-usage.jsonl")), UsageLog([], []))


if __name__ == "__main__":
    unittest.main()
