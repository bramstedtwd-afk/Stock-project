"""Past-context and per-sector learning tests — the memory feeding the model."""

from datetime import date, timedelta

import pytest

from stocksage.db import Database
from stocksage.engine import Engine
from stocksage.learning import MIN_SECTOR_GRADES, blended_weights
from stocksage.scoring import PastContext, build_suggestion, reliability_multiplier
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket

RISK = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}


# --- reliability: the model consults its own record ---

def test_reliability_multiplier_shape():
    assert reliability_multiplier(0, None) == 1.0
    assert reliability_multiplier(3, 1.0) == 1.0          # too few grades: neutral
    assert reliability_multiplier(10, 0.5) == pytest.approx(1.0)
    assert reliability_multiplier(10, 0.8) > 1.0
    assert reliability_multiplier(10, 0.2) < 1.0
    assert reliability_multiplier(10, 0.0) == 0.60        # floor
    assert reliability_multiplier(10, 1.0) == 1.25        # ceiling


def test_bad_record_downgrades_action():
    good = build_suggestion(
        "T", {"x": 0.6}, 0.5, RISK, past=PastContext(graded_calls=10, hit_rate=0.9)
    )
    bad = build_suggestion(
        "T", {"x": 0.6}, 0.5, RISK, past=PastContext(graded_calls=10, hit_rate=0.1)
    )
    assert good.risk_adjusted_score > bad.risk_adjusted_score
    assert any("confidence boosted" in n for n in good.notes)
    assert any("confidence reduced" in n for n in bad.notes)


def test_recent_shock_tempers_conviction():
    shock = PastContext(
        recent_event={"date": "2026-07-07", "return_pct": -0.06, "reasons": ["earnings"]}
    )
    calm = build_suggestion("T", {"x": 0.6}, 0.5, RISK, past=PastContext())
    shocked = build_suggestion("T", {"x": 0.6}, 0.5, RISK, past=shock)
    assert shocked.risk_adjusted_score == pytest.approx(
        calm.risk_adjusted_score * 0.75, abs=1e-4
    )
    assert any("Recent shock" in n and "earnings" in n for n in shocked.notes)


def test_event_prone_name_gets_smaller_size():
    quiet = build_suggestion("T", {"x": 0.9}, 0.8, RISK, past=PastContext(events_12mo=2))
    jumpy = build_suggestion("T", {"x": 0.9}, 0.8, RISK, past=PastContext(events_12mo=15))
    assert 0 < jumpy.position_fraction < quiet.position_fraction
    assert any("Event-prone" in n for n in jumpy.notes)


# --- sector weights ---

def test_blended_weights_gate_and_math():
    g = {"a": 0.7, "b": 0.3}
    s = {"a": 0.1, "b": 0.9}
    assert blended_weights(g, s, sector_grades=MIN_SECTOR_GRADES - 1) == g  # gated
    blended = blended_weights(g, s, sector_grades=MIN_SECTOR_GRADES)
    assert blended["b"] > g["b"]  # sector view pulled it up
    assert sum(blended.values()) == pytest.approx(1.0)
    assert blended_weights(g, {}, 100) == g


def test_sector_weights_roundtrip():
    db = Database(":memory:")
    db.save_sector_weights("Technology", {"macd": 0.6, "trend_long": 0.4})
    db.save_sector_weights("Technology", {"macd": 0.7, "trend_long": 0.3})
    assert db.load_sector_weights("Technology") == {"macd": 0.7, "trend_long": 0.3}
    assert db.load_sector_weights("Energy") == {}


def test_evaluation_trains_sector_weights():
    """Grading a real-universe ticker must update its sector's own weights —
    from a market-relative result, like every other update."""
    from tests.test_engine import CALLED_AT, GRADED_AT, _backdate

    aapl = make_ohlcv(daily_drift=0.003, seed=31)
    frames = {"AAPL": aapl, "SPY": make_ohlcv(daily_drift=0.0005, seed=9)}
    engine = Engine(db=Database(":memory:"), market=FakeMarket(frames))
    engine.scan(tickers=["AAPL"], capture_context=False, record=True)
    _backdate(engine)

    # AAPL jumps 15% in the sessions after the call while SPY drifts: a
    # decisive win over the market, well clear of the learner's noise floor.
    after = aapl.index > CALLED_AT[:10]
    boosted = aapl.copy()
    boosted.loc[after, ["Open", "High", "Low", "Close"]] *= 1.15
    engine.market.frames["AAPL"] = boosted

    assert engine.evaluate_pending(now=GRADED_AT) >= 1
    assert engine.db.load_sector_weights("Technology")  # sector learned
    assert engine.db.sector_grade_counts().get("Technology", 0) >= 1


# --- past context assembly from the brain ---

def test_past_context_reads_the_brain():
    db = Database(":memory:")
    engine = Engine(db=db, market=FakeMarket({}))
    # Graded record: 6 calls, 5 right.
    for i in range(6):
        sid = db.record_suggestion("NVDA", "BUY", 0.4, 100.0, {"x": 0.4}, 5)
        db.mark_evaluated(sid, 0.03 if i < 5 else -0.03, i < 5)
    # One fresh shock and one old event.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    long_ago = (date.today() - timedelta(days=200)).isoformat()
    db.record_move_event("NVDA", yesterday, -0.05, 2.5, ["earnings"], [])
    db.record_move_event("NVDA", long_ago, 0.04, 2.1, ["analyst_rating"], [])
    ctx = engine._past_context("NVDA")
    assert ctx.graded_calls == 6
    assert ctx.hit_rate == pytest.approx(5 / 6)
    assert ctx.recent_event is not None and ctx.recent_event["date"] == yesterday
    assert ctx.events_12mo == 2
    # And an unknown name yields a neutral context.
    empty = engine._past_context("ZZZZ")
    assert empty.graded_calls == 0 and empty.recent_event is None


def test_ticker_track_record_query():
    db = Database(":memory:")
    assert db.ticker_track_record("AAPL") == (0, None, None)
    sid = db.record_suggestion("AAPL", "BUY", 0.4, 100.0, {}, 5)
    db.mark_evaluated(sid, 0.02, True)
    n, hit, avg = db.ticker_track_record("AAPL")
    assert n == 1 and hit == 1.0 and avg == pytest.approx(0.02)
