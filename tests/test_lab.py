"""The strategy lab must not be able to crown a lucky result.

It tries several ideas, which is exactly how people fool themselves: run six
and one looks good by chance. These tests attack that, plus the usual ways a
backtest lies (look-ahead, free trading, cash months that cost nothing).
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from stocksage import lab
from stocksage.lab import Row, lab_level, run_all, run_strategy


def three_bad_months(m):
    """Regime off in months 4-6 of every year."""
    return (m % 12) not in (4, 5, 6)


def make_rows(n_months=72, n_names=30, effect=0.0, noise=0.04, seed=1,
              regime_pattern=None, spy_when_on=0.012, spy_when_off=-0.05):
    """`effect` is how much 12-1 momentum predicts next month's excess return."""
    rng = random.Random(seed)
    start = date(2020, 1, 31)
    rows = []
    for m in range(n_months):
        d = (start + timedelta(days=30 * m)).isoformat()
        on = True if regime_pattern is None else regime_pattern(m)
        spy = (spy_when_on if on else spy_when_off) + rng.gauss(0, 0.01)
        for j in range(n_names):
            mom = rng.uniform(-0.4, 1.0)
            fwd = spy + effect * mom + rng.gauss(0, noise)
            rows.append(Row(d, f"T{j}", mom, None, fwd, spy, on))
    return rows


# ------------------------------------------------------------- finds truth

def test_a_real_momentum_effect_is_found():
    r = run_strategy(make_rows(effect=0.03), "mom", lab.pick_momentum, tests_run=6)
    assert r.edge > 0.01 and r.vs_random > 0.01
    assert r.level == "earned"


def test_noise_is_never_crowned_even_though_several_ideas_are_tried():
    """Fifteen noise worlds, six ideas each: none may come out 'earned'."""
    for seed in range(15):
        rows = make_rows(effect=0.0, seed=seed)
        for r in run_all(rows):
            assert r.level != "earned", f"noise world {seed}: {r.name}"


def test_a_market_timing_filter_that_dodges_real_crashes_is_found():
    rows = make_rows(effect=0.0, regime_pattern=three_bad_months, spy_when_on=0.012, spy_when_off=-0.06)
    r = run_strategy(rows, "timing", None, regime=True, tests_run=6)
    assert r.edge > 0.008 and r.level == "earned"


def test_a_filter_that_sits_out_rallies_is_caught_as_costly():
    rows = make_rows(effect=0.0, regime_pattern=three_bad_months, spy_when_on=0.012, spy_when_off=+0.05)
    r = run_strategy(rows, "wrong-way timing", None, regime=True, tests_run=6)
    assert r.edge < 0 and r.level == "failing"


def test_a_month_in_cash_is_charged_exactly_the_rally_it_missed():
    rows = make_rows(n_months=6, regime_pattern=lambda m: False, spy_when_off=0.03)
    r = run_strategy(rows, "cash", None, regime=True)
    by_date = {}
    for row in rows:
        by_date.setdefault(row.date, row.spy_fwd)
    assert r.per_period == [-v for _, v in sorted(by_date.items())]


# ------------------------------------------------------------- honesty

def test_the_correction_gets_stricter_as_more_ideas_are_tried():
    args = dict(n=60, mean=0.0105, se=0.004, ci_low=0.001)
    assert lab_level(tests_run=1, **args) == "earned"
    assert lab_level(tests_run=20, **args) == "unproven", \
        "twenty tries should not let the same result through"


def test_earned_also_needs_the_bootstrap_interval_clear_of_zero():
    assert lab_level(60, 0.0105, 0.004, ci_low=-0.001, tests_run=1) == "unproven"
    assert lab_level(60, 0.0105, 0.004, ci_low=float("nan"), tests_run=1) == "unproven"


def test_earned_needs_enough_months_for_the_maths_to_hold():
    assert lab_level(30, 0.05, 0.001, ci_low=0.04, tests_run=1) == "unproven"


def test_failing_needs_real_evidence_too():
    assert lab_level(10, -0.05, 0.001, ci_low=-0.06, tests_run=1) == "unproven"
    assert lab_level(60, -0.02, 0.003, ci_low=-0.03, tests_run=1) == "failing"


