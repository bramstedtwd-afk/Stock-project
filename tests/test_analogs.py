"""Look-alike evidence must not manufacture confidence.

The danger is a number that makes a coin-flip look like a pattern: neighbours
bunched into one rally, a sell scored as if it were a buy, or history that
includes the very outcome being judged. Each is attacked here.
"""

from __future__ import annotations

import numpy as np
import pytest

from stocksage import analogs
from stocksage.analogs import AnalogBook, evidence_arrays
from stocksage.backtest import Sample, evaluate
from tests.test_backtest import make_samples


def book(signal=0.02, n_dates=140, seed=1):
    return AnalogBook.from_samples(make_samples(signal=signal, n_dates=n_dates, seed=seed))


STRONG = {"trend_long": 0.95, "momentum_20d": 0.0, "macd": 0.0}
WEAK = {"trend_long": -0.95, "momentum_20d": 0.0, "macd": 0.0}


# ------------------------------------------------------------------ finds truth

def test_a_real_pattern_is_backed_on_the_buy_side():
    ev = book().evidence(STRONG, "buy")
    assert ev.backed and ev.edge > 0.005 and ev.lower > 0
    assert "BACKS" in ev.sentence()


def test_the_mirror_setup_backs_a_sell_not_a_buy():
    b = book()
    assert b.evidence(WEAK, "sell").backed
    wrong_way = b.evidence(WEAK, "buy")
    assert not wrong_way.backed and wrong_way.edge < 0
    assert "does NOT back" in wrong_way.sentence()


def test_buy_and_sell_evidence_are_exact_opposites_before_costs():
    b = book(seed=3)
    buy, sell = b.evidence(STRONG, "buy", cost=0.0), b.evidence(STRONG, "sell", cost=0.0)
    assert buy.edge == pytest.approx(-sell.edge)
    assert buy.hit_rate == pytest.approx(1.0 - sell.hit_rate, abs=0.02)


def test_costs_come_straight_off_the_edge():
    b = book(seed=4)
    free, paid = b.evidence(STRONG, "buy", cost=0.0), b.evidence(STRONG, "buy", cost=0.004)
    assert free.edge - paid.edge == pytest.approx(0.004)


# ------------------------------------------------------------------ honesty

def test_in_pure_noise_it_rarely_claims_to_back_a_call():
    """A false 'history backs this' is the dangerous error. Over many noise
    worlds and many random setups it must stay close to the nominal 2.5%."""
    rng = np.random.default_rng(0)
    backed = total = 0
    for seed in range(25):
        b = book(signal=0.0, n_dates=120, seed=seed)
        for _ in range(8):
            v = {"trend_long": rng.uniform(-1, 1), "momentum_20d": rng.uniform(-1, 1),
                 "macd": rng.uniform(-1, 1)}
            total += 1
            backed += b.evidence(v, "buy").backed
    assert backed / total < 0.10, f"{backed}/{total} noise setups were 'backed'"


def test_neighbours_from_one_episode_are_not_a_pattern():
    """Two hundred samples that all come from a handful of dates are one
    rally, however many stocks took part."""
    rng = np.random.default_rng(1)
    x = rng.uniform(-1, 1, (400, 3))
    dates = np.array([f"2024-01-{1 + i % 5:02d}" for i in range(400)])     # five dates only
    ev = evidence_arrays(x, dates, np.full(400, 0.05), np.zeros(3), "buy")
    assert not ev.backed and ev.edge is None
    assert "one episode" in ev.sentence()


def test_too_little_history_says_so_instead_of_guessing():
    ev = evidence_arrays(np.zeros((5, 3)), np.array(["2024-01-01"] * 5), np.zeros(5), np.zeros(3), "buy")
    assert not ev.backed and "not enough history" in ev.sentence()


