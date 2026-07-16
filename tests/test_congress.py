from datetime import date, timedelta

from stocksage import congress
from stocksage.advisor import build_brief
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


def test_parse_amount_range():
    assert congress.parse_amount_range("$1,001 - $15,000") == 8000.5
    assert congress.parse_amount_range("$50,000") == 50000.0
    assert congress.parse_amount_range(None) == 0.0
    assert congress.parse_amount_range("garbage") == 0.0


def test_to_iso_and_type():
    assert congress._to_iso("2026-07-01") == "2026-07-01"
    assert congress._to_iso("07/01/2026") == "2026-07-01"
    assert congress._to_iso("nonsense") == ""
    assert congress.normalize_type("Purchase") == "buy"
    assert congress.normalize_type("Sale (Full)") == "sell"
    assert congress.normalize_type("exchange") is None


def test_normalize_filters_junk():
    raw = [
        {"ticker": "nvda", "type": "purchase", "transaction_date": "2026-07-01",
         "representative": "A", "amount": "$1,001 - $15,000"},
        {"ticker": "--", "type": "purchase", "transaction_date": "2026-07-01"},  # no ticker
        {"ticker": "AAPL", "type": "exchange", "transaction_date": "2026-07-01"},  # not buy/sell
        "not a dict",
    ]
    out = congress.normalize(raw)
    assert len(out) == 1
    assert out[0]["ticker"] == "NVDA" and out[0]["type"] == "buy"


def test_summarize_and_notable():
    today = date(2026, 7, 15)
    recent = today.isoformat()
    old = (today - timedelta(days=200)).isoformat()
    txns = [
        {"ticker": "NVDA", "type": "buy", "date": recent, "member": "A", "amount": 8000},
        {"ticker": "NVDA", "type": "buy", "date": recent, "member": "B", "amount": 20000},
        {"ticker": "NVDA", "type": "buy", "date": recent, "member": "A", "amount": 5000},
        {"ticker": "XOM", "type": "buy", "date": recent, "member": "C", "amount": 3000},
        {"ticker": "XOM", "type": "sell", "date": recent, "member": "C", "amount": 3000},
        {"ticker": "OLD", "type": "buy", "date": old, "member": "D", "amount": 9000},
    ]
    summary = congress.summarize(txns, today=today)
    assert summary["NVDA"]["buys"] == 3
    assert summary["NVDA"]["members"] == 2  # A counted once
    assert summary["NVDA"]["net_buys"] == 3
    assert "OLD" not in summary  # outside the lookback window
    # XOM net zero -> not notable; NVDA is.
    assert congress.notable_buys(summary) == ["NVDA"]


def test_summary_defensive_on_bad_data(monkeypatch, tmp_path):
    cd = congress.CongressData(cache_dir=tmp_path)
    monkeypatch.setattr(cd, "_fetch", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))
    # _fetch raising is caught inside; transactions returns [] -> summary {}
    monkeypatch.setattr(cd, "transactions", lambda: [])
    assert cd.summary() == {}


def test_dead_source_retried_once_per_ttl_not_every_call(tmp_path):
    """Regression: a permanently unreachable feed (e.g. a dead S3 bucket)
    must be retried at most once per TTL window, not on every single call —
    the old behavior only cached successes, so a persistently dead source
    hit the network (and logged a warning) on every scan, forever."""
    cd = congress.CongressData(cache_dir=tmp_path)
    calls = []

    def failing_fetch(url):
        calls.append(url)
        return []  # simulates every real 403/network failure path

    cd._fetch = failing_fetch
    cd.transactions()
    cd.transactions()
    cd.transactions()
    # Two URLs (house + senate) hit once, then served from the empty-result
    # cache for every subsequent call within the TTL window.
    assert len(calls) == 2


def test_brief_includes_congress_context(monkeypatch):
    from stocksage import universe

    frames = {"NVDA": make_ohlcv(days=300, start_price=120.0, daily_drift=0.004, seed=91)}
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    engine = Engine(db=Database(":memory:"), market=FakeMarket(frames))
    summary = {"NVDA": {"buys": 3, "sells": 0, "net_buys": 3, "members": 2,
                        "est_amount": 30000, "last_date": "2026-07-10"}}
    brief = build_brief(engine, tickers=["NVDA"], congress_summary=summary)
    assert brief["congress_watch"][0]["ticker"] == "NVDA"
    assert brief["focus"][0]["congress_buying"]["members"] == 2


def test_brief_no_congress_when_empty(monkeypatch):
    from stocksage import universe

    frames = {"NVDA": make_ohlcv(days=300, daily_drift=0.004, seed=92)}
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    engine = Engine(db=Database(":memory:"), market=FakeMarket(frames))
    brief = build_brief(engine, tickers=["NVDA"], congress_summary={})
    assert brief["congress_watch"] == []
    assert brief["focus"][0]["congress_buying"] is None
