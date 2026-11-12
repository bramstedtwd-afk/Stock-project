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
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

TAGS_NAME = "StockSage Trade Tags.txt"
MEMORY_NAME = "StockSage Trade Memory.json"
# The watcher's pulse: one small file it overwrites every few minutes while it watches, so
# an hourly run can tell within 20 minutes that the always-on computer has gone dark.
PULSE_NAME = "StockSage Watcher Pulse.txt"
PULSE_TOLD_NAME = "StockSage Watcher Silent.txt"     # the day the phone was last told
PULSE_SILENT = timedelta(minutes=20)
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
# Discipline (playbook "Discipline"): the habits a good trader keeps on a bad day.
DAILY_LOSS_LIMIT = -0.04   # account this far down on the day: no new buys until tomorrow
COOL_OFF_LOSSES = 2        # this many losing trades in a row, the latest today: next buy at test size
RISK_GUIDE = {"SAFE": 0.01, "AGGRESSIVE": 0.05}   # planned risk to the stop, share of the account
STOP_RE = re.compile(r"\bstop(?:[\s\-]*(?:loss|at|under|below|@|:|=))*\s*(\d+(?:\.\d+)?)", re.I)


def _ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


_ET_ZONE = None


def _et_date(ts: datetime):
    global _ET_ZONE
    if _ET_ZONE is None:
        from zoneinfo import ZoneInfo

        _ET_ZONE = ZoneInfo(ET)
    return ts.astimezone(_ET_ZONE).date()


# --- tags -----------------------------------------------------------------------------


def _num(value, allow_zero: bool = False) -> float | None:
    try:
        v = float(str(value).strip().replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if (v > 0 or (allow_zero and v == 0)) and math.isfinite(v) else None


SETUP_WORDS = ("breakout", "pullback", "momentum", "gap", "news", "earnings", "reversal", "brain", "index", "inverse",
               "defensive", "bounce", "social",
               "trend", "fear", "meme",
               "target", "stop", "thesis", "time", "close", "rotate", "profit")


def setup_word(text: str) -> str:
    """One of the playbook's words, so trips group together: "take profit before
    the weekend" is `profit`. Unknown phrases keep their first word."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    for w in words:
        if w in SETUP_WORDS:
            return w
    for w in words:
        for known in SETUP_WORDS:
            if w.startswith(known) or known.startswith(w) and len(w) >= 4:
                return known
    return words[0] if words else "unspecified"


def stated_stop(note: str) -> float | None:
    """The stop a buy's why names ("stop 118.40", "stop under 0.95"), if any."""
    m = STOP_RE.search(note or "")
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


TARGET_RE = re.compile(r"\btarget(?:[\s\-]*(?:at|of|near|@|:|=))*\s*(\d+(?:\.\d+)?)", re.I)


def stated_target(note: str) -> float | None:
    """The target a buy's why names ("target 310"), if any."""
    m = TARGET_RE.search(note or "")
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


def parse_stops(text: str) -> list[dict]:
    """`STOP | <UTC time> | TICKER | stop price | target price (optional) | why (optional)`:
    the trader moved a stop (or set one for a position that had none). Fractional
    shares cannot carry a resting stop order at Robinhood, so this line is what the
    watcher uses to wake the trader the minute a stop is touched."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4 or parts[0].upper() != "STOP":
            continue
        at, ticker, stop = _ts(parts[1]), parts[2].upper(), _num(parts[3])
        if at is None or not stop or not re.fullmatch(r"[A-Z.\-]{1,10}", ticker):
            continue
        out.append({"at": at, "ticker": ticker, "stop": stop,
                    "target": _num(parts[4]) if len(parts) > 4 else None,
                    "note": " | ".join(parts[5:])[:200]})
    return sorted(out, key=lambda x: x["at"])


def parse_closed(text: str) -> list[dict]:
    """`CLOSED | <UTC time> | TICKER | why (optional)`: the owner closed a position left
    behind in an EARLIER account (after an account switch nothing can read that account,
    so its sales never reach the record). It only takes the position off the leftover
    list; the sale is the owner's, so it is not graded as one of the trader's trades."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or parts[0].upper() != "CLOSED":
            continue
        at, ticker = _ts(parts[1]), parts[2].upper()
        if at is None or not re.fullmatch(r"[A-Z.\-]{1,10}", ticker):
            continue
        out.append({"at": at, "ticker": ticker, "note": " | ".join(parts[3:])[:200]})
    return sorted(out, key=lambda x: x["at"])


def drop_closed(left_behind: list[dict], closed: list[dict]) -> tuple[list[dict], list[dict]]:
    """(still left, closed by the owner): a leftover lot is closed by a CLOSED line for its
    ticker written after it was bought."""
    still, gone = [], []
    for lot in left_behind:
        opened = _ts(lot.get("opened"))
        hit = next((c for c in closed if c["ticker"] == lot["ticker"] and (opened is None or c["at"] >= opened)), None)
        if hit:
            gone.append(dict(lot, closed=hit["at"].isoformat(timespec="minutes"), closed_note=hit["note"]))
        else:
            still.append(lot)
    return still, gone


def apply_stops(open_lots: list[dict], stops: list[dict]) -> list[dict]:
    """Each open lot takes the latest STOP line for its ticker written after it was bought."""
    for lot in open_lots:
        opened = _ts(lot.get("opened"))
        for st in stops:
            if st["ticker"] == lot["ticker"] and (opened is None or st["at"] >= opened - timedelta(minutes=5)):
                lot["stop"] = st["stop"]
                if st.get("target"):
                    lot["target"] = st["target"]
    return open_lots


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
        note = " | ".join(rest)[:200]
        level = re.match(r"\s*\[(HIGH|MEDIUM|LOW|NO)\]", note, re.I)
        out.append({"at": at, "side": side, "ticker": ticker,
                    "play": play if play in PLAYS else "untagged",
                    "setup": setup_word(parts[5]),
                    "confidence": level.group(1).upper() if level else "unrated",
                    "qty": qty, "price": price, "note": note,
                    "stop": stated_stop(note) if side == "buy" else None,
                    "target": stated_target(note) if side == "buy" else None})
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
        # 0 is "no reading", not a value: during an account move the trader saw an empty
        # new account and wrote 0 (2026-11-06), which would read as a 100% loss.
        out.append({"at": at, "equity": _num(parts[2]),
                    "cash": _num(parts[3], allow_zero=True) if len(parts) > 3 else None,
                    "note": " | ".join(parts[4:])[:200]})
    return sorted(out, key=lambda r: r["at"])


def is_crypto(ticker: str) -> bool:
    """Crypto is tagged as a pair (BTC-USD). Robinhood's stock order history never
    contains it, so its fills come from the trader's notes (shares and price)."""
    return str(ticker).upper().endswith("-USD")


def crypto_fills(tags: list[dict], broker: list[dict] | None = None, account_id: str = "") -> list[dict]:
    """Crypto fills for the trade memory. The trader's TAG notes are the base. When
    the broker's crypto orders are available they correct each note's amount and
    price (same pair and side within 3 hours, amount within 5%). The broker's list
    spans EVERY crypto account under the login, so its other orders are used only
    when `account_id` (STOCKSAGE_AGENTIC_CRYPTO_ID) names the trading account's."""
    noted = fills_from_tags([t for t in tags if is_crypto(t["ticker"])])
    if not broker:
        return noted
    pool = [b for b in broker if is_crypto(b.get("ticker", ""))]
    out, used = [], set()
    for f in noted:
        at = _ts(f["executed_at"])
        match = None
        for b in pool:
            bt = _ts(b.get("executed_at"))
            if (b["order_id"] in used or bt is None or at is None or b["ticker"] != f["ticker"] or b["side"] != f["side"]
                    or abs(bt - at) > MATCH_WINDOW or abs(float(b["quantity"]) - f["quantity"]) > 0.05 * f["quantity"]):
                continue
            if match is None or abs(bt - at) < abs(_ts(match["executed_at"]) - at):
                match = b
        if match is not None:
            used.add(match["order_id"])
            out.append({k: match[k] for k in ("order_id", "ticker", "side", "quantity", "price", "executed_at")})
        else:
            out.append(f)
    if account_id:
        out += [{k: b[k] for k in ("order_id", "ticker", "side", "quantity", "price", "executed_at")}
                for b in pool if b["order_id"] not in used and b.get("crypto_account") == account_id]
    return out


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
    free: dict[tuple, list] = {}
    for t in sorted(tags, key=lambda t: t["at"]):
        free.setdefault((t["ticker"], t["side"]), []).append(t)
    out = []
    for f in sorted(fills, key=lambda f: (_ts(f["executed_at"]) or datetime.min.replace(tzinfo=timezone.utc))):
        at = _ts(f["executed_at"])
        best = None
        if at is not None:
            pool = free.get((f["ticker"].upper(), f["side"]), [])
            near = [t for t in pool if abs(t["at"] - at) <= MATCH_WINDOW]
            best = min(near, key=lambda t: abs(t["at"] - at), default=None)
            if best is not None:
                pool.remove(best)
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
    confidence: str = "unrated"   # the level the trader stated on the buy ([HIGH] / [MEDIUM] / [LOW])
    risk: float | None = None     # dollars that were at risk to the buy's stated stop
    r: float | None = None        # the result in units of that risk (+2.0 = made twice what was risked)


def _style(opened: datetime, closed: datetime) -> str:
    days = (_et_date(closed) - _et_date(opened)).days
    return "same day" if days == 0 else "1-3 days" if days <= 3 else "4+ days"


SPY_KEEP = 300               # trading days of SPY closes carried in the memory for the light path


def _spy_arrays(spy):
    """SPY's daily closes as (dates, closes) lists, sorted. Accepts a pandas Series (the
    engine), a {"YYYY-MM-DD": close} dict (carried in the memory file, so the trader's
    light path, with no pandas and no market data, still grades against the market),
    or an already-converted tuple. Pure Python, so the light path never needs numpy."""
    if spy is None:
        return None
    if isinstance(spy, tuple):
        return spy if spy[0] else None
    from datetime import date as _date

    if isinstance(spy, dict):
        items = []
        for k, v in spy.items():
            try:
                items.append((_date.fromisoformat(str(k)[:10]), float(v)))
            except (TypeError, ValueError):
                continue
    else:
        if len(spy) == 0:
            return None
        items = [(ts.date() if hasattr(ts, "date") else ts, float(v)) for ts, v in zip(spy.index, spy.tolist())]
    items = sorted(i for i in items if i[1] > 0)
    return ([d for d, _ in items], [v for _, v in items]) if items else None


def spy_for_memory(spy) -> dict:
    """The last SPY_KEEP closes as {"YYYY-MM-DD": close}, carried in the memory file."""
    arrays = _spy_arrays(spy)
    if arrays is None:
        return {}
    idx, vals = arrays
    return {d.isoformat(): round(v, 4) for d, v in zip(idx[-SPY_KEEP:], vals[-SPY_KEEP:])}


def _day(at):
    return at if not isinstance(at, datetime) else _et_date(at)


def regime_of_closes(closes) -> str:
    """The market's mood from SPY's daily closes up to yesterday: up (above its 50-day
    average and up 0.5%+ in 5 days), down (below it and down 1%+), else mixed. The
    memory grades setups per regime with it, so a setup that works only in rising
    markets is learned as exactly that."""
    if closes is None or len(closes) < 51:
        return "mixed"
    c = closes
    vs50, d5 = c[-1] / (sum(c[-50:]) / 50) - 1, c[-1] / c[-6] - 1
    down = (vs50 < 0) + (d5 < -0.01)
    up = (vs50 > 0) + (d5 > 0.005)
    return "down" if down == 2 else "up" if up == 2 else "mixed"


def regime_before(spy, at) -> str:
    """The regime as it stood before the open of `at`'s day (closes strictly earlier)."""
    from bisect import bisect_left

    arrays = _spy_arrays(spy)
    if arrays is None:
        return "mixed"
    idx, vals = arrays
    return regime_of_closes(vals[:bisect_left(idx, _day(at))])


def _close_on_or_before(arrays, at) -> tuple[int, float] | None:
    from bisect import bisect_right

    idx, vals = arrays
    i = bisect_right(idx, _day(at)) - 1
    return (i, vals[i]) if i >= 0 else None


def _spy_return(spy, opened: datetime, closed: datetime) -> float | None:
    """SPY close-to-close over the trade's dates; None for a same-day trade or missing data."""
    arrays = _spy_arrays(spy)
    if arrays is None or _et_date(opened) == _et_date(closed):
        return None
    a, b = _close_on_or_before(arrays, opened), _close_on_or_before(arrays, closed)
    if a is None or b is None or b[0] <= a[0]:
        return None
    return float(b[1] / a[1] - 1)


def round_trips(fills: list[dict], spy=None, now: datetime | None = None) -> tuple[list[Trip], list[dict]]:
    """Closed trips and still-open lots from tagged fills (first in, first out)."""
    now = now or datetime.now(timezone.utc)
    spy = _spy_arrays(spy)
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
                                                "setup": tag.get("setup", "untagged"), "note": tag.get("note", ""),
                                                "confidence": tag.get("confidence", "unrated"),
                                                "stop": tag.get("stop"), "target": tag.get("target")})
            continue
        if f["side"] != "sell":
            continue
        remaining, queue = qty, lots.get(ticker, [])
        while remaining > 1e-9 and queue:
            lot = queue[0]
            take = min(remaining, lot["qty"])
            ret = price / lot["price"] - 1
            spy_ret = _spy_return(spy, lot["at"], at)
            stop = lot.get("stop")
            risk = round(take * (lot["price"] - stop), 2) if stop and stop < lot["price"] else None
            pnl = take * (price - lot["price"])
            trips.append(Trip(ticker=ticker, dollars_in=round(take * lot["price"], 2),
                              pnl=round(take * (price - lot["price"]), 2), ret=round(ret, 4),
                              opened=lot["at"].isoformat(timespec="minutes"), closed=at.isoformat(timespec="minutes"),
                              hours=round((at - lot["at"]).total_seconds() / 3600, 1), style=_style(lot["at"], at),
                              play=lot["play"], setup=lot["setup"], exit=tag.get("setup", "untagged"),
                              vs_spy=None if spy_ret is None else round(ret - spy_ret, 4),
                              buy_why=lot["note"], sell_why=tag.get("note", ""),
                              confidence=lot.get("confidence", "unrated"), risk=risk,
                              r=round(pnl / risk, 2) if risk else None))
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                queue.pop(0)
    open_lots = [{"ticker": t, "dollars_in": round(lot["qty"] * lot["price"], 2), "entry": lot["price"],
                  "opened": lot["at"].isoformat(timespec="minutes"),
                  "hours_held": round((now - lot["at"]).total_seconds() / 3600, 1),
                  "play": lot["play"], "setup": lot["setup"], "why": lot["note"], "stop": lot.get("stop"),
                  "target": lot.get("target")}
                 for t, queue in lots.items() for lot in queue if lot["qty"] > 1e-9]
    return trips, open_lots


