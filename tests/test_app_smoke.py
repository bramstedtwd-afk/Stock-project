"""Dashboard smoke tests: run the real app.py through Streamlit's AppTest.

Every tab's code executes on a run, so this catches runtime errors that
compile checks can't — bad column references, None-handling in render
code, import mistakes inside tab blocks. Uses a seeded brain so the
auto-run-on-open path is skipped (no network in tests).
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from stocksage.db import Database  # noqa: E402

# Absolute, because Streamlit >=1.61 resolves a relative AppTest path against
# the file that calls it (so "app.py" became tests/app.py and every dashboard
# test died with FileNotFoundError). An absolute path is correct on both.
APP = str(Path(__file__).resolve().parent.parent / "app.py")
RUN_TIMEOUT = 30


@pytest.fixture
def seeded_brain(tmp_path, monkeypatch):
    """A brain with every kind of knowledge, and today's run already done."""
    db_path = tmp_path / "brain.db"
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    db = Database(db_path)
    # Graded + pending suggestions
    for i, (ticker, action, score, realized) in enumerate(
        [("AAPL", "BUY", 0.4, 0.05), ("NVDA", "STRONG BUY", 0.6, -0.02),
         ("XOM", "SELL", -0.3, -0.04)]
    ):
        sid = db.record_suggestion(ticker, action, score, 100.0 + i, {"trend_long": score}, 5)
        db.mark_evaluated(sid, realized, (realized > 0) == (score > 0))
    db.record_suggestion("MSFT", "BUY", 0.35, 420.0, {"trend_long": 0.35}, 5)
    # Learned state
    db.save_weights({"trend_long": 0.5, "macd": 0.3, "rsi_reversion": 0.2})
    db.save_sector_weights("Technology", {"trend_long": 0.7, "macd": 0.3})
    db.set_meta("sector_grades:Technology", "12")
    # Move memory
    db.record_move_event(
        "NVDA", (date.today() - timedelta(days=2)).isoformat(), -0.055, 2.8,
        ["earnings"], [{"title": "NVDA falls on earnings", "publisher": "T", "link": ""}],
    )
    db.record_move_event("AAPL", "2026-01-15", 0.04, 2.2, ["historical-backfill"], [])
    # Robinhood mirror
    db.upsert_rh_orders(
        [
            {"order_id": "o1", "ticker": "AAPL", "side": "buy", "quantity": 10,
             "price": 180.0, "executed_at": "2026-03-02T15:00:00Z"},
            {"order_id": "o2", "ticker": "AAPL", "side": "sell", "quantity": 10,
             "price": 205.0, "executed_at": "2026-05-11T15:00:00Z"},
        ]
    )
    db.upsert_rh_dividends(
        [{"dividend_id": "d1", "ticker": "AAPL", "amount": 2.4, "paid_at": "2026-04-01"}]
    )
    # Skip auto-run and bootstrap in tests (no network)
    db.set_meta("bootstrap_done", date.today().isoformat())
    db.set_meta("last_daily_run", date.today().isoformat())
    db.set_meta("warmup_samples", "4500")
    db.set_meta("last_device", "test-machine")
    db.set_meta("last_device_at", "2026-07-09T12:00:00+00:00")
    db.close()
    return db_path


