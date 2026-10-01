"""The research harness must not manufacture winners.

The classic ways a backtest lies are peeking at the future, ignoring costs,
crowning the best of many tries, and tuning on the data used to judge. Each is
attacked here with synthetic prices, offline.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocksage import research as rs


def synth_prices(days=4500, seed=3, vol=0.01):
    """A world with no edge anywhere: every asset earns exactly the cash rate."""
    drift = 0.00008 - 0.5 * vol ** 2
    idx = pd.bdate_range("2003-01-02", periods=days)
    rng = np.random.default_rng(seed)
    data = {t: 100 * np.exp(np.cumsum(rng.normal(drift, vol, days))) for t in rs.ALL_TICKERS}
    data[rs.CASH] = 100 * np.exp(np.cumsum(np.full(days, 0.00008)))
    return pd.DataFrame(data, index=idx)


@pytest.fixture(scope="module")
def prices():
    return synth_prices()


# ------------------------------------------------------------ no look-ahead

def test_no_candidate_rule_ever_looks_at_the_future(prices):
    """Perturb every price after a cut date: weights on or before it must not move."""
    cut = prices.index[2800]
    changed = prices.copy()
    rng = np.random.default_rng(99)
    changed.loc[changed.index > cut] *= rng.uniform(0.5, 1.5, size=(len(changed.loc[changed.index > cut]), changed.shape[1]))
    for s in rs.candidates() + rs.benchmarks():
        a = s.weights(prices).loc[:cut]
        b = s.weights(changed).loc[:cut]
        pd.testing.assert_frame_equal(a, b, check_exact=False, obj=s.name)


def test_a_weight_set_today_earns_tomorrows_return_not_todays(prices):
    px = prices.copy()
    day = px.index[3300]
    px.loc[day, rs.EQUITY] = px[rs.EQUITY].iloc[3299] * 1.5     # +50% on `day` only
    target = pd.DataFrame(0.0, index=px.index, columns=[rs.EQUITY])
    target.loc[day, rs.EQUITY] = 1.0                           # decided at that close
    r = rs.simulate(px, target, cost=0.0)
    assert abs(r.loc[day]) < 0.05, "a position opened at the close cannot earn that day's jump"


def test_costs_come_off_exactly_by_turnover(prices):
    target = pd.DataFrame({rs.EQUITY: np.where(np.arange(len(prices)) % 40 < 20, 1.0, 0.0)}, index=prices.index)
    free = rs.simulate(prices, target, cost=0.0)
    paid = rs.simulate(prices, target, cost=0.001)
    held = target.reindex(prices.index).shift(1).fillna(0.0).loc[rs.EVAL_START:]
    flips = held[rs.EQUITY].diff().abs().fillna(0.0)
    # Switching SPY<->cash also moves the cash leg, so turnover is twice the SPY change.
    assert (free - paid).sum() == pytest.approx(0.001 * 2 * flips.sum(), rel=1e-6)


def test_holding_an_asset_with_no_price_is_an_error_not_a_zero(prices):
    px = prices.copy()
    px.loc[px.index[:3100], "GLD"] = np.nan
    target = pd.DataFrame({"GLD": 1.0}, index=px.index)
    with pytest.raises(ValueError, match="no price"):
        rs.simulate(px, target)


def test_weights_never_exceed_the_whole_account(prices):
    for s in rs.candidates():
        w = s.weights(prices).loc[rs.EVAL_START:]
        assert (w.sum(axis=1) <= 1.0 + 1e-9).all(), s.name
        assert (w.to_numpy() >= -1e-12).all(), s.name


# ------------------------------------------------------------ the rules do what they say

def test_the_trend_rule_steps_aside_after_a_crash():
    idx = pd.bdate_range("2003-01-02", periods=900)
    spy = np.concatenate([np.linspace(100, 200, 500), np.linspace(200, 100, 100), np.full(300, 100.0)])
    px = pd.DataFrame({t: np.linspace(100, 120, 900) for t in rs.ALL_TICKERS}, index=idx)
    px[rs.EQUITY] = spy
    w = rs.trend_spy(px, 200)
    assert w[rs.EQUITY].iloc[450] == 1.0 and w[rs.EQUITY].iloc[-1] == 0.0


def test_rotation_picks_the_strongest_and_the_cash_filter_can_sit_out():
    idx = pd.bdate_range("2003-01-02", periods=600)
    px = pd.DataFrame({t: np.full(600, 100.0) for t in rs.ALL_TICKERS}, index=idx)
    px["XLK"] = np.linspace(100, 300, 600)
    px[rs.CASH] = np.linspace(100, 110, 600)
    w = rs.rotation(px, rs.SECTORS, 1, 6, absolute=True)
    assert w["XLK"].iloc[-1] == 1.0 and w.drop(columns="XLK").iloc[-1].sum() == 0.0
    flat = px.copy()
    flat["XLK"] = 100.0                      # nothing beats cash -> sit in cash
    assert rs.rotation(flat, rs.SECTORS, 1, 6, absolute=True).iloc[-1].sum() == 0.0
    assert rs.rotation(flat, rs.SECTORS, 1, 6, absolute=False).iloc[-1].sum() == 1.0


# ------------------------------------------------------------ the statistics

def test_the_reality_check_does_not_crown_a_winner_out_of_pure_noise():
    """With 40 noise candidates the best always looks good alone; the corrected
    p-value must still fire at roughly its nominal rate, not 40x as often."""
    hits = 0
    trials = 60
    for seed in range(trials):
        d = np.random.default_rng(seed).normal(0, 0.01, size=(1200, 40))
        _, p = rs.reality_check(d, draws=200, seed=seed)
        hits += p.min() < 0.05
    assert hits / trials < 0.15


def test_the_reality_check_finds_a_real_edge_in_a_crowd_of_noise():
    rng = np.random.default_rng(5)
    d = rng.normal(0, 0.01, size=(2500, 40))
    d[:, 7] += 0.0008                        # ~20% a year of genuine alpha
    t, p = rs.reality_check(d, draws=300)
    assert p.argmin() == 7 and p[7] < 0.05 and (np.delete(p, 7) > 0.05).mean() > 0.9


def test_a_naive_p_value_would_have_been_fooled_by_the_same_noise():
    """The reason the correction exists: the best of many noise series has a big t."""
    d = np.random.default_rng(1).normal(0, 0.01, size=(1200, 60))
    t = d.mean(axis=0) / d.std(axis=0) * np.sqrt(len(d))
    assert t.max() > 2.0


def test_alpha_removes_what_spy_alone_explains(prices):
    f = rs.factor_returns(prices).dropna()
    cash = pd.Series(0.0, index=f.index)
    half_spy = 0.5 * f["spy"]                # half of SPY, nothing else
    a, betas = rs.alpha_series(half_spy, f, cash)
    assert betas[0] == pytest.approx(0.5) and abs(a.mean()) < 1e-12


def test_owning_bonds_and_gold_is_not_alpha(prices):
    """A fixed bag of the asset classes earns their premia without any skill, so
    it must show no alpha against the passive mix (it would against SPY alone)."""
    f = rs.factor_returns(prices).dropna()
    cash = pd.Series(0.0, index=f.index)
    a, betas = rs.alpha_series(f["mix"] * 1.0, f, cash)
    assert betas[1] == pytest.approx(1.0) and abs(a.mean()) < 1e-12


# ------------------------------------------------------------ the whole run

class Foresight(rs.Strategy):
    """Cheats on purpose (tomorrow's sign): the pipeline MUST flag this one."""

    def __init__(self):
        super().__init__("cheat", "test", None)

    def weights(self, p):
        up = (p[rs.EQUITY].pct_change().shift(-1) > 0).astype(float)
        return pd.DataFrame({rs.EQUITY: up})


def test_with_no_real_edge_the_holdout_is_never_touched(prices):
    res = rs.run(prices, draws=150, strategies=rs.candidates()[:12])
    assert not any(r.passed for r in res["rows"])
    assert all(r.holdout is None for r in res["rows"])
    assert "never touched" in "\n".join(rs.describe(res))


def test_a_planted_edge_passes_development_and_only_then_gets_a_holdout(prices):
    res = rs.run(prices, draws=150, strategies=rs.candidates()[:6] + [Foresight()])
    by = {r.name: r for r in res["rows"]}
    assert by["cheat"].passed and by["cheat"].holdout is not None
    assert not any(r.holdout for n, r in by.items() if n != "cheat")


def test_development_and_holdout_do_not_overlap(prices):
    res = rs.run(prices, draws=100, strategies=[Foresight()])
    assert res["split"] == rs.SPLIT
    assert res["rows"][0].t_dev != res["rows"][0].t_full


def test_the_report_states_the_family_size_and_never_hides_the_benchmarks(prices):
    res = rs.run(prices, draws=100, strategies=rs.candidates()[:5])
    text = "\n".join(rs.describe(res))
    assert "5 candidate rules" in text and "hold SPY" in text and "corrected" in text


def test_the_research_command_runs_saves_and_remembers(monkeypatch, tmp_path, capsys, prices):
    from stocksage import cli, today
    from stocksage.db import Database

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(rs, "load_prices", lambda cache=None, **k: prices)
    monkeypatch.setattr(rs, "candidates", lambda: _SIX)
    assert cli.main(["research", "--draws", "60"]) == 0
    out = capsys.readouterr().out
    assert "STRATEGY RESEARCH" in out and "hold SPY" in out
    assert (today.state_dir() / "research.json").exists()
    db = Database(tmp_path / "b.db")
    assert db.get_meta("research_at")


_SIX = rs.candidates()[:6]


def test_the_research_command_says_so_when_prices_cannot_be_fetched(monkeypatch, capsys):
    from stocksage import cli

    def down(*a, **k):
        raise RuntimeError("no price data for SPY")

    monkeypatch.setattr(rs, "load_prices", down)
    assert cli.main(["research"]) == 1
    assert "Could not get the price history" in capsys.readouterr().out