# --- verdicts -------------------------------------------------------------------------


# How sure before a group is called working or losing. The scenario lab (STRATEGY.md) found
# t >= 2 wrong on 11% of decided idea verdicts and t >= 2.5 on 4%, at no cost in returns.
VERDICT_T = 2.5


def tstat(rets: list[float]) -> float | None:
    """Mean over its standard error: how far from luck the average is."""
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
    return round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0


def verdict(rets: list[float]) -> str:
    """Plain words, and no judgement on fewer than MIN_TO_JUDGE trades."""
    n = len(rets)
    if n < MIN_TO_JUDGE:
        return f"too few to judge ({n} of {MIN_TO_JUDGE} trades)"
    t = tstat(rets) or 0.0
    if t >= VERDICT_T:
        return "working: keep doing it"
    if t <= -VERDICT_T:
        return "losing money: do less of this"
    return "no clear edge yet"


RAW_GRADED = {"inverse"}    # judged against cash: an inverse fund's job is to make money while SPY falls


def market_adjusted(t: Trip) -> float:
    """What a trade earned beyond simply holding SPY over the same closes. Verdicts use
    this, so a rising market cannot make a setup look skilled (nor a falling one make
    it look broken): the scenario lab caught a zero-edge setup judged "losing" in a
    bear market on raw returns. Same-day trades (hours of market exposure), crypto
    and inverse funds are judged on their own return."""
    if t.vs_spy is None or is_crypto(t.ticker) or t.setup in RAW_GRADED:
        return t.ret
    return t.vs_spy


def stats(trips: list[Trip]) -> dict:
    if not trips:
        return {"trades": 0}
    rets = [t.ret for t in trips]
    adj = [market_adjusted(t) for t in trips]
    excess = [t.vs_spy for t in trips if t.vs_spy is not None]
    rs = [t.r for t in trips if t.r is not None]
    return {"trades": len(trips), "wins": sum(t.pnl > 0 for t in trips),
            "win_rate": round(sum(t.pnl > 0 for t in trips) / len(trips), 3),
            "pnl": round(sum(t.pnl for t in trips), 2), "avg_return": round(sum(rets) / len(rets), 4),
            "avg_vs_spy": round(sum(excess) / len(excess), 4) if excess else None,
            "avg_hours": round(sum(t.hours for t in trips) / len(trips), 1),
            "best": round(max(rets), 4), "worst": round(min(rets), 4), "verdict": verdict(adj),
            "avg_adj": round(sum(adj) / len(adj), 4), "t": tstat(adj),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "r_trades": len(rs)}


