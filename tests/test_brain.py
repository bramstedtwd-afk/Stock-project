import os

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


def test_db_path_honors_env_at_call_time(tmp_path, monkeypatch):
    """Regression: STOCKSAGE_DB from .env must apply even after module import."""
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "custom.db"))
    db = Database()
    assert db.path == tmp_path / "custom.db"
    db.close()
