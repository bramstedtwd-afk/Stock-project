"""The overnight edge, tested forward without risking a cent.

The backtest (STRATEGY.md, "The overnight effect") found that holding QQQ or IWM only from
the afternoon to the next open, while the fund is above its 200-day average, beat its own
risk on 2001-2016 AND on the untouched 2017+ years, but only while a trade costs under
about 0.25 basis points a side; at 0.5 bp it no longer clears the bar, at 1 bp nothing
does. So the whole question is the real cost, and this measures it:

  * every trading day at the 3:35 PM and 9:35 AM run times (ET) it records the live bid
    and ask of each fund (the cost meter: half the gap between them is what a market order
    gives away, before fees);
  * when the fund is in its uptrend it buys $10,000 of it at 3:35 PM in the Alpaca PAPER
    account and sells the same shares at 9:35 AM the next session (the trade itself, so
    the fills can be compared with the quotes and with the official close and open);
  * `report()` compares the measured cost with the backtest's break-even and gives a
    verdict only after MIN_NIGHTS measured nights.

Everything goes through `paper` (pinned to Alpaca's paper host; refuses any account whose
number does not start with PA). Orders carry client ids `ov-...`, which the practice-trade
grading (`paper.CLIENT_RE`, `ss-...`) never counts. Standard library only; state in
state_dir/overnight.json. Alpaca's free quotes are IEX's own, usually WIDER than the whole
market's best prices, so the measured spread is an upper bound on the real one. StockSage
never trades a real account; this changes nothing the trader does.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paper

FUNDS = ("QQQ", "IWM")
STAKE = 10_000                      # paper dollars per fund per night
BUY_AT, SELL_AT = (15, 35), (9, 35)  # the trader's run times, ET
WINDOW_MIN = 15                     # act once inside the 15 minutes after each time
TREND_DAYS = 200
PASS_BP, FAIL_BP = 0.25, 0.50       # per-side cost: the backtest's holdout passed / failed (STRATEGY.md)
FEE_BP = 0.28                       # SEC fee on sales (about $27.80 per $1M), so ~0.14 bp per side on average
MIN_NIGHTS = 40
KEEP = 600


def path() -> Path:
    from .journal import state_dir

    return state_dir() / "overnight.json"


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(book: dict) -> None:
    try:
        path().parent.mkdir(parents=True, exist_ok=True)
        path().write_text(json.dumps(book, indent=1), encoding="utf-8")
    except OSError:
        pass


def _et(now: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo("America/New_York"))


def _in_window(et: datetime, at: tuple[int, int]) -> bool:
    start = et.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return et.weekday() < 5 and start <= et < start + timedelta(minutes=WINDOW_MIN)


def quote(ticker: str, http=None) -> dict:
    got = paper._call("GET", f"/v2/stocks/{ticker}/quotes/latest?feed=iex", host=paper.DATA_HOST, http=http)
    q = got.get("quote") or {}
    return {"bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0)}


def half_spread_bp(q: dict) -> float | None:
    bid, ask = q.get("bid") or 0, q.get("ask") or 0
    if not (0 < bid <= ask) or ask > bid * 1.01:            # one-sided or broken quote: no reading
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / 2 / mid * 1e4


def in_uptrend(ticker: str, now: datetime, http=None) -> bool | None:
    """Above its 200-day average of daily closes before today (known before the buy)."""
    start = (now - timedelta(days=330)).date().isoformat()
    got = paper._call("GET", f"/v2/stocks/{ticker}/bars?timeframe=1Day&start={start}&limit=400&feed=iex"
                             f"&adjustment=all", host=paper.DATA_HOST, http=http)
    today = _et(now).date().isoformat()
    closes = [float(b["c"]) for b in got.get("bars") or [] if str(b.get("t", ""))[:10] < today]
    if len(closes) < TREND_DAYS:
        return None
    return closes[-1] > statistics.fmean(closes[-TREND_DAYS:])


def _order(cid: str, http=None) -> dict:
    return paper._call("GET", f"/v2/orders:by_client_order_id?client_order_id={cid}", http=http)


def step(book: dict, now: datetime | None = None, http=None) -> list[str]:
    """One look, every minute from the watcher. Acts once per window per day; never fatal for
    one fund (a failure is recorded and the other fund goes on)."""
    now = now or datetime.now(timezone.utc)
    et = _et(now)
    day = et.date().isoformat()
    nights = book.setdefault("nights", [])
    quotes = book.setdefault("quotes", [])
    done = book.setdefault("done", {})
    out: list[str] = []
    _fill_in(nights, http)
    for phase, at in (("buy", BUY_AT), ("sell", SELL_AT)):
        if not _in_window(et, at) or done.get(phase) == day:
            continue
        done[phase] = day
        clock = paper._call("GET", "/v2/clock", http=http)
        if not clock.get("is_open"):
            out.append(f"overnight test: market closed at {phase} time ({day}), nothing done")
            continue
        paper.account(http)                       # refuses anything but a paper account before any order
        for f in FUNDS:
            try:
                q = quote(f, http)
                quotes.append({"fund": f, "at": now.isoformat(timespec="minutes"), "phase": phase, **q})
                if phase == "buy":
                    trend = in_uptrend(f, now, http)
                    night = {"fund": f, "day": day, "trend": trend, "buy_q": q}
                    if trend:
                        cid = f"ov-{f}-{et:%Y%m%d}"
                        paper._call("POST", "/v2/orders", {"symbol": f, "notional": str(STAKE), "side": "buy",
                                                           "type": "market", "time_in_force": "day",
                                                           "client_order_id": cid}, http=http)
                        night["buy_id"] = cid
                        out.append(f"overnight test: bought ${STAKE:,} of {f} on paper (uptrend), "
                                   f"spread {half_spread_bp(q) or 0:.2f} bp a side")
                    else:
                        out.append(f"overnight test: {f} not in its uptrend, quotes recorded only")
                    nights.append(night)
                else:
                    for n in nights:
                        if n["fund"] == f and n.get("buy_id") and not n.get("sell_id") and n["day"] < day:
                            n["sell_q"], n["sell_day"] = q, day
                            buy = _order(n["buy_id"], http)
                            qty = buy.get("filled_qty")
                            if not qty or float(qty) <= 0:
                                n["sell_id"] = "none (buy not filled)"
                                continue
                            n["qty"], n["buy_px"] = float(qty), float(buy.get("filled_avg_price") or 0)
                            cid = f"{n['buy_id']}-out"
                            paper._call("POST", "/v2/orders", {"symbol": f, "qty": str(qty), "side": "sell",
                                                               "type": "market", "time_in_force": "day",
                                                               "client_order_id": cid}, http=http)
                            n["sell_id"] = cid
                            out.append(f"overnight test: sold the {f} bought {n['day']} on paper")
            except paper.NotPaper:
                raise
            except Exception as exc:
                out.append(f"overnight test: {f} {phase} skipped ({type(exc).__name__})")
    book["nights"], book["quotes"] = nights[-KEEP:], quotes[-2 * KEEP:]
    return out


def _fill_in(nights: list[dict], http=None) -> None:
    """Read each sale's fill price once it exists (a minute or more after the order)."""
    for n in nights:
        if n.get("sell_id", "").startswith("ov-") and not n.get("sell_px"):
            try:
                o = _order(n["sell_id"], http)
                if o.get("filled_avg_price"):
                    n["sell_px"] = float(o["filled_avg_price"])
            except Exception:
                pass


