"""Trade memory: what Claude's own trades in the agentic account actually did.

The trading routine is the live mind; this is its memory. Every filled order in
the agentic account (read from Robinhood, the ground truth) is paired into round
trips, first in first out, and each trip is scored: dollars, percent, how long
it was held, and for trades held past a day, the return against holding SPY
over the same closes. Trips are grouped by play (SAFE / AGGRESSIVE), by holding
style (same day / 1-3 days / longer), by setup and by exit reason, and every
group gets an honest verdict that refuses to judge on a handful of trades.

Play and setup come from the routine's own tags: one line per order in the
Drive file "StockSage Trade Tags.txt", which the desktop creates (the desktop's
Drive access only reaches files it created) and the routine appends to:

    TAG | 2026-10-29T14:35Z | BUY | XYZ | AGGRESSIVE | breakout | why, in a few words

An untagged fill is still remembered; it is just grouped as "untagged".

The result is published to Drive as "StockSage Trade Memory.json", written only
by the broker-capable computer, for the routine to read at the start of every
run. It trains nothing: it is a record, not a signal.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

TAGS_NAME = "StockSage Trade Tags.txt"
MEMORY_NAME = "StockSage Trade Memory.json"
TAGS_HEADER = (
    "# One line per order the trading routine places in the agentic account. Append, never edit.\n"
    "# TAG | <UTC time, e.g. 2026-10-29T14:35Z> | BUY or SELL | TICKER | SAFE or AGGRESSIVE | setup or exit "
    "reason (one word) | why, in a few words\n"
)
PLAYS = ("SAFE", "AGGRESSIVE")
MATCH_WINDOW = timedelta(hours=3)
MIN_TO_JUDGE = 10
ET = "America/New_York"


def _ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _et_date(ts: datetime):
    from zoneinfo import ZoneInfo

    return ts.astimezone(ZoneInfo(ET)).date()


# --- tags -----------------------------------------------------------------------------


def parse_tags(text: str) -> list[dict]:
    """Tag lines from the routine's file. Anything malformed is skipped, not guessed."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 6 or parts[0].upper() != "TAG":
            continue
        at, side, ticker, play = _ts(parts[1]), parts[2].lower(), parts[3].upper(), parts[4].upper()
        if at is None or side not in ("buy", "sell") or not re.fullmatch(r"[A-Z.\-]{1,10}", ticker):
            continue
        out.append({"at": at, "side": side, "ticker": ticker,
                    "play": play if play in PLAYS else "untagged",
                    "setup": re.sub(r"[^a-z0-9\-]+", "-", parts[5].lower()).strip("-") or "unspecified",
                    "note": " | ".join(parts[6:])[:200]})
    return out


def attach_tags(fills: list[dict], tags: list[dict]) -> list[dict]:
    """Give each fill the nearest unused tag for the same ticker and side within 3 hours."""
    free = sorted(tags, key=lambda t: t["at"])
    out = []
    for f in sorted(fills, key=lambda f: (_ts(f["executed_at"]) or datetime.min.replace(tzinfo=timezone.utc))):
        at = _ts(f["executed_at"])
        best = None
        if at is not None:
            near = [t for t in free if t["ticker"] == f["ticker"].upper() and t["side"] == f["side"]
                    and abs(t["at"] - at) <= MATCH_WINDOW]
            best = min(near, key=lambda t: abs(t["at"] - at), default=None)
        if best is not None:
            free.remove(best)
        out.append({**f, "tag": best})
    return out


# --- round trips ----------------------------------------------------------------------


@dataclass
class Trip:
    ticker: str
    dollars_in: float
    pnl: float
    ret: float
    opened: str
    closed: str
    hours: float
    style: str            # same day / 1-3 days / 4+ days
    play: str
    setup: str
    exit: str
    vs_spy: float | None  # return minus SPY's over the same closes; None for same-day trades


def _style(opened: datetime, closed: datetime) -> str:
    days = (_et_date(closed) - _et_date(opened)).days
    return "same day" if days == 0 else "1-3 days" if days <= 3 else "4+ days"


def _spy_return(spy, opened: datetime, closed: datetime) -> float | None:
    """SPY close-to-close over the trade's dates; None for a same-day trade or missing data."""
    if spy is None or len(spy) == 0 or _et_date(opened) == _et_date(closed):
        return None
    import pandas as pd

    a, b = pd.Timestamp(_et_date(opened)), pd.Timestamp(_et_date(closed))
    before_a, before_b = spy.loc[:a], spy.loc[:b]
    if before_a.empty or before_b.empty or before_b.index[-1] <= before_a.index[-1]:
        return None
    return float(before_b.iloc[-1] / before_a.iloc[-1] - 1)