def _grouped(trips: list[Trip], key) -> dict:
    groups: dict[str, list[Trip]] = {}
    for t in trips:
        groups.setdefault(key(t), []).append(t)
    return {k: stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _adj(group: dict) -> float:
    """A group's average market-adjusted return (older memories have only the raw one)."""
    v = group.get("avg_adj")
    return (group.get("avg_return") or 0.0) if v is None else v


def lessons(summary: dict) -> list[str]:
    """A few plain sentences the routine can act on. Only groups with enough trades speak."""
    out = []
    total = summary["overall"]
    if total["trades"] == 0:
        return ["No closed trades yet: nothing to learn from. Tag every order so the memory can sort them."]
    out.append(f"{total['trades']} closed trades: {total['wins']} won, {_pct(total['avg_return'])} average, "
               f"${total['pnl']:+.2f} in all. {total['verdict'].capitalize()}.")
    conf = summary.get("by_confidence") or {}
    hi, lo = conf.get("HIGH") or {}, conf.get("LOW") or {}
    if hi.get("trades", 0) >= MIN_TO_JUDGE and lo.get("trades", 0) >= MIN_TO_JUDGE:
        if _adj(hi) <= _adj(lo):
            out.append(f"HIGH-confidence trades ({_pct(_adj(hi))} vs the market) are not beating LOW ones "
                       f"({_pct(_adj(lo))}) yet: confidence is not calibrated yet. Ten of each is mostly luck, so "
                       "keep sizing by the evidence and watch this line.")
        else:
            out.append(f"Confidence is calibrated: HIGH {_pct(_adj(hi))} vs LOW {_pct(_adj(lo))} a trade, "
                       "beyond the market.")
    out += _risk_lessons(summary.get("risk") or {})
    for name in ("by_asset", "by_confidence", "by_play", "by_style", "by_setup", "by_exit"):
        judged = {k: v for k, v in summary[name].items() if v["trades"] >= MIN_TO_JUDGE}
        if len(judged) < 2:
            continue
        best = max(judged.items(), key=lambda kv: _adj(kv[1]))
        worst = min(judged.items(), key=lambda kv: _adj(kv[1]))
        if best[0] != worst[0]:
            label = name.replace("by_", "")
            out.append(f"By {label}: '{best[0]}' does best ({_pct(_adj(best[1]))} a trade vs the market, "
                       f"{best[1]['verdict']}); '{worst[0]}' does worst ({_pct(_adj(worst[1]))}, "
                       f"{worst[1]['verdict']}).")
    untagged = summary["by_play"].get("untagged", {}).get("trades", 0)
    if untagged:
        out.append(f"{untagged} trades have no tag, so they cannot be sorted by play or setup. Tag every order.")
    if total["trades"] < MIN_TO_JUDGE:
        out.append("Small sample: treat every number here as a hint, not proof.")
    return out


def _risk_lessons(rk: dict) -> list[str]:
    """What a disciplined trader checks: a stop on every buy, risk near the guide,
    losses no bigger than planned, and the result in units of risk (R)."""
    out = []
    if rk.get("without_stop"):
        out.append(f"{rk['without_stop']} of the last {rk['buys']} buys named no stop: write 'stop 118.40' in every "
                   "buy's why, so the risk can be measured.")
    if rk.get("over_guide"):
        out.append("Risked more than the guide (about 1% SAFE, up to 5% AGGRESSIVE) on " + ", ".join(rk["over_guide"])
                   + ": size so that hitting the stop costs about the guide.")
    if rk.get("stops_blown"):
        out.append(f"{len(rk['stops_blown'])} trade(s) lost more than 1.5x the planned risk ("
                   + ", ".join(sorted(set(rk["stops_blown"]))) + "): honour the stop, use a resting stop order.")
    n, avg = rk.get("r_trades", 0), rk.get("avg_r")
    if n >= MIN_TO_JUDGE and avg is not None:
        out.append(f"Average result {avg:+.2f}R over {n} trades with a stated stop: "
                   + ("winners outweigh losers. Losing trades are the cost of this plan: keep taking the setups "
                      "the evidence backs, at the size it allows." if avg > 0 else
                      "losers outweigh winners: be pickier on entries or let winners run to the target."))
    elif n >= 3 and not rk.get("stops_blown"):
        out.append(f"All {n} trades with a stated stop kept their losses within the plan: that is the discipline "
                   "working, whatever the result.")
    return out


def confidence(overall: dict, market: dict | None = None) -> dict:
    """Has the record earned more money? Plain checks, all required."""
    n = overall.get("trades", 0)
    if n < CONFIDENCE_TRADES:
        return {"status": "too early", "line": f"NOT YET: {n} of {CONFIDENCE_TRADES} closed trades. "
                f"Too few to tell skill from luck; do not add money on this record yet."}
    checks = [("made money overall", overall["pnl"] > 0),
              ("better than luck (verdict 'working')", overall["verdict"].startswith("working")),
              ("not behind SPY on trades held overnight", overall["avg_vs_spy"] is None or overall["avg_vs_spy"] >= 0),
              ("the account ahead of simply holding SPY since the first check-in",
               not market or market.get("ahead") is None or market["ahead"] >= 0)]
    failed = [name for name, ok in checks if not ok]
    if failed:
        return {"status": "not earned", "line": f"NOT EARNED after {n} trades: not yet " + "; ".join(failed) + "."}
    return {"status": "earned", "line": f"EARNED: {n} trades, profitable and better than luck. The record supports "
            "adding money; add in steps and keep watching it."}


IDEA_HORIZONS = (1, 5)          # trading days after the idea
MIN_IDEAS = 20                  # an idea group speaks only with this many graded


NOT_AN_IDEA = re.compile(r"not a real idea|\btest (check|only|line|run)\b|--help|\bdup(licate)? check\b|"
                         r"^\W*(\[\w+\]\s*)?(test|check|testing)\W*$", re.I)


def _idea_rows(text: str) -> list[dict]:
    """Every well-formed IDEA line, in file order, repeats included."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 6 or parts[0].upper() != "IDEA":
            continue
        at, side, ticker = _ts(parts[1]), parts[2].lower(), parts[3].upper()
        price = _num(parts[5])
        if at is None or side not in ("buy", "sell") or not re.fullmatch(r"[A-Z.\-]{1,10}", ticker) or not price:
            continue
        note = " | ".join(parts[6:])[:200]
        if NOT_AN_IDEA.search(note):
            continue                       # a check of the command, not a trade considered
        level = re.match(r"\s*\[(HIGH|MEDIUM|LOW|NO)\]", note, re.I)
        out.append({"at": at, "side": side, "ticker": ticker, "setup": setup_word(parts[4]), "price": price,
                    "confidence": level.group(1).upper() if level else "unrated", "note": note})
    return out


def parse_ideas(text: str) -> list[dict]:
    """`IDEA | <UTC time> | BUY or SELL | TICKER | setup | price | [LEVEL] why`: a trade the
    trader considered (taken or not). Graded later against the market, free evidence."""
    out: list[dict] = []
    for idea in _idea_rows(text):
        if any(o["ticker"] == idea["ticker"] and o["side"] == idea["side"] and o["setup"] == idea["setup"]
               and abs((idea["at"] - o["at"]).total_seconds()) <= 2 * 3600 for o in out):
            continue                       # the same idea noted twice counts once
        out.append(idea)
    return out


# --- ideas worth watching: a good idea that was not bought, waiting for its entry price ---

ENTRY_RE = re.compile(r"\bentry(?:[\s\-]*(?:price|level|at|near|above|over|below|under|@|:|=))*\s*(\d+(?:\.\d+)?)", re.I)
IDEA_WATCH_LEVELS = ("HIGH", "MEDIUM")     # a LOW or NO idea is never worth a wake-up
IDEA_WATCH_SESSIONS = 1                    # watched the day it was written and one trading day more
IDEA_WATCH_MAX = 6                         # each watched idea costs a price lookup a minute
ENTRY_MIN_GAP = 0.001                      # an entry at the price it was written at was a pass, not a wait


def stated_entry(note: str) -> float | None:
    """The entry level an idea's why names ("entry 141.50", "entry above 141.50"), if any."""
    m = ENTRY_RE.search(note or "")
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


def _sessions_after(day, n: int):
    """The date `n` weekdays after `day` (holidays are not known here: one extra day at worst)."""
    while n > 0:
        day += timedelta(days=1)
        n -= day.weekday() < 5
    return day


def idea_watch(text: str, open_tickers=(), now: datetime | None = None) -> list[dict]:
    """The unbought ideas the watcher should keep an eye on, from the notes alone (so the
    trader's light path rebuilds it after every note). For each ticker only its LATEST
    idea line counts, and it is watched only if it is a BUY rated HIGH or MEDIUM, names
    an entry level away from the price it was written at, is not held, was not bought
    since, and is at most one trading day old. Old IDEA lines (no entry level) are
    graded as always and simply never watched."""
    now = now or datetime.now(timezone.utc)
    today = _et_date(now)
    held = {str(t).upper() for t in open_tickers if t}
    bought: dict = {}
    for t in parse_tags(text):
        if t["side"] == "buy":
            bought[t["ticker"]] = max(bought.get(t["ticker"], t["at"]), t["at"])
    latest: dict[str, dict] = {}
    for idea in sorted(_idea_rows(text), key=lambda i: i["at"]):
        latest[idea["ticker"]] = idea
    out = []
    for t, idea in latest.items():
        entry = stated_entry(idea["note"])
        expires = _sessions_after(_et_date(idea["at"]), IDEA_WATCH_SESSIONS)
        if idea["side"] != "buy" or idea["confidence"] not in IDEA_WATCH_LEVELS or not entry or t in held:
            continue
        if today > expires or idea["at"] > now or (t in bought and bought[t] >= idea["at"]):
            continue
        if abs(entry / idea["price"] - 1) < ENTRY_MIN_GAP:
            continue
        stop = stated_stop(idea["note"])
        out.append({"ticker": t, "entry": entry, "direction": "above" if entry > idea["price"] else "below",
                    "price": idea["price"], "level": idea["confidence"], "setup": idea["setup"],
                    "at": idea["at"].isoformat(timespec="minutes"), "expires": expires.isoformat(),
                    "stop": stop if stop and stop < entry else None})
    return sorted(out, key=lambda i: (i["level"] != "HIGH", -_ts(i["at"]).timestamp()))[:IDEA_WATCH_MAX]


def grade_ideas(ideas: list[dict], history, spy=None, now: datetime | None = None) -> dict:
    """Each idea's move over 1 and 5 trading days from its stated price, in the idea's
    direction and (for stocks) against SPY. `history(ticker)` returns daily closes."""
    import pandas as pd

    import numpy as np

    now = now or datetime.now(timezone.utc)
    graded, cache = [], {}
    spy_idx = spy_vals = None
    spy_arr = _spy_arrays(spy)
    if spy is not None and len(spy):
        spy_idx, spy_vals = spy.index.values, spy.to_numpy(dtype=float)
    for idea in ideas:
        t = idea["ticker"]
        if t not in cache:
            try:
                got = history(t)
                cache[t] = None if got is None or len(got) == 0 else (got.index.values, got.to_numpy(dtype=float))
            except Exception:
                cache[t] = None
        if cache[t] is None:
            continue
        idx, vals = cache[t]
        day = pd.Timestamp(_et_date(idea["at"])).to_datetime64()
        first_after = int(np.searchsorted(idx, day, side="right"))      # first close strictly after the idea's day
        row = {"ticker": t, "setup": idea["setup"], "confidence": idea["confidence"], "side": idea["side"],
               "setup_regime": f"{idea['setup']}|{regime_before(spy_arr, idea['at'])}" if spy_arr else None}
        sign = 1 if idea["side"] == "buy" else -1
        for h in IDEA_HORIZONS:
            if len(vals) - first_after < h:
                break
            ret = float(vals[first_after + h - 1] / idea["price"] - 1)
            bench = None
            if spy_idx is not None and not is_crypto(t):
                cut = int(np.searchsorted(spy_idx, day, side="right"))
                if cut > 0 and len(spy_vals) - cut >= h:
                    bench = float(spy_vals[cut + h - 1] / spy_vals[cut - 1] - 1)
            row[f"d{h}"] = round(sign * (ret - (bench or 0.0)), 4)
        if "d1" in row:
            graded.append(row)

    def group(key):
        out = {}
        for k in sorted({g[key] for g in graded}):
            rows = [g for g in graded if g[key] == k and "d5" in g]
            if rows:
                vals = [r["d5"] for r in rows]
                out[k] = {"ideas": len(rows), "avg_5d": round(sum(vals) / len(vals), 4), "t": tstat(vals),
                          "right": round(sum(v > 0 for v in vals) / len(vals), 2),
                          "verdict": verdict(vals) if len(vals) >= MIN_IDEAS else
                          f"too few to judge ({len(vals)} of {MIN_IDEAS} ideas)"}
        return out

    day1 = [g["d1"] for g in graded]
    return {"graded": len(graded), "by_setup": group("setup"), "by_confidence": group("confidence"),
            "by_setup_regime": group("setup_regime") if spy is not None else {},
            "day1": {"ideas": len(day1), "avg": round(sum(day1) / len(day1), 4) if day1 else None,
                     "right": round(sum(v > 0 for v in day1) / len(day1), 2) if day1 else None},
            "recent": graded[-10:][::-1]}


def parse_deposits(text: str) -> list[dict]:
    """`DEPOSIT | <UTC time> | amount` lines: money the owner added (or withdrew, negative).
    They keep the account-vs-market comparison honest: a deposit is not a gain."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or parts[0].upper() != "DEPOSIT":
            continue
        at = _ts(parts[1])
        try:                               # a withdrawal (or less money coming over in an account switch) is negative
            amount = float(parts[2].replace("$", "").replace(",", "").strip())
        except ValueError:
            amount = None
        if at is not None and amount and math.isfinite(amount):
            out.append({"at": at, "amount": amount})
    return out


def vs_market(runs: list[dict], spy=None, deposits: list[dict] | None = None) -> dict | None:
    """The account since its first check-in against simply holding SPY over the same days.
    Deposits after the first check-in are taken out, so added money never looks like skill."""
    eq = [r for r in runs if r.get("equity") is not None]
    if len(eq) < 2:
        return None
    first, last = eq[0], eq[-1]
    added = sum(d["amount"] for d in deposits or [] if first["at"] < d["at"] <= last["at"])
    mine = (last["equity"] - added) / first["equity"] - 1 if first["equity"] else None
    out = {"since": first["at"].isoformat(timespec="minutes"), "account": None if mine is None else round(mine, 4),
           "deposits": round(added, 2), "spy": None, "ahead": None}
    arrays = _spy_arrays(spy)
    if arrays is not None:
        a, b = _close_on_or_before(arrays, first["at"]), _close_on_or_before(arrays, last["at"])
        if a is not None and b is not None:
            out["spy"] = round(float(b[1] / a[1] - 1), 4)
            if mine is not None:
                out["ahead"] = round(mine - out["spy"], 4)
    return out


def drawdown(runs: list[dict]) -> float | None:
    """How far the latest account value sits below its highest check-in (0 at a peak)."""
    eq = [r["equity"] for r in runs if r.get("equity")]
    return round(eq[-1] / max(eq) - 1, 4) if eq else None


def trading_drawdown(trips: list, equity: float | None) -> float | None:
    """How far the trades' own realized result sits below its best point, as a share of
    the account. Unlike the account's drawdown it ignores what parked cash did, so
    the brake answers "are MY trades failing?", not "did the market dip?"."""
    if not equity or equity <= 0:
        return None
    cum = peak = 0.0
    for t in sorted(trips, key=lambda t: t.closed):
        cum += t.pnl
        peak = max(peak, cum)
    return round((cum - peak) / equity, 4)


def _equity_at(runs: list[dict], at: datetime) -> float | None:
    """The account value at the last check-in before `at` (else the first one after)."""
    eq = [r for r in runs if r.get("equity")]
    before = [r for r in eq if r["at"] <= at]
    return (before[-1] if before else eq[0])["equity"] if eq else None


def _sales(trips: list) -> list[dict]:
    """One row per sale: FIFO splits one sell across lots, but it is one decision."""
    rows: dict[tuple, dict] = {}
    for t in trips:
        row = rows.setdefault((t.ticker, t.closed), {"ticker": t.ticker, "closed": t.closed, "pnl": 0.0})
        row["pnl"] += t.pnl
    return sorted(rows.values(), key=lambda r: r["closed"])


def discipline(trips: list, runs: list[dict], now: datetime, deposits: list[dict] | None = None) -> dict:
    """The bad-day habits, as of `now` (they reset each trading day):
    DAILY LIMIT  the account is 4%+ below its last check-in before today: no new buys today.
    NO REVENGE   a name sold at a loss today is not bought back today.
    COOL-OFF     2 losing sales in a row, the latest today: the next buy is test size.
                 It ends with the next winning sale or the next day: a stop doing its
                 job is the plan working, not a reason to stop trading well."""
    today = _et_date(now)
    sales = _sales(trips)
    streak = 0
    for row in reversed(sales):
        if row["pnl"] >= 0:
            break
        streak += 1
    todays = [r for r in sales if _ts(r["closed"]) and _et_date(_ts(r["closed"])) == today]
    eq = [r for r in runs if r.get("equity")]
    first = next((r for r in reversed(eq) if _et_date(r["at"]) < today), None)
    last = next((r for r in reversed(eq) if _et_date(r["at"]) == today), None)
    start = first["equity"] if first else None
    latest = last["equity"] if last else None
    if first and last:                 # money added or moved today is not a gain or a loss
        latest -= sum(d["amount"] for d in deposits or [] if first["at"] < d["at"] <= last["at"])
    change = round(latest / start - 1, 4) if start and latest else None
    out = {"day": today.isoformat(), "losses_in_a_row": streak,
           "stopped_today": sorted({r["ticker"] for r in todays if r["pnl"] < 0}),
           "realized_today": round(sum(r["pnl"] for r in todays), 2),
           # bought and sold today: the part of today's result the watcher cannot see in what is still held
           "realized_today_new": round(sum(t.pnl for t in trips if _ts(t.closed) and _ts(t.opened)
                                           and _et_date(_ts(t.closed)) == today == _et_date(_ts(t.opened))), 2),
           "day_start_equity": start, "day_change": change,
           "limit": DAILY_LOSS_LIMIT,
           "limit_hit": change is not None and change <= DAILY_LOSS_LIMIT,
           "cool_off": streak >= COOL_OFF_LOSSES and bool(todays) and todays[-1]["pnl"] < 0}
    if out["limit_hit"]:
        out["line"] = (f"DAILY LIMIT HIT: the account is {change:+.1%} today (limit {DAILY_LOSS_LIMIT:.0%}). "
                       "No new buys until tomorrow; manage exits and stops only.")
    elif out["cool_off"]:
        out["line"] = (f"COOL-OFF: {streak} losing sales in a row, the latest today. The next buy is test size; "
                       "back to normal after a winning sale or tomorrow.")
    elif out["stopped_today"]:
        out["line"] = "No buying back today: " + ", ".join(out["stopped_today"]) + " (sold at a loss today)."
    else:
        out["line"] = "Clear: no daily limit, no cool-off."
    return out


def risk_view(tags: list[dict], runs: list[dict], trips: list, now: datetime) -> dict:
    """Did each buy state its stop, and how much of the account did it risk?
    The playbook's guide: about 1% for SAFE, 2-5% for AGGRESSIVE (index parking exempt).
    Losses much bigger than the planned risk mean stops were not honoured."""
    recent = [t for t in tags if t["side"] == "buy" and t.get("qty") and t.get("price")
              and t["setup"] != "index" and now - t["at"] <= timedelta(days=30)]
    sized = []
    for t in recent:
        eq = _equity_at(runs, t["at"])
        if t.get("stop") and t["stop"] < t["price"] and eq:
            pct = t["qty"] * (t["price"] - t["stop"]) / eq
            guide = RISK_GUIDE.get(t["play"], RISK_GUIDE["SAFE"])
            sized.append({"ticker": t["ticker"], "play": t["play"], "risk_pct": round(pct, 4),
                          "over": pct > guide * 1.5})
    with_r = [t for t in trips if t.r is not None]
    blown = [t for t in with_r if t.r < -1.5]
    return {"buys": len(recent), "with_stop": len(sized), "without_stop": len(recent) - len(sized),
            "avg_risk_pct": round(sum(x["risk_pct"] for x in sized) / len(sized), 4) if sized else None,
            "max_risk_pct": max((x["risk_pct"] for x in sized), default=None),
            "over_guide": [x["ticker"] for x in sized if x["over"]],
            "r_trades": len(with_r),
            "avg_r": round(sum(t.r for t in with_r) / len(with_r), 2) if with_r else None,
            "stops_blown": [t.ticker for t in blown]}


def tax_view(trips: list, fills: list[dict], now: datetime) -> dict:
    """This calendar year's realized gains (all short-term: nothing here is held a
    year) and possible wash sales: a loss sale with a buy of the same ticker within
    30 days either side, which defers the loss. An estimate for planning, not advice."""
    year = now.year
    closed = [t for t in trips if _ts(t.closed) and _ts(t.closed).year == year]
    buys = [(f["ticker"].upper(), _ts(f["executed_at"])) for f in fills if f["side"] == "buy" and _ts(f["executed_at"])]
    wash = []
    for t in closed:
        if t.pnl >= 0:
            continue
        sold, opened = _ts(t.closed), _ts(t.opened)
        if any(tk == t.ticker and abs((at - sold).days) <= 30 and abs((at - opened).total_seconds()) > 60
               for tk, at in buys):
            wash.append({"ticker": t.ticker, "closed": t.closed, "loss": t.pnl})
    gains = sum(t.pnl for t in closed if t.pnl > 0)
    losses = sum(t.pnl for t in closed if t.pnl < 0)
    return {"year": year, "realized": round(gains + losses, 2), "gains": round(gains, 2), "losses": round(losses, 2),
            "wash_sale_losses": round(sum(w["loss"] for w in wash), 2), "wash_sales": wash[-10:]}


# --- the wake-up scorecard --------------------------------------------------------------

WAKE_KINDS = ("STOP", "LIMIT", "NEAR", "TARGET", "HALFWAY", "MOVE", "DROP", "MARKET", "NEWS", "ENTRY", "EVENT")
WAKE_DIRECTION = {"STOP": -1, "NEAR": -1, "DROP": -1, "LIMIT": -1, "TARGET": 1, "HALFWAY": 1, "ENTRY": 1}
WAKE_ANSWER = timedelta(minutes=45)     # a run that followed a wake-up wrote its RUN line by then
WAKE_WINDOW = timedelta(days=60)        # the scorecard looks this far back
WAKE_FLAT = 0.003                       # under 0.3% after the wake-up: the price went nowhere
NEWS_MOVED = 0.01                       # a headline "mattered" if the price moved 1%+ either way
# What the scorecard may adjust, and the fence around each: (lowest, highest, default).
# MOVE and NEAR are multipliers on the watcher's own trigger sizes; NEWS_CAP is headline-only
# wake-ups a day. STOP and LIMIT are exits and the daily brake: graded, never tuned.
WAKE_TUNE_BOUNDS = {"MOVE": (0.8, 1.5, 1.0), "NEAR": (0.75, 1.25, 1.0), "NEWS_CAP": (1, 3, 3)}
NEVER_TUNED = ("STOP", "LIMIT")
_WAKE_SPLIT = re.compile(r"; (?=(?:%s): )" % "|".join(WAKE_KINDS))
_WAKE_HEAD = re.compile(r"(%s): (?:([A-Z][A-Z0-9.\-]*)(?![a-z]))?" % "|".join(WAKE_KINDS))


def parse_wakes(text: str) -> list[dict]:
    """The watcher's WAKE lines, oldest first, each split into its events
    (`kind`, `ticker` where the event names one, `direction` +1/-1/0)."""
    out = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or parts[0].upper() != "WAKE":
            continue
        at = _ts(parts[1])
        if at is None:
            continue
        events, seen = [], set()
        for piece in _WAKE_SPLIT.split(" | ".join(parts[2:])):
            m = _WAKE_HEAD.match(piece)
            if not m:
                continue
            kind, ticker = m.group(1), m.group(2)
            if (kind, ticker) in seen:          # three headlines on one stock are one wake-up reason
                continue
            seen.add((kind, ticker))
            direction = WAKE_DIRECTION.get(kind, 0)
            if kind in ("MOVE", "MARKET"):
                pct = re.search(r"([+-]\d+(?:\.\d+)?)% against the market", piece) or \
                    re.search(r"([+-]\d+(?:\.\d+)?)%", piece)
                direction = (1 if float(pct.group(1)) > 0 else -1) if pct and float(pct.group(1)) else 0
            events.append({"kind": kind, "ticker": ticker, "direction": direction, "text": piece[:160]})
        if events:
            out.append({"at": at, "events": events})
    return sorted(out, key=lambda w: w["at"])


def parse_afters(text: str) -> dict:
    """`AFTER | <UTC time of the wake-up> | minutes | TICKER price-then price-after; ...`:
    the watcher's own price check about an hour after each wake-up. Keyed by wake-up time."""
    out: dict = {}
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4 or parts[0].upper() != "AFTER":
            continue
        at, minutes = _ts(parts[1]), _num(parts[2])
        if at is None or not minutes:
            continue
        prices = {}
        for piece in parts[3].split(";"):
            bits = piece.split()
            if len(bits) == 3 and _num(bits[1]) and _num(bits[2]):
                prices[bits[0].upper()] = (_num(bits[1]), _num(bits[2]))
        if prices:
            out[at] = {"minutes": minutes, "prices": prices}
    return out


