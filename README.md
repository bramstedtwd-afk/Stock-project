# 📈 StockSage

A personal, continuously-learning stock market intelligence engine. It scans
the **top 10 industries × their top 10 stocks**, scores every name with an
**adaptive signal model that learns from its own track record**, remembers
**why** big moves happened, and gives you **portfolio-aware daily buy/sell
suggestions** through a friendly dashboard — with your **Robinhood** account
linked read-only.

> **This is decision support, not financial advice.** StockSage suggests;
> you decide. It deliberately cannot place trades.

---

## Quick start — one command

**Mac / Linux:**

```bash
./start.sh
```

**Windows:**

```bat
start.bat
```

That's it. The first run sets up everything automatically (virtual
environment, dependencies, settings file — allow a few minutes), then
**StockSage opens in its own app window** — no browser tabs, no terminal
juggling. Close the window and everything shuts down cleanly. Press
**Run daily cycle** to get your first suggestions, and link Robinhood right
from the **Portfolio tab** — no file editing needed.

The only prerequisite is [Python 3.10+](https://www.python.org/downloads/)
(on Windows, tick *"Add python.exe to PATH"* during install).

### Install it like a real app (recommended)

```bash
./start.sh install        # Windows: start.bat install
```

- **macOS** — creates **StockSage.app** in `~/Applications`: launch it from
  Spotlight or drag it to your Dock.
- **Windows** — puts a **StockSage** shortcut on your Desktop.
- **Linux** — adds StockSage to your applications menu and Desktop.

From then on it's double-click → app window. (The app window uses
Chrome/Edge/Brave under the hood; if none is installed it opens in your
default browser instead. `./start.sh web` forces browser mode.)

### Take it to any device — the brain travels with you

Everything StockSage has learned — signal weights, graded track record, move
memory — lives in **one file: the brain**. Three ways to move it, easiest
first:

**Shared brain across all your devices (set-and-forget):** open the
dashboard sidebar → **🧠 Brain → Share across your devices**. StockSage
detects your Dropbox / iCloud / OneDrive / Google Drive folder — pick it,
press **Share my brain**, done. Repeat on each device and they all read and
write the *same* brain — what one learns, all know. The sidebar always shows
where the brain lives, what it knows, and which device learned last. (Use
one device at a time; let the folder finish syncing before switching.)

Terminal equivalent:

```bash
./start.sh brain sync ~/Dropbox/StockSage     # or iCloud Drive / OneDrive / ...
```

**One-off transfer:** press **⬇️ Export brain** on the Learning tab (or
`./start.sh brain export`), move the file however you like, then **Import →
Merge** on the other device. Merging *compounds* knowledge — suggestions and
move history are unioned, and the most recently trained weights win — so
nothing is ever lost, no matter which direction you merge.

**New computer from scratch:**

```bash
git clone <your-repo-url> && cd Stock-project
./start.sh                        # sets itself up, opens the app
./start.sh brain import <file>    # or brain sync <folder>
```

Robinhood credentials are deliberately **never** part of the brain — link
Robinhood fresh on each device. `./start.sh brain info` shows where the
brain lives and what it knows.

**Code improvements travel too:**

```bash
./start.sh update                 # Windows: start.bat update
```

Pull the latest StockSage code from your repository on any device — so when
we improve the tool on one machine (or merge a change on GitHub), every
other device catches up with one command. It's deliberately safe: it only
fast-forwards, refuses to touch uncommitted local edits, tells you exactly
what came in, and refreshes dependencies automatically when they changed.
Brain + code together mean a device is never more than two commands from
fully current: `./start.sh update` for the code, the shared brain (or
`brain import`) for the knowledge.

### Use it on your phone

```bash
./start.sh phone          # Windows: start.bat phone
```

This runs StockSage on your computer and shares it to your home Wi-Fi,
printing a **QR code** — scan it with your phone's camera and the dashboard
opens in your phone browser. Then use **Add to Home Screen** (Share menu on
iPhone, ⋮ menu on Android) and StockSage gets its own icon on your phone,
opening full-screen like a native app.

How it works and what to know:

- **Your credentials never leave your computer.** The engine (and your
  Robinhood link) runs on the computer; the phone is just a screen for it.
- The computer must be **on and running phone mode** while you use it, and
  the phone must be on the **same Wi-Fi**.
- While phone mode runs, anyone on your Wi-Fi network could open the
  dashboard — fine at home, skip it on public networks.
