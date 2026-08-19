"""Security of the broker link: the access trail, and the exposure audit.

The question these tests protect is a real one the owner asked after two
Robinhood sign-in alerts: *"was that one at 8:30 my own app, or somebody
else?"* Answering it requires that every broker session this app opens is
recorded truthfully, that a reused token is never reported as a sign-in
(only a fresh sign-in triggers a Robinhood alert), and that no credential
ever leaks into the log that exists to answer the question.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from stocksage import security


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Redirect the access log into a temp dir; never touch the real one."""
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    return tmp_path


def _write_env(tmp_path, monkeypatch, body: str, mode: int = 0o600):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", path)
    return path


# --- the access trail -------------------------------------------------------


def test_events_round_trip_newest_first(state):
    security.record_access(security.SESSION_REUSED, "publish-drive")
    security.record_access(security.FRESH_LOGIN, "doctor")
    events = security.recent_access()
    assert [e["event"] for e in events] == [
        security.FRESH_LOGIN, security.SESSION_REUSED
    ]
    assert events[0]["trigger"] == "doctor"


def test_log_never_contains_a_credential(state, monkeypatch):
    """The log lives outside the brain precisely so it holds nothing secret."""
    monkeypatch.setenv("ROBINHOOD_USERNAME", "owner@example.com")
    monkeypatch.setenv("ROBINHOOD_PASSWORD", "hunter2-very-secret")
    monkeypatch.setenv("ROBINHOOD_MFA_SECRET", "JBSWY3DPEHPK3PXP")
    security.record_access(security.FRESH_LOGIN, "publish-drive", "detail line")
    body = security.access_log_path().read_text(encoding="utf-8")
    for secret in ("owner@example.com", "hunter2-very-secret", "JBSWY3DPEHPK3PXP"):
        assert secret not in body


def test_a_torn_line_does_not_lose_the_rest(state):
    security.record_access(security.FRESH_LOGIN, "daily")
    with security.access_log_path().open("a", encoding="utf-8") as fh:
        fh.write('{"at": "2026-08-19T08:3')  # interrupted write, no newline
    security.record_access(security.SESSION_REUSED, "publish-drive")
    events = security.recent_access()
    assert len(events) == 2


def test_log_is_trimmed_and_keeps_the_newest(state, monkeypatch):
    monkeypatch.setattr(security, "MAX_ACCESS_LINES", 10)
    for i in range(25):
        security.record_access(security.SESSION_REUSED, f"run-{i}")
    lines = security.access_log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 10
    assert json.loads(lines[-1])["trigger"] == "run-24"


def test_recording_never_raises_when_the_log_cannot_be_written(monkeypatch, tmp_path):
    """Bookkeeping must never be able to break a run."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("STOCKSAGE_STATE", str(blocked))
    security.record_access(security.FRESH_LOGIN, "daily")  # must not raise
    assert security.recent_access() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_access_log_is_private(state):
    security.record_access(security.FRESH_LOGIN, "daily")
    assert security.access_log_path().stat().st_mode & 0o077 == 0


# --- answering "was that sign-in me?" ---------------------------------------


def _at(minutes_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")


def test_a_fresh_login_near_the_alert_is_claimed(state):
    security.record_access(security.FRESH_LOGIN, "publish-drive")
    when = datetime.now().astimezone().isoformat()
    verdict = security.explain_signin(when)
    assert verdict["ours"] is True
    assert "publish-drive" in verdict["note"]


def test_a_reused_session_is_not_a_signin(state, monkeypatch):
    """Robinhood does not alert on a reused token, so a nearby reuse entry is
    evidence the alert came from somewhere else — the dangerous mistake here
    would be reassuring the owner about a stranger's login."""
    monkeypatch.setattr(
        security, "recent_access",
        lambda limit=20, since_hours=None: [
            {"at": _at(1), "event": security.SESSION_REUSED, "trigger": "publish-drive"}
        ],
    )
    verdict = security.explain_signin(datetime.now().astimezone().isoformat())
    assert verdict["ours"] is False
    assert "not StockSage" in verdict["note"]