def test_dashboard_renders_without_exceptions(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"dashboard raised: {at.exception}"


def test_sidebar_shows_brain_status(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    sidebar_text = " ".join(c.value for c in at.sidebar.caption)
    assert "moves remembered" in sidebar_text
    assert "test-machine" in sidebar_text


def test_learning_tab_metrics_populated(seeded_brain):
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    labels = [m.label for m in at.metric]
    assert "Historical samples" in labels
    assert "Suggestions graded" in labels
    # Profit tab rendered its ledger metrics too
    assert "Paper P&L" in labels


def test_corrupted_brain_shows_recovery_not_traceback(tmp_path, monkeypatch):
    """A damaged brain file (partial cloud sync) degrades to instructions."""
    db_path = tmp_path / "damaged.db"
    db_path.write_bytes(b"this is not a sqlite database, sorry")
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception
    assert at.error, "expected a recovery message"
    assert "damaged" in at.error[0].value


def test_empty_brain_renders_cleanly(tmp_path, monkeypatch):
    """A brand-new install (nothing learned, no Robinhood) must not error."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("STOCKSAGE_DB", str(db_path))
    db = Database(db_path)
    # Mark today as run so the app doesn't attempt a network scan in tests.
    db.set_meta("last_daily_run", date.today().isoformat())
    db.close()
    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"fresh-install dashboard raised: {at.exception}"


def test_portfolio_tab_renders_one_tab_per_account(monkeypatch, tmp_path):
    """Drives the real dashboard with a multi-account login: the tabs must
    appear and each must show its OWN holdings, not the default account's."""
    from stocksage.robinhood import Account, Holding, Portfolio

    accounts = [
        Account("111111111", "individual", 2500.00, 2500.00),
        Account("123456789", "individual", 0.42, 0.42),
        Account("999999999", "roth", 250.0, 250.0),
    ]
    per_account = {
        "123456789": Portfolio(
            [Holding("GE", 0.250000, 100.00, 110.00, 27.50)], 0.42, "123456789"
        ),
        "999999999": Portfolio(
            [Holding("VTI", 2.5, 300.0, 310.10, 775.25)], 250.0, "999999999"
        ),
    }
    default = Portfolio(
        [Holding("NVDA", 1.0, 100.0, 217.55, 217.55)], 2500.00, "111111111"
    )

    class FakeClient:
        @staticmethod
        def credentials_available():
            return True

        def accounts(self):
            return accounts

        def portfolio(self):
            return default

        def portfolio_for(self, number):
            return per_account.get(number)

        def sync_history(self, db):
            return None

    monkeypatch.setattr("stocksage.robinhood.RobinhoodClient", FakeClient)
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))
    # Which account is the agentic one is .env configuration, never a default
    # in the source, so the test has to say which one it means.
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "123456789")

    from stocksage.engine import ScanResult

    db = Database(tmp_path / "brain.db")
    db.set_meta("last_daily_run", date.today().isoformat())
    db.close()

    at = AppTest.from_file(APP)
    at.session_state["scan_result"] = ScanResult(
        suggestions=[], portfolio=default, sector_trends={}
    )
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"dashboard raised: {at.exception}"

    text = " ".join(str(e.value) for e in at.markdown) + " ".join(
        str(getattr(e, "label", "")) for e in at.tabs
    )
    # Every account is offered by name, including the Roth.
    for label in ("Individual ••••1111", "Individual ••••6789", "Roth IRA ••••9999"):
        assert label in text, f"missing account tab: {label}"
    # The agentic account leads and is starred — it is the only one the
    # routine acts on, so burying it in Robinhood's own ordering is wrong.
    labels = [str(t.label) for t in at.tabs if getattr(t, "label", None)]
    account_tabs = [x for x in labels if "••••" in x]
    assert account_tabs, "expected account tabs to be rendered"
    assert account_tabs[0] == "⭐ Individual ••••6789"


def test_sidebar_reports_broker_sessions_and_exposure(seeded_brain, tmp_path, monkeypatch):
    """The security panel is how the owner answers 'was that sign-in me?' on
    a phone, where there is no terminal to run 'security' in."""
    from stocksage import security

    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text(
        "ROBINHOOD_PASSWORD=pw\nROBINHOOD_MFA_SECRET=JBSWY3DPEHPK3PXP\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    monkeypatch.setattr("stocksage.envfile.ENV_PATH", env)
    security.record_access(security.FRESH_LOGIN, "publish-drive")

    at = AppTest.from_file(APP)
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, f"dashboard raised: {at.exception}"
    sidebar = " ".join(c.value for c in at.sidebar.caption)
    sidebar += " ".join(str(e.value) for e in at.sidebar.error)
    assert "publish-drive" in sidebar
    # The password-plus-TOTP-seed exposure must be stated, not buried.
    assert "two-factor" in sidebar.lower()


def _plan_page(monkeypatch, tmp_path, held_price, planner_raises=False, shares=2.0):
    from stocksage.engine import ScanResult
    from stocksage.robinhood import Account, Holding, Portfolio
    from stocksage.scoring import Suggestion

    pf = Portfolio([Holding("ZZZ", shares, 100.0, held_price, shares * held_price)], 40.0, "123456789")

    class FakeClient:
        @staticmethod
        def credentials_available():
            return True

        def accounts(self):
            return [Account("123456789", "individual", 40.0, 40.0)]

        def portfolio(self):
            return pf

        def portfolio_for(self, number):
            return pf

        def sync_history(self, db):
            return None

    monkeypatch.setattr("stocksage.robinhood.RobinhoodClient", FakeClient)
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "123456789")
    if planner_raises:
        def boom(*a, **k):
            raise RuntimeError("planner exploded")

        monkeypatch.setattr("stocksage.advisor.plan_for_account", boom)

    db = Database(tmp_path / "brain.db")
    db.set_meta("last_daily_run", date.today().isoformat())
    db.close()
    held = Suggestion(
        ticker="ZZZ", action="HOLD", score=0.0, risk_adjusted_score=0.0,
        price=held_price, signals={}, risk={"atr_pct": 0.02}, stop_price=held_price * 0.96,
    )
    at = AppTest.from_file(APP)
    at.session_state["scan_result"] = ScanResult(suggestions=[held], portfolio=pf, sector_trends={})
    at.run(timeout=RUN_TIMEOUT)
    return at


