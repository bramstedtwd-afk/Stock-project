"""Dashboard smoke tests: run the real app.py through Streamlit's AppTest.

Every tab's code executes on a run, so this catches runtime errors that
compile checks can't — bad column references, None-handling in render
code, import mistakes inside tab blocks. Uses a seeded brain so the
auto-run-on-open path is skipped (no network in tests).
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from stocksage.db import Database  # noqa: E402

# Absolute, because Streamlit >=1.61 resolves a relative AppTest path against
# the file that calls it (so "app.py" became tests/app.py and every dashboard
# test died with FileNotFoundError). An absolute path is correct on both.
APP = str(Path(__file__).resolve().parent.parent / "app.py")
RUN_TIMEOUT = 30


@pytest.fixture
def seeded_brain(tmp_path, monkeypatch):
    """A brain with every kind of knowledge, and today's run already done."""
    db_path = tmp_path / "brain.db"
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    db = Database(db_path)
    # Graded + pending suggestions
    for i, (ticker, action, score, realized) in enumerate(
        [("AAPL", "BUY", 0.4, 0.05), ("NVDA", "STRONG BUY", 0.6, -0.02),
         ("XOM", "SELL", -0.3, -0.04)]
    ):
        sid = db.record_suggestion(ticker, action, score, 100.0 + i, {"trend_long": score}, 5)
        db.mark_evaluated(sid, realized, (realized > 0) == (score > 0))
    db.record_suggestion("MSFT", "BUY", 0.35, 420.0, {"trend_long": 0.35}, 5)
    # Learned state
    db.save_weights({"trend_long": 0.5, "macd": 0.3, "rsi_reversion": 0.2})
    db.save_sector_weights("Technology", {"trend_long": 0.7, "macd": 0.3})
    db.set_meta("sector_grades:Technology", "12")
    # Move memory
    db.record_move_event(
        "NVDA", (date.today() - timedelta(days=2)).isoformat(), -0.055, 2.8,
        ["earnings"], [{"title": "NVDA falls on earnings", "publisher": "T", "link": ""}],
    )
    db.record_move_event("AAPL", "2026-01-15", 0.04, 2.2, ["historical-backfill"], [])
    # Robinhood mirror
    db.upsert_rh_orders(
        [
            {"order_id": "o1", "ticker": "AAPL", "side": "buy", "quantity": 10,
             "price": 180.0, "executed_at": "2026-03-02T15:00:00Z"},
            {"order_id": "o2", "ticker": "AAPL", "side": "sell", "quantity": 10,
             "price": 205.0, "executed_at": "2026-05-11T15:00:00Z"},
        ]
    )
    db.upsert_rh_dividends(
        [{"dividend_id": "d1", "ticker": "AAPL", "amount": 2.4, "paid_at": "2026-04-01"}]
    )
    # Skip auto-run and bootstrap in tests (no network)
    db.set_meta("bootstrap_done", date.today().isoformat())
    db.set_meta("last_daily_run", date.today().isoformat())
    db.set_meta("warmup_samples", "4500")
    db.set_meta("last_device", "test-machine")
    db.set_meta("last_device_at", "2026-07-09T12:00:00+00:00")
    db.close()
    return db_path


def test_dashboard_renders_without_exceptions(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"dashboard raised: {at.exception}"


def test_sidebar_shows_brain_status(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    sidebar_text = " ".join(c.value for c in at.sidebar.caption)
    assert "moves remembered" in sidebar_text
    assert "test-machine" in sidebar_text


def test_learning_tab_metrics_populated(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    labels = [m.label for m in at.metric]
    assert "Historical samples" in labels
    assert "Suggestions graded" in labels
    # Profit tab rendered its ledger metrics too
    assert "Paper P&L" in labels


def test_corrupted_brain_shows_recovery_not_traceback(tmp_path, monkeypatch):
    """A damaged brain file (partial cloud sync) degrades to instructions."""
    db_path = tmp_path / "damaged.db"
    db_path.write_bytes(b"this is not a sqlite database, sorry")
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception
    assert at.error, "expected a recovery message"
    assert "damaged" in at.error[0].value


def test_empty_brain_renders_cleanly(tmp_path, monkeypatch):
    """A brand-new install (nothing learned, no Robinhood) must not error."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    db = Database(db_path)
    # Mark today as run so the app doesn't attempt a network scan in tests.
    db.set_meta("last_daily_run", date.today().isoformat())
    db.close()
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"fresh-install dashboard raised: {at.exception}"
