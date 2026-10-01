"""Looking inside the brain, and mending only what is provably broken.

These build the situation the owner's real brain is believed to be in:
calls graded before market-relative grading existed (inflated, no
benchmark), owner trades recorded twice by a device move, and weight updates
that learned from raw returns. The audit must report each plainly, never
write to the file, and the repair must touch nothing that is not an exact
duplicate.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from stocksage import audit
from stocksage.db import SOURCE_MODEL, SOURCE_OWNER, Database

WHEN = "2026-08-03T14:00:00+00:00"


def _seed(tmp_path):
    db = Database(tmp_path / "brain.db")

    def model_call(ticker, realized, bench):
        sid = db.record_suggestion(
            ticker, "BUY", 0.4, 100.0, {"trend_long": 0.4}, 5, source=SOURCE_MODEL
        )
        db.mark_evaluated(sid, realized, realized > 0, bench)
        db.log_learning(sid, {
            "realized_return": realized - (bench or 0), "updates": {},
            "raw_return": realized, "benchmark_return": bench,
        })
        return sid

    # Calls graded by the old method: no benchmark, wildly good-looking.
    for i in range(4):
        model_call(f"OLD{i}", 0.12, None)
    # Calls graded honestly: market-measured, and unremarkable.
    for i in range(3):
        model_call(f"NEW{i}", -0.005, 0.0025)

    # Owner trades, one of them recorded twice (the device-move duplicate).
    for oid, price in (("o1", 100.0), ("o2", 101.0)):
        db.upsert_rh_orders([{"order_id": oid, "ticker": "AAPL", "side": "buy",
                              "quantity": 1, "price": price, "executed_at": WHEN}])
        sid = db.record_suggestion("AAPL", "BUY", 0.35, price, {}, 5,
                                   created_at=WHEN, source=SOURCE_OWNER)
        db.mark_fill_ingested(oid, sid)
    dup = db.record_suggestion("AAPL", "BUY", 0.35, 100.0, {}, 5,
                               created_at=WHEN, source=SOURCE_OWNER)
    db.mark_fill_ingested("o1-again", dup)
    db.close()
    return tmp_path / "brain.db"


def test_audit_reports_what_is_inside(tmp_path):
    a = audit.audit_brain(_seed(tmp_path))
    assert a["by_source"] == {SOURCE_MODEL: 7, SOURCE_OWNER: 3}
    assert a["model_graded"] == 7
    assert a["model_with_benchmark"] == 3 and a["model_without_benchmark"] == 4
    assert a["duplicate_rows"] == 1 and a["duplicate_groups"] == 1
    assert a["updates_raw_return"] == 4 and a["updates_market_relative"] == 3


def test_audit_separates_the_inflated_calls_from_the_honest_ones(tmp_path):
    """The whole point: the blended average hides the problem; splitting by
    whether a market benchmark exists exposes it."""
    a = audit.audit_brain(_seed(tmp_path))
    assert a["avg_return_without_benchmark"] == pytest.approx(0.12)
    assert a["avg_return_with_benchmark"] == pytest.approx(-0.005)
    assert a["avg_edge_with_benchmark"] == pytest.approx(-0.0075)


def test_audit_never_writes_to_the_brain(tmp_path):
    path = _seed(tmp_path)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    audit.audit_brain(path)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before


def test_audit_of_a_missing_brain_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit.audit_brain(tmp_path / "nope.db")


def test_audit_describes_each_problem_in_plain_language(tmp_path):
    text = "\n".join(audit.describe(audit.audit_brain(_seed(tmp_path))))
    assert "recorded twice" in text
    assert "RAW returns" in text
    assert "brain repair --apply" in text
    assert "+12.00%" in text and "-0.50%" in text


def test_a_healthy_brain_is_reported_healthy(tmp_path):
    db = Database(tmp_path / "ok.db")
    sid = db.record_suggestion("AAA", "BUY", 0.4, 10.0, {}, 5, source=SOURCE_MODEL)
    db.mark_evaluated(sid, 0.01, True, 0.005)
    db.log_learning(sid, {"realized_return": 0.005, "updates": {},
                          "raw_return": 0.01, "benchmark_return": 0.005})
    db.close()
    text = "\n".join(audit.describe(audit.audit_brain(tmp_path / "ok.db")))
    assert "Nothing wrong found" in text


# --- repair ---------------------------------------------------------------


def test_dry_run_changes_nothing(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    res = audit.repair_duplicates(path, apply=False)
    assert res["rows_removed"] == 1 and res["applied"] is False
    assert path.read_bytes() == before


def test_apply_removes_only_the_duplicate_and_backs_up_first(tmp_path):
    path = _seed(tmp_path)
    res = audit.repair_duplicates(path, apply=True)

    assert res["applied"] and res["rows_removed"] == 1
    conn = sqlite3.connect(str(path))
    owner = conn.execute(
        "SELECT price FROM suggestions WHERE source = 'owner' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in owner] == [100.0, 101.0], "a real trade was removed"
    assert conn.execute(
        "SELECT COUNT(*) FROM suggestions WHERE source = 'model'"
    ).fetchone()[0] == 7, "model calls must never be touched"

    # The backup is the brain as it was BEFORE the repair.
    backup = sqlite3.connect(res["backup"])
    assert backup.execute(
        "SELECT COUNT(*) FROM suggestions WHERE source = 'owner'"
    ).fetchone()[0] == 3


def test_the_ledger_points_at_the_survivor(tmp_path):
    path = _seed(tmp_path)
    audit.repair_duplicates(path, apply=True)
    conn = sqlite3.connect(str(path))
    keep = conn.execute(
        "SELECT MIN(id) FROM suggestions WHERE source='owner' AND price = 100.0"
    ).fetchone()[0]
    ids = {r[0] for r in conn.execute(
        "SELECT suggestion_id FROM ingested_fills WHERE order_id IN ('o1','o1-again')"
    )}
    assert ids == {keep}, "ledger rows must not point at a deleted call"


def test_repair_is_idempotent(tmp_path):
    path = _seed(tmp_path)
    audit.repair_duplicates(path, apply=True)
    again = audit.repair_duplicates(path, apply=True)
    assert again["rows_removed"] == 0
    assert audit.audit_brain(path)["duplicate_rows"] == 0


def test_two_different_trades_are_never_merged(tmp_path):
    """Same name, side and instant but a different price is a different trade."""
    path = _seed(tmp_path)
    res = audit.repair_duplicates(path, apply=True)
    conn = sqlite3.connect(str(path))
    assert conn.execute(
        "SELECT COUNT(*) FROM suggestions WHERE source='owner'"
    ).fetchone()[0] == 2
    assert res["rows_removed"] == 1


def test_a_repaired_brain_resyncs_without_recreating_the_duplicates(tmp_path):
    """The end-to-end promise: after repair, the next sync adds nothing."""
    from stocksage.advisor import ingest_fills
    from stocksage.engine import Engine
    from tests.conftest import make_ohlcv
    from tests.test_engine import FakeMarket

    path = _seed(tmp_path)
    audit.repair_duplicates(path, apply=True)
    engine = Engine(db=Database(path),
                    market=FakeMarket({"AAPL": make_ohlcv(days=400, seed=81)}))
    orders = [dict(r) for r in engine.db.rh_orders()]
    assert ingest_fills(engine, orders)["fills_ingested"] == 0
    assert audit.audit_brain(path)["duplicate_rows"] == 0


def test_repair_rebuilds_a_missing_ledger_from_content(tmp_path):
    """A brain merged before the ledger travelled has orders with no ledger
    entry at all. Repair must link each to the call it already became."""
    path = _seed(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM ingested_fills")
    conn.commit()
    conn.close()

    assert audit.audit_brain(path)["orders_without_ledger"] == 2
    res = audit.repair_duplicates(path, apply=True)

    assert res["ledger_linked"] == 2
    after = audit.audit_brain(path)
    assert after["orders_without_ledger"] == 0
    conn = sqlite3.connect(str(path))
    # Each order is linked to a call that still exists, not a deleted one.
    dangling = conn.execute(
        "SELECT COUNT(*) FROM ingested_fills WHERE suggestion_id NOT IN"
        " (SELECT id FROM suggestions)"
    ).fetchone()[0]
    assert dangling == 0