def test_the_future_cannot_leak_into_the_evidence():
    s = make_samples(signal=0.01, n_dates=100, seed=5)
    dates = sorted({x.date for x in s})
    cut = dates[60]
    tampered = [Sample(x.date, x.ticker, x.features, 9.0 if x.date >= cut else x.excess, x.vol) for x in s]
    a = AnalogBook.from_samples(s).evidence(STRONG, "buy", before=cut)
    b = AnalogBook.from_samples(tampered).evidence(STRONG, "buy", before=cut)
    assert (a.edge, a.lower, a.n) == (b.edge, b.lower, b.n)


def test_an_invalid_side_is_refused():
    with pytest.raises(ValueError):
        book().evidence(STRONG, "short")


def test_a_feature_the_book_never_saw_is_ignored_not_a_crash():
    assert book().evidence({**STRONG, "brand_new_signal": 0.9}, "buy") is not None


# ------------------------------------------------------------------ the cache

def test_the_book_survives_a_round_trip_to_disk(tmp_path):
    b = book(n_dates=60)
    path = b.save(tmp_path / "a.npz")
    again = AnalogBook.load(path)
    assert len(again) == len(b) and again.names == b.names
    assert again.evidence(STRONG, "buy").edge == pytest.approx(b.evidence(STRONG, "buy").edge)


def test_a_missing_or_damaged_cache_is_not_built_yet_never_a_crash(tmp_path):
    assert AnalogBook.load(tmp_path / "nope.npz") is None
    bad = tmp_path / "bad.npz"
    bad.write_bytes(b"not a real archive")
    assert AnalogBook.load(bad) is None


def test_an_old_book_is_reported_stale(tmp_path):
    import time

    b = book(n_dates=40)
    assert not b.stale
    b.built_at = time.time() - 12 * 86400
    assert b.stale and b.age_days == pytest.approx(12, abs=0.1)


def test_the_cache_lives_where_state_lives(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    assert analogs.default_path() == tmp_path / "analogs.npz"


# ------------------------------------------------------------------ tested out of sample

def test_confirmation_only_ever_removes_picks_never_adds_them():
    s = make_samples(signal=0.01, n_dates=120, seed=6)
    plain, confirmed = evaluate(s), evaluate(s, confirm_with_analogs=True)
    for d, picks in confirmed.picks_log.items():
        assert set(picks) <= set(plain.picks_log[d])


def test_the_confirmed_strategy_cannot_see_the_future():
    full = make_samples(signal=0.01, n_dates=120, seed=7)
    cut = sorted({x.date for x in full})[79]
    early = [x for x in full if x.date <= cut]
    a, b = evaluate(full, confirm_with_analogs=True), evaluate(early, confirm_with_analogs=True)
    for d, picks in b.picks_log.items():
        assert a.picks_log[d] == picks


def test_the_confirmed_strategy_does_not_invent_an_edge_from_noise():
    for seed in range(8):
        r = evaluate(make_samples(signal=0.0, seed=seed, n_dates=160), confirm_with_analogs=True)
        assert r.level != "earned", f"noise world {seed}"


def test_with_a_real_signal_confirmation_keeps_the_edge():
    r = evaluate(make_samples(signal=0.02, n_dates=160), confirm_with_analogs=True)
    assert r.edge is not None and r.edge > 0.005


def test_a_market_wide_swing_is_one_observation_not_ten_stocks():
    """Twelve dates up 3%, eight down 3%, every stock on a date moving together.
    Counted stock by stock that is 200 observations and a confident edge;
    counted by date it is 20 noisy swings that prove nothing. The clustering
    protection exists for exactly this."""
    x = np.zeros((200, 3))
    dates = np.repeat([f"2024-{1 + i // 20:02d}-{1 + i % 20:02d}" for i in range(20)], 10)
    excess = np.repeat([0.03] * 12 + [-0.03] * 8, 10)
    ev = evidence_arrays(x, dates, excess, np.zeros(3), "buy", k=200, cost=0.0)
    assert ev.dates == 20 and ev.edge == pytest.approx(0.006)
    assert not ev.backed, "ten stocks moving together were counted as ten pieces of evidence"
