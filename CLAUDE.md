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
`moves` · `performance` · `profit` · `watch add|remove|list` ·
`bootstrap` · `brain export|import|sync|info`

## Trading-routine integration (stocksage/advisor.py)

The owner runs a separate scheduled Claude routine that day-trades a small
agentic Robinhood account (human confirm on every order — that gate is
sacred, never automate order placement). **That routine runs in a
connector-only environment (Robinhood + Google Drive, no terminal/repo)**,
so it cannot run Python or read the private repo. The bridge is Google
Drive: the desktop publishes research there, the routine reads it there.
`ROUTINE.md` is the full playbook (canonical here; a synced copy lives in
the owner's Drive as the doc "StockSage Routine Playbook").

Desktop-side commands (Path B, full environment):
- `python -m stocksage brief [TICKERS...] --max-price N --json` — research
  packet: grades matured calls first, then market mood, verdicts + ATR-based
  2:1 stop/target, ranked candidates under the cap, avoid list, per-ticker
  reliability, recent shocks, model stats. Degrades to brain-only context
  when market data is unreachable.
- `python -m stocksage log-call TICKER ACTION [--price P] [--note ...]` —
  records a decision so it's graded at the 5-day horizon.
- `python -m stocksage publish <drive-folder> [TICKERS...] [--max-price N]`
  — writes a dated "StockSage Brief" JSON, the playbook, and the brain
  snapshot into a Google-Drive-synced folder for the routine to read. Run
  it daily (e.g. after autopilot). No Google API creds needed — it uses the
  Drive-for-Desktop synced-folder mechanism, same as `brain sync`.

The routine (Path A) reads the freshest "StockSage Brief" from Drive and
logs decisions to the Drive trading log; the desktop reconciles/grades those
later. If Drive research is stale/absent, the routine proceeds on its own
live technical analysis (degraded run, never a halt).

## Development conventions

- Tests: `python -m pytest` — 121 tests, **fully offline** (synthetic OHLCV via `tests/conftest.make_ohlcv`, in-memory DBs, `FakeMarket` injection, Streamlit AppTest for the dashboard). Keep it that way: no test may need network.
- Lint: `ruff check stocksage/ app.py tests/` must stay clean.
- Push to branch `claude/stock-trend-analyzer-robinhood-67iqou` (the repo's only/default branch).
- Signal names are stable identifiers (learned weights key on them) — renaming one resets its learned weight.
- DB schema changes must be additive (`CREATE TABLE IF NOT EXISTS`) — existing brains migrate automatically; brain merges must tolerate older-schema files.
- Credentials live only in `.env` (gitignored, 0600). Never move them into the brain, logs, or commits.

## Current state & first jobs on the owner's machine (2026-07-11)

Everything was built in a cloud sandbox whose proxy **blocked live Yahoo
Finance and Robinhood traffic** — those paths are written against
documented behavior and covered by offline tests, but have never run live.
So, in order:

1. `start.bat update` (get latest), then `start.bat doctor` — fix whatever it flags.
2. Confirm the first real daily cycle: bootstrap completes (~1-2 min), briefing populates with real suggestions. Watch `~/.stocksage/server.log` and the terminal for yfinance errors; fix and push fixes.
3. When the owner links Robinhood (they type credentials into the Portfolio tab themselves — never ask for them in chat), verify holdings, the history mirror, and the insights panel against what their Robinhood app shows.
4. Offer: `start.bat install` (desktop icon) and `start.bat autopilot` (weekday auto-learning; requires machine awake at 17:30).

## Roadmap the owner has seen (build on request)

Notifications for the briefing after autopilot runs → earnings-calendar
awareness (no entries right before a print) → plain-English "why" sentence
per suggestion card → correlation-aware sizing → paper-trading ledger
before any talk of automation.