def test_a_login_outside_the_window_is_not_claimed(state, monkeypatch):
    monkeypatch.setattr(
        security, "recent_access",
        lambda limit=20, since_hours=None: [
            {"at": _at(180), "event": security.FRESH_LOGIN, "trigger": "daily"}
        ],
    )
    assert security.explain_signin(datetime.now().astimezone().isoformat())["ours"] is False


def test_no_history_at_all_errs_toward_suspicion(state):
    verdict = security.explain_signin(datetime.now().astimezone().isoformat())
    assert verdict["ours"] is False
    assert "somebody else" in verdict["note"]


def test_unreadable_time_is_reported_not_guessed(state):
    verdict = security.explain_signin("sometime tuesday")
    assert verdict["ours"] is False
    assert "could not read" in verdict["note"]


# --- the standing exposure audit --------------------------------------------


def _level(findings, name):
    return next(f["level"] for f in findings if f["name"] == name)


def test_totp_seed_beside_the_password_is_critical(state, tmp_path, monkeypatch):
    _write_env(
        tmp_path, monkeypatch,
        "ROBINHOOD_USERNAME=a@b.c\nROBINHOOD_PASSWORD=pw\n"
        "ROBINHOOD_MFA_SECRET=JBSWY3DPEHPK3PXP\n",
    )
    findings = security.audit()
    assert _level(findings, "Two-factor") == security.CRITICAL
    assert findings[0]["level"] == security.CRITICAL, "worst finding must sort first"


def test_password_without_a_seed_is_not_flagged(state, tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "ROBINHOOD_USERNAME=a@b.c\nROBINHOOD_PASSWORD=pw\n")
    assert _level(security.audit(), "Two-factor") == security.OK


def test_an_empty_seed_setting_is_not_a_stored_seed(state, tmp_path, monkeypatch):
    """.env templates ship the key with no value; that is not an exposure."""
    _write_env(
        tmp_path, monkeypatch,
        "ROBINHOOD_PASSWORD=pw\nROBINHOOD_MFA_SECRET=\n",
    )
    assert _level(security.audit(), "Two-factor") == security.OK


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_world_readable_env_is_flagged(state, tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "ROBINHOOD_PASSWORD=pw\n", mode=0o644)
    assert _level(security.audit(), "Settings file") == security.RISK


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_world_readable_token_is_flagged(state, tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "ROBINHOOD_PASSWORD=pw\n")
    pickle = tmp_path / "robinhood.pickle"
    pickle.write_bytes(b"token")
    pickle.chmod(0o644)
    monkeypatch.setattr(security, "token_pickle_path", lambda: pickle)
    assert _level(security.audit(), "Session token") == security.RISK
    assert security.harden_file(pickle) is True
    assert _level(security.audit(), "Session token") == security.OK


def test_many_daily_signins_are_called_out_as_noise(state, tmp_path, monkeypatch):
    """Five sign-in alerts a day is how a real intrusion goes unnoticed."""
    _write_env(tmp_path, monkeypatch, "ROBINHOOD_PASSWORD=pw\n")
    for _ in range(35):
        security.record_access(security.FRESH_LOGIN, "publish-drive")
    assert _level(security.audit(), "Sign-in noise") == security.RISK


def test_audit_survives_a_missing_env_file(state, tmp_path, monkeypatch):
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", tmp_path / "nope.env")
    assert all(f["level"] == security.OK for f in security.audit())


# --- the client actually records what it did --------------------------------


class _FakeRH(types.ModuleType):
    """Stand-in for robin_stocks.robinhood — no network, no credentials used."""

    def __init__(self, result=None, raises=None):
        super().__init__("robin_stocks.robinhood")
        self.result, self.raises, self.calls = result, raises, []

    def login(self, username, password, mfa_code=None, expiresIn=None, store_session=None):
        self.calls.append({"expiresIn": expiresIn, "store_session": store_session})
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def fake_rh(monkeypatch):
    def install(result=None, raises=None):
        module = _FakeRH(result, raises)
        parent = types.ModuleType("robin_stocks")
        parent.robinhood = module
        monkeypatch.setitem(sys.modules, "robin_stocks", parent)
        monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", module)
        return module

    return install


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_USERNAME", "a@b.c")
    monkeypatch.setenv("ROBINHOOD_PASSWORD", "pw")
    monkeypatch.delenv("ROBINHOOD_MFA_SECRET", raising=False)


