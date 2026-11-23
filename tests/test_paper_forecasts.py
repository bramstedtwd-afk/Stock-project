"""Practice trades (Alpaca PAPER only), the forecast ledger and calibrated sizing. Offline."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from stocksage import confidence as cf
from stocksage import forecasts as fc
from stocksage import journal as jr
from stocksage import paper

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


def test_no_file_in_stocksage_knows_the_live_trading_host():
    """The live host is api.alpaca.markets; only the paper host may ever appear."""
    for f in Path(paper.__file__).parent.glob("*.py"):
        assert "//api.alpaca.markets" not in f.read_text(encoding="utf-8"), f.name
    assert paper.PAPER_HOST == "https://paper-api.alpaca.markets"
    with pytest.raises(paper.NotPaper):
        paper._call("GET", "/v2/account", host="https://api.alpaca.markets", http=lambda *a: {})


class FakeAlpaca:
    def __init__(self, number="PA3XYZ"):
        self.number, self.sent = number, []

    def __call__(self, method, url, body):
        if url.endswith("/v2/account"):
            return {"account_number": self.number, "equity": "100000", "cash": "100000"}
        if "/trades/latest" in url:
            return {"trade": {"p": 100.0}}
        self.sent.append((method, url, body))
        return dict(body or {}, status="accepted")


def test_a_practice_trade_risks_the_same_share_and_carries_its_level():
    http = FakeAlpaca()
    o = paper.bracket("abc", "high", "pull-back", stop=95.0, target=110.0, http=http, now=NOW)
    assert o["qty"] == "100", "0.5% of 100,000 = 500 at risk, 5 a share"
    assert o["order_class"] == "bracket" and o["stop_loss"]["stop_price"] == 95.0
    assert o["client_order_id"].startswith("ss-HIGH-pullback-ABC-")
    with pytest.raises(ValueError):
        paper.bracket("abc", "NO", "pullback", 95, 110, http=http)


def test_keys_that_open_a_real_account_send_nothing():
    http = FakeAlpaca(number="5AB123")
    with pytest.raises(paper.NotPaper):
        paper.bracket("ABC", "LOW", "pullback", 95, 110, http=http)
    assert http.sent == []


def test_closed_brackets_are_graded_in_r_by_level():
    def order(level, exit_px, n):
        return {"client_order_id": f"ss-{level}-pullback-ABC-{n}", "status": "filled", "symbol": "ABC",
                "filled_avg_price": "100", "filled_at": "2026-11-20T15:00:00Z",
                "legs": [{"stop_price": "95", "status": "filled" if exit_px < 100 else "canceled",
                          "filled_avg_price": str(exit_px) if exit_px < 100 else None},
                         {"limit_price": "110", "status": "filled" if exit_px >= 100 else "canceled",
                          "filled_avg_price": str(exit_px) if exit_px >= 100 else None}]}
    closed = paper.trades([order("HIGH", 110, 1), order("HIGH", 95, 2), order("LOW", 95, 3),
                           {"client_order_id": "someone-else", "status": "filled"}])
    assert [(t["level"], t["r"]) for t in closed] == [("HIGH", 2.0), ("HIGH", -1.0), ("LOW", -1.0)]
    s = paper.summarize(closed)
    assert s["by_confidence"]["HIGH"]["avg_r"] == 0.5 and "PRACTICE TRADES" in "\n".join(paper.lines(s))


def test_forecasts_are_scored_against_the_market_and_settle_themselves():
    text = "\n".join(["FORECAST | 2026-11-20T15:00Z | pm:fed-hike-dec | 0.65 | 78% | Fed hike in December?",
                      "FORECAST | 2026-11-20T18:00Z | pm:fed-hike-dec | 60 | 0.78 | updated the same day",
                      "FORECAST | 2026-11-20T15:00Z | KXCPI-NOV | 0.30 | 0.50 | CPI above 0.3%?",
                      "RESOLVED | 2026-11-22T15:00Z | KXCPI-NOV | NO"])
    found = fc.parse_forecasts(text)
    assert len(found) == 2 and found[-1]["mine"] == 0.60, "the day's last forecast counts once"
    asked = []

    def gamma(url):
        asked.append(url)
        return [{"closed": True, "outcomePrices": "[\"0\", \"1\"]"}]
    auto = fc.settle(found, fc.parse_resolved(text), {}, fetch=gamma, now=NOW)
    assert auto["pm:fed-hike-dec"]["outcome"] == 0 and len(asked) == 1
    s = fc.score(found, fc.parse_resolved(text), auto)
    assert s["settled"] == 2 and s["edge"] > 0, "60% and 30% were closer than 78% and 50% to two NOs"
    assert s["bets"] == 2 and s["bet_pnl_total"] > 0 and "too few" in s["verdict"]
    assert jr.parse_tags("x") == [] and jr.append_note("RESOLVED | 2026-11-22T15:00Z | X | YES", upload=False)


def test_sizes_widen_only_once_high_has_beaten_low():
    strong = dict(setup="pullback", lookalike={"n": 150, "dates": 60, "edge": 0.006, "lower": 0.002,
                                               "hit_rate": 0.58, "backed": True}, regime="up", confirmations=2)
    r = cf.score(memory={}, **strong)
    assert r["level"] == "HIGH" and r["risk_mult"] == 1.0 and "NOT CALIBRATED" in r["calibration"]
    proven = {"ideas": {"by_confidence": {"HIGH": {"ideas": 40, "avg_5d": 0.02, "t": 4.0},
                                          "LOW": {"ideas": 60, "avg_5d": -0.002, "t": -0.5}}}}
    assert cf.calibration(proven)["proven"]
    assert cf.score(memory=proven, **strong)["risk_mult"] == 2.0
    noisy = {"paper": {"by_confidence": {"HIGH": {"trades": 40, "avg_r": 0.3, "t": 1.0},
                                         "LOW": {"trades": 40, "avg_r": 0.1, "t": 0.5}}}}
    assert not cf.calibration(noisy)["proven"], "HIGH ahead, but not clearly"


def test_fresh_scored_ideas_become_practice_trades_once_and_stale_ones_never():
    tags = "\n".join([
        "IDEA | 2026-11-23T14:50Z | BUY | ABC | pullback | 100.20 | [MEDIUM] stop 95 target 110, pullback",
        "IDEA | 2026-11-23T14:50Z | BUY | DEF | momentum | 50 | [NO] stop 48 target 55, chase",
        "IDEA | 2026-11-23T14:50Z | BUY | GHI | pullback | 30 | [LOW] no stop named",
        "IDEA | 2026-11-23T10:00Z | BUY | OLD | pullback | 100 | [HIGH] stop 95 target 110"])
    ideas = jr.ideas_recent(tags, now=NOW)
    assert [i["ticker"] for i in ideas] == ["ABC", "OLD"], "NO ideas and ideas with no stop are not practised"
    http, state = FakeAlpaca(), {}
    done = paper.mirror(ideas, state, now=NOW, http=http)
    assert len(done) == 1 and "ABC" in done[0] and len(http.sent) == 1
    assert state["paper"]["done"]["OLD|2026-11-23T10:00+00:00"]["why"] == "too old"
    assert paper.mirror(ideas, state, now=NOW, http=http) == [] and len(http.sent) == 1, "once"
    evening = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
    assert paper.mirror(ideas, {}, now=evening, http=FakeAlpaca()) == [], "market orders in the session only"


class PricedAlpaca(FakeAlpaca):
    def __init__(self, price):
        super().__init__()
        self.price = price

    def __call__(self, method, url, body):
        if "/trades/latest" in url:
            return {"trade": {"p": self.price}}
        return super().__call__(method, url, body)


def test_an_idea_with_an_entry_price_waits_for_it_on_paper_too():
    """2026-11-23: GOOGL 'entry 275' and BAC 'entry 47.50' were bought on paper at 283 and 48
    the moment the keys arrived, so the practice record graded a trade the trader never planned."""
    tags = "\n".join([
        "IDEA | 2026-11-23T14:40Z | BUY | ABC | bounce | 104 | [MEDIUM] entry 100 stop 95 target 115, dip buy",
        "IDEA | 2026-11-23T14:50Z | BUY | ABC | bounce | 104.5 | [MEDIUM] entry 100 stop 95 target 115, same plan",
        "IDEA | 2026-11-23T14:50Z | BUY | XYZ | breakout | 98 | [LOW] entry above 100 stop 95 target 110"])
    ideas = jr.ideas_recent(tags, now=NOW)
    assert ideas[0]["entry"] == 100 and ideas[0]["expires"] == "2026-11-24"
    state = {}
    high = PricedAlpaca(104.0)
    assert paper.mirror(ideas, state, now=NOW, http=high) == [] and high.sent == [], "not at the entry yet"
    assert "ABC|entry 100" not in state["paper"]["done"], "still waiting, asked again next time"
    later = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    dip = PricedAlpaca(99.5)
    done = paper.mirror(ideas, state, now=later, http=dip)
    assert len(done) == 1 and "ABC" in done[0] and len(dip.sent) == 1, "the two same-entry lines trade once"
    assert paper.mirror(ideas, state, now=later, http=dip) == [] and len(dip.sent) == 1
    assert paper.entry_reached(ideas[-1], 100.8) and not paper.entry_reached(ideas[-1], 102.0), "a run-away breakout"
    gone = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
    assert paper.mirror(ideas, {}, now=gone, http=PricedAlpaca(99.5)) == [], "expired the day after next"


def test_the_watcher_never_practises_without_paper_keys():
    from stocksage import watcher

    assert watcher.practice({}, NOW, {"ideas_recent": [{"ticker": "ABC"}]}) == []
