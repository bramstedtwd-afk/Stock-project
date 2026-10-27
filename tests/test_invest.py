"""The monthly index reminder: stated plainly on the sheet, never an amount in a push."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from stocksage import cli, envfile, notify, today
from stocksage.db import Database

NOW = datetime(2026, 11, 3, 9, 0)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    for k in ("STOCKSAGE_MONTHLY_INVEST", "STOCKSAGE_MONTHLY_INVEST_FUND", "STOCKSAGE_MONTHLY_INVEST_ACCOUNT",
              "STOCKSAGE_NTFY_TOPIC"):
        monkeypatch.delenv(k, raising=False)
    yield
    for k in ("STOCKSAGE_MONTHLY_INVEST", "STOCKSAGE_MONTHLY_INVEST_FUND", "STOCKSAGE_MONTHLY_INVEST_ACCOUNT"):
        os.environ.pop(k, None)


def buy(db, ticker, qty, price, when):
    db.conn.execute("INSERT INTO rh_orders (order_id, ticker, side, quantity, price, executed_at)"
                    " VALUES (?, ?, 'buy', ?, ?, ?)", (f"{ticker}{when}{qty}", ticker, qty, price, when))
    db.conn.commit()


def test_nothing_is_shown_until_an_amount_is_set():
    assert today.invest_line(Database(":memory:"), NOW) is None


def test_the_command_saves_the_plan_and_the_sheet_states_it(capsys, tmp_path):
    assert cli.main(["invest", "$200", "--account", "roth"]) == 0
    assert "STOCKSAGE_MONTHLY_INVEST=200" in (tmp_path / ".env").read_text()
    line = today.invest_line(Database(":memory:"), NOW)
    assert line == "MONTHLY  Put $200 into VTI in your Roth IRA this month."


def test_a_purchase_this_month_counts_and_last_month_does_not():
    plan = {"amount": 200.0, "fund": "VTI", "account": "roth"}
    db = Database(":memory:")
    buy(db, "VTI", 0.2, 300.0, "2026-10-28T15:00:00Z")          # last month
    assert "Put $200" in today.invest_line(db, NOW, plan)
    buy(db, "VTI", 0.3, 300.0, "2026-11-02T15:00:00Z")          # $90 this month
    assert "Put $110" in today.invest_line(db, NOW, plan) and "$90 already in" in today.invest_line(db, NOW, plan)
    buy(db, "VTI", 0.4, 300.0, "2026-11-03T14:00:00Z")          # now $210
    assert today.invest_line(db, NOW, plan).startswith("MONTHLY  Done")


def test_bad_amounts_are_refused_and_off_turns_it_off(capsys):
    assert cli.main(["invest", "lots"]) == 1
    assert ".\\start.bat invest 200" in capsys.readouterr().out
    cli.main(["invest", "150"])
    assert cli.main(["invest", "off"]) == 0
    assert today.invest_plan() is None


def test_the_phone_reminder_never_carries_the_amount():
    class Rec:
        def __init__(self):
            self.sent = []

        def __call__(self, url, data, headers):
            self.sent.append(data.decode())

    cli.main(["invest", "275"])
    rec = Rec()
    assert notify.monthly_scorecard(Database(":memory:"), "t", rec, NOW) is True
    assert "contribution is due" in rec.sent[0] and "275" not in rec.sent[0] and "$" not in rec.sent[0]
