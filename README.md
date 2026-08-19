# 📈 StockSage

A personal, continuously-learning stock market intelligence engine. It scans
the **top 10 sectors × their top 10 stocks**, scores every name with an
**adaptive signal model that learns from its own graded track record**,
remembers **why** big moves happened, and produces **portfolio-aware daily
buy/sell suggestions** through a Streamlit dashboard and a CLI — with a
**read-only** Robinhood link.

> **Decision support, not financial advice.** StockSage suggests; you
> decide. It deliberately cannot place trades.

**Stack:** Python · SQLite · yfinance · pandas · Streamlit · robin_stocks ·
scheduled pipeline (launchd / cron / Task Scheduler) · pytest (345 tests,
fully offline)

**Setup, device transfer, autopilot and troubleshooting live in
[SETUP.md](SETUP.md).** To run it right now: `./start.sh` (Windows:
`.\start.bat`).

---

## How it works

```
┌────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐
│ Market data │→ │ Signal engine │→ │ Adaptive score │→ │ Suggestions  │
│ (yfinance,  │  │ trend/momentum│  │ learned weights│  │ risk-sized,  │
│  cached)    │  │ RSI/MACD/vol… │  │ (Hedge algo)   │  │ stop levels  │
└────────────┘   └──────────────┘   └───────┬───────┘   └──────┬───────┘
                                            │                   │
                    ┌───────────────┐       │            ┌──────▼───────┐
                    │ Move context  │       │            │  Robinhood   │
                    │ news + reason │   ┌───▼────────┐   │  (read-only) │
                    │ tagging       │→  │  SQLite DB │ ← │  holdings    │
                    └───────────────┘   └───┬────────┘   └──────────────┘
                                            │
                                 outcomes graded after 5 days
                                 → weights updated → smarter tomorrow
```


The pipeline runs end to end on a schedule: **source** (yfinance, with a
parquet cache and stale-fallback) → **transform** (technical signals, each
normalized to [-1, 1]) → **score** (learned weights) → **persist** (SQLite)
→ **grade** (outcomes read back at a fixed horizon) → **re-weight**. The
last two steps are what make it a learning system rather than a screener.

### It arrives already educated

The very first daily cycle **bootstraps from two years of history** before
doing anything else:

- **Signal weights are pre-trained** on thousands of walk-forward samples:
  at each historical point the engine computes the signals exactly as it
  would have seen them live (no look-ahead), observes the 5-day return that
  followed, and applies the same learning update it uses in production. Your
  day-one suggestions already reflect which signals have actually worked.
- **The move memory is backfilled**: every significant historical move for
  all 100 stocks is recorded, so "why it moved" starts with each name's
  event history instead of an empty page.

Re-run it anytime with `./start.sh bootstrap` to train further on the
freshest history.

