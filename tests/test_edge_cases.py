"""Adversarial tests for long-horizon trust.

These do not test features. They test the invariants that must hold after
months of unattended running, because the failures that destroy trust in a
learning system are quiet ones: a number that drifts flattering, a brain
that loses history on merge, a corporate action that teaches the model a
lie. Each test here encodes something that would be believed if it broke.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from stocksage.db import SOURCE_MODEL, SOURCE_OWNER, Database
from stocksage.engine import Engine
from stocksage.learning import initial_weights, normalize, update_weights, weighted_score
from stocksage.profit import paper_trades, profit_stats
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket

SIGNALS = ["trend_long", "trend_medium", "momentum_20d", "macd", "rsi_reversion"]


# ---------------------------------------------------------------- learning

@pytest.mark.parametrize(
    "realized",
    [1e-9, -1e-9, 0.0, 1e6, -0.999999, 50.0, -50.0, float("inf"), float("-inf")],
)
def test_weights_survive_absurd_returns(realized):
    """A split, a data glitch, or a delisting can hand the learner a return
    no sane market produces. It must never emit NaN, a negative weight, or a
    vector that stops summing to one — those corrupt every later decision
    silently rather than loudly."""
    weights = initial_weights(SIGNALS)
    signals = {name: 0.5 for name in SIGNALS}
    out, _ = update_weights(weights, signals, realized)
    assert all(np.isfinite(v) for v in out.values()), f"non-finite weight from {realized}"
    assert all(v > 0 for v in out.values()), "a signal was silenced permanently"
    assert sum(out.values()) == pytest.approx(1.0)


def test_weights_never_collapse_onto_one_signal():
    """Hundreds of consistent updates must not drive the others to zero: a
    regime change would then have nothing left to re-learn with."""
    weights = initial_weights(SIGNALS)
    signals = {"trend_long": 1.0, **{n: -1.0 for n in SIGNALS if n != "trend_long"}}
    for _ in range(500):
        weights, _ = update_weights(weights, signals, 0.05)
    assert sum(weights.values()) == pytest.approx(1.0)
    losers = [v for k, v in weights.items() if k != "trend_long"]
    assert min(losers) > 0.0, "a signal hit zero and can never recover"
    assert weights["trend_long"] < 1.0


def test_weighted_score_stays_in_range_with_hostile_input():
    """Score feeds thresholds and position sizing; out-of-range values would
    size positions off a scale that doesn't exist."""
    for signals in (
        {n: 1.0 for n in SIGNALS},
        {n: -1.0 for n in SIGNALS},
        {"only": 1.0},
        {},
        {n: 0.0 for n in SIGNALS},
    ):
        score = weighted_score(signals, initial_weights(SIGNALS))
        assert -1.0 <= score <= 1.0, f"{score} out of range for {signals}"
        assert np.isfinite(score)


def test_normalize_handles_degenerate_vectors():
    assert sum(normalize({"a": 0.0, "b": 0.0}).values()) == pytest.approx(1.0)
    assert sum(normalize({"a": 1e-300, "b": 1e-300}).values()) == pytest.approx(1.0)
    assert normalize({}) == {}


# ------------------------------------------------------------- corporate actions

def _engine_with(frames):
    return Engine(db=Database(":memory:"), market=FakeMarket(frames))


def test_a_stale_stored_price_across_a_split_grades_correctly():
    """The realistic hazard. Price history is retroactively split-adjusted;
    the price recorded at call time is not. Dividing one by the other turns
    an ordinary 2-for-1 into a -50% call. Grading within a single adjusted
    series makes the adjustment cancel."""
    df = make_ohlcv(days=120, daily_drift=0.001, daily_vol=0.004, seed=3)
    eng = _engine_with({"SPLIT": df, "SPY": make_ohlcv(days=120, seed=9)})
    entry_pos = -11
    adjusted_entry = float(df["Close"].iloc[entry_pos])
    sid = eng.db.record_suggestion(
        "SPLIT", "BUY", 0.5,
        adjusted_entry * 2,  # what it cost pre-split, never re-adjusted
        {"trend_long": 1.0}, 5,
        created_at=str(df.index[entry_pos].date()) + "T21:00:00+00:00",
    )
    eng.evaluate_pending(now=(df.index[-1] + timedelta(days=2)).tz_localize(timezone.utc))
    row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    expected = float(df["Close"].iloc[entry_pos + 5]) / adjusted_entry - 1.0
    assert row["realized_return"] == pytest.approx(expected), (
        "the stored pre-split price leaked into the return"
    )
    assert abs(row["realized_return"]) < 0.2


