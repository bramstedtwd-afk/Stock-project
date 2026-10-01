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


# --- today's actions travel with the brief -----------------------------------


def _held(engine, ticker="WEAK", shares=2.0):
    price = float(engine.market.frames[ticker]["Close"].iloc[-1])
    return [{"ticker": ticker, "shares": shares, "avg_cost": price * 1.3,
             "price": price, "equity": round(shares * price, 2)}]


def test_the_brief_carries_a_plan_for_the_account_it_was_given(engine):
    brief = build_brief(engine, tickers=["WEAK"], buying_power=200.0,
                        positions=_held(engine))
    plan = brief["actions"]
    assert plan is not None
    assert set(plan) == {"actions", "no_action_needed", "notes", "model_trust"}
    # WEAK was bought 30% above where it trades: through its stop.
    assert any(a["kind"] == "EXIT_STOP" and a["ticker"] == "WEAK" for a in plan["actions"])


def test_no_plan_is_invented_when_the_account_is_unknown(engine):
    """Without positions the brief must not pretend to know what is held."""
    assert build_brief(engine, tickers=["WEAK"], buying_power=200.0)["actions"] is None
    assert build_brief(engine, tickers=["WEAK"], positions=_held(engine))["actions"] is None


def test_a_planning_bug_cannot_cost_the_whole_brief(engine, monkeypatch):
    from stocksage import advisor

    def boom(*a, **k):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(advisor, "plan_for_account", boom)
    brief = build_brief(engine, tickers=["CHEAP"], buying_power=100.0,
                        positions=_held(engine, "WEAK"))
    assert brief["actions"] is None
    assert brief["focus"], "the rest of the brief must survive"


def test_the_plan_respects_the_owners_switch(engine):
    engine.db.set_model_suggestions(False)
    brief = build_brief(engine, tickers=["CHEAP"], buying_power=300.0, positions=[])
    assert not [a for a in brief["actions"]["actions"] if a["kind"] == "ENTER"]
    assert any("switched off" in n for n in brief["actions"]["notes"])


def test_the_planner_reads_entry_dates_from_the_broker_mirror(engine):
    from datetime import datetime, timedelta, timezone

    from stocksage.advisor import plan_for_account

    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    engine.db.upsert_rh_orders([{
        "order_id": "o1", "ticker": "CHEAP", "side": "buy", "quantity": 1,
        "price": 8.0, "executed_at": long_ago,
    }])
    price = float(engine.market.frames["CHEAP"]["Close"].iloc[-1])
    held = [{"ticker": "CHEAP", "shares": 1.0, "avg_cost": price * 0.99,
             "price": price, "equity": price}]
    suggestions = engine.scan(capture_context=False, record=False).suggestions
    plan = plan_for_account(engine, suggestions, held, cash=500.0)
    kinds = {a.kind for a in plan.actions if a.ticker == "CHEAP"}
    assert kinds & {"EXIT_TIME", "HOLD_PAST_TIME", "EXIT_TARGET"}, kinds


def test_a_holding_the_daily_scan_never_covered_still_gets_its_stop_checked(engine):
    """The scan covers the universe, watchlist and the DEFAULT account. A name
    held only in the trading account may be in none of them; without a scan it
    has no ATR, so no stop, so a breach would pass in silence."""
    from stocksage.advisor import plan_for_account

    plan = plan_for_account(engine, [], _held(engine, "WEAK"), cash=100.0)
    assert any(a.kind == "EXIT_STOP" and a.ticker == "WEAK" for a in plan.actions)
