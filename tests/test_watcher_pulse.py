"""The backstop for a dark computer: the watcher writes a pulse every five minutes, and the
trader's hourly run (and the watchdog) say so within 20 minutes when it stops."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stocksage import journal, watchdog, watcher

UTC = timezone.utc
NOON = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)          # Thursday 12:00 PM ET
STAMP = "%Y-%m-%dT%H:%MZ"


def test_the_watcher_writes_its_pulse_every_five_minutes_in_stock_hours_only():
    state, wrote = {}, []
    publish = lambda name, text: wrote.append((name, text))  # noqa: E731
    assert watcher.pulse(state, NOON, publish)
    assert not watcher.pulse(state, NOON + timedelta(minutes=4), publish), "not yet"
    assert watcher.pulse(state, NOON + timedelta(minutes=5), publish)
    assert wrote == [(journal.PULSE_NAME, "2026-11-12T17:00Z"), (journal.PULSE_NAME, "2026-11-12T17:05Z")]
    assert not watcher.pulse({}, datetime(2026, 11, 14, 17, 0, tzinfo=UTC), publish), "Saturday"
    assert not watcher.pulse({}, datetime(2026, 11, 12, 3, 0, tzinfo=UTC), publish), "the night"


def test_a_pulse_that_cannot_reach_drive_never_stops_the_watcher_and_is_retried_soon():
    state = {}

    def offline(name, text):
        raise OSError("offline")

    assert not watcher.pulse(state, NOON, offline)
    wrote = []
    assert not watcher.pulse(state, NOON + timedelta(minutes=1), lambda n, t: wrote.append(t)) and not wrote
    assert watcher.pulse(state, NOON + timedelta(minutes=2), lambda n, t: wrote.append(t)) and wrote


def test_silent_means_twenty_minutes_in_the_session_and_never_set_up_is_not_an_alarm():
    old = NOON - timedelta(minutes=21)
    assert journal.watcher_silent(old.strftime(STAMP), NOON) == old
    assert journal.watcher_silent((NOON - timedelta(minutes=20)).strftime(STAMP), NOON) is None
    assert journal.watcher_silent(None, NOON) is None and journal.watcher_silent("", NOON) is None
    assert journal.watcher_silent("not a time", NOON) is None
    for quiet_time in (datetime(2026, 11, 12, 14, 40, tzinfo=UTC),      # 9:40 AM ET: just opened
                       datetime(2026, 11, 12, 21, 30, tzinfo=UTC),      # after the close
                       datetime(2026, 11, 14, 17, 0, tzinfo=UTC)):      # Saturday
        assert journal.watcher_silent("2026-11-11T15:00Z", quiet_time) is None


def test_the_trader_is_told_in_its_run_and_asked_to_tell_the_phone_once_a_day():
    files = {journal.PULSE_NAME: (NOON - timedelta(minutes=38)).strftime(STAMP)}
    said = []
    assert journal.watcher_notice(said.append, NOON, files.get, files.__setitem__)
    assert said[0].startswith("WATCHER SILENT") and "Thu 11:22 AM ET (38 minutes ago)" in said[0]
    assert "watchdog.yml/dispatches" in said[1] and files[journal.PULSE_TOLD_NAME] == "2026-11-12"
    again = []
    assert journal.watcher_notice(again.append, NOON + timedelta(hours=1), files.get, files.__setitem__)
    assert again[0].startswith("WATCHER SILENT") and again[1] == "The owner's phone was already told today."
    tomorrow = []
    journal.watcher_notice(tomorrow.append, NOON + timedelta(days=1), files.get, files.__setitem__)
    assert "watchdog.yml/dispatches" in tomorrow[1], "a new day asks again"


def test_an_alive_watcher_or_an_unreachable_drive_says_nothing_alarming():
    alive = {journal.PULSE_NAME: (NOON - timedelta(minutes=3)).strftime(STAMP)}
    said = []
    assert not journal.watcher_notice(said.append, NOON, alive.get, alive.__setitem__) and said == []
    assert not journal.watcher_notice(said.append, NOON, {}.get, None) and said == [], "never set up"

    def broken(name):
        raise OSError("offline")

    assert not journal.watcher_notice(said.append, NOON, broken, None)
    assert said == ["(Watcher not checked: OSError)"]

    def cannot_write(name, text):
        raise OSError("offline")

    files = {journal.PULSE_NAME: "2026-11-12T15:00Z"}
    assert journal.watcher_notice(said.append, NOON, files.get, cannot_write), "still told, asked again next run"


def test_the_note_command_carries_the_notice_and_the_start_of_run_check_never_fails(monkeypatch):
    said = []
    monkeypatch.setattr(journal, "append_notes", lambda lines: (list(lines), [], "text"))
    code = journal.note_command(["RUN | 2026-11-12T17:00Z | 100 | 10 | v39: held"], out=said.append,
                                notice=lambda out: out("WATCHER SILENT: test"))
    assert code == 0 and said[-1] == "WATCHER SILENT: test"
    monkeypatch.setattr(journal, "watcher_notice", lambda *a, **k: False)
    assert journal.main(["watcher"]) == 0


def test_the_watchdog_tells_the_phone_after_twenty_minutes_when_a_pulse_exists():
    tags = "WATCH | 2026-11-12T16:00Z | watching AMD | 0 wake-up(s) today"      # an hour ago: fine by the old rule
    assert watchdog.watcher_verdict(tags, NOON) is None
    msg = watchdog.watcher_verdict(tags, NOON, pulse="2026-11-12T16:30Z")
    assert msg and "Thu 11:30 AM ET" in msg and ".\\start.bat watch-trader install" in msg
    assert watchdog.watcher_verdict(tags, NOON, pulse="2026-11-12T16:50Z") is None
    sent = []
    watchdog.check_trader(post=lambda u, d, h: sent.append(h["Title"]), now=NOON, pulse="2026-11-12T16:30Z",
                          tags_text=tags + "\nRUN | 2026-11-12T16:40Z | 100 | 10 | v39: held")
    assert sent in ([], ["StockSage watcher has gone quiet"])     # no topic in tests: nothing is posted