@pytest.mark.parametrize(
    "factor,label",
    [(1 / 3, "3-for-1 split"), (1 / 10, "10-for-1 split"), (10.0, "1-for-10 reverse")],
)
def test_gross_price_artifacts_are_refused_not_learned_from(factor, label):
    """Defence in depth for a feed that stops adjusting. A move this size is
    not a return, and one lost data point is far cheaper than a silently
    poisoned weight vector.

    Deliberate limit: a 2-for-1 lands at exactly -50%, inside the plausible
    band, so it is NOT caught here — it is handled upstream by grading both
    endpoints from one adjusted series. Widening the band far enough to
    catch it would start discarding genuine crashes, which is a worse trade.
    """
    df = make_ohlcv(days=120, daily_drift=0.001, daily_vol=0.004, seed=3)
    df.iloc[-8:, df.columns.get_loc("Close")] *= factor
    eng = _engine_with({"RAW": df, "SPY": make_ohlcv(days=120, seed=9)})
    entry_pos = -11
    sid = eng.db.record_suggestion(
        "RAW", "BUY", 0.5, float(df["Close"].iloc[entry_pos]), {"trend_long": 1.0}, 5,
        created_at=str(df.index[entry_pos].date()) + "T21:00:00+00:00",
    )
    eng.evaluate_pending(now=(df.index[-1] + timedelta(days=2)).tz_localize(timezone.utc))
    row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    assert row["evaluated"] == 1, f"{label} left the call pending forever"
    assert row["realized_return"] is None, f"{label} became a graded return"
    assert eng.db.load_weights() == {}, f"the model trained on a {label} artifact"


def test_a_genuine_large_crash_is_still_graded():
    """The guard must not quietly discard real losses — a model that only
    learns from moves it finds comfortable is worse than no model."""
    df = make_ohlcv(days=120, daily_drift=0.001, daily_vol=0.004, seed=3)
    entry_pos = -11
    df.iloc[-8:, df.columns.get_loc("Close")] *= 0.62  # a brutal but real -38%
    eng = _engine_with({"CRASH": df, "SPY": make_ohlcv(days=120, seed=9)})
    sid = eng.db.record_suggestion(
        "CRASH", "BUY", 0.5, float(df["Close"].iloc[entry_pos]), {"trend_long": 1.0}, 5,
        created_at=str(df.index[entry_pos].date()) + "T21:00:00+00:00",
    )
    eng.evaluate_pending(now=(df.index[-1] + timedelta(days=2)).tz_localize(timezone.utc))
    row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    assert row["realized_return"] is not None, "a real crash was discarded as noise"
    assert row["realized_return"] < -0.3
    assert row["hit"] == 0  # a buy call that fell is a miss, and must count


