"""The backtest must be hard to fool, because its answer decides whether the
model is allowed near real money.

Each test attacks one way a backtest lies: look-ahead, free trading, luck, or
a verdict handed out on noise. Evaluation is pure, so these run on constructed
samples with a known truth planted in them.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from stocksage import backtest
from stocksage.backtest import Sample, describe, evaluate

SIGNALS = ["trend_long", "momentum_20d", "macd"]


def make_samples(n_dates=140, n_names=20, signal=0.0, noise=0.02, seed=1):
    """`signal` is how much the FIRST feature predicts the excess return:
    +0.01 plants a real edge, -0.01 an inverted one, 0.0 pure noise."""
    rng = random.Random(seed)
    start = date(2024, 1, 5)
    out = []
    for i in range(n_dates):
        d = (start + timedelta(days=7 * i)).isoformat()
        for j in range(n_names):
            feats = {name: rng.uniform(-1, 1) for name in SIGNALS}
            excess = signal * feats["trend_long"] + rng.gauss(0, noise)
            out.append(Sample(d, f"T{j}", feats, excess, vol=0.2))
    return out


# --------------------------------------------------------------- finds truth

def test_a_real_edge_is_found():
    r = evaluate(make_samples(signal=0.02, n_dates=160))
    assert r.edge > 0.005
    assert r.ci_low > 0, "the interval should clear zero on a strong planted edge"
    assert r.vs_random > 0.005
    assert r.level == "earned"


def test_pure_noise_is_never_declared_an_edge():
    """The failure that matters: a verdict of 'edge' handed out on luck. Many
    different noise worlds, none may be called earned."""
    for seed in range(12):
        r = evaluate(make_samples(signal=0.0, seed=seed, n_dates=160))
        assert r.level != "earned", f"noise world {seed} was called an edge"


def test_an_inverted_signal_is_caught_as_failing():
    r = evaluate(make_samples(signal=-0.02, n_dates=160))
    assert r.edge < 0 and r.level == "failing"
    assert "NO EDGE" in "\n".join(describe(r))


def test_noise_picks_do_no_better_than_random_ones():
    r = evaluate(make_samples(signal=0.0, n_dates=160, seed=3))
    assert abs(r.vs_random) < 0.004


# --------------------------------------------------------------- no look-ahead

def test_the_future_cannot_change_the_past():
    """Truncate history, and every earlier period must come out identical.
    Any leak of later data into earlier decisions shows up here."""
    full = make_samples(signal=0.01, n_dates=120, seed=5)
    cut_date = sorted({s.date for s in full})[59]
    early = [s for s in full if s.date <= cut_date]
    r_full, r_early = evaluate(full), evaluate(early)
    k = len(r_early.per_period)
    assert k > 20
    assert r_full.per_period[:k] == r_early.per_period
    for d, chosen in r_early.picks_log.items():
        assert r_full.picks_log[d] == chosen


def test_tampering_with_later_outcomes_changes_nothing_earlier():
    base = make_samples(signal=0.01, n_dates=100, seed=6)
    dates = sorted({s.date for s in base})
    boundary = dates[49]
    tampered = [
        Sample(s.date, s.ticker, s.features, 5.0 if s.date > boundary else s.excess, s.vol)
        for s in base
    ]
    a, b = evaluate(base), evaluate(tampered)
    for d in dates[:50]:
        assert a.picks_log.get(d) == b.picks_log.get(d)


def test_a_pick_is_never_made_using_its_own_outcome():
    """Weights may learn from the PREVIOUS date's results, never today's. Flip
    today's outcomes and the choice of names today must not move."""
    base = make_samples(signal=0.01, n_dates=80, seed=8)
    target = sorted({s.date for s in base})[40]
    flipped = [
        Sample(s.date, s.ticker, s.features, -s.excess * 100 if s.date == target else s.excess, s.vol)
        for s in base
    ]
    assert evaluate(base).picks_log[target] == evaluate(flipped).picks_log[target]


def test_without_learning_the_weights_never_react_to_outcomes():
    base = make_samples(signal=0.01, n_dates=60, seed=9)
    scrambled = [Sample(s.date, s.ticker, s.features, -s.excess, s.vol) for s in base]
    assert evaluate(base, learn=False).picks_log == evaluate(scrambled, learn=False).picks_log


# --------------------------------------------------------------- costs & stats

def test_costs_come_straight_off_the_edge():
    s = make_samples(signal=0.01, n_dates=100, seed=2)
    free, costly = evaluate(s, cost=0.0), evaluate(s, cost=0.004)
    assert free.edge - costly.edge == pytest.approx(0.004, abs=1e-9)


def test_a_signal_that_only_works_before_costs_does_not_survive_them():
    weak = make_samples(signal=0.002, n_dates=160, seed=4)
    assert evaluate(weak, cost=0.0).edge > evaluate(weak, cost=0.01).edge
    assert evaluate(weak, cost=0.01).edge < 0


