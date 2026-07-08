"""Bootstrap tests: offline, via the injected fake market."""

import numpy as np
import pytest

from stocksage.bootstrap import backfill_move_history, bootstrap, warmup_learning
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


@pytest.fixture
def frames():
    up = make_ohlcv(days=500, daily_drift=0.002, daily_vol=0.01, seed=21)
    down = make_ohlcv(days=500, daily_drift=-0.002, daily_vol=0.01, seed=22)
    # Give the down frame a couple of unmistakable event days.
    for i, factor in ((-100, 0.93), (-40, 1.06)):
        col = down.columns.get_loc("Close")
        down.iloc[i, col] = down["Close"].iloc[i - 1] * factor
    return {"UPUP": up, "DOWN": down}


def test_warmup_trains_weights(frames):
    db = Database(":memory:")
    stats = warmup_learning(FakeMarket(frames), db, tickers=["UPUP", "DOWN"])
    assert stats["warmup_samples"] > 100
    weights = db.load_weights()
    assert weights and sum(weights.values()) == pytest.approx(1.0)
    # Training must actually differentiate: not all weights still uniform.
    values = np.array(list(weights.values()))
    assert values.std() > 1e-4
    assert db.get_meta("warmup_samples") == str(stats["warmup_samples"])


def test_backfill_records_history_but_skips_recent(frames):
    db = Database(":memory:")
    count = backfill_move_history(FakeMarket(frames), db, tickers=["UPUP", "DOWN"])
    assert count >= 2  # the two injected spikes at minimum
    events = db.move_events()
    assert all("historical-backfill" in row["reasons"] for row in events)
    # conftest frames end 2026-07-07; today (>= 2026-07-08) minus 7d cushion
    # means nothing within a week of now may be backfilled.
    assert all(row["event_date"] < "2026-07-02" for row in events)


def test_bootstrap_sets_done_flag(frames):
    db = Database(":memory:")
    stats = bootstrap(FakeMarket(frames), db, tickers=["UPUP", "DOWN"])
    assert db.get_meta("bootstrap_done") is not None
    assert stats["move_events_backfilled"] >= 2


def test_daily_run_bootstraps_only_once(frames, monkeypatch):
    from stocksage import universe

    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    engine = Engine(db=Database(":memory:"), market=FakeMarket(frames))
    first = engine.daily_run(with_robinhood=False)
    assert first.bootstrap_stats is not None
    assert first.bootstrap_stats["warmup_samples"] > 0
    assert engine.db.get_meta("last_daily_run") is not None

    second = engine.daily_run(with_robinhood=False)
    assert second.bootstrap_stats is None  # once only
