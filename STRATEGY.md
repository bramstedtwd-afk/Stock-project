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
