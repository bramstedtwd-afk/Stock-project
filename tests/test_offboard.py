"""Leaving a machine for good: nothing of the owner may stay behind.

The dangerous outcome here is a half-teardown that reports success — the
owner walks away believing the machine is clean while .env still holds a
live brokerage password. So these tests care about two things: that
everything sensitive is actually gone, and that the command refuses rather
than proceeding whenever it cannot finish honestly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stocksage import offboard
from stocksage.cli import main


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with everything StockSage ever writes, present and full."""
    state = tmp_path / ".stocksage"
    state.mkdir()
    env = tmp_path / ".env"
    env.write_text("ROBINHOOD_USERNAME=a@b.c\nROBINHOOD_PASSWORD=secret\n", encoding="utf-8")
    pickle = tmp_path / "robinhood.pickle"
    pickle.write_bytes(b"bearer-token")
    for name in ("drive_token.json", "drive_credentials.json",
                 "daily.log", "publish.log", "server.log", "access.log"):
        (state / name).write_text("x", encoding="utf-8")

    from stocksage.db import Database

    db = Database(state / "stocksage.db")
    db.save_weights({"trend_long": 0.5})
    db.record_suggestion("AAPL", "BUY", 0.4, 100.0, {"trend_long": 0.4}, 5)
    db.close()

    monkeypatch.setenv("STOCKSAGE_DB", str(state / "stocksage.db"))
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", env)
    monkeypatch.setattr(offboard, "sensitive_targets", lambda: [
        offboard.Target(env, "your Robinhood username and password", "plain text"),
        offboard.Target(pickle, "the cached broker session", "a bearer token"),
        offboard.Target(state / "drive_token.json", "your Google Drive token", "reaches Drive"),
        offboard.Target(state / "drive_credentials.json", "your Google API client", "identifies you"),
        offboard.Target(state / "stocksage.db", "the brain", "your trading history"),
        offboard.Target(state / "access.log", "the broker access log", "when it signed in"),
        offboard.Target(state / "daily.log", "the autopilot log", "run history"),
        offboard.Target(state / "publish.log", "the publish log", "run history"),
        offboard.Target(state / "server.log", "the dashboard log", "run history"),
    ])
    monkeypatch.setattr(offboard, "shortcut_targets", list)
    monkeypatch.setattr("stocksage.autopilot.turn_off", lambda: 0)
    return tmp_path, state, env, pickle


def test_everything_sensitive_is_gone(machine):
    tmp_path, state, env, pickle = machine
    result = offboard.offboard(keep_brain_at=tmp_path / "saved.db")

    assert not env.exists(), "the Robinhood password stayed on the machine"
    assert not pickle.exists(), "the broker session token stayed on the machine"
    assert not (state / "drive_token.json").exists()
    assert not (state / "stocksage.db").exists()
    assert not (state / "access.log").exists()
    assert result.unscheduled, "scheduled jobs were not removed"
    assert not result.failed


def test_the_brain_is_saved_before_anything_is_deleted(machine):
    """Months of graded calls are not reproducible; credentials are."""
    tmp_path, state, _, _ = machine
    saved = tmp_path / "my-brain.db"
    result = offboard.offboard(keep_brain_at=saved)

    assert result.brain_saved_to == str(saved)
    assert saved.exists()
    from stocksage.db import Database

    db = Database(saved)
    assert db.conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 1
    db.close()


def test_a_failed_save_deletes_nothing(machine, monkeypatch):
    """If the brain cannot be written, stop. Credentials can be revoked;
    a deleted brain that was never saved is simply gone."""
    tmp_path, state, env, _ = machine
    monkeypatch.setattr(
        "stocksage.brain.export_brain",
        lambda *a, **k: (_ for _ in ()).throw(OSError("USB stick full")),
    )
    result = offboard.offboard(keep_brain_at=tmp_path / "nope.db")

    assert result.brain_saved_to is None
    assert result.failed
    assert env.exists(), "deleted credentials after failing to save the brain"
    assert (state / "stocksage.db").exists()