It keeps learning by itself — on open, and on autopilot via
your OS scheduler. See [SETUP.md](SETUP.md#running-it-unattended-autopilot).

### The learning loop (the point of the whole tool)

1. Every actionable suggestion is **recorded** with its full signal breakdown.
2. Five trading days later, the outcome is **graded** at exactly that
   horizon — the price five *trading* bars after the call, not whatever the
   price happens to be when the app next runs. Grading runs on every
   touchpoint — daily cycle, dashboard open, even a quick `suggest` — so no
   matured call waits.
3. The weights learn from the **market-relative** result: the call's return
   minus SPY's return over the same window. A +2% week when the whole market
   rose 3% is a losing call — grading raw returns would just teach the model
   to always be bullish in bull markets (and the first-run bootstrap trains
   the same way). The raw dollar result still goes to the profit ledger,
   because that's real money.
4. Weights update via the **multiplicative-weights (Hedge)** algorithm, at
   two levels: the **global** vector, and a **per-sector** vector (what works
   in Energy isn't what works in Tech). A sector's own weights start voting —
   blended 50/50 with the global view — once it has 10 graded calls of its
   own. A weight floor keeps every signal alive so the model can re-adapt
   when regimes change.
5. The weights are fully **inspectable** (`performance` command / Learning
   tab) — you can always see what the tool currently believes works. No black
   box.

### The brain talks back: past context shapes every suggestion

Before scoring a name, the model consults what the brain remembers about it,
and each adjustment is spelled out in the suggestion's notes:

- **Its own record on that name** — after 5+ graded calls, a name the model
  keeps reading correctly earns up to +25% conviction; one it keeps
  misreading loses up to 40%. The model literally knows which stocks it
  understands.
- **Recent shocks** — a significant move within the last 5 days (with its
  tagged reason from the move memory) tempers conviction by 25% while the
  dust settles.
- **Event-proneness** — names with 10+ outsized moves in the past year get
  smaller suggested positions; jumpy names deserve smaller bets.
- **Earnings blackout** — a buy signal within 5 days of a scheduled earnings
  report keeps its read but gets **zero suggested size**: a new entry right
  before a print is a bet on the report, not on the setup. The card says so
  and the name can be re-judged after the report.

Every card also opens with a **plain-English "why"** — the two or three
signals pulling hardest ("a solid long-term uptrend, strong momentum this
month and beating its own sector lately"), plus the strongest signal leaning
the other way when there is one. The tension is part of the truth.

### Why-it-moved memory

Any day a stock moves more than 2.5% **and** 2× its own average true range,
StockSage pulls the freshest headlines, tags them against a reason taxonomy
(earnings, guidance, analyst ratings, Fed/macro, M&A, legal, product news,
management, dividends, insider activity), and files the event. Over months
this becomes your private research notebook of what actually drives each name.

### Safety rails (growth *with* seatbelts)

- **Risk-adjusted scoring** — high-volatility names need a much stronger raw
  signal to earn a BUY.
- **Position sizing** — suggested size grows with conviction, shrinks with
  volatility, and is hard-capped at 10% of investable cash per idea. With
  Robinhood linked, sizes are also shown in **real dollars** from your
  actual buying power.
- **Sector concentration guard** — past the second buy idea in the same
  sector on the same day, suggested sizes are halved; five copies of the
  same bet is one bet in disguise.
- **Completed bars only** — a scan during market hours ignores the
  in-progress session, so signals are never judged on half a day's price
  and volume.
- **Protective stops** — every buy suggestion includes a 2×ATR stop level.
- **Read-only broker link** — architecture prevents auto-trading; your
  Robinhood credentials live only in your local `.env` on that one machine
  (gitignored, `0600`) — never in the brain, the repo, or Drive. See
  [Security model](#security-model).
- **Honest self-grading** — the hit rate on the Learning tab is computed from
  real recorded calls, not backtests.

### Your Robinhood account, fully mirrored

Link once from the Portfolio tab form. The broker session is cached in
`~/.tokens/robinhood.pickle` and reused until it expires, so most runs
reconnect without a new sign-in; storing your TOTP seed additionally lets
unattended scheduled runs re-authenticate on their own when it does expire
(read the tradeoff in [Security model](#security-model) before you do). From then on, **every
daily cycle silently mirrors your complete account history into the brain**:
all filled orders and all dividends, incrementally and idempotently — only
new activity is added, no matter how often it runs.

The Portfolio tab then shows **what your history says**, computed locally:

- **Realized P&L** per name and overall, FIFO lot-matched (the same
  convention your broker and the IRS use)
- **Your** win rate across completed round trips, and your real average
  holding time
- **Dividends collected**, your best and costliest names
- **Model agreement** — how often your trades matched the model's standing
  call at the time, with every disagreement listed. Over time this is the
  most interesting number in the app: it tells you whose judgment to trust,
  yours or the model's, situation by situation.

Your **Robinhood watchlists** are scanned too: any name you star in the
Robinhood app is automatically part of the daily scan, alongside the
built-in universe and StockSage's own watchlist — no retyping.

Because the mirror lives in the brain, your trading history and its
insights travel to all your devices with the shared brain — while the
credentials themselves never do.

### The profit meter

The **💰 Profit tab** (and `./start.sh profit`) scores every graded call as
a fixed-stake paper trade ($1,000 per idea; set `STOCKSAGE_STAKE` in `.env`
to change): cumulative P&L curve, win rate, profit factor (gross wins ÷
gross losses), best and worst calls, and the risk its sell/avoid calls
saved you. Fixed staking is deliberate — it measures the quality of the
calls themselves, uncontaminated by sizing luck. **This is the number to
watch before trusting the tool with real size**, and because it's derived
from the graded record, it travels with the brain.

It also answers the question every honest meter must face: **would the same
money have done better just sitting in SPY?** Every graded call stores the
market's return over the same window, and the Profit tab shows the model's
edge (or deficit) against that do-nothing alternative.

### The published brief (what the trading routine reads)

`./start.sh brief` builds the research packet — candidates with stops,
targets, sizing and earnings status; names to avoid; sector mood; the
model's track record — and `./start.sh publish <folder>` writes it, the
playbook, and the brain snapshot into a Drive-synced folder. That's the
handoff between StockSage (which learns) and the routine that acts on it.

## Security model

What is stored, and where — stated precisely, because a vague version of
this is worse than none.

| Thing | Where it lives | Travels? |
|---|---|---|
| Robinhood username / password | `.env` on that machine, `0600`, gitignored | Never |
| TOTP seed (optional) | same `.env`, if you choose to store it | Never |
| Broker session token | `~/.tokens/robinhood.pickle`, owner-only | Never |
| Google Drive OAuth token | `~/.stocksage/drive_token.json` | Only if you move it deliberately |
| Broker access log | `~/.stocksage/access.log` | Never |
| The brain (weights, move memory, calls) | `~/.stocksage/stocksage.db` | Yes — that's the point |

Credentials are written to disk, on that one machine, and nowhere else.
They are **never** part of the brain, so they do not travel when the brain
syncs between your devices, publishes to Drive, or is committed as a repo
snapshot. Link Robinhood separately on each device.

- **The broker link is read-only by architecture.** There is no order-placing
  code path to disable.
- **The repo snapshot is scrubbed.** `brain/brain-snapshot.db` is committed to
  this public repository, and the brain mirrors real Robinhood fills — so
  `brain snapshot` strips the account mirror and owner-sourced calls, then
  `VACUUM`s so deleted rows don't survive in the file's free pages. Knowledge
  travels; the trade history does not.
- **Storing the TOTP seed is a real tradeoff, not a free win.** Beside the
  password in the same file, it means one file compromise yields both
  factors. It buys unattended scheduled runs. `security` reports which way
  yours is set; `.env.example` states the tradeoff at the point of decision.
- **Every broker session is logged** — `security` shows when StockSage opened
  your account and whether it was a fresh sign-in (which Robinhood alerts on)
  or a silent token reuse, so an unexpected alert is answerable instead of
  guessed at.

## The universe

10 sectors, 10 leaders each (100 stocks) plus SPDR sector ETFs for trend:
Technology, Healthcare, Financials, Consumer Discretionary, Communication
Services, Industrials, Consumer Staples, Energy, Utilities, Real Estate.
Edit `stocksage/universe.py` to change it — nothing else hardcodes tickers.

## Testing

```bash
.venv/bin/python -m pytest        # 345 tests, fully offline
```

No test touches the network: synthetic OHLCV fixtures, in-memory databases,
an injected `FakeMarket`, and Streamlit's `AppTest` for the dashboard. The
suite includes an adversarial layer (`tests/test_edge_cases.py`) covering
splits, delistings, clock skew, corrupted brains, future-schema merges and a
simulated 600-call year — the failure modes that would quietly corrupt the
learning system rather than crash it.

Something not right? `./start.sh doctor` checks nine things and prints a
plain-language fix for each. See [SETUP.md](SETUP.md).

## Roadmap (evolves with use)

- [x] Earnings-calendar awareness (don't suggest entries into a print)
- [x] Per-sector learned weights (what works in Energy ≠ Tech)
- [x] Plain-English "why" sentence on every suggestion card
- [x] Sector concentration guard (first cut of correlation-aware sizing)
- [x] Paper ledger measured against SPY (the do-nothing alternative)
- [ ] Full correlation-aware portfolio sizing (cross-sector correlations)
- [ ] Notifications when the autopilot briefing is ready
- [ ] Optional LLM summarization of move context into plain-English narratives
