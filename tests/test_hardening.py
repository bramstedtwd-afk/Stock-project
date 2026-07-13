"""Block-3 hardening tests: edge cases found by the error audit."""

from datetime import datetime, timezone


from stocksage import brain
from stocksage.data import MarketData, parse_news_items
from stocksage.db import Database
from stocksage.envfile import load_env
from stocksage.indicators import compute_features
from tests.conftest import make_ohlcv


# --- brain merge carries the Robinhood mirror and sector weights ---

def _brain_with_history(path, order_id, ticker):
    db = Database(path)
    db.upsert_rh_orders(
        [{"order_id": order_id, "ticker": ticker, "side": "buy", "quantity": 1,
          "price": 100.0, "executed_at": "2026-06-01T15:00:00Z"}]
    )
    db.upsert_rh_dividends(
        [{"dividend_id": f"d-{order_id}", "ticker": ticker, "amount": 1.0,
          "paid_at": "2026-06-15"}]
    )
    db.save_sector_weights("Technology", {"macd": 0.6, "trend_long": 0.4})
    db.close()
    return path


def test_merge_carries_rh_mirror_and_sector_weights(tmp_path):
    local = _brain_with_history(tmp_path / "a.db", "o1", "AAPL")
    other = _brain_with_history(tmp_path / "b.db", "o2", "NVDA")
    stats = brain.merge_brains(local, other)
    assert stats["rh_orders_added"] == 1
    assert stats["rh_dividends_added"] == 1
    merged = Database(local)
    assert {r["ticker"] for r in merged.rh_orders()} == {"AAPL", "NVDA"}
    assert merged.load_sector_weights("Technology")  # survived the merge
    merged.close()


def test_merge_from_old_brain_without_rh_tables(tmp_path):
    """A brain exported before the mirror existed must still merge cleanly."""
    import sqlite3

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)  # minimal old-generation schema
    conn.executescript(
        "CREATE TABLE suggestions (id INTEGER PRIMARY KEY, created_at TEXT,"
        " ticker TEXT, action TEXT, score REAL, price REAL, signals TEXT,"
        " horizon_days INTEGER, evaluated INTEGER DEFAULT 0,"
        " realized_return REAL, hit INTEGER);"
        "CREATE TABLE weights (signal TEXT PRIMARY KEY, weight REAL, updated_at TEXT);"
        "CREATE TABLE move_events (id INTEGER PRIMARY KEY, created_at TEXT,"
        " ticker TEXT, event_date TEXT, return_pct REAL, atr_multiple REAL,"
        " reasons TEXT, headlines TEXT, UNIQUE (ticker, event_date));"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    conn.commit()
    conn.close()
    target = tmp_path / "new.db"
    Database(target).close()
    stats = brain.merge_brains(target, old)  # must not raise
    assert "rh_orders_added" not in stats


# --- grading survives malformed rows ---

def test_pending_evaluations_skips_malformed_timestamp():
    db = Database(":memory:")
    good = db.record_suggestion("AAPL", "BUY", 0.4, 100.0, {}, 5)
    bad = db.record_suggestion("NVDA", "BUY", 0.4, 100.0, {}, 5)
    db.conn.execute(
        "UPDATE suggestions SET created_at = 'not-a-date' WHERE id = ?", (bad,)
    )
    db.conn.execute(
        "UPDATE suggestions SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (good,),
    )
    db.conn.commit()
    due = db.pending_evaluations(datetime.now(timezone.utc))
    assert [r["id"] for r in due] == [good]


# --- .env parsing oddities ---

def test_load_env_shell_style_and_junk(tmp_path, monkeypatch):
    monkeypatch.delenv("SHELLY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "export SHELLY=works\n"
        "THIS LINE HAS SPACES=ignored\n"
        "COMPLEX=a=b=c\n"
    )
    loaded = load_env(env)
    assert loaded["SHELLY"] == "works"
    assert "THIS LINE HAS SPACES" not in loaded
    assert loaded["COMPLEX"] == "a=b=c"


# --- price cache corruption ---

def test_corrupt_cache_is_discarded_not_fatal(tmp_path):
    md = MarketData(cache_dir=tmp_path)
    bad = md._cache_path("AAPL", "1y")
    bad.write_bytes(b"not parquet at all")
    assert md._read_cache("AAPL", "1y") is None
    assert not bad.exists()  # poisoned file removed so it can't loop forever


# --- yfinance news schema generations ---

def test_parse_news_old_and_new_schemas():
    raw = [
        {"title": "Old style", "publisher": "Reuters", "link": "http://x",
         "providerPublishTime": 1720000000},
        {"content": {"title": "New style", "provider": {"displayName": "Bloomberg"},
                     "canonicalUrl": {"url": "http://y"}, "pubDate": "2026-07-09"}},
        {"content": "garbage string"},
        "not even a dict",
        {"content": {"title": ""}},  # missing title dropped
    ]
    items = parse_news_items(raw, limit=10)
    assert [i["title"] for i in items] == ["Old style", "New style"]
    assert items[0]["publisher"] == "Reuters"
    assert items[1]["publisher"] == "Bloomberg"
    assert items[1]["link"] == "http://y"


# --- indicators with degenerate volume ---

def test_features_with_zero_volume_column():
    df = make_ohlcv(days=120, seed=77)
    df["Volume"] = 0.0
    feats = compute_features(df)
    assert feats is not None
    assert "volume_confirmation" not in feats  # skipped, not NaN
    for v in feats.values():
        assert -1.0 <= v <= 1.0


def test_features_all_nan_price_returns_none():
    import numpy as np

    df = make_ohlcv(days=120, seed=78)
    df.iloc[-1, df.columns.get_loc("Close")] = np.nan
    assert compute_features(df) is None


def test_extract_watchlist_symbols_tolerates_all_shapes():
    from stocksage.robinhood import extract_watchlist_symbols

    assert extract_watchlist_symbols({"results": [{"symbol": "aapl"}, {"symbol": "MSFT"}]}) == ["AAPL", "MSFT"]
    assert extract_watchlist_symbols([{"object_symbol": "nvda"}]) == ["NVDA"]
    assert extract_watchlist_symbols({"results": [{"no": "symbol"}, "junk", None]}) == []
    assert extract_watchlist_symbols(None) == []
    assert extract_watchlist_symbols("garbage") == []
