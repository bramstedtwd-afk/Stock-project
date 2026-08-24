"""Moving to a new machine: the guided setup must not lose the brain.

The brain is the one thing a new install cannot recreate — credentials get
retyped, Drive gets re-consented, but months of graded calls exist only in
that file. So the failures worth testing are the quiet ones: offering a
half-downloaded file as if it were valid, or reporting success while the
brain stayed empty.
"""

from __future__ import annotations

import sqlite3

import pytest

from stocksage import firstrun
from stocksage.db import Database


def _brain(path, calls=3):
    db = Database(path)
    db.save_weights({"trend_long": 0.5})
    for i in range(calls):
        db.record_suggestion(f"AAA{i}", "BUY", 0.4, 100.0 + i, {"trend_long": 0.4}, 5)
    db.record_move_event("NVDA", "2026-08-01", 0.06, 2.4, ["earnings"], [])
    db.close()
    return path


def test_finds_a_downloaded_brain(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    _brain(downloads / "brain-snapshot.db")
    assert firstrun.find_brain_files([downloads]) == [downloads / "brain-snapshot.db"]


def test_a_truncated_download_is_never_offered(tmp_path):
    """A part-downloaded file is not a brain. Importing one is worse than
    finding nothing, because the owner believes their history came across."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "brain-snapshot.db").write_bytes(b"SQLite format 3\x00 truncated...")
    assert firstrun.find_brain_files([downloads]) == []


def test_an_empty_but_valid_brain_is_still_offered(tmp_path):
    """Valid and empty is a real case — a fresh export. Let the owner decide."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    Database(downloads / "brain-snapshot.db").close()
    assert firstrun.find_brain_files([downloads])


def test_newest_first_when_several_exist(tmp_path):
    import os
    import time

    root = tmp_path / "Downloads"
    root.mkdir()
    old = _brain(root / "stocksage.db")
    new = _brain(root / "brain-snapshot.db")
    os.utime(old, (time.time() - 5000, time.time() - 5000))
    assert firstrun.find_brain_files([root])[0] == new


def test_an_unreadable_drive_letter_is_not_an_error(tmp_path):
    """Windows reports empty card readers as drives that raise on access."""
    assert firstrun.find_brain_files([tmp_path / "nope", tmp_path]) == []


def test_importing_brings_the_history_across(tmp_path, monkeypatch):
    source = _brain(tmp_path / "old-machine.db", calls=4)
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "new.db"))
    assert firstrun.brain_is_populated() is False

    stats = firstrun.import_brain_file(source)
    assert stats["suggestions_added"] == 4
    assert firstrun.brain_is_populated() is True


def test_a_damaged_brain_file_raises_rather_than_half_importing(tmp_path, monkeypatch):
    bad = tmp_path / "damaged.db"
    bad.write_bytes(b"not a database")
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "new.db"))
    with pytest.raises(sqlite3.DatabaseError):
        firstrun.import_brain_file(bad)


def test_account_number_is_stored_digits_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", env)
    assert firstrun.set_agentic_account("  123-456-789 ") == "123456789"
    assert "STOCKSAGE_AGENTIC_ACCOUNT=123456789" in env.read_text(encoding="utf-8")


def test_a_non_number_is_rejected_not_silently_stored(tmp_path, monkeypatch):
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", tmp_path / ".env")
    with pytest.raises(ValueError):
        firstrun.set_agentic_account("the roth one")


def test_remaining_steps_reports_each_piece(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_DB", str(_brain(tmp_path / "b.db")))
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "123456789")
    monkeypatch.setenv("ROBINHOOD_USERNAME", "a@b.c")
    monkeypatch.setenv("ROBINHOOD_PASSWORD", "pw")
    steps = {s.key: s for s in firstrun.remaining_steps()}
    assert steps["brain"].done and steps["account"].done and steps["robinhood"].done
    assert "6789" in steps["account"].detail
    assert "123456789" not in steps["account"].detail, "printed the whole number"


def test_setup_command_runs_end_to_end_without_input(tmp_path, monkeypatch, capsys):
    """Enter-through-everything must still finish and report, not hang."""
    from stocksage.cli import main

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))
    # This test is about the not-yet-linked path, so say so rather than
    # inheriting whatever an earlier test left in the environment.
    for key in ("ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("stocksage.cli._ask", lambda *a, **k: "")
    monkeypatch.setattr("stocksage.firstrun.find_brain_files", lambda roots=None: [])
    monkeypatch.setattr("stocksage.doctor.run_doctor", lambda: 0)
    main(["setup"])
    out = capsys.readouterr().out
    assert "SETTING UP STOCKSAGE" in out
    assert "Portfolio tab" in out


def test_setup_never_asks_for_a_password(tmp_path, monkeypatch, capsys):
    """Credentials belong in the dashboard form, not a terminal prompt."""
    from stocksage.cli import main

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))
    asked: list[str] = []
    monkeypatch.setattr("stocksage.cli._ask", lambda p, d="": asked.append(p) or "")
    monkeypatch.setattr("stocksage.firstrun.find_brain_files", lambda roots=None: [])
    monkeypatch.setattr("stocksage.doctor.run_doctor", lambda: 0)
    main(["setup"])
    for prompt in asked:
        low = prompt.lower()
        assert "password" not in low and "username" not in low
