"""The profitability ledger: what following StockSage would actually earn.

Every graded suggestion becomes a paper trade with a fixed stake (default
$1,000 per idea, STOCKSAGE_STAKE to change): buy-side calls earn the
realized 5-day return on the stake; sell/avoid calls are scored separately
as risk avoided (money NOT lost by being out). Fixed-stake scoring is
deliberate — it measures the quality of the calls themselves, uncontaminated
by compounding or sizing luck, which is the number you need when deciding
how much real capital to trust the tool with.

Everything here is derived from the graded record in the brain, so the
ledger travels with the brain and two merged devices agree on it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

BUY_ACTIONS = ("BUY", "STRONG BUY")
SELL_ACTIONS = ("SELL", "STRONG SELL")


def default_stake() -> float:
    try:
        return float(os.environ.get("STOCKSAGE_STAKE", "1000"))
    except ValueError:
        return 1000.0


@dataclass
class PaperTrade:
    when: str            # date the call was made
    ticker: str
    action: str
    stake: float
    realized_return: float
    pnl: float           # buys: stake * return; sells: stake * -return (avoided)
    cumulative: float    # running buy-side P&L (buys only)


def paper_trades(
    evaluated_rows, stake: float | None = None
) -> tuple[list[PaperTrade], list[PaperTrade]]:
    """Split the graded record into (buy trades, sell/avoided calls).

    Rows must be chronological and carry: created_at, ticker, action,
    realized_return. Cumulative P&L is tracked on the buy side only —
    that is the money line; avoided risk is reported separately.
    """
    stake = default_stake() if stake is None else stake
    buys: list[PaperTrade] = []
    avoided: list[PaperTrade] = []
    cumulative = 0.0
    for row in evaluated_rows:
        ret = row["realized_return"]
        if ret is None:
            continue
        action = row["action"]
        when = row["created_at"][:10]
        if action in BUY_ACTIONS:
            pnl = stake * ret
            cumulative += pnl
            buys.append(
                PaperTrade(when, row["ticker"], action, stake, ret, pnl, cumulative)
            )
        elif action in SELL_ACTIONS:
            avoided.append(
                PaperTrade(when, row["ticker"], action, stake, ret, stake * -ret, 0.0)
            )
    return buys, avoided


def profit_stats(buys: list[PaperTrade], avoided: list[PaperTrade]) -> dict:
    wins = [t.pnl for t in buys if t.pnl > 0]
    losses = [t.pnl for t in buys if t.pnl <= 0]
    total = sum(t.pnl for t in buys)
    gross_loss = abs(sum(losses))
    stats = {
        "trades": len(buys),
        "total_pnl": round(total, 2),
        "win_rate": len(wins) / len(buys) if buys else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "best": max(buys, key=lambda t: t.pnl) if buys else None,
        "worst": min(buys, key=lambda t: t.pnl) if buys else None,
        "risk_avoided": round(sum(t.pnl for t in avoided), 2),
        "avoid_calls": len(avoided),
    }
    if buys:
        stats["return_per_trade"] = total / (len(buys) * buys[0].stake)
    return stats
