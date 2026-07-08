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

### The learning loop (the point of the whole tool)

1. Every actionable suggestion is **recorded** with its full signal breakdown.
2. Five trading days later, the outcome is **graded**: did price move the way
   the score predicted?
3. Signal weights update via the **multiplicative-weights (Hedge)** algorithm:
   signals that keep calling direction correctly earn influence; ones that
   miss lose it. A weight floor keeps every signal alive so the model can
   re-adapt when market regimes change.
4. The weights are fully **inspectable** (`performance` command / Learning
   tab) — you can always see what the tool currently believes works. No black
   box.

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

## The universe

10 sectors, 10 leaders each (100 stocks) plus SPDR sector ETFs for trend:
Technology, Healthcare, Financials, Consumer Discretionary, Communication
Services, Industrials, Consumer Staples, Energy, Utilities, Real Estate.
Edit `stocksage/universe.py` to change it — nothing else hardcodes tickers.

## Automate the daily run

```cron
# weekdays at 5:30pm ET, after the close
30 17 * * 1-5 /path/to/Stock-project/start.sh daily >> ~/.stocksage/daily.log 2>&1
```

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