def test_untested_ideas_elsewhere_count_toward_the_family():
    """The weekly models are tried too. Leaving them out would understate how
    many ideas there were — the very number the correction needs."""
    rows = make_rows(effect=0.012, seed=4)
    alone = run_all(rows)
    crowded = run_all(rows, extra=[lab.LabResult("x")] * 6)
    mom_alone = next(r for r in alone if r.name == "12-1 momentum")
    mom_crowded = next(r for r in crowded if r.name == "12-1 momentum")
    assert mom_alone.edge == mom_crowded.edge
    order = {"failing": 0, "unproven": 1, "earned": 2}
    assert order[mom_crowded.level] <= order[mom_alone.level]


def test_costs_come_off_every_active_month():
    rows = make_rows(effect=0.01, n_months=60)
    free = run_strategy(rows, "m", lab.pick_momentum, cost=0.0)
    paid = run_strategy(rows, "m", lab.pick_momentum, cost=0.004)
    assert free.edge - paid.edge == pytest.approx(0.004, abs=1e-9)


def test_a_month_with_no_picks_holds_the_market_and_is_not_counted_as_activity():
    rows = [Row("2024-01-31", f"T{i}", None, None, 0.02, 0.01, True) for i in range(10)] * 30
    for i, r in enumerate(rows):
        rows[i] = Row(f"2024-{1 + i // 30 % 12:02d}-{1 + i % 28:02d}", r.ticker, None, None, 0.02, 0.01, True)
    res = run_strategy(rows, "drift", lab.pick_drift)
    assert res.active_periods == 0 and all(e == 0.0 for e in res.per_period)


def test_the_future_cannot_change_the_past():
    full = make_rows(effect=0.02, n_months=60, seed=5)
    dates = sorted({r.date for r in full})
    early = [r for r in full if r.date <= dates[29]]
    a, b = run_strategy(full, "m", lab.pick_momentum), run_strategy(early, "m", lab.pick_momentum)
    assert a.per_period[:30] == b.per_period


def test_a_pick_never_depends_on_its_own_outcome():
    rows = make_rows(effect=0.02, n_months=3, seed=6)
    month = [r for r in rows if r.date == rows[0].date]
    flipped = [Row(r.date, r.ticker, r.momentum, r.drift, -r.fwd * 50, r.spy_fwd, r.regime_on) for r in month]
    assert [r.ticker for r in lab.pick_momentum(month)] == [r.ticker for r in lab.pick_momentum(flipped)]


def test_results_are_reproducible():
    rows = make_rows(effect=0.01)
    a, b = run_strategy(rows, "m", lab.pick_momentum), run_strategy(rows, "m", lab.pick_momentum)
    assert (a.edge, a.ci_low, a.ci_high) == (b.edge, b.ci_low, b.ci_high)


# ------------------------------------------------------------- the words

def test_the_report_states_the_family_size_and_never_crowns_nothing():
    results = run_all(make_rows(effect=0.0, seed=3))
    text = "\n".join(lab.describe(results))
    assert "4 ideas tried" in text
    assert "nothing cleared the corrected bar" in text
    assert "EDGE (survives" not in text


def test_a_winner_is_called_a_reason_to_test_further_not_to_bet():
    results = run_all(make_rows(effect=0.03))
    text = "\n".join(lab.describe(results))
    assert "cleared the corrected bar" in text and "not to bet on it yet" in text


def test_too_few_periods_is_reported_not_judged():
    res = run_strategy(make_rows(n_months=5), "m", lab.pick_momentum)
    assert "too few periods" in "\n".join(lab.describe([res]))


# ------------------------------------------------------------- collection

def _frames(days=400):
    from tests.conftest import make_ohlcv

    return make_ohlcv(days=days, daily_drift=0.002, seed=21), make_ohlcv(days=days, daily_drift=0.0004, seed=22)


def test_momentum_and_the_forward_month_match_a_hand_calculation():
    from tests.test_engine import FakeMarket

    aaa, spy = _frames()
    rows = lab.collect_monthly(FakeMarket({"AAA": aaa, "SPY": spy}), tickers=["AAA"])
    assert len(rows) >= 4
    row = rows[1]
    last = list(aaa.index.astype(str).str[:10]).index(row.date)
    c, s = aaa["Close"].to_numpy(), spy["Close"].to_numpy()
    assert row.momentum == pytest.approx(c[last - 21] / c[last - 252] - 1)
    assert row.fwd == pytest.approx(c[last + 21] / c[last] - 1)
    assert row.spy_fwd == pytest.approx(s[last + 21] / s[last] - 1)


