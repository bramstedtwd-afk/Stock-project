"""The watcher's weekly digest: what it woke the trader for, what came of it and what it
tuned, in counts only. In the phone report always; one push a week after Friday's close."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from stocksage import journal, notify
from stocksage.db import Database

NOW = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)          # Friday 5:00 PM ET, after the close
TOPIC = "stocksage-test-topic"


def notes(days_ago: float, kind: str = "MOVE", ticker: str = "PLD", acted: bool = False, kept: bool = True) -> str:
    at = NOW - timedelta(days=days_ago)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%MZ")  # noqa: E731
    lines = [f"WAKE | {stamp(at)} | {kind}: {ticker} -3.4% today, now 121.00",
             f"RUN | {stamp(at + timedelta(minutes=8))} | 400.65 | 22.00 | v38: wake-up handled",
             f"AFTER | {stamp(at)} | 61 | {ticker} 121 {118 if kept else 123}"]
    if acted:
        lines.insert(1, f"STOP | {stamp(at + timedelta(minutes=5))} | {ticker} | 119 | 131 | tightened")
    return "\n".join(lines)


def week_text() -> str:
    return "\n".join([notes(1, acted=True), notes(2, "NEAR", "AMD", kept=False), notes(3, "NEWS", "LMT"),
                      notes(9), notes(10), notes(12, "NEWS", "ORCL")])


def test_the_week_counts_wake_ups_answers_and_actions_without_naming_anything():
    card = journal.wake_scorecard(week_text(), now=NOW)
    week = card["week"]
    assert week["wakes"] == 3 and week["prev_wakes"] == 3 and week["answered"] == 3 and week["acted"] == 1
    assert week["by_kind"]["MOVE"] == {"wakes": 1, "acted": 1, "priced": 1, "continued": 1}
    assert week["by_kind"]["NEAR"]["continued"] == 0 and week["changes"] == []
    text = "\n".join(journal.wake_digest(card))
    assert "Wake-ups in the last 7 days: 3 (the 7 days before: 3)" in text
    assert "MOVE 1 (acted on 1, price kept going 1 of 1)" in text and "Nothing was tuned this week" in text
    assert "STOP and LIMIT are never tuned" in text
    for private in ("PLD", "AMD", "LMT", "ORCL", "400.65", "22.00", "121", "$"):
        assert private not in text, f"{private}: counts and settings only"
    assert all("when" not in r for r in card["recent"]), "the memory stays plain JSON"


def test_a_setting_the_watcher_changed_this_week_is_said():
    # Ten price-checked MOVE wake-ups this week that all reversed and were ignored: MOVE gets harder to set off.
    noisy = "\n".join(notes(0.5 + i * 0.5, ticker=f"Q{chr(65 + i)}", kept=False).replace("121 123", "121 124")
                      for i in range(10))
    card = journal.wake_scorecard(noisy.replace("-3.4%", "-3.4%"), now=NOW)
    assert card["tuning"]["MOVE"] == 1.5 and card["week"]["changes"] == ["the MOVE trigger: x1.00 to x1.50"]
    text = "\n".join(journal.wake_digest(card))
    assert "Tuned this week: the MOVE trigger: x1.00 to x1.50. Now: MOVE trigger x1.50" in text
    assert "MOVE mostly noise" in text
    lo, hi, _ = journal.WAKE_TUNE_BOUNDS["MOVE"]
    assert lo <= card["tuning"]["MOVE"] <= hi and "STOP" not in card["tuning"] and "LIMIT" not in card["tuning"]


def test_a_quiet_fortnight_says_nothing_and_a_past_scorecard_ignores_later_wake_ups():
    assert journal.wake_digest(journal.wake_scorecard("", now=NOW)) == []
    assert journal.wake_digest({}) == [] and journal.wake_digest({"week": {}}) == []
    earlier = journal.wake_scorecard(week_text(), now=NOW - timedelta(days=8), week=False)
    assert earlier["wakes"] == 3, "wake-ups after the moment asked about are not counted"


def test_the_phone_report_carries_the_week():
    lines = journal._wake_lines(journal.wake_scorecard(week_text(), now=NOW))
    at = lines.index("THE WATCHER'S WEEK")
    assert lines[at + 1].startswith("  Wake-ups in the last 7 days: 3")


def test_one_push_a_week_after_fridays_close_with_counts_only():
    db, sent = Database(":memory:"), []
    card = journal.wake_scorecard(week_text(), now=NOW)

    def post(url, data, headers):
        sent.append((url, data.decode("utf-8"), headers))

    thursday = NOW - timedelta(days=1)
    assert not notify.weekly_watcher(db, card, TOPIC, post, thursday), "the first one waits for a Friday close"
    assert not notify.weekly_watcher(db, card, TOPIC, post, NOW.replace(hour=20)), "3 PM Friday: market still open"
    assert notify.weekly_watcher(db, card, TOPIC, post, NOW)
    assert not notify.weekly_watcher(db, card, TOPIC, post, NOW + timedelta(hours=3)), "once"
    assert not notify.weekly_watcher(db, card, TOPIC, post, NOW + timedelta(days=5)), "next Wednesday: same week"
    assert notify.weekly_watcher(db, card, TOPIC, post, NOW + timedelta(days=10)), \
        "the computer was off on Friday: sent on its next run"
    assert len(sent) == 2
    url, body, headers = sent[0]
    assert url.endswith(TOPIC) and headers["Title"] == "StockSage: the watcher's week" and headers["Priority"] == "low"
    assert "Wake-ups in the last 7 days: 3" in body
    assert not re.search(r"\$|PLD|AMD|LMT|ORCL|\d{3}\.\d\d", body), "no tickers, prices, amounts or account"


def test_no_topic_nothing_to_say_or_a_failing_post_never_raises_and_is_not_marked_sent(monkeypatch):
    db = Database(":memory:")
    card = journal.wake_scorecard(week_text(), now=NOW)
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    assert not notify.weekly_watcher(db, card, None, lambda *a: None, NOW)
    assert not notify.weekly_watcher(db, {}, TOPIC, lambda *a: None, NOW)

    def broken(*a):
        raise OSError("offline")

    assert not notify.weekly_watcher(db, card, TOPIC, broken, NOW)
    assert not db.get_meta(notify.WATCHER_WEEK_KEY)
    assert notify.weekly_watcher(db, card, TOPIC, lambda *a: None, NOW), "sent on the next try"