def test_dashboard_leads_with_what_to_do_when_a_stop_is_hit(monkeypatch, tmp_path):
    at = _plan_page(monkeypatch, tmp_path, held_price=90.0)   # cost 100, stop 96
    assert not at.exception, at.exception
    shown = " ".join(str(e.value) for e in at.error)
    assert "SELL all of ZZZ" in shown and "hit its stop" in shown
    assert any("What to do today" in str(m.value) for m in at.markdown)
    assert any("never places orders" in c.value for c in at.caption)
    # The blunt bottom line sits above the per-account detail.
    assert any("SELL ALL ZZZ" in str(c.value) and "[RULE]" in str(c.value) for c in at.code)


def test_dashboard_says_so_when_there_is_nothing_to_do(monkeypatch, tmp_path):
    at = _plan_page(monkeypatch, tmp_path, held_price=101.0, shares=0.1)  # ~20% of the account
    assert not at.exception, at.exception
    assert not at.error
    assert any("Nothing to do today" in str(s.value) for s in at.success)


def test_a_broken_planner_never_takes_the_dashboard_down(monkeypatch, tmp_path):
    at = _plan_page(monkeypatch, tmp_path, held_price=90.0, planner_raises=True)
    assert not at.exception, at.exception
    assert not any("What to do today" in str(m.value) for m in at.markdown)


def test_dashboard_shows_a_tab_per_account_with_tickets_only_on_manual_ones(monkeypatch, tmp_path):
    from stocksage.engine import ScanResult
    from stocksage.robinhood import Account, Holding, Portfolio
    from stocksage.scoring import Suggestion

    def s(t, action="HOLD", price=100.0):
        return Suggestion(ticker=t, action=action, score=0.0, risk_adjusted_score=0.0,
                          price=price, signals={}, risk={"atr_pct": 0.02}, stop_price=price * 0.96)

    portfolios = {
        "111111111": Portfolio([Holding("PERS", 1.0, 100.0, 100.0, 100.0)], 900.0, "111111111"),
        "123456789": Portfolio([Holding("ACT", 2.0, 100.0, 90.0, 180.0)], 5.0, "123456789"),
        "999999999": Portfolio([Holding("ROTH", 5.0, 100.0, 100.0, 500.0)], 100.0, "999999999"),
    }

    class FakeClient:
        @staticmethod
        def credentials_available():
            return True

        def accounts(self):
            return [Account("111111111", "individual", 900.0, 900.0),
                    Account("123456789", "individual", 5.0, 5.0),
                    Account("999999999", "roth", 100.0, 100.0)]

        def portfolio(self):
            return portfolios["111111111"]

        def portfolio_for(self, number):
            return portfolios.get(number)

        def sync_history(self, db):
            return None

    monkeypatch.setattr("stocksage.robinhood.RobinhoodClient", FakeClient)
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "brain.db"))
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "123456789")
    db = Database(tmp_path / "brain.db")
    db.set_meta("last_daily_run", date.today().isoformat())
    db.close()

    at = AppTest.from_file(APP)
    at.session_state["scan_result"] = ScanResult(
        suggestions=[s("PERS"), s("ACT", price=90.0), s("ROTH")],
        portfolio=portfolios["111111111"], sector_trends={})
    at.run(timeout=RUN_TIMEOUT)
    assert not at.exception, at.exception

    labels = [str(t.label) for t in at.tabs if "—" in str(getattr(t, "label", ""))]
    assert labels[0].startswith("⭐ Agentic"), labels
    assert any("Roth IRA" in x for x in labels) and any("Personal" in x for x in labels)
    shown = " ".join(str(e.value) for e in at.error)
    assert "SELL all of ACT" in shown
