"""The drivers lab must only use what was known at the time, and must not let
'why it moved' masquerade as 'what comes next'. Offline throughout."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocksage import drivers as dv


def cal(n=3000):
    return pd.bdate_range("2000-01-03", periods=n)


def monthly(values, start="2000-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"))


# ------------------------------------------------------------ publication lags

def test_a_monthly_figure_is_invisible_until_it_is_published():
    raw = pd.DataFrame({"UNRATE": monthly([4.0, 5.0, 6.0])})
    known = dv.as_known(raw, pd.bdate_range("2000-01-03", "2000-04-28"))["UNRATE"]
    lag = dv.BY_ID["UNRATE"].lag_days
    feb_obs_public = pd.Timestamp("2000-02-01") + pd.Timedelta(days=lag)
    assert known[known.index < feb_obs_public].dropna().max() == 4.0, "February's figure leaked early"
    assert known[known.index >= feb_obs_public].iloc[0] == 5.0


def test_as_published_figures_replace_revised_ones_when_given():
    """The revised history says 'bad'; what was published at the time said 'fine'.
    The test must see what was published."""
    calendar = pd.bdate_range("2005-01-03", "2005-06-30")
    raw = pd.DataFrame({"INDPRO": monthly([100.0] * 6, "2005-01-01")})
    rt = pd.DataFrame({"INDPRO": [101.0, 102.0], "INDPRO__g": [0.01, 0.02]},
                      index=pd.to_datetime(["2005-02-15", "2005-03-15"]))
    read = dv.readings(raw, calendar, rt)["INDPRO"]          # growth series: reads __g
    assert np.isnan(read.loc["2005-02-15"]), "published on the 15th, usable from the 16th"
    assert read.loc["2005-02-16"] == pytest.approx(0.01) and read.loc["2005-03-16"] == pytest.approx(0.02)


def test_alfred_vintages_are_parsed_without_the_network():
    def fake(url):
        dates = url.split("vintage_date=")[1].split(",")
        head = "observation_date," + ",".join(f"INDPRO_{d.replace('-', '')}" for d in dates)
        rows = [f"2004-{m:02d}-01," + ",".join(str(100 + m) for _ in dates) for m in range(1, 7)]
        return head + "\n" + "\n".join(rows) + "\n"

    rt = dv.load_realtime(fetch=fake, ids=["INDPRO"], start="2005-01-01", end="2005-03-01")
    assert len(rt) == 5 and rt["INDPRO"].iloc[0] == 106.0
    assert rt["INDPRO__g"].iloc[0] == pytest.approx(106 / 103 - 1)


def test_fred_csv_is_parsed_without_the_network():
    got = dv.load_fred(fetch=lambda url: "observation_date,X\n2020-01-01,1.5\n2020-02-01,.\n", ids=["UNRATE"])
    assert got["UNRATE"].dropna().tolist() == [1.5]


# ------------------------------------------------------------ no look-ahead in the rules

def test_no_rule_ever_looks_at_the_future():
    rng = np.random.default_rng(0)
    calendar = cal()
    read = pd.DataFrame({sid: rng.normal(size=len(calendar)).cumsum() for sid in ("BAA10Y", "DGS10", "ICSA")},
                        index=calendar)
    vix = pd.Series(rng.uniform(10, 40, len(calendar)), index=calendar)
    cut = calendar[2200]
    later = read.copy()
    later.loc[later.index > cut] *= -5
    vix2 = vix.copy()
    vix2[vix2.index > cut] = 80
    a, b = dv.rules(read, vix), dv.rules(later, vix2)
    for ra, rb in zip(a, b):
        pd.testing.assert_series_equal(ra.signal.loc[:cut], rb.signal.loc[:cut], obj=ra.name)


def test_a_signal_is_acted_on_the_day_after():
    calendar = cal(1500)
    x = pd.Series(0.0, index=calendar)
    x.iloc[1200:] = 10.0                                     # credit spread blows out on day 1200
    rule = next(r for r in dv.rules(pd.DataFrame({"BAA10Y": x}), None)
                if r.name.startswith("S&P 500, out when credit spread") and "riskiest" in r.name)
    assert bool(rule.signal.iloc[1200]) is True and bool(rule.signal.iloc[1201]) is False


# ------------------------------------------------------------ explain

def test_explain_finds_the_force_that_actually_moved_the_market():
    rng = np.random.default_rng(3)
    days = pd.bdate_range("1998-01-01", "2020-12-31")
    spread = pd.Series(np.cumsum(rng.normal(0, 0.02, len(days))) + 2, index=days)
    noise = pd.Series(np.cumsum(rng.normal(0, 0.02, len(days))) + 4, index=days)
    raw = pd.DataFrame({"BAA10Y": spread, "DGS10": noise})
    m_spread = spread.resample("ME").last().diff()
    monthly_ret = -0.05 * m_spread + rng.normal(0, 0.002, len(m_spread))
    level = (1 + monthly_ret.fillna(0)).cumprod()
    tr = level.reindex(days, method="bfill").ffill()
    ex = dv.explain(raw, tr, start="1999-01-01")
    assert ex.r2 > 0.8
    top = max(ex.betas, key=lambda k: abs(ex.betas[k]))
    assert "credit spread" in top and ex.betas[top] < 0
    m = ex.attribution.index[-1]
    assert "credit spread" in ex.month_story(m, 0.01)


# ------------------------------------------------------------ the verdict

def _row(**kw):
    base = dict(name="x", base="put-write", dev={}, full={}, alpha_dev=0.05, t_dev=3, p_dev=0.01, p_full=0.01,
                passed=True, time_in=0.6, holdout={"sharpe": 1, "maxdd": -0.1}, alpha_holdout=0.03,
                t_holdout=2.3, safer=True)
    base.update(kw)
    return dv.Row(**base)


def test_the_bar_rises_with_the_number_of_survivors_checked():
    one = [_row()]
    six = [_row(name=str(i)) for i in range(6)]
    assert dv.holdout_bar(one) == pytest.approx(1.96, abs=0.01)
    assert dv.holdout_bar(six) == pytest.approx(2.64, abs=0.01)
    assert dv.strong(one) and not dv.strong(six), "t 2.3 is enough alone, not as the best of six"


def test_safer_and_passed_are_both_required():
    assert not dv.strong([_row(safer=False)])
    assert not dv.strong([_row(passed=False)])


def test_the_judge_spends_the_holdout_only_on_survivors():
    rng = np.random.default_rng(5)
    days = pd.bdate_range("1997-01-01", "2016-12-30")
    tr = pd.Series((1 + rng.normal(0.0004, 0.01, len(days))).cumprod(), index=days)
    put = pd.Series(rng.normal(0.0002, 0.006, len(days)), index=days)
    cash = pd.Series(0.00008, index=days)
    sig = pd.Series(True, index=days)
    rl = [dv.Rule("always in", "S&P 500", sig, "cash"), dv.Rule("put always", "put-write", sig, "cash")]
    res = dv.judge({"S&P 500": tr.pct_change(), "put-write": put}, rl, tr, cash, split="2008-01-01", draws=50)
    for r in res["rows"]:
        assert (r.holdout is not None) == r.passed
    assert "RESULT" in "\n".join(dv.describe(None, res))


def test_context_lines_are_plain_and_never_orders():
    days = pd.date_range("2012-01-01", "2026-10-01", freq="D")
    raw = pd.DataFrame({"BAA10Y": np.linspace(1, 3, len(days))}, index=days)
    lines = dv.context_lines(raw, today=pd.Timestamp("2026-10-01"), ids=["BAA10Y"])
    assert lines and lines[0].startswith("DRIVER  credit spread") and "riskier than usual" in lines[0]
    assert not any(w in lines[0] for w in ("SELL", "BUY"))