def test_the_regime_flag_is_spy_against_its_own_200_day_average():
    from tests.test_engine import FakeMarket

    aaa, spy = _frames()
    rows = lab.collect_monthly(FakeMarket({"AAA": aaa, "SPY": spy}), tickers=["AAA"])
    s = spy["Close"].to_numpy()
    for row in rows:
        last = list(aaa.index.astype(str).str[:10]).index(row.date)
        assert row.regime_on == bool(s[last] > s[last + 1 - 200: last + 1].mean())


def test_signals_ignore_everything_after_the_decision_date():
    from tests.test_engine import FakeMarket

    aaa, spy = _frames()
    altered = aaa.copy()
    altered.iloc[330:, :4] *= 3.0                                  # a wild future
    a = lab.collect_monthly(FakeMarket({"AAA": aaa, "SPY": spy}), tickers=["AAA"])
    b = lab.collect_monthly(FakeMarket({"AAA": altered, "SPY": spy}), tickers=["AAA"])
    cutoff = str(aaa.index[300].date())
    early_a = [(r.date, r.momentum, r.drift, r.regime_on) for r in a if r.date <= cutoff]
    early_b = [(r.date, r.momentum, r.drift, r.regime_on) for r in b if r.date <= cutoff]
    assert early_a and early_a == early_b


def test_a_heavy_volume_gap_up_is_detected_and_a_quiet_one_is_not():
    from tests.test_engine import FakeMarket

    aaa, spy = _frames()
    loud, quiet = aaa.copy(), aaa.copy()
    for frame, boost in ((loud, 6.0), (quiet, 1.0)):
        i = 310                                                    # inside the window before the 316 decision
        frame.iloc[i, frame.columns.get_loc("Open")] = frame["Close"].iloc[i - 1] * 1.07
        frame.iloc[i, frame.columns.get_loc("Volume")] = frame["Volume"].iloc[i] * boost
    rows_loud = lab.collect_monthly(FakeMarket({"AAA": loud, "SPY": spy}), tickers=["AAA"])
    rows_quiet = lab.collect_monthly(FakeMarket({"AAA": quiet, "SPY": spy}), tickers=["AAA"])
    when = str(aaa.index[315].date())
    assert next(r for r in rows_loud if r.date == when).drift == pytest.approx(0.07, abs=0.01)
    assert next(r for r in rows_quiet if r.date == when).drift is None


def test_collection_needs_a_benchmark():
    from tests.test_engine import FakeMarket

    aaa, _ = _frames()
    with pytest.raises(RuntimeError, match="SPY"):
        lab.collect_monthly(FakeMarket({"AAA": aaa}), tickers=["AAA"])


# ------------------------------------------------------------- the command

def test_the_lab_command_runs_everything_and_remembers_the_winners(monkeypatch, tmp_path, capsys):
    from stocksage import backtest, cli
    from tests.test_backtest import make_samples

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)
    monkeypatch.setattr(backtest, "collect_samples",
                        lambda market, tickers=None, period="2y": make_samples(signal=0.0, n_dates=120))
    monkeypatch.setattr(lab, "collect_monthly",
                        lambda market, tickers=None, period="5y": make_rows(effect=0.03))
    assert cli.main(["lab"]) == 0
    out = capsys.readouterr().out
    assert "6 ideas tried" in out
    for name in ("12-1 momentum", "regime filter only", "current model (weekly)", "ridge challenger (weekly)"):
        assert name in out

    from stocksage.db import Database

    db = Database(tmp_path / "b.db")
    assert "12-1 momentum" in (db.get_meta("lab_winners") or "")
    assert db.get_meta("lab_at")


def test_the_lab_command_explains_a_data_failure(monkeypatch, tmp_path, capsys):
    from stocksage import backtest, cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)

    def boom(*a, **k):
        raise RuntimeError("no SPY history, so there is nothing to measure against")

    monkeypatch.setattr(backtest, "collect_samples", boom)
    assert cli.main(["lab"]) == 1
    assert "Could not run the lab" in capsys.readouterr().out
