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

    TAG | 2026-10-29T14:35Z | BUY | XYZ | AGGRESSIVE | breakout | 0.5 | 41.20 | why, in a few words
    RUN | 2026-10-29T14:40Z | 103.20 | 12.10 | one-line summary of the run

The first form is the original; the second adds shares and fill price, which
lets the memory be rebuilt without a broker login (the cloud runner, while the
always-on computer is off). RUN lines are the trader's heartbeat: one per run,
with account value and cash, so a silent trader is noticed and the account's
value over time is on record. An untagged fill is still remembered; it is just
grouped as "untagged".

Two writers, one truth: the always-on computer builds the memory from
Robinhood's own fills and keeps them in the file; the cloud runner reuses
those fills and adds only fills tagged AFTER the last one Robinhood confirmed.

The result is published to Drive as "StockSage Trade Memory.json", written only
by the broker-capable computer, for the routine to read at the start of every
run. It trains nothing: it is a record, not a signal.
"""

from __future__ import annotations

import hashlib
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
    "reason (one word) | shares | fill price | why, in a few words\n"
    "# RUN | <UTC time> | account value | cash | one-line summary   (one line at the end of every run)\n"
)
TRADES_NAME = "StockSage Trades.txt"
PLAYS = ("SAFE", "AGGRESSIVE")
CONFIDENCE_TRADES = 30
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


def _num(value, allow_zero: bool = False) -> float | None:
    try:
        v = float(str(value).strip().replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if (v > 0 or (allow_zero and v == 0)) and math.isfinite(v) else None


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
        qty = price = None
        rest = parts[6:]
        if len(parts) >= 8 and _num(parts[6]) and _num(parts[7]):
            qty, price, rest = _num(parts[6]), _num(parts[7]), parts[8:]
        out.append({"at": at, "side": side, "ticker": ticker,
                    "play": play if play in PLAYS else "untagged",
                    "setup": re.sub(r"[^a-z0-9\-]+", "-", parts[5].lower()).strip("-") or "unspecified",
                    "qty": qty, "price": price, "note": " | ".join(rest)[:200]})
    return out


def parse_runs(text: str) -> list[dict]:
    """The trader's heartbeat lines, oldest first."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or parts[0].upper() != "RUN":
            continue
        at = _ts(parts[1])
        if at is None:
            continue
        out.append({"at": at, "equity": _num(parts[2], allow_zero=True),
                    "cash": _num(parts[3], allow_zero=True) if len(parts) > 3 else None,
                    "note": " | ".join(parts[4:])[:200]})
    return sorted(out, key=lambda r: r["at"])


def fills_from_tags(tags: list[dict], after: datetime | None = None) -> list[dict]:
    """Fills rebuilt from tags that carry shares and price, newer than `after`."""
    out = []
    for t in tags:
        if t.get("qty") and t.get("price") and (after is None or t["at"] > after):
            key = f"{t['at'].isoformat()}|{t['side']}|{t['ticker']}|{t['qty']}|{t['price']}"
            out.append({"order_id": "tag-" + hashlib.sha256(key.encode()).hexdigest()[:16], "ticker": t["ticker"],
                        "side": t["side"], "quantity": t["qty"], "price": t["price"],
                        "executed_at": t["at"].isoformat(), "from_tag": True})
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
    vs_spy: float | None
    buy_why: str = ""
    sell_why: str = ""  # return minus SPY's over the same closes; None for same-day trades


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
                              vs_spy=None if spy_ret is None else round(ret - spy_ret, 4),
                              buy_why=lot["note"], sell_why=tag.get("note", "")))
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


def confidence(overall: dict) -> dict:
    """Has the record earned more money? Four plain checks, all required."""
    n = overall.get("trades", 0)
    if n < CONFIDENCE_TRADES:
        return {"status": "too early", "line": f"NOT YET: {n} of {CONFIDENCE_TRADES} closed trades. "
                f"Too few to tell skill from luck; do not add money on this record yet."}
    checks = [("made money overall", overall["pnl"] > 0),
              ("better than luck (verdict 'working')", overall["verdict"].startswith("working")),
              ("not behind SPY on trades held overnight", overall["avg_vs_spy"] is None or overall["avg_vs_spy"] >= 0)]
    failed = [name for name, ok in checks if not ok]
    if failed:
        return {"status": "not earned", "line": f"NOT EARNED after {n} trades: not yet " + "; ".join(failed) + "."}
    return {"status": "earned", "line": f"EARNED: {n} trades, profitable and better than luck. The record supports "
            "adding money; add in steps and keep watching it."}


