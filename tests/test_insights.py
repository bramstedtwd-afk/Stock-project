import pytest

from stocksage.db import Database
from stocksage.insights import fifo_round_trips, model_alignment, trading_insights


def order(oid, ticker, side, qty, price, at):
    return {
        "order_id": oid, "ticker": ticker, "side": side,
        "quantity": qty, "price": price, "executed_at": at,
    }


ORDERS = [
    order("1", "AAPL", "buy", 10, 100.0, "2026-01-05T15:00:00Z"),
    order("2", "AAPL", "buy", 5, 110.0, "2026-02-02T15:00:00Z"),
    order("3", "AAPL", "sell", 12, 120.0, "2026-03-02T15:00:00Z"),  # 10@100 + 2@110
    order("4", "NVDA", "buy", 4, 500.0, "2026-04-01T15:00:00Z"),
    order("5", "NVDA", "sell", 4, 450.0, "2026-04-20T15:00:00Z"),   # a loss
    order("6", "XOM", "buy", 8, 90.0, "2026-05-01T15:00:00Z"),      # still open
]


def test_fifo_matching_and_partial_lots():
    trips, open_lots = fifo_round_trips(ORDERS)
    # AAPL sell consumes the whole first lot then part of the second.
    aapl = [t for t in trips if t.ticker == "AAPL"]
    assert [(t.quantity, t.buy_price) for t in aapl] == [(10, 100.0), (2, 110.0)]
    assert aapl[0].pnl == pytest.approx(200.0)
    assert aapl[1].pnl == pytest.approx(20.0)
    assert aapl[0].held_days == 56
    # 3 AAPL shares remain open plus the XOM lot.
    assert open_lots["AAPL"]["quantity"] == pytest.approx(3)
    assert open_lots["XOM"]["cost"] == pytest.approx(720.0)
    assert "NVDA" not in open_lots


def test_sell_without_lot_is_skipped_not_guessed():
    trips, open_lots = fifo_round_trips(
        [order("1", "TSLA", "sell", 5, 200.0, "2026-01-05T15:00:00Z")]
    )
    assert trips == [] and open_lots == {}


def test_trading_insights_headline_numbers():
    dividends = [{"amount": 12.5}, {"amount": 7.5}]
    ti = trading_insights(ORDERS, dividends)
    assert ti["round_trips"] == 3
    assert ti["realized_pnl"] == pytest.approx(200 + 20 - 200)
    assert ti["win_rate"] == pytest.approx(2 / 3)
    assert ti["dividends_total"] == 20.0
    assert ti["best_name"][0] == "AAPL" and ti["worst_name"][0] == "NVDA"
    assert ti["open_positions"] == 2  # AAPL remainder + XOM


def test_model_alignment():
    suggestions = [
        {"created_at": "2026-01-03T00:00:00", "ticker": "AAPL", "action": "BUY"},
        {"created_at": "2026-04-18T00:00:00", "ticker": "NVDA", "action": "STRONG BUY"},
        {"created_at": "2026-04-30T00:00:00", "ticker": "XOM", "action": "HOLD"},
    ]
    align = model_alignment(ORDERS, suggestions)
    # AAPL buy on Jan 5 agreed with the Jan 3 BUY call.
    # NVDA sell on Apr 20 disagreed with the Apr 18 STRONG BUY.
    # XOM buy had a HOLD (no direction) -> uncovered; others out of window.
    assert align["agreed"] == 1
    assert align["disagreed"] == 1
    assert align["agreement_rate"] == pytest.approx(0.5)
    assert align["disagreements"][0]["ticker"] == "NVDA"
    assert align["uncovered"] >= 1


def test_alignment_empty_inputs():
    assert model_alignment([], [])["agreement_rate"] is None


def test_rh_mirror_idempotent():
    db = Database(":memory:")
    orders = [order("a1", "AAPL", "buy", 1, 100.0, "2026-01-05T15:00:00Z")]
    assert db.upsert_rh_orders(orders) == 1
    assert db.upsert_rh_orders(orders) == 0  # same Robinhood id: no duplicate
    dividends = [{"dividend_id": "d1", "ticker": "KO", "amount": 3.2, "paid_at": "2026-02-01"}]
    assert db.upsert_rh_dividends(dividends) == 1
    assert db.upsert_rh_dividends(dividends) == 0
    assert len(db.rh_orders()) == 1 and len(db.rh_dividends()) == 1


def test_sync_history_via_stubbed_client():
    from stocksage.robinhood import RobinhoodClient

    class StubClient(RobinhoodClient):
        def order_history(self):
            return [order("o1", "AAPL", "buy", 2, 150.0, "2026-06-01T15:00:00Z")]

        def dividend_history(self):
            return [{"dividend_id": "d9", "ticker": "AAPL", "amount": 1.1, "paid_at": "2026-06-15"}]

    db = Database(":memory:")
    stats = StubClient().sync_history(db)
    assert stats == {"orders_added": 1, "orders_total": 1, "dividends_added": 1}
    assert db.get_meta("last_rh_sync") is not None
    again = StubClient().sync_history(db)
    assert again["orders_added"] == 0  # constant update stays idempotent


def test_sync_history_offline_returns_none():
    from stocksage.robinhood import RobinhoodClient

    class OfflineClient(RobinhoodClient):
        def order_history(self):
            return None

        def dividend_history(self):
            return None

    assert OfflineClient().sync_history(Database(":memory:")) is None
