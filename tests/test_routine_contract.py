"""The routine's data contract: every field ROUTINE.md tells the routine to
use must exist in the brief, well-formed, on every run. This locks the
instructions and the data together so they can never silently drift apart.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from stocksage.advisor import build_brief, ingest_fills
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


class SimMarket(FakeMarket):
    def __init__(self, frames, earnings=None):
        super().__init__(frames)
        self._earnings = earnings or {}

    def earnings_date(self, ticker, today=None):
        return self._earnings.get(ticker)


@pytest.fixture
def brief(monkeypatch):
    from stocksage import universe

    frames = {
        "F": make_ohlcv(days=400, start_price=12.0, daily_drift=0.0, daily_vol=0.02, seed=1),
        "GOOD": make_ohlcv(days=400, start_price=9.0, daily_drift=0.004, seed=3),
        "SOON": make_ohlcv(days=400, start_price=8.0, daily_drift=0.004, seed=4),
        "WEAK": make_ohlcv(days=400, start_price=20.0, daily_drift=-0.008, seed=5),
    }
    earnings = {"SOON": (date.today() + timedelta(days=2)).isoformat()}
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    eng = Engine(db=Database(":memory:"), market=SimMarket(frames, earnings))
    for t in ("GOOD", "SOON", "WEAK"):
        eng.db.watchlist_add(t)

    def order(oid, tk, side, px, days):
        return {"order_id": oid, "ticker": tk, "side": side, "quantity": 1, "price": px,
                "executed_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()}

    ingest_fills(eng, [order("g1", "GOOD", "buy", 8.0, 25), order("g2", "GOOD", "buy", 8.5, 18)])
    eng.evaluate_pending()
    congress = {"GOOD": {"buys": 3, "sells": 0, "net_buys": 3, "members": 2,
                         "est_amount": 40000, "last_date": date.today().isoformat()}}
    return build_brief(
        eng, tickers=["F", "GOOD", "SOON", "WEAK"], max_price=50.0, congress_summary=congress
    )


TOP_LEVEL = {
    "as_of", "snapshot_absorbed", "graded_this_call", "market_mood",
    "congress_watch", "focus", "candidates", "avoid", "model_stats", "scan_errors",
}
ENTRY_FIELDS = {
    "ticker", "verdict", "actionable", "score", "price", "sector", "stop", "target",
    "reward_to_risk", "atr_pct", "annualized_vol", "model_record", "recent_shock",
    "events_12mo", "earnings_days", "earnings_blackout", "congress_buying",
    "size_hint_pct", "top_signals", "notes",
}


def test_brief_has_all_top_level_fields(brief):
    assert TOP_LEVEL <= set(brief)


def test_every_focus_entry_is_complete(brief):
    for e in brief["focus"]:
        if e.get("data") == "unavailable":
            assert "actionable" in e and e["actionable"] is False
            continue
        missing = ENTRY_FIELDS - set(e)
        assert not missing, f"{e['ticker']} missing {missing}"


def test_actionable_flag_is_unambiguous(brief):
    by = {e["ticker"]: e for e in brief["focus"]}
    # Earnings-blackout name: verdict may be BUY but NOT actionable, size 0.
    assert by["SOON"]["earnings_blackout"] is True
    assert by["SOON"]["actionable"] is False
    assert by["SOON"]["size_hint_pct"] == 0.0
    # A clean buy with size is actionable.
    assert by["GOOD"]["actionable"] is True
    assert by["GOOD"]["size_hint_pct"] > 0
    # Blackout names never appear as actionable candidates.
    assert "SOON" not in [c["ticker"] for c in brief["candidates"]]


def test_model_stats_has_no_misleading_dollar_figure(brief):
    ms = brief["model_stats"]
    assert "paper_pnl" not in ms  # a fixed-stake dollar P&L would mislead
    assert "hit_rate" in ms and "paper_profit_factor" in ms
    assert "avg_return_per_call" in ms


def test_reliability_and_congress_surface_for_the_routine(brief):
    good = next(e for e in brief["focus"] if e["ticker"] == "GOOD")
    assert good["model_record"]["graded_calls"] == 2
    assert good["congress_buying"]["members"] == 2  # tilt visible on the card


def test_candidates_are_all_actionable(brief):
    for c in brief["candidates"]:
        assert c["actionable"] is True
        assert c["verdict"] in ("BUY", "STRONG BUY")
        assert c["size_hint_pct"] > 0


def test_sell_actionability_depends_on_holdings(monkeypatch):
    from stocksage import universe

    frames = {"DOWN": make_ohlcv(days=250, start_price=14.0, daily_drift=-0.004, daily_vol=0.02, seed=9)}
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    eng = Engine(db=Database(":memory:"), market=SimMarket(frames))
    held = build_brief(eng, tickers=["DOWN"], holdings=["DOWN"], congress_summary={})
    unheld = build_brief(eng, tickers=["DOWN"], holdings=[], congress_summary={})
    e_held = held["focus"][0]
    e_unheld = unheld["focus"][0]
    assert e_held["verdict"] in ("SELL", "STRONG SELL")
    assert e_held["actionable"] is True     # you hold it -> selling is a real action
    assert e_unheld["actionable"] is False  # you don't -> it's only an avoid