def _wake_verdict(row: dict) -> str:
    if row["priced"] < MIN_TO_JUDGE:
        return f"too few to judge ({row['priced']} of {MIN_TO_JUDGE} with a price check)"
    rate = row["useful_rate"]
    return "earning its wake-ups" if rate >= 0.5 else "mostly noise" if rate < 0.25 else "mixed"


def wake_tuning(by_kind: dict) -> dict:
    """What the watcher may change by itself, from the grades, always inside
    WAKE_TUNE_BOUNDS. A kind speaks only with 10 price-checked wake-ups. Noise makes a
    trigger harder to set off; a kind that earns its wake-ups gets a little more room.
    STOP and LIMIT are not here and never will be: exits are never tuned or gated."""
    def rate(kind):
        row = by_kind.get(kind)
        return row["useful_rate"] if row and row["priced"] >= MIN_TO_JUDGE else None

    out = {k: d for k, (_lo, _hi, d) in WAKE_TUNE_BOUNDS.items()}
    move, near, headlines = rate("MOVE"), rate("NEAR"), rate("NEWS")
    if move is not None:
        out["MOVE"] = 1.5 if move < 0.10 else 1.25 if move < 0.25 else 0.9 if move >= 0.6 else 1.0
    if near is not None:                     # a smaller band is a later, rarer warning
        out["NEAR"] = 0.75 if near < 0.25 else 1.25 if near >= 0.6 else 1.0
    if headlines is not None:
        out["NEWS_CAP"] = 1 if headlines < 0.10 else 2 if headlines < 0.25 else 3
    return {k: min(hi, max(lo, out[k])) for k, (lo, hi, _d) in WAKE_TUNE_BOUNDS.items() if k not in NEVER_TUNED}


