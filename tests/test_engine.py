"""End-to-end engine tests with an injected offline market — no network."""

from datetime import datetime, timedelta, timezone

import pytest

from stocksage.db import Database
from stocksage.engine import SUGGESTION_HORIZON_DAYS, Engine
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


def test_learning_loop_end_to_end(engine):
    """Record -> mature -> evaluate -> weights actually move."""
    engine.scan(tickers=["UPUP", "DOWN"], capture_context=False, record=True)
    assert engine.db.load_weights() == {}

    # Simulate the future: price resolves 10% above the suggestion price.
    for row in engine.db.recent_suggestions():
        engine.market.price_overrides[row["ticker"]] = row["price"] * 1.10

    future = datetime.now(timezone.utc) + timedelta(days=SUGGESTION_HORIZON_DAYS * 2)
    count = engine.evaluate_pending(now=future)
    assert count >= 1

    weights = engine.db.load_weights()
    assert weights and sum(weights.values()) == pytest.approx(1.0)
    graded = [r for r in engine.db.recent_suggestions() if r["evaluated"]]
    assert graded
    up_rows = [r for r in graded if r["ticker"] == "UPUP"]
    if up_rows:  # UPUP predicted up, price went up -> hit
        assert up_rows[0]["hit"] == 1


def test_evaluate_skips_immature(engine):
    engine.scan(tickers=["UPUP"], capture_context=False, record=True)
    assert engine.evaluate_pending(now=datetime.now(timezone.utc)) == 0


def test_missing_data_reported_not_fatal(engine):
    result = engine.scan(tickers=["NOPE", "UPUP"], capture_context=False, record=False)
    assert any("NOPE" in e for e in result.errors)
    assert len(result.suggestions) == 1