def test_a_delisted_ticker_never_grades_as_a_total_loss():
    """When a ticker stops returning data the call must stay ungraded, not
    be scored against a stale or zero price."""
    eng = _engine_with({"SPY": make_ohlcv(days=120, seed=9)})
    sid = eng.db.record_suggestion(
        "GONE", "BUY", 0.5, 100.0, {"trend_long": 1.0}, 5,
        created_at="2026-01-05T21:00:00+00:00",
    )
    eng.evaluate_pending(now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    assert row["realized_return"] is None, "graded a name with no price data"


def test_zero_and_negative_prices_are_refused():
    eng = _engine_with({"SPY": make_ohlcv(days=60, seed=9)})
    for price in (0.0, -5.0):
        sid = eng.db.record_suggestion(
            "BAD", "BUY", 0.5, price, {"trend_long": 1.0}, 5,
            created_at="2026-01-05T21:00:00+00:00",
        )
        eng.evaluate_pending(now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        assert row["realized_return"] is None


# ------------------------------------------------------------------- clocks

def test_a_call_dated_in_the_future_is_not_graded():
    """Clock skew on one device must not make a call gradeable before its
    horizon has actually elapsed."""
    df = make_ohlcv(days=90, seed=4)
    eng = _engine_with({"FUT": df, "SPY": make_ohlcv(days=90, seed=9)})
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sid = eng.db.record_suggestion(
        "FUT", "BUY", 0.5, 100.0, {"trend_long": 1.0}, 5, created_at=future
    )
    eng.evaluate_pending()
    row = eng.db.conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    assert row["evaluated"] == 0, "graded a call from the future"


def test_grading_is_idempotent():
    """Running the daily cycle twice, or on two devices, must not grade the
    same call twice and double-count it into the weights."""
    df = make_ohlcv(days=120, daily_drift=0.003, seed=5)
    eng = _engine_with({"AAA": df, "SPY": make_ohlcv(days=120, seed=9)})
    eng.db.record_suggestion(
        "AAA", "BUY", 0.5, float(df["Close"].iloc[-10]), {"trend_long": 1.0}, 5,
        created_at=str(df.index[-10].date()) + "T21:00:00+00:00",
    )
    now = (df.index[-1] + timedelta(days=2)).tz_localize(timezone.utc)
    first = eng.evaluate_pending(now=now)
    weights_after_first = dict(eng.db.load_weights())
    second = eng.evaluate_pending(now=now)
    assert first == 1 and second == 0
    assert eng.db.load_weights() == weights_after_first


# ------------------------------------------------------------------ the ledger

def test_profit_stats_never_divides_by_zero():
    """Every combination of empty/one-sided data must produce numbers or
    None, never an exception on the tab the owner checks most."""
    for rows in ([], [(0.05, 0.01)], [(-0.05, 0.01)], [(0.0, 0.0)]):
        db = Database(":memory:")
        for i, (realized, bench) in enumerate(rows):
            sid = db.record_suggestion(f"T{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
            db.mark_evaluated(sid, realized, realized > 0, benchmark_return=bench)
        buys, avoided = paper_trades(db.evaluated_suggestions())
        stats = profit_stats(buys, avoided)
        for key, value in stats.items():
            if isinstance(value, float):
                assert np.isfinite(value), f"{key} is not finite for {rows}"


def test_a_winning_streak_does_not_produce_infinite_profit_factor():
    """Gross losses of zero must not read as an infinitely good strategy."""
    db = Database(":memory:")
    for i in range(5):
        sid = db.record_suggestion(f"W{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        db.mark_evaluated(sid, 0.04, True, benchmark_return=0.01)
    buys, avoided = paper_trades(db.evaluated_suggestions())
    pf = profit_stats(buys, avoided)["profit_factor"]
    assert pf is None or np.isfinite(pf), "profit factor became infinite"


# ------------------------------------------------------------- brain longevity

def test_merging_the_same_brain_twice_adds_nothing_the_second_time():
    """Devices re-sync constantly. A merge that duplicates history would
    inflate the track record a little more every single day."""
    from stocksage.brain import merge_brains

    src = Database(":memory:")
    for i in range(6):
        sid = src.record_suggestion(f"T{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        src.mark_evaluated(sid, 0.03, True, benchmark_return=0.01)
    src.record_move_event("AAPL", "2026-05-01", 0.06, 2.5, ["earnings"], [])

    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    src_path, dest_path = tmp / "src.db", tmp / "dest.db"
    disk = Database(src_path)
    src.conn.backup(disk.conn)
    disk.conn.commit()
    disk.close()
    Database(dest_path).close()

    first = merge_brains(dest_path, src_path)
    second = merge_brains(dest_path, src_path)
    assert first["suggestions_added"] == 6
    assert second["suggestions_added"] == 0, "re-syncing duplicated the record"
    assert second["move_events_added"] == 0

    db = Database(dest_path)
    assert db.conn.execute("SELECT COUNT(*) c FROM suggestions").fetchone()["c"] == 6
    db.close()


def test_a_brain_from_the_future_schema_still_opens():
    """A device on a newer version writes columns this one doesn't know.
    Opening must not fail — the owner would lose access to their history."""
    import sqlite3
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "future.db"
    db = Database(path)
    db.conn.execute("ALTER TABLE suggestions ADD COLUMN quantum_score REAL")
    db.record_suggestion("AAPL", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
    db.close()

    reopened = Database(path)  # must not raise
    assert reopened.conn.execute("SELECT COUNT(*) c FROM suggestions").fetchone()["c"] == 1
    reopened.close()
    assert sqlite3.connect(path).execute("SELECT 1").fetchone() == (1,)


def test_source_split_survives_a_merge():
    """Whose call it was must travel with the call. If merges lost it, the
    owner's trades would silently rejoin the model's record."""
    from stocksage.brain import merge_brains
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    src_path, dest_path = tmp / "s.db", tmp / "d.db"
    src = Database(src_path)
    src.record_suggestion("AAA", "BUY", 0.5, 100.0, {}, 5, source=SOURCE_MODEL)
    src.record_suggestion("BBB", "BUY", 0.35, 50.0, {}, 5, source=SOURCE_OWNER)
    src.close()
    Database(dest_path).close()

    merge_brains(dest_path, src_path)
    db = Database(dest_path)
    got = {r["ticker"]: r["source"] for r in db.conn.execute(
        "SELECT ticker, source FROM suggestions")}
    db.close()
    assert got == {"AAA": SOURCE_MODEL, "BBB": SOURCE_OWNER}


# ------------------------------------------------------------- price history

@pytest.mark.parametrize("bad", ["empty", "one_row", "all_nan", "duplicate_index"])
def test_malformed_history_never_crashes_a_scan(bad):
    """One broken ticker in a 110-name universe must cost that ticker, not
    the run — an unattended nightly job that dies learns nothing."""
    from stocksage.indicators import compute_features

    good = make_ohlcv(days=200, seed=2)
    if bad == "empty":
        df = good.iloc[0:0]
    elif bad == "one_row":
        df = good.iloc[:1]
    elif bad == "all_nan":
        df = good.copy()
        df[:] = np.nan
    else:
        df = pd.concat([good, good])

    assert compute_features(df) is None or isinstance(compute_features(df), dict)

    eng = _engine_with({"BAD": df, "OK": good, "SPY": make_ohlcv(days=200, seed=9)})
    result = eng.scan(tickers=["BAD", "OK"], capture_context=False, record=False)
    assert any(s.ticker == "OK" for s in result.suggestions), (
        f"a {bad} ticker took down the whole scan"
    )


# ------------------------------------------------- honesty of the scoreboard

def _scoreboard(rows):
    from stocksage.advisor import scoreboard_headline

    db = Database(":memory:")
    for i, (realized, bench) in enumerate(rows):
        sid = db.record_suggestion(f"T{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        db.mark_evaluated(sid, realized, realized > 0, benchmark_return=bench)
    buys, avoided = paper_trades(db.evaluated_suggestions())
    return scoreboard_headline(db.performance_summary(), profit_stats(buys, avoided), buys)


def test_the_headline_never_calls_a_loss_a_gain():
    """The single most corrosive failure: reading 'earned' when money was
    lost. Beating a falling market is still a loss."""
    for own, bench in [(-0.01, -0.05), (-0.10, -0.20), (-0.001, -0.002)]:
        line = _scoreboard([(own, bench)] * 15)
        assert "lost" in line, f"a loss of {own} was not called a loss: {line}"
        assert "earned" not in line and "returned +" not in line


def test_the_headline_refuses_a_verdict_on_thin_evidence():
    """Trust is built slowly; a confident claim from three data points
    destroys it the first time it reverses."""
    for n in (1, 3, 9):
        line = _scoreboard([(0.10, 0.01)] * n)
        assert "Too early to judge" in line, f"claimed an edge from {n} calls"
        assert "beating the market" not in line


def test_the_headline_states_a_verdict_once_evidence_exists():
    line = _scoreboard([(0.10, 0.01)] * 12)
    assert "beating the market by" in line
    assert "Too early" not in line


def test_a_losing_model_is_never_described_as_winning():
    """The direction of the verdict must follow the arithmetic, always."""
    line = _scoreboard([(0.01, 0.05)] * 20)
    assert "trailing the market by" in line
    assert "beating" not in line


# --------------------------------------------------- long-run accumulation

def test_hundreds_of_graded_calls_keep_every_invariant():
    """Simulates roughly a year of unattended running: the invariants that
    matter must still hold at the end, not just on day one."""
    import random

    rng = random.Random(1234)
    db = Database(":memory:")
    weights = initial_weights(SIGNALS)
    for i in range(600):
        signals = {n: rng.uniform(-1, 1) for n in SIGNALS}
        realized = rng.gauss(0.002, 0.05)
        bench = rng.gauss(0.001, 0.02)
        sid = db.record_suggestion(f"T{i % 40}", "BUY", 0.4, 100.0, signals, 5)
        db.mark_evaluated(sid, realized, realized > 0, benchmark_return=bench)
        weights, _ = update_weights(weights, signals, realized - bench)

    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(np.isfinite(v) and v > 0 for v in weights.values())
    buys, avoided = paper_trades(db.evaluated_suggestions())
    stats = profit_stats(buys, avoided)
    assert len(buys) == 600
    for key, value in stats.items():
        if isinstance(value, float):
            assert np.isfinite(value), f"{key} degenerated after 600 calls"
    # Per-call figures must stay in a range a human can read.
    assert abs(stats["return_per_trade"]) < 1.0


def test_the_ledger_and_the_track_record_never_disagree_on_count():
    """Two numbers describing the same thing must not drift apart — that is
    how a dashboard starts quietly lying."""
    db = Database(":memory:")
    for i in range(25):
        sid = db.record_suggestion(f"T{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        db.mark_evaluated(sid, 0.01 * (1 if i % 2 else -1), i % 2 == 1,
                          benchmark_return=0.002)
    # An ungradeable row must not inflate either count.
    stray = db.record_suggestion("UNG", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
    db.mark_ungradeable(stray)

    buys, _ = paper_trades(db.evaluated_suggestions())
    assert len(buys) == 25
    assert db.performance_summary()["evaluated"] == 25


def test_owner_and_model_records_never_bleed_into_each_other():
    db = Database(":memory:")
    for i in range(10):
        sid = db.record_suggestion(f"M{i}", "BUY", 0.5, 100.0, {"x": 0.5}, 5,
                                   source=SOURCE_MODEL)
        db.mark_evaluated(sid, 0.05, True, benchmark_return=0.01)
    for i in range(7):
        sid = db.record_suggestion(f"O{i}", "BUY", 0.35, 100.0, {"x": 0.5}, 5,
                                   source=SOURCE_OWNER)
        db.mark_evaluated(sid, -0.05, False, benchmark_return=0.01)

    assert db.performance_summary(source=SOURCE_MODEL)["evaluated"] == 10
    assert db.performance_summary(source=SOURCE_OWNER)["evaluated"] == 7
    assert db.performance_summary(source=None)["evaluated"] == 17
    # The model's hit rate must be its own, untouched by the owner's losses.
    assert db.performance_summary(source=SOURCE_MODEL)["hit_rate"] == pytest.approx(1.0)
    assert db.performance_summary(source=SOURCE_OWNER)["hit_rate"] == pytest.approx(0.0)
