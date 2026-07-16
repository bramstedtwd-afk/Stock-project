"""Routine-facing research API: the brain as a day-trading analyst's aide.

Built for an agentic trading routine that runs every ~90 minutes:

  brief     one research packet per run — market mood, verdicts on the
            tickers the routine cares about, ranked buy candidates under a
            price cap with ATR-based stop/target at 2:1, and for every name
            the brain's memory: the model's own graded record on it, recent
            shocks with tagged reasons, and event-proneness. `--json` emits
            a machine-readable packet for the agent to reason over.

  log-call  record the routine's decision (acted OR passed) into the brain.
            Logged calls are graded like any suggestion once the horizon
            elapses, so the routine's own judgment builds per-ticker
            reliability, trains the weights, and shows up in the profit
            ledger. This closes the loop: every run is smarter because
            previous runs were graded.

If market data is unreachable, entries degrade to brain-known context
(record, shocks) with data="unavailable" instead of failing — the routine
combines them with its own live quotes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .engine import Engine
from .indicators import compute_features
from .learning import weighted_score
from .profit import paper_trades, profit_stats

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")

STOP_ATR_MULT = 2.0     # stop distance: 2x daily ATR
REWARD_TO_RISK = 2.0    # target distance: 2x the stop distance

SIGNAL_GLOSS = {
    "trend_long": "long-term trend vs its ~200-day average",
    "trend_medium": "medium-term trend vs its 50-day average",
    "momentum_20d": "strength of the past month's move",
    "macd": "short-term momentum turning up or down",
    "rsi_reversion": "how stretched it is vs recent prices (snap-back potential)",
    "bollinger_reversion": "position within its recent trading band",
    "volume_confirmation": "whether volume is backing the latest move",
    "range_position": "where it sits in its 52-week range",
}


def _congress_watch(summary: dict, top: int = 8) -> list[dict]:
    """Top tickers Congress is buying lately — brief-level context."""
    from .congress import notable_buys

    return [
        {
            "ticker": tk,
            "members": summary[tk]["members"],
            "net_buys": summary[tk]["net_buys"],
            "last_date": summary[tk]["last_date"],
        }
        for tk in notable_buys(summary, top=top)
    ]


def _congress_note(summary: dict, ticker: str) -> dict | None:
    info = summary.get(ticker)
    if not info or info.get("net_buys", 0) <= 0:
        return None
    return {
        "members": info["members"],
        "net_buys": info["net_buys"],
        "last_date": info["last_date"],
        "est_amount": info["est_amount"],
    }


def _entry_from_suggestion(s, engine: Engine, congress: dict | None = None) -> dict:
    price = s.price
    atr_pct = s.risk.get("atr_pct")
    stop = target = None
    if atr_pct and price and price > 0:
        stop_dist = STOP_ATR_MULT * atr_pct
        stop = round(price * (1 - stop_dist), 2)
        target = round(price * (1 + REWARD_TO_RISK * stop_dist), 2)
    ctx = engine._past_context(s.ticker)
    top_signals = sorted(s.signals.items(), key=lambda kv: -abs(kv[1]))[:3]
    return {
        "ticker": s.ticker,
        "verdict": s.action,
        "score": s.risk_adjusted_score,
        "price": round(price, 2) if price else None,
        "sector": s.sector,
        "stop": stop,
        "target": target,
        "reward_to_risk": f"{REWARD_TO_RISK:.0f}:1" if stop else None,
        "atr_pct": round(atr_pct, 4) if atr_pct else None,
        "annualized_vol": round(s.risk.get("annualized_vol", float("nan")), 3)
        if s.risk.get("annualized_vol") is not None
        else None,
        "model_record": {
            "graded_calls": ctx.graded_calls,
            "hit_rate": round(ctx.hit_rate, 3) if ctx.hit_rate is not None else None,
        },
        "recent_shock": ctx.recent_event,
        "events_12mo": ctx.events_12mo,
        "earnings_days": s.earnings_days,
        "earnings_blackout": (
            s.earnings_days is not None and 0 <= s.earnings_days <= 3
        ),
        "congress_buying": _congress_note(congress or {}, s.ticker),
        "size_hint_pct": round(s.position_fraction * 100, 1),
        "top_signals": [
            {"name": n, "value": round(v, 3), "meaning": SIGNAL_GLOSS.get(n, n)}
            for n, v in top_signals
        ],
        "notes": s.notes,
    }


def _degraded_entry(ticker: str, engine: Engine) -> dict:
    ctx = engine._past_context(ticker)
    return {
        "ticker": ticker,
        "data": "unavailable",
        "verdict": None,
        "model_record": {
            "graded_calls": ctx.graded_calls,
            "hit_rate": round(ctx.hit_rate, 3) if ctx.hit_rate is not None else None,
        },
        "recent_shock": ctx.recent_event,
        "events_12mo": ctx.events_12mo,
        "notes": ["Price data unreachable — brain context only; use live quotes."],
    }


def build_brief(
    engine: Engine,
    tickers: list[str] | None = None,
    max_price: float | None = None,
    top: int = 5,
    congress_summary: dict | None = None,
) -> dict:
    """One research packet: verdicts on `tickers`, plus ranked candidates."""
    # Absorb any repo-carried brain snapshot first (idempotent; merges only
    # add), so a routine running in a fresh environment starts with the
    # accumulated knowledge before grading and scanning.
    from .brain import absorb_snapshot

    snapshot = None
    if str(engine.db.path) != ":memory:":
        try:
            snapshot = absorb_snapshot(db_path=engine.db.path)
        except Exception:  # a bad snapshot file must never block a trading run
            snapshot = {"error": "snapshot unreadable — continuing on local brain"}
    graded_now = engine.evaluate_pending()  # constant grading, every touchpoint

    # Congressional-buying context (light tilt, never a mechanical signal).
    if congress_summary is None:
        try:
            from .congress import CongressData

            congress_summary = CongressData().summary()
        except Exception:
            congress_summary = {}

    result = engine.scan(capture_context=False, record=False)
    by_ticker = {s.ticker: s for s in result.suggestions}

    requested = [t.strip().upper() for t in (tickers or []) if t.strip()]
    missing = [t for t in requested if t not in by_ticker]
    if missing:
        extra = engine.scan(tickers=missing, capture_context=False, record=False)
        by_ticker.update({s.ticker: s for s in extra.suggestions})

    focus = []
    for t in requested:
        s = by_ticker.get(t)
        focus.append(
            _entry_from_suggestion(s, engine, congress_summary)
            if s
            else _degraded_entry(t, engine)
        )

    candidates = [
        s
        for s in result.suggestions
        if s.action in BUYISH
        and s.position_fraction > 0  # excludes earnings-blackout / zero-size names
        and (max_price is None or (s.price and s.price <= max_price))
    ]
    avoid = [s for s in result.suggestions if s.action in SELLISH][:top]

    summary = engine.db.performance_summary()
    buys, avoided = paper_trades(engine.db.evaluated_suggestions())
    ledger = profit_stats(buys, avoided)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_absorbed": snapshot,
        "graded_this_call": graded_now,
        "market_mood": engine.sector_trends(),
        "congress_watch": _congress_watch(congress_summary),
        "focus": focus,
        "candidates": [
            _entry_from_suggestion(s, engine, congress_summary)
            for s in candidates[:top]
        ],
        "avoid": [
            {"ticker": s.ticker, "verdict": s.action, "score": s.risk_adjusted_score}
            for s in avoid
        ],
        "model_stats": {
            "graded_calls": summary["evaluated"],
            "hit_rate": round(summary["hit_rate"], 3)
            if summary["hit_rate"] is not None
            else None,
            "paper_profit_factor": round(ledger["profit_factor"], 2)
            if ledger["profit_factor"] is not None
            else None,
            "paper_pnl": ledger["total_pnl"],
        },
        "scan_errors": len(result.errors),
    }


def ingest_fills(engine: Engine, orders: list[dict], horizon_days: int = 5) -> dict:
    """Turn actual executed orders into graded, weight-training brain calls.

    This is how the routine's real behavior compounds into memory: every
    filled buy/sell (from the Robinhood mirror) becomes a recorded call,
    backdated to when it actually executed, with the technical signals as
    they stood that day (no look-ahead). The normal grading loop then scores
    it against what price did next — so per-ticker reliability, the learned
    weights, and the profit ledger all learn from what you truly did, not
    from self-reported intentions. Idempotent: each order is ingested once.
    """
    import pandas as pd

    recorded = 0
    for o in orders:
        order_id = str(o.get("order_id") or "")
        if not order_id or engine.db.fill_ingested(order_id):
            continue
        side = (o.get("side") or "").lower()
        action = "BUY" if side == "buy" else "SELL" if side == "sell" else None
        price = float(o.get("price") or 0)
        ticker = (o.get("ticker") or "").upper()
        executed_at = str(o.get("executed_at") or "").replace("Z", "+00:00")
        if action is None or price <= 0 or not ticker or not executed_at:
            continue

        # Signals as they stood on the fill date (history sliced, no peeking).
        signals: dict[str, float] = {}
        df = engine.market.history(ticker)
        if df is not None and not df.empty:
            try:
                as_of = pd.Timestamp(executed_at).tz_localize(None).normalize()
                sliced = df.loc[:as_of]
                feats = compute_features(sliced) if len(sliced) else None
                if feats:
                    signals = feats
            except (ValueError, TypeError, KeyError):
                signals = {}

        direction = 1.0 if action == "BUY" else -1.0
        sid = engine.db.record_suggestion(
            ticker, action, round(direction * 0.35, 4), price, signals,
            horizon_days, created_at=executed_at,
        )
        engine.db.mark_fill_ingested(order_id, sid)
        recorded += 1
    return {"fills_ingested": recorded}


def desktop_sync_cycle(engine: Engine, client=None) -> dict:
    """The full desktop-side heartbeat that keeps the brain current.

    Pull the live Robinhood order/dividend history, turn any new fills into
    graded calls, then grade everything matured. Run this before every
    publish (and it's safe to run anytime) so the brain captures every real
    trade automatically — "grading every single run". Robinhood being absent
    degrades to just grading what's already recorded.
    """
    from .robinhood import RobinhoodClient

    client = client or RobinhoodClient()
    stats = {"synced": None, "fills_ingested": 0, "graded": 0, "holdings": []}
    try:
        sync = client.sync_history(engine.db)
        if sync is not None:
            stats["synced"] = sync
            orders = [dict(r) for r in engine.db.rh_orders()]
            stats["fills_ingested"] = ingest_fills(engine, orders)["fills_ingested"]
        portfolio = client.portfolio()
        if portfolio is not None:
            stats["holdings"] = [h.ticker for h in portfolio.holdings]
    except Exception as exc:  # never let a broker hiccup block grading/publish
        stats["sync_error"] = str(exc)
    stats["graded"] = engine.evaluate_pending()
    return stats


def publish_brief(
    engine: Engine,
    folder: str,
    tickers: list[str] | None = None,
    max_price: float | None = None,
    top: int = 8,
    include_playbook: bool = True,
    include_snapshot: bool = True,
) -> dict:
    """Write a fresh research packet into a folder for the routine to read.

    Point `folder` at a directory that Google Drive for Desktop (or any
    cloud client) syncs. The routine reads the freshest "StockSage Brief"
    file from Drive — no Python needed on its side. Idempotent per day: the
    dated filename means one file per day, overwritten on re-publish.
    """
    import json
    import shutil
    from datetime import date
    from pathlib import Path as _Path

    from .brain import ROUTINE_PATH, write_snapshot

    out_dir = _Path(folder).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    focus = tickers if tickers is not None else engine.db.watchlist()
    packet = build_brief(engine, tickers=focus, max_price=max_price, top=top)

    brief_path = out_dir / f"StockSage Brief - {date.today().isoformat()}.json"
    brief_path.write_text(json.dumps(packet, indent=2))
    written = [str(brief_path)]

    if include_playbook and ROUTINE_PATH.exists():
        dest = out_dir / "StockSage Routine Playbook.md"
        shutil.copyfile(ROUTINE_PATH, dest)
        written.append(str(dest))
    if include_snapshot:
        snap = write_snapshot(db_path=engine.db.path)
        dest = out_dir / "brain-snapshot.db"
        shutil.copyfile(snap, dest)
        written.append(str(dest))

    return {"folder": str(out_dir), "written": written, "candidates": len(packet["candidates"])}


def log_call(
    engine: Engine,
    ticker: str,
    action: str,
    price: float | None = None,
    note: str | None = None,
    horizon_days: int = 5,
) -> int:
    """Record a routine decision into the brain so it gets graded.

    Direction for grading comes from the action; signals (when data is
    reachable) let the weight-learning loop learn from this call too.
    Returns the suggestion id.
    """
    ticker = ticker.strip().upper()
    action = action.strip().upper()
    if action not in BUYISH + SELLISH:
        raise ValueError(f"action must be one of {BUYISH + SELLISH}, got {action!r}")

    df = engine.market.history(ticker)
    feats = compute_features(df) if df is not None else None
    if price is None:
        if df is None or df.empty:
            raise ValueError(
                f"no market data for {ticker} — pass the executed/quoted price explicitly"
            )
        price = float(df["Close"].iloc[-1])

    direction = 1.0 if action in BUYISH else -1.0
    magnitude = 0.5 if action.startswith("STRONG") else 0.3
    score = direction * magnitude
    if feats:
        model_view = weighted_score(feats, engine.weights_for(ticker, engine.db.load_weights()))
        # Keep the routine's direction; nudge magnitude by model agreement.
        score = direction * max(0.2, min(0.6, magnitude + 0.2 * direction * model_view))

    sid = engine.db.record_suggestion(
        ticker, action, round(score, 4), price, feats or {}, horizon_days
    )
    if note:
        engine.db.log_learning(sid, {"routine_note": note, "logged_at_price": price})
    return sid