def trader_state(runs: list[dict], now: datetime) -> dict:
    if not runs:
        return {"last_run": None}
    eq = [r for r in runs if r["equity"] is not None]
    today = _et_date(now)
    return {"last_run": runs[-1]["at"].isoformat(timespec="minutes"),
            "minutes_since": round((now - runs[-1]["at"]).total_seconds() / 60),
            "runs_today": sum(_et_date(r["at"]) == today for r in runs),
            "last_note": runs[-1]["note"],
            "equity": eq[-1]["equity"] if eq else None, "cash": runs[-1]["cash"],
            "equity_first": eq[0]["equity"] if eq else None,
            "equity_first_at": eq[0]["at"].isoformat(timespec="minutes") if eq else None,
            "equity_peak": max(r["equity"] for r in eq) if eq else None}


def summarize(fills: list[dict], tags: list[dict], spy=None, now: datetime | None = None,
              account: str = "", runs: list[dict] | None = None, source: str = "robinhood",
              grades: list[str] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    trips, open_lots = round_trips(attach_tags(fills, tags), spy=spy, now=now)
    today = _et_date(now)
    todays = [f for f in fills if _ts(f["executed_at"]) and _et_date(_ts(f["executed_at"])) == today]
    summary = {
        "as_of": now.isoformat(timespec="seconds"),
        "source": source,
        "account": f"••••{account[-4:]}" if account else "",
        "overall": stats(trips),
        "by_play": _grouped(trips, lambda t: t.play),
        "by_style": _grouped(trips, lambda t: t.style),
        "by_setup": _grouped(trips, lambda t: t.setup),
        "by_exit": _grouped(trips, lambda t: t.exit),
        "by_ticker": dict(list(_grouped(trips, lambda t: t.ticker).items())[:10]),
        "today": {"orders": len(todays), "buys": sum(f["side"] == "buy" for f in todays)},
        "open": open_lots,
        "recent": [asdict(t) for t in trips[-15:][::-1]],
        "trader": trader_state(runs or [], now),
        "grades": list(grades or []),
        "fills": [{k: f.get(k) for k in ("order_id", "ticker", "side", "quantity", "price", "executed_at")}
                  for f in sorted(fills, key=lambda f: str(f["executed_at"]))],
    }
    summary["confidence"] = confidence(summary["overall"])
    summary["lessons"] = lessons(summary)
    return summary


def _et(iso: str | None, fmt: str = "%a %d %b %I:%M %p") -> str:
    from zoneinfo import ZoneInfo

    ts = _ts(iso) if iso else None
    return ts.astimezone(ZoneInfo(ET)).strftime(fmt) + " ET" if ts else "never"


def render_text(summary: dict) -> str:
    """The phone report: confidence first, then the trader's pulse, then every trade's reasons."""
    lines = [f"STOCKSAGE TRADES  ({summary.get('account') or 'agentic account'}, updated {_et(summary['as_of'])})",
             "", "CONFIDENCE", "  " + summary["confidence"]["line"], ""]
    t = summary.get("trader") or {}
    lines.append("TRADER")
    if not t.get("last_run"):
        lines.append("  No run has checked in yet.")
    else:
        lines.append(f"  Last run {_et(t['last_run'])} ({t['minutes_since']} min before this update), "
                     f"{t['runs_today']} run(s) today.")
        if t.get("equity") is not None:
            lines.append(f"  Account ${t['equity']:,.2f}" + (f", cash ${t['cash']:,.2f}" if t.get("cash") is not None else "")
                         + f". First recorded ${t['equity_first']:,.2f} on {_et(t['equity_first_at'], '%d %b')}, "
                         f"peak ${t['equity_peak']:,.2f}.")
        if t.get("last_note"):
            lines.append(f"  Last run said: {t['last_note']}")
    lines += ["", "LESSONS", *("  " + x for x in summary["lessons"])]
    for name in ("by_play", "by_style", "by_setup", "by_exit"):
        if summary[name]:
            lines += ["", name.replace("by_", "GRADES BY ").upper()]
            for k, st in summary[name].items():
                lines.append(f"  {k:<12}{st['trades']:>3} trades, won {st['win_rate'] * 100:.0f}%, "
                             f"avg {_pct(st['avg_return'])}, ${st['pnl']:+.2f}: {st['verdict']}")
    lines += ["", "OPEN NOW"]
    if not summary["open"]:
        lines.append("  Nothing open.")
    for o in summary["open"]:
        lines.append(f"  {o['ticker']}: ${o['dollars_in']:.2f} at ${o['entry']:.2f}, held {o['hours_held']:.0f}h, "
                     f"{o['play']} {o['setup']}")
        if o.get("why"):
            lines.append(f"    why: {o['why']}")
    lines += ["", "RECENT TRADES (newest first)"]
    if not summary["recent"]:
        lines.append("  No closed trades yet.")
    for r in summary["recent"]:
        lines.append(f"  {_et(r['closed'], '%d %b')} {r['ticker']}: {_pct(r['ret'])} (${r['pnl']:+.2f}) in "
                     f"{r['hours']:.0f}h, {r['play']} {r['setup']}, sold on {r['exit']}")
        if r.get("buy_why"):
            lines.append(f"    bought: {r['buy_why']}")
        if r.get("sell_why"):
            lines.append(f"    sold: {r['sell_why']}")
    if summary.get("grades"):
        lines += ["", "STOCKSAGE'S OWN CALLS", *("  " + g for g in summary["grades"])]
    if summary.get("source") != "robinhood":
        lines += ["", "(Built from the trader's own notes while the always-on computer was away; "
                  "Robinhood's records replace it on the computer's next run.)"]
    return "\n".join(lines)


# --- the scheduled step ---------------------------------------------------------------


def _spy_closes(engine):
    try:
        hist = engine.market.history("SPY")
        return None if hist is None else hist["Close"]
    except Exception:
        return None


def _save_and_publish(summary: dict, upload: bool) -> None:
    from .today import state_dir

    text, report = json.dumps(summary, indent=1), render_text(summary)
    try:
        folder = state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "trade_memory.json").write_text(text, encoding="utf-8")
        (folder / "trades.txt").write_text(report, encoding="utf-8")
    except OSError:
        pass
    if upload:
        try:
            from .drive_api import publish_text

            publish_text(MEMORY_NAME, text)
            publish_text(TRADES_NAME, report)
        except Exception:
            pass


