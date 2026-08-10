"""Grading-from-fills: real trades become graded, weight-training calls."""

from datetime import datetime, timedelta, timezone


from stocksage.advisor import desktop_sync_cycle, ingest_fills
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


def _engine():
    frames = {
        "AAPL": make_ohlcv(days=400, start_price=180.0, daily_drift=0.003, seed=81),
        "F": make_ohlcv(days=400, start_price=14.0, daily_drift=-0.004, seed=82),
    }
    return Engine(db=Database(":memory:"), market=FakeMarket(frames))


def _order(oid, ticker, side, price, days_ago):
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"order_id": oid, "ticker": ticker, "side": side, "quantity": 1,
            "price": price, "executed_at": when}


def test_fills_become_recorded_calls_with_signals():
    engine = _engine()
    orders = [_order("o1", "AAPL", "buy", 180.0, 20)]
    assert ingest_fills(engine, orders)["fills_ingested"] == 1
    rows = engine.db.recent_suggestions()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL" and rows[0]["action"] == "BUY"
    assert rows[0]["score"] > 0
    # Signals captured as of the fill date (no look-ahead) -> weights can learn.
    import json
    assert json.loads(rows[0]["signals"]) != {}


def test_ingest_is_idempotent():
    engine = _engine()
    orders = [_order("o1", "AAPL", "buy", 180.0, 20)]
    assert ingest_fills(engine, orders)["fills_ingested"] == 1
    assert ingest_fills(engine, orders)["fills_ingested"] == 0  # same order id
    assert len(engine.db.recent_suggestions()) == 1


def test_backdated_fill_grades_immediately():
    """A fill 20 days old is already matured, so grading scores it now."""
    engine = _engine()
    ingest_fills(engine, [_order("o1", "AAPL", "buy", 100.0, 20)])  # bought cheap
    graded = engine.evaluate_pending()
    assert graded == 1
    row = [r for r in engine.db.recent_suggestions() if r["evaluated"]][0]
    # AAPL uptrend now well above 100 -> a buy call is a hit.
    assert row["hit"] == 1
    # The fill lands under the OWNER's record, not the model's: the model's
    # per-ticker reliability answers "does the model read this name well",
    # and blending the owner's trades into it makes it answer neither.
    from stocksage.db import SOURCE_OWNER

    n, hit_rate, _ = engine.db.ticker_track_record("AAPL", source=SOURCE_OWNER)
    assert n == 1 and hit_rate == 1.0
    assert engine.db.ticker_track_record("AAPL")[0] == 0


def test_recent_fill_not_yet_matured_waits():
    engine = _engine()
    ingest_fills(engine, [_order("o1", "AAPL", "buy", 180.0, 1)])  # yesterday
    assert engine.evaluate_pending() == 0  # horizon not elapsed


def test_malformed_orders_skipped():
    engine = _engine()
    orders = [
        {"order_id": "", "ticker": "AAPL", "side": "buy", "price": 1, "executed_at": "x"},
        {"order_id": "o2", "ticker": "AAPL", "side": "hold", "price": 1,
         "executed_at": "2026-06-01T00:00:00Z"},  # not buy/sell
        {"order_id": "o3", "ticker": "F", "side": "sell", "price": 0,
         "executed_at": "2026-06-01T00:00:00Z"},  # bad price
    ]
    assert ingest_fills(engine, orders)["fills_ingested"] == 0


def test_desktop_sync_cycle_end_to_end():
    engine = _engine()

    class StubClient:
        def sync_history(self, db):
            db.upsert_rh_orders([_order("o1", "AAPL", "buy", 100.0, 20)])
            return {"orders_total": 1, "orders_added": 1, "dividends_added": 0}

    stats = desktop_sync_cycle(engine, client=StubClient())
    assert stats["fills_ingested"] == 1
    assert stats["graded"] == 1  # backdated fill matured and got graded
    # Second run: nothing new, nothing double-counted.
    stats2 = desktop_sync_cycle(engine, client=StubClient())
    assert stats2["fills_ingested"] == 0


def test_sync_cycle_survives_broker_error():
    engine = _engine()

    class BrokenClient:
        def sync_history(self, db):
            raise RuntimeError("robinhood down")

    stats = desktop_sync_cycle(engine, client=BrokenClient())
    assert "sync_error" in stats
    assert stats["graded"] == 0  # still ran grading, just nothing pending
