"""GIVEBACK (a winner that has given back half its gain) and SETUP (a dip or strength in the
brief's buy-rated names). Owner, 2026-11-23: "watch for any other triggers to buy or sell"."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stocksage import journal, mobile, watcher

NOW = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)        # 11:05 AM ET Tuesday, clear of the :35 runs


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")
    monkeypatch.setenv(mobile.KEY_VAR, "k" * 32)


def quotes(table):
    def q(t):
        price, change = table[t]
        return {"symbol": t, "price": price, "day_change": change}
    return q


def held(entry=100.0, stop=None, ticker="ABC"):
    return [{"ticker": ticker, "entry": entry, "stop": stop, "shares": 1.0,
             "lots": [{"shares": 1.0, "entry": entry, "opened": "2026-10-30T15:00+00:00"}]}]


def look(state, price, h, ranges=None):
    return watcher.check(state, NOW, h, quotes({h[0]["ticker"]: (price, 0.0), "SPY": (700, 0.0)}),
                         lambda t: None, None, ranges=ranges)


# --- GIVEBACK ----------------------------------------------------------------------------------

def test_a_winner_that_gives_back_half_its_gain_wakes_the_trader_once():
    state, h = {}, held()
    assert not any(e.startswith("GIVEBACK") for e in look(state, 108.0, h))         # best +8%
    assert not any(e.startswith("GIVEBACK") for e in look(state, 105.0, h)), "gave back less than half"
    said = [e for e in look(state, 103.9, h) if e.startswith("GIVEBACK")]
    assert len(said) == 1 and "+8.0%" in said[0] and "+3.9%" in said[0] and watcher._event_ticker(said[0]) == "ABC"
    assert not any(e.startswith("GIVEBACK") for e in look(state, 103.0, h)), "once per best price"
    look(state, 112.0, h)
    assert any(e.startswith("GIVEBACK") for e in look(state, 105.5, h)), "a new best is a new giveback"


def test_no_giveback_on_a_small_gain_below_the_entry_or_once_the_stop_locks_it_in():
    state = {}
    look(state, 103.0, held())
    assert not any(e.startswith("GIVEBACK") for e in look(state, 101.0, held())), "+3% never counted"
    state = {}
    look(state, 110.0, held())
    assert not any(e.startswith("GIVEBACK") for e in look(state, 99.0, held())), "below the entry: DROP/stop speak"
    state = {}
    look(state, 110.0, held(stop=105.5))
    assert not any(e.startswith("GIVEBACK") for e in look(state, 105.8, held(stop=105.5))), "stop locks half in"


def test_the_gain_that_counts_follows_the_stocks_normal_day():
    assert watcher.give_min("ABC", False, {"ABC": 0.03}) == pytest.approx(0.06)
    assert watcher.give_min("ABC", False, {"ABC": 0.01}) == watcher.GIVE_MIN
    assert watcher.give_min("ABC", False, {}) == watcher.GIVE_DEFAULT
    assert watcher.give_min("BTC-USD", True) == watcher.GIVE_CRYPTO


def test_parking_funds_never_give_back():
    state, h = {}, held(ticker="SGOV")
    look(state, 120.0, h)
    assert not any(e.startswith("GIVEBACK") for e in look(state, 104.0, h))


# --- SETUP -------------------------------------------------------------------------------------

BRIEF = {
    "focus": [{"ticker": "TSN", "verdict": "STRONG BUY", "actionable": True, "price": 65.0, "stop": 61.5,
               "atr_pct": 0.028, "earnings_days": 77}],
    "candidates": [
        {"ticker": "COST", "verdict": "STRONG BUY", "actionable": True, "price": 1100.0, "stop": 1064.0,
         "atr_pct": 0.017, "earnings_days": 17},
        {"ticker": "PG", "verdict": "BUY", "actionable": True, "price": 170.0, "stop": 163.0, "atr_pct": 0.014},
        {"ticker": "SOON", "verdict": "BUY", "actionable": True, "price": 50.0, "stop": 47.0, "atr_pct": 0.02,
         "earnings_days": 3},
        {"ticker": "HOLDQ", "verdict": "HOLD", "actionable": True, "price": 50.0, "stop": 47.0, "atr_pct": 0.02},
        {"ticker": "NOACT", "verdict": "BUY", "actionable": False, "price": 50.0, "stop": 47.0, "atr_pct": 0.02}],
    "evidence": {"PG": {"buy": {"edge": -0.007}}, "COST": {"buy": {"edge": 0.007}}},
}


def mem(cash=40.0, open_=()):
    return {"open": [{"ticker": t, "dollars_in": 25.0, "entry": e} for t, e in open_],
            "trader": {"cash": cash, "equity": 100.0}, "idea_watch": []}


def test_only_buy_rated_actionable_names_clear_of_earnings_and_not_losing_on_look_alikes_are_watched():
    got = [s["ticker"] for s in watcher.setups_from_brief(BRIEF)]
    assert got == ["TSN", "COST"], "PG's look-alikes lose; SOON reports in 3 days; HOLD and not actionable are out"


def test_a_dip_and_strength_each_speak_once_a_day_and_need_free_cash():
    state = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.002}}}
    table = {"TSN": (65.0, 0.0), "COST": (1075.0, -0.02)}          # COST down 2% = 1.2 normal days
    said = watcher.setup_events(state, NOW, mem(), [], quotes(table))
    assert len(said) == 1 and said[0].startswith("SETUP: COST") and "dipped" in said[0]
    assert watcher.setup_events(state, NOW, mem(), [], quotes(table)) == [], "once a day"
    table["TSN"] = (67.0, 0.035)                                   # +3.5%, +3.3% vs market = 1.2 normal days
    said = watcher.setup_events(state, NOW, mem(), [], quotes(table))
    assert said and "against the market" in said[0]
    fresh = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.0}}}
    assert watcher.setup_events(fresh, NOW, mem(cash=2.0), [], quotes({"TSN": (67, 0.04), "COST": (1075, -0.02)})) == []


def test_a_dividend_coming_off_the_price_is_not_a_dip():
    """2026-11-24: Costco's dividend came off its price; the raw quote said -1.7%, the
    adjusted close said +1.1%. A real dip on another day still speaks."""
    state = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.0}}}
    table = {"TSN": (65.0, 0.0), "COST": (1079.0, -0.017)}
    ex_div = lambda t, now: 1067.91 / 1097.7 if t == "COST" else 1.0  # noqa: E731
    assert watcher.setup_events(state, NOW, mem(), [], quotes(table), dividend=ex_div) == []
    fresh = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.0}}}
    said = watcher.setup_events(fresh, NOW, mem(), [], quotes(table), dividend=lambda t, now: 1.0)
    assert said and said[0].startswith("SETUP: COST")


def test_the_dividend_factor_reads_the_adjusted_previous_close():
    day = lambda d: int(datetime(2026, 11, d, 21, 0, tzinfo=timezone.utc).timestamp())  # noqa: E731
    daily = {"chart": {"result": [{"timestamp": [day(20), day(23), day(24)],
                                   "indicators": {"quote": [{"close": [1090.0, 1097.7, 1079.0]}],
                                                  "adjclose": [{"adjclose": [1060.4, 1067.91, 1079.0]}]}}]}}
    on_ex_date = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)
    assert watcher.dividend_factor("COST", on_ex_date, fetch=lambda u: daily) == pytest.approx(1067.91 / 1097.7)
    plain = {"chart": {"result": [{"timestamp": [day(23)], "indicators": {"quote": [{"close": [100.0]}]}}]}}
    assert watcher.dividend_factor("X", on_ex_date, fetch=lambda u: plain) == 1.0
    assert watcher.dividend_factor("X", on_ex_date, fetch=lambda u: 1 / 0) == 1.0


def test_no_setup_for_a_chase_a_held_name_below_the_stop_or_outside_the_session():
    state = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.0}}}
    assert watcher.setup_events(state, NOW, mem(), [], quotes({"TSN": (69, 0.06), "COST": (1060, -0.04)})) == [], \
        "TSN up 6% is a chase; COST under its stop is broken"
    h = [{"ticker": "COST"}]
    assert watcher.setup_events(state, NOW, mem(), h, quotes({"TSN": (65, 0.0), "COST": (1075, -0.02)})) == []
    evening = NOW + timedelta(hours=6)
    assert watcher.setup_events(state, evening, mem(), [], quotes({"TSN": (65, 0.0), "COST": (1075, -0.02)})) == []


def test_setup_only_wake_ups_are_capped_at_two_a_day_and_graded_by_the_scorecard():
    sent = []

    def post(url, token, body):
        sent.append(body)
        return {}

    state = {"setups": watcher.setups_from_brief(BRIEF), "live": {"spy": {"change": 0.0}}}
    now = NOW
    for name in ("COST", "TSN", "COST"):
        state["setup_said"] = []
        state.pop("last_fire", None)
        table = {"SPY": (700, 0.0), "TSN": (65, 0.0), "COST": (1100, 0.0)}
        table[name] = (table[name][0] * 0.97, -0.03)
        watcher.tick(state, now, quote=quotes(table), crypto_quote=lambda t: None, news=None, memory=mem(), post=post)
        now += timedelta(minutes=1)
    wakes = [b for b in sent if "SETUP" in str(b)]
    assert len(wakes) == 2, "the third setup-only wake-up waits for the next scheduled run"
    line = "WAKE | 2026-11-03T16:05Z | SETUP: COST at 1,067.00 has dipped -3.0% today"
    assert journal.wake_scorecard(line, now=NOW + timedelta(hours=2))["by_kind"]["SETUP"]["wakes"] == 1
