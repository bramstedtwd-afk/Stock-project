import pytest

from stocksage.briefing import briefing_lines, build_briefing
from stocksage.db import Database
from stocksage.engine import Engine, ScanResult
from stocksage.scoring import Suggestion
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


def sugg(ticker, action, score, owned=0.0, size=0.0):
    return Suggestion(
        ticker=ticker, action=action, score=score, risk_adjusted_score=score,
        price=100.0, signals={}, risk={}, owned_shares=owned, position_fraction=size,
    )


def seeded_result():
    return ScanResult(
        suggestions=[
            sugg("NVDA", "STRONG BUY", 0.55, size=0.06),
            sugg("AAPL", "BUY", 0.30, size=0.04),
            sugg("XOM", "SELL", -0.35, owned=10.0),
            sugg("KO", "HOLD", 0.02),
        ],
        sector_trends={"Technology": 0.4, "Energy": -0.2, "Healthcare": 0.15},
        evaluated_count=3,
        move_events=[{"ticker": "XOM", "return_pct": -0.05, "reasons": ["earnings"]}],
    )


def graded_db():
    db = Database(":memory:")
    sid = db.record_suggestion("AAPL", "BUY", 0.4, 100.0, {}, 5)
    db.mark_evaluated(sid, 0.05, True)  # created today -> inside the week window
    return db


def test_briefing_fields():
    result = seeded_result()
    result.portfolio = object()  # linked
    b = build_briefing(result, graded_db())
    assert b["mood"] == "bullish"
    assert [i["ticker"] for i in b["top_ideas"]] == ["NVDA", "AAPL"]
    assert b["owned_alerts"] == [{"ticker": "XOM", "action": "SELL", "score": -0.35}]
    assert b["graded_this_cycle"] == 3
    assert b["week_paper_pnl"] == pytest.approx(50.0)
    assert b["shocks"][0]["ticker"] == "XOM"


def test_briefing_lines_read_like_sentences():
    result = seeded_result()
    result.portfolio = object()
    lines = briefing_lines(build_briefing(result, graded_db()))
    text = " ".join(lines)
    assert "bullish" in text
    assert "NVDA" in text and "Top ideas" in text
    assert "Action needed on names you own: XOM (SELL)" in text
    assert "$+50.00" in text
    assert "Big move: XOM" in text


def test_briefing_no_ideas_message():
    result = ScanResult(suggestions=[sugg("KO", "HOLD", 0.0)], sector_trends={"Energy": -0.3})
    lines = briefing_lines(build_briefing(result, Database(":memory:")))
    assert any("cash is a position too" in line for line in lines)
    assert any("bearish" in line for line in lines)


# --- watchlist + scan coverage ---

def test_watchlist_roundtrip():
    db = Database(":memory:")
    assert db.watchlist() == []
    db.watchlist_add("pltr")   # normalized to upper
    db.watchlist_add("PLTR")   # no duplicate
    db.watchlist_add("SOFI")
    assert db.watchlist() == ["PLTR", "SOFI"]
    db.watchlist_remove("pltr")
    assert db.watchlist() == ["SOFI"]


def test_scan_covers_watchlist_and_holdings(monkeypatch):
    from stocksage import universe
    from stocksage.robinhood import Holding, Portfolio

    frames = {
        "UNIV": make_ohlcv(daily_drift=0.001, seed=51),
        "WATCHED": make_ohlcv(daily_drift=0.002, seed=52),
        "HELD": make_ohlcv(daily_drift=-0.001, seed=53),
    }
    monkeypatch.setattr(universe, "all_tickers", lambda: ["UNIV"])
    engine = Engine(db=Database(":memory:"), market=FakeMarket(frames))
    engine.db.watchlist_add("WATCHED")
    portfolio = Portfolio(
        holdings=[Holding("HELD", 5, 90.0, 100.0, 500.0)], buying_power=1000.0
    )
    result = engine.scan(portfolio=portfolio, capture_context=False, record=False)
    scanned = {s.ticker for s in result.suggestions}
    assert scanned == {"UNIV", "WATCHED", "HELD"}  # a stock you own is never unwatched
