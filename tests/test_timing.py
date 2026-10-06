"""The timing lab must not manufacture an edge.

Offline and synthetic: peeking at the future, free leverage, and crowning the
luckiest of many rules are each attacked directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocksage import timing as tm


def walk(days=3000, seed=4, drift=0.0003, vol=0.012, start="2001-01-02"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=days)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, days))), index=idx)


# ------------------------------------------------------------ no look-ahead

@pytest.mark.parametrize("rule", tm.rules(), ids=lambda r: r.name)
def test_no_rule_reads_the_future(rule):
    c = walk()
    rate = pd.Series(0.02, index=c.index)
    cut = 2000
    changed = c.copy()
    rng = np.random.default_rng(1)
    changed.iloc[cut + 1:] *= rng.uniform(0.5, 1.5, len(c) - cut - 1)
    a = pd.Series(rule.fn(c, rate), index=c.index).iloc[:cut + 1]
    b = pd.Series(rule.fn(changed, rate), index=c.index).iloc[:cut + 1]
    pd.testing.assert_series_equal(a.fillna(-9), b.fillna(-9))


def test_exposure_decided_at_a_close_earns_the_next_day():
    idx = pd.bdate_range("2020-01-01", periods=4)
    c = pd.Series([100, 110, 121, 121], index=idx, dtype=float)
    rf = pd.Series(0.0, index=idx)
    e = pd.Series([0, 1, 1, 0], index=idx, dtype=float)
    r = tm.returns_for(c, e, rf, cost=0.0)
    # in from day 1's close: misses day 1's +10%, earns day 2's +10%, then flat
    assert r.tolist() == pytest.approx([0.0, 0.0, 0.10, 0.0])


# ------------------------------------------------------------ costs

def test_leverage_pays_borrowing_and_fees():
    idx = pd.bdate_range("2020-01-01", periods=253)
    c = pd.Series(100.0, index=idx)               # the fund never moves
    rf = pd.Series(0.0, index=idx)
    r = tm.returns_for(c, pd.Series(2.0, index=idx), rf, cost=0.0)
    yearly = float((1 + r).prod() - 1)
    days = (idx[-1] - idx[0]).days
    assert yearly == pytest.approx(-(tm.BORROW_SPREAD + tm.LEVER_FEE) * days / 365, rel=0.02)


def test_idle_money_earns_bills_and_trading_costs_money():
    idx = pd.bdate_range("2020-01-01", periods=6)
    c = pd.Series(100.0, index=idx)
    rf = pd.Series(0.0001, index=idx)
    cash = tm.returns_for(c, pd.Series(0.0, index=idx), rf)
    assert cash.iloc[1:].tolist() == pytest.approx([0.0001] * 5)
    flip = tm.returns_for(c, pd.Series([1, 0, 1, 0, 1, 0], index=idx, dtype=float), rf, cost=0.001)
    assert flip.iloc[2:].lt(0.0001).all()


def test_matched_level_equalises_volatility():
    c = walk()
    rf = pd.Series(0.0, index=c.index)
    hold = tm.constant(c, 1.0, rf, 0.0)
    half = tm.constant(c, 0.5, rf, 0.0)
    assert tm.matched_level(half, hold) == pytest.approx(0.5, rel=1e-6)


# ------------------------------------------------------------ luck

def test_in_a_world_with_no_edge_nothing_passes():
    """Random walks: no rule can time them, so none may clear the luck bar."""
    prices = pd.DataFrame({f"F{i}": walk(days=3600, seed=10 + i) for i in range(6)})
    groups = {t: ("sector" if i < 3 else "industry") for i, t in enumerate(prices)}
    rate = pd.Series(0.01, index=prices.index)
    some = [r for r in tm.rules() if r.lever == 1.0][:8]
    res = tm.run(prices, rate, draws=200, rule_list=some, groups=groups)
    assert res["n_funds"] == 6
    assert not any(s["passed"] for s in res["summary"])
    assert all(s["p"] > 0.05 for s in res["summary"])
    text = "\n".join(tm.describe(res))
    assert "Reality Check" in text


def test_a_real_edge_is_found():
    """A market that trends (momentum built in) must be detected by a trend rule."""
    rng = np.random.default_rng(3)
    days = 4000
    idx = pd.bdate_range("2000-01-03", periods=days)
    prices = {}
    for k in range(4):
        state, r = 1, np.empty(days)
        for i in range(days):
            if rng.random() < 1 / 250:          # long regimes: up years, down years
                state = -state
            r[i] = rng.normal(0.0012 * state, 0.01)
        prices[f"T{k}"] = pd.Series(100 * np.exp(np.cumsum(r)), index=idx)
    prices = pd.DataFrame(prices)
    rate = pd.Series(0.0, index=idx)
    trend = [r for r in tm.rules() if r.name == "Trend 200d @1x"]
    res = tm.run(prices, rate, draws=200, rule_list=trend, groups={t: "sector" for t in prices})
    row = res["summary"][0]
    assert row["edge"] > 0.02 and row["p"] < 0.05
