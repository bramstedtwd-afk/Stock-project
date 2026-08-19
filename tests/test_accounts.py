"""Multi-account Robinhood access — offline, against a fake robin_stocks.

The real client is never touched: a stub module is installed into sys.modules
so every branch (enumeration, per-account positions, malformed data) is
exercised with no network and no credentials.
"""

import sys
import types

import pytest

from stocksage.robinhood import Account, RobinhoodClient

ACCOUNTS = [
    {"account_number": "111111111", "type": "individual",
     "buying_power": "2500.00", "portfolio_cash": "2500.00"},
    {"account_number": "123456789", "type": "individual",
     "buying_power": "0.42", "portfolio_cash": "0.42"},
    {"account_number": "999999999", "type": "roth",
     "buying_power": "250.00", "portfolio_cash": "250.00"},
]

POSITIONS = {
    "123456789": [
        {"instrument": "https://api.robinhood.com/instruments/ge/",
         "quantity": "0.250000", "average_buy_price": "100.00"},
        {"instrument": "https://api.robinhood.com/instruments/ms/",
         "quantity": "0.175087", "average_buy_price": "214.18"},
        # A closed position Robinhood still lists — must not appear.
        {"instrument": "https://api.robinhood.com/instruments/old/",
         "quantity": "0", "average_buy_price": "10.00"},
    ],
    "999999999": [
        {"instrument": "https://api.robinhood.com/instruments/vti/",
         "quantity": "2.5", "average_buy_price": "300.00"},
    ],
}

SYMBOLS = {
    "https://api.robinhood.com/instruments/ge/": "GE",
    "https://api.robinhood.com/instruments/ms/": "MS",
    "https://api.robinhood.com/instruments/old/": "OLD",
    "https://api.robinhood.com/instruments/vti/": "VTI",
}

PRICES = {"GE": 110.00, "MS": 180.00, "VTI": 310.10}


@pytest.fixture
def fake_rh(monkeypatch):
    """Install a stub robin_stocks.robinhood and log the client straight in."""
    calls = {"positions_for": []}

    def load_account_profile(account_number=None, info=None, dataType="indexzero"):
        if dataType == "results":
            return list(ACCOUNTS)
        if account_number:
            return next(
                (a for a in ACCOUNTS if a["account_number"] == account_number), {}
            )
        return ACCOUNTS[0]

    def get_open_stock_positions(account_number=None, info=None):
        calls["positions_for"].append(account_number)
        return list(POSITIONS.get(account_number, []))

    rh = types.ModuleType("robin_stocks.robinhood")
    rh.profiles = types.SimpleNamespace(load_account_profile=load_account_profile)
    rh.account = types.SimpleNamespace(
        get_open_stock_positions=get_open_stock_positions,
        load_account_profile=load_account_profile,
    )
    rh.stocks = types.SimpleNamespace(
        get_symbol_by_url=lambda url: SYMBOLS.get(url),
        get_latest_price=lambda syms, **kw: [PRICES.get(s) for s in syms],
    )
    pkg = types.ModuleType("robin_stocks")
    pkg.robinhood = rh
    monkeypatch.setitem(sys.modules, "robin_stocks", pkg)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", rh)

    client = RobinhoodClient()
    client._logged_in = True  # skip credential handling; this is read-only
    return client, calls


def test_every_account_under_the_login_is_listed(fake_rh):
    client, _ = fake_rh
    accounts = client.accounts()
    assert [a.number for a in accounts] == ["111111111", "123456789", "999999999"]
    assert all(isinstance(a, Account) for a in accounts)
    agentic = next(a for a in accounts if a.number == "123456789")
    assert agentic.buying_power == pytest.approx(0.42)


def test_account_labels_are_human_readable(fake_rh):
    client, _ = fake_rh
    labels = {a.number: a.label for a in client.accounts()}
    assert labels["999999999"] == "Roth IRA ••••9999"
    assert labels["123456789"] == "Individual ••••6789"


def test_positions_are_scoped_to_the_named_account(fake_rh):
    """The whole point: two accounts under one login must not blur together."""
    client, calls = fake_rh
    agentic = client.portfolio_for("123456789")
    roth = client.portfolio_for("999999999")

    assert {h.ticker for h in agentic.holdings} == {"GE", "MS"}
    assert {h.ticker for h in roth.holdings} == {"VTI"}
    assert agentic.account_number == "123456789"
    assert roth.account_number == "999999999"
    # Each fetch asked for its own account, never the default.
    assert calls["positions_for"] == ["123456789", "999999999"]


def test_holdings_are_priced_and_valued(fake_rh):
    client, _ = fake_rh
    ge = next(h for h in client.portfolio_for("123456789").holdings if h.ticker == "GE")
    assert ge.current_price == pytest.approx(110.00)
    assert ge.equity == pytest.approx(0.250000 * 110.00)


def test_closed_positions_are_dropped(fake_rh):
    """Robinhood keeps listing a sold-out position at quantity 0."""
    client, _ = fake_rh
    assert "OLD" not in {h.ticker for h in client.portfolio_for("123456789").holdings}


def test_buying_power_comes_from_the_same_account(fake_rh):
    """The v11 hazard: the brief once carried $2,500 of a personal account's
    buying power while the routine traded an account holding $0.42."""
    client, _ = fake_rh
    assert client.portfolio_for("123456789").buying_power == pytest.approx(0.42)
    assert client.portfolio_for("111111111").buying_power == pytest.approx(2500.00)


def test_default_portfolio_records_which_account_it_described(fake_rh, monkeypatch):
    """Silent single-account reporting is what caused the mismatch."""
    import robin_stocks.robinhood as rh

    rh.build_holdings = lambda with_dividends=False: {
        "GE": {"quantity": "0.250000", "average_buy_price": "100.00",
               "price": "110.00", "equity": "27.50"}
    }
    client, _ = fake_rh
    p = client.portfolio()
    assert p.account_number == "111111111"  # the default, now stated not implied


def test_malformed_entries_never_break_a_run(fake_rh, monkeypatch):
    import robin_stocks.robinhood as rh

    monkeypatch.setattr(
        rh.profiles, "load_account_profile",
        lambda account_number=None, info=None, dataType="indexzero": (
            [{"account_number": "1", "type": "individual", "buying_power": "x"},
             "not-a-dict",
             {"account_number": "", "type": "individual", "buying_power": "1"},
             {"account_number": "222", "type": "roth", "buying_power": "5.0",
              "portfolio_cash": "5.0"}]
            if dataType == "results" else {}
        ),
    )
    client, _ = fake_rh
    # Only the well-formed, identifiable account survives; no exception.
    assert [a.number for a in client.accounts()] == ["222"]


def test_a_broken_account_call_returns_none_not_an_exception(fake_rh, monkeypatch):
    import robin_stocks.robinhood as rh

    def boom(account_number=None, info=None):
        raise RuntimeError("Robinhood 503")

    monkeypatch.setattr(rh.account, "get_open_stock_positions", boom)
    client, _ = fake_rh
    assert client.portfolio_for("123456789") is None
