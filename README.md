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

## Quick start

```bash
# 1. Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) Link Robinhood — read-only
cp .env.example .env        # fill in your credentials; .env is gitignored
set -a; source .env; set +a

# 3. Run the dashboard
streamlit run app.py
```

Press **Run daily cycle** in the dashboard, or from the terminal:

```bash
python -m stocksage daily          # the once-a-day heartbeat
python -m stocksage suggest        # quick ranked scan (nothing recorded)
python -m stocksage suggest NVDA   # look at specific tickers
python -m stocksage sectors        # sector trend scoreboard
python -m stocksage portfolio      # your holdings + live signals
python -m stocksage moves          # "why it moved" memory
python -m stocksage performance    # learning status & signal weights
```

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
30 17 * * 1-5 cd /path/to/Stock-project && .venv/bin/python -m stocksage daily >> ~/.stocksage/daily.log 2>&1
```

## Testing

```bash
pytest            # fully offline — synthetic data, in-memory DB
```

## Roadmap (evolves with use)

- [ ] Earnings-calendar awareness (don't suggest entries into a print)
- [ ] Correlation-aware portfolio sizing (avoid five copies of the same bet)
- [ ] Per-sector learned weights (what works in Energy ≠ Tech)
- [ ] Optional LLM summarization of move context into plain-English narratives
- [ ] Paper-trading ledger to measure the strategy before any real automation