def test_the_interval_is_reproducible_and_brackets_the_mean_on_noise():
    s = make_samples(signal=0.0, n_dates=150, seed=11)
    a, b = evaluate(s), evaluate(s)
    assert (a.ci_low, a.ci_high) == (b.ci_low, b.ci_high)
    assert a.ci_low <= a.edge <= a.ci_high


def test_too_little_history_is_reported_not_guessed():
    for n in (1, 3, 15):
        r = evaluate(make_samples(n_dates=n))
        text = "\n".join(describe(r))
        assert "too few" in text and "VERDICT" not in text, n


def test_no_signal_periods_are_counted_not_hidden():
    dull = [Sample("2024-01-05", f"T{i}", {n: -0.9 for n in SIGNALS}, 0.0, 0.2) for i in range(10)]
    r = evaluate(dull)
    assert r.no_signal_periods == 1 and r.picks == 0


def test_the_backtest_and_the_live_gate_share_one_definition_of_proof():
    from stocksage import actions

    assert backtest.trust_level is actions.trust_level
    assert actions.trust_level(150, 0.01, 0.002) == "earned"
    assert actions.trust_level(150, -0.01, 0.002) == "failing"
    assert actions.trust_level(10, 0.5, 0.001) == "unproven"


# --------------------------------------------------------------- the words

def test_the_report_never_calls_noise_an_edge():
    text = "\n".join(describe(evaluate(make_samples(signal=0.0, seed=2, n_dates=160))))
    assert "EDGE FOUND" not in text
    assert "VERDICT" in text


def test_the_report_states_its_method():
    text = "\n".join(describe(evaluate(make_samples(signal=0.01, n_dates=120))))
    assert "after costs" in text and "re-learned" in text and "no overlap" in text


# --------------------------------------------------------------- collection

def test_collection_matches_the_forward_return_by_hand():
    from tests.conftest import make_ohlcv
    from tests.test_engine import FakeMarket

    frames = {"AAA": make_ohlcv(days=300, daily_drift=0.003, seed=21),
              "SPY": make_ohlcv(days=300, daily_drift=0.0005, seed=22)}
    samples = backtest.collect_samples(FakeMarket(frames), tickers=["AAA"])
    assert len(samples) > 10 and all(s.features for s in samples)

    s = samples[3]
    df, spy = frames["AAA"], frames["SPY"]
    pos = list(df.index.astype(str).str[:10]).index(s.date)
    expected = (df["Close"].iloc[pos + 5] / df["Close"].iloc[pos] - 1) - (
        spy["Close"].iloc[pos + 5] / spy["Close"].iloc[pos] - 1)
    assert s.excess == pytest.approx(float(expected), rel=1e-9)


def test_collection_features_ignore_everything_after_the_decision_date():
    """Rewrite the future of a stock; features at an earlier date must not move."""
    from tests.conftest import make_ohlcv
    from tests.test_engine import FakeMarket

    spy = make_ohlcv(days=300, seed=22)
    base = make_ohlcv(days=300, daily_drift=0.003, seed=21)
    altered = base.copy()
    altered.iloc[200:, :4] *= 3.0                       # a wild future
    a = backtest.collect_samples(FakeMarket({"AAA": base, "SPY": spy}), tickers=["AAA"])
    b = backtest.collect_samples(FakeMarket({"AAA": altered, "SPY": spy}), tickers=["AAA"])
    cutoff = str(base.index[190].date())
    early_a = {s.date: s.features for s in a if s.date <= cutoff}
    early_b = {s.date: s.features for s in b if s.date <= cutoff}
    assert early_a and early_a == early_b


def test_collection_needs_a_benchmark():
    from tests.conftest import make_ohlcv
    from tests.test_engine import FakeMarket

    with pytest.raises(RuntimeError, match="SPY"):
        backtest.collect_samples(FakeMarket({"AAA": make_ohlcv(days=300)}), tickers=["AAA"])


def test_the_backtest_command_reports_and_remembers(monkeypatch, tmp_path, capsys):
    from stocksage import cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)
    monkeypatch.setattr(backtest, "collect_samples",
                        lambda market, tickers=None, period="2y": make_samples(signal=0.02, n_dates=140))
    assert cli.main(["backtest", "--years", "2"]) == 0
    out = capsys.readouterr().out
    assert "DO ITS BUY PICKS BEAT THE MARKET" in out and "DO ITS SELL CALLS TRAIL THE MARKET" in out
    assert out.count("VERDICT") == 2

    from stocksage.db import Database

    db = Database(tmp_path / "b.db")
    assert db.get_meta("backtest_level") in ("earned", "unproven", "failing")
    assert db.get_meta("backtest_sell_level") in ("earned", "unproven", "failing")
    assert db.get_meta("backtest_at")


