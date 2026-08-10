import pytest

from stocksage.advisor import build_brief, log_call
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


@pytest.fixture
def engine(monkeypatch):
    from stocksage import universe

    frames = {
        "CHEAP": make_ohlcv(days=300, start_price=8.0, daily_drift=0.004, seed=61),
        "RICH": make_ohlcv(days=300, start_price=400.0, daily_drift=0.004, seed=62),
        "WEAK": make_ohlcv(days=300, start_price=20.0, daily_drift=-0.008, seed=63),
    }
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    return Engine(db=Database(":memory:"), market=FakeMarket(frames))


def test_brief_structure_and_price_filter(engine):
    brief = build_brief(engine, tickers=["CHEAP", "GHOST"], max_price=30.0, top=5)
    assert brief["market_mood"] is not None
    # Focus: CHEAP fully scored, GHOST degraded to brain context.
    cheap, ghost = brief["focus"]
    assert cheap["ticker"] == "CHEAP" and cheap["verdict"] in ("BUY", "STRONG BUY")
    assert ghost == {**ghost, "ticker": "GHOST", "data": "unavailable"}
    # Candidates respect the price cap: RICH (~$400+) excluded.
    names = [c["ticker"] for c in brief["candidates"]]
    assert "CHEAP" in names and "RICH" not in names
    # Stop/target hold the 2:1 promise.
    assert cheap["stop"] < cheap["price"] < cheap["target"]
    risk = cheap["price"] - cheap["stop"]
    reward = cheap["target"] - cheap["price"]
    assert reward == pytest.approx(2 * risk, rel=0.01)
    # Weak downtrend shows up as avoid.
    assert any(a["ticker"] == "WEAK" for a in brief["avoid"])
    assert brief["model_stats"]["graded_calls"] == 0


def test_dollar_sizing_makes_expensive_stocks_affordable(engine):
    """Regression: a small account must not be structurally locked out of
    good-but-expensive names. RICH (~$400/share) is unaffordable as even
    one whole share against a small account's per-ticker cap, but must
    still be actionable via a fractional dollar amount."""
    buying_power = 80.0
    brief = build_brief(engine, tickers=["RICH"], buying_power=buying_power)
    rich = brief["focus"][0]
    assert rich["verdict"] in ("BUY", "STRONG BUY")
    assert rich["size_hint_dollars"] is not None
    # The dollar size must respect the account's cap and never suggest
    # spending more than what's available.
    assert 0 < rich["size_hint_dollars"] <= buying_power
    assert rich["size_hint_dollars"] == pytest.approx(
        rich["size_hint_pct"] / 100 * buying_power, rel=0.01
    )
    # Fractional share count follows directly, well under 1 share.
    assert rich["est_shares"] is not None
    assert rich["est_shares"] < 1.0
    assert rich["est_shares"] == pytest.approx(
        rich["size_hint_dollars"] / rich["price"], abs=0.0001
    )


def test_dollar_sizing_absent_without_buying_power(engine):
    """No account balance known -> only the percentage hint, no dollar
    figure invented from nothing."""
    brief = build_brief(engine, tickers=["RICH"])
    rich = brief["focus"][0]
    assert rich["size_hint_dollars"] is None
    assert rich["est_shares"] is None
    assert rich["size_hint_pct"] > 0  # percentage still available


def test_dollar_sizing_zero_for_non_actionable_names(engine):
    """A name with no suggested position (e.g. a HOLD/SELL) gets no
    dollar size, even when buying_power is known."""
    brief = build_brief(engine, tickers=["WEAK"], buying_power=80.0)
    weak = brief["focus"][0]
    assert weak["verdict"] in ("SELL", "STRONG SELL", "HOLD")
    assert weak["size_hint_dollars"] is None


def test_brief_includes_model_record(engine):
    for i in range(6):
        sid = engine.db.record_suggestion("CHEAP", "BUY", 0.4, 8.0, {"x": 0.4}, 5)
        engine.db.mark_evaluated(sid, 0.05, True)
    brief = build_brief(engine, tickers=["CHEAP"])
    rec = brief["focus"][0]["model_record"]
    assert rec["graded_calls"] == 6 and rec["hit_rate"] == 1.0
    assert brief["model_stats"]["graded_calls"] == 6


def test_log_call_records_and_grades(engine):
    sid = log_call(engine, "cheap", "buy", note="bounced off support on volume")
    rows = engine.db.recent_suggestions()
    assert rows[0]["id"] == sid
    assert rows[0]["ticker"] == "CHEAP" and rows[0]["action"] == "BUY"
    assert rows[0]["score"] > 0  # direction follows the action
    sell_id = log_call(engine, "WEAK", "STRONG SELL", price=19.0)
    sell = engine.db.recent_suggestions()[0]
    assert sell["id"] == sell_id and sell["score"] < 0
    assert sell["price"] == 19.0
    # These calls now sit in the normal grading queue.
    from datetime import datetime, timedelta, timezone

    engine.market.price_overrides["CHEAP"] = 999.0
    engine.market.price_overrides["WEAK"] = 999.0
    graded = engine.evaluate_pending(now=datetime.now(timezone.utc) + timedelta(days=15))
    assert graded == 2
    graded_rows = [r for r in engine.db.recent_suggestions() if r["evaluated"]]
    hits = {r["ticker"]: r["hit"] for r in graded_rows}
    assert hits["CHEAP"] == 1 and hits["WEAK"] == 0  # buy was right, sell was wrong


def test_log_call_validation(engine):
    with pytest.raises(ValueError, match="action"):
        log_call(engine, "CHEAP", "HOLD")
    with pytest.raises(ValueError, match="price"):
        log_call(engine, "GHOST", "BUY")  # no data and no explicit price
