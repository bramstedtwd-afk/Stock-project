"""The morning briefing: everything synthesized into 15 seconds of reading.

A good advisor doesn't hand you six tables — they open with "here's where
things stand and what deserves your attention." build_briefing distills a
completed scan plus the brain into exactly that: market mood, the top
ideas, alerts on names you own, what just got graded, the week's paper
P&L, and any fresh shocks. The dashboard renders it as the first thing
you see; the daily CLI prints it first.
"""

from __future__ import annotations

from datetime import date, timedelta

from .profit import paper_trades, profit_stats

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")


def build_briefing(result, db) -> dict:
    """Distill a ScanResult + brain into briefing fields (all precomputed)."""
    trends = result.sector_trends or {}
    rising = sum(1 for v in trends.values() if v > 0.05)
    falling = sum(1 for v in trends.values() if v < -0.05)
    avg = sum(trends.values()) / len(trends) if trends else 0.0
    if trends and avg > 0.10 and rising >= falling:
        mood = "bullish"
    elif trends and avg < -0.10 and falling >= rising:
        mood = "bearish"
    else:
        mood = "mixed"

    top_ideas = [s for s in result.suggestions if s.action in BUYISH][:3]
    owned_alerts = [
        s for s in result.suggestions if s.action in SELLISH and s.owned_shares > 0
    ]

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    buys, avoided = paper_trades(db.evaluated_suggestions())
    week_pnl = sum(t.pnl for t in buys if t.when >= week_ago)
    graded_week = sum(1 for t in buys if t.when >= week_ago)
    edge = profit_stats(buys, avoided).get("edge_vs_market")

    buying_power = getattr(result.portfolio, "buying_power", None)

    shocks = [
        {
            "ticker": ev["ticker"],
            "return_pct": ev["return_pct"],
            "reasons": ev.get("reasons") or ["unexplained"],
        }
        for ev in (result.move_events or [])
    ]

    return {
        "mood": mood,
        "sectors_rising": rising,
        "sectors_falling": falling,
        "top_ideas": [
            {
                "ticker": s.ticker,
                "action": s.action,
                "score": s.risk_adjusted_score,
                "size": s.position_fraction,
                "dollars": round(s.position_fraction * buying_power, 2)
                if buying_power and s.position_fraction
                else None,
                "why": s.why,
            }
            for s in top_ideas
        ],
        "buying_power": buying_power,
        "edge_vs_market": edge,
        "owned_alerts": [
            {"ticker": s.ticker, "action": s.action, "score": s.risk_adjusted_score}
            for s in owned_alerts
        ],
        "graded_this_cycle": result.evaluated_count,
        "week_paper_pnl": round(week_pnl, 2),
        "week_graded": graded_week,
        "shocks": shocks,
        "portfolio_linked": result.portfolio is not None,
        "scanned": len(result.suggestions),
    }


_MOOD_LABEL = {
    "bullish": "Market mood: bullish",
    "bearish": "Market mood: bearish — smaller sizes, more patience",
    "mixed": "Market mood: mixed",
}


def briefing_lines(b: dict) -> list[str]:
    """The briefing as plain sentences (CLI and dashboard share these)."""
    lines = [
        f"{_MOOD_LABEL[b['mood']]} ({b['sectors_rising']} sectors rising, "
        f"{b['sectors_falling']} falling; {b['scanned']} names scanned)."
    ]
    if b["top_ideas"]:
        parts = []
        for i in b["top_ideas"]:
            tag = f"{i['ticker']} ({i['action']}, {i['score']:+.2f}"
            if i.get("dollars"):
                tag += f", ≈${i['dollars']:,.0f}"
            parts.append(tag + ")")
        lines.append(f"Top ideas today: {', '.join(parts)}.")
    else:
        lines.append("No buy-side ideas clear the bar today — cash is a position too.")
    if b["owned_alerts"]:
        alerts = ", ".join(f"{a['ticker']} ({a['action']})" for a in b["owned_alerts"])
        lines.append(f"⚠ Action needed on names you own: {alerts}.")
    elif b["portfolio_linked"]:
        lines.append("Your holdings: no sell signals today.")
    if b["graded_this_cycle"]:
        lines.append(
            f"Graded {b['graded_this_cycle']} matured calls this cycle — "
            "the model just updated its weights."
        )
    if b["week_graded"]:
        lines.append(
            f"Paper ledger this week: ${b['week_paper_pnl']:+,.2f} "
            f"across {b['week_graded']} graded trades."
        )
    if b.get("edge_vs_market") is not None:
        verdict = "ahead of" if b["edge_vs_market"] >= 0 else "behind"
        lines.append(
            f"All-time, the model's calls are ${abs(b['edge_vs_market']):,.2f} "
            f"{verdict} just parking the same money in SPY."
        )
    for shock in b["shocks"][:3]:
        reasons = ", ".join(shock["reasons"])
        lines.append(
            f"Big move: {shock['ticker']} {shock['return_pct']:+.1%} [{reasons}]."
        )
    return lines
