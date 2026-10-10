"""Silence must be noticed, but a quiet weekend must not be mistaken for it."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stocksage import watchdog

UTC = timezone.utc
FRI_AFTERNOON = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)      # a Friday
MON_MORNING = datetime(2026, 10, 12, 15, 30, tzinfo=UTC)       # the Monday after
WED_MORNING = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)


def test_weekends_do_not_count_as_silence():
    assert watchdog.business_hours_between(FRI_AFTERNOON, MON_MORNING) == pytest.approx(
        (24 - 19.5) + 15.5)
    assert watchdog.verdict(FRI_AFTERNOON, MON_MORNING) is None, "Friday's sheet is fine on Monday"


def test_a_missed_day_is_noticed():
    msg = watchdog.verdict(FRI_AFTERNOON, WED_MORNING)
    assert msg and "since Fri 09 Oct" in msg and "always-on computer" in msg


def test_no_file_at_all_is_noticed():
    assert "never saved" in watchdog.verdict(None, MON_MORNING)


def test_a_fresh_sheet_says_nothing():
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    assert watchdog.verdict(now - timedelta(hours=5), now) is None


def test_the_boundary_is_thirty_business_hours():
    base = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)               # Tuesday
    assert watchdog.verdict(base, base + timedelta(hours=29.9)) is None
    assert watchdog.verdict(base, base + timedelta(hours=30.5)) is not None


def test_a_stale_sheet_sends_one_plain_push(monkeypatch, capsys):
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "t")
    sent = []
    code = watchdog.run(post=lambda u, d, h: sent.append((u, d.decode(), h)), now=WED_MORNING,
                        modified=FRI_AFTERNOON)
    assert code == 0 and len(sent) == 1
    url, body, headers = sent[0]
    assert url.endswith("/t") and "gone quiet" in headers["Title"]
    assert not re.search(r"\d{4,}|\$", body), "a push must carry no amounts or account numbers"


def test_a_fresh_sheet_sends_nothing(monkeypatch):
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "t")
    sent = []
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    assert watchdog.run(post=lambda *a: sent.append(a), now=now, modified=now - timedelta(hours=2)) == 0
    assert sent == []


def test_a_failed_check_is_reported_not_swallowed(monkeypatch, capsys):
    from stocksage import drive_api

    def boom(name):
        raise RuntimeError("drive down")

    monkeypatch.setattr(drive_api, "modified_time", boom)
    assert watchdog.run(post=lambda *a: None) == 1
    assert "Could not check Drive" in capsys.readouterr().out


def test_no_topic_means_a_printed_note_not_a_crash(monkeypatch, capsys):
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    assert watchdog.run(post=lambda *a: 1 / 0, now=WED_MORNING, modified=FRI_AFTERNOON) == 0
    assert "nothing was sent" in capsys.readouterr().out


# ------------------------------------------------------------ the workflow file

yaml = pytest.importorskip("yaml")
PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "watchdog.yml"
TEXT = PATH.read_text(encoding="utf-8")
WF = yaml.safe_load(TEXT)


def test_the_watchdog_holds_no_broker_credentials_and_only_two_secrets():
    parsed = yaml.dump(WF).upper()
    for word in ("ROBINHOOD", "ROBIN_STOCKS", "MFA", "TOTP", "PASSWORD"):
        assert word not in parsed
    assert set(re.findall(r"secrets\.([A-Z_]+)", TEXT)) == {"STOCKSAGE_DRIVE_TOKEN", "STOCKSAGE_NTFY_TOPIC"}
    for step in WF["jobs"]["watch"]["steps"]:
        assert "secrets." not in step.get("run", "")


def test_it_runs_weekdays_only_and_can_be_run_by_hand():
    triggers = WF.get("on", WF.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    for t in triggers["schedule"]:
        assert t["cron"].split()[4] == "1-5"
    assert "python -m stocksage watchdog" in TEXT
