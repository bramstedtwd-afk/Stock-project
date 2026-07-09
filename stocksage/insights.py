"""Background insight from your real Robinhood history.

Once the brain mirrors your full order and dividend history, this module
answers the questions a good advisor would ask about YOU, not the market:

  - What is your actual realized P&L, per name and overall? (FIFO lot
    matching — the same convention brokers and the IRS use)
  - What is your win rate on completed round trips, and how long do you
    really hold?
  - How much have dividends quietly contributed?
  - Do your trades agree with the model — and what happened when they
    didn't? Over time this shows whose judgment to trust, per situation.

Everything is computed from the mirrored history in the brain, so it works
offline, travels with the brain, and updates automatically each sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")
ALIGNMENT_WINDOW_DAYS = 5  # a suggestion this recent counts as model opinion


def _parse_ts(value: str) -> datetime | None:
    """Parse to a UTC-aware datetime; naive timestamps are assumed UTC
    (suggestions are stored naive-UTC, Robinhood sends explicit offsets)."""
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class RoundTrip:
    ticker: str
    quantity: float
    buy_price: float
    sell_price: float
    pnl: float
    held_days: int
    opened: str
    closed: str


def fifo_round_trips(orders) -> tuple[list[RoundTrip], dict[str, dict]]:
    """Match sells against buys FIFO. Returns (closed trips, open lots).

    `orders` are rows/dicts with ticker, side, quantity, price, executed_at,
    in chronological order. Open lots come back as
    {ticker: {"quantity": q, "cost": total_cost}} for cost-basis display.
    Sells without a matching buy lot (transferred-in shares) are skipped
    rather than guessed at.
    """
    lots: dict[str, list[dict]] = {}
    trips: list[RoundTrip] = []
    for o in orders:
        ticker, side = o["ticker"], o["side"]
        qty, price = float(o["quantity"]), float(o["price"])
        if qty <= 0 or price <= 0:
            continue
        if side == "buy":
            lots.setdefault(ticker, []).append(
                {"quantity": qty, "price": price, "at": o["executed_at"]}
            )
            continue
        if side != "sell":
            continue
        remaining = qty
        queue = lots.get(ticker, [])
        while remaining > 1e-9 and queue:
            lot = queue[0]
            take = min(remaining, lot["quantity"])
            opened, closed = _parse_ts(lot["at"]), _parse_ts(o["executed_at"])
            held = (closed - opened).days if opened and closed else 0
            trips.append(
                RoundTrip(
                    ticker=ticker,
                    quantity=take,
                    buy_price=lot["price"],
                    sell_price=price,
                    pnl=take * (price - lot["price"]),
                    held_days=max(held, 0),
                    opened=lot["at"][:10],
                    closed=o["executed_at"][:10],
                )
            )
            lot["quantity"] -= take
            remaining -= take
            if lot["quantity"] <= 1e-9:
                queue.pop(0)

    open_lots = {
        ticker: {
            "quantity": sum(l["quantity"] for l in queue),
            "cost": sum(l["quantity"] * l["price"] for l in queue),
        }
        for ticker, queue in lots.items()
        if sum(l["quantity"] for l in queue) > 1e-9
    }
    return trips, open_lots


def trading_insights(orders, dividends) -> dict:
    """The headline numbers about your own trading."""
    trips, open_lots = fifo_round_trips(orders)
    wins = [t for t in trips if t.pnl > 0]
    total_pnl = sum(t.pnl for t in trips)
    div_total = sum(float(d["amount"]) for d in dividends)
    per_ticker: dict[str, float] = {}
    for t in trips:
        per_ticker[t.ticker] = per_ticker.get(t.ticker, 0.0) + t.pnl
    ranked = sorted(per_ticker.items(), key=lambda kv: kv[1])
    return {
        "round_trips": len(trips),
        "realized_pnl": round(total_pnl, 2),
        "win_rate": len(wins) / len(trips) if trips else None,
        "avg_held_days": sum(t.held_days for t in trips) / len(trips) if trips else None,
        "dividends_total": round(div_total, 2),
        "best_name": ranked[-1] if ranked else None,   # (ticker, pnl)
        "worst_name": ranked[0] if ranked else None,
        "open_positions": len(open_lots),
        "open_cost_basis": round(sum(v["cost"] for v in open_lots.values()), 2),
        "trips": trips,
    }


def model_alignment(orders, suggestion_rows) -> dict:
    """Did your trades agree with what the model was saying at the time?

    For each of your orders, find the model's most recent call on that name
    within the alignment window. Agreement = you bought on a buyish call or
    sold on a sellish one. Trades with no standing opinion are 'uncovered'.
    """
    calls: dict[str, list[tuple[datetime, str]]] = {}
    for row in suggestion_rows:
        ts = _parse_ts(row["created_at"])
        if ts is not None:
            calls.setdefault(row["ticker"], []).append((ts, row["action"]))
    for series in calls.values():
        series.sort()

    agreed = disagreed = uncovered = 0
    disagreements: list[dict] = []
    window = timedelta(days=ALIGNMENT_WINDOW_DAYS)
    for o in orders:
        ts = _parse_ts(o["executed_at"])
        if ts is None or o["side"] not in ("buy", "sell"):
            continue
        opinion = None
        for call_ts, action in reversed(calls.get(o["ticker"], [])):
            if call_ts <= ts and ts - call_ts <= window:
                opinion = action
                break
        if opinion is None:
            uncovered += 1
            continue
        buyish, sellish = opinion in BUYISH, opinion in SELLISH
        if (o["side"] == "buy" and buyish) or (o["side"] == "sell" and sellish):
            agreed += 1
        elif (o["side"] == "buy" and sellish) or (o["side"] == "sell" and buyish):
            disagreed += 1
            disagreements.append(
                {
                    "date": o["executed_at"][:10],
                    "ticker": o["ticker"],
                    "you": o["side"],
                    "model": opinion,
                }
            )
        else:  # model said HOLD
            uncovered += 1
    covered = agreed + disagreed
    return {
        "agreed": agreed,
        "disagreed": disagreed,
        "uncovered": uncovered,
        "agreement_rate": agreed / covered if covered else None,
        "disagreements": disagreements[-10:],
    }