WAKE_WEEK = timedelta(days=7)
_TUNE_WORDS = {"MOVE": ("the MOVE trigger", "x{:.2f}"), "NEAR": ("the NEAR band", "x{:.2f}"),
               "NEWS_CAP": ("headline wake-ups a day", "{:.0f}")}


def wake_week(rows: list[dict], wakes: list[dict], now: datetime, tuning_then: dict, tuning_now: dict) -> dict:
    """The last seven days of the scorecard, for the weekly digest: counts only (no
    tickers, no prices, no amounts), and what the watcher changed about itself."""
    start = now - WAKE_WEEK
    mine = [r for r in rows if r["when"] > start]
    by_kind: dict = {}
    for r in mine:
        k = by_kind.setdefault(r["kind"], {"wakes": 0, "acted": 0, "priced": 0, "continued": 0})
        k["wakes"] += 1
        k["acted"] += r["traded"] or r["adjusted"]
        k["priced"] += r["went"] is not None
        k["continued"] += r["went"] in ("continued", "moved")
    changes = []
    for key, (name, fmt) in _TUNE_WORDS.items():
        was, is_ = tuning_then.get(key), tuning_now.get(key)
        if was is not None and is_ is not None and abs(was - is_) > 1e-9:
            changes.append(f"{name}: {fmt.format(was)} to {fmt.format(is_)}")
    this_week = [w for w in wakes if w["at"] > start]
    return {"from": start.isoformat(timespec="minutes"), "to": now.isoformat(timespec="minutes"),
            "wakes": len(this_week), "prev_wakes": sum(start - WAKE_WEEK < w["at"] <= start for w in wakes),
            "answered": len({r["at"] for r in mine if r["answered"]}),
            "acted": len({r["at"] for r in mine if r["traded"] or r["adjusted"]}),
            "by_kind": {k: by_kind[k] for k in WAKE_KINDS if k in by_kind}, "changes": changes}


def wake_scorecard(text: str, now: datetime | None = None, week: bool = True) -> dict:
    """Grade every wake-up of the last 60 days per trigger kind, from the notes file alone
    (so the trader's light path can rebuild it): was it answered by a run, did the trader
    trade or move a stop on it in that run, and did the price keep going the wake-up's
    way in the hour after (the watcher's AFTER line). A wake-up was `useful` when the
    trader acted on it or the price kept going (a headline: the price moved 1%+)."""
    now = now or datetime.now(timezone.utc)
    wakes = [w for w in parse_wakes(text) if timedelta(0) <= now - w["at"] <= WAKE_WINDOW]
    runs, tags, stops, afters = parse_runs(text), parse_tags(text), parse_stops(text), parse_afters(text)
    by_kind: dict[str, dict] = {}
    recent = []
    for w in wakes:
        answer = next((r for r in runs if w["at"] < r["at"] <= w["at"] + WAKE_ANSWER), None)
        end = answer["at"] + timedelta(minutes=5) if answer else w["at"] + WAKE_ANSWER
        after = afters.get(w["at"])
        for ev in w["events"]:
            whole = ev["ticker"] is None or ev["kind"] in ("MARKET", "LIMIT")   # about the whole account
            mine = lambda x: w["at"] < x["at"] <= end and (whole or x["ticker"] == ev["ticker"])  # noqa: E731, B023
            traded, adjusted = any(mine(t) for t in tags), any(mine(s) for s in stops)
            pair = (after or {}).get("prices", {}).get(ev["ticker"] or "SPY") if ev["kind"] != "LIMIT" else None
            ret = pair[1] / pair[0] - 1 if pair else None
            if ret is None:
                went = None
            elif ev["direction"] == 0:
                went = "moved" if abs(ret) >= NEWS_MOVED else "flat"
            else:
                went = "continued" if ret * ev["direction"] >= WAKE_FLAT else \
                    "reversed" if ret * ev["direction"] <= -WAKE_FLAT else "flat"
            row = by_kind.setdefault(ev["kind"], {"wakes": 0, "answered": 0, "traded": 0, "adjusted": 0, "acted": 0,
                                                  "priced": 0, "continued": 0, "reversed": 0, "useful": 0})
            row["wakes"] += 1
            row["answered"] += answer is not None
            row["traded"] += traded
            row["adjusted"] += adjusted
            row["acted"] += traded or adjusted
            if went is not None:
                row["priced"] += 1
                row["continued"] += went in ("continued", "moved")
                row["reversed"] += went == "reversed"
                row["useful"] += traded or adjusted or went in ("continued", "moved")
            recent.append({"when": w["at"], "at": w["at"].isoformat(timespec="minutes"), "kind": ev["kind"], "ticker": ev["ticker"],
                           "answered": answer is not None, "traded": traded, "adjusted": adjusted,
                           "after": round(ret, 4) if ret is not None else None, "went": went})
    for row in by_kind.values():
        row["useful_rate"] = round(row["useful"] / row["priced"], 3) if row["priced"] else None
        row["verdict"] = _wake_verdict(row)
    ordered = {k: by_kind[k] for k in WAKE_KINDS if k in by_kind}
    tuning = wake_tuning(ordered)
    seven = {}
    if week:                                 # what changed since the settings of a week ago
        then = wake_scorecard(text, now - WAKE_WEEK, week=False)["tuning"]
        seven = wake_week(recent, wakes, now, then, tuning)
    shown = [{k: v for k, v in r.items() if k != "when"} for r in recent[-12:][::-1]]
    return {"days": WAKE_WINDOW.days, "wakes": len(wakes), "by_kind": ordered, "tuning": tuning, "week": seven,
            "bounds": {k: [lo, hi] for k, (lo, hi, _d) in WAKE_TUNE_BOUNDS.items()}, "recent": shown,
            "note": "graded from the notes: acted = a TAG or STOP line in the run that followed; price = the "
                    "watcher's check about an hour later. STOP and LIMIT wake-ups are graded but never tuned."}


def _wakes(text: str | None) -> dict:
    """The scorecard for a rebuild. It is a report card, never worth a failed rebuild:
    the trade record must publish even if a strange WAKE line trips the grading."""
    try:
        return wake_scorecard(text or "")
    except Exception as exc:
        log.warning("wake-ups not graded: %s", exc)
        return {}


def wake_digest(wk: dict) -> list[str]:
    """The watcher's week in plain lines, for the phone report and the weekly push:
    how often it woke the trader, what for, what came of it, what it tuned. Counts
    only: no tickers, no prices, no amounts, no account."""
    w = (wk or {}).get("week") or {}
    if not w or not (w.get("wakes") or w.get("prev_wakes")):
        return []
    lines = [f"Wake-ups in the last 7 days: {w['wakes']} (the 7 days before: {w['prev_wakes']}). A trader run "
             f"followed {w['answered']}; the trader traded or moved a stop on {w['acted']}."]
    parts = []
    for kind, r in (w.get("by_kind") or {}).items():
        bit = f"{kind} {r['wakes']}"
        notes = [f"acted on {r['acted']}"] if r["acted"] else []
        if r["priced"]:
            notes.append(f"price kept going {r['continued']} of {r['priced']}")
        parts.append(bit + (f" ({', '.join(notes)})" if notes else ""))
    if parts:
        lines.append("By reason: " + "; ".join(parts) + ".")
    judged = [f"{k} {r['verdict']}" for k, r in (wk.get("by_kind") or {}).items()
              if not r["verdict"].startswith("too few")]
    lines.append("Grades so far (60 days): " + ("; ".join(judged) if judged else
                                                 "none yet (a reason needs 10 price-checked wake-ups to be judged)") + ".")
    t = wk.get("tuning") or {}
    now_at = (f"MOVE trigger x{t.get('MOVE', 1.0):.2f}, NEAR band x{t.get('NEAR', 1.0):.2f}, headline wake-ups "
              f"{int(t.get('NEWS_CAP', 3))} a day")
    lines.append(("Tuned this week: " + "; ".join(w["changes"]) + f". Now: {now_at}." if w.get("changes") else
                  f"Nothing was tuned this week ({now_at}).") + " STOP and LIMIT are never tuned.")
    return lines


def _wake_lines(wk: dict) -> list[str]:
    """The phone report's section."""
    if not (wk or {}).get("by_kind"):
        return []
    lines = ["", f"WAKE-UPS GRADED (last {wk['days']} days: did the trader act, did the price keep going)"]
    for kind, r in wk["by_kind"].items():
        lines.append(f"  {kind:<8}{r['wakes']:>3} wake-ups, trader acted on {r['acted']}"
                     + (f", price kept going in {r['continued']} of {r['priced']} checked" if r["priced"] else
                        ", none price-checked yet") + f": {r['verdict']}")
    t = wk.get("tuning") or {}
    lines.append(f"  Learned settings (inside fixed limits): MOVE trigger x{t.get('MOVE', 1.0):.2f}, "
                 f"NEAR band x{t.get('NEAR', 1.0):.2f}, headline wake-ups {int(t.get('NEWS_CAP', 3))} a day. "
                 "STOP and LIMIT are never tuned.")
    week = wake_digest(wk)
    if week:
        changed = bool((wk.get("week") or {}).get("changes"))      # the settings line above already says "now"
        lines += ["", "THE WATCHER'S WEEK"] + ["  " + x for x in (week if changed else week[:-1])]
    return lines


