"""定期実行(launchd)の 1 回分。

0. Claude Code から /usage の値(モデル別の週間上限の % など)を取り、記録に足す(取れなくても通知はしない)。
1. 終わった週のうち未作成の週のレポート(PDF)を作り、作ったら通知する。
   会話ログが更新されていれば、活動の再生画面も作り直す(今後の使用に合わせて画面が変わるように)。
2. デスクトップアプリの使用量の記録が止まる前(使用量メニューを最後に開いてから tray_notice 経ったとき)と、
   止まったとき(24 時間の期限切れか、アプリが起動していないとき)に、使用量メニューを開くよう通知する。
   アプリは使用量メニューを 24 時間以内に開いたときだけ記録を続けるため(設計書 §2.5・§2.6)。
   Mac のスリープや通信の失敗で一時的に止まっただけなら、起きれば自動で再開するので通知しない。
通知は Mac を操作している間(最後の操作から notify_active_idle 以内)だけ出し、それ以外は次の回に持ち越す
(画面が消えている間の一時的な起動で出ても、目に入らないため)。
通知を出すかどうかの判断は純粋関数にし、同じ通知は repeat の間は繰り返さない(state.json に最後に出した時刻を残す)。
"""
from __future__ import annotations

import re
import subprocess
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, Mapping

from .activity import activity_due, make_activity, newest_log
from .claude_code import record_usage
from .config import Config, ReminderOptions
from .notify import notify
from .render.pdf import PdfError
from .sources import HistoryFormatError, read_last_tray_open, read_plan_usage_history
from .state import load_state, save_state
from .weekly import make_reports, summary_message

APP_EXECUTABLE_SUFFIX = "/Claude.app/Contents/MacOS/Claude"
TITLE = "Claude 使用量"

Notifier = Callable[[str, str], object]


def in_active_hours(now: datetime, tz: tzinfo, r: ReminderOptions) -> bool:
    return r.active_from_hour <= now.astimezone(tz).hour < r.active_until_hour


def throttled(last: str | None, now: datetime, repeat) -> bool:
    """前回(ISO 文字列)から repeat が経っていなければ True(まだ出さない)。"""
    return last is not None and now - datetime.fromisoformat(last) < repeat


def window_lapsed(last_open: datetime | None, now: datetime, r: ReminderOptions) -> bool:
    """使用量メニューを最後に開いてから tray_window を過ぎ、アプリが記録を止めているか。時刻が読めなければ過ぎたとみなす。"""
    return last_open is None or now >= last_open + r.tray_window


def reminder_due(latest: datetime | None, now: datetime, last_reminded: str | None, r: ReminderOptions,
                 tz: tzinfo, running: bool, lapsed: bool) -> bool:
    """記録が止まった通知を出すか。

    記録が stale より古く、かつ止まった原因がメニューを開けば直るもの(24 時間の期限切れ lapsed)か、アプリが起動して
    いない(running でない)ときだけ。スリープや通信の失敗で一時的に止まっただけなら、起きれば自動で再開するので出さない。
    """
    if not r.enabled or not in_active_hours(now, tz, r):
        return False
    if latest is not None and now - latest < r.stale:
        return False
    if running and not lapsed:
        return False
    return not throttled(last_reminded, now, r.repeat)


