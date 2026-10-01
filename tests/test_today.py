"""StockSage Today: blunt calls, but a BUY is only plain when history backs it.

The dangerous failure is a BUY worded as an instruction without look-alike
support (or a sell sheet that quietly drops a risk rule). Each is attacked here.
Everything is offline; the book, broker and market are fakes.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from stocksage import today
from stocksage.actions import Action, Plan
from stocksage.analogs import AnalogBook
from stocksage.db import Database
from stocksage.engine import Engine
from tests.test_actions import FakeBroker, sug
from tests.test_backtest import make_samples
from tests.test_engine import FakeMarket

STRONG = {"trend_long": 0.95, "momentum_20d": 0.0, "macd": 0.0}
WEAK = {"trend_long": -0.95, "momentum_20d": 0.0, "macd": 0.0}
NOW = datetime(2026, 10, 6, 10, 0)


@pytest.fixture
def real_book():
    return AnalogBook.from_samples(make_samples(signal=0.02, n_dates=140, seed=1))


@pytest.fixture
def noise_book():
    return AnalogBook.from_samples(make_samples(signal=0.0, n_dates=140, seed=2))


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "123456789")


def enter(ticker="NEW", dollars=80.0):
    return Action("ENTER", ticker, "BUY", 3, f"Buy {ticker}", "the model likes it",
                  dollars=dollars, confidence="medium")


def stop(ticker="AAA", dollars=180.0):
    return Action("EXIT_STOP", ticker, "SELL", 1, f"Sell {ticker}", "through its stop",
                  dollars=dollars)


def evidence_from(book, features):
    return lambda ticker, side: book.evidence(features, side)


# ------------------------------------------------------------ the gate on BUY

def test_a_buy_history_backs_stays_a_buy(real_book):
    out = today.apply_evidence(Plan(actions=[enter()]), evidence_from(real_book, STRONG))
    assert [a.kind for a in out.actions] == ["ENTER"]


def test_a_buy_history_does_not_back_becomes_an_unnumbered_idea(real_book):
    """Same book, mirror-image setup: the book says this one trails the market."""
    out = today.apply_evidence(Plan(actions=[enter()]), evidence_from(real_book, WEAK))
    assert [a.kind for a in out.actions] == ["IDEA"]
    assert "does NOT back" in out.actions[0].why


def test_with_no_book_nothing_is_a_buy():
    out = today.apply_evidence(Plan(actions=[enter()]), lambda t, s: None)
    assert out.actions[0].kind == "IDEA"
    assert "analogs" in out.actions[0].why and ".\\start.bat" in out.actions[0].why
    assert "\\\\" not in out.actions[0].why, "a doubled backslash would not paste into PowerShell"


def test_risk_rule_exits_pass_through_the_gate_untouched(noise_book):
    """History being unconvinced about buying must never hold back a stop."""
    out = today.apply_evidence(Plan(actions=[stop(), enter()]), evidence_from(noise_book, STRONG))
    assert [a.kind for a in out.actions] == ["EXIT_STOP", "IDEA"]


def test_the_gate_does_not_mutate_the_plan_it_was_given(real_book):
    plan = Plan(actions=[enter()])
    today.apply_evidence(plan, evidence_from(real_book, WEAK))
    assert plan.actions[0].kind == "ENTER"


# ------------------------------------------------------------ the whole sheet

def sheet_for(book, suggestions=None, news=True, market=None):
    engine = Engine(db=Database(":memory:"), market=market or FakeMarket({}))
    suggestions = suggestions or default_suggestions(STRONG)
    return engine, today.build_sheet(engine, FakeBroker(), suggestions=suggestions,
                                     book=book, news=news, now=NOW)


def default_suggestions(features):
    out = [sug("PERS", action="HOLD", price=120), sug("VTI", action="HOLD"),
           sug("ACT", price=90), sug("ROTH", action="HOLD", price=80),
           sug("NEWIDEA", price=30)]
    for s in out:
        s.signals = dict(features)
    return out


def test_a_backed_buy_is_stated_plainly_with_its_tag(real_book):
    _, sheet = sheet_for(real_book)
    text = today.render_text(sheet)
    assert "BUY about $" in text and "[MODEL]" in text
    assert "BACKS this call" in text


def test_an_unbacked_buy_never_appears_as_an_instruction(real_book):
    suggestions = default_suggestions(WEAK)
    _, sheet = sheet_for(real_book, suggestions)
    text = today.render_text(sheet)
    assert "BUY about" not in text
    assert "Ideas history does not back" in text


def test_without_a_book_the_sheet_says_how_to_build_one_and_buys_nothing():
    _, sheet = sheet_for(None)
    text = today.render_text(sheet)
    assert "BUY about" not in text
    assert ".\\start.bat analogs" in text and "\\\\" not in text


def test_risk_rule_sells_are_plain_and_tagged_as_rules(noise_book):
    _, sheet = sheet_for(noise_book)
    agentic = sheet.accounts[0]
    sells = [d for d in agentic.directives if d.verb == "SELL"]
    assert sells and sells[0].text.startswith("SELL ALL ACT")
    assert sells[0].basis == "risk rule"
    assert "[RULE]" in today.render_text(sheet)


def test_the_bottom_line_comes_before_the_detail(real_book):
    _, sheet = sheet_for(real_book)
    text = today.render_text(sheet)
    assert text.index("BOTTOM LINE") < text.index("DETAIL")
    bottom = text[text.index("BOTTOM LINE"):text.index("DETAIL")]
    for account in sheet.accounts:
        assert account.title.upper() in bottom


def test_an_account_with_nothing_to_do_says_so_bluntly():
    quiet = today.AccountSheet(role="roth", title="Roth IRA", label="x", manual=True)
    lines = today._bottom_line(quiet)
    assert "  SELL: nothing." in lines and "  BUY: nothing." in lines
    with_ideas = today.AccountSheet(role="roth", title="Roth IRA", label="x", manual=True, ideas=["ZZZ"])
    assert any("Ideas history does not back: ZZZ" in ln for ln in today._bottom_line(with_ideas))


def test_news_is_attached_to_a_call_but_does_not_change_it(real_book):
    class Gloomy(FakeMarket):
        def news(self, ticker, limit=8):
            return [{"title": f"{ticker} faces lawsuit and downgrade", "publisher": "T", "link": ""}]

    _, quiet = sheet_for(real_book, news=False)
    _, loud = sheet_for(real_book, market=Gloomy({}))
    calls = lambda s: [(a.title, d.text) for a in s.accounts for d in a.directives]   # noqa: E731
    assert calls(quiet) == calls(loud), "headlines are context, not a veto"
    ctx = [c for a in loud.accounts for d in a.directives for c in d.context]
    assert any(c.startswith("News:") and "lawsuit" in c for c in ctx)
    assert not any(c.startswith("News:") for a in quiet.accounts for d in a.directives for c in d.context)


def test_a_news_feed_that_blows_up_does_not_break_the_sheet(real_book):
    class Broken(FakeMarket):
        def news(self, ticker, limit=8):
            raise OSError("feed down")

    _, sheet = sheet_for(real_book, market=Broken({}))
    assert sheet.accounts and "STOCKSAGE TODAY" in today.render_text(sheet)


def test_manual_accounts_get_a_link_the_routine_account_does_not(real_book):
    _, sheet = sheet_for(real_book)
    detail = today.render_text(sheet)
    detail = detail[detail.index("DETAIL"):]
    assert "DO IT: https://robinhood.com/us/en/stocks/" in detail
    agentic_block = detail.split("PERSONAL")[0]
    assert "DO IT" not in agentic_block


def test_the_sheet_never_claims_to_place_orders_or_leaks_account_numbers(real_book):
    _, sheet = sheet_for(real_book)
    text = today.render_text(sheet) + json.dumps(sheet.to_dict())
    assert "never places orders" in text
    for number in ("111111111", "123456789", "999999999"):
        assert number not in text


def test_the_lab_status_is_shown_beside_a_buy_and_says_untested_by_default(real_book):
    _, sheet = sheet_for(real_book)
    assert any("not been tested" in n for n in sheet.notes)


# ------------------------------------------------------------ lab status

def test_lab_status_reflects_whether_the_filter_passed():
    db = Database(":memory:")
    assert "not been tested" in today.lab_status(db)
    db.set_meta("lab_at", "2026-10-01T12:00:00")
    db.set_meta("lab_winners", "momentum")
    assert "did NOT pass" in today.lab_status(db)
    db.set_meta("lab_winners", f"momentum,{today.LAB_FILTER_NAME}")
    assert "passed an out-of-sample test on 2026-10-01" in today.lab_status(db)


# ------------------------------------------------------------ market + saving

def test_market_lines_survive_missing_data():
    engine = Engine(db=Database(":memory:"), market=FakeMarket({}))
    assert today._market_lines(engine) == []


def test_save_local_writes_json_and_text(real_book, tmp_path):
    _, sheet = sheet_for(real_book)
    path = today.save_local(sheet)
    assert path == tmp_path / "today.txt" and path.read_text().startswith("STOCKSAGE TODAY")
    data = json.loads((tmp_path / "today.json").read_text())
    assert [a["role"] for a in data["accounts"]] == ["agentic", "personal", "roth"]


def test_a_stale_book_is_called_out(real_book):
    real_book.built_at -= 30 * 86400
    _, sheet = sheet_for(real_book)
    assert any("days old" in n for n in sheet.notes)


# ------------------------------------------------------------ wiring

def _publish(monkeypatch, tmp_path, uploads, upload_fails=False, no_broker=False):
    from stocksage import cli
    from tests.test_notify import _one_account, _publish_env, act

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    cli_mod = _publish_env(monkeypatch, tmp_path, topic=None)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "positions": [{}], "buying_power": 1.0})
    monkeypatch.setattr("stocksage.advisor.plans_for_accounts",
                        lambda *a, **k: _one_account(Plan(actions=[act("EXIT_STOP", "AAA")])))

    def upload(name, text):
        if upload_fails:
            raise OSError("drive down")
        uploads.append((name, text))
        return "id"

    monkeypatch.setattr("stocksage.drive_api.publish_text", upload)
    assert cli is cli_mod
    return cli.main(["publish-drive"] + (["--no-broker"] if no_broker else []))


def test_the_scheduled_run_puts_the_sheet_in_drive_for_the_phone(monkeypatch, tmp_path):
    uploads = []
    assert _publish(monkeypatch, tmp_path, uploads) == 0
    assert [n for n, _ in uploads] == ["StockSage Today.txt"]
    assert "SELL ALL AAA" in uploads[0][1]


def test_a_drive_failure_never_fails_the_publish_or_loses_the_local_copy(monkeypatch, tmp_path, capsys):
    assert _publish(monkeypatch, tmp_path, [], upload_fails=True) == 0
    assert "could not upload the sheet" in capsys.readouterr().out
    assert (today.state_dir() / "today.txt").exists()


def test_a_run_without_broker_access_builds_no_sheet(monkeypatch, tmp_path):
    from tests.test_notify import _publish_env

    uploads = []
    from stocksage import cli
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    _publish_env(monkeypatch, tmp_path, topic=None)
    monkeypatch.setattr("stocksage.advisor.desktop_sync_cycle",
                        lambda *a, **k: {"fills_ingested": 0, "graded": 0, "holdings": [],
                                         "broker_skipped": True})
    monkeypatch.setattr("stocksage.drive_api.publish_text", lambda n, t: uploads.append(n))
    assert cli.main(["publish-drive", "--no-broker"]) == 0
    assert uploads == [] and not (today.state_dir() / "today.txt").exists()


def test_the_analogs_command_builds_the_book(monkeypatch, capsys):
    from stocksage import backtest, cli

    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)
    monkeypatch.setattr(backtest, "collect_samples",
                        lambda market, tickers=None, period="5y": make_samples(signal=0.02, n_dates=60))
    assert cli.main(["analogs"]) == 0
    assert "past setups saved" in capsys.readouterr().out
    book = AnalogBook.load()
    assert book is not None and len(book) > 0


def test_a_short_backtest_does_not_replace_a_bigger_fresh_book():
    from stocksage.analogs import store_if_better

    big = AnalogBook.from_samples(make_samples(signal=0.02, n_dates=120))
    small = AnalogBook.from_samples(make_samples(signal=0.02, n_dates=40))
    assert store_if_better(big) and not store_if_better(small)
    assert len(AnalogBook.load()) == len(big)
    big.built_at -= 30 * 86400
    big.save()                                         # now stale: anything fresh replaces it
    assert store_if_better(small) and len(AnalogBook.load()) == len(small)


def test_the_lab_tests_the_lookalike_filter_and_remembers_whether_it_passed(monkeypatch, capsys):
    from stocksage import backtest, cli, lab
    from tests.test_lab import make_rows

    monkeypatch.setattr("stocksage.data.MarketData.prefetch", lambda self, *a, **k: 0)
    monkeypatch.setattr(backtest, "collect_samples",
                        lambda market, tickers=None, period="2y": make_samples(signal=0.0, n_dates=120))
    monkeypatch.setattr(lab, "collect_monthly",
                        lambda market, tickers=None, period="5y": make_rows(effect=0.03))
    monkeypatch.setenv("HOME", str(today.state_dir()))
    monkeypatch.setenv("USERPROFILE", str(today.state_dir()))
    assert cli.main(["lab"]) == 0
    assert today.LAB_FILTER_NAME in capsys.readouterr().out