def test_a_locked_file_is_reported_not_silently_skipped(machine, monkeypatch):
    tmp_path, state, env, _ = machine
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.name == ".env":
            raise PermissionError("file in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    result = offboard.offboard(keep_brain_at=tmp_path / "saved.db")
    assert any("password" in f for f in result.failed)
    assert result.removed, "one locked file must not abort the rest"


def test_a_scheduler_that_refuses_does_not_stop_the_wipe(machine, monkeypatch):
    tmp_path, state, env, _ = machine
    monkeypatch.setattr(
        "stocksage.autopilot.turn_off",
        lambda: (_ for _ in ()).throw(RuntimeError("access denied")),
    )
    result = offboard.offboard(keep_brain_at=tmp_path / "saved.db")
    assert any("scheduled jobs" in f for f in result.failed)
    assert not env.exists(), "the password must come off even if unscheduling fails"


# --- the command must not fire by accident ---------------------------------


def test_a_cloud_synced_brain_is_left_alone(machine, monkeypatch, capsys):
    """A shared brain is the copy that follows the owner to the next machine.
    Deleting it here would delete it out of every device that syncs it."""
    tmp_path, state, env, _ = machine
    monkeypatch.setattr(offboard, "brain_is_shared", lambda: True)
    result = offboard.offboard()

    assert (state / "stocksage.db").exists(), "deleted a brain that syncs elsewhere"
    assert result.brain_left_in_sync_folder == str(state / "stocksage.db")
    assert not env.exists(), "the credentials must still be removed"


def test_bare_command_changes_nothing_and_says_so(machine, capsys):
    tmp_path, state, env, _ = machine
    assert main(["leave"]) == 1
    assert env.exists() and (state / "stocksage.db").exists()
    assert "Nothing has been changed" in capsys.readouterr().out


def test_it_refuses_until_the_brain_question_is_answered(machine, capsys):
    tmp_path, state, env, _ = machine
    assert main(["leave", "--yes"]) == 1
    assert (state / "stocksage.db").exists(), "deleted the brain without being told to"
    assert env.exists()
    out = capsys.readouterr().out
    assert "--keep-brain" in out and "--forget-brain" in out


def test_forget_brain_plus_yes_completes_and_prints_the_revocations(machine, capsys):
    tmp_path, state, env, _ = machine
    assert main(["leave", "--forget-brain", "--yes"]) == 0
    assert not env.exists()
    assert not (state / "stocksage.db").exists()
    out = capsys.readouterr().out
    # The revocations are the part that actually protects the account.
    assert "change your password" in out
    assert "myaccount.google.com/permissions" in out
    assert "read" in out.lower() and "cannot place" in out


def test_keep_brain_path_is_honoured_through_the_cli(machine, capsys):
    tmp_path, state, env, _ = machine
    saved = tmp_path / "take-with-me.db"
    assert main(["leave", "--keep-brain", str(saved), "--yes"]) == 0
    assert saved.exists()
    assert not (state / "stocksage.db").exists()
    assert "take that file with you" in capsys.readouterr().out


def test_a_brain_on_another_local_disk_is_still_deleted(machine, monkeypatch):
    """STOCKSAGE_DB pointing at a second local disk is not 'shared' — it
    syncs nowhere, so leaving it behind abandons the trading history on a
    machine the owner no longer controls."""
    tmp_path, state, env, _ = machine
    monkeypatch.setattr("stocksage.brain.detect_cloud_folders", list)
    result = offboard.offboard(keep_brain_at=tmp_path / "saved.db")

    assert not (state / "stocksage.db").exists()
    assert result.brain_left_in_sync_folder is None


def test_only_a_brain_inside_a_sync_folder_counts_as_shared(machine, monkeypatch):
    tmp_path, state, _, _ = machine
    cloud = tmp_path / "Dropbox"
    cloud.mkdir()
    monkeypatch.setattr(
        "stocksage.brain.detect_cloud_folders", lambda home=None: [("Dropbox", cloud)]
    )
    # The brain is in .stocksage, NOT in Dropbox.
    assert offboard.brain_is_shared() is False

    monkeypatch.setenv("STOCKSAGE_DB", str(cloud / "StockSage" / "stocksage.db"))
    assert offboard.brain_is_shared() is True
