"""One trader, all day (playbook v41): the watcher sees pre-market and after-hours prices
and watches whole-share positions and ideas' entries from 7 AM to 8 PM; fractional
positions are still left to the regular session, where they can be sold."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stocksage import watcher

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _wake_up_address(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")
AFTER_HOURS = datetime(2026, 11, 17, 22, 10, tzinfo=UTC)      # Tuesday 5:10 PM ET
SESSION = datetime(2026, 11, 17, 17, 10, tzinfo=UTC)          # Tuesday 12:10 PM ET


def chart(closes, stamps=None, prev=739.95):
    stamps = stamps or [int(AFTER_HOURS.timestamp()) - 60 * (len(closes) - i) for i in range(len(closes))]
    return {"chart": {"result": [{"meta": {"previousClose": prev, "regularMarketPrice": 732.44},
                                  "timestamp": stamps, "indicators": {"quote": [{"close": closes}]}}]}}


def test_yahoo_carries_the_after_hours_trade_against_the_previous_close():
    q = watcher.yahoo_quote("spy", fetch=lambda url: chart([732.44, None, 733.63]))
    assert q["symbol"] == "SPY" and q["price"] == 733.63 and q["prev_close"] == 739.95
    assert q["day_change"] == round(733.63 / 739.95 - 1, 4) and q["at"].startswith("2026-11-17T22:09")
    assert watcher.yahoo_quote("SPY", fetch=lambda url: chart([None, None])) is None
    assert watcher.yahoo_quote("SPY", fetch=lambda url: {"chart": {"result": None}}) is None

    def offline(url):
        raise OSError("offline")

    assert watcher.yahoo_quote("SPY", fetch=offline) is None


def test_finnhub_in_the_session_yahoo_outside_it_and_finnhub_when_yahoo_has_nothing():
    fin = lambda t: {"price": 1.0, "src": "finnhub"}  # noqa: E731
    yah = lambda t: {"price": 2.0, "src": "yahoo"}  # noqa: E731
    assert watcher.session_quote("TSN", SESSION, fin, yah)["src"] == "finnhub"
    assert watcher.session_quote("TSN", AFTER_HOURS, fin, yah)["src"] == "yahoo"
    assert watcher.session_quote("TSN", AFTER_HOURS, fin, lambda t: None)["src"] == "finnhub"


def memory(shares: float, entry: float = 64.05, stop: float = 61.40, cash: float = 74.0) -> dict:
    return {"open": [{"ticker": "TSN", "dollars_in": shares * entry, "entry": entry, "stop": stop,
                      "target": 68.90, "opened": "2026-11-16T14:37+00:00"}],
            "trader": {"last_run": "2026-11-17T21:41+00:00", "cash": cash}}


def run(mem: dict, price: float, now=AFTER_HOURS) -> tuple[dict, list]:
    state, fired = {}, []
    quote = lambda t: {"symbol": t, "price": price if t == "TSN" else 733.0,  # noqa: E731
                       "day_change": price / 63.65 - 1 if t == "TSN" else -0.009, "at": now.isoformat()}
    text = watcher.tick(state, now, quote=quote, crypto_quote=lambda t: None, news=lambda t: [], memory=mem,
                        names=lambda t: "Tyson Foods", ranges={}, events={},
                        post=lambda url, token, body: {}, push=lambda *a, **k: None)
    if text:
        fired.append(text)
    return state, fired


def test_a_whole_share_position_that_breaks_its_stop_after_hours_wakes_the_trader():
    _, fired = run(memory(1), 61.00)
    assert fired and "STOP: TSN at 61.00 has touched its stop 61.40" in fired[0]


def test_a_fractional_position_after_hours_is_left_to_the_open_as_before():
    state, fired = run(memory(0.5), 61.00)
    assert not fired and not any("STOP" in s for s in state.get("stopped", []))


def test_an_ideas_entry_is_watched_after_hours_when_one_whole_share_fits_the_cash():
    mem = memory(0)
    mem["open"] = []
    mem["idea_watch"] = [{"ticker": "GIS", "entry": 35.50, "direction": "above", "level": "MEDIUM",
                          "stop": 34.40, "setup": "defensive", "at": "2026-11-17T21:40+00:00",
                          "expires": "2026-11-18"}]
    quote = lambda t: {"price": 35.60, "day_change": 0.01, "at": AFTER_HOURS.isoformat()}  # noqa: E731
    events = watcher.entry_events({}, AFTER_HOURS, mem, [], quote, lambda t: None)
    assert events and "whole shares, limit order, extended hours" in events[0]
    mem["trader"]["cash"] = 20.0
    state = {}
    assert watcher.entry_events(state, AFTER_HOURS, mem, [], quote, lambda t: None) == []
    assert "one share costs more than the free cash" in state["idea_rows"]["GIS"]["waiting"]
    late = datetime(2026, 11, 18, 2, 0, tzinfo=UTC)                 # 9 PM ET: nothing watched
    assert watcher.entry_events({}, late, mem, [], quote, lambda t: None) == []
