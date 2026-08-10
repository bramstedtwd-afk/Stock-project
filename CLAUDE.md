# StockSage — context for Claude sessions working in this repo

## What this is

A personal, continuously-learning stock market intelligence app for its
single owner. It scans the top 10 sectors × top 10 stocks (plus the owner's
watchlist and Robinhood holdings), scores names with an adaptive signal
model that learns from its own graded track record, explains significant
moves, and produces daily buy/sell suggestions through a Streamlit
dashboard and CLI. **It is decision support only — it must never place
trades.** The Robinhood link is deliberately read-only.

The owner is non-technical: prioritize things working out of the box,
plain-language errors, and the `start.bat` / `start.sh` launcher flow over
developer conveniences. When they report a problem, fix it end-to-end
rather than giving them instructions.

## Architecture (one line per module)

- `app.py` — Streamlit dashboard: briefing, suggestions, profit ledger, sectors, portfolio+insights, move memory, learning status; sidebar has brain status/sharing and watchlist
- `stocksage/engine.py` — orchestrator: `daily_run()` = bootstrap-once → grade matured calls → sync Robinhood → scan → suggest
- `stocksage/indicators.py` — technical signals, all normalized to [-1, 1]
- `stocksage/scoring.py` — risk-adjusted suggestion building incl. PastContext (per-ticker reliability, recent shocks, event-proneness)
- `stocksage/learning.py` — Hedge (multiplicative-weights) updates; global + per-sector weight vectors, blended
- `stocksage/context.py` — significant-move detection + news reason tagging (the "why it moved" memory)
- `stocksage/bootstrap.py` — first-run walk-forward pre-training from 2y of history (no look-ahead)
- `stocksage/robinhood.py` — read-only portfolio + full order/dividend history mirror (idempotent sync)
- `stocksage/insights.py` — FIFO round trips, realized P&L, owner win rate, model-agreement analysis
- `stocksage/profit.py` — fixed-stake paper ledger from graded calls (the honesty meter)
- `stocksage/briefing.py` — the morning briefing synthesis
- `stocksage/brief.py` — the published JSON brief the routine agent reads (`headline` = edge vs SPY, `health.degraded` = what was broken); schema is versioned + tested here, not hand-rolled on the desktop
- `stocksage/db.py` — SQLite "brain" (default `~/.stocksage/stocksage.db`, `STOCKSAGE_DB` overrides)
- `stocksage/brain.py` — brain export / merging import / cloud-folder sync (merges only ever add)
- `stocksage/data.py` — yfinance with parquet cache + stale fallback
- `stocksage/desktop.py` — app-window/web/phone launch modes + desktop icon install
- `stocksage/autopilot.py` — OS-scheduler registration (launchd/cron/Task Scheduler)
- `stocksage/update.py` — safe self-update (ff-only, refuses dirty tree)
- `stocksage/doctor.py` — 8-point self-diagnosis with fixes
- `stocksage/cli.py` — all terminal commands; `stocksage/envfile.py` — .env load/save

## Commands (via ./start.sh or start.bat)

`(none)`=app window · `web` · `phone` · `install` · `autopilot [off|status]` ·
`update` · `doctor` · `daily` · `suggest [TICKERS]` · `sectors` · `portfolio` ·
`moves` · `performance` · `profit` · `brief [--out PATH]` · `watch add|remove|list` ·
`bootstrap` · `brain export|import|sync|info`

## Development conventions

