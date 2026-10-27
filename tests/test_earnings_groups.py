"""Earnings drift and groups labs: timing, no look-ahead, honest verdicts. Offline."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocksage import earnings as er
from stocksage import groups as gr

DAYS = pd.bdate_range("2010-01-04", periods=900)


def prices(cols, seed=1, drift=0.0003):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({c: 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, len(DAYS)))) for c in cols},
                        index=DAYS)


# ------------------------------------------------------------ earnings: timing

def test_an_after_close_report_moves_the_next_session():
    after = pd.Timestamp("2010-03-03 16:05", tz="America/New_York")
    before = pd.Timestamp("2010-03-03 07:00", tz="America/New_York")
    unknown = pd.Timestamp("2010-03-03 00:00", tz="America/New_York")
    i_after, i_before = er.reaction_day(after, DAYS), er.reaction_day(before, DAYS)
    assert DAYS[i_before] == pd.Timestamp("2010-03-03") and DAYS[i_after] == pd.Timestamp("2010-03-04")
    assert er.reaction_day(unknown, DAYS) == i_after, "an unknown time is treated as after the close"


def test_reports_before_a_company_joined_the_index_are_ignored():
    px = prices(["AAA", "SPY"])
    uni = pd.DataFrame({"ticker": ["AAA"], "sector": ["Energy"], "industry": ["x"], "added": ["2011-01-01"]})
    raw = pd.DataFrame({"ticker": ["AAA", "AAA"], "when": ["2010-06-01 07:00:00-04:00", "2011-06-01 07:00:00-04:00"],
                        "surprise": [5.0, 5.0], "estimate": [1, 1], "reported": [1.05, 1.05]})
    ev = er.build_events(raw, uni, px)
    assert list(ev["date"].dt.year) == [2011]


def test_surprise_ranks_use_only_earlier_reports():
    n = 200
    ev = pd.DataFrame({"date": pd.bdate_range("2012-01-02", periods=n), "surprise": np.arange(n, dtype=float)})
    r = er.past_rank(ev, "surprise")
    assert np.isnan(r.iloc[10]), "needs 50 earlier reports"
    assert r.iloc[150] == 1.0, "bigger than every earlier one"
    later = ev.copy()
    later.loc[180:, "surprise"] = -999
    assert er.past_rank(later, "surprise").iloc[:180].equals(r.iloc[:180])


def test_the_position_starts_the_session_after_the_reaction():
    px = prices(["AAA", "SPY"])
    px.loc[DAYS[101], "AAA"] = px.loc[DAYS[100], "AAA"] * 1.5          # reaction day 101 jump
    ev = pd.DataFrame({"ticker": ["AAA"], "day": [101], "date": [DAYS[101]], "sector": ["x"]})
    port = er.portfolio(ev, pd.Series([True]), px, hold=5, cash=pd.Series(0.0, index=DAYS))
    assert abs(port.iloc[101]) < 1e-12 and abs(port.iloc[102]) < 1e-12, "the jump itself is not captured"
    assert port.iloc[103] == pytest.approx(px["AAA"].iloc[103] / px["AAA"].iloc[102] - 1)
    assert (port.iloc[103:108] != 0).all() and port.iloc[108] == 0


def test_a_planted_drift_is_found_and_noise_is_not():
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2004-01-02", "2020-12-31")
    tick = [f"T{i}" for i in range(40)]
    px = pd.DataFrame({t: 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, len(days)))) for t in tick},
                      index=days)
    px["SPY"] = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(days))))
    rows = []
    for t in tick:
        for i in range(60, len(days) - 80, 63):
            s = rng.normal(0, 5)
            rows.append({"ticker": t, "day": i, "date": days[i], "sector": "S", "surprise": s,
                         "reaction": rng.normal(0, 0.03)})
            if s > 4:                                   # big beats drift up 6% over 40 sessions
                px.iloc[i + 2:i + 42, px.columns.get_loc(t)] *= np.linspace(1.0015, 1.06, 40)
    ev = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    cash = pd.Series(0.00005, index=days)
    sig = [er.Signal("big beat", "all", 40, lambda e: e["surprise_rank"] >= 0.8),
           er.Signal("noise", "all", 40, lambda e: e["reaction_rank"] >= 0.8)]
    res = er.run(ev, px, cash, draws=100, split="2013-01-01", start="2005-01-01", sigs=sig)
    by = {r.name: r for r in res["rows"]}
    assert by["big beat"].alpha_dev > 0.03 and by["big beat"].p_dev < 0.05
    # Random picks also catch some drifting stocks, so compare the size of the effect.
    assert by["big beat"].alpha_dev > by["noise"].alpha_dev + 0.03
    assert "EARNINGS DRIFT" in "\n".join(er.describe(res))


# ------------------------------------------------------------ groups

@pytest.fixture(scope="module")
def gpx():
    cols = gr.all_tickers()
    px = prices(cols, seed=3)
    px.index = pd.bdate_range("2006-01-02", periods=len(px))
    return px


def test_no_group_rule_looks_at_the_future(gpx):
    cut = gpx.index[600]
    later = gpx.copy()
    later.loc[later.index > cut] *= np.random.default_rng(2).uniform(0.5, 1.5, later.loc[later.index > cut].shape)
    for r in (gr.group_rules() + gr.calendar_rules())[::5]:
        a, b = r.build(gpx).loc[:cut], r.build(later).loc[:cut]
        cols = a.columns.union(b.columns)
        a, b = a.reindex(columns=cols, fill_value=0.0), b.reindex(columns=cols, fill_value=0.0)
        a, b = a.loc[:, (a != 0).any() | (b != 0).any()], b.loc[:, (a != 0).any() | (b != 0).any()]
        pd.testing.assert_frame_equal(a, b, obj=r.name)


def test_turn_of_month_holds_the_last_session_and_first_three():
    idx = pd.bdate_range("2015-01-01", "2015-03-31")
    tom = gr.turn_of_month(idx)
    pos = np.flatnonzero(tom.to_numpy()) + 1                          # weights earn the next session
    held_returns = idx[pos[pos < len(idx)]]
    feb = [d for d in held_returns if d.month == 2]
    assert [d.day for d in feb][:3] == [2, 3, 4] and pd.Timestamp("2015-01-30") in held_returns


def test_sell_in_may_is_out_from_may_to_october():
    idx = pd.bdate_range("2015-01-01", "2015-12-31")
    s = gr.sell_in_may(idx)
    assert not s[pd.Timestamp("2015-07-15")] and s[pd.Timestamp("2015-12-15")]


def test_the_session_before_a_holiday_is_found():
    idx = pd.bdate_range("2015-06-29", "2015-07-10").drop(pd.Timestamp("2015-07-03"))
    pre = gr.pre_holiday(idx)
    # Weight set at the close of Jul 1 earns Jul 2, the session before the Jul 3 holiday.
    assert pre[pd.Timestamp("2015-07-01")] and pre.sum() == 1


def test_groups_run_judges_against_the_group_itself(gpx):
    rules = gr.group_rules()[:3] + gr.calendar_rules()[:2]
    res = gr.run(gpx, draws=50, split="2008-06-01", start="2007-01-02", rule_list=rules)
    assert res["n"] == 5 and all((r.holdout is not None) == r.passed for r in res["rows"])
    assert "GROUPS LAB" in "\n".join(gr.describe(res))
