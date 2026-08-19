
import pytest

from stocksage import brain
from stocksage.db import Database


def make_brain(path, ticker="AAPL", weights=None, updated_hint=None):
    db = Database(path)
    db.record_suggestion(ticker, "BUY", 0.4, 100.0, {"trend_long": 0.5}, 5)
    db.record_move_event(ticker, "2026-06-01", -0.04, 2.5, ["earnings"], [])
    if weights:
        db.save_weights(weights)
        if updated_hint:  # force a deterministic updated_at for merge ordering
            db.conn.execute("UPDATE weights SET updated_at = ?", (updated_hint,))
            db.conn.commit()
    db.set_meta("warmup_samples", "100")
    db.close()
    return path


def test_export_creates_snapshot(tmp_path):
    src = make_brain(tmp_path / "a.db")
    out = brain.export_brain(tmp_path / "exported.db", db_path=src)
    assert out.exists()
    exported = Database(out)
    assert len(exported.recent_suggestions()) == 1
    assert len(exported.move_events()) == 1
    exported.close()


def test_merge_compounds_knowledge(tmp_path):
    local = make_brain(
        tmp_path / "local.db", "AAPL", {"trend_long": 0.7, "macd": 0.3}, "2026-07-01T00:00:00"
    )
    other = make_brain(
        tmp_path / "other.db", "NVDA", {"trend_long": 0.2, "macd": 0.8}, "2026-07-05T00:00:00"
    )
    stats = brain.merge_brains(local, other)
    assert stats["suggestions_added"] == 1
    assert stats["move_events_added"] == 1
    assert stats["weights_taken_from"] == "imported"  # other learned more recently
    merged = Database(local)
    assert {r["ticker"] for r in merged.recent_suggestions()} == {"AAPL", "NVDA"}
    assert merged.load_weights() == {"trend_long": 0.2, "macd": 0.8}
    assert merged.get_meta("warmup_samples") == "100"
    merged.close()


def test_merge_is_idempotent(tmp_path):
    local = make_brain(tmp_path / "local.db", "AAPL")
    other = make_brain(tmp_path / "other.db", "NVDA")
    brain.merge_brains(local, other)
    stats = brain.merge_brains(local, other)  # second merge adds nothing
    assert stats["suggestions_added"] == 0
    assert stats["move_events_added"] == 0


def test_merge_keeps_newer_local_weights(tmp_path):
    local = make_brain(tmp_path / "local.db", "AAPL", {"macd": 1.0}, "2026-07-06T00:00:00")
    other = make_brain(tmp_path / "other.db", "NVDA", {"macd": 0.1}, "2026-07-01T00:00:00")
    stats = brain.merge_brains(local, other)
    assert stats["weights_taken_from"] == "local"
    merged = Database(local)
    assert merged.load_weights() == {"macd": 1.0}
    merged.close()


def test_import_replace_backs_up(tmp_path):
    target = make_brain(tmp_path / "target.db", "AAPL")
    incoming = make_brain(tmp_path / "incoming.db", "NVDA")
    stats = brain.import_brain(incoming, db_path=target, replace=True)
    assert stats["replaced"] and stats["backup"]
    replaced = Database(target)
    assert {r["ticker"] for r in replaced.recent_suggestions()} == {"NVDA"}
    replaced.close()
    backup = Database(stats["backup"])
    assert {r["ticker"] for r in backup.recent_suggestions()} == {"AAPL"}
    backup.close()


def test_import_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        brain.import_brain(tmp_path / "ghost.db", db_path=tmp_path / "t.db")


def test_sync_moves_brain_and_points_env(tmp_path, monkeypatch):
    local = make_brain(tmp_path / "local.db", "AAPL")
    folder = tmp_path / "Dropbox"
    folder.mkdir()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(brain, "save_env", lambda values: env_file.write_text(
        f"STOCKSAGE_DB={values['STOCKSAGE_DB']}\n"
    ))
    target = brain.sync_to_folder(folder, db_path=local)
    assert target == folder / "stocksage.db" and target.exists()
    assert f"STOCKSAGE_DB={target}" in env_file.read_text()
    assert not local.exists()  # renamed aside so it can't diverge silently
    synced = Database(target)
    assert len(synced.recent_suggestions()) == 1
    synced.close()