def _read_drive_tags() -> str:
    try:
        from .drive_api import read_or_create_text

        return read_or_create_text(TAGS_NAME, TAGS_HEADER)
    except Exception:
        return ""


def refresh(engine, client, upload: bool = True, account: str | None = None,
            tags_text: str | None = None, grades: list[str] | None = None) -> dict | None:
    """The always-on computer's run: Robinhood's fills are the truth.

    Returns the summary, or None when there is no agentic account to read.
    `tags_text` is injectable for tests; otherwise the tags file is read from
    Drive (and created there, empty, the first time).
    """
    import os

    account = (account if account is not None else os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    if not account:
        return None
    fills = client.order_history(account_number=account)
    if fills is None:
        return None
    if tags_text is None:
        tags_text = _read_drive_tags() if upload else ""
    if grades is None:
        try:
            from .scorekeeping import lines as grade_lines

            grades = grade_lines(engine.db)
        except Exception:
            grades = []
    summary = summarize(fills, parse_tags(tags_text), spy=_spy_closes(engine), account=account,
                        runs=parse_runs(tags_text), source="robinhood", grades=grades)
    _save_and_publish(summary, upload)
    return summary


def refresh_without_broker(engine, upload: bool = True, previous: dict | None = None,
                           tags_text: str | None = None) -> dict:
    """The cloud runner's run: no broker login. Reuse the fills Robinhood last
    confirmed (kept in the previous memory file) and add only tagged fills that
    came after them, so the phone report stays current while the computer is off."""
    if previous is None and upload:
        try:
            from .drive_api import read_drive_text

            raw = read_drive_text(MEMORY_NAME)
            previous = json.loads(raw) if raw else None
        except Exception:
            previous = None
    previous = previous or {}
    if tags_text is None:
        tags_text = _read_drive_tags() if upload else ""
    tags = parse_tags(tags_text)
    confirmed = [f for f in previous.get("fills") or [] if not str(f.get("order_id", "")).startswith("tag-")]
    last = max((_ts(f["executed_at"]) for f in confirmed if _ts(f["executed_at"])), default=None)
    def already_confirmed(f):              # the trader's note can be stamped a few minutes after the fill
        at = _ts(f["executed_at"])
        return any(c["ticker"] == f["ticker"] and c["side"] == f["side"] and _ts(c["executed_at"])
                   and abs(_ts(c["executed_at"]) - at) <= MATCH_WINDOW
                   and abs(float(c["quantity"]) - f["quantity"]) <= 0.02 * max(f["quantity"], 1e-9)
                   for c in confirmed)

    fills = confirmed + [f for f in fills_from_tags(tags, after=last) if not already_confirmed(f)]
    account = (previous.get("account") or "").replace("••••", "")
    summary = summarize(fills, tags, spy=_spy_closes(engine), account=account, runs=parse_runs(tags_text),
                        source="robinhood" if not fills or len(fills) == len(confirmed) else "robinhood+notes",
                        grades=previous.get("grades"))
    _save_and_publish(summary, upload)
    return summary