def reminder_message(latest: datetime | None, now: datetime, app_running: bool, r: ReminderOptions) -> str:
    if not app_running:
        return "Claude のデスクトップアプリが起動していないため、使用量が記録されていません。起動して、メニューバーの Claude アイコンから使用量のメニューを開いてください。"
    window = int(r.tray_window.total_seconds() // 3600)
    if latest is None:
        return "使用量の記録が見つかりません。メニューバーの Claude アイコンから使用量のメニューを開いてください。"
    hours = int((now - latest).total_seconds() // 3600)
    return (f"使用量メニューを最後に開いてから {window} 時間を過ぎたため、使用量の記録が {hours} 時間止まっています。"
            f"メニューバーの Claude アイコンから使用量のメニューを開いてください(開くと {window} 時間記録が続きます)。")


def parse_hid_idle(ioreg_output: str) -> timedelta | None:
    """ioreg の HIDIdleTime(最後のキーボード・マウスなどの操作からの時間、ナノ秒)を読む。読めなければ None。"""
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', ioreg_output)
    return timedelta(microseconds=int(m.group(1)) // 1000) if m else None


def read_idle() -> timedelta | None:
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-r", "-k", "HIDIdleTime", "-d", "1"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_hid_idle(out)


def user_active(idle: timedelta | None, threshold: timedelta) -> bool:
    """Mac を操作しているか(最後の操作から threshold 以内)。操作の時間が読めなければ、通知を止めないよう操作中とみなす。"""
    return idle is None or idle <= threshold


def _at_hour(day: date, hour: int, tz: tzinfo) -> datetime:
    """day の 0 時から hour 時間後(hour = 24 なら翌日の 0 時)。"""
    return datetime.combine(day, time(0), tz) + timedelta(hours=hour)


def tray_notice_at(last_open: datetime, r: ReminderOptions, tz: tzinfo) -> datetime:
    """使用量メニューを最後に開いた時刻 last_open から、記録が止まる前に知らせる時刻を決める。

    基本は last_open + tray_notice。その時刻が通知しない時間帯(夜)に入り、記録が止まる時刻(last_open + tray_window)が
    次に通知できる時刻(朝)より前なら、その夜に入る前の最後の 1 時間の始まりに早める(朝まで待つと記録が止まるため)。
    """
    at = last_open + r.tray_notice
    local = at.astimezone(tz)
    if r.active_from_hour <= local.hour < r.active_until_hour:
        return at
    if local.hour >= r.active_until_hour:
        quiet_start = _at_hour(local.date(), r.active_until_hour, tz)
        next_active = _at_hour(local.date() + timedelta(days=1), r.active_from_hour, tz)
    else:
        quiet_start = _at_hour(local.date() - timedelta(days=1), r.active_until_hour, tz)
        next_active = _at_hour(local.date(), r.active_from_hour, tz)
    if last_open + r.tray_window <= next_active:
        return quiet_start - timedelta(hours=1)
    return at


def tray_reminder_due(last_open: datetime | None, now: datetime, reminded_for: str | None, r: ReminderOptions,
                      tz: tzinfo) -> bool:
    """記録が止まる前の通知を出すか。使用量メニューを開くたびに 1 回だけ(reminded_for は通知した回の last_open)。

    記録がすでに止まっている(last_open + tray_window を過ぎた)ときは出さない(止まったことの通知に任せる)。
    """
    if not r.enabled or last_open is None or reminded_for == last_open.isoformat():
        return False
    if now >= last_open + r.tray_window or not in_active_hours(now, tz, r):
        return False
    return now >= tray_notice_at(last_open, r, tz)


def tray_reminder_message(last_open: datetime, now: datetime, r: ReminderOptions, tz: tzinfo) -> str:
    hours = int((now - last_open).total_seconds() // 3600)
    stop = (last_open + r.tray_window).astimezone(tz)
    return (f"使用量メニューを最後に開いてから {hours} 時間たちました。{stop.month}/{stop.day} {stop:%H:%M} に使用量の記録が"
            "止まるので、それまでにメニューバーの Claude アイコンから使用量のメニューを開いてください。")


def app_running(ps_output: str) -> bool:
    return any(line.strip().endswith(APP_EXECUTABLE_SUFFIX) for line in ps_output.splitlines())


def read_ps() -> str:
    return subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout


def run_tick(config: Config, now: datetime | None = None, notifier: Notifier = notify,
             ps: Callable[[], str] = read_ps, idle: Callable[[], timedelta | None] = read_idle,
             usage: Callable[[Config, datetime], str | None] = record_usage,
             activity: Callable[[Config, datetime], object] = make_activity) -> list[str]:
    """1 回分を実行し、ログに残す行を返す(何も起きなければ空)。

    通知は Mac を操作している間だけ出す。操作していないときは、レポートの通知は state.json の pending_notices に
    持ち越し、記録の通知は出したことにせず次の回に判断し直す(その間に記録が再開すれば出さない)。
    """
    now = now or datetime.now(timezone.utc)
    log: list[str] = []
    active = user_active(idle(), config.notify_active_idle)
    if (problem := usage(config, now)) is not None:
        log.append(problem)

    error: Exception | None = None
    try:
        created = make_reports(config, "pending", now=now)
    except (PdfError, OSError, ValueError) as exc:
        created, error = [], exc
        log.append(f"レポートの作成に失敗: {type(exc).__name__}: {exc}")
    log += [f"作成: {c.pdf}" for c in created]

    state = load_state(config.state_path)  # make_reports が書いた後に読む
    rebuilt = activity_due(state.get("activity_built_at"), newest_log(config.transcripts_root), now, config.activity.refresh)
    if rebuilt:
        try:
            activity(config, now)
            state["activity_built_at"] = now.isoformat()
        except (OSError, ValueError) as exc:   # 再生画面は付録なので、失敗しても通知はしない
            rebuilt = False
            log.append(f"活動の再生画面を作れませんでした: {type(exc).__name__}: {exc}")
    notices = [tuple(n) for n in state.get("pending_notices") or []]
    if created and config.notify:
        notices.append((f"{TITLE} 週次レポート", summary_message(created)))
    if error is not None and config.notify and active \
            and not throttled(state.get("error_notified_at"), now, config.reminder.repeat):
        notices.append((TITLE, f"週次レポートを作れませんでした: {error}"))
        state["error_notified_at"] = now.isoformat()

    try:
        samples = read_plan_usage_history(config.plan_usage_history)
    except HistoryFormatError as exc:
        samples = []
        log.append(f"デスクトップアプリの記録を読めません: {exc}")
    latest = samples[-1].ts if samples else None
    last_open = read_last_tray_open(config.app_config)
    running = app_running(ps())
    r, tz = config.reminder, config.timezone
    if reminder_due(latest, now, state.get("reminded_at"), r, tz, running, window_lapsed(last_open, now, r)):
        if active:
            message = reminder_message(latest, now, running, r)
            notices.append((TITLE, message))
            state["reminded_at"] = now.isoformat()
            log.append(f"通知: {message}")
    elif tray_reminder_due(last_open, now, state.get("tray_reminded_for"), r, tz) and active:
        message = tray_reminder_message(last_open, now, r, tz)
        notices.append((TITLE, message))
        state["tray_reminded_for"] = last_open.isoformat()
        log.append(f"通知: {message}")

    if active:
        for title, message in notices:
            notifier(title, message)
        notices = []
    if notices or state.get("pending_notices") or active or rebuilt:
        state["pending_notices"] = [list(n) for n in notices]
        save_state(config.state_path, state)
    return log


def format_log(lines: list[str], now: datetime, tz: tzinfo) -> str:
    stamp = now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    return "".join(f"{stamp} {line}\n" for line in lines)
