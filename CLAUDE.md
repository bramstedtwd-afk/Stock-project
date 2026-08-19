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
plain-language errors, and the `.\start.bat` / `start.sh` launcher flow over
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
- `stocksage/advisor.py` — the research packet + Drive publishing for the trading routine (`brief`, `publish`, `log-call`); `stocksage/drive_api.py` — no-install Drive upload/download
- `stocksage/db.py` — SQLite "brain" (default `~/.stocksage/stocksage.db`, `STOCKSAGE_DB` overrides)
- `stocksage/brain.py` — brain export / merging import / cloud-folder sync (merges only ever add)
- `stocksage/data.py` — yfinance with parquet cache + stale fallback
- `stocksage/desktop.py` — app-window/web/phone launch modes + desktop icon install
- `stocksage/autopilot.py` — OS-scheduler registration (launchd/cron/Task Scheduler)
- `stocksage/update.py` — safe self-update (ff-only, refuses dirty tree)
- `stocksage/doctor.py` — 10-point self-diagnosis with fixes
- `stocksage/security.py` — broker access log (fresh sign-in vs reused token) + exposure audit; answers "was that Robinhood sign-in alert us?"
- `stocksage/cli.py` — all terminal commands; `stocksage/envfile.py` — .env load/save

## Commands (via ./start.sh or .\start.bat)

`(none)`=app window · `web` · `phone` · `install` · `autopilot [off|status]` ·
`update` · `doctor` · `daily` · `suggest [TICKERS]` · `sectors` · `portfolio` ·
`moves` · `performance` · `profit` · `watch add|remove|list` ·
`congress` · `bootstrap` · `brain export|import|sync|info` ·
`security [--signin TIME]`

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

- Tests: `python -m pytest` — 317 tests, **fully offline** (synthetic OHLCV via `tests/conftest.make_ohlcv`, in-memory DBs, `FakeMarket` injection, Streamlit AppTest for the dashboard). Keep it that way: no test may need network.
- Lint: `ruff check stocksage/ app.py tests/` must stay clean.
- Push to `main` (the repo's default branch). The repository is **public** —
  it doubles as a portfolio piece, so nothing personal may enter a tracked
  file. `tests/test_privacy.py` enforces that; read it before adding fixtures.
- Signal names are stable identifiers (learned weights key on them) — renaming one resets its learned weight.
- DB schema changes must be additive (`CREATE TABLE IF NOT EXISTS`) — existing brains migrate automatically; brain merges must tolerate older-schema files.
- Credentials live only in `.env` (gitignored, 0600). Never move them into the brain, logs, or commits.
- The broker access log (`~/.stocksage/access.log`) is deliberately **not** in
  the brain: brains get exported, merged, committed as a repo snapshot and
  uploaded to Drive, and a real account's sign-in history must not ride along.
  It records timestamps and command names only — never a credential.

## ⚠️ On Windows the launcher is `.\start.bat`, never `start.bat`

PowerShell does not run programs from the current directory — it refuses
`start.bat update` with CommandNotFoundException even while standing in the
project folder, and helpfully points at `.\start.bat` in a suggestion the
owner should never have needed to read. Every Windows command in chat, in an
error string, in README/ROUTINE, must carry the `.\` prefix. In Python
sources write it `".\\start.bat"` so the escape stays valid.
`tests/test_owner_commands.py` fails the build on a bare one.

## ⚠️ Never hand the owner a bare `stocksage …` command

`stocksage` is not on PATH — it lives only inside the project's virtualenv.
Every command given to the owner (in chat, in an error message, in
ROUTINE.md) must use the launcher and assume a fresh terminal:
`cd C:\Users\<you>\Stock-project` then `.\start.bat <cmd>` on Windows,
`./start.sh <cmd>` elsewhere. A bare `stocksage publish-drive` was shipped
inside a user-facing error string and failed with CommandNotFoundException
the moment they tried it.

## ⚠️ Always `git fetch` before reasoning about branch state

A previous session read `origin/...` refs cached at container-clone time,
concluded the default branch was a month stale, and sent the owner into a
merge that conflicted in four files and left conflict markers in
`engine.py` — which broke every command with an `IndentationError` until
`git merge --abort`. The default branch moves fast (Playbook v6→v9, Drive
publishing, Congress, dollar sizing all landed inside one month). **Fetch
first, then look.** Never hand the owner a git command you have not
verified against freshly-fetched refs.

## Current state & first jobs on the owner's machine (2026-07-11)

Everything was built in a cloud sandbox whose proxy **blocked live Yahoo
Finance and Robinhood traffic** — those paths are written against
documented behavior and covered by offline tests, but have never run live.
So, in order:

1. `.\start.bat update` (get latest), then `.\start.bat doctor` — fix whatever it flags.
2. Confirm the first real daily cycle: bootstrap completes (~1-2 min), briefing populates with real suggestions. Watch `~/.stocksage/server.log` and the terminal for yfinance errors; fix and push fixes.
3. When the owner links Robinhood (they type credentials into the Portfolio tab themselves — never ask for them in chat), verify holdings, the history mirror, and the insights panel against what their Robinhood app shows.
4. Offer: `.\start.bat install` (desktop icon) and `.\start.bat autopilot` (weekday auto-learning; requires machine awake at 17:30).

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

## Merged 2026-08-10 — what survived reconciliation

Both branches independently built earnings gating, health reporting, and
brief publishing. **The default branch's versions won everywhere they
overlapped** (its earnings gate takes an explicit `earnings_days` param and
also warns on names already held into a print; `advisor.py` already
publishes the brief to Drive). A duplicate `stocksage/brief.py` and a second
`cmd_brief` were deleted — the second definition was silently shadowing
`advisor.build_brief`.

Kept from the feature branch, none of which existed on default:

- **Market-relative grading** — outcomes read the close exactly
  `horizon_days` trading bars after the call, and the Hedge update uses the
  SPY-excess return (`suggestions.benchmark_return`, additive column;
  bootstrap warmup trains the same way). The ledger keeps raw returns.
  Without this the model is taught to look brilliant in any rising market.
- **`profit.edge_vs_market`** — the ledger against holding SPY instead.
- **Completed bars only** (`data.drop_partial_bar`) + `MarketData.prefetch`.
- **`relative_strength_20d`** (vs sector ETF, `compute_features(df,
  benchmark_df=...)`), `scoring.why_sentence`, `scoring.apply_sector_caps`.
- **Earnings answers cached on disk**, including the "no date" answer, and
  consulted only for buy candidates and held names (~15 lookups/scan, not
  ~110). Uncached this turned a 10-second scan into an 11-minute one.

## Roadmap the owner has seen (build on request)

Notifications for the briefing after autopilot runs → full
correlation-aware sizing (cross-sector) → optional LLM summaries of move
context. Auto-trading stays off the table; the broker link is read-only by
design.
