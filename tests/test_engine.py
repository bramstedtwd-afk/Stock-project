"""End-to-end engine tests with an injected offline market — no network."""

from datetime import datetime, timedelta, timezone

import pytest

from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv


class FakeMarket:
    """Offline MarketData stand-in with controllable prices."""

    def __init__(self, frames: dict):
        self.frames = frames
        self.price_overrides: dict[str, float] = {}

    def history(self, ticker, period="1y"):
        return self.frames.get(ticker)

    def latest_price(self, ticker):
        if ticker in self.price_overrides:
            return self.price_overrides[ticker]
        df = self.frames.get(ticker)
        return float(df["Close"].iloc[-1]) if df is not None else None

    def news(self, ticker, limit=8):
        return [{"title": f"{ticker} beats earnings expectations", "publisher": "T", "link": ""}]


@pytest.fixture
def engine():
    frames = {
        "UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11),
        "DOWN": make_ohlcv(daily_drift=-0.008, daily_vol=0.008, seed=12),
        "FLAT": make_ohlcv(daily_drift=0.0, daily_vol=0.004, seed=13),
    }
    return Engine(db=Database(":memory:"), market=FakeMarket(frames))


def test_scan_ranks_uptrend_above_downtrend(engine):
    result = engine.scan(tickers=["UPUP", "DOWN", "FLAT"], capture_context=False, record=False)
    tickers = [s.ticker for s in result.suggestions]
    assert tickers[0] == "UPUP" and tickers[-1] == "DOWN"
    up = result.suggestions[0]
    down = result.suggestions[-1]
    assert up.risk_adjusted_score > down.risk_adjusted_score
    assert up.action in ("BUY", "STRONG BUY")
    assert down.action in ("SELL", "STRONG SELL")


def test_scan_records_convictions(engine):
    engine.scan(tickers=["UPUP", "DOWN"], capture_context=False, record=True)
    recorded = engine.db.recent_suggestions()
    assert len(recorded) >= 1
    assert all(abs(r["score"]) >= 0.2 for r in recorded)


# The synthetic bars end on a fixed date (see conftest.make_ohlcv), so grading
# tests live on that timeline: calls made ~10 sessions before the last bar,
# graded the evening after it. Real "now" would sit months past the data.
CALLED_AT = "2026-06-22T14:00:00+00:00"
GRADED_AT = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _backdate(engine):
    """Place every recorded call inside the fixture's history so grading
    resolves from price bars, exactly as it does against real data."""
    engine.db.conn.execute("UPDATE suggestions SET created_at = ?", (CALLED_AT,))
    engine.db.conn.commit()


def _market_engine(with_spy: bool):
    frames = {
        "UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11),
        "DOWN": make_ohlcv(daily_drift=-0.008, daily_vol=0.008, seed=12),
    }
    if with_spy:
        frames["SPY"] = make_ohlcv(daily_drift=0.001, daily_vol=0.006, seed=9)
    return Engine(db=Database(":memory:"), market=FakeMarket(frames))


def test_learning_loop_end_to_end():
    """Record -> mature -> grade against the market -> weights actually move."""
    engine = _market_engine(with_spy=True)
    engine.scan(tickers=["UPUP", "DOWN"], capture_context=False, record=True)
    assert engine.db.load_weights() == {}
    _backdate(engine)

    count = engine.evaluate_pending(now=GRADED_AT)
    assert count >= 1

    weights = engine.db.load_weights()
    assert weights and sum(weights.values()) == pytest.approx(1.0)
    graded = [r for r in engine.db.recent_suggestions() if r["evaluated"]]
    assert graded
    assert all(r["benchmark_return"] is not None for r in graded), (
        "every grade the weights learned from must carry its market benchmark"
    )
    up_rows = [r for r in graded if r["ticker"] == "UPUP"]
    if up_rows:  # UPUP predicted up, price went up -> hit
        assert up_rows[0]["hit"] == 1


