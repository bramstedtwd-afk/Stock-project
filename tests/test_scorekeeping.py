"""The sheet's forward record must be honest about luck and about privacy."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from stocksage import scorekeeping as sk
from stocksage import today
from stocksage.db import Database
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


def tiny_sheet(**calls):
    """A sheet with just the calls given: kind -> [tickers]."""
    d = []
    ideas = []
    for kind, tickers in calls.items():
        for t in tickers:
            if kind == "idea":
                ideas.append(t)
                continue
            verb, basis = {"sell_rule": ("SELL", "risk rule"), "sell_model": ("SELL", "model call"),
                           "trim": ("TRIM", "risk rule"), "buy": ("BUY", "model call")}[kind]
            d.append(today.Directive(verb, t, f"{verb} {t}", basis))
    acct = today.AccountSheet(role="personal", title="Personal", label="x", manual=True,
                              directives=d, ideas=ideas)
    return today.Sheet(generated_at="x", accounts=[acct])


class Market(FakeMarket):
    """SPY drifts at +0.0% and each ticker is given a fixed daily drift."""

    def __init__(self, drifts, days=80):
        frames = {"SPY": make_ohlcv(days=days, daily_drift=0.0, daily_vol=0.0001, seed=1)}
        for t, mu in drifts.items():
            frames[t] = make_ohlcv(days=days, daily_drift=mu, daily_vol=0.0001, seed=2)
        super().__init__(frames)


def test_each_kind_of_call_is_logged_once_per_ticker_while_it_is_live():
    db = Database(":memory:")
    sheet = tiny_sheet(sell_rule=["AAA"], idea=["BBB"], buy=["CCC"], trim=["DDD"], sell_model=["EEE"])
    assert sk.record(db, sheet, date(2026, 7, 6)) == 5
    assert sk.record(db, sheet, date(2026, 7, 7)) == 0, "the same live call is not logged again tomorrow"
    kinds = {r["ticker"]: r["kind"] for r in db.conn.execute("SELECT ticker, kind FROM sheet_calls")}
    assert kinds == {"AAA": "sell_rule", "BBB": "idea", "CCC": "buy", "DDD": "trim", "EEE": "sell_model"}


def test_a_call_waits_until_its_horizon_has_passed():
    db = Database(":memory:")
    market = Market({"AAA": 0.01})
    last = market.frames["AAA"].index[-1].date()
    sk.record(db, tiny_sheet(idea=["AAA"]), last - timedelta(days=4))     # ~3 bars old
    assert sk.grade(db, market) == 0
    sk.record(db, tiny_sheet(idea=["ZZZ"]), last - timedelta(days=40))
    db.conn.execute("UPDATE sheet_calls SET call_date = ? WHERE ticker = 'AAA'",
                    ((market.frames["AAA"].index[-30]).date().isoformat(),))
    assert sk.grade(db, market) == 1


def test_a_rising_idea_scores_as_a_win_and_a_sell_of_a_riser_as_a_loss():
    db = Database(":memory:")
    market = Market({"UP": 0.01})
    when = market.frames["UP"].index[-40].date()
    sk.record(db, tiny_sheet(idea=["UP"], sell_model=["UP"]), when)
    assert sk.grade(db, market) == 2
    rows = {r["kind"]: r for r in db.conn.execute("SELECT kind, ret, bench_ret FROM sheet_calls")}
    assert rows["idea"]["ret"] > 0.05 and abs(rows["idea"]["bench_ret"]) < 0.01
    card = sk.scorecard(db)
    assert card["idea"]["edge"] > 0.05
    assert card["sell_model"]["edge"] < -0.05, "selling something that then beat the market is a miss"


def test_grading_uses_the_spy_return_over_the_same_bars():
    db = Database(":memory:")
    frames = {"SPY": make_ohlcv(days=80, daily_drift=0.01, daily_vol=0.0001, seed=1),
              "TWIN": make_ohlcv(days=80, daily_drift=0.01, daily_vol=0.0001, seed=1)}
    market = FakeMarket(frames)
    sk.record(db, tiny_sheet(idea=["TWIN"]), frames["SPY"].index[-40].date())
    sk.grade(db, market)
    assert sk.scorecard(db)["idea"]["edge"] == pytest.approx(0.0, abs=1e-9), "it moved with the market"


def _seed_graded(db, kind, edges, start=date(2025, 1, 6), spacing=15):
    for i, e in enumerate(edges):
        day = (start + timedelta(days=spacing * i)).isoformat()
        sign = sk.SIGN[kind]
        db.conn.execute(
            "INSERT INTO sheet_calls (call_date, ticker, kind, horizon_days, evaluated, ret, bench_ret)"
            " VALUES (?, ?, ?, 10, 1, ?, 0.0)", (day, f"T{i}", kind, sign * e))
    db.conn.commit()


def test_the_verdict_is_the_shared_definition_of_proof_not_a_private_one():
    from stocksage.actions import trust_level

    db = Database(":memory:")
    rng = np.random.default_rng(0)
    _seed_graded(db, "idea", list(rng.normal(0.02, 0.01, 120)))
    e = sk.scorecard(db)["idea"]
    assert e["level"] == trust_level(e["graded"], e["edge"], e["se"]) == "earned"


def test_a_pile_of_calls_in_one_fortnight_is_one_observation_not_many():
    """Fifty names called the same week all rode the same market: 50 wins in
    one fortnight must not 'earn' anything."""
    db = Database(":memory:")
    for i in range(50):
        db.conn.execute(
            "INSERT INTO sheet_calls (call_date, ticker, kind, horizon_days, evaluated, ret, bench_ret)"
            " VALUES ('2026-03-02', ?, 'idea', 10, 1, 0.05, 0.0)", (f"T{i}",))
    db.conn.commit()
    e = sk.scorecard(db)["idea"]
    assert e["graded"] == 50 and e["buckets"] == 1 and e["level"] == "unproven" and e["se"] is None


def test_noise_is_not_crowned():
    db = Database(":memory:")
    _seed_graded(db, "idea", list(np.random.default_rng(3).normal(0.0, 0.03, 150)))
    assert sk.scorecard(db)["idea"]["level"] != "earned"


def test_the_words_say_too_early_until_there_is_enough_to_judge():
    db = Database(":memory:")
    assert sk.lines(db) == []
    _seed_graded(db, "trim", [0.01, 0.02, -0.01])
    text = "\n".join(sk.lines(db))
    assert "too early to call" in text and "size-cap trims" in text and "3 graded" in text


def test_the_sheet_logs_its_calls_only_when_asked_to_track():
    from stocksage.analogs import AnalogBook
    from stocksage.engine import Engine
    from tests.test_backtest import make_samples
    from tests.test_today import STRONG, FakeBroker, default_suggestions

    book = AnalogBook.from_samples(make_samples(signal=0.02, n_dates=140, seed=1))
    engine = Engine(db=Database(":memory:"), market=FakeMarket({}))
    today.build_sheet(engine, FakeBroker(), suggestions=default_suggestions(STRONG), book=book, news=False)
    assert engine.db.conn.execute("SELECT COUNT(*) FROM sheet_calls").fetchone()[0] == 0
    today.build_sheet(engine, FakeBroker(), suggestions=default_suggestions(STRONG), book=book, news=False,
                      track=True)
    assert engine.db.conn.execute("SELECT COUNT(*) FROM sheet_calls").fetchone()[0] > 0


def test_a_broken_market_feed_never_breaks_the_sheet():
    class Down(FakeMarket):
        def history(self, ticker, period="1y"):
            raise OSError("down")

    from stocksage.engine import Engine

    engine = Engine(db=Database(":memory:"), market=Down({}))
    assert sk.update(engine, tiny_sheet(idea=["AAA"])) == []


# ------------------------------------------------------------ privacy and moving machines

def test_the_forward_record_is_private_and_stripped_from_the_snapshot(tmp_path):
    from stocksage import brain

    assert "sheet_calls" in brain.PRIVATE_TABLES
    db = Database(tmp_path / "b.db")
    _seed_graded(db, "trim", [0.01])
    db.close()
    removed = brain.scrub_personal_data(tmp_path / "b.db")
    assert removed["sheet_calls"] == 1


def test_the_forward_record_follows_the_owner_to_a_new_machine(tmp_path):
    from stocksage import brain

    src, dst = Database(tmp_path / "a.db"), Database(tmp_path / "b.db")
    _seed_graded(src, "idea", [0.01, 0.02])
    src.close()
    dst.close()
    brain.merge_brains(tmp_path / "b.db", tmp_path / "a.db")
    stats = brain.merge_brains(tmp_path / "b.db", tmp_path / "a.db")        # twice: idempotent
    again = Database(tmp_path / "b.db")
    assert again.conn.execute("SELECT COUNT(*) FROM sheet_calls").fetchone()[0] == 2
    assert stats["sheet_calls_added"] == 0


def test_an_older_brain_without_the_table_still_merges(tmp_path):
    from stocksage import brain

    old = Database(tmp_path / "old.db")
    old.conn.execute("DROP TABLE sheet_calls")
    old.conn.commit()
    old.close()
    Database(tmp_path / "new.db").close()
    stats = brain.merge_brains(tmp_path / "new.db", tmp_path / "old.db")
    assert "sheet_calls_added" not in stats
