"""コマンドの振り分け。

  cu report                 終わった週のうち、まだ作っていない週のレポート(PDF)を作る
  cu report --all           終わった週をすべて作り直す
  cu report --week 日付     その日(YYYY-MM-DD)に終わる週だけを作る(作り直す)
  cu demo-report [--pdf]    ダミーデータで見た目を確認する
  cu tick                   定期実行の 1 回分(/usage の記録、未作成の週のレポート作成、使用量の記録が止まったときの通知)
  cu compare-sources        今週の週次% の 2 つの記録(デスクトップアプリ・/usage)を照合する(--week 日付 でその日に終わる週)
  cu activity               全期間の活動を再生する画面(HTML)を作る(--open で開く)
  cu schedule               launchd の設定内容を表示する(--apply で適用、--remove で外す)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


def _cmd_report(args: argparse.Namespace) -> int:
    from .config import load_config
    from .notify import notify
    from .render.pdf import PdfError
    from .weekly import make_reports, summary_message

    config = load_config()
    mode = "all" if args.all else "week" if args.week else "pending"
    try:
        created = make_reports(config, mode, args.week)
    except (PdfError, ValueError) as exc:
        print(f"中止: {exc}", file=sys.stderr)
        if config.notify and not args.no_notify:
            notify("Claude 使用量", f"レポートを作れませんでした: {exc}")
        return 1
    for c in created:
        print(c.pdf)
    if not created:
        print("作るレポートはありません(終わった週はすべて作成済み)。")
    elif config.notify and not args.no_notify:
        notify("Claude 使用量 週次レポート", summary_message(created))
    return 0


def _cmd_demo_report(args: argparse.Namespace) -> int:
    from .config import load_config
    from .demo import SCENARIOS, build_demo_report
    from .render.pdf import build_helper, html_to_pdf
    from .render.report import render_report

    config = load_config()
    out_dir = args.out_dir or config.demo_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    helper = build_helper(config.pdf_helper_path) if args.pdf else None
    for scenario in (SCENARIOS if args.scenario == "all" else (args.scenario,)):
        path = out_dir / f"demo-{scenario}.html"
        path.write_text(render_report(build_demo_report(scenario, seed=args.seed), config.timezone, config.report),
                        encoding="utf-8")
        print(path)
        if helper is not None:
            print(html_to_pdf(path, path.with_suffix(".pdf"), helper, config.pdf_timeout_seconds))
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .config import load_config
    from .tick import format_log, run_tick

    config = load_config()
    now = datetime.now(timezone.utc)
    sys.stdout.write(format_log(run_tick(config, now), now, config.timezone))  # launchd がログファイルへ書く
    return 0


def _cmd_compare_sources(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .config import load_config
    from .render.svg import time_label
    from .weekly import source_comparison, week_ending_on
    from .weeks import week_containing

    config = load_config()
    tz = config.timezone
    now = datetime.now(timezone.utc)
    if args.week:
        week = week_ending_on(args.week, tz, config.reset_anchor, config.cycle)
    else:
        week = week_containing(now, config.reset_anchor, config.cycle)
    print(f"{time_label(week.start, tz)} 〜 {time_label(week.end, tz)} の週")
    for line in source_comparison(config, week, now):
        print(line)
    return 0


def _cmd_activity(args: argparse.Namespace) -> int:
    import subprocess

    from .activity import make_activity
    from .config import load_config

    path = make_activity(load_config(), out=args.out)
    if path is None:
        print("会話ログに応答がないため、作りませんでした。")
        return 0
    print(path)
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    from .config import CU_SCRIPT, load_config
    from .schedule import build_plist, install, plist_path, uninstall

    config = load_config()
    label = config.schedule_label
    if args.remove:
        removed = uninstall(label)
        print(f"外しました: {removed}" if removed else f"{plist_path(label)} はありませんでした(launchd からは外しました)")
        return 0
    content = build_plist(label, args.python, CU_SCRIPT, int(config.schedule_interval.total_seconds()), config.log_path)
    if not args.apply:
        print(f"# {plist_path(label)} に置く内容(まだ書き込んでいません。--apply で適用します)")
        print(content.decode("utf-8"), end="")
        return 0
    path = install(label, content, config.log_path)
    print(f"適用しました: {path}(ログ: {config.log_path})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu", description="Claude 使用量の週次レポート")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="週次レポート(PDF)を作る")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="終わった週をすべて作り直す")
    which.add_argument("--week", type=date.fromisoformat, metavar="YYYY-MM-DD", help="その日に終わる週だけを作る")
    p.add_argument("--no-notify", action="store_true", help="通知を出さない")
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("demo-report", help="ダミーデータでレポートの見た目を確認する")
    p.add_argument("--scenario", choices=("dense", "sparse", "none", "all"), default="all")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out-dir", type=Path, help="出力先(既定: 設定の demo_dir)")
    p.add_argument("--pdf", action="store_true", help="同じ場所に PDF も作る")
    p.set_defaults(func=_cmd_demo_report)

    sub.add_parser("tick", help="定期実行の 1 回分(launchd から呼ばれる)").set_defaults(func=_cmd_tick)

    p = sub.add_parser("compare-sources", help="週次%% の 2 つの記録(デスクトップアプリ・/usage)を照合する")
    p.add_argument("--week", type=date.fromisoformat, metavar="YYYY-MM-DD", help="その日に終わる週(既定: 今週)")
    p.set_defaults(func=_cmd_compare_sources)

    p = sub.add_parser("activity", help="全期間の活動を再生する画面(HTML)を作る")
    p.add_argument("--out", type=Path, help="出力先(既定: 設定の activity_html)")
    p.add_argument("--open", action="store_true", help="作った画面をブラウザで開く")
    p.set_defaults(func=_cmd_activity)

    p = sub.add_parser("schedule", help="launchd の設定(既定は内容の表示だけ)")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="~/Library/LaunchAgents に置いて読み込む")
    action.add_argument("--remove", action="store_true", help="launchd から外して plist を消す")
    p.add_argument("--python", default=sys.executable, help="使う Python(既定: 今の実行環境)")
    p.set_defaults(func=_cmd_schedule)

    args = parser.parse_args(argv)
    return args.func(args)