def trader_state(runs: list[dict], now: datetime) -> dict:
    if not runs:
        return {"last_run": None}
    eq = [r for r in runs if r["equity"] is not None]
    today = _et_date(now)
    return {"last_run": runs[-1]["at"].isoformat(timespec="minutes"),
            "minutes_since": round((now - runs[-1]["at"]).total_seconds() / 60),
            "runs_today": sum(_et_date(r["at"]) == today for r in runs),
            "last_note": runs[-1]["note"],
            "playbook": next((int(m.group(1)) for r in reversed(runs)
                              for m in [re.match(r"\s*v(\d{1,3})\b", r["note"] or "", re.I)] if m), None),
            "equity": eq[-1]["equity"] if eq else None, "cash": runs[-1]["cash"],
            "equity_first": eq[0]["equity"] if eq else None,
            "equity_first_at": eq[0]["at"].isoformat(timespec="minutes") if eq else None,
            "equity_peak": max(r["equity"] for r in eq) if eq else None}


def summarize(fills: list[dict], tags: list[dict], spy=None, now: datetime | None = None,
              account: str = "", runs: list[dict] | None = None, source: str = "robinhood",
              grades: list[str] | None = None, deposits: list[dict] | None = None,
              stops: list[dict] | None = None, archived: list[dict] | None = None,
              closed: list[dict] | None = None, wakes: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    trips, open_lots = round_trips(attach_tags(fills, tags), spy=spy, now=now)
    archived = list(archived or [])
    left_behind: list[dict] = []
    closed_by_owner: list[dict] = []
    if archived:                       # earlier accounts: their closed trades stay in the record
        old_trips, left_behind = round_trips(attach_tags(archived, tags), spy=spy, now=now)
        trips = sorted(old_trips + trips, key=lambda t: t.closed)
        left_behind, closed_by_owner = drop_closed(left_behind, closed or [])
    spy_arrays = _spy_arrays(spy)
    regimes: dict = {}

    def regime_day(day):
        if day not in regimes:
            regimes[day] = regime_before(spy_arrays, day)
        return regimes[day]

    today = _et_date(now)
    todays = [f for f in fills if _ts(f["executed_at"]) and _et_date(_ts(f["executed_at"])) == today]
    summary = {
        "as_of": now.isoformat(timespec="seconds"),
        "source": source,
        "account": f"••••{account[-4:]}" if account else "",
        "overall": stats(trips),
        "by_asset": _grouped(trips, lambda t: "crypto" if is_crypto(t.ticker) else "stocks"),
        "regime_now": regime_day(today) if spy is not None else None,
        "by_setup_regime": _grouped(trips, lambda t: f"{t.setup}|{regime_day(_et_date(_ts(t.opened)))}")
        if spy is not None else {},
        "by_confidence": _grouped(trips, lambda t: t.confidence),
        "by_play": _grouped(trips, lambda t: t.play),
        "by_style": _grouped(trips, lambda t: t.style),
        "by_setup": _grouped(trips, lambda t: t.setup),
        "by_exit": _grouped(trips, lambda t: t.exit),
        "by_ticker": dict(list(_grouped(trips, lambda t: t.ticker).items())[:10]),
        "today": {"orders": len(todays), "buys": sum(f["side"] == "buy" for f in todays)},
        "open": apply_stops(open_lots, stops or []),
        "recent": [asdict(t) for t in trips[-15:][::-1]],
        "trader": trader_state(runs or [], now),
        "vs_market": vs_market(runs or [], spy, deposits),
        "drawdown": drawdown(runs or []),
        "trading_drawdown": trading_drawdown(trips, (trader_state(runs or [], now) or {}).get("equity")),
        "discipline": discipline(trips, runs or [], now, deposits),
        "risk": risk_view(tags, runs or [], trips, now),
        "tax": tax_view(trips, fills + archived, now),
        "archived_fills": archived,
        "old_account_open": left_behind,
        "old_account_closed": closed_by_owner,
        "spy_closes": spy_for_memory(spy_arrays),
        "grades": list(grades or []),
        "wakes": wakes or {},
        "fills": [{k: f.get(k) for k in ("order_id", "ticker", "side", "quantity", "price", "executed_at")}
                  for f in sorted(fills, key=lambda f: str(f["executed_at"]))],
    }
    summary["confidence"] = confidence(summary["overall"], summary["vs_market"])
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
                     f"{t['runs_today']} run(s) today"
                     + (f", on playbook v{t['playbook']}." if t.get("playbook") else
                        "; playbook version not stated in the RUN line."))
        if t.get("equity") is not None:
            lines.append(f"  Account ${t['equity']:,.2f}" + (f", cash ${t['cash']:,.2f}" if t.get("cash") is not None else "")
                         + f". First recorded ${t['equity_first']:,.2f} on {_et(t['equity_first_at'], '%d %b')}, "
                         f"peak ${t['equity_peak']:,.2f}.")
        if t.get("last_note"):
            lines.append(f"  Last run said: {t['last_note']}")
    m = summary.get("vs_market") or {}
    if m.get("account") is not None:
        line = f"  VS THE MARKET: account {m['account']:+.1%} since {_et(m['since'], '%d %b')}"
        if m.get("spy") is not None:
            line += f", SPY {m['spy']:+.1%}: {'AHEAD' if m['ahead'] >= 0 else 'BEHIND'} by {abs(m['ahead']):.1%}"
        if m.get("deposits"):
            line += f" (deposits of ${m['deposits']:,.2f} taken out)"
        lines.append(line)
    if summary.get("drawdown") is not None and summary["drawdown"] <= -0.05:
        lines.append(f"  DRAWDOWN: the account is {summary['drawdown']:.1%} below its peak.")
    tdd = summary.get("trading_drawdown")
    if tdd is not None and tdd <= -0.05:
        lines.append(f"  TRADES' DRAWDOWN: the trades have given back {-tdd:.1%} of the account from their best"
                     + ("; brake on: new trades LOW size until they win it back." if tdd <= -0.10 else "."))
    dsc = summary.get("discipline") or {}
    if dsc.get("line"):
        lines.append(f"  DISCIPLINE: {dsc['line']}")
    rk = summary.get("risk") or {}
    if rk.get("buys"):
        line = f"  RISK (30 days): {rk['buys']} buys, {rk['with_stop']} with a stated stop"
        if rk.get("avg_risk_pct") is not None:
            line += f", {rk['avg_risk_pct']:.1%} of the account at risk on average"
        if rk.get("avg_r") is not None:
            line += f"; average result {rk['avg_r']:+.2f}R over {rk['r_trades']} trades"
        lines.append(line + ".")
    tx = summary.get("tax") or {}
    if tx.get("realized") is not None and (tx.get("gains") or tx.get("losses")):
        lines.append(f"  TAX {tx['year']}: realized ${tx['realized']:+,.2f} (all short-term)"
                     + (f"; ${-tx['wash_sale_losses']:,.2f} of losses may be wash sales (deferred)"
                        if tx.get("wash_sale_losses") else ""))
    lines += ["", "LESSONS", *("  " + x for x in summary["lessons"])]
    ideas = summary.get("ideas") or {}
    if ideas.get("graded"):
        lines += ["", f"IDEAS GRADED (trades considered, 5 days later, vs the market): {ideas['graded']}"]
        d1 = ideas.get("day1") or {}
        if d1.get("ideas"):
            lines.append(f"  after 1 day: {d1['ideas']} ideas, right {d1['right'] * 100:.0f}%, avg {_pct(d1['avg'])} "
                         "vs the market (early read; the 5-day grades below are the ones that count)")
        if not ideas.get("by_setup"):
            lines.append("  no idea is 5 trading days old yet: per-setup grades start then")
        for k, st in list(ideas.get("by_setup", {}).items()) + [(f"[{k}]", v) for k, v in ideas.get("by_confidence", {}).items()]:
            lines.append(f"  {k:<12}{st['ideas']:>4} ideas, right {st['right'] * 100:.0f}%, avg {_pct(st['avg_5d'])}: {st['verdict']}")
    for name in ("by_asset", "by_confidence", "by_play", "by_style", "by_setup", "by_exit"):
        if summary.get(name) and (name not in ("by_asset", "by_confidence") or len(summary[name]) > 1):
            lines += ["", name.replace("by_", "GRADES BY ").upper()]
            for k, st in summary[name].items():
                lines.append(f"  {k:<12}{st['trades']:>3} trades, won {st['win_rate'] * 100:.0f}%, "
                             f"avg {_pct(st['avg_return'])} ({_pct(st.get('avg_adj'))} vs the market), "
                             f"${st['pnl']:+.2f}: {st['verdict']}")
    lines += _wake_lines(summary.get("wakes") or {})
    lines += ["", "OPEN NOW"]
    if not summary["open"]:
        lines.append("  Nothing open.")
    for o in summary["open"]:
        lines.append(f"  {o['ticker']}: ${o['dollars_in']:.2f} at ${o['entry']:.2f}, held {o['hours_held']:.0f}h, "
                     f"{o['play']} {o['setup']}"
                     + (f", stop {o['stop']:g}" if o.get("stop") else ", NO STOP ON RECORD")
                     + (f", target {o['target']:g}" if o.get("target") else ""))
        if o.get("why"):
            lines.append(f"    why: {o['why']}")
    if (summary.get("account_move") or {}).get("line"):
        lines += ["", summary["account_move"]["line"]]
    if summary.get("old_account_open"):
        lines += ["", "LEFT IN AN EARLIER ACCOUNT (not watched, not in today's plan)"]
        for o in summary["old_account_open"]:
            lines.append(f"  {o['ticker']}: ${o['dollars_in']:.2f} at ${o['entry']:.2f}")
    if summary.get("old_account_closed"):
        lines += ["", "CLOSED BY YOU IN AN EARLIER ACCOUNT (not graded as the trader's trades): "
                  + ", ".join(o["ticker"] for o in summary["old_account_closed"])]
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


def _with_ideas(summary: dict, engine, tags_text: str, previous: dict | None = None) -> dict:
    """Grade the trader's IDEA lines with the same price source the scan uses. Without
    prices (the trader's own light path after every note, or a blocked cloud runner)
    the last grades are carried forward: wiping them made every confidence check
    between two full runs blind to the ideas, the memory's best evidence."""
    empty = {"graded": 0, "by_setup": {}, "by_confidence": {}, "recent": []}
    carried = (previous or {}).get("ideas") or empty
    try:                                   # the watcher's list; never worth a failed rebuild
        summary["idea_watch"] = idea_watch(tags_text, [o.get("ticker") for o in summary.get("open") or []])
    except Exception as exc:
        log.warning("ideas to watch not listed: %s", exc)
        summary["idea_watch"] = []
    ideas = parse_ideas(tags_text)
    if not ideas:
        summary["ideas"] = empty
        return summary
    if engine is None:
        summary["ideas"] = carried
        return summary

    def closes(t):
        hist = engine.market.history(t)
        return None if hist is None else hist["Close"]

    try:
        graded = grade_ideas(ideas, closes, spy=_spy_closes(engine))
    except Exception as exc:
        log.warning("ideas not graded: %s", exc)
        graded = empty
    summary["ideas"] = graded if graded.get("graded", 0) >= carried.get("graded", 0) else carried
    return summary


def state_dir():
    """Same folder as today.state_dir, without importing the sheet's dependencies."""
    import os
    from pathlib import Path

    override = os.environ.get("STOCKSAGE_STATE")
    return Path(override).expanduser() if override else Path("~/.stocksage").expanduser()


