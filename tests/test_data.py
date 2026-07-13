"""Data-layer behavior that matters for signal integrity — all offline."""

import json
import time
from datetime import datetime

import pandas as pd

from stocksage.data import MARKET_TZ, MarketData, drop_partial_bar
from tests.conftest import make_ohlcv


def test_drop_partial_bar_removes_todays_bar_while_market_open():
    df = make_ohlcv(days=50)
    # Pretend "now" is 11am ET on the frame's final bar date.
    last_day = df.index[-1]
    now = datetime(last_day.year, last_day.month, last_day.day, 11, 0, tzinfo=MARKET_TZ)
    trimmed = drop_partial_bar(df, now=now)
    assert len(trimmed) == len(df) - 1
    assert trimmed.index[-1] == df.index[-2]


def test_drop_partial_bar_keeps_completed_bars():
    df = make_ohlcv(days=50)
    last_day = df.index[-1]
    after_close = datetime(last_day.year, last_day.month, last_day.day, 17, 0, tzinfo=MARKET_TZ)
    assert len(drop_partial_bar(df, now=after_close)) == len(df)
    next_day = datetime(last_day.year, last_day.month, last_day.day + 1 if last_day.day < 28 else 28, 9, 0, tzinfo=MARKET_TZ)
    # A frame ending on an earlier day is untouched even during market hours.
    if next_day.date() != last_day.date():
        assert len(drop_partial_bar(df, now=next_day)) == len(df)


def test_history_serves_from_cache_without_network(tmp_path):
    md = MarketData(cache_dir=tmp_path)
    df = make_ohlcv(days=80)
    md._write_cache("FAKE", "1y", df)
    out = md.history("FAKE")
    assert out is not None and len(out) >= len(df) - 1  # at most the partial bar dropped
    pd.testing.assert_frame_equal(out, drop_partial_bar(df), check_freq=False)


def test_prefetch_skips_fresh_cache(tmp_path):
    md = MarketData(cache_dir=tmp_path)
    md._write_cache("AAA", "1y", make_ohlcv(days=80))
    md._write_cache("BBB", "1y", make_ohlcv(days=80, seed=9))
    # Everything cached -> no download attempted, zero fetched.
    assert md.prefetch(["AAA", "BBB"]) == 0


def test_next_earnings_date_uses_disk_cache(tmp_path):
    md = MarketData(cache_dir=tmp_path)
    cache_file = tmp_path / "earnings_dates.json"
    cache_file.write_text(json.dumps({"AAPL": "2026-07-30", "NOPE": ""}))
    # Fresh mtime -> served from cache, no network touched.
    assert time.time() - cache_file.stat().st_mtime < 60
    assert md.next_earnings_date("AAPL") == "2026-07-30"
    assert md.next_earnings_date("NOPE") is None
