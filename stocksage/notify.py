"""Tell the owner when something needs doing — once, and without leaking.

Opt-in. Set STOCKSAGE_NTFY_TOPIC in .env, install the free ntfy app on the
phone and subscribe to that topic, and a new action produces a push.

Two design constraints, both about not being a nuisance or a risk:

  * One alert per CHANGE, not per run. Publishing runs five times a day; the
    same stop breach must not buzz the phone five times. A signature of the
    current alertable actions is remembered, an alert fires only when it
    changes, and it resets when the list clears so a recurrence is heard.
  * ntfy topics are not secret by design — anyone who knows the name can
    read it. So the message carries only a verb and a ticker. No dollar
    amounts, no balances, no account identifiers, ever.

Only actions that warrant interrupting someone qualify: risk exits, a trim,
and entries the model rates at least medium confidence. A watch-list line or
a low-confidence idea is information for the dashboard, not a buzz.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request

from .actions import ROBINHOOD_STOCK_URL, Plan

NTFY_URL = "https://ntfy.sh/{topic}"
ALERT_KINDS = ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "TRIM", "ENTER")
SIGNATURE_KEY = "last_alert_signature"


def topic_from_env() -> str | None:
    topic = (os.environ.get("STOCKSAGE_NTFY_TOPIC") or "").strip()
    return topic or None


def alertable(plan: Plan) -> list:
    return [a for a in _alertable_actions(plan)]


def _alertable_actions(plan: Plan):
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


def message_for(items: list, roles: list[str] | None = None) -> tuple[str, str]:
    """(title, body) — verbs, tickers and which account, nothing else.

    `roles` names the account for each item ("Agentic", "Roth IRA"...): a role,
    never a number, so it is no more identifying than a ticker is.
    """
    title = "StockSage: 1 action" if len(items) == 1 else f"StockSage: {len(items)} actions"
    roles = roles or [None] * len(items)
    lines = [
        (f"{r}: " if r else "") + _VERB[a.kind].format(t=a.ticker)
        for a, r in zip(items, roles)
    ]
    return title, "\n".join(lines) + "\nNothing was ordered. Agentic: confirm in your routine; the rest you place yourself."


def signature(items: list) -> str:
    key = "|".join(sorted(f"{a.kind}:{a.ticker}" for a in items))
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        pass


def alert_if_new(db, plan: Plan, topic: str | None = None, post=_post) -> bool:
    """Single-account form of alert_accounts."""
    return alert_accounts(db, [("Agentic", plan)], topic, post)


def alert_accounts(db, account_plans, topic: str | None = None, post=_post) -> bool:
    """Send one push if the set of alertable actions, across ALL accounts, changed.

    account_plans: [(role label, Plan)]. One combined message, not one per
    account — a single decision-point a day is the point. The tap target is the
    Robinhood page for the most urgent ticker, so acting is one tap away.

    Never raises: a flaky network must not break a publish. If sending
    fails the signature is NOT recorded, so the next run tries again.
    """
    topic = topic or topic_from_env()
    if not topic:
        return False
    try:
        pairs = [(role, a) for role, plan in account_plans for a in alertable(plan)]
        pairs.sort(key=lambda ra: ra[1].urgency)
        items = [a for _, a in pairs]
        roles = [r for r, _ in pairs]
        sig = hashlib.sha256(
            "|".join(sorted(f"{r}:{a.kind}:{a.ticker}" for r, a in pairs)).encode()
        ).hexdigest()[:16] if pairs else ""
        if sig == (db.get_meta(SIGNATURE_KEY) or ""):
            return False
        if not items:
            db.set_meta(SIGNATURE_KEY, "")  # cleared: a recurrence should be heard
            return False
        title, body = message_for(items, roles)
        urgent = any(a.kind == "EXIT_STOP" for a in items)
        post(
            NTFY_URL.format(topic=topic), body.encode("utf-8"),
            {"Title": title, "Priority": "high" if urgent else "default",
             "Tags": "chart_with_downwards_trend" if urgent else "chart_with_upwards_trend",
             "Click": ROBINHOOD_STOCK_URL.format(t=items[0].ticker)},
        )
        db.set_meta(SIGNATURE_KEY, sig)
        return True
    except Exception:
        return False
