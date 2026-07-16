"""Earnings-calendar awareness: don't open new positions into a print."""

from datetime import date, timedelta

from stocksage.advisor import build_brief
from stocksage.data import next_future_earnings
from stocksage.db import Database
from stocksage.engine import Engine
from stocksage.scoring import build_suggestion
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket

RISK = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}


# --- pure date helper ---

def test_next_future_earnings_picks_soonest_upcoming():
    today = date(2026, 7, 15)
    dates = ["2026-05-01", "2026-07-20", "2026-10-30", "2026-07-16"]
    assert next_future_earnings(dates, today) == "2026-07-16"
    assert next_future_earnings(["2026-01-01"], today) is None  # all past
    assert next_future_earnings([], today) is None
    # tolerates datetime-ish strings
    assert next_future_earnings(["2026-07-20T00:00:00"], today) == "2026-07-20"


# --- scoring gate ---

def test_earnings_blackout_holds_new_entry():
    hot = build_suggestion("T", {"x": 0.9}, 0.8, RISK, earnings_days=2)
    clear = build_suggestion("T", {"x": 0.9}, 0.8, RISK, earnings_days=20)
    assert hot.action in ("BUY", "STRONG BUY")  # signal still bullish
    assert hot.position_fraction == 0.0          # but no fresh entry
    assert hot.stop_price is None
    assert any("Earnings in 2 day(s)" in n for n in hot.notes)
    assert clear.position_fraction > 0           # far-off earnings: normal


def test_earnings_note_for_held_position_on_sell():
    s = build_suggestion(
        "T", {"x": -0.9}, -0.8, RISK, owned_shares=5, earnings_days=1
    )
    assert any("you hold this" in n.lower() for n in s.notes)


def test_no_earnings_data_behaves_normally():
    s = build_suggestion("T", {"x": 0.9}, 0.8, RISK, earnings_days=None)
    assert s.position_fraction > 0
    assert s.earnings_days is None


# --- end to end through the engine + brief ---

class EarningsMarket(FakeMarket):
    def __init__(self, frames, earnings):
        super().__init__(frames)
        self._earnings = earnings

    def earnings_date(self, ticker, today=None):
        return self._earnings.get(ticker)


def test_brief_flags_earnings_blackout(monkeypatch):
    from stocksage import universe

    frames = {
        "SOON": make_ohlcv(days=300, start_price=10.0, daily_drift=0.004, seed=41),
        "CLEAR": make_ohlcv(days=300, start_price=10.0, daily_drift=0.004, seed=42),
    }
    soon = (date.today() + timedelta(days=2)).isoformat()
    far = (date.today() + timedelta(days=40)).isoformat()
    market = EarningsMarket(frames, {"SOON": soon, "CLEAR": far})
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    engine = Engine(db=Database(":memory:"), market=market)

    brief = build_brief(engine, tickers=["SOON", "CLEAR"], max_price=20.0)
    focus = {e["ticker"]: e for e in brief["focus"]}
    assert focus["SOON"]["earnings_blackout"] is True
    assert focus["SOON"]["earnings_days"] == 2
    assert focus["SOON"]["size_hint_pct"] == 0.0     # entry held before print
    assert focus["CLEAR"]["earnings_blackout"] is False
    # A name in earnings blackout should not appear as an actionable candidate.
    assert "SOON" not in [c["ticker"] for c in brief["candidates"]]


def test_engine_days_to_earnings_is_defensive():
    engine = Engine(db=Database(":memory:"), market=FakeMarket({}))
    # FakeMarket has no earnings_date attribute -> None, never raises.
    assert engine._days_to_earnings("AAPL") is None