def round_trips(fills: list[dict], spy=None, now: datetime | None = None) -> tuple[list[Trip], list[dict]]:
    """Closed trips and still-open lots from tagged fills (first in, first out)."""
    now = now or datetime.now(timezone.utc)
    lots: dict[str, list[dict]] = {}
    trips: list[Trip] = []
    for f in sorted(fills, key=lambda f: f["executed_at"]):
        at, qty, price = _ts(f["executed_at"]), float(f["quantity"] or 0), float(f["price"] or 0)
        ticker, tag = f["ticker"].upper(), f.get("tag") or {}
        if at is None or qty <= 0 or price <= 0:
            continue
        if f["side"] == "buy":
            lots.setdefault(ticker, []).append({"qty": qty, "price": price, "at": at,
                                                "play": tag.get("play", "untagged"),
                                                "setup": tag.get("setup", "untagged"), "note": tag.get("note", "")})
            continue
        if f["side"] != "sell":
            continue
        remaining, queue = qty, lots.get(ticker, [])
        while remaining > 1e-9 and queue:
            lot = queue[0]
            take = min(remaining, lot["qty"])
            ret = price / lot["price"] - 1
            spy_ret = _spy_return(spy, lot["at"], at)
            trips.append(Trip(ticker=ticker, dollars_in=round(take * lot["price"], 2),
                              pnl=round(take * (price - lot["price"]), 2), ret=round(ret, 4),
                              opened=lot["at"].isoformat(timespec="minutes"), closed=at.isoformat(timespec="minutes"),
                              hours=round((at - lot["at"]).total_seconds() / 3600, 1), style=_style(lot["at"], at),
                              play=lot["play"], setup=lot["setup"], exit=tag.get("setup", "untagged"),
                              vs_spy=None if spy_ret is None else round(ret - spy_ret, 4)))
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                queue.pop(0)
    open_lots = [{"ticker": t, "dollars_in": round(lot["qty"] * lot["price"], 2), "entry": lot["price"],
                  "opened": lot["at"].isoformat(timespec="minutes"),
                  "hours_held": round((now - lot["at"]).total_seconds() / 3600, 1),
                  "play": lot["play"], "setup": lot["setup"], "why": lot["note"]}
                 for t, queue in lots.items() for lot in queue if lot["qty"] > 1e-9]
    return trips, open_lots


# --- verdicts -------------------------------------------------------------------------


def verdict(rets: list[float]) -> str:
    """Plain words, and no judgement on fewer than MIN_TO_JUDGE trades."""
    n = len(rets)
    if n < MIN_TO_JUDGE:
        return f"too few to judge ({n} of {MIN_TO_JUDGE} trades)"
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    if t >= 2:
        return "working: keep doing it"
    if t <= -2:
        return "losing money: do less of this"
    return "no clear edge yet"


def stats(trips: list[Trip]) -> dict:
    if not trips:
        return {"trades": 0}
    rets = [t.ret for t in trips]
    excess = [t.vs_spy for t in trips if t.vs_spy is not None]
    return {"trades": len(trips), "wins": sum(t.pnl > 0 for t in trips),
            "win_rate": round(sum(t.pnl > 0 for t in trips) / len(trips), 3),
            "pnl": round(sum(t.pnl for t in trips), 2), "avg_return": round(sum(rets) / len(rets), 4),
            "avg_vs_spy": round(sum(excess) / len(excess), 4) if excess else None,
            "avg_hours": round(sum(t.hours for t in trips) / len(trips), 1),
            "best": round(max(rets), 4), "worst": round(min(rets), 4), "verdict": verdict(rets)}


