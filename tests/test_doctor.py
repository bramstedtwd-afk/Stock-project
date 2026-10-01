from stocksage import doctor


def test_python_and_dependency_checks_pass_here():
    assert doctor.check_python()["status"] == doctor.PASS
    # Deterministic regardless of which interpreter runs the suite:
    assert doctor.check_dependencies(("pandas", "numpy"))["status"] == doctor.PASS
    result = doctor.check_dependencies(("pandas", "not_a_real_module"))
    assert result["status"] == doctor.FAIL
    assert "not_a_real_module" in result["detail"]


def test_brain_check_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    result = doctor.check_brain()
    assert result["status"] == doctor.WARN  # healthy but not bootstrapped
    from stocksage.db import Database

    db = Database()
    db.set_meta("bootstrap_done", "2026-07-09")
    db.close()
    assert doctor.check_brain()["status"] == doctor.PASS


def test_robinhood_check_unlinked(monkeypatch):
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    monkeypatch.delenv("ROBINHOOD_PASSWORD", raising=False)
    result = doctor.check_robinhood()
    assert result["status"] == doctor.WARN
    assert "optional" in result["detail"]


def test_update_channel_detects_git_checkout():
    assert doctor.check_update_channel()["status"] == doctor.PASS


def test_run_doctor_survives_crashing_check(monkeypatch, capsys):
    def bomb():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "ALL_CHECKS", (doctor.check_python, bomb))
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert "kaboom" in out
    assert rc == 1  # crashed check reports as FAIL, not a traceback


# --- earnings calendar (silent-failure check) ---


def test_earnings_check_passes_when_a_date_resolves(monkeypatch):
    from stocksage import doctor
    from stocksage.data import MarketData

    monkeypatch.setattr(
        MarketData, "_fetch_earnings_date",
        lambda self, t, today=None: "2026-10-28" if t == "AAPL" else None,
    )
    result = doctor.check_earnings_calendar(("AAPL",))
    assert result["status"] == doctor.PASS
    assert "AAPL" in result["detail"]


def test_earnings_check_warns_and_names_the_real_error(monkeypatch):
    """The failure is silent in normal use — the brief just shows nulls — so
    doctor must surface the underlying exception, not a generic message."""
    from stocksage import doctor
    from stocksage.data import MarketData

    def boom(self, ticker, today=None):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(MarketData, "_fetch_earnings_date", boom)
    result = doctor.check_earnings_calendar(("AAPL", "MSFT"))
    assert result["status"] == doctor.WARN
    assert "429 Too Many Requests" in result["detail"]
    assert "blackout is switched off" in result["fix"]


def test_earnings_check_warns_when_every_lookup_is_simply_empty(monkeypatch):
    """No exception, just nothing found — still a warning, because that is
    exactly what the owner's brief showed."""
    from stocksage import doctor
    from stocksage.data import MarketData

    monkeypatch.setattr(
        MarketData, "_fetch_earnings_date", lambda self, t, today=None: None
    )
    result = doctor.check_earnings_calendar(("AAPL", "MSFT"))
    assert result["status"] == doctor.WARN


def test_earnings_check_is_registered_with_the_others():
    from stocksage import doctor

    assert doctor.check_earnings_calendar in doctor.ALL_CHECKS


def test_routine_account_warns_when_unset(monkeypatch):
    """An unset account is a silent blocker: publish succeeds, the playbook
    looks fine, and the routine refuses to trade the next morning."""
    from stocksage.doctor import WARN, check_routine_account

    monkeypatch.delenv("STOCKSAGE_AGENTIC_ACCOUNT", raising=False)
    result = check_routine_account()
    assert result["status"] == WARN
    assert "STOCKSAGE_AGENTIC_ACCOUNT" in result["detail"]
    assert ".env" in result["fix"]


def test_routine_account_passes_and_shows_only_the_last_four(monkeypatch):
    """Doctor output gets pasted into chats and screenshots — it must not
    print the whole account number back out."""
    from stocksage.doctor import PASS, check_routine_account

    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "555000111")
    result = check_routine_account()
    assert result["status"] == PASS
    assert "0111" in result["detail"]
    assert "555000111" not in result["detail"]


# --- publishing: scheduled is not the same as working -----------------------


