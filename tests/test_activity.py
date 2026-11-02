import itertools
import json
import math
import re
import tempfile
import tomllib
import unittest
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_usage.activity import (BRANCH, CONTINUED, FILE, GAP, NEBULA_SHAPES, PR, SESSION_EDGE, Edge, Traits,
                                   activity_due, activity_payload, binned, composition, day_rows, describe_mix,
                                   heat_values, make_activity, nebula_radius, nebula_shape, newest_log, percentile,
                                   place_projects, place_sessions, project_roots, scan_folder, session_edges,
                                   split_sessions, usage_traits, version_file)
from claude_usage.config import EXAMPLE_CONFIG_PATH, ActivityOptions, Price, Weights, parse_config
from claude_usage.render.activity import embed_json, render_activity
from claude_usage.sources import SessionLinks, UsageEvent

TZ = ZoneInfo("Asia/Tokyo")
START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)   # 10/20 火 19:00 JST
STEP = timedelta(minutes=15)
OPTIONS = ActivityOptions(min_responses=3, bin=STEP, half_life=timedelta(minutes=30), heat_percentile=99, fade_level=0.05,
                          default_branches=frozenset({"main"}), max_file_sessions=2, refresh=timedelta(minutes=60),
                          mature_usd=300, dormant=timedelta(days=7), dead=timedelta(days=30),
                          categories={"game": "spiral", "app": "ring", "web": "emission", "data": "bipolar",
                                      "tool": "pillars", "media": "reflection"},
                          project_kinds={"beta": "data"})
W = Weights(prices={"claude": Price(1e6, 1.25e6, 2e6, 1e5, 5e6)})   # 入力 1 トークン 1 ドル


def event(minutes, session="s1", project="/r/alpha", tokens=1):
    return UsageEvent(START + timedelta(minutes=minutes), session, project, "claude-opus-5", False, tokens, 0, 0, 0, 0)


def responses(session, count, project="/r/alpha", at=0):
    return [event(at + i, session, project) for i in range(count)]


class SplitSessionsTest(unittest.TestCase):
    def test_small_runs_are_set_aside(self):
        major, minor = split_sessions([*responses("big", 3), *responses("small", 2)], 3)
        self.assertEqual((sorted(major), sorted(minor)), (["big"], ["small"]))


class SessionEdgesTest(unittest.TestCase):
    PROJECT = {"a": "p1", "b": "p1", "c": "p1", "d": "p2"}

    def edges(self, links=None, continued=()):
        return session_edges(self.PROJECT, self.PROJECT, links or SessionLinks(), continued, OPTIONS)

    def test_strongest_kind_wins_for_a_pair(self):
        links = SessionLinks(branches={"feat": {"a", "b"}}, prs={"r#1": {"a", "b"}}, files={"x.py": {"a", "b"}})
        self.assertEqual(self.edges(links), [Edge("a", "b", PR, 1)])
        self.assertEqual(self.edges(links, [("b", "a")]), [Edge("a", "b", CONTINUED, 1)])

    def test_default_branch_is_not_a_relation(self):
        self.assertEqual(self.edges(SessionLinks(branches={"main": {"a", "b"}})), [])

    def test_same_name_in_another_project_is_not_a_relation(self):
        links = SessionLinks(branches={"feat": {"a", "d"}}, files={"README.md": {"a", "d"}})
        self.assertEqual(self.edges(links), [])
        self.assertEqual(self.edges(SessionLinks(prs={"r#1": {"a", "d"}})), [Edge("a", "d", PR, 1)])  # PR はリポジトリまで同じ

    def test_file_touched_by_many_sessions_is_not_a_relation(self):
        self.assertEqual(self.edges(SessionLinks(files={"CLAUDE.md": {"a", "b", "c"}})), [])
        self.assertEqual(self.edges(SessionLinks(files={"a.py": {"a", "b"}, "b.py": {"a", "b"}})), [Edge("a", "b", FILE, 2)])

    def test_sessions_outside_the_major_set_are_ignored(self):
        self.assertEqual(self.edges(SessionLinks(prs={"r#1": {"a", "tiny"}}), [("a", "tiny"), ("a", "a")]), [])
        self.assertEqual(self.edges(SessionLinks(branches={"feat": {"a", "b", "c"}})),
                         [Edge("a", "b", BRANCH, 1), Edge("a", "c", BRANCH, 1), Edge("b", "c", BRANCH, 1)])