def _save_and_publish(summary: dict, upload: bool) -> None:
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


def append_note(line: str, upload: bool = True) -> str:
    """Add one TAG or RUN line to the tags file in Drive, through this app (the
    trader's own Drive connector can create files but not edit them). The line
    is checked first: anything that does not parse is refused, not stored."""
    line = " ".join((line or "").split())
    if re.search(r"/bin/(ba|z)?sh|/usr/bin/", line):
        raise ValueError("the terminal swallowed a dollar sign (\"$0.82\" became \"/bin/bash.82\"): "
                         "put the whole line in single quotes, or write amounts without the $ sign")
    kind = line.split("|", 1)[0].strip().upper()
    if kind == "TAG" and not parse_tags(line):
        raise ValueError("not a valid TAG line: TAG | UTC time | BUY or SELL | TICKER | SAFE or AGGRESSIVE | "
                         "setup | shares | price | why")
    if kind == "RUN" and not parse_runs(line):
        raise ValueError("not a valid RUN line: RUN | UTC time | account value | cash | what this run did")
    if kind == "IDEA" and not parse_ideas(line):
        raise ValueError("not a valid IDEA line: IDEA | UTC time | BUY or SELL | TICKER | setup | price | [LEVEL] why")
    if kind == "DEPOSIT" and not parse_deposits(line):
        raise ValueError("not a valid DEPOSIT line: DEPOSIT | UTC time | amount (negative for a withdrawal)")
    if kind == "AFTER" and not parse_afters(line):
        raise ValueError("not a valid AFTER line: AFTER | UTC time of the wake-up | minutes | TICKER price price")
    if kind in ("WAKE", "WATCH"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or _ts(parts[1]) is None:
            raise ValueError(f"not a valid {kind} line: {kind} | UTC time | what happened")
    if kind == "STOP" and not parse_stops(line):
        raise ValueError("not a valid STOP line: STOP | UTC time | TICKER | stop price | target price (optional)")
    if kind == "CLOSED" and not parse_closed(line):
        raise ValueError("not a valid CLOSED line: CLOSED | UTC time | TICKER | why (optional)")
    if kind not in NOTE_KINDS:
        raise ValueError("a note starts with TAG, RUN, IDEA, DEPOSIT, STOP, CLOSED, WAKE, WATCH or AFTER")
    if not upload:
        return line
    _write_notes([line])
    return line


NOTE_KINDS = ("TAG", "RUN", "IDEA", "DEPOSIT", "STOP", "CLOSED", "WAKE", "WATCH", "AFTER")


def split_notes(args) -> list[str]:
    """The lines given to one note command. When any quoted argument (or line of one)
    starts with a note word and a bar, every argument is a line of its own, so a
    malformed one is refused by itself and never glued onto its neighbour. Otherwise
    it is the old unquoted form: one line that arrived word by word."""
    starts = re.compile(r"(?:%s)\s*\|" % "|".join(NOTE_KINDS), re.I)
    pieces = [p.strip() for arg in args for p in str(arg).splitlines() if p.strip()]
    if any(starts.match(p) for p in pieces):
        return pieces
    return [" ".join(pieces)] if pieces else []


def _write_notes(lines: list[str]) -> str:
    """Add checked lines to the notes file in ONE read and ONE write; returns the file's
    text afterwards. The read is strict: a failed read used to pass for an empty file,
    and the write that followed would have replaced every note with the new line."""
    from .drive_api import ensure_folder, get_service, read_or_create_text, upload_or_update

    current = read_or_create_text(TAGS_NAME, TAGS_HEADER)
    have = set(current.splitlines())
    fresh = [x for x in dict.fromkeys(lines) if x not in have]   # a retried command adds nothing
    if not fresh:
        return current
    text = (current.rstrip("\n") + "\n" if current else TAGS_HEADER) + "\n".join(fresh) + "\n"
    service = get_service()
    upload_or_update(service, TAGS_NAME, text, "text/plain", ensure_folder(service))
    return text


def append_notes(lines: list[str], upload: bool = True) -> tuple[list[str], list[tuple[str, str]], str | None]:
    """Several note lines at once (the trader's whole run in one command): every line is
    checked, the good ones are stored together, a bad one is refused with its reason and
    does not hold the others back. Returns (recorded, refused, the file's text)."""
    good: list[str] = []
    bad: list[tuple[str, str]] = []
    for raw in lines:
        try:
            good.append(append_note(raw, upload=False))
        except ValueError as exc:
            bad.append((" ".join(str(raw).split()), str(exc)))
    text = _write_notes(good) if upload and good else None
    return good, bad, text


def watcher_silent(pulse_at, now: datetime | None = None) -> datetime | None:
    """When the owner's watcher last said it was alive, if that is too long ago to trust.

    The watcher overwrites a small pulse file in Drive every few minutes while it watches.
    Judged on weekdays from 9:50 AM to 4:00 PM New York time (the session, once the watcher
    has had time to write). None = alive, not judged now, or never set up (no pulse yet)."""
    from zoneinfo import ZoneInfo

    last = _ts(pulse_at) if pulse_at else None
    if last is None:
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(ET))
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5 or not (9 * 60 + 50 <= minutes <= 16 * 60):
        return None
    return last if now - last > PULSE_SILENT else None


def _drive_text(name: str) -> str | None:
    from .drive_api import read_drive_text

    return read_drive_text(name)


def _drive_put(name: str, text: str) -> None:
    from .drive_api import publish_text

    publish_text(name, text)