def test_a_call_with_no_market_benchmark_trains_nothing():
    """Weights learn from SPY-EXCESS return or not at all. When the benchmark
    is unavailable the old code fell back to the raw return, which in a rising
    market rewards every long call and teaches permanent bullishness — the
    exact bias market-relative grading exists to remove, silently readmitted
    whenever SPY history was missing."""
    engine = _market_engine(with_spy=False)
    engine.scan(tickers=["UPUP", "DOWN"], capture_context=False, record=True)
    _backdate(engine)

    count = engine.evaluate_pending(now=GRADED_AT)

    assert count >= 1, "the call is real and belongs in the ledger"
    assert engine.db.load_weights() == {}, "weights learned from an unbenchmarked grade"
    graded = [r for r in engine.db.recent_suggestions() if r["evaluated"]]
    assert graded and all(r["benchmark_return"] is None for r in graded)
    assert all(r["realized_return"] is not None for r in graded)
    assert engine.db.conn.execute("SELECT COUNT(*) FROM learning_log").fetchone()[0] == 0


def test_evaluate_skips_immature(engine):
    engine.scan(tickers=["UPUP"], capture_context=False, record=True)
    assert engine.evaluate_pending(now=datetime.now(timezone.utc)) == 0


def test_missing_data_reported_not_fatal(engine):
    result = engine.scan(tickers=["NOPE", "UPUP"], capture_context=False, record=False)
    assert any("NOPE" in e for e in result.errors)
    assert len(result.suggestions) == 1


# --- true-horizon, benchmark-relative grading ---


def _backdate_suggestion(engine, ticker, entry_pos, score=0.5):
    """Record a call as if it were made at the close of the frame's entry_pos bar."""
    df = engine.market.frames[ticker]
    price = float(df["Close"].iloc[entry_pos])
    sid = engine.db.record_suggestion(ticker, "BUY", score, price, {"trend_long": 1.0}, 5)
    created = str(df.index[entry_pos].date()) + "T21:00:00+00:00"
    engine.db.conn.execute(
        "UPDATE suggestions SET created_at = ? WHERE id = ?", (created, sid)
    )
    engine.db.conn.commit()
    return sid, price, df


def test_grading_uses_exact_trading_day_horizon(engine):
    sid, price, df = _backdate_suggestion(engine, "UPUP", entry_pos=-10)
    now = (df.index[-1] + timedelta(days=5)).tz_localize(timezone.utc)
    assert engine.evaluate_pending(now=now) == 1
    row = engine.db.conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
    # Graded at exactly entry + 5 trading bars, not at whatever bar is latest.
    expected = float(df["Close"].iloc[-10 + 5]) / price - 1.0
    assert row["realized_return"] == pytest.approx(expected)


def test_grading_stores_benchmark_and_learns_from_excess(engine):
    spy = make_ohlcv(daily_drift=0.001, daily_vol=0.006, seed=99)
    engine.market.frames["SPY"] = spy
    sid, price, df = _backdate_suggestion(engine, "UPUP", entry_pos=-10)
    now = (df.index[-1] + timedelta(days=5)).tz_localize(timezone.utc)
    assert engine.evaluate_pending(now=now) == 1
    row = engine.db.conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
    expected_bench = float(spy["Close"].iloc[-10 + 5]) / float(spy["Close"].iloc[-10]) - 1.0
    assert row["benchmark_return"] == pytest.approx(expected_bench)
    # The learning log shows the update was made on the SPY-excess return.
    import json as _json

    detail = _json.loads(
        engine.db.conn.execute(
            "SELECT detail FROM learning_log WHERE suggestion_id = ?", (sid,)
        ).fetchone()["detail"]
    )
    assert detail["benchmark_return"] == pytest.approx(expected_bench)
    if "realized_return" in detail:  # not skipped as noise
        assert detail["realized_return"] == pytest.approx(
            row["realized_return"] - expected_bench
        )


