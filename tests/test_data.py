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


def test_earnings_date_uses_disk_cache(tmp_path):
    md = MarketData(cache_dir=tmp_path)
    now = time.time()
    # The empty string is a cached "no date found" — it must be honored as an
    # answer, not treated as a miss, or every scan re-pays the network cost.
    (tmp_path / "earnings_dates.json").write_text(
        json.dumps({"AAPL": ["2026-07-30", now], "NOPE": ["", now]})
    )
    assert md.earnings_date("AAPL") == "2026-07-30"
    assert md.earnings_date("NOPE") is None


def test_earnings_misses_expire_far_sooner_than_hits(tmp_path):
    """A 'no date' answer is usually an unreachable source. Caching that for
    days would keep the earnings blackout switched off long after a fix."""
    from stocksage.data import EARNINGS_CACHE_TTL_SECONDS, EARNINGS_MISS_TTL_SECONDS

    assert EARNINGS_MISS_TTL_SECONDS < EARNINGS_CACHE_TTL_SECONDS
    md = MarketData(cache_dir=tmp_path)
    now = time.time()
    aged = now - (EARNINGS_MISS_TTL_SECONDS + 60)  # older than a miss may live
    (tmp_path / "earnings_dates.json").write_text(
        json.dumps({"HIT": ["2026-09-30", aged], "MISS": ["", aged]})
    )
    cache = md._earnings_cache(now=now)
    assert "HIT" in cache      # a found date is still good at this age
    assert "MISS" not in cache  # the failure is retried instead


def test_earnings_cache_tolerates_the_older_flat_format(tmp_path):
    """Brains written before per-entry timestamps must still be readable."""
    md = MarketData(cache_dir=tmp_path)
    (tmp_path / "earnings_dates.json").write_text(json.dumps({"AAPL": "2026-07-30"}))
    assert md.earnings_date("AAPL") == "2026-07-30"
