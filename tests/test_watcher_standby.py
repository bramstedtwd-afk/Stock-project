"""The standby watcher on a second computer (owner's choice 2026-11-23): silent while the
main watcher's pulse is fresh, watching in its place after 10 quiet minutes in stock hours."""
from datetime import UTC, datetime, timedelta

import pytest

from stocksage import envfile, watcher

MON = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)          # 10:00 AM ET, a Monday


def _reader(text):
    return lambda name: text


def test_the_standby_stays_silent_while_the_main_pulse_is_fresh():
    state: dict = {}
    assert not watcher.primary_silent(state, MON, reader=_reader("2026-11-23T14:56Z"))
    assert not watcher.primary_silent({}, MON, reader=_reader("2026-11-23T14:51Z")), "9 minutes: not yet"
    assert watcher.primary_silent({}, MON, reader=_reader("2026-11-23T14:49Z")), "11 minutes: take over"


def test_the_morning_gives_the_first_pulse_time_and_nights_are_never_judged():
    yesterday = _reader("2026-11-20T00:59Z")
    assert not watcher.primary_silent({}, datetime(2026, 11, 23, 12, 5, tzinfo=UTC), reader=yesterday)
    assert watcher.primary_silent({}, datetime(2026, 11, 23, 12, 11, tzinfo=UTC), reader=yesterday)
    assert not watcher.primary_silent({}, datetime(2026, 11, 23, 2, 0, tzinfo=UTC), reader=yesterday)
    assert not watcher.primary_silent({}, datetime(2026, 11, 22, 15, 0, tzinfo=UTC), reader=yesterday)


def test_an_unreadable_drive_keeps_the_last_answer():
    def broken(name):
        raise OSError("no network")

    assert not watcher.primary_silent({}, MON, reader=broken), "no proof the main computer is dark"
    assert watcher.primary_silent({"standing_in": MON.isoformat()}, MON, reader=broken)


def test_taking_over_and_handing_back_are_each_said_once(monkeypatch):
    monkeypatch.setattr("stocksage.notify.topic_from_env", lambda: "topic")
    pushed: list = []
    push = lambda url, body, headers: pushed.append(headers["Title"])
    state: dict = {}
    silent = _reader("2026-11-23T14:40Z")
    assert watcher.standby_gate(state, MON, reader=silent, push=push)
    assert watcher.standby_gate(state, MON + timedelta(minutes=1), reader=silent, push=push)
    assert pushed == ["StockSage: the standby watcher took over"]
    assert not watcher.standby_gate(state, MON + timedelta(minutes=2), reader=_reader("2026-11-23T15:01Z"), push=push)
    assert "standing_in" not in state
    watcher.standby_gate(state, MON + timedelta(minutes=30), reader=silent, push=push)
    assert len(pushed) == 1, "one phone push a day"


def test_the_standby_is_never_set_up_where_the_broker_login_lives(monkeypatch, capsys):
    monkeypatch.setattr(envfile, "load_env", lambda *a, **k: {})
    saved: list = []
    monkeypatch.setattr(envfile, "save_env", lambda values, path=None: saved.append(values))
    monkeypatch.setenv("ROBINHOOD_USERNAME", "someone")
    assert watcher.make_standby(run=lambda *a, **k: None) == 1 and not saved
    assert "MAIN watcher" in capsys.readouterr().out


def test_the_standby_starts_at_boot_on_a_linux_server(monkeypatch):
    monkeypatch.setattr(envfile, "load_env", lambda *a, **k: {})
    saved: list = []
    monkeypatch.setattr(envfile, "save_env", lambda values, path=None: saved.append(values))
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    monkeypatch.setenv(watcher.ROLE_VAR, "")             # restored after the test: make_standby sets it
    for var in (watcher.URL_VAR, watcher.TOKEN_VAR, "FINNHUB_API_KEY", "STOCKSAGE_FINNHUB_KEY"):
        monkeypatch.setenv(var, "x")
    from stocksage import finnhub

    monkeypatch.setenv(finnhub.KEY_VAR, "x")
    monkeypatch.setattr(watcher.sys, "platform", "linux")
    calls: list = []

    class Done:
        returncode, stdout = 1, ""

    def run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        return Done()

    assert watcher.make_standby(run=run) == 0
    assert saved == [{watcher.ROLE_VAR: "standby"}]
    crontab = [inp for cmd, inp in calls if cmd == ["crontab", "-"]]
    assert crontab and "@reboot" in crontab[0] and "watch-trader serve" in crontab[0]
    assert watcher.is_standby()


@pytest.mark.parametrize("role,expected", [("standby", True), ("", False), ("main", False)])
def test_only_the_word_standby_makes_a_standby(monkeypatch, role, expected):
    monkeypatch.setenv(watcher.ROLE_VAR, role)
    assert watcher.is_standby() is expected
