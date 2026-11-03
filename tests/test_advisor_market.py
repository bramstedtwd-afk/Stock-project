"""The brief's market snapshot: indexes, sectors ranked, built from cached history. Offline."""

from __future__ import annotations

import numpy as np
import pandas as pd

from stocksage import advisor


class _Market:
    def __init__(self, drift):
        self.drift = drift

    def prefetch(self, symbols, **kw):
        pass

    def history(self, sym):
        days = pd.bdate_range("2026-01-01", periods=80)
        step = self.drift.get(sym, 0.0)
        return pd.DataFrame({"Close": 100 * np.exp(np.cumsum(np.full(80, step)))}, index=days)


class _Engine:
    def __init__(self, drift):
        self.market = _Market(drift)


def test_snapshot_reports_moves_and_ranks_sectors():
    snap = advisor.build_market_snapshot(_Engine({"SPY": 0.001, "XLE": 0.01, "XLK": -0.01}))
    spy = snap["indexes"]["SPY"]
    assert spy["name"] == "S&P 500" and abs(spy["d1"] - (np.exp(0.001) - 1)) < 1e-4
    assert spy["d5"] > spy["d1"] > 0 and spy["vs_50d"] > 0
    assert snap["leaders_5d"][0] == "XLE" and snap["laggards_5d"][-1] == "XLK"
    assert list(snap["sectors"])[0] == "XLE"


def test_snapshot_skips_symbols_without_enough_history():
    class Short(_Market):
        def history(self, sym):
            return pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.bdate_range("2026-01-01", periods=2))

    eng = _Engine({})
    eng.market = Short({})
    snap = advisor.build_market_snapshot(eng)
    assert snap["indexes"] == {} and snap["sectors"] == {}


def test_regime_reads_a_falling_tape_and_lists_the_down_market_funds():
    falling = {"SPY": -0.004, "QQQ": -0.006, "^VIX": 0.01, "SH": 0.004,
               **{s: -0.003 for s in advisor.SECTOR_SYMBOLS}, "XLP": 0.002, "XLU": 0.002}
    snap = advisor.build_market_snapshot(_Engine(falling))
    assert snap["regime"]["label"] == "down" and snap["regime"]["sectors_up_5d"] == 2
    assert snap["hedges"]["SH"]["d5"] > 0 and "SGOV" in snap["hedges"]
    rising = {"SPY": 0.003, **{s: 0.002 for s in advisor.SECTOR_SYMBOLS}, "^VIX": -0.002}
    assert advisor.build_market_snapshot(_Engine(rising))["regime"]["label"] == "up"


def test_regime_is_mixed_on_a_dip_inside_an_uptrend():
    idx = {"SPY": {"vs_50d": 0.03, "d5": -0.012}, "^VIX": {"last": 17.0, "d5": 0.05}}
    sectors = {str(i): {"d5": 0.01 if i < 6 else -0.01} for i in range(11)}
    assert advisor.market_regime(idx, sectors)["label"] == "mixed"
    assert advisor.market_regime({}, {}) == {}


def test_a_blocked_scan_keeps_the_last_good_picks_but_fresh_market_context():
    prev = {"as_of": "2026-11-02T20:00:00+00:00", "candidates": [{"ticker": "AAA"}], "focus": [{"ticker": "MCD"}],
            "avoid": [{"ticker": "ZZZ"}], "market": {"old": True}}
    blocked = {"as_of": "2026-11-03T02:00:00+00:00", "candidates": [], "focus": [], "avoid": [],
               "scan_errors": 100, "market": {"fresh": True}}
    out = advisor.carry_forward_picks(blocked, prev)
    assert out["candidates"] == [{"ticker": "AAA"}] and out["focus"] == [{"ticker": "MCD"}]
    assert out["market"] == {"fresh": True} and out["carried_over"]["from"] == prev["as_of"]
    again = advisor.carry_forward_picks(dict(blocked), out)
    assert again["carried_over"]["from"] == prev["as_of"], "a second blocked run still dates the picks honestly"
    quiet = {**blocked, "scan_errors": 3}
    assert advisor.carry_forward_picks(quiet, prev) is quiet, "few errors: an empty list is a real answer"
    assert advisor.carry_forward_picks(blocked, None) is blocked


def test_a_run_without_look_alike_history_keeps_the_last_evidence_dated():
    prev = {"as_of": "2026-11-02T20:00:00+00:00", "candidates": [{"ticker": "AAA"}],
            "evidence": {"AAA": {"buy": {"edge": 0.01, "backed": True}}}}
    fresh = {"as_of": "2026-11-03T02:00:00+00:00", "candidates": [{"ticker": "BBB"}], "scan_errors": 0, "evidence": {}}
    out = advisor.carry_forward_picks(fresh, prev)
    assert out["evidence"] == prev["evidence"] and out["evidence_from"] == prev["as_of"]
    assert out["candidates"] == [{"ticker": "BBB"}], "a working scan keeps its own picks"
    own = {**fresh, "evidence": {"BBB": {}}}
    assert advisor.carry_forward_picks(own, prev) is own


def test_attention_tickers_come_from_memory_crowd_and_earnings(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    (tmp_path / "trade_memory.json").write_text(json.dumps(
        {"open": [{"ticker": "MCD"}], "recent": [{"ticker": "CMCSA"}, {"ticker": "BTC-USD"}],
         "ideas": {"recent": [{"ticker": "PLD"}]}}))
    social = {"stocktwits_trending": [{"ticker": "PLTR", "kind": "Stock"}, {"ticker": "BTC.X", "kind": "Crypto"}],
              "reddit_surging": ["MU"]}
    live = {"earnings_today": [{"ticker": "AAPL"}], "earnings_next": [{"ticker": "AMD"}, {"ticker": "MCD"}]}
    assert advisor._attention_tickers(social, live) == ["MCD", "CMCSA", "PLD", "PLTR", "MU", "AAPL", "AMD"]