def _brain_with(tmp_path, monkeypatch, ok=None, err=None):
    from stocksage.db import Database

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("STOCKSAGE_DRIVE_TOKEN", "x")
    db = Database(tmp_path / "b.db")
    if ok:
        db.set_meta("last_publish_ok", ok)
    if err:
        db.set_meta("last_publish_error", err)
    db.close()


def _at(y, m, d, h=16):
    from datetime import datetime, timezone

    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


def test_weekends_do_not_count_as_missed_publishes():
    from stocksage.doctor import business_days_since

    friday, monday = _at(2026, 9, 25), _at(2026, 9, 28)
    assert business_days_since(friday, monday) == 1
    assert business_days_since(_at(2026, 9, 28), _at(2026, 9, 30)) == 2


def test_a_recent_publish_is_healthy(tmp_path, monkeypatch):
    from stocksage.doctor import PASS, check_publish_freshness

    _brain_with(tmp_path, monkeypatch, ok="2026-09-29T19:30:00+00:00")
    assert check_publish_freshness(now=_at(2026, 9, 30))["status"] == PASS


def test_two_missed_weekdays_warns_and_names_the_fix(tmp_path, monkeypatch):
    """The exact outage that happened: last publish Monday 9/28, nothing
    Tuesday or Wednesday, and only the routine noticed."""
    from stocksage.doctor import WARN, check_publish_freshness

    _brain_with(tmp_path, monkeypatch, ok="2026-09-28T19:30:00+00:00")
    result = check_publish_freshness(now=_at(2026, 9, 30, 18))
    assert result["status"] == WARN
    assert "stale brief" in result["detail"]
    assert "asleep or signed out" in result["fix"]


def test_the_command_shown_is_one_the_owner_can_paste(tmp_path, monkeypatch):
    """Rendered, not read from source — an over-escaped backslash passes a
    source scan and still fails on the owner's keyboard."""
    from stocksage.doctor import check_publish_freshness

    _brain_with(tmp_path, monkeypatch, ok="2026-09-28T19:30:00+00:00")
    fix = check_publish_freshness(now=_at(2026, 9, 30, 18))["fix"]
    assert ".\\start.bat publish-drive" in fix
    assert ".\\\\start.bat" not in fix


def test_the_failure_reason_is_surfaced_when_there_is_one(tmp_path, monkeypatch):
    from stocksage.doctor import check_publish_freshness

    _brain_with(
        tmp_path, monkeypatch, ok="2026-09-28T19:30:00+00:00",
        err="2026-09-29T14:00:00+00:00|Google sign-in expired",
    )
    detail = check_publish_freshness(now=_at(2026, 9, 30, 18))["detail"]
    assert "Google sign-in expired" in detail


def test_an_old_error_does_not_blame_a_later_success(tmp_path, monkeypatch):
    from stocksage.doctor import PASS, check_publish_freshness

    _brain_with(
        tmp_path, monkeypatch, ok="2026-09-30T14:00:00+00:00",
        err="2026-09-26T14:00:00+00:00|old failure",
    )
    result = check_publish_freshness(now=_at(2026, 9, 30, 18))
    assert result["status"] == PASS and "old failure" not in result["detail"]


def test_never_published_warns(tmp_path, monkeypatch):
    from stocksage.doctor import WARN, check_publish_freshness

    _brain_with(tmp_path, monkeypatch)
    assert check_publish_freshness()["status"] == WARN


def test_no_drive_setup_means_nothing_to_check(tmp_path, monkeypatch):
    from stocksage.doctor import PASS, check_publish_freshness

    monkeypatch.delenv("STOCKSAGE_DRIVE_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    assert check_publish_freshness()["status"] == PASS


def test_publish_records_success_and_failure_in_the_brain(tmp_path, monkeypatch):
    from stocksage import cli
    from stocksage.db import Database

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))

    class E:
        db = Database(tmp_path / "b.db")

    cli._record_publish(E)
    cli._record_publish(E, error="token expired")
    assert E.db.get_meta("last_publish_ok")
    assert E.db.get_meta("last_publish_error").endswith("|token expired")


def test_recording_a_publish_can_never_break_one():
    from stocksage import cli

    class Broken:
        class db:
            @staticmethod
            def set_meta(*a):
                raise RuntimeError("disk full")

    cli._record_publish(Broken)  # must not raise