def watcher_notice(out=print, now: datetime | None = None, read=None, write=None) -> bool:
    """Tell the trader, in its own run, that the owner's watcher has gone silent: until it
    is back the hourly run is the only stop. The first notice of a day also asks the trader
    to start the watchdog, which tells the owner's phone (the trader's environment holds no
    phone topic; the watchdog job does). Never raises; True when the watcher is silent."""
    from zoneinfo import ZoneInfo

    read, write = read or _drive_text, write or _drive_put
    now = now or datetime.now(timezone.utc)
    try:
        last = watcher_silent((read(PULSE_NAME) or "").strip(), now)
    except Exception as exc:
        out(f"(Watcher not checked: {type(exc).__name__})")
        return False
    if last is None:
        return False
    eastern = ZoneInfo(ET)
    quiet = int((now - last).total_seconds() // 60)
    out(f"WATCHER SILENT: the owner's watcher last checked in at "
        f"{last.astimezone(eastern).strftime('%a %I:%M %p ET')} ({quiet} minutes ago). Until it is back, "
        "this hourly run is the only stop (playbook section 2b).")
    today = now.astimezone(eastern).date().isoformat()
    try:
        told = (read(PULSE_TOLD_NAME) or "").strip() == today
    except Exception:
        told = False
    if told:
        out("The owner's phone was already told today.")
        return True
    out("TELL THE OWNER'S PHONE now (once a day): gh api --method POST "
        "repos/bramstedtwd-afk/Stock-project/actions/workflows/watchdog.yml/dispatches -f ref=main")
    try:
        write(PULSE_TOLD_NAME, today)
    except Exception:
        pass                      # not marked: the next run asks again, which is the safe side
    return True


def note_command(args, refresh=None, out=print, notice=None) -> int:
    """What `journal note` and `trader-note` do: record the lines, then rebuild the
    report ONCE. Exit 0 = all recorded, 2 = a line was refused (the others are in),
    1 = Drive could not be reached (nothing recorded). `notice` (the light path passes
    `watcher_notice`) adds a line when the owner's watcher has gone silent."""
    lines = split_notes(args)
    try:
        good, bad, text = append_notes(lines)
    except Exception as exc:
        out(f"Not recorded (Drive): {type(exc).__name__}: {exc}")
        return 1
    for line in good:
        out(f"Recorded: {line}")
    for line, why in bad:
        out(f"Not recorded: {why}")
        if len(lines) > 1:
            out(f"    the refused line: {line[:160]}")
    if good and refresh is not None:
        try:
            refresh(text)
            out("Trade report refreshed.")
        except Exception as exc:
            out(f"(Report not refreshed: {exc})")
    if notice is not None:
        notice(out)
    return 2 if bad else 0


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
    reachable = getattr(client, "has_account", None)
    if reachable is not None and reachable(account) is False:
        # The agentic account lives under another Robinhood login than this computer's
        # (an account switch to a new login). Robinhood cannot be asked, so the record is
        # built the way the cloud builds it: confirmed fills plus the trader's notes.
        log.warning("account ••••%s is not under this computer's Robinhood login; the trade memory is "
                    "built from the trader's notes", account[-4:])
        return refresh_without_broker(engine, upload=upload, tags_text=tags_text, account=account)
    fills = client.order_history(account_number=account)
    if fills is None:
        return None
    archived: list[dict] = []
    since = None
    if upload:
        try:
            previous = _read_previous_memory()
        except MemoryUnreadable as exc:
            log.warning("%s; built from Robinhood's list alone and NOT published (the record of earlier "
                        "accounts lives in that file)", exc)
            previous, upload = None, False
        if tags_text is None:
            tags_text = _read_drive_tags()
        since = (datetime.now(timezone.utc).isoformat(timespec="minutes") if switched(previous, account)
                 else _account_since(previous))
        fills, archived = carry_over(fills, previous, account, parse_tags(tags_text or ""))
    if tags_text is None:
        tags_text = ""
    broker_crypto = None
    try:
        broker_crypto = client.crypto_order_history()
    except Exception:
        broker_crypto = None
    fills = list(fills) + crypto_fills(parse_tags(tags_text), broker_crypto,
                                       os.environ.get("STOCKSAGE_AGENTIC_CRYPTO_ID", "").strip())
    if grades is None:
        try:
            from .scorekeeping import lines as grade_lines

            grades = grade_lines(engine.db)
        except Exception:
            grades = []
    runs, deposits = parse_runs(tags_text), parse_deposits(tags_text)
    move = account_move(runs, deposits, since)
    summary = summarize(fills, parse_tags(tags_text), spy=_spy_closes(engine), account=account,
                        runs=runs, source="robinhood", grades=grades,
                        deposits=_with_move(deposits, move), stops=parse_stops(tags_text), archived=archived,
                        closed=parse_closed(tags_text), wakes=_wakes(tags_text))
    if since:
        summary["account_since"] = since
        summary["account_move"] = _move_view(move)
    _with_ideas(summary, engine, tags_text or "")
    _save_and_publish(summary, upload)
    return summary


class MemoryUnreadable(RuntimeError):
    """The previous trade memory exists but could not be read: rebuilding without it
    would publish a memory that has lost every Robinhood-confirmed fill."""


def _read_previous_memory(reader=None, tries: int = 2, wait: float = 2.0) -> dict | None:
    """The trade memory now in Drive, or None only when there truly is none yet.
    A failed or garbled read raises MemoryUnreadable instead of passing for "empty":
    on 2026-11-05 a failed read on the trader's light path was taken as no history,
    and the rebuilt file dropped 28 closed trades and the PLD position."""
    import time

    if reader is None:
        from .drive_api import read_drive_text

        reader = read_drive_text
    last = None
    for attempt in range(tries):
        try:
            raw = reader(MEMORY_NAME)
            if not raw:
                return None
            got = json.loads(raw)
            if isinstance(got, dict):
                return got
            last = ValueError("not a JSON object")
        except Exception as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(wait)
    raise MemoryUnreadable(f"could not read {MEMORY_NAME} from Drive ({type(last).__name__}); the report was not "
                           "rebuilt, so the confirmed trade history is kept as it is")


def _confirmed(fills: list | None) -> list:
    return [f for f in fills or [] if not str(f.get("order_id", "")).startswith("tag-")]


def _last4(account: str | None) -> str:
    digits = re.sub(r"\D", "", str(account or ""))
    return digits[-4:]


def carry_over(fills: list[dict], previous: dict | None, account: str,
               tags: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """(this account's fills, the archive of earlier accounts' fills).

    Same account as the memory in Drive: Robinhood's read merged with every fill it
    confirmed before (keep_confirmed). A NEW agentic account: the old account's fills
    move to the archive, where their closed trades stay in the record (the trader's
    setups, verdicts and confidence history carry over) but nothing in it counts as
    open in the new account, so the watcher never guards a position that is not there.
    The old account is never read again, so its trades the trader noted after
    Robinhood's last confirmation (`tags`) are archived from the notes."""
    previous = previous or {}
    archived = [dict(f) for f in previous.get("archived_fills") or []]
    if switched(previous, account):
        old = _last4(previous.get("account"))
        confirmed = [f for f in _confirmed(previous.get("fills")) if not is_crypto(f.get("ticker", ""))]
        stock_tags = [t for t in tags or [] if not is_crypto(t["ticker"])]
        seen = {f.get("order_id") for f in archived}
        moved = datetime.now(timezone.utc)           # notes after the move belong to the new account
        archived += [dict(f, account=old) for f in confirmed + _noted_after(confirmed, stock_tags)
                     if f.get("order_id") not in seen and (_ts(f["executed_at"]) or moved) <= moved]
        return list(fills), archived
    return keep_confirmed(fills, previous), archived


def _account_since(previous: dict | None) -> str | None:
    """When the memory's account took over. A memory moved to a new account before the move
    time was recorded (2026-11-06) holds an archive but no confirmed fills of its own: the time
    it was built is the move, so the old account's notes are never read as the new one's trades."""
    previous = previous or {}
    if previous.get("account_since"):
        return previous["account_since"]
    if previous.get("archived_fills") and not _confirmed(previous.get("fills")):
        return previous.get("as_of")
    return None


def account_move(runs: list[dict], deposits: list[dict], since) -> dict | None:
    """The bridge across an account move, for the account-vs-market comparison.

    `value_before` is the old account's last check-in. When the new account's first
    check-in with money came without a DEPOSIT line since the move, the difference is
    entered as an implied deposit (`implied`), as playbook section 14 prescribes, so
    money that did or did not come over never counts as a gain or a loss."""
    since = _ts(since) if isinstance(since, str) else since
    if since is None:
        return None
    eq = [r for r in runs if r.get("equity")]
    before = [r for r in eq if r["at"] <= since]
    after = [r for r in eq if r["at"] > since]
    out = {"since": since.isoformat(timespec="minutes"),
           "value_before": before[-1]["equity"] if before else None, "implied": None}
    if before and after and not any(d["at"] > since for d in deposits):
        out["implied"] = {"at": after[0]["at"] - timedelta(seconds=1),
                          "amount": round(after[0]["equity"] - before[-1]["equity"], 2)}
    return out


def _move_view(move: dict | None) -> dict | None:
    """What the trader reads in the memory: the value to measure the first DEPOSIT from."""
    if not move:
        return None
    implied = move.get("implied")
    return {"since": move["since"], "value_before": move["value_before"],
            "implied_deposit": implied["amount"] if implied else None,
            "line": (f"Account moved {move['since'][:10]}: the old account's last value was "
                     f"{move['value_before']:.2f}. First run with money in the new account: DEPOSIT = its value "
                     f"minus {move['value_before']:.2f}." if move.get("value_before") else
                     f"Account moved {move['since'][:10]}.")
                    + (f" Not written yet, so {implied['amount']:+.2f} is counted as moved money."
                       if implied else "")}


def _with_move(deposits: list[dict], move: dict | None) -> list[dict]:
    return deposits + [move["implied"]] if move and move.get("implied") else deposits


def switched(previous: dict | None, account: str | None) -> bool:
    """True when the memory in Drive belongs to another agentic account than `account`."""
    old = _last4((previous or {}).get("account"))
    return bool(old and _last4(account) and old != _last4(account))


def _noted_after(confirmed: list[dict], stock_tags: list[dict], since=None) -> list[dict]:
    """Fills from the trader's notes that came after Robinhood's last confirmed fill
    (and after `since`, when the account changed), minus any it already confirmed."""
    last = max((_ts(f["executed_at"]) for f in confirmed if _ts(f["executed_at"])), default=None)
    since = _ts(since) if isinstance(since, str) else since
    if since is not None and (last is None or since > last):
        last = since

    def already_confirmed(f):              # the trader's note can be stamped a few minutes after the fill
        at = _ts(f["executed_at"])
        return any(c["ticker"] == f["ticker"] and c["side"] == f["side"] and _ts(c["executed_at"])
                   and abs(_ts(c["executed_at"]) - at) <= MATCH_WINDOW
                   and abs(float(c["quantity"]) - f["quantity"]) <= 0.02 * max(f["quantity"], 1e-9)
                   for c in confirmed)
    return [f for f in fills_from_tags(stock_tags, after=last) if not already_confirmed(f)]


def keep_confirmed(fills: list[dict], previous: dict | None) -> list[dict]:
    """Robinhood's fills plus every stock fill it confirmed before. An order once
    filled never goes away, but a read can come back short: the history skips any
    order whose ticker lookup fails, and on 2026-11-05 (Robinhood answering 502/503)
    the memory lost the PLD position and 28 closed trades. Merged by order id."""
    merged = {f["order_id"]: f for f in _confirmed((previous or {}).get("fills"))
              if f.get("order_id") and not is_crypto(f.get("ticker", ""))}
    for f in fills:
        merged[f["order_id"]] = f
    return sorted(merged.values(), key=lambda f: str(f.get("executed_at")))


def refresh_without_broker(engine, upload: bool = True, previous: dict | None = None,
                           tags_text: str | None = None, account: str | None = None) -> dict:
    """The cloud runner's run: no broker login. Reuse the fills Robinhood last
    confirmed (kept in the previous memory file) and add only tagged fills that
    came after them, so the phone report stays current while the computer is off.

    `account` (the owner's computer only, from .env) moves the record to a new agentic
    account when the memory names another one: the old account's fills are archived
    and only notes made after the move count as the new account's trades."""
    if previous is None and upload:
        previous = _read_previous_memory()
    previous = previous or {}
    if tags_text is None:
        tags_text = _read_drive_tags() if upload else ""
    tags = parse_tags(tags_text)
    account = (account or "").strip() or (previous.get("account") or "").replace("••••", "")
    since = _account_since(previous)
    archived = previous.get("archived_fills")
    if switched(previous, account):
        since = datetime.now(timezone.utc).isoformat(timespec="minutes")
        _, archived = carry_over([], previous, account, tags)
        previous_view = {}                 # nothing the old account confirmed belongs to the new one
    else:
        previous_view = previous
    confirmed = _confirmed(previous_view.get("fills"))
    crypto_confirmed = [f for f in confirmed if is_crypto(f["ticker"])]
    confirmed = [f for f in confirmed if not is_crypto(f["ticker"])]
    stock_tags = [t for t in tags if not is_crypto(t["ticker"])]
    crypto_noted = [f for f in crypto_fills(tags)
                    if not any(c["ticker"] == f["ticker"] and c["side"] == f["side"] and _ts(c["executed_at"])
                               and abs(_ts(c["executed_at"]) - _ts(f["executed_at"])) <= MATCH_WINDOW
                               for c in crypto_confirmed)]
    fills = confirmed + _noted_after(confirmed, stock_tags, since) + crypto_confirmed + crypto_noted
    spy = _spy_closes(engine) if engine is not None else None
    if spy is None or len(spy) == 0:
        spy = previous.get("spy_closes") or None       # the last full run's SPY closes, carried in the file
    runs, deposits = parse_runs(tags_text), parse_deposits(tags_text)
    move = account_move(runs, deposits, since)
    summary = summarize(fills, tags, spy=spy, account=account, runs=runs,
                        stops=parse_stops(tags_text), archived=archived, closed=parse_closed(tags_text),
                        deposits=_with_move(deposits, move),
                        source="robinhood" if not fills or len(fills) == len(confirmed) + len(crypto_confirmed)
                        else "robinhood+notes",
                        grades=previous.get("grades"), wakes=_wakes(tags_text))
    if since:
        summary["account_since"] = since
        summary["account_move"] = _move_view(move)
    _with_ideas(summary, engine, tags_text or "", previous)
    _refuse_smaller(summary, previous)
    _save_and_publish(summary, upload)
    return summary


def _refuse_smaller(summary: dict, previous: dict) -> None:
    """A rebuild may never hold fewer Robinhood-confirmed fills (this account's plus the
    archive of earlier accounts') than the file it replaces."""
    new_archive, old_archive = len(summary.get("archived_fills") or []), len(previous.get("archived_fills") or [])
    if new_archive < old_archive or \
            len(_confirmed(summary.get("fills"))) + new_archive < len(_confirmed(previous.get("fills"))) + old_archive:
        raise MemoryUnreadable("the rebuilt report would hold fewer Robinhood-confirmed fills than the one in Drive; "
                               "not published")


def main(argv: list[str] | None = None) -> int:
    """`python -m stocksage.journal note '<line>' ['<line>' ...]`: the trading routine's
    light path. Needs only STOCKSAGE_DRIVE_TOKEN, not the rest of StockSage. Records
    the lines in one write, then rebuilds the phone report once."""
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["watcher"]:
        # The start of a run: is the owner's watcher alive? Exit 0 either way (a silent
        # watcher is something to act on, not a failed command).
        if not watcher_notice():
            print("Watcher: checking in (or not judged outside 9:50 AM-4:00 PM ET on weekdays).")
        return 0
    if len(argv) < 2 or argv[0] != "note":
        print("usage: python -m stocksage.journal note 'RUN | 2026-10-29T15:35Z | 104.22 | 11.56 | what happened'"
              " ['IDEA | ...' 'STOP | ...']   (each line in its own single quotes)")
        return 2
    # One rebuild for the whole command: each rebuild is about half a minute of a run.
    return note_command(argv[1:], refresh=lambda text: refresh_without_broker(None, upload=True, tags_text=text),
                        notice=watcher_notice)


if __name__ == "__main__":
    raise SystemExit(main())