class ProjectLayoutTest(unittest.TestCase):
    RADII = {"big": 280.0, "mid": 120.0, "small": 60.0, "tiny": 40.0, "other": 90.0}

    def assert_apart(self, placed, radii):
        for a, b in itertools.combinations(radii, 2):
            self.assertGreaterEqual(math.dist(placed[a], placed[b]), radii[a] + radii[b] + GAP - 1, (a, b))

    def test_nebulae_never_touch(self):
        self.assert_apart(place_projects(self.RADII, {}), self.RADII)

    def test_same_records_give_the_same_layout(self):
        self.assertEqual(place_projects(self.RADII, {}), place_projects(self.RADII, {}))

    def test_existing_nebulae_stay_when_new_ones_are_added(self):
        first = place_projects(self.RADII, {})
        radii = {**self.RADII, "new": 70.0}
        second = place_projects(radii, first)
        self.assertEqual({k: second[k] for k in first}, first)   # 既存の星雲は動かない
        self.assert_apart(second, radii)

    def test_grown_nebula_pushes_its_neighbours_away(self):
        first = place_projects(self.RADII, {})
        radii = {**self.RADII, "mid": 330.0}                     # これからの使用で大きくなった
        second = place_projects(radii, first)
        self.assert_apart(second, radii)
        far = max(self.RADII, key=lambda n: math.dist(first[n], first["mid"]))
        self.assertLess(math.dist(second[far], first[far]), 400)  # 押し広げるのは必要な分だけ

    def test_radius_keeps_growing_with_use(self):
        self.assertLess(nebula_radius(0), nebula_radius(100))
        self.assertLess(nebula_radius(10000), nebula_radius(100000))  # 上限がない


class ProjectLookTest(unittest.TestCase):
    """星雲の姿は種類を 1 つに決めず、連続した特徴を混ぜて作る(2026-11-03 ユーザーの指定「全く同じものはない感じで」)。"""
    QUIET = Traits(parallel=0, idle=0, burst=0, warmth=0, intensity=0, sessions=0)

    def test_composition_is_a_continuous_mix(self):
        mix = composition({"package.json"}, Counter({".ts": 40, ".py": 10, ".png": 3}))
        self.assertAlmostEqual(sum(mix.values()), 1)
        self.assertGreater(mix["web"], mix["tool"])
        self.assertGreater(mix["tool"], mix["media"])
        self.assertGreater(mix["media"], 0)                       # 少ない分も 0 にしない
        self.assertEqual(composition(set(), Counter()), {})      # フォルダがない

    def test_markers_count_more_than_extensions(self):
        mix = composition({"ProjectSettings", "*.unity"}, Counter({".py": 20}))
        self.assertGreater(mix["game"], mix["tool"])

    def test_override_pulls_towards_the_chosen_kind(self):
        mix = composition({"package.json"}, Counter({".ts": 40}), override="data")
        self.assertGreater(mix["data"], 0.5)
        self.assertGreater(mix["web"], 0)                         # もとの比率も残る

    def test_shape_follows_the_mix_and_usage(self):
        shapes = {"game": "spiral", "app": "ring", "web": "emission", "data": "bipolar", "tool": "pillars",
                  "media": "reflection"}
        web = dict(zip(NEBULA_SHAPES, nebula_shape({"web": 0.7, "tool": 0.3}, self.QUIET, shapes)))
        self.assertAlmostEqual(sum(web.values()), 1, places=2)
        self.assertGreater(web["emission"], web["pillars"])
        busy = dict(zip(NEBULA_SHAPES, nebula_shape({"web": 1.0}, replace(self.QUIET, parallel=1.0, idle=1.0), shapes)))
        self.assertGreater(busy["remnant"], 0)                    # 並行の作業 → 細い筋
        self.assertGreater(busy["dark"], 0)                       # 休みが多い → 影
        unknown = dict(zip(NEBULA_SHAPES, nebula_shape({}, self.QUIET, shapes)))
        self.assertEqual((unknown["emission"], unknown["reflection"]), (0.5, 0.5))

    def test_small_parts_are_dropped(self):
        shape = nebula_shape({"web": 0.6, "tool": 0.25, "data": 0.1, "media": 0.05}, self.QUIET,
                             {"web": "emission", "tool": "pillars", "data": "bipolar", "media": "reflection"})
        self.assertLessEqual(sum(1 for v in shape if v), 3)

    def test_usage_traits(self):
        events = [event(0, tokens=10), event(1, tokens=10), replace(event(2, tokens=20), is_subagent=True),
                  replace(event(60 * 24 * 4, tokens=10), model="claude-fable-5-1")]
        traits = usage_traits(events, W, TZ, "claude-fable")
        self.assertAlmostEqual(traits.parallel, 0.4)
        self.assertAlmostEqual(traits.warmth, 0.2)
        self.assertAlmostEqual(traits.idle, 0.6)                  # 5 日のうち 2 日だけ使った
        self.assertTrue(all(0 <= v <= 1 for v in vars(traits).values()))

    def test_describe_mix(self):
        self.assertEqual(describe_mix({"data": 0.62, "web": 0.31, "media": 0.07}), "AI・データ 6 割・Web 3 割")
        self.assertEqual(describe_mix({}), "不明")

    def test_scan_folder_reads_names_only_and_skips_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            (root / "src").mkdir()
            (root / "src" / "main.swift").write_text("// 中身は読まない")
            (root / "node_modules" / "x").mkdir(parents=True)
            (root / "node_modules" / "x" / "package.json").write_text("{}")
            marks, exts = scan_folder(root)
        self.assertEqual(marks, {"*.xcodeproj"})       # node_modules の中の package.json は数えない
        self.assertEqual(exts, {".swift": 1})
        self.assertEqual(scan_folder(None), (set(), Counter()))

    def test_project_roots_climb_to_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "alpha"
            (repo / ".git").mkdir(parents=True)
            (repo / "sub" / "deep").mkdir(parents=True)
            roots = project_roots([event(0, project=str(repo / "sub" / "deep")), event(1, project="/gone/beta")],
                                  lambda d: "alpha" if "alpha" in d else "beta")
        self.assertEqual(roots, {"alpha": repo, "beta": None})


