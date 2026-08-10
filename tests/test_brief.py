"""Brief-publishing tests — the contract the routine agent reads. Offline."""

import json

import pytest

from stocksage.brief import (
    SCHEMA_VERSION,
    build_brief,
    headline_sentence,
    scoreboard,
    write_brief,
)
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


class EarningsMarket(FakeMarket):
    """FakeMarket that also answers the earnings-calendar question."""

    def __init__(self, frames, earnings=None):
        super().__init__(frames)
        self.earnings = earnings or {}

    def next_earnings_date(self, ticker):
        return self.earnings.get(ticker)


@pytest.fixture
def engine():
    frames = {
        "UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11),
        "DOWN": make_ohlcv(daily_drift=-0.008, daily_vol=0.008, seed=12),
        "SPY": make_ohlcv(daily_drift=0.001, daily_vol=0.006, seed=99),
    }
    return Engine(db=Database(":memory:"), market=EarningsMarket(frames))


def _scan(engine, **kw):
    return engine.scan(tickers=["UPUP", "DOWN"], capture_context=False, record=False, **kw)


def test_brief_has_stable_schema(engine):
    brief = build_brief(_scan(engine), engine)
    for key in (
        "schema_version", "as_of", "headline", "health", "snapshot_absorbed",
        "graded_this_call", "market_mood", "candidates", "avoid", "model_stats",
        "scan_errors",
    ):
        assert key in brief, f"missing top-level key {key!r}"
    assert brief["schema_version"] == SCHEMA_VERSION
    assert json.loads(json.dumps(brief))  # must be JSON-serializable


def test_candidate_carries_what_the_agent_needs(engine):
    brief = build_brief(_scan(engine), engine)
    assert brief["candidates"], "uptrend name should reach the brief"
    c = brief["candidates"][0]
    for key in (
        "ticker", "verdict", "actionable", "score", "price", "stop", "target",
        "reward_to_risk", "model_record", "earnings_days", "earnings_blackout",
        "size_hint_pct", "top_signals", "why", "notes",
    ):
        assert key in c, f"missing candidate key {key!r}"
    # Target must sit a 2:1 measured move above the stop.
    assert c["target"] > c["price"] > c["stop"]
    assert c["target"] - c["price"] == pytest.approx(2 * (c["price"] - c["stop"]), rel=1e-3)
    assert len(c["top_signals"]) <= 3
    assert all(s["meaning"] for s in c["top_signals"])


def test_earnings_blackout_marks_candidate_not_actionable():
    frames = {"UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11)}
    from datetime import date, timedelta

    soon = (date.today() + timedelta(days=2)).isoformat()
    eng = Engine(
        db=Database(":memory:"), market=EarningsMarket(frames, {"UPUP": soon})
    )
    brief = build_brief(
        eng.scan(tickers=["UPUP"], capture_context=False, record=False), eng
    )
    c = brief["candidates"][0]
    assert c["earnings_days"] == 2
    assert c["earnings_blackout"] is True
    assert c["actionable"] is False  # visible, but never proposable into a print
    assert c["size_hint_pct"] == 0.0


def test_headline_reports_edge_over_spy():
    db = Database(":memory:")
    for ticker, realized, bench in (("AAA", 0.06, 0.02), ("BBB", 0.01, 0.03)):
        sid = db.record_suggestion(ticker, "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        db.mark_evaluated(sid, realized, realized > 0, benchmark_return=bench)
    stats = scoreboard(db)
    assert stats["edge_vs_market"] == pytest.approx(20.0)
    line = headline_sentence(stats)
    assert "$20.00 MORE than" in line and "SPY" in line

    # And the losing case reads as plainly as the winning one.
    db2 = Database(":memory:")
    sid = db2.record_suggestion("CCC", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
    db2.mark_evaluated(sid, 0.01, True, benchmark_return=0.05)
    assert "LESS than" in headline_sentence(scoreboard(db2))


def test_headline_refuses_to_claim_edge_without_benchmark_grades():
    db = Database(":memory:")
    sid = db.record_suggestion("AAA", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
    db.mark_evaluated(sid, 0.06, True)  # graded, but never against the market
    line = headline_sentence(scoreboard(db))
    assert "SPY" not in line
    assert "none yet measured against the market" in line
    assert headline_sentence(scoreboard(Database(":memory:"))).startswith("No calls graded")


def test_health_flags_missing_earnings_and_broker(engine):
    brief = build_brief(_scan(engine), engine)
    health = brief["health"]
    assert health["ok"] is False
    assert health["checks"]["earnings_calendar"] is False
    assert health["checks"]["robinhood_linked"] is False
    joined = " ".join(health["degraded"])
    assert "blackout is NOT protecting" in joined
    assert "Robinhood not linked" in joined


def test_health_ok_when_everything_present():
    from datetime import date, timedelta

    frames = {
        "UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11),
        "SPY": make_ohlcv(daily_drift=0.001, daily_vol=0.006, seed=99),
    }
    far = (date.today() + timedelta(days=60)).isoformat()
    eng = Engine(db=Database(":memory:"), market=EarningsMarket(frames, {"UPUP": far}))
    eng.db.set_meta("bootstrap_done", "2026-01-01")

    class Port:
        buying_power = 1000.0
        holdings: list = []

        def shares_of(self, t):
            return 0.0

    result = eng.scan(
        tickers=["UPUP"], capture_context=False, record=False, portfolio=Port()
    )
    brief = build_brief(result, eng)
    assert brief["health"]["ok"] is True
    assert brief["health"]["degraded"] == []
    # With buying power known, sizing is expressed in real money. (Dollars come
    # from the exact fraction; the percent is rounded for display, so they agree
    # only to within that rounding.)
    c = brief["candidates"][0]
    assert c["size_hint_dollars"] == pytest.approx(c["size_hint_pct"] / 100 * 1000.0, abs=0.5)
    assert c["est_shares"] > 0


def test_write_brief_is_atomic_and_leaves_no_temp(tmp_path):
    brief = {"headline": "hi", "candidates": []}
    path = write_brief(brief, tmp_path / "nested" / "Brief.json")
    assert json.loads(path.read_text())["headline"] == "hi"
    assert list(path.parent.glob("*.tmp")) == []


def test_earnings_calendar_consulted_only_for_buy_candidates():
    """A cold calendar must not cost one network lookup per universe name."""

    class CountingMarket(EarningsMarket):
        def __init__(self, frames):
            super().__init__(frames)
            self.lookups: list[str] = []

        def next_earnings_date(self, ticker):
            self.lookups.append(ticker)
            return None

    frames = {
        "UPUP": make_ohlcv(daily_drift=0.004, daily_vol=0.008, seed=11),
        "DOWN": make_ohlcv(daily_drift=-0.008, daily_vol=0.008, seed=12),
        "FLAT": make_ohlcv(daily_drift=0.0, daily_vol=0.004, seed=13),
    }
    market = CountingMarket(frames)
    eng = Engine(db=Database(":memory:"), market=market)
    result = eng.scan(
        tickers=["UPUP", "DOWN", "FLAT"], capture_context=False, record=False
    )
    buys = {s.ticker for s in result.suggestions if s.action in ("BUY", "STRONG BUY")}
    assert buys, "fixture should produce at least one buy"
    # Sells and holds never touch the calendar.
    assert set(market.lookups) == buys