def test_sync_merges_with_existing_folder_brain(tmp_path, monkeypatch):
    local = make_brain(tmp_path / "local.db", "AAPL")
    folder = tmp_path / "Dropbox"
    folder.mkdir()
    make_brain(folder / "stocksage.db", "NVDA")  # another device got there first
    monkeypatch.setattr(brain, "save_env", lambda values: None)
    target = brain.sync_to_folder(folder, db_path=local)
    merged = Database(target)
    assert {r["ticker"] for r in merged.recent_suggestions()} == {"AAPL", "NVDA"}
    merged.close()


def test_brain_info_counts(tmp_path):
    path = make_brain(tmp_path / "b.db")
    info = brain.brain_info(db_path=path)
    assert info["suggestions"] == 1
    assert info["move_events"] == 1
    assert info["warmup_samples"] == "100"


def test_detect_cloud_folders(tmp_path):
    (tmp_path / "Dropbox").mkdir()
    (tmp_path / "OneDrive").mkdir()
    found = brain.detect_cloud_folders(home=tmp_path)
    names = [name for name, _ in found]
    assert names == ["Dropbox", "OneDrive"]
    assert all(path.is_dir() for _, path in found)
    assert brain.detect_cloud_folders(home=tmp_path / "empty-nowhere") == []


def test_is_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("STOCKSAGE_DB", raising=False)
    assert brain.is_shared() is False  # default local location
    assert brain.is_shared(tmp_path / "Dropbox" / "stocksage.db") is True


def test_db_path_honors_env_at_call_time(tmp_path, monkeypatch):
    """Regression: STOCKSAGE_DB from .env must apply even after module import."""
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "custom.db"))
    db = Database()
    assert db.path == tmp_path / "custom.db"
    db.close()


def test_merge_from_old_schema_brain_without_benchmark(tmp_path):
    import sqlite3

    from stocksage.brain import merge_brains

    # Source brain exported by an older StockSage: no benchmark_return column.
    src = tmp_path / "old-export.db"
    conn = sqlite3.connect(src)
    conn.executescript(
        "CREATE TABLE suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL,"
        " score REAL NOT NULL, price REAL NOT NULL, signals TEXT NOT NULL,"
        " horizon_days INTEGER NOT NULL, evaluated INTEGER NOT NULL DEFAULT 0,"
        " realized_return REAL, hit INTEGER);"
        "CREATE TABLE move_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL, ticker TEXT NOT NULL, event_date TEXT NOT NULL,"
        " return_pct REAL NOT NULL, atr_multiple REAL, reasons TEXT NOT NULL,"
        " headlines TEXT NOT NULL, UNIQUE (ticker, event_date));"
        "CREATE TABLE weights (signal TEXT PRIMARY KEY, weight REAL NOT NULL,"
        " updated_at TEXT NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO suggestions (created_at, ticker, action, score, price, signals,"
        " horizon_days, evaluated, realized_return, hit)"
        " VALUES ('2026-01-05T00:00:00', 'AAPL', 'BUY', 0.5, 100, '{}', 5, 1, 0.04, 1)"
    )
    conn.commit()
    conn.close()

    dest = tmp_path / "current.db"
    Database(dest).close()
    stats = merge_brains(dest, src)
    assert stats["suggestions_added"] == 1
    db = Database(dest)
    row = db.conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["ticker"] == "AAPL" and row["benchmark_return"] is None
    db.close()


def test_merge_preserves_benchmark_between_current_brains(tmp_path):
    from stocksage.brain import merge_brains

    src_db = Database(tmp_path / "src.db")
    sid = src_db.record_suggestion("NVDA", "BUY", 0.6, 500.0, {"x": 0.6}, 5)
    src_db.mark_evaluated(sid, 0.08, True, benchmark_return=0.02)
    src_db.close()
    dest = tmp_path / "dest.db"
    Database(dest).close()
    merge_brains(dest, tmp_path / "src.db")
    db = Database(dest)
    row = db.conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["benchmark_return"] == 0.02
    db.close()


# --- the repo snapshot is published; the account behind it is not -----------


