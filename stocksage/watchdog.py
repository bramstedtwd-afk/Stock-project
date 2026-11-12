"""Is the always-on computer still doing its job?

Only your own computer can see your Robinhood accounts, so only it can write
"StockSage Today" to Drive. If that file stops changing, the computer is off,
asleep, signed out, or broken, and nothing on that computer can say so. This
runs somewhere else (the scheduled cloud job: no broker access, no secrets
beyond the Drive token and the alert topic) and tells your phone.

Weekends do not count: nothing publishes on them, so a Friday afternoon sheet
is not stale on Monday morning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import notify

WATCHED_FILE = "StockSage Today.txt"
STALE_AFTER_BUSINESS_HOURS = 30.0


def business_hours_between(then: datetime, now: datetime) -> float:
    """Hours between two moments, not counting Saturdays and Sundays."""
    if now <= then:
        return 0.0
    total, cursor = 0.0, then
    while cursor < now:
        midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(midnight, now)
        if cursor.weekday() < 5:
            total += (end - cursor).total_seconds() / 3600.0
        cursor = end
    return total


def verdict(modified: datetime | None, now: datetime | None = None) -> str | None:
    """A plain message if the sheet has gone stale, else None."""
    now = now or datetime.now(timezone.utc)
    if modified is None:
        return ("StockSage has never saved your sheet to Drive. Check that the always-on "
                "computer is on and that publishing is set up.")
    age = business_hours_between(modified, now)
    if age <= STALE_AFTER_BUSINESS_HOURS:
        return None
    last = modified.strftime("%a %d %b")
    return (f"StockSage has not updated your sheet since {last}. Check that the always-on "
            "computer is on, awake and signed in.")


TRADER_QUIET_MINUTES = 150


def trader_verdict(runs: list[dict], now: datetime | None = None) -> str | None:
    """A plain message if the hourly trader has stopped checking in during market hours.

    Only judged on weekdays between 11:00 and 16:30 New York time, when at least
    two hourly runs should have happened. A market holiday can trip it; the
    message says so."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5 or not (11 * 60 <= minutes <= 16 * 60 + 30):
        return None
    if not runs:
        return None        # check-ins not set up yet: nothing to judge, and no false alarms
    quiet = (now - runs[-1]["at"]).total_seconds() / 60
    if quiet <= TRADER_QUIET_MINUTES:
        return None
    last = runs[-1]["at"].astimezone(ZoneInfo("America/New_York")).strftime("%a %I:%M %p ET")
    return (f"The trader has not checked in since {last}. Open the 'Agentic trader (hourly)' routine "
            "at claude.ai/code/routines and look at its last run. (Ignore this on a market holiday.)")


WATCHER_QUIET_MINUTES = 150