class SessionLayoutTest(unittest.TestCase):
    PROJECT = {f"s{i}": "p1" for i in range(8)}
    EDGES = [Edge("s0", "s1", CONTINUED, 1), Edge("s2", "s3", PR, 2)]

    def test_sessions_stay_inside_their_nebula(self):
        at = place_sessions(self.PROJECT, self.EDGES, {})
        self.assertTrue(all(math.hypot(*p) <= SESSION_EDGE + 0.001 for p in at.values()))

    def test_related_sessions_are_closer(self):
        at = place_sessions(self.PROJECT, self.EDGES, {})
        pairs = [(a, b) for a, b in itertools.combinations(sorted(at), 2)]
        related = {("s0", "s1"), ("s2", "s3")}
        others = [math.dist(at[a], at[b]) for a, b in pairs if (a, b) not in related]
        self.assertLess(max(math.dist(at[a], at[b]) for a, b in related), sorted(others)[len(others) // 2])

    def test_existing_sessions_keep_their_place(self):
        first = place_sessions(self.PROJECT, self.EDGES, {})
        second = place_sessions({**self.PROJECT, "new": "p1"}, [*self.EDGES, Edge("new", "s0", PR, 1)], first)
        self.assertEqual({k: second[k] for k in first}, first)
        self.assertLess(math.dist(second["new"], second["s0"]), 0.5)   # 新しいものは関係のある所へ


class RefreshTest(unittest.TestCase):
    NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    HOUR = timedelta(hours=1)

    def test_rebuilt_when_logs_changed_and_enough_time_passed(self):
        built = (self.NOW - 2 * self.HOUR).isoformat()
        self.assertTrue(activity_due(None, self.NOW, self.NOW, self.HOUR))                   # 初めて
        self.assertTrue(activity_due(built, self.NOW - self.HOUR, self.NOW, self.HOUR))      # 前回の後にログが更新された
        self.assertFalse(activity_due(built, self.NOW - 3 * self.HOUR, self.NOW, self.HOUR))  # 使っていなければ作らない
        recent = (self.NOW - timedelta(minutes=20)).isoformat()
        self.assertFalse(activity_due(recent, self.NOW, self.NOW, self.HOUR))                # 間隔が短ければ待つ
        self.assertFalse(activity_due(None, None, self.NOW, self.HOUR))                      # ログがない

    def test_newest_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(newest_log(root))
            (root / "a.jsonl").write_text("{}")
            self.assertIsNotNone(newest_log(root))

    def test_version_file_sits_beside_the_page(self):
        self.assertEqual(version_file(Path("/x/Activity.html")), Path("/x/Activity.version.js"))


class SeriesTest(unittest.TestCase):
    def test_binned(self):
        rows = binned([event(1), event(14), event(16)], START, STEP, W)
        self.assertEqual(rows, [[0, 2, 2.0], [1, 1, 1.0]])

    def test_heat_decays_between_responses(self):
        decay = 0.5
        self.assertEqual(heat_values([[0, 8, 0], [1, 2, 0], [4, 1, 0]], decay), [8, 6, 1.75])
        self.assertEqual(heat_values([], decay), [])

    def test_percentile(self):
        self.assertEqual(percentile(list(range(100)), 99), 99)
        self.assertEqual(percentile([5.0], 99), 5.0)
        self.assertEqual(percentile([], 99), 0.0)

    def test_days_follow_the_local_date(self):
        end = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)   # 10/22 1:00 JST
        days = day_rows(START, end, STEP, [[0, 2, 1.5], [24, 3, 2.0]], TZ)   # 10/20 19:00 と 10/21 1:00 JST
        self.assertEqual([(d["date"], d["bin"], d["n"], d["usd"]) for d in days],
                         [("2026-10-20", 0, 2, 1.5), ("2026-10-21", 20, 3, 2.0), ("2026-10-22", 116, 0, 0.0)])
        self.assertEqual(days[0]["label"], "10/20(火)")


class PayloadTest(unittest.TestCase):
    def payload(self):
        events = sorted([*responses("old", 4, "/r/alpha"), *responses("new", 5, "/r/alpha", at=60),
                         *responses("other", 3, "/r/beta", at=120), *responses("tiny", 2, "/r/beta", at=20)],
                        key=lambda e: e.ts)
        links = SessionLinks(branches={"secret-branch": {"old", "new"}}, files={"secret/path.py": {"old", "new"}})
        return activity_payload(events, links, [("old", "new")], lambda d: d.rsplit("/", 1)[-1], W, OPTIONS, TZ,
                                "Asia/Tokyo", START, timedelta(days=7), START + timedelta(hours=4))[0]

    def test_contents(self):
        p = self.payload()
        self.assertEqual([x["name"] for x in p["projects"]], ["alpha", "beta"])
        self.assertEqual(len(p["sessions"]), 3)
        self.assertEqual([s["p"] for s in p["sessions"]], [0, 0, 1])          # 始まった順
        self.assertEqual(p["sessions"][0]["label"], "10/20 火 19:00")
        self.assertEqual(p["edges"], [{"a": 1, "b": 0, "k": CONTINUED}])      # ID ではなく sessions の番号
        self.assertEqual(p["smallRuns"], 1)
        self.assertEqual(p["radius"], [34.0, 11.0])                           # 画面も同じ式で星雲の広がりを計算する
        self.assertEqual({"shape", "look", "seed", "r"} - set(p["projects"][0]), set())
        alpha, beta = p["projects"]
        self.assertEqual(alpha["mix"], "不明")                                 # フォルダがない
        self.assertEqual(beta["mix"], "AI・データ 10 割")                       # 設定で寄せた
        self.assertEqual(len(beta["shape"]), len(NEBULA_SHAPES))
        self.assertEqual(beta["shape"][NEBULA_SHAPES.index("bipolar")], 1.0)
        self.assertEqual(set(beta["look"]), {"turb", "dust", "fil", "warm", "stars", "core"})
        self.assertEqual((p["matureUsd"], p["dormantBins"], p["deadBins"]), (300, 7 * 96, 30 * 96))
        self.assertTrue(all(math.hypot(s["ux"], s["uy"]) <= SESSION_EDGE + 0.001 for s in p["sessions"]))
        self.assertEqual(p["projects"][1]["b"], [[1, 2, 2.0]])                # 小さな実行はプロジェクトにまとめる
        self.assertEqual(p["projects"][0]["b"], [])
        self.assertEqual(p["bins"], 9)
        self.assertEqual(p["weeks"], [{"label": "10/20〜10/27", "from": 0, "to": 9}])
        self.assertEqual([h["label"] for h in p["highlights"]], ["10/20(火)"])
        self.assertAlmostEqual(p["decay"], 0.5 ** 0.5)

    def test_paths_and_branches_stay_out_of_the_screen(self):
        text = json.dumps(self.payload(), ensure_ascii=False)
        for secret in ("secret-branch", "secret/path.py", "/r/alpha"):
            self.assertNotIn(secret, text)


class RenderTest(unittest.TestCase):
    def test_single_file_without_external_resources(self):
        html = render_activity({"projects": [], "days": []})
        self.assertIn('<canvas id="sky"', html)
        self.assertIn('<canvas id="timeline"', html)
        self.assertNotRegex(html, r'(src|href)="https?://')
        self.assertIn("activityPlayer", html)

    def test_layers_and_playback_defaults(self):
        html = render_activity({"projects": [], "days": []})
        order = [html.index(f'<canvas id="{name}"') for name in ("stars", "deep", "sky")]
        self.assertEqual(order, sorted(order))                       # 奥から 遠い星 → 星雲 → 星
        self.assertIn('id="skipIdle" class="text" aria-pressed="false"', html)  # 早送りは初めはオフ(2026-11-02 ユーザーの指定)
        self.assertIn("skipIdle: false", html)
        for removed in ('id="card"', "showSummary", "showCard", 'id="camera"'):   # タイトル・エンディング・カメラの切り替えは外した
            self.assertNotIn(removed, html)

    def test_waits_while_the_screen_has_no_size(self):
        # 大きさ 0 の間は描かずに待つ。大きさ 0 の層を描くと例外で描画の繰り返しが止まっていた(2026-11-03)
        html = render_activity({"projects": [], "days": []})
        frame = html[html.index("function frame(now) {"):]
        frame = frame[:frame.index("\n  }\n")]
        self.assertLess(frame.index("if (!W || !H) {"), frame.index("drawGlowLayer();"))
        self.assertIn("requestAnimationFrame(frame);\n      return;", frame)   # 待つ間も繰り返しは続ける

    def test_names_cannot_close_the_script_element(self):
        embedded = embed_json({"name": "</script><script>alert(1)</script>"})
        self.assertNotIn("<", embedded)
        self.assertEqual(json.loads(embedded)["name"], "</script><script>alert(1)</script>")

    def test_embedded_data_reads_back(self):
        html = render_activity({"name": "プロジェクト"})
        found = re.search(r'id="activity-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertEqual(json.loads(found.group(1)), {"name": "プロジェクト"})


class MakeActivityTest(unittest.TestCase):
    def test_writes_the_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with EXAMPLE_CONFIG_PATH.open("rb") as fh:
                table = tomllib.load(fh)
            table["paths"].update(transcripts_root=str(root / "projects"), data_dir=str(root / "data"),
                                  desktop_sessions=str(root / "sessions"), activity_html=str(root / "out" / "a.html"))
            config = parse_config(table)
            self.assertIsNone(make_activity(config, now=START))     # 会話ログがなければ作らない
            (root / "projects" / "p").mkdir(parents=True)
            lines = [json.dumps({"type": "assistant", "timestamp": (START + timedelta(minutes=i)).isoformat(),
                                 "sessionId": "s", "cwd": "/x/alpha", "requestId": f"r{i}",
                                 "message": {"id": f"m{i}", "model": "claude-opus-5",
                                             "usage": {"input_tokens": 1, "output_tokens": 2}}}) for i in range(12)]
            (root / "projects" / "p" / "s.jsonl").write_text("\n".join(lines) + "\n")
            path = make_activity(config, now=START + timedelta(hours=1))
            self.assertEqual(path, root / "out" / "a.html")
            self.assertIn('"name":"alpha"', path.read_text(encoding="utf-8"))
            self.assertIn("2026-10-20T11:00:00", version_file(path).read_text(encoding="utf-8"))
            first = json.loads(config.activity_layout_path.read_text(encoding="utf-8"))
            self.assertEqual(list(first["projects"]), ["alpha"])
            make_activity(config, now=START + timedelta(hours=2))        # 作り直しても配置は引き継ぐ
            self.assertEqual(json.loads(config.activity_layout_path.read_text(encoding="utf-8")), first)


if __name__ == "__main__":
    unittest.main()