def test_grading_waits_when_horizon_not_reached(engine):
    df = engine.market.frames["UPUP"]
    _backdate_suggestion(engine, "UPUP", entry_pos=-3)
    # Calendar gate has passed but only 2 completed bars exist after the call:
    # the row must stay pending rather than be graded on a 2-day return.
    now = (df.index[-1] + timedelta(days=6)).tz_localize(timezone.utc)
    assert engine.evaluate_pending(now=now) == 0
    # Much later, with the data clearly never arriving, it grades with what exists.
    stale = (df.index[-1] + timedelta(days=40)).tz_localize(timezone.utc)
    assert engine.evaluate_pending(now=stale) == 1


# --- source separation and ungradeable backdated fills ---


def test_backdated_fill_before_history_is_never_graded_as_a_five_day_call(engine):
    """A real broker fill from years ago has no resolvable 5-day horizon in a
    1-year window. Grading it on today's price books the whole holding period
    as one call — that is what produced a +22% average and a 4.35 profit
    factor on the owner's brain."""
    df = engine.market.frames["UPUP"]
    old_price = float(df["Close"].iloc[0]) / 4  # bought years ago, far cheaper
    sid = engine.db.record_suggestion(
        "UPUP", "BUY", 0.35, old_price, {"trend_long": 1.0}, 5,
        created_at="2019-03-04T15:00:00+00:00",
    )
    now = (df.index[-1] + timedelta(days=1)).tz_localize(timezone.utc)
    engine.evaluate_pending(now=now)
    row = engine.db.conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["evaluated"] == 1          # closed, so it stops being retried
    assert row["realized_return"] is None  # but contributes no number anywhere
    # And it stays out of every aggregate.
    assert engine.db.performance_summary(source=None)["evaluated"] == 0
    assert engine.db.evaluated_suggestions(source=None) == []


def test_owner_fills_are_graded_but_do_not_train_the_model(engine):
    from stocksage.db import SOURCE_OWNER

    df = engine.market.frames["UPUP"]
    entry = float(df["Close"].iloc[-10])
    sid = engine.db.record_suggestion(
        "UPUP", "BUY", 0.35, entry, {"trend_long": 1.0}, 5,
        created_at=str(df.index[-10].date()) + "T21:00:00+00:00",
        source=SOURCE_OWNER,
    )
    now = (df.index[-1] + timedelta(days=5)).tz_localize(timezone.utc)
    assert engine.evaluate_pending(now=now) == 1

    row = engine.db.conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["realized_return"] is not None   # graded, so it can be measured
    assert engine.db.load_weights() == {}       # but it trained nothing
    # It is visible under its own source, and absent from the model's record.
    assert len(engine.db.evaluated_suggestions(source=SOURCE_OWNER)) == 1
    assert engine.db.evaluated_suggestions() == []
    assert engine.db.performance_summary()["evaluated"] == 0


def test_existing_brains_split_retroactively_on_upgrade(tmp_path):
    """The owner already has hundreds of mixed calls. The ingested_fills
    table makes the split recoverable without losing history."""
    import sqlite3

    from stocksage.db import SOURCE_MODEL, SOURCE_OWNER, Database

    path = tmp_path / "old.db"
    db = Database(path)
    model_id = db.record_suggestion("AAA", "BUY", 0.5, 100.0, {}, 5)
    fill_id = db.record_suggestion("BBB", "BUY", 0.35, 50.0, {}, 5)
    db.mark_fill_ingested("order-1", fill_id)
    # Simulate a brain written before the column existed.
    db.conn.execute("ALTER TABLE suggestions RENAME TO s_old")
    db.conn.execute(
        "CREATE TABLE suggestions AS SELECT id, created_at, ticker, action,"
        " score, price, signals, horizon_days, evaluated, realized_return,"
        " hit, benchmark_return FROM s_old"
    )
    db.conn.commit()
    db.close()
    assert "source" not in {
        r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(suggestions)")
    }

    upgraded = Database(path)  # opening migrates and backfills
    rows = {r["id"]: r["source"] for r in upgraded.conn.execute(
        "SELECT id, source FROM suggestions")}
    assert rows[model_id] == SOURCE_MODEL
    assert rows[fill_id] == SOURCE_OWNER
    upgraded.close()