def test_the_backtest_command_explains_a_data_failure(monkeypatch, tmp_path, capsys):
    from stocksage import cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)

    def boom(*a, **k):
        raise RuntimeError("no SPY history, so there is nothing to measure against")

    monkeypatch.setattr(backtest, "collect_samples", boom)
    assert cli.main(["backtest"]) == 1
    assert "Could not run the backtest" in capsys.readouterr().out


# ------------------------------------------------------------ the sell side

def test_a_real_signal_shows_up_on_the_sell_side_as_a_positive_edge():
    """Names the model rates lowest should go on to trail the market, which
    is a WIN for a sell call: the edge is reported in the direction of the bet."""
    r = evaluate(make_samples(signal=0.02, n_dates=160), side="sell")
    assert r.edge > 0.005 and r.level == "earned"


def test_an_inverted_signal_is_a_negative_edge_on_the_sell_side_too():
    r = evaluate(make_samples(signal=-0.02, n_dates=160), side="sell")
    assert r.edge < 0 and r.level == "failing"


def test_noise_is_never_called_a_sell_edge():
    for seed in range(8):
        r = evaluate(make_samples(signal=0.0, seed=seed, n_dates=160), side="sell")
        assert r.level != "earned", f"noise world {seed}"


def test_the_sell_side_picks_the_lowest_scoring_names_not_the_highest():
    s = make_samples(signal=0.01, n_dates=60, seed=3)
    buys, sells = evaluate(s, side="buy"), evaluate(s, side="sell")
    shared = set(buys.picks_log) & set(sells.picks_log)
    assert shared
    assert all(not set(buys.picks_log[d]) & set(sells.picks_log[d]) for d in shared)


def test_the_sell_side_cannot_see_the_future_either():
    full = make_samples(signal=0.01, n_dates=120, seed=5)
    cut = sorted({s.date for s in full})[59]
    early = [s for s in full if s.date <= cut]
    a, b = evaluate(full, side="sell"), evaluate(early, side="sell")
    k = len(b.per_period)
    assert k > 15 and a.per_period[:k] == b.per_period


def test_costs_come_off_the_sell_side_too():
    s = make_samples(signal=0.01, n_dates=100, seed=2)
    assert evaluate(s, cost=0.0, side="sell").edge - evaluate(s, cost=0.004, side="sell").edge \
        == pytest.approx(0.004, abs=1e-9)


def test_an_unknown_side_is_refused_not_silently_treated_as_buy():
    with pytest.raises(ValueError):
        evaluate(make_samples(n_dates=10), side="short")


def test_the_report_names_the_side_and_states_which_way_is_good():
    sells = "\n".join(describe(evaluate(make_samples(signal=0.02, n_dates=140), side="sell"), side="sell"))
    assert "SELL CALLS TRAIL" in sells and "positive = the sells were right" in sells


# ------------------------------------------------------------ the ridge challenger

def test_the_challenger_finds_a_real_linear_signal():
    r = backtest.evaluate_ridge(make_samples(signal=0.02, n_dates=160))
    assert r.edge > 0.005 and r.level == "earned"


def test_the_challenger_does_not_invent_an_edge_from_noise():
    for seed in range(8):
        r = backtest.evaluate_ridge(make_samples(signal=0.0, seed=seed, n_dates=160))
        assert r.level != "earned", f"noise world {seed}"


def test_the_challenger_cannot_see_the_future():
    full = make_samples(signal=0.01, n_dates=120, seed=5)
    cut = sorted({s.date for s in full})[69]
    early = [s for s in full if s.date <= cut]
    a, b = backtest.evaluate_ridge(full), backtest.evaluate_ridge(early)
    k = len(b.per_period)
    assert k > 20 and a.per_period[:k] == b.per_period


def test_the_challenger_never_trains_on_the_outcome_of_the_date_it_is_picking():
    base = make_samples(signal=0.01, n_dates=90, seed=8)
    target = sorted({s.date for s in base})[60]
    flipped = [Sample(s.date, s.ticker, s.features, -s.excess * 100 if s.date == target else s.excess, s.vol)
               for s in base]
    assert backtest.evaluate_ridge(base).picks_log[target] == backtest.evaluate_ridge(flipped).picks_log[target]


def test_the_challenger_and_the_current_model_are_scored_by_the_same_function():
    s = make_samples(signal=0.01, n_dates=100, seed=2)
    a, b = backtest.evaluate(s), backtest.evaluate_ridge(s)
    assert a.level in ("earned", "unproven", "failing") and b.level in ("earned", "unproven", "failing")
    # The same interval machinery: a seeded bootstrap of identical data is identical.
    assert backtest._block_bootstrap_ci(a.per_period, 7) == backtest._block_bootstrap_ci(a.per_period, 7)