- Tests: `python -m pytest` — 141 tests, **fully offline** (synthetic OHLCV via `tests/conftest.make_ohlcv`, in-memory DBs, `FakeMarket` injection, Streamlit AppTest for the dashboard). Keep it that way: no test may need network.
- Lint: `ruff check stocksage/ app.py tests/` must stay clean.
- Push to branch `claude/stock-trend-analyzer-robinhood-67iqou` (the repo's only/default branch).
- Signal names are stable identifiers (learned weights key on them) — renaming one resets its learned weight.
- DB schema changes must be additive (`CREATE TABLE IF NOT EXISTS`) — existing brains migrate automatically; brain merges must tolerate older-schema files.
- Credentials live only in `.env` (gitignored, 0600). Never move them into the brain, logs, or commits.

## ⚠️ Unmerged work — read this first (as of 2026-08-10)

**This branch (`claude/stock-analyzer-features-x2q2o3`) has never been merged
into the default branch `claude/stock-trend-analyzer-robinhood-67iqou`.** The
default branch is still at `d60e448` (2026-07-11), so the owner's desktop —
and therefore the published brief and the routine agent that reads it — has
been running the pre-7/13 build for a month. Everything under "Recently
built" and "Brief publishing" below is live only here.

Consequences visible in the wild: the published `StockSage Brief.json` shows
`earnings_days: null` on every candidate (the blackout code is not in the
build the desktop runs) and no edge-vs-SPY figure in `model_stats`. Merging
is the precondition for any of it mattering. Do not merge without the
owner's explicit go-ahead.

## Current state & first jobs on the owner's machine (2026-07-11)

Everything was built in a cloud sandbox whose proxy **blocked live Yahoo
Finance and Robinhood traffic** — those paths are written against
documented behavior and covered by offline tests, but have never run live.
So, in order:

1. `start.bat update` (get latest), then `start.bat doctor` — fix whatever it flags.
2. Confirm the first real daily cycle: bootstrap completes (~1-2 min), briefing populates with real suggestions. Watch `~/.stocksage/server.log` and the terminal for yfinance errors; fix and push fixes.
3. When the owner links Robinhood (they type credentials into the Portfolio tab themselves — never ask for them in chat), verify holdings, the history mirror, and the insights panel against what their Robinhood app shows.
4. Offer: `start.bat install` (desktop icon) and `start.bat autopilot` (weekday auto-learning; requires machine awake at 17:30).

## Recently built (2026-07-11 session)

- Grading is now **market-relative at the true horizon**: outcomes read the
  close exactly `horizon_days` trading bars after the call (latest-price
  fallback only when history can't resolve it), and the Hedge update uses
  the SPY-excess return (`suggestions.benchmark_return`, additive column;
  bootstrap warmup trains the same way). The ledger keeps raw returns.
- Scans use **completed bars only** (`data.drop_partial_bar`), batch-download
  via `MarketData.prefetch`, and know the **earnings calendar**
  (`next_earnings_date`, disk-cached): a buy within 5 days of a print keeps
  its read but gets zero suggested size.
- New signal `relative_strength_20d` (vs sector ETF benchmark, passed as
  `compute_features(df, benchmark_df=...)`).
- `scoring.why_sentence` puts a plain-English "why" on every card;
  `apply_sector_caps` halves sizes past 2 buy ideas per sector.
- Robinhood (still strictly read-only): app watchlists are scanned too
  (`watchlist_tickers`), and suggestion sizes show real dollars from
  buying power. Profit tab/briefing show the edge vs parking the same
  stakes in SPY.

## Brief publishing (2026-08-10 session)

- `stocksage brief --out <path>` publishes the JSON the routine agent reads.
  The schema lives in `stocksage/brief.py` (`SCHEMA_VERSION`), is additive,
  and is covered by `tests/test_brief.py` — previously it was hand-rolled in
  an unseen desktop script, which is how it drifted from the repo.
- Two fields exist so the owner is never misled: `headline` (edge over SPY,
  in one sentence — never raw P&L, which flatters in a rising market) and
  `health.degraded` (plain-English list of what was broken during the run,
  e.g. earnings calendar down, broker unlinked). The agent leads with both.
- The earnings calendar is now consulted **only for buy candidates** (~10
  lookups/scan instead of ~110). On a machine where the calendar endpoint is
  slow or blocked this is the difference between a usable and an unusable scan.
- `docs/agent-playbook-amendments.md` holds proposed playbook v8 rules
  (notification budget, concentration measured on equity not buying power,
  splitting the risk check from the research sweep, one rolling log). The
  Drive playbook must be edited **in place** — a second playbook document
  reintroduces the v5/v6 loader bug.

## Roadmap the owner has seen (build on request)

Notifications for the briefing after autopilot runs → full
correlation-aware sizing (cross-sector) → optional LLM summaries of move
context. Auto-trading stays off the table; the broker link is read-only by
design.
