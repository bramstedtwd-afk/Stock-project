"""The options lab must price correctly, never peek, and never crown luck.

Offline: a synthetic S&P 500, VIX family and T-bill rate stand in for the
market, and a synthetic "real" put-write index is built from a KNOWN pricing
factor so calibration can be checked against the truth.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stocksage import optionslab as ol


def synth(days=1400, seed=4, drift=0.0003, vol=0.011):
    idx = pd.bdate_range("2010-01-04", periods=days)
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, days)
    spx = 1000 * np.exp(np.cumsum(rets))
    tr = spx * np.exp(np.arange(days) * ol.DIV_YIELD / 252)
    vix = np.clip(18 + 6 * np.sin(np.arange(days) / 90) + rng.normal(0, 1, days), 10, 60)
    df = pd.DataFrame({"^GSPC": spx, "^SP500TR": tr, "^VIX": vix, "^VIX9D": vix * 0.95,
                       "^VIX3M": vix * 1.05, "^IRX": np.full(days, 2.0)}, index=idx)
    return df


@pytest.fixture(scope="module")
def df():
    return synth()


@pytest.fixture(scope="module")
def mkt(df):
    return ol.Market.from_frame(df)


# ------------------------------------------------------------ pricing

def test_put_call_parity_holds():
    S, K, T, r, q, v = 100.0, 95.0, 0.25, 0.03, 0.015, 0.22
    c, p = ol.bs(S, K, T, r, q, v, "C"), ol.bs(S, K, T, r, q, v, "P")
    assert c - p == pytest.approx(S * math.exp(-q * T) - K * math.exp(-r * T), abs=1e-9)


def test_at_expiry_an_option_is_worth_its_intrinsic_value():
    assert ol.bs(90, 100, 0, 0, 0, 0.2, "P") == 10 and ol.bs(90, 100, 0, 0, 0, 0.2, "C") == 0


def test_the_smile_prices_downside_puts_richer_and_upside_calls_cheaper():
    atm = 0.20
    assert ol.smile(atm, 100, 90, 0.08) > atm > ol.smile(atm, 100, 110, 0.08)
    assert ol.smile(atm, 100, 90, 0.08, skew=0.0) == atm


@pytest.mark.parametrize("kind,delta", [("P", 0.30), ("P", 0.10), ("C", 0.30), ("C", 0.50)])
def test_strike_for_delta_lands_on_that_delta(kind, delta):
    S, T, r, q, atm = 100.0, 30 / 365, 0.02, 0.018, 0.2
    K = ol.strike_for_delta(S, T, r, q, atm, delta, kind)
    v = ol.smile(atm, S, K, T)
    d1 = (math.log(S / K) + (r - q + 0.5 * v * v) * T) / (v * math.sqrt(T))
    got = math.exp(-q * T) * (ol._ncdf(d1) if kind == "C" else ol._ncdf(-d1))
    assert got == pytest.approx(delta, abs=0.01)
    assert (K < S) if kind == "P" and delta < 0.5 else True


def test_expiries_are_third_fridays_and_week_ends(mkt):
    monthly = mkt.dates[ol.expiries(mkt.dates, "M")]
    assert all(d.weekday() == 4 and 15 <= d.day <= 21 for d in monthly[:12])
    weekly = mkt.dates[ol.expiries(mkt.dates, "W")]
    assert all(d.weekday() == 4 for d in weekly[:20])


# ------------------------------------------------------------ simulation honesty

def test_no_strategy_ever_looks_at_the_future(df):
    """Change prices after a cut date: returns on or before it must not move."""
    cut = df.index[900]
    later = df.copy()
    rng = np.random.default_rng(1)
    later.loc[later.index > cut, "^GSPC"] *= rng.uniform(0.7, 1.3, (later.index > cut).sum())
    later.loc[later.index > cut, "^VIX"] *= 2.0
    a, b = ol.Market.from_frame(df), ol.Market.from_frame(later)
    for s in ol.candidates()[::7]:
        ra = ol.simulate(a, s, start="2010-02-01").loc[:cut]
        rb = ol.simulate(b, s, start="2010-02-01").loc[:cut]
        pd.testing.assert_series_equal(ra, rb, obj=s.name, check_exact=False, rtol=1e-12)


def test_a_strategy_that_never_trades_earns_the_t_bill_rate(mkt):
    never = ol.Strategy("never", "x", "M", ol.L2, ol.gated(ol.short_put(0.3), lambda m, i: False))
    r = ol.simulate(mkt, never, start="2010-02-01")
    assert r.mean() * 365 / (np.diff(r.index).astype("timedelta64[D]").astype(int).mean()) == \
        pytest.approx(0.02, rel=0.05)


def test_a_covered_call_switched_off_just_holds_the_index(mkt):
    off = ol.Strategy("off", "x", "M", ol.L2, ol.gated(ol.covered_call(0.3), lambda m, i: False),
                      hold_stock_when_off=True)
    r = ol.simulate(mkt, off, start="2010-02-01")
    tr = pd.Series(mkt.TR, index=mkt.dates).pct_change().reindex(r.index)
    assert (r - tr).iloc[5:].abs().max() < 1e-5      # only the 1 bp entry cost differs


def test_costs_lower_returns(mkt, monkeypatch):
    s = ol.Strategy("csp", "x", "M", ol.L2, ol.short_put(0.3))
    free = ol.simulate(mkt, s, start="2010-02-01")
    monkeypatch.setattr(ol, "COST_REL", 0.10)
    paid = ol.simulate(mkt, s, start="2010-02-01")
    assert paid.sum() < free.sum()


def test_a_cash_secured_put_cannot_lose_more_than_the_account():
    crash = synth(drift=-0.004, vol=0.03, seed=9)
    m = ol.Market.from_frame(crash)
    r = ol.simulate(m, ol.Strategy("csp", "x", "M", ol.L2, ol.short_put(0.5)), start="2010-02-01")
    assert (1 + r).prod() > 0


# ------------------------------------------------------------ calibration against the "real" index

def test_calibration_recovers_the_pricing_that_made_the_real_index(df):
    truth = 0.9
    m = ol.Market.from_frame(df)
    m.a[:] = truth
    real = (1 + ol.simulate(m, ol.Strategy("r", "x", "M", ol.L2, ol.short_put(0.50)),
                            start="2010-01-15")).cumprod()
    with_put = df.copy()
    with_put["^PUT"] = real.reindex(df.index)
    fresh = ol.Market.from_frame(with_put)
    factors = ol.calibrate(with_put, fresh)
    fitted = [v for y, v in factors.items() if 2011 <= y <= 2014]
    # What calibration promises: the replica earns what the index earned, year by year.
    # (A year's return pins the premium level only loosely, so the factor itself is
    # only required to land near the truth.)
    assert fitted and all(abs(v - truth) < 0.1 for v in fitted)
    rep = (1 + ol.simulate(fresh, ol.Strategy("r", "x", "M", ol.L2, ol.short_put(0.50)),
                           start="2010-01-15")).cumprod()
    for y in range(2011, 2015):
        got = np.log(rep[rep.index.year == y].iloc[-1] / rep[rep.index.year < y].iloc[-1])
        want = np.log(real[real.index.year == y].iloc[-1] / real[real.index.year < y].iloc[-1])
        assert got == pytest.approx(want, abs=0.01)


def test_fit_weekly_finds_the_factor_that_made_a_weekly_index(df):
    m = ol.Market.from_frame(df)
    m.weekly = 0.9
    real = (1 + ol.simulate(m, ol.Strategy("w", "x", "W", ol.L2, ol.short_put_at(1.0)), start="2010-02-01")).cumprod()
    fresh = ol.Market.from_frame(df)
    assert ol.fit_weekly(fresh, real) == pytest.approx(0.9, abs=0.02)


# ------------------------------------------------------------ switching rules and the verdict

def test_switching_rules_act_on_yesterdays_signal():
    idx = pd.bdate_range("2012-01-02", periods=300)
    lv = pd.DataFrame({"PUT": np.linspace(100, 130, 300)}, index=idx)
    tr = pd.Series(np.linspace(100, 160, 300), index=idx)
    vix = pd.Series(15.0, index=idx)
    vix.iloc[150:] = 30.0                                  # jumps on day 150
    out = ol.overlays(lv, tr, pd.Series(0.0, index=idx), vix, vix * 1.1, tr)
    rule = out["PUT when VIX > 20, else S&P 500"]
    put_r, tr_r = lv["PUT"].pct_change(), tr.pct_change()
    assert rule.iloc[150] == pytest.approx(tr_r.iloc[150]), "the day of the jump still uses yesterday's signal"
    assert rule.iloc[151] == pytest.approx(put_r.iloc[151])


def _row(**kw):
    base = dict(name="x", family="f", level="L2", dev={}, holdout={"cagr": 0.2, "sharpe": 1.0}, full={},
                alpha_dev=0.05, t_dev=3.0, p_dev=0.01, alpha_full=0.04, p_full=0.01, passed=True,
                alpha_holdout=0.04, t_holdout=2.5, robust=True, beats_spx=True)
    base.update(kw)
    return ol.Row(**base)


def test_the_bar_for_works_needs_every_part():
    assert ol.strong([_row()], model=True)
    assert not ol.strong([_row(passed=False)], model=True)
    assert not ol.strong([_row(t_holdout=1.5)], model=True)
    assert not ol.strong([_row(beats_spx=False)], model=True)
    assert not ol.strong([_row(robust=False)], model=True), "a smile-dependent model result is not a result"
    assert ol.strong([_row(robust=None)], model=False), "real records need no smile check"


def test_a_short_run_reports_honestly(df, monkeypatch):
    few = [ol.Strategy("csp", "put-write", "M", ol.L2, ol.short_put(0.3)),
           ol.Strategy("cc", "buy-write", "M", ol.L2, ol.covered_call(0.3), hold_stock_when_off=True)]
    res = ol.run(df, None, draws=50, strategies=few, split="2013-01-01", calibrate_prices=False)
    text = "\n".join(ol.describe(res))
    assert "2 strategies" in text and "RESULT" in text and "S&P 500" in text
    for r in res["rows"]:
        assert (r.holdout is not None) == r.passed, "holdout only for survivors"


def test_cboe_files_are_parsed_without_the_network():
    csv = "DATE,PUT\n01/02/2020,100.0\n01/03/2020,101.5\n"
    got = ol.load_cboe(fetch=lambda url: csv.replace("PUT", url.split("/")[-1].split("_")[0]))
    assert got.loc["2020-01-03", "PUT"] == 101.5 and "BXMD" in got


def test_the_command_is_wired(monkeypatch, tmp_path, capsys, df):
    from stocksage import cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(ol, "load_data", lambda *a, **k: df)
    monkeypatch.setattr(ol, "load_cboe", lambda *a, **k: pd.DataFrame(index=df.index))
    monkeypatch.setattr(ol, "load_funds", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(ol, "candidates", lambda: [ol.Strategy("csp", "x", "M", ol.L2, ol.short_put(0.3))])
    real_run = ol.run
    monkeypatch.setattr(ol, "run", lambda d, c, draws=2000: real_run(d, None, draws=40, calibrate_prices=False))
    assert cli.main(["options", "--draws", "40"]) == 0
    assert "OPTIONS LAB" in capsys.readouterr().out


def test_an_option_stops_moving_once_it_has_expired():
    """The last expiry in the data must settle; an expired option is cash, not a live bet."""
    mkt = ol.Market.from_frame(synth(days=1410))          # data runs past the last expiry
    s = ol.Strategy("csp", "x", "M", ol.L2, ol.short_put(0.5))
    r = ol.simulate(mkt, s, start="2010-02-01")
    last_exp = mkt.dates[ol.expiries(mkt.dates, "M")[-1]]
    after = r[r.index > last_exp]
    assert len(after) > 3 and after.abs().max() < 0.001, "only T-bill interest after the final expiry"
