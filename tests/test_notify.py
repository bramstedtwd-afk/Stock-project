"""Phone alerts: urgent now, everything else once a day, nothing private.

The clock is injected, so none of these depend on the hour the suite runs.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from stocksage import notify
from stocksage.actions import Action, Plan
from stocksage.db import Database

URGENCY = {"EXIT_STOP": 1, "EXIT_TARGET": 2, "EXIT_TIME": 2, "TRIM": 3, "ENTER": 3}
MORNING = datetime(2026, 10, 6, 6, 30)     # before the 8:00 digest hour
LATER = datetime(2026, 10, 6, 10, 15)      # after it
NEXT_DAY = LATER + timedelta(days=1)


def act(kind, ticker, confidence=None, dollars=123.45):
    return Action(kind, ticker, "SELL" if kind != "ENTER" else "BUY", URGENCY.get(kind, 4),
                  "headline", "why", dollars=dollars, confidence=confidence)


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, url, data, headers):
        if self.fail:
            raise OSError("network down")
        self.sent.append((url, data.decode(), headers))


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture(autouse=True)
def default_hour(monkeypatch):
    monkeypatch.delenv("STOCKSAGE_DIGEST_HOUR", raising=False)


def plans(*items, role="Agentic"):
    return [(role, Plan(actions=list(items)))]


# ------------------------------------------------------------- urgent: now

def test_a_stop_breach_buzzes_immediately_even_before_the_digest_hour(db):
    rec = Recorder()
    assert notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), "t", rec, MORNING)
    url, body, headers = rec.sent[0]
    assert url.endswith("/t") and "Agentic: SELL AAA" in body
    assert headers["Priority"] == "high" and "URGENT" in headers["Title"]


def test_the_same_breach_does_not_buzz_again_on_every_run(db):
    """Publishing runs five times a day: one URGENT push, then silence until
    the daily digest, then silence again."""
    rec, p = Recorder(), plans(act("EXIT_STOP", "AAA"))
    notify.alert_accounts(db, p, "t", rec, MORNING)
    for minutes in (30, 60, 80):                       # all still before 8:00
        assert notify.alert_accounts(db, p, "t", rec, MORNING + timedelta(minutes=minutes)) is False
    assert [s[2]["Title"].split(":")[0] for s in rec.sent] == ["StockSage URGENT"]
    notify.alert_accounts(db, p, "t", rec, LATER)      # the day's digest
    assert notify.alert_accounts(db, p, "t", rec, LATER + timedelta(hours=3)) is False
    assert len(rec.sent) == 2


def test_a_digest_that_repeats_a_known_breach_does_not_shout(db):
    rec, p = Recorder(), plans(act("EXIT_STOP", "AAA"))
    notify.alert_accounts(db, p, "t", rec, MORNING)
    notify.alert_accounts(db, p, "t", rec, LATER)
    digest = rec.sent[1][2]
    assert "today" in digest["Title"] and digest["Priority"] == "default"


def test_a_second_breach_is_announced(db):
    rec = Recorder()
    notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), "t", rec, MORNING)
    assert notify.alert_accounts(
        db, plans(act("EXIT_STOP", "AAA"), act("EXIT_STOP", "BBB")), "t", rec, MORNING)
    assert len(rec.sent) == 2


def test_a_recurrence_after_the_all_clear_is_heard_again(db):
    rec, p = Recorder(), plans(act("EXIT_STOP", "AAA"))
    notify.alert_accounts(db, p, "t", rec, MORNING)
    notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, MORNING)
    assert notify.alert_accounts(db, p, "t", rec, MORNING) is True


# ------------------------------------------------------------- everything else: daily

def test_a_trim_waits_for_the_daily_digest(db):
    rec, p = Recorder(), plans(act("TRIM", "BBB"))
    assert notify.alert_accounts(db, p, "t", rec, MORNING) is False, "not urgent, not yet 9am"
    assert notify.alert_accounts(db, p, "t", rec, LATER) is True
    assert "today" in rec.sent[0][2]["Title"] and rec.sent[0][2]["Priority"] == "default"


def test_the_digest_is_sent_once_a_day_not_once_a_run(db):
    rec, p = Recorder(), plans(act("TRIM", "BBB"))
    notify.alert_accounts(db, p, "t", rec, LATER)
    for minutes in (30, 90, 200):
        assert notify.alert_accounts(db, p, "t", rec, LATER + timedelta(minutes=minutes)) is False
    assert len(rec.sent) == 1
    assert notify.alert_accounts(db, p, "t", rec, NEXT_DAY) is True, "a new day, a new digest"


def test_a_quiet_day_sends_one_all_clear_not_silence(db):
    """Silence is indistinguishable from a tool that has stopped working."""
    rec = Recorder()
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, MORNING) is False, "not before the hour"
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, LATER) is True
    assert "all clear" in rec.sent[0][2]["Title"] and rec.sent[0][2]["Priority"] == "low"
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, LATER + timedelta(hours=3)) is False
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, NEXT_DAY) is True


def test_the_all_clear_can_be_switched_off_and_never_names_anything(db, monkeypatch):
    rec = Recorder()
    notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, LATER)
    assert not re.search(r"[A-Z]{2,5}:|\$|\d{3}", rec.sent[0][1])
    monkeypatch.setenv("STOCKSAGE_ALL_CLEAR", "off")
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, NEXT_DAY) is False


def test_with_no_accounts_read_there_is_no_all_clear(db):
    """An all-clear that rests on reading nothing would be a lie."""
    assert notify.alert_accounts(db, [], "t", Recorder(), LATER) is False


def test_a_quiet_day_sends_nothing_without_a_topic(db, monkeypatch):
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    rec = Recorder()
    assert notify.alert_accounts(db, [("Agentic", Plan())], None, rec, LATER) is False
    assert not rec.sent


def test_the_digest_hour_is_configurable_and_garbage_falls_back(db, monkeypatch):
    rec, p = Recorder(), plans(act("TRIM", "BBB"))
    monkeypatch.setenv("STOCKSAGE_DIGEST_HOUR", "14")
    assert notify.alert_accounts(db, p, "t", rec, LATER) is False
    assert notify.alert_accounts(db, p, "t", rec, LATER.replace(hour=14)) is True
    for bad in ("", "noon", "25", "-1"):
        monkeypatch.setenv("STOCKSAGE_DIGEST_HOUR", bad)
        assert notify.digest_hour() == 8


def test_a_breach_and_a_due_digest_are_one_message_not_two(db):
    rec = Recorder()
    both = plans(act("EXIT_STOP", "AAA"), act("TRIM", "BBB"))
    assert notify.alert_accounts(db, both, "t", rec, LATER) is True
    assert len(rec.sent) == 1
    body = rec.sent[0][1]
    assert "SELL AAA" in body and "TRIM BBB" in body
    assert rec.sent[0][2]["Priority"] == "high", "a NEW breach is still loud"
    # ...and that message counted as today's digest too.
    assert notify.alert_accounts(db, both, "t", rec, LATER + timedelta(hours=2)) is False


def test_urgent_alone_does_not_use_up_the_days_digest(db):
    rec = Recorder()
    notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), "t", rec, MORNING)
    notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA"), act("TRIM", "BBB")), "t", rec, LATER)
    assert len(rec.sent) == 2 and "TRIM BBB" in rec.sent[1][1]


# ------------------------------------------------------------- what qualifies

def test_low_confidence_ideas_and_watch_lines_never_interrupt(db):
    rec = Recorder()
    p = plans(act("ENTER", "AAA", confidence="low"), act("WATCH", "BBB"),
              act("HEADS_UP", "CCC"), act("EXIT_SIGNAL", "DDD"), act("IDEA", "EEE"))
    notify.alert_accounts(db, p, "t", rec, LATER)
    assert len(rec.sent) == 1 and "all clear" in rec.sent[0][2]["Title"], "none of those may interrupt"


def test_a_medium_confidence_entry_waits_for_the_digest(db):
    rec, p = Recorder(), plans(act("ENTER", "AAA", "medium"))
    assert notify.alert_accounts(db, p, "t", rec, MORNING) is False
    assert notify.alert_accounts(db, p, "t", rec, LATER) is True


# ------------------------------------------------------------- reliability

def test_a_failed_send_is_retried_next_run_not_swallowed(db):
    p = plans(act("EXIT_STOP", "AAA"))
    assert notify.alert_accounts(db, p, "t", Recorder(fail=True), MORNING) is False
    good = Recorder()
    assert notify.alert_accounts(db, p, "t", good, MORNING) is True, \
        "the failure must not be remembered as sent"


def test_a_failed_digest_is_retried_the_same_day(db):
    p = plans(act("TRIM", "BBB"))
    notify.alert_accounts(db, p, "t", Recorder(fail=True), LATER)
    good = Recorder()
    assert notify.alert_accounts(db, p, "t", good, LATER + timedelta(hours=1)) is True


def test_a_dead_network_never_raises_into_a_publish(db):
    notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), "t", Recorder(fail=True), MORNING)


def test_no_topic_means_no_alerts_at_all(db, monkeypatch):
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    rec = Recorder()
    assert notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), None, rec, MORNING) is False
    assert not rec.sent


def test_the_topic_is_read_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "  from-env  ")
    rec = Recorder()
    notify.alert_accounts(db, plans(act("EXIT_STOP", "AAA")), None, rec, MORNING)
    assert rec.sent[0][0].endswith("/from-env")


# ------------------------------------------------------------- privacy & tap target

def test_a_push_never_carries_money_or_an_account_number(db):
    """ntfy topics are readable by anyone who learns the name."""
    rec = Recorder()
    p = [("Agentic", Plan(actions=[act("EXIT_STOP", "AAA", dollars=98765.43)])),
         ("Roth IRA", Plan(actions=[act("TRIM", "BBB", dollars=4321.0)]))]
    notify.alert_accounts(db, p, "t", rec, LATER)
    _, body, headers = rec.sent[0]
    text = body + " ".join(headers.values())
    assert not re.search(r"\d{3,}", text), text
    assert "$" not in text
    assert "Agentic: SELL AAA" in body and "Roth IRA: TRIM BBB" in body
    assert "Nothing was ordered" in body


def test_tapping_the_alert_opens_the_most_urgent_ticker_in_robinhood(db):
    rec = Recorder()
    p = [("Roth IRA", Plan(actions=[act("TRIM", "SLOW")])),
         ("Agentic", Plan(actions=[act("EXIT_STOP", "FAST")]))]
    notify.alert_accounts(db, p, "t", rec, LATER)
    assert rec.sent[0][2]["Click"] == "https://robinhood.com/us/en/stocks/FAST/"


def test_the_same_ticker_in_two_accounts_is_two_different_alerts(db):
    rec = Recorder()
    notify.alert_accounts(db, plans(act("EXIT_STOP", "XXX"), role="Agentic"), "t", rec, MORNING)
    assert notify.alert_accounts(
        db, plans(act("EXIT_STOP", "XXX"), role="Personal"), "t", rec, MORNING) is True


def test_the_single_account_wrapper_still_works(db):
    rec = Recorder()
    assert notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), "t", rec, MORNING)


# --- wired into the scheduled publish ---------------------------------------

def _publish_env(monkeypatch, tmp_path, topic="t"):
    from stocksage import cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    if topic:
        monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", topic)
    else:
        monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(cli, "_pull_drive_brain", lambda e: None)
    monkeypatch.setattr("stocksage.advisor.publish_brief_via_api",
                        lambda *a, **k: {"candidates": 1, "files": ["x"], "actions": None})
    monkeypatch.setattr("stocksage.robinhood.RobinhoodClient", lambda: object())
    # Nothing here may reach the network: no look-alike rebuild, no Drive upload.
    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)

    def offline(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("stocksage.backtest.collect_samples", offline)
    monkeypatch.setattr("stocksage.drive_api.publish_text", lambda name, text: "id")
    return cli


def _one_account(plan):
    return {"accounts": [{"role": "agentic", "title": "Agentic", "label": "x",
                          "manual": False, "plan": plan}],
            "overall_notes": [], "suggestions": {}}


def test_the_scheduled_publish_plans_every_account_and_alerts(monkeypatch, tmp_path):
    cli = _publish_env(monkeypatch, tmp_path)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "positions": [{}], "buying_power": 1.0})
    seen = {}
    monkeypatch.setattr("stocksage.advisor.plans_for_accounts",
                        lambda *a, **k: _one_account(Plan(actions=[act("EXIT_STOP", "AAA")])))
    monkeypatch.setattr("stocksage.notify.alert_accounts",
                        lambda db, plans, *a, **k: seen.setdefault("plans", plans))
    assert cli.main(["publish-drive"]) == 0
    assert [(r, [x.ticker for x in p.actions]) for r, p in seen["plans"]] == [("Agentic", ["AAA"])]


def test_with_no_topic_the_sheet_is_still_built_but_nothing_is_pushed(monkeypatch, tmp_path):
    cli = _publish_env(monkeypatch, tmp_path, topic=None)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "positions": [{}], "buying_power": 1.0})
    monkeypatch.setattr("stocksage.advisor.plans_for_accounts",
                        lambda *a, **k: _one_account(Plan(actions=[act("EXIT_STOP", "AAA")])))

    def boom(*a, **k):
        raise AssertionError("pushed although alerts are off")

    monkeypatch.setattr("stocksage.notify.alert_accounts", boom)
    assert cli.main(["publish-drive"]) == 0
    from stocksage import today
    assert "SELL ALL AAA" in (today.state_dir() / "today.txt").read_text()


def test_a_no_broker_publish_never_alerts(monkeypatch, tmp_path):
    cli = _publish_env(monkeypatch, tmp_path)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "broker_skipped": True})

    def boom(*a, **k):
        raise AssertionError("alerted from a run that never opened the account")

    monkeypatch.setattr("stocksage.advisor.plans_for_accounts", boom)
    assert cli.main(["publish-drive", "--no-broker"]) == 0


def test_an_alerting_failure_can_never_fail_a_publish(monkeypatch, tmp_path):
    cli = _publish_env(monkeypatch, tmp_path)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "positions": [{}], "buying_power": 1.0})

    def boom(*a, **k):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr("stocksage.advisor.plans_for_accounts", boom)
    assert cli.main(["publish-drive"]) == 0