def _seeded_brain(tmp_path):
    """A brain holding both kinds of thing: knowledge, and account activity."""
    from stocksage.db import SOURCE_MODEL, SOURCE_OWNER, Database

    db = Database(tmp_path / "brain.db")
    db.save_weights({"trend_long": 0.6, "macd": 0.4})
    db.record_move_event("NVDA", "2026-08-01", 0.06, 2.4, ["earnings"], [])
    db.record_suggestion("AAPL", "BUY", 0.4, 210.0, {"trend_long": 0.4}, 5)
    db.record_suggestion(
        "GE", "BUY", 0.3, 100.00, {"trend_long": 0.3}, 5, source=SOURCE_OWNER
    )
    db.upsert_rh_orders(
        [{"order_id": "o1", "ticker": "GE", "side": "buy", "quantity": 0.250000,
          "price": 100.00, "executed_at": "2026-08-01T15:00:00Z"}]
    )
    db.upsert_rh_dividends(
        [{"dividend_id": "d1", "ticker": "GE", "amount": 0.12, "paid_at": "2026-08-05"}]
    )
    assert db.record_suggestion.__name__  # sanity: fixture built something
    db.close()
    return tmp_path / "brain.db", SOURCE_MODEL


def _counts(path):
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # A table an older brain never had counts as zero rows, not a crash.
        return {
            t: (conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if t in present else 0)
            for t in ("rh_orders", "rh_dividends", "ingested_fills",
                      "suggestions", "move_events", "weights")
        }
    finally:
        conn.close()


def test_snapshot_keeps_knowledge_and_drops_the_account(tmp_path, monkeypatch):
    """brain/brain-snapshot.db is committed to a PUBLIC repo. Learned weights
    and move memory are the point of it; a mirror of the owner's real fills —
    ticker, share count, execution price, timestamp — must not ride along."""
    from stocksage import brain

    db_path, _ = _seeded_brain(tmp_path)
    snapshot = tmp_path / "snapshot.db"
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", snapshot)

    brain.write_snapshot(db_path=db_path)
    counts = _counts(snapshot)

    assert counts["rh_orders"] == 0, "real Robinhood fills were published"
    assert counts["rh_dividends"] == 0
    assert counts["ingested_fills"] == 0
    # The owner's own trades, re-shaped as calls, are the same data.
    assert counts["suggestions"] == 1, "owner-sourced calls were published"
    # Knowledge survives — otherwise the snapshot has no reason to exist.
    assert counts["move_events"] == 1
    assert counts["weights"] == 2
    # The original brain is untouched: scrubbing is for the copy only.
    assert _counts(db_path)["rh_orders"] == 1


def test_scrubbed_rows_are_not_recoverable_from_the_file(tmp_path, monkeypatch):
    """A DELETE leaves the old rows in the file's free pages. Anyone who
    downloads the repo can read them back, so the scrub must VACUUM."""
    from stocksage import brain

    db_path, _ = _seeded_brain(tmp_path)
    snapshot = tmp_path / "snapshot.db"
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", snapshot)
    brain.write_snapshot(db_path=db_path)

    raw = snapshot.read_bytes()
    assert b"100.00" not in raw, "an execution price survived in the file's slack space"
    assert raw.count(b"o1") == 0, "a Robinhood order id survived in the file"


def test_scrub_is_idempotent_and_safe_on_an_older_brain(tmp_path):
    """Snapshots written by older versions predate some tables entirely."""
    import sqlite3

    from stocksage import brain

    db_path, _ = _seeded_brain(tmp_path)
    snapshot = tmp_path / "snap.db"
    brain.export_brain(snapshot, db_path=db_path)
    conn = sqlite3.connect(str(snapshot))
    conn.execute("DROP TABLE rh_dividends")
    conn.commit()
    conn.close()

    brain.scrub_personal_data(snapshot)
    assert brain.scrub_personal_data(snapshot)["rh_orders"] == 0  # nothing left to remove
    assert _counts(snapshot)["weights"] == 2


def test_the_committed_snapshot_in_this_repo_is_clean():
    """Guards the file that is actually published, not just the code path."""
    from stocksage.brain import PRIVATE_TABLES, SNAPSHOT_PATH

    if not SNAPSHOT_PATH.exists():
        pytest.skip("no snapshot committed")
    counts = _counts(SNAPSHOT_PATH)
    for table in PRIVATE_TABLES:
        assert counts[table] == 0, f"{table} in the committed snapshot has real data"
