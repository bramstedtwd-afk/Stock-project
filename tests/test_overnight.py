"""The overnight edge's forward test: paper only, once per window, costs measured honestly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stocksage import overnight as ov
from stocksage import paper

BUY = datetime(2026, 11, 24, 20, 36, tzinfo=timezone.utc)     # 3:36 PM ET, Tuesday
SELL = datetime(2026, 11, 25, 14, 36, tzinfo=timezone.utc)    # 9:36 AM ET, Wednesday


class FakeAlpaca:
    def __init__(self, number="PA1", is_open=True, rising=True):
        self.number, self.is_open, self.rising = number, is_open, rising
        self.orders, self.sent = {}, []

    def __call__(self, method, url, body):
        if url.endswith("/v2/account"):
            return {"account_number": self.number, "equity": "100000"}
        if url.endswith("/v2/clock"):
            return {"is_open": self.is_open}
        if "/quotes/latest" in url:
            return {"quote": {"bp": 99.99, "ap": 100.01}}
        if "/bars?" in url:
            day0 = datetime(2026, 1, 2)
            closes = [100 + (k if self.rising else -k) * 0.1 for k in range(220)]
            return {"bars": [{"t": (day0 + timedelta(days=k)).strftime("%Y-%m-%dT05:00:00Z"), "c": c}
                             for k, c in enumerate(closes)]}
        if "by_client_order_id" in url:
            cid = url.split("client_order_id=")[1]
            return self.orders.get(cid, {})
        if method == "POST" and url.endswith("/v2/orders"):
            self.sent.append(body)
            fill = 100.01 if body["side"] == "buy" else 100.49
            self.orders[body["client_order_id"]] = {"filled_qty": "99.99", "filled_avg_price": str(fill)}
            return {"status": "accepted"}
        raise AssertionError(url)


def test_buys_in_the_afternoon_and_sells_the_same_shares_next_morning():
    http, book = FakeAlpaca(), {}
    said = ov.step(book, BUY, http=http)
    assert [o["symbol"] for o in http.sent] == ["QQQ", "IWM"] and http.sent[0]["notional"] == "10000"
    assert http.sent[0]["client_order_id"].startswith("ov-") and "bought" in said[0]
    assert not paper.CLIENT_RE.match(http.sent[0]["client_order_id"]), "never graded as a practice idea"
    assert ov.step(book, BUY + timedelta(minutes=1), http=http) == [] and len(http.sent) == 2, "once a day"
    ov.step(book, SELL, http=http)
    sells = [o for o in http.sent if o["side"] == "sell"]
    assert [(o["symbol"], o["qty"]) for o in sells] == [("QQQ", "99.99"), ("IWM", "99.99")]
    ov.step(book, SELL + timedelta(minutes=2), http=http)
    r = ov.report(book)
    assert len(r["nights"]) == 2 and r["nights"][0]["paper"] == pytest.approx(100.49 / 100.01 - 1)
    assert r["spread_bp"] == pytest.approx(1.0, abs=0.01) and "too early" in r["verdict"]
    assert "OVERNIGHT TEST" in "\n".join(ov.lines(r))


def test_out_of_its_uptrend_it_only_records_the_cost():
    http, book = FakeAlpaca(rising=False), {}
    ov.step(book, BUY, http=http)
    assert http.sent == [] and len(book["quotes"]) == 2 and book["nights"][0]["trend"] is False


def test_nothing_outside_the_windows_or_on_a_closed_day():
    http = FakeAlpaca()
    assert ov.step({}, BUY - timedelta(minutes=5), http=http) == [] and http.sent == []
    assert ov.step({}, BUY + timedelta(minutes=20), http=http) == []
    closed = FakeAlpaca(is_open=False)
    assert "closed" in ov.step({}, BUY, http=closed)[0] and closed.sent == []


def test_keys_that_open_a_real_account_send_nothing():
    http = FakeAlpaca(number="5XY")
    with pytest.raises(paper.NotPaper):
        ov.step({}, BUY, http=http)
    assert http.sent == []


def test_the_verdict_waits_and_then_judges_against_the_backtests_lines():
    def book(gap):
        return {"quotes": [{"fund": "QQQ", "at": f"2026-12-{d:02d}T20:35", "bid": 100 - gap, "ask": 100 + gap}
                           for d in range(1, 31)] + [{"fund": "IWM", "at": f"2027-01-{d:02d}T20:35",
                                                       "bid": 100 - gap, "ask": 100 + gap} for d in range(1, 21)]}
    assert "survives" in ov.report(book(0.0005))["verdict"]       # 0.05 bp + fees 0.14 = 0.19
    assert "too costly" in ov.report(book(0.005))["verdict"]      # 0.5 bp + 0.14
    assert "on the edge" in ov.report(book(0.002))["verdict"]
    short = {"quotes": book(0.0005)["quotes"][:10]}
    assert "too early" in ov.report(short)["verdict"]


def test_the_watcher_never_runs_it_without_paper_keys_and_never_lets_it_break_a_check():
    from stocksage import watcher

    assert watcher.overnight_test(BUY) == []

    def broken(*a):
        raise OSError("down")
    assert watcher.overnight_test(BUY, http=broken, book={}) == []