def watcher_verdict(tags_text: str, now: datetime | None = None, pulse=None) -> str | None:
    """A plain message if the minute-by-minute watcher has stopped checking in.

    `pulse` (the time in the watcher's pulse file, written every five minutes) is the fast
    check: 20 silent minutes in the session. Without one the two-hour WATCH lines decide.

    The watcher (always-on computer) writes a WATCH line when it starts and every two
    hours from 7 AM to 8 PM New York time, and a WAKE line for each wake-up. It is the
    only stop a fractional position has between the hourly runs, and nothing else
    notices when its window is closed. Judged on weekdays from 9:45 to 4:30; silent
    when it has never written a line (not set up is not an alarm)."""
    from zoneinfo import ZoneInfo

    from .journal import _ts, watcher_silent

    now = now or datetime.now(timezone.utc)
    dark = watcher_silent(pulse, now)
    if dark is not None:
        when = dark.astimezone(ZoneInfo("America/New_York")).strftime("%a %I:%M %p ET")
        return (f"The watcher has not checked in since {when}: the always-on computer may be off, asleep or "
                "offline. Until it is back, stops are checked only on the hourly runs. On that computer, open "
                "the StockSage folder in PowerShell and run .\\start.bat watch-trader install")
    local = now.astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5 or not (9 * 60 + 45 <= minutes <= 16 * 60 + 30):
        return None
    seen = []
    for line in (tags_text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 2 and parts[0] in ("WATCH", "WAKE"):
            at = _ts(parts[1])
            if at:
                seen.append(at)
    if not seen:
        return None
    last = max(seen)
    if (now - last).total_seconds() / 60 <= WATCHER_QUIET_MINUTES:
        return None
    when = last.astimezone(ZoneInfo("America/New_York")).strftime("%a %I:%M %p ET")
    return (f"The watcher has not checked in since {when}. Between the hourly runs nothing is watching stops. "
            "On the always-on computer, open the StockSage folder in PowerShell and run "
            ".\\start.bat watch-trader install")


def _send(post, title: str, message: str) -> None:
    topic = notify.topic_from_env()
    if not topic:
        print("(No alert topic set, so nothing was sent to a phone.)")
        return
    try:
        post(notify.NTFY_URL.format(topic=topic), message.encode("utf-8"),
             {"Title": title, "Priority": "high", "Tags": "warning"})
    except Exception as exc:
        print(f"(Could not send the alert: {exc})")


def _alert(post, title: str, message: str, now: datetime | None, once_a_day) -> None:
    """One push, at most one a day when `once_a_day` (a file path) is given."""
    from pathlib import Path

    print(message)
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    if once_a_day is not None:
        try:
            if Path(once_a_day).read_text(encoding="utf-8").strip() == today:
                return
        except OSError:
            pass
    _send(post, title, message)
    if once_a_day is not None:
        try:
            Path(once_a_day).parent.mkdir(parents=True, exist_ok=True)
            Path(once_a_day).write_text(today, encoding="utf-8")
        except OSError:
            pass


def check_trader(post=notify._post, now: datetime | None = None, tags_text: str | None = None,
                 once_a_day=None, pulse=None) -> None:
    """Warn the phone if the trader, or the watcher that wakes it, has gone quiet. Never raises.

    `once_a_day` (a file path) limits each alert to one push per day: the always-on
    computer checks at every publish, and one alert is enough. The watcher's alert
    keeps its own day file beside it."""
    from pathlib import Path

    from . import journal

    try:
        if tags_text is None:
            from . import drive_api

            tags_text = drive_api.read_drive_text(journal.TAGS_NAME) or ""
            if pulse is None:
                try:
                    pulse = (drive_api.read_drive_text(journal.PULSE_NAME) or "").strip() or None
                except Exception:
                    pulse = None           # the two-hour WATCH lines still decide
    except Exception as exc:
        print(f"Could not read the trader's check-ins: {exc}")
        return
    message = trader_verdict(journal.parse_runs(tags_text), now)
    if message is None:
        print("The trader is checking in.")
    else:
        _alert(post, "StockSage trader has gone quiet", message, now, once_a_day)
    try:
        message = watcher_verdict(tags_text, now, pulse)
    except Exception as exc:
        print(f"Could not check the watcher: {exc}")
        return
    if message is not None:
        beside = Path(once_a_day).with_name("watcher_alert_day.txt") if once_a_day is not None else None
        _alert(post, "StockSage watcher has gone quiet", message, now, beside)


def run(post=notify._post, now: datetime | None = None, modified=None, sheet: bool = True,
        trader: bool = False, tags_text: str | None = None) -> int:
    """Alert if stale. 0 = fine or alerted, 1 = could not check."""
    from . import drive_api

    if trader:
        check_trader(post, now, tags_text)
    if not sheet:
        return 0
    try:
        found = modified if modified is not None else drive_api.modified_time(WATCHED_FILE)
    except Exception as exc:
        print(f"Could not check Drive: {exc}")
        return 1
    message = verdict(found, now)
    if message is None:
        print("The sheet is fresh.")
        return 0
    print(message)
    _send(post, "StockSage has gone quiet", message)
    return 0
