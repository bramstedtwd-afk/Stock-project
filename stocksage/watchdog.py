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


def run(post=notify._post, now: datetime | None = None, modified=None) -> int:
    """Alert if stale. 0 = fine or alerted, 1 = could not check."""
    from . import drive_api

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
    topic = notify.topic_from_env()
    if topic:
        try:
            post(notify.NTFY_URL.format(topic=topic), message.encode("utf-8"),
                 {"Title": "StockSage has gone quiet", "Priority": "high", "Tags": "warning"})
        except Exception as exc:
            print(f"(Could not send the alert: {exc})")
    else:
        print("(No alert topic set, so nothing was sent to a phone.)")
    return 0
