"""The red-day backtester must count every dollar the way a broker would.

Each test builds a price path small enough to work out by hand, or perturbs
the future to prove no decision ever saw it. Offline and synthetic.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stocksage import dipbuy as db


def frame(closes, opens=None, rate=0.0, start="2020-01-01", spy=None):
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({"close": np.asarray(closes, float),
                       "open": np.asarray(opens if opens is not None else closes, float),
                       "rate": rate}, index=idx)
    if spy is not None:
        df["spy_close"] = np.asarray(spy, float)
        df["spy_open"] = np.asarray(spy, float)
    return df


def red_mask(df):
    return db.red_any(df["close"]).fillna(False).to_numpy()


def synth(days=3000, seed=5, drift=0.0004, vol=0.012):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, days)))
    opens = close * (1 + rng.normal(0, vol / 3, days))
    return frame(close, opens, rate=0.02, start="2005-01-03", spy=close * 1.0)


# ------------------------------------------------------------ hand-checked trades

def test_each_buy_sells_on_the_first_green_day_it_is_up_the_target():
    #           0    1(red) 2     3      4(red) 5
    df = frame([100, 99, 100, 102, 101, 104])
    r = db.simulate(df, red_mask(df), db.SELL_BY_NAME["lot2"], cost=0.0)
    # bought at 99 -> +1.0% on day 2 (not enough), +3.0% on day 3 (sold)
    # bought at 101 -> +2.97% on day 5 (sold)
    assert r.buys == 2 and r.sells == 2
    assert r.new_money == 1000
    assert r.final_value == pytest.approx(500 * 102 / 99 + 500 * 104 / 101, abs=0.01)
    assert r.win_rate == 1.0 and r.open_lots == 0
    assert r.longest_wait == 2          # the 99 lot waited two days


def test_a_lot_is_never_sold_on_the_day_it_was_bought():
    # a time exit of 0 days would sell at once; time20 must hold, and the
    # lot bought on day 1 must still be open at the end.
    df = frame([100, 90, 95])
    r = db.simulate(df, np.array([False, True, False]), db.SELL_BY_NAME["time20"], cost=0.0)
    assert r.sells == 0 and r.open_lots == 1


def test_target_needs_a_green_day():
    # lot from day 1 is +5% on day 3 but day 3 is red; sold on green day 4.
    df = frame([100, 95, 101, 99.75, 100])
    mask = np.array([False, True, False, False, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot3"], cost=0.0)
    assert r.sells == 1
    assert r.final_value == pytest.approx(500 * 101 / 95, abs=0.01)   # sold day 2 (green, +6.3%)
    df2 = frame([100, 95, 94, 98.5, 98.0])   # +3.7% first reached on day 3 (green)
    r2 = db.simulate(df2, mask, db.SELL_BY_NAME["lot3"], cost=0.0)
    assert r2.final_value == pytest.approx(500 * 98.5 / 95, abs=0.01)


def test_big_green_day_sells_everything_but_winners_only_keeps_losers():
    #              buy@100 (red)  buy@90 (red)   +2.2% day
    df = frame([105, 100, 90, 92])
    mask = np.array([False, True, True, False])
    everything = db.simulate(df, mask, db.SELL_BY_NAME["green2"], cost=0.0)
    assert everything.sells == 2 and everything.open_lots == 0
    winners = db.simulate(df, mask, db.SELL_BY_NAME["green2w"], cost=0.0)
    assert winners.sells == 1 and winners.open_lots == 1       # the 100 lot is still underwater


def test_stop_loss_fires_on_any_day():
    df = frame([100, 99, 95, 93])
    mask = np.array([False, True, False, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot5s5"], cost=0.0)
    assert r.sells == 1 and r.win_rate == 0.0
    assert r.avg_closed == pytest.approx(93 / 99 - 1)


def test_whole_position_target_uses_the_average_cost():
    df = frame([100, 99, 90, 95, 99.2, 100])
    mask = np.array([False, True, True, False, False, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["pos5"], cost=0.0)
    # $1,000 in at 99 and 90; worth >= $1,050 first at 99.2 (a green day)
    shares = 500 / 99 + 500 / 90
    assert shares * 95 < 1050 <= shares * 99.2
    assert r.sells == 2 and r.final_value == pytest.approx(shares * 99.2, abs=0.01)


def test_next_open_fills_at_tomorrows_open():
    df = frame([100, 99, 103, 104], opens=[100, 100, 101, 105])
    mask = np.array([False, True, False, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot2"], cost=0.0, fill="next_open")
    # signal day 1 -> bought at day-2 open 101; day 2 close 103 (+1.98%) not yet;
    # day 3 close 104 (+2.97%) -> sold at day-4 open... there is none, so still open.
    assert r.buys == 1 and r.sells == 0
    assert r.final_value == pytest.approx(500 / 101 * 104, abs=0.01)


def test_costs_are_charged_both_ways():
    df = frame([100, 99, 110])
    mask = np.array([False, True, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot2"], cost=0.001)
    assert r.final_value == pytest.approx(500 / (99 * 1.001) * 110 * 0.999, abs=0.01)


def test_idle_cash_earns_the_bill_rate():
    closes = [100, 99, 110] + [110] * 250
    df = frame(closes, rate=0.05)
    mask = np.zeros(len(closes), bool)
    mask[1] = True
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot2"], cost=0.0)
    days = (df.index[-1] - df.index[2]).days
    assert r.final_value == pytest.approx(500 * 110 / 99 * (1 + 0.05 / 365) ** days, abs=0.01)


# ------------------------------------------------------------ fairness

def test_every_sell_rule_puts_in_the_same_money_on_the_same_days():
    df = synth()
    mask = red_mask(df)
    money = {db.simulate(df, mask, rule).new_money for rule in db.SELL_RULES}
    assert money == {mask.sum() * db.STAKE}


def test_proceeds_parked_in_the_same_fund_match_never_selling():
    df = synth()
    mask = red_mask(df)
    hold = db.simulate(df, mask, db.SELL_BY_NAME["hold"], cost=0.0)
    flip = db.simulate(df, mask, db.SELL_BY_NAME["lot2"], cost=0.0, dest="spy")
    assert flip.sells > 100
    assert flip.final_value == pytest.approx(hold.final_value, rel=1e-9)


def test_proceeds_wait_in_cash_until_spy_exists():
    closes = [100, 99, 110, 110, 110]
    spy = [np.nan, np.nan, np.nan, 50, 55]
    df = frame(closes, spy=spy)
    mask = np.array([False, True, False, False, False])
    r = db.simulate(df, mask, db.SELL_BY_NAME["lot2"], cost=0.0, dest="spy")
    assert r.final_value == pytest.approx(500 * 110 / 99 / 50 * 55, abs=0.01)


# ------------------------------------------------------------ no look-ahead

@pytest.mark.parametrize("fill", ["close", "next_open"])
@pytest.mark.parametrize("rule", ["lot5", "green2", "green2w", "pos10", "lot5s5", "lot10t60"])
@pytest.mark.parametrize("buy", list(db.BUY_RULES))
def test_nothing_before_a_date_depends_on_prices_after_it(fill, rule, buy):
    """Scramble every price after a cut: the account up to the cut must not move."""
    df = synth(days=1500)
    cut = 900
    changed = df.copy()
    rng = np.random.default_rng(11)
    for col in ("close", "open", "spy_close", "spy_open"):
        changed.iloc[cut + 1:, changed.columns.get_loc(col)] *= rng.uniform(0.6, 1.4, len(df) - cut - 1)
    fn = db.BUY_RULES[buy][1]
    runs = []
    for frame_ in (df, changed):
        mask = fn(frame_["close"]).fillna(False).to_numpy()
        res = db.simulate(frame_, mask, db.SELL_BY_NAME[rule], fill=fill, dest="spy", curve=True)
        runs.append([w for w in res.wealth if w[0] < str(df.index[cut - 1].date())])
    assert len(runs[0]) > 20
    assert runs[0] == runs[1]


# ------------------------------------------------------------ helpers

def test_irr_matches_a_known_answer():
    assert db.irr(np.array([0.0]), np.array([100.0]), 2.0, 121.0) == pytest.approx(0.10, abs=1e-7)
    two = db.irr(np.array([0.0, 1.0]), np.array([100.0, 100.0]), 2.0, 100 * 1.05 ** 2 + 100 * 1.05)
    assert two == pytest.approx(0.05, abs=1e-7)


def test_buy_rules_flag_the_right_days():
    close = pd.Series([100, 99, 98, 99.5, 97, 96.9],
                      index=pd.bdate_range("2020-01-01", periods=6))
    assert db.red_any(close).tolist() == [False, True, True, False, True, True]
    assert db.red_1(close).tolist() == [False, True, True, False, True, False]
    assert db.red_2(close).tolist() == [False, False, False, False, True, False]
    assert db.red_streak2(close).tolist() == [False, False, True, False, False, True]


def test_correction_rule_needs_a_ten_percent_fall_from_the_high():
    close = pd.Series(np.r_[np.linspace(100, 120, 80), [119, 110, 107.9, 107.5]],
                      index=pd.bdate_range("2020-01-01", periods=84))
    flags = db.red_in_correction(close).tolist()
    assert flags[-4:] == [False, False, True, True]     # 107.9 is just under 108


def test_random_masks_keep_the_number_of_buys():
    masks = db.random_masks(37, 500, draws=5)
    assert all(m.sum() == 37 and not m[0] for m in masks)
    assert not np.array_equal(masks[0], masks[1])


def test_run_reports_every_combination_and_sane_benchmarks():
    df = synth(days=2700)
    prices = {"close": pd.DataFrame({"SPY": df["close"]}), "open": pd.DataFrame({"SPY": df["open"]}),
              "rate": df["rate"]}
    res = db.run(prices, ["SPY"], draws=5)
    r = res["tickers"]["SPY"]
    assert len(r["combos"]) == len(db.BUY_RULES) * len(db.SELL_RULES)
    assert set(r["eras"]) == {"2010s"}       # 2005-2015: only 2010s has 5+ years
    assert all(math.isfinite(c["irr"]) for c in r["combos"])
    text = "\n".join(db.describe(res))
    assert "SPY" in text and "Taxes not included" in text


def test_waiting_for_signals_is_charged_for_the_wait():
    # Price only rises: waiting in cash for red days can only cost money.
    closes = list(np.linspace(100, 200, 300))
    closes[150] = closes[149] * 0.99            # one red day mid-way
    df = frame(closes, rate=0.0)
    every = db.paced(df, np.ones(len(df), bool), cost=0.0)
    waits = db.paced(df, red_mask(df), cost=0.0)
    assert every["new_money"] == waits["new_money"] == 500 * len(df)
    assert waits["buys"] == 1 and waits["cash_left"] == pytest.approx(500 * 149)
    assert waits["final_value"] < every["final_value"]
    assert waits["irr"] < every["irr"]
