"""Phone alerts: heard once per change, and nothing private in them."""

from __future__ import annotations

import re

import pytest

from stocksage import notify
from stocksage.actions import Action, Plan
from stocksage.db import Database


def act(kind, ticker, confidence=None, dollars=123.45):
    return Action(kind, ticker, "SELL" if kind != "ENTER" else "BUY", 1,
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


def test_a_new_stop_breach_sends_one_high_priority_push(db):
    rec = Recorder()
    assert notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), "t-abc", rec)
    url, body, headers = rec.sent[0]
    assert url.endswith("/t-abc") and "SELL AAA" in body
    assert headers["Priority"] == "high"


def test_the_same_situation_does_not_buzz_again_on_every_run(db):
    """Publishing runs five times a day."""
    rec, plan = Recorder(), Plan(actions=[act("EXIT_STOP", "AAA")])
    assert notify.alert_if_new(db, plan, "t", rec) is True
    for _ in range(4):
        assert notify.alert_if_new(db, plan, "t", rec) is False
    assert len(rec.sent) == 1


def test_a_changed_situation_is_announced(db):
    rec = Recorder()
    notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), "t", rec)
    assert notify.alert_if_new(
        db, Plan(actions=[act("EXIT_STOP", "AAA"), act("TRIM", "BBB")]), "t", rec)
    assert len(rec.sent) == 2


def test_a_recurrence_after_the_all_clear_is_heard_again(db):
    rec, plan = Recorder(), Plan(actions=[act("EXIT_STOP", "AAA")])
    notify.alert_if_new(db, plan, "t", rec)
    notify.alert_if_new(db, Plan(), "t", rec)          # all clear
    assert notify.alert_if_new(db, plan, "t", rec) is True


def test_a_failed_send_is_retried_next_run_not_swallowed_forever(db):
    plan = Plan(actions=[act("EXIT_STOP", "AAA")])
    assert notify.alert_if_new(db, plan, "t", Recorder(fail=True)) is False
    good = Recorder()
    assert notify.alert_if_new(db, plan, "t", good) is True, "the failure must not be remembered as sent"


def test_a_dead_network_never_raises_into_a_publish(db):
    notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), "t", Recorder(fail=True))


def test_low_confidence_ideas_and_watch_lines_do_not_interrupt(db):
    rec = Recorder()
    plan = Plan(actions=[act("ENTER", "AAA", confidence="low"), act("WATCH", "BBB"),
                         act("HEADS_UP", "CCC"), act("EXIT_SIGNAL", "DDD")])
    assert notify.alert_if_new(db, plan, "t", rec) is False and not rec.sent


def test_a_medium_confidence_entry_does_qualify(db):
    rec = Recorder()
    assert notify.alert_if_new(db, Plan(actions=[act("ENTER", "AAA", "medium")]), "t", rec)


def test_no_topic_means_no_alerts_at_all(db, monkeypatch):
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    rec = Recorder()
    assert notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), None, rec) is False
    assert not rec.sent


def test_the_topic_is_read_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "  from-env  ")
    rec = Recorder()
    notify.alert_if_new(db, Plan(actions=[act("EXIT_STOP", "AAA")]), None, rec)
    assert rec.sent[0][0].endswith("/from-env")


def test_a_push_never_carries_money_or_an_account(db):
    """ntfy topics are readable by anyone who learns the name."""
    rec = Recorder()
    items = [act("EXIT_STOP", "AAA", dollars=98765.43), act("ENTER", "BBB", "high", dollars=4321.0)]
    notify.alert_if_new(db, Plan(actions=items), "t", rec)
    _, body, headers = rec.sent[0]
    text = body + " ".join(headers.values())
    assert not re.search(r"\d{3,}", text), text
    assert "$" not in text
    assert "Nothing was ordered" in body


def test_the_signature_ignores_order(db):
    a, b = act("EXIT_STOP", "AAA"), act("TRIM", "BBB")
    assert notify.signature([a, b]) == notify.signature([b, a])