def test_a_reused_token_is_logged_as_a_reuse(state, creds, fake_rh, monkeypatch):
    from stocksage.robinhood import RobinhoodClient

    monkeypatch.setattr(security, "harden_file", lambda path: True)
    fake_rh({"detail": "logged in using authentication in robinhood.pickle"})
    assert RobinhoodClient().login(trigger="publish-drive") is True
    assert security.recent_access()[0]["event"] == security.SESSION_REUSED


def test_a_real_signin_is_logged_as_a_signin(state, creds, fake_rh, monkeypatch):
    from stocksage.robinhood import RobinhoodClient

    monkeypatch.setattr(security, "harden_file", lambda path: True)
    fake_rh({"access_token": "x", "token_type": "Bearer"})
    assert RobinhoodClient().login(trigger="daily") is True
    entry = security.recent_access()[0]
    assert entry["event"] == security.FRESH_LOGIN
    assert entry["trigger"] == "daily"


def test_a_failed_login_is_logged_without_the_reason_leaking_secrets(
    state, creds, fake_rh, monkeypatch
):
    from stocksage.robinhood import RobinhoodClient

    fake_rh(raises=RuntimeError("bad password pw for a@b.c"))
    assert RobinhoodClient().login(trigger="doctor") is False
    entry = security.recent_access()[0]
    assert entry["event"] == security.LOGIN_FAILED
    assert entry["detail"] == "RuntimeError"
    assert "pw" not in json.dumps(entry)


def test_the_session_is_asked_to_last_longer_than_a_day(state, creds, fake_rh, monkeypatch):
    """Every token expiry costs a fresh sign-in *and* a new device fingerprint."""
    from stocksage.robinhood import RobinhoodClient, SESSION_SECONDS

    monkeypatch.setattr(security, "harden_file", lambda path: True)
    rh = fake_rh({"detail": "logged in using authentication in robinhood.pickle"})
    RobinhoodClient().login()
    assert rh.calls[0]["expiresIn"] == SESSION_SECONDS > 86400
    assert rh.calls[0]["store_session"] is True


def test_the_second_login_in_a_process_opens_no_new_session(
    state, creds, fake_rh, monkeypatch
):
    from stocksage.robinhood import RobinhoodClient

    monkeypatch.setattr(security, "harden_file", lambda path: True)
    rh = fake_rh({"detail": "logged in using authentication in robinhood.pickle"})
    client = RobinhoodClient()
    client.login()
    client.login()
    assert len(rh.calls) == 1
    assert len(security.recent_access()) == 1


def test_missing_credentials_touch_neither_broker_nor_log(state, monkeypatch, fake_rh):
    from stocksage.robinhood import RobinhoodClient

    for key in ("ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    rh = fake_rh({"detail": "x"})
    assert RobinhoodClient().login() is False
    assert rh.calls == []
    assert security.recent_access() == []


def test_the_trigger_falls_back_to_the_command_name(state, creds, fake_rh, monkeypatch):
    from stocksage.robinhood import RobinhoodClient

    monkeypatch.setattr(security, "harden_file", lambda path: True)
    monkeypatch.setenv("STOCKSAGE_TRIGGER", "publish-drive")
    fake_rh({"access_token": "x"})
    RobinhoodClient().login()
    assert security.recent_access()[0]["trigger"] == "publish-drive"


# --- publishing without the broker ------------------------------------------


def test_no_broker_publishes_and_grades_without_a_session(tmp_path, monkeypatch):
    from stocksage.advisor import desktop_sync_cycle
    from stocksage.engine import Engine

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))

    class Tripwire:
        def sync_history(self, db):
            raise AssertionError("--no-broker must not open a Robinhood session")

        def portfolio(self):
            raise AssertionError("--no-broker must not open a Robinhood session")

    engine = Engine()
    monkeypatch.setattr(engine, "evaluate_pending", lambda: 3)
    stats = desktop_sync_cycle(engine, client=Tripwire(), broker=False)
    assert stats["broker_skipped"] is True
    assert stats["graded"] == 3
    engine.db.close()