def _grouped(trips: list[Trip], key) -> dict:
    groups: dict[str, list[Trip]] = {}
    for t in trips:
        groups.setdefault(key(t), []).append(t)
    return {k: stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def lessons(summary: dict) -> list[str]:
    """A few plain sentences the routine can act on. Only groups with enough trades speak."""
    out = []
    total = summary["overall"]
    if total["trades"] == 0:
        return ["No closed trades yet: nothing to learn from. Tag every order so the memory can sort them."]
    out.append(f"{total['trades']} closed trades: {total['wins']} won, {_pct(total['avg_return'])} average, "
               f"${total['pnl']:+.2f} in all. {total['verdict'].capitalize()}.")
    for name in ("by_play", "by_style", "by_setup", "by_exit"):
        judged = {k: v for k, v in summary[name].items() if v["trades"] >= MIN_TO_JUDGE}
        if len(judged) < 2:
            continue
        best = max(judged.items(), key=lambda kv: kv[1]["avg_return"])
        worst = min(judged.items(), key=lambda kv: kv[1]["avg_return"])
        if best[0] != worst[0]:
            label = name.replace("by_", "")
            out.append(f"By {label}: '{best[0]}' does best ({_pct(best[1]['avg_return'])} a trade, "
                       f"{best[1]['verdict']}); '{worst[0]}' does worst ({_pct(worst[1]['avg_return'])}, "
                       f"{worst[1]['verdict']}).")
    untagged = summary["by_play"].get("untagged", {}).get("trades", 0)
    if untagged:
        out.append(f"{untagged} trades have no tag, so they cannot be sorted by play or setup. Tag every order.")
    if total["trades"] < MIN_TO_JUDGE:
        out.append("Small sample: treat every number here as a hint, not proof.")
    return out


def summarize(fills: list[dict], tags: list[dict], spy=None, now: datetime | None = None,
              account: str = "") -> dict:
    now = now or datetime.now(timezone.utc)
    trips, open_lots = round_trips(attach_tags(fills, tags), spy=spy, now=now)
    today = _et_date(now)
    todays = [f for f in fills if _ts(f["executed_at"]) and _et_date(_ts(f["executed_at"])) == today]
    summary = {
        "as_of": now.isoformat(timespec="seconds"),
        "account": f"••••{account[-4:]}" if account else "",
        "overall": stats(trips),
        "by_play": _grouped(trips, lambda t: t.play),
        "by_style": _grouped(trips, lambda t: t.style),
        "by_setup": _grouped(trips, lambda t: t.setup),
        "by_exit": _grouped(trips, lambda t: t.exit),
        "by_ticker": dict(list(_grouped(trips, lambda t: t.ticker).items())[:10]),
        "today": {"orders": len(todays), "buys": sum(f["side"] == "buy" for f in todays)},
        "open": open_lots,
        "recent": [asdict(t) for t in trips[-12:][::-1]],
    }
    summary["lessons"] = lessons(summary)
    return summary


def render_text(summary: dict) -> str:
    lines = ["TRADE MEMORY (Claude's trades in the agentic account)", "=" * 60, *summary["lessons"], ""]
    for name in ("by_play", "by_style", "by_setup", "by_exit"):
        if not summary[name]:
            continue
        lines.append(name.replace("by_", "BY ").upper())
        for k, s in summary[name].items():
            lines.append(f"  {k:<16}{s['trades']:>4} trades  win {s['win_rate'] * 100:>3.0f}%  "
                         f"avg {_pct(s['avg_return']):>7}  ${s['pnl']:>+8.2f}  {s['verdict']}")
    if summary["open"]:
        lines.append("OPEN")
        for o in summary["open"]:
            lines.append(f"  {o['ticker']:<6} ${o['dollars_in']:.2f} at {o['entry']:.2f}, {o['hours_held']:.0f}h, "
                         f"{o['play']} {o['setup']}")
    if summary["recent"]:
        lines.append("RECENT")
        for t in summary["recent"]:
            lines.append(f"  {t['closed'][:10]} {t['ticker']:<6} {_pct(t['ret']):>7} ${t['pnl']:+.2f} "
                         f"{t['hours']:.0f}h {t['play']} {t['setup']} -> {t['exit']}")
    return "\n".join(lines)


# --- the scheduled step ---------------------------------------------------------------


def refresh(engine, client, upload: bool = True, account: str | None = None,
            tags_text: str | None = None) -> dict | None:
    """Read the agentic account's fills, score them, save locally and publish to Drive.

    Returns the summary, or None when there is no agentic account to read.
    `tags_text` is injectable for tests; otherwise the tags file is read from
    Drive (and created there, empty, the first time).
    """
    import os

    from .today import state_dir

    account = (account if account is not None else os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    if not account:
        return None
    fills = client.order_history(account_number=account)
    if fills is None:
        return None
    if tags_text is None and upload:
        try:
            from .drive_api import read_or_create_text

            tags_text = read_or_create_text(TAGS_NAME, TAGS_HEADER)
        except Exception:
            tags_text = ""
    spy = None
    try:
        hist = engine.market.history("SPY")
        spy = None if hist is None else hist["Close"]
    except Exception:
        spy = None
    summary = summarize(fills, parse_tags(tags_text or ""), spy=spy, account=account)
    text = json.dumps(summary, indent=1)
    try:
        path = state_dir() / "trade_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    if upload:
        try:
            from .drive_api import publish_text

            publish_text(MEMORY_NAME, text)
        except Exception:
            pass
    return summary
