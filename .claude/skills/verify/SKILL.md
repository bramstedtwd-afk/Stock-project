---
name: verify
description: Drive StockSage end-to-end offline by seeding its real parquet cache — no network, no code injection.
---

# Verifying StockSage offline

Yahoo/Robinhood are blocked in sandboxes, but the real CLI/dashboard can be
driven end-to-end because `MarketData` serves from its parquet cache before
downloading.

1. **Seed the cache** with synthetic OHLCV for every universe ticker + the
   10 sector ETFs + SPY, for BOTH periods `1y` and `2y` (bootstrap uses 2y):
   write frames (see `tests/conftest.make_ohlcv`) to
   `~/.stocksage/cache/<TICKER>_<period>.parquet`. End the index a few days
   in the past so `drop_partial_bar` is a no-op. Optionally seed
   `~/.stocksage/cache/earnings_dates.json` (`{"CRM": "YYYY-MM-DD"}`) to see
   the earnings blackout.
2. **Point the brain somewhere disposable**: `export STOCKSAGE_DB=/tmp/.../brain.db`.
3. **Drive the CLI**: `python -m stocksage daily` (first run bootstraps,
   ~40s compute), then `suggest`, `profit`, `performance`. To exercise
   grading, backdate a recorded row's `created_at` ~2+ weeks (SQL UPDATE)
   so its 5-trading-bar horizon lies inside the seeded frames, rerun, and
   compare `realized_return`/`benchmark_return` against the parquet closes.
4. **Dashboard boot**: `streamlit run app.py --server.headless true --server.port 8601`,
   curl for HTTP 200. (Full script-path coverage comes from the AppTest
   smoke tests.)

Gotchas: cache TTL is 4h — expired files log noisy yfinance errors then
fall back to stale cache, which is expected offline. News/earnings fetches
each hang for seconds against a blocked proxy on first run; they negative-
cache afterwards.