- Want it from anywhere (cellular, work, travel)? Install
  [Tailscale](https://tailscale.com) (free for personal use) on both your
  computer and phone, run phone mode, and use the computer's Tailscale
  address instead — a private encrypted tunnel, no ports exposed to the
  internet.

### Terminal mode

Anything you pass to the launcher goes to the CLI instead of the dashboard:

```bash
./start.sh daily          # the once-a-day heartbeat (learn -> scan -> suggest)
./start.sh suggest        # quick ranked scan (nothing recorded)
./start.sh suggest NVDA   # look at specific tickers
./start.sh sectors        # sector trend scoreboard
./start.sh portfolio      # your holdings + live signals
./start.sh moves          # "why it moved" memory
./start.sh performance    # learning status & signal weights
```

(Windows: `start.bat daily`, etc.)

## Your day with StockSage

Open the app. It learns by itself, then opens with **☀️ Today's briefing** —
the whole situation in 15 seconds: market mood, the top ideas, alerts on
names you own, what just got graded, your week's paper P&L, and any big
moves with their reasons. Everything below it is detail.

Beyond the built-in universe, add any name to your **⭐ Watchlist**
(sidebar, or `./start.sh watch add PLTR`) — and anything you hold on
Robinhood is **always** scanned automatically, whether or not it's in the
universe. A stock you own is never unwatched.

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

### It keeps learning by itself

- **On open** — the dashboard automatically runs the learn+scan cycle the
  first time you open it each day.
- **On autopilot** — schedule it with your operating system so it learns
  every weekday at 5:30pm even when nothing is open:

  ```bash
  ./start.sh autopilot                       # learn every weekday 17:30
  ./start.sh autopilot publish "<folder>"    # + supply the trading routine
  ./start.sh autopilot status                # check both
  ./start.sh autopilot off                   # stop everything
  ```

  Uses launchd on macOS, cron on Linux, Task Scheduler on Windows; output
  goes to `~/.stocksage/daily.log` and `~/.stocksage/publish.log`. The
  computer must be awake at run time.

  **`autopilot publish "<folder>"`** is the hands-off trading bridge: every
  weekday (5 times, timed just ahead of a typical trading routine's runs)
  it mirrors your Robinhood fills into graded calls, grades matured ones,
  and writes a fresh research brief into a Google-Drive-synced folder — so
  your trading routine reads freshly-graded research on every run with
  zero manual steps. Point `<folder>` at a directory synced by Google
  Drive for Desktop (e.g. `"G:\My Drive\StockSage"`).

  **No admin rights / can't install Google Drive for Desktop?** Use
  `./start.sh autopilot publish-drive` instead — it talks to Google
  Drive's API directly from Python, with nothing installed beyond two
  packages already in StockSage's own virtual environment. One-time
  setup (all in a browser, no downloads):

  1. Go to [console.cloud.google.com](https://console.cloud.google.com/),
     create a project (free), then **APIs & Services → Library** →
     enable the **Google Drive API**.
  2. **APIs & Services → OAuth consent screen** → External → fill in an
     app name and your email → save (you can leave it in "Testing" mode).
  3. **APIs & Services → Credentials → Create Credentials → OAuth client
     ID** → Application type: **Desktop app** → Create.
  4. Click the download icon next to the new client → save the file as
     `drive_credentials.json` in your `~/.stocksage/` folder (Windows:
     `C:\Users\<you>\.stocksage\drive_credentials.json` — create the
     folder if it doesn't exist).
  5. Run `./start.sh publish-drive` once by hand — it opens your browser
     for a one-time "Sign in with Google" consent, then never asks again.

  From then on `./start.sh autopilot publish-drive` schedules the exact
  same capture → grade → publish cycle, just delivered straight to Drive's
  API instead of a synced folder. Everything lands in one "StockSage"
  folder in your Drive, in three files that get updated in place each
  run (no dated duplicates to clean up, no ambiguity about which is
  current).

### The learning loop (the point of the whole tool)

1. Every actionable suggestion is **recorded** with its full signal breakdown.
2. Five trading days later, the outcome is **graded**: did price move the way
   the score predicted? Grading runs on every touchpoint — daily cycle,
   dashboard open, even a quick `suggest` — so no matured call waits.
3. Weights update via the **multiplicative-weights (Hedge)** algorithm, at
   two levels: the **global** vector, and a **per-sector** vector (what works
   in Energy isn't what works in Tech). A sector's own weights start voting —
   blended 50/50 with the global view — once it has 10 graded calls of its
   own. A weight floor keeps every signal alive so the model can re-adapt
   when regimes change.
4. The weights are fully **inspectable** (`performance` command / Learning
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
  volatility, and is hard-capped at 10% of investable cash per idea.
- **Protective stops** — every buy suggestion includes a 2×ATR stop level.
- **Read-only broker link** — architecture prevents auto-trading; your
  Robinhood credentials live only in your local `.env` (gitignored) and are
  never written to disk by the app.
- **Honest self-grading** — the hit rate on the Learning tab is computed from
  real recorded calls, not backtests.

### Your Robinhood account, fully mirrored

Link once (Portfolio tab form — with your TOTP secret saved, the session
persists and every future connection is automatic). From then on, **every
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

## The universe

10 sectors, 10 leaders each (100 stocks) plus SPDR sector ETFs for trend:
Technology, Healthcare, Financials, Consumer Discretionary, Communication
Services, Industrials, Consumer Staples, Energy, Utilities, Real Estate.
Edit `stocksage/universe.py` to change it — nothing else hardcodes tickers.

## If anything seems off

```bash
./start.sh doctor             # Windows: start.bat doctor
```

Checks everything that can go wrong — Python, dependencies, settings,
brain integrity, market-data access, Robinhood login, update channel,
autopilot — and prints a plain-language fix for anything that isn't right.
Safe to run anytime; changes nothing.

## Testing

```bash
.venv/bin/python -m pytest    # fully offline — synthetic data, in-memory DB
```

## Roadmap (evolves with use)

- [ ] Earnings-calendar awareness (don't suggest entries into a print)
- [ ] Correlation-aware portfolio sizing (avoid five copies of the same bet)
- [ ] Per-sector learned weights (what works in Energy ≠ Tech)
- [ ] Optional LLM summarization of move context into plain-English narratives
- [ ] Paper-trading ledger to measure the strategy before any real automation
