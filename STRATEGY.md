# StockSage strategy: what we do, why, and what would change it

Decision support only. StockSage never places an order; the agentic account's
every order still waits for a human confirm.

## What the research found (run `.\start.bat research`; last run 2026-10-01)

61 rule-based strategies on liquid ETFs, 2006-2026, 5 bps costs, each judged on
alpha (return over cash after removing plain SPY and a passive mix of asset
classes) and corrected for how many were tried (White's Reality Check). The
years from 2017 were a holdout, spent only on rules that passed on the earlier
years.

| Family | Result |
|---|---|
| Trend following (SPY, asset classes), asset-class momentum, inverse-vol + trend, dual momentum | Real, repeatable alpha across variants (all-years corrected p about 0.01-0.06). Not one lucky setting |
| Vol targeting | Weak (p about 0.16) |
| Sector rotation, short-term mean reversion | Nothing (p 0.5-0.8) |

**The catch.** From 2017 on, the six or seven rules that passed kept a small
positive alpha (+1% to +3% a year) but none was statistically significant
alone, and every one earned less than plain SPY (5-12% a year against 15%) in
that strong bull run. Their worst drops were 11-27%, against 34% for SPY (and
55% in 2008). So these rules are a **risk dial that cuts crashes and gives up
some upside**, not a way to beat the market. Their alpha is not only 2008: it
stays +2% to +5% a year without it.

Which individual rules sit just under the pass line (corrected p near 0.10)
changes with the random draws, so read the *families*, not a single name.

Separate results from the earlier lab and backtest: the model's weekly buy
picks are no better than random picks; 12-month momentum on stocks looks good
but is within luck once corrected; the look-alike filter did worse than not
using it (-1.39% a period, all below zero). Treat anything on stocks with
extra suspicion: the stock universe is today's winners, which flatters every
momentum test (survivorship).

## Buying red days and selling green days (run `.\start.bat dipbuy`; last run 2026-10-06)

The owner asked: buy $500 on every red day, sell each buy after a green day once
it is up X%, what does that return? Tested on SPY, QQQ, AAPL, MSFT and NVDA over
each one's full daily history (SPY 1993, AAPL 1980, MSFT 1986, QQQ and NVDA
1999): 5 buy rules (every red day, down 1%+, down 2%+, second red day in a row,
red day 10%+ under the 52-week high) x 20 sell rules (never; each buy at +1% to
+50%; everything on a +1/2/3% day; whole position at +5/10/20%; targets with
stops or time limits; a plain 20-day exit). Every buy is new money, so all sell
rules on one buy rule put in the same dollars on the same days. 5 bps slippage,
idle cash at the T-bill rate, no taxes.

| Question | Answer |
|---|---|
| Does any sell rule beat holding the same buys? | **No: 0 of 475**, with fills at the close or the next open, and with sale money in T-bills or SPY. Example: SPY, $1.92M in over 3,834 red days, held = $17.4M (10.9% a year); each buy sold at +2% = $2.8M (2.2% a year) |
| Why does a 100% win rate lose? | Losers are never sold; they wait (one SPY buy 6.6 years, one QQQ buy ~15 years). Between trades the money sits in cash: the SPY +2% rule had ~3% of its money invested on an average day and earned ~$1,400 a year from trading |
| Sale money into SPY instead? | On SPY it just matches holding, minus costs. On the stocks, 10-13% a year against 15-37% for holding them |
| Stops / sell-on-a-big-green-day? | Cut the worst open loss (to ~4% with a -5% stop) but still 2-3% a year; win rates 56-68% |
| Is buying on red days better than any day? | No. Within 0.15 point a year of buying daily. With the waiting time charged (same savings every day, held in T-bills until the signal), waiting for 2%+ drops or 10% corrections never beat buying daily by more than 0.01 point and lost up to 0.5 point (SPY corrections 10.39% vs 10.87%) |
| Any decade where selling won? | Only 2000-2009 on SPY (holding 1.4% a year, profits parked in T-bills 2.3-4.9%) |

Trap to remember: a per-buy IRR makes "wait for a correction" look +0.5 point
better on SPY, because it never charges for the months the cash waited.
`dipbuy.paced` is the fair test. Survivorship: AAPL, MSFT and NVDA were chosen
knowing they won; SPY and QQQ are the fair tickers.

**Policy consequence:** none new. It backs "hold the market, manage risk only".
Profit targets and green-day exits are risk tools; never present one as a way to
earn more than holding.

## The owner's goal (2026-10-01)

**Maximum growth: hold the market, manage risk only.** The trend rules are
therefore context, not instructions. Sale proceeds default to a broad index
fund, because nothing tested here has beaten one. The sheet keeps a forward
scorecard (below) so any idea that later earns a place does so on live evidence.

## What is in force

1. **Rules, always.** Stop-loss, 25% size cap, 10-day clock (agentic account
   only), $15 floor, cross-account concentration. These need no model skill and
   are stated as orders, tagged `[RULE]`.
2. **A trend dial.** The sheet shows which broad asset classes are above their
   150-day average. It is context, never an order, because the evidence is that
   it lowers risk, not that it raises return.
3. **Model opinions are ideas.** A model BUY is a plain BUY only when look-alike
   history backs it *and* the buy side has earned trust (`STOCKSAGE_ENTRIES`).
   Today neither holds, so buys are listed as ideas.
4. **Where money goes.** Proceeds from trims and sells on the manual accounts
   default to a broad index fund (the one already held, else VTI), unless a plain
   BUY wants the cash.
5. **Forward scorecard.** Every call the sheet makes (rule sells, model sells,
   trims, plain buys, and the buy ideas history did not back) is logged and graded
   against SPY after 10 trading days. Verdicts use `actions.trust_level`, the one
   definition of proof, with uncertainty taken from two-week buckets. It trains
   nothing. This is the only evidence that can promote a buy idea.
6. **Model SELLs** are stated as `[MODEL]`. The live record is promising and the
   backtest inconclusive; treat them as a prompt to look, weigh the rules first.

## How something gets promoted

| Step | Gate |
|---|---|
| Idea tested | counted in the family: every variant tried raises the bar for all |
| Becomes a rule | passes on development years (corrected p < 0.10), then positive on holdout |
| Becomes an instruction | the forward scorecard shows `earned` for that kind of call (enough graded calls and a clear margin over SPY) |

Adding candidates to `research.candidates()` is allowed but is logged by the
family size in the report. Do not delete the ones that failed.

## Cadence

- Daily: the sheet (`.\start.bat today`, the dashboard, the phone copy in Drive).
- First trading day of the month: look at the trend line; a change of state
  is the only time the dial matters.
- Quarterly: re-run `research` and `lab`. A rule that stops passing is demoted.

## Limits to keep in mind

- Three real crashes (2008, 2020, 2022) carry most of what trend rules show.
- Rules like these are published, so some of their past edge may already be gone.
- In a taxable account, selling on a trend signal realises gains; the Roth is the
  natural home for any rotation. The margin account is sized from cash only.
- A rule that trails SPY in a bull market is working as designed. Whether that
  trade is worth it is the owner's choice, not a statistical question.
