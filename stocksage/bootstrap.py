"""First-run bootstrap: arrive with knowledge instead of a blank slate.

Two jobs, both driven from two years of history for the whole universe:

1. **Warm up the signal weights.** Walk forward through history in 5-day
   steps: at each point, compute the signals exactly as a live scan would
   have seen them, observe the realized 5-day return that followed, and
   apply the same Hedge update the live learning loop uses. Thousands of
   honest walk-forward samples (no look-ahead: features at time t only see
   data through t) mean day-one suggestions already reflect which signals
   have actually been working — instead of waiting weeks for live grades.
   A gentler learning rate than live keeps any single historical sample
   from dominating what live experience will later teach.

2. **Backfill the move memory.** Every historically significant move
   (same thresholds as live detection) is recorded, so "why it moved" has
   each stock's event history — dates, magnitudes, frequency — from the
   start. Headlines aren't available retroactively, so backfilled events
   are tagged `historical-backfill`; the most recent week is left to the
   live capture path, which does attach news.

Bootstrap runs automatically on the first daily cycle (the `bootstrap_done`
meta flag makes it once-only) or explicitly via `stocksage bootstrap`,
which re-trains on the freshest history on top of the current weights.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from . import universe
from .context import detect_all_significant_moves
from .data import MarketData
from .db import Database
from .indicators import MIN_HISTORY_ROWS, compute_features
from .learning import update_weights

log = logging.getLogger(__name__)

WARMUP_PERIOD = "2y"
WARMUP_STEP_DAYS = 5      # sample the walk-forward every 5 trading days
WARMUP_HORIZON_DAYS = 5   # same horizon the live loop grades against
WARMUP_ETA = 0.05         # gentler than live so history informs, not dictates
BACKFILL_SKIP_RECENT_DAYS = 7  # leave fresh moves to live capture (has news)


def warmup_learning(
    market: MarketData, db: Database, tickers: list[str] | None = None
) -> dict:
    """Walk-forward train the weights on history. Returns stats."""
    weights = db.load_weights()
    samples = 0
    skipped = 0
    started = time.time()
    for ticker in tickers or universe.all_tickers():
        df = market.history(ticker, period=WARMUP_PERIOD)
        if df is None or len(df) < MIN_HISTORY_ROWS + WARMUP_HORIZON_DAYS:
            skipped += 1
            continue
        close = df["Close"]
        for end in range(MIN_HISTORY_ROWS, len(df) - WARMUP_HORIZON_DAYS, WARMUP_STEP_DAYS):
            feats = compute_features(df.iloc[:end])
            if not feats:
                continue
            entry = float(close.iloc[end - 1])
            exit_ = float(close.iloc[end - 1 + WARMUP_HORIZON_DAYS])
            if entry <= 0:
                continue
            weights, _ = update_weights(weights, feats, exit_ / entry - 1.0, eta=WARMUP_ETA)
            samples += 1
    if samples:
        db.save_weights(weights)
    prior = int(db.get_meta("warmup_samples", "0"))
    db.set_meta("warmup_samples", str(prior + samples))
    stats = {
        "warmup_samples": samples,
        "tickers_skipped": skipped,
        "seconds": round(time.time() - started, 1),
        "weights": {k: round(v, 4) for k, v in weights.items()},
    }
    log.info("warmup: %d samples in %.1fs (%d tickers skipped)", samples, stats["seconds"], skipped)
    return stats


def backfill_move_history(
    market: MarketData, db: Database, tickers: list[str] | None = None
) -> int:
    """Record all historically significant moves. Returns events added."""
    cutoff = (date.today() - timedelta(days=BACKFILL_SKIP_RECENT_DAYS)).isoformat()
    count = 0
    for ticker in tickers or universe.all_tickers():
        df = market.history(ticker, period=WARMUP_PERIOD)
        for event_date, ret, atr_mult in detect_all_significant_moves(df):
            if event_date >= cutoff:
                continue  # live capture handles fresh moves and attaches news
            db.record_move_event(ticker, event_date, ret, atr_mult, ["historical-backfill"], [])
            count += 1
    log.info("backfill: %d historical move events recorded", count)
    return count


def bootstrap(
    market: MarketData, db: Database, tickers: list[str] | None = None
) -> dict:
    """Full first-run bootstrap: warm-up + backfill. Sets the done flag."""
    stats = warmup_learning(market, db, tickers)
    stats["move_events_backfilled"] = backfill_move_history(market, db, tickers)
    db.set_meta("bootstrap_done", date.today().isoformat())
    return stats