def _slip_bp(fill: float, q: dict, side: str) -> float | None:
    if not fill or half_spread_bp(q) is None:
        return None
    mid = (q["bid"] + q["ask"]) / 2
    return ((fill - mid) if side == "buy" else (mid - fill)) / mid * 1e4


def report(book: dict, official: dict | None = None) -> dict:
    """The cost per side measured so far and the verdict. `official` = {fund: {date: (open, close)}}
    (from the CLI, yfinance) adds the ideal close-to-open move for the nights held."""
    spreads = [b for q in book.get("quotes") or [] if (b := half_spread_bp(q)) is not None]
    slips = []
    nights_out = []
    for n in book.get("nights") or []:
        if n.get("sell_px") and n.get("buy_px"):
            for fill, q, side in ((n["buy_px"], n["buy_q"], "buy"), (n["sell_px"], n.get("sell_q") or {}, "sell")):
                s = _slip_bp(fill, q, side)
                if s is not None:
                    slips.append(s)
            row = {"fund": n["fund"], "day": n["day"], "paper": n["sell_px"] / n["buy_px"] - 1}
            ideal = None
            o = (official or {}).get(n["fund"]) or {}
            if n["day"] in o and n.get("sell_day") in o:
                ideal = o[n["sell_day"]][0] / o[n["day"]][1] - 1
            row["ideal"] = ideal
            nights_out.append(row)
    spread = statistics.fmean(spreads) if spreads else None
    slip = statistics.fmean(slips) if slips else None
    cost = None if spread is None else max(spread, slip if slip is not None else spread) + FEE_BP / 2
    measured = len({q["at"][:10] for q in book.get("quotes") or []})
    if cost is None or measured < MIN_NIGHTS:
        verdict = f"too early to judge ({measured} of {MIN_NIGHTS} days measured)"
    elif cost <= PASS_BP:
        verdict = "costs are low enough: the overnight edge survives them (worth a real-money proposal to the owner)"
    elif cost >= FAIL_BP:
        verdict = "too costly: real trading costs eat the overnight edge"
    else:
        verdict = "on the edge: costs sit between the backtest's pass and fail lines"
    return {"days": measured, "spread_bp": spread, "slip_bp": slip, "cost_bp": cost, "nights": nights_out,
            "verdict": verdict, "pass_bp": PASS_BP, "fail_bp": FAIL_BP}


def lines(r: dict) -> list[str]:
    def bp(x):
        return "n/a" if x is None else f"{x:.2f} bp"

    out = ["OVERNIGHT TEST (paper money, QQQ and IWM, 3:35 PM -> 9:35 AM in an uptrend)",
           f"  days measured: {r['days']}   half the bid-ask gap: {bp(r['spread_bp'])}   "
           f"fill vs mid: {bp(r['slip_bp'])}   cost per side incl. fees: {bp(r['cost_bp'])}",
           f"  backtest: passes at {r['pass_bp']} bp a side, fails at {r['fail_bp']} bp",
           f"  verdict: {r['verdict']}"]
    held = r["nights"]
    if held:
        avg = statistics.fmean(n["paper"] for n in held)
        ideal = [n["ideal"] for n in held if n["ideal"] is not None]
        out.append(f"  nights held on paper: {len(held)}, average {avg * 100:+.3f}% a night"
                   + (f" (ideal close-to-open {statistics.fmean(ideal) * 100:+.3f}%)" if ideal else ""))
    return out
