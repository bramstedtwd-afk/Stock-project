from stocksage.db import Database


def test_suggestion_roundtrip():
    db = Database(":memory:")
    sid = db.record_suggestion("AAPL", "BUY", 0.4, 200.0, {"trend_long": 0.5}, 5)
    rows = db.recent_suggestions()
    assert rows[0]["id"] == sid and rows[0]["ticker"] == "AAPL"
    db.mark_evaluated(sid, 0.03, True)
    summary = db.performance_summary()
    assert summary["evaluated"] == 1
    assert summary["hit_rate"] == 1.0


def test_weights_upsert():
    db = Database(":memory:")
    db.save_weights({"a": 0.6, "b": 0.4})
    db.save_weights({"a": 0.7, "b": 0.3})
    w = db.load_weights()
    assert w == {"a": 0.7, "b": 0.3}


def test_move_event_dedup():
    db = Database(":memory:")
    for _ in range(2):
        db.record_move_event("NVDA", "2026-07-07", -0.05, 3.1, ["earnings"], [{"title": "x"}])
    assert len(db.move_events("NVDA")) == 1


def test_empty_performance_summary():
    db = Database(":memory:")
    s = db.performance_summary()
    assert s["evaluated"] == 0 and s["hit_rate"] is None


def test_old_brain_gains_benchmark_column_in_place(tmp_path):
    import sqlite3

    # A brain created before benchmark_return existed.
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute(
        "CREATE TABLE suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL,"
        " score REAL NOT NULL, price REAL NOT NULL, signals TEXT NOT NULL,"
        " horizon_days INTEGER NOT NULL, evaluated INTEGER NOT NULL DEFAULT 0,"
        " realized_return REAL, hit INTEGER)"
    )
    conn.execute(
        "INSERT INTO suggestions (created_at, ticker, action, score, price, signals,"
        " horizon_days) VALUES ('2026-01-05T00:00:00', 'AAPL', 'BUY', 0.5, 100, '{}', 5)"
    )
    conn.commit()
    conn.close()

    db = Database(old)  # opening migrates additively
    sid = db.conn.execute("SELECT id FROM suggestions").fetchone()["id"]
    db.mark_evaluated(sid, 0.03, True, benchmark_return=0.01)
    row = db.conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["benchmark_return"] == 0.01
    db.close()
