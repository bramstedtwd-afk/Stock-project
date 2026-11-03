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
