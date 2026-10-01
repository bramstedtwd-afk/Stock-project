"""Tell the owner when something needs doing — rarely, and without leaking.

Opt-in. Set STOCKSAGE_NTFY_TOPIC in .env, install the free ntfy app on the
phone and subscribe to that topic.

The owner's rule for interruptions: URGENT NOW, EVERYTHING ELSE ONCE A DAY.

  * Urgent means a stop has been breached. That is a risk that gets worse
    with time, so it buzzes as soon as a scheduled run sees it — once per
    change, not once per run (publishing runs five times a day).
  * Everything else (trims, exits on the clock, entries worth a look) is
    held back and sent as ONE digest, the first time a run happens after the
    digest hour (STOCKSAGE_DIGEST_HOUR, local time, default 9) on a day it
    has not been sent. A day with nothing to report sends nothing.
  * At most one push per run. If a stop breach and the digest fall due
    together they are one message.

Privacy: ntfy topics are not secret by design — anyone who learns the name can
read it. So messages carry a verb, a ticker and the account's ROLE (Agentic,
Personal, Roth IRA) — never an amount, a balance, or an account number.

Only actions that warrant a human are alertable: risk exits, a trim, and
entries the model rates at least medium confidence. Watch-list lines, ideas
and low-confidence buys belong on the dashboard, not on a lock screen.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from datetime import datetime

from .actions import ROBINHOOD_STOCK_URL, URGENT, Plan

NTFY_URL = "https://ntfy.sh/{topic}"
ALERT_KINDS = ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "TRIM", "ENTER")
URGENT_SIGNATURE_KEY = "last_urgent_signature"
DIGEST_DATE_KEY = "last_digest_date"
DEFAULT_DIGEST_HOUR = 9


def topic_from_env() -> str | None:
    topic = (os.environ.get("STOCKSAGE_NTFY_TOPIC") or "").strip()
    return topic or None


def digest_hour() -> int:
    raw = (os.environ.get("STOCKSAGE_DIGEST_HOUR") or "").strip()
    try:
        hour = int(raw)
    except ValueError:
        return DEFAULT_DIGEST_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_DIGEST_HOUR


def alertable(plan: Plan) -> list:
    out = []
    for a in plan.actions:
        if a.kind not in ALERT_KINDS:
            continue
        if a.kind == "ENTER" and a.confidence not in ("medium", "high"):
            continue
        out.append(a)
    return out


_VERB = {
    "EXIT_STOP": "SELL {t} - hit its stop",
    "EXIT_TARGET": "SELL {t} - reached its target",
    "EXIT_TIME": "SELL {t} - 10 days, not working",
    "TRIM": "TRIM {t} - over the size cap",
    "ENTER": "BUY idea: {t}",
}

_ADVICE = "Nothing was ordered. Agentic: confirm in your routine; the rest you place yourself."


def message_for(items: list, roles: list[str] | None = None, digest: bool = False) -> tuple[str, str]:
    """(title, body) — verbs, tickers and the account's role, nothing else."""
    n = len(items)
    noun = "action" if n == 1 else "actions"
    title = f"StockSage today: {n} {noun}" if digest else f"StockSage URGENT: {n} {noun}"
    roles = roles or [None] * n
    lines = [
        (f"{r}: " if r else "") + _VERB[a.kind].format(t=a.ticker)
        for a, r in zip(items, roles)
    ]
    return title, "\n".join(lines) + "\n" + _ADVICE


def _sig(pairs) -> str:
    key = "|".join(sorted(f"{r}:{a.kind}:{a.ticker}" for r, a in pairs))
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


def signature(items: list) -> str:
    return _sig([("", a) for a in items])


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        pass


def alert_if_new(db, plan: Plan, topic: str | None = None, post=_post,
                 now: datetime | None = None) -> bool:
    """Single-account form of alert_accounts."""
    return alert_accounts(db, [("Agentic", plan)], topic, post, now)


def alert_accounts(db, account_plans, topic: str | None = None, post=_post,
                   now: datetime | None = None) -> bool:
    """Send at most one push for this run. True if one was sent.

    account_plans: [(role label, Plan)]. Never raises: a flaky network must not
    break a publish. If sending fails nothing is recorded as sent, so the next
    run tries again.
    """
    topic = topic or topic_from_env()
    if not topic:
        return False
    try:
        now = now or datetime.now()
        today = now.date().isoformat()
        pairs = [(role, a) for role, plan in account_plans for a in alertable(plan)]
        pairs.sort(key=lambda ra: ra[1].urgency)
        urgent = [(r, a) for r, a in pairs if a.urgency == URGENT]

        # Urgent: announce a CHANGE; clear the memory when it is all clear so a
        # recurrence is heard again.
        urgent_sig = _sig(urgent)
        last_urgent = db.get_meta(URGENT_SIGNATURE_KEY) or ""
        urgent_new = bool(urgent) and urgent_sig != last_urgent
        if not urgent and last_urgent:
            db.set_meta(URGENT_SIGNATURE_KEY, "")

        # Digest: once a day, after the chosen hour, if there is anything to say.
        digest_due = (
            bool(pairs)
            and now.hour >= digest_hour()
            and (db.get_meta(DIGEST_DATE_KEY) or "") != today
        )
        if not urgent_new and not digest_due:
            return False

        # Loud only for a breach the owner has not been told about. A digest
        # that merely repeats a known one is a summary, and summaries do not shout.
        chosen = pairs if digest_due else urgent
        title, body = message_for(
            [a for _, a in chosen], [r for r, _ in chosen],
            digest=digest_due and not urgent_new,
        )
        post(
            NTFY_URL.format(topic=topic), body.encode("utf-8"),
            {"Title": title, "Priority": "high" if urgent_new else "default",
             "Tags": "rotating_light" if urgent_new else "chart_with_upwards_trend",
             "Click": ROBINHOOD_STOCK_URL.format(t=chosen[0][1].ticker)},
        )
        if urgent_new:
            db.set_meta(URGENT_SIGNATURE_KEY, urgent_sig)
        if digest_due:
            db.set_meta(DIGEST_DATE_KEY, today)
        return True
    except Exception:
        return False
