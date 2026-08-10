"""The published brief: one JSON file the routine agent reads each run.

This is the contract between StockSage (the brain, which learns) and the
routine agent (the hands, which watch positions and ask the owner
questions). Keeping the schema here — rather than hand-rolled in a desktop
script — means it is versioned, reviewable, and covered by offline tests,
so a change to what the brain knows can't silently break what the agent
reads.

Two fields exist specifically so the owner is never misled:

  `headline`  the one sentence worth reading — the model's edge over simply
              parking the same money in SPY. Raw P&L flatters itself in a
              rising market; this is the number that decides whether the
              tool earns its keep at all.

  `health`    what was actually working when this brief was built. A brief
              produced with the earnings calendar down or the broker
              unlinked still *looks* authoritative, which is the dangerous
              case: the agent is told plainly, in `health.degraded`, so it
              can lead with the problem instead of the suggestions.

Schema note: fields are additive. The agent reads specific keys, so
existing ones keep their names and meanings; new information arrives as
new keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import universe
from .profit import paper_trades, profit_stats

SCHEMA_VERSION = 2

# How many names reach the brief. The agent ranks within these.
CANDIDATE_LIMIT = 6
AVOID_LIMIT = 5

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")

# Reward:risk used for the advisory target — a 2:1 measured move off the stop.
REWARD_RISK_MULTIPLE = 2.0

# Plain-language gloss per signal, so the agent (and the owner) never has to
# guess what a raw signal name means.
SIGNAL_MEANINGS: dict[str, str] = {
    "trend_long": "price versus its long-term average",
    "trend_medium": "price versus its medium-term average",
    "momentum_20d": "strength of the past month's move",
    "macd": "short-term momentum turning up or down",
    "rsi_reversion": "how stretched it is vs recent prices (snap-back potential)",
    "bollinger_reversion": "position within its recent trading band",
    "volume_confirmation": "whether volume is backing the latest move",
    "range_position": "where it sits in its 52-week range",
    "relative_strength_20d": "how it is doing versus its own sector",
}

TOP_SIGNAL_COUNT = 3


def _round(value, digits: int = 4):
    return round(value, digits) if isinstance(value, (int, float)) else None


def _target_price(price: float, stop: float | None) -> float | None:
    """Advisory target: a 2:1 measured move measured off the protective stop."""
    if stop is None or not price or price <= stop:
        return None
    return round(price + REWARD_RISK_MULTIPLE * (price - stop), 2)


def _top_signals(signals: dict[str, float]) -> list[dict]:
    ranked = sorted(signals.items(), key=lambda kv: -abs(kv[1]))[:TOP_SIGNAL_COUNT]
    return [
        {
            "name": name,
            "value": round(value, 3),
            "meaning": SIGNAL_MEANINGS.get(name, "learned signal"),
        }
        for name, value in ranked
    ]


def _candidate(suggestion, engine, buying_power: float | None) -> dict:
    """One buy-side idea, with everything the agent needs to judge it."""
    db = engine.db
    graded, hit_rate, _ = db.ticker_track_record(suggestion.ticker)
    past = engine._past_context(
        suggestion.ticker, engine._days_to_earnings(suggestion.ticker)
    )
    days = past.days_to_earnings
    from .scoring import EARNINGS_BLACKOUT_DAYS

    blackout = days is not None and 0 <= days <= EARNINGS_BLACKOUT_DAYS

    stop = suggestion.stop_price
    dollars = (
        round(suggestion.position_fraction * buying_power, 2)
        if buying_power and suggestion.position_fraction
        else None
    )
    return {
        "ticker": suggestion.ticker,
        "verdict": suggestion.action,
        # Blacked-out names stay visible (the read is still information) but
        # are explicitly not actionable, so the agent can't propose into a print.
        "actionable": suggestion.action in BUYISH and not blackout,
        "score": _round(suggestion.risk_adjusted_score),
        "price": suggestion.price,
        "sector": suggestion.sector,
        "stop": stop,
        "target": _target_price(suggestion.price, stop),
        "reward_to_risk": "2:1",
        "atr_pct": _round(suggestion.risk.get("atr_pct")),
        "annualized_vol": _round(suggestion.risk.get("annualized_vol"), 3),
        "model_record": {"graded_calls": graded, "hit_rate": hit_rate},
        "recent_shock": past.recent_event,
        "events_12mo": past.events_12mo,
        "earnings_days": days,
        "earnings_blackout": blackout,
        "congress_buying": None,
        "size_hint_pct": round(suggestion.position_fraction * 100, 1),
        "size_hint_dollars": dollars,
        "est_shares": round(dollars / suggestion.price, 6)
        if dollars and suggestion.price
        else None,
        "top_signals": _top_signals(suggestion.signals),
        "why": suggestion.why,
        "notes": list(suggestion.notes),
    }


def scoreboard(db) -> dict:
    """Model track record, including the only comparison that settles it.

    Raw P&L answers "did it make money"; the market rose too, so that is
    the easy question. `edge_vs_market` answers "did it beat doing nothing",
    which is the one worth acting on.
    """
    buys, avoided = paper_trades(db.evaluated_suggestions())
    stats = profit_stats(buys, avoided)
    summary = db.performance_summary()
    out = {
        "graded_calls": summary["evaluated"],
        "hit_rate": _round(summary["hit_rate"], 3),
        "paper_profit_factor": _round(stats.get("profit_factor"), 2),
        "avg_return_per_call": _round(summary["avg_return"]),
        "total_paper_pnl": stats.get("total_pnl"),
        # Present only once calls have been graded against the benchmark.
        "edge_vs_market": stats.get("edge_vs_market"),
        "benchmark_pnl": stats.get("benchmark_pnl"),
        "covered_trades": stats.get("covered_trades", 0),
    }
    return out


def headline_sentence(stats: dict) -> str:
    """The one sentence to put in front of the owner."""
    edge = stats.get("edge_vs_market")
    covered = stats.get("covered_trades") or 0
    if edge is None or not covered:
        graded = stats.get("graded_calls") or 0
        if not graded:
            return (
                "No calls graded yet — there is nothing to judge this tool on "
                "so far. Check back after a week of daily runs."
            )
        return (
            f"{graded} calls graded, but none yet measured against the market — "
            "the honest scoreboard needs benchmark-graded calls before it can "
            "say whether this beats an index fund."
        )
    verdict = "MORE than" if edge >= 0 else "LESS than"
    return (
        f"Following these calls has earned ${abs(edge):,.2f} {verdict} putting "
        f"the same money in SPY, across {covered} graded "
        f"trade{'s' if covered != 1 else ''}."
    )


def health_report(result, engine, candidates: list[dict]) -> dict:
    """What was actually working when this brief was built.

    A half-broken run still produces confident-looking suggestions. Anything
    false here becomes a plain-English line the agent leads with, so a silent
    degradation can't sit unnoticed for weeks.
    """
    db = engine.db
    bench = engine.market.history(universe.MARKET_BENCHMARK)
    linked = result.portfolio is not None
    earnings_ok = any(c["earnings_days"] is not None for c in candidates)
    benchmark_ok = bench is not None and not bench.empty
    bootstrap_ok = db.get_meta("bootstrap_done") is not None
    scan_errors = len(result.errors)

    checks = {
        "robinhood_linked": linked,
        "earnings_calendar": earnings_ok,
        "benchmark_available": benchmark_ok,
        "bootstrap_complete": bootstrap_ok,
        "scan_errors": scan_errors,
    }
    degraded: list[str] = []
    if not earnings_ok and candidates:
        degraded.append(
            "No earnings dates resolved for any candidate — the earnings "
            "blackout is NOT protecting these suggestions. Verify the "
            "earnings calendar before acting on them."
        )
    if not benchmark_ok:
        degraded.append(
            "SPY history unavailable — new calls cannot be graded against the "
            "market, so the edge-vs-SPY scoreboard will stop updating."
        )
    if not linked:
        degraded.append(
            "Robinhood not linked on this run — dollar sizes and share counts "
            "are unavailable, and held positions were not cross-checked."
        )
    if not bootstrap_ok:
        degraded.append(
            "Brain never bootstrapped — signal weights are still at uniform "
            "defaults and carry no learned information yet."
        )
    if scan_errors:
        degraded.append(
            f"{scan_errors} names failed to scan — the candidate list is "
            "incomplete for this run."
        )
    return {"ok": not degraded, "degraded": degraded, "checks": checks}


def build_brief(
    result,
    engine,
    snapshot_absorbed: dict | None = None,
    graded_this_call: int | None = None,
) -> dict:
    """Assemble the full brief from a completed scan."""
    buying_power = getattr(result.portfolio, "buying_power", None)

    ranked_buys = [s for s in result.suggestions if s.action in BUYISH][:CANDIDATE_LIMIT]
    candidates = [_candidate(s, engine, buying_power) for s in ranked_buys]

    avoid = [
        {
            "ticker": s.ticker,
            "verdict": s.action,
            "score": _round(s.risk_adjusted_score),
            "owned": s.owned_shares > 0,
        }
        for s in sorted(result.suggestions, key=lambda s: s.risk_adjusted_score)
        if s.action in SELLISH
    ][:AVOID_LIMIT]

    stats = scoreboard(engine.db)
    health = health_report(result, engine, candidates)

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "headline": headline_sentence(stats),
        "health": health,
        "snapshot_absorbed": snapshot_absorbed
        or {
            "move_events_added": 0,
            "rh_orders_added": 0,
            "rh_dividends_added": 0,
            "suggestions_added": 0,
            "weights_taken_from": "local",
        },
        "graded_this_call": (
            result.evaluated_count if graded_this_call is None else graded_this_call
        ),
        "market_mood": result.sector_trends or {},
        "congress_watch": [],
        "focus": [s.ticker for s in result.suggestions if s.owned_shares > 0],
        "candidates": candidates,
        "avoid": avoid,
        "model_stats": stats,
        "buying_power": buying_power,
        "scan_errors": len(result.errors),
    }


def write_brief(brief: dict, path: str | Path) -> Path:
    """Write the brief atomically — a half-written file must never be read.

    The agent polls this file; a torn write would either crash its JSON parse
    or, worse, look like a valid older brief.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(brief, indent=1))
    tmp.replace(path)
    return path
