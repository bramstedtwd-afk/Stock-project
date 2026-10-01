"""The strategy lab: try ideas that might actually work, and be hard on them.

The weekly buy picks turned out no better than random. Rather than tune them,
this tests different IDEAS with documented track records, through the same
honest machinery as the backtest:

  momentum        the past 12 months' winners, skipping the latest month, held
                  a month. Fewer trades, so costs bite less.
  momentum+regime the same, but in cash whenever SPY is under its 200-day average.
  regime only     SPY when it is above its 200-day average, cash when below.
  drift           names that gapped up on heavy volume in the last 10 sessions,
                  held a month. A price-based stand-in for post-earnings drift:
                  real drift needs EPS-surprise history, which is not reliably
                  available for free, so this is labelled a PROXY, not the thing.
  hedge / ridge   the current model and a challenger, on the weekly samples.

Every result is a per-period edge over simply holding SPY, so a strategy that
hides in cash is charged for the rally it missed.

MULTIPLE TESTING. Try six things and one will look good by luck. Each verdict
is therefore corrected for how many strategies were tried (Bonferroni), and
"earned" additionally demands a bootstrap interval clear of zero and enough
periods for the maths to hold. A lab that crowns its best-looking result
without that correction is a way of fooling yourself, not of finding an edge.

collect_monthly touches the network; everything else is pure.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import NormalDist


from . import universe
from .backtest import DEFAULT_COST, _block_bootstrap_ci

MONTH = 21                 # trading days per holding period
LOOKBACK = 252             # a year
SKIP = 21                  # skip the most recent month (short-term reversal)
TREND_DAYS = 200
GAP_MIN = 0.04             # a 4% overnight gap...
VOL_SPIKE = 2.0            # ...on at least twice the usual volume
GAP_WINDOW = 10            # looked back over the last ten sessions
VOL_AVG = 50
TOP_N = 10
MIN_PERIODS_EARNED = 48    # four years of months
FAMILY_ALPHA = 0.01        # familywise, one-sided
RANDOM_DRAWS = 200
MIN_PERIODS_TO_REPORT = 12


@dataclass
class Row:
    date: str
    ticker: str
    momentum: float | None     # 12-1 month return, from data up to `date` only
    drift: float | None        # size of a recent heavy-volume gap-up, if any
    fwd: float                 # return over the next month
    spy_fwd: float
    regime_on: bool            # SPY above its 200-day average at `date`

    @property
    def excess(self) -> float:
        return self.fwd - self.spy_fwd


# --- data (network) ----------------------------------------------------------


def collect_monthly(market, tickers=None, period: str = "5y") -> list[Row]:
    tickers = tickers or universe.all_tickers()
    spy = market.history(universe.MARKET_BENCHMARK, period=period)
    if spy is None or spy.empty:
        raise RuntimeError("no SPY history, so there is nothing to measure against")
    rows: list[Row] = []
    for ticker in tickers:
        df = market.history(ticker, period=period)
        if df is None or len(df) < LOOKBACK + MONTH + 2:
            continue
        close = df["Close"].to_numpy(dtype=float)
        open_ = df["Open"].to_numpy(dtype=float)
        vol = df["Volume"].to_numpy(dtype=float)
        spy_close = spy["Close"].reindex(df.index, method="ffill").to_numpy(dtype=float)
        for end in range(LOOKBACK + 1, len(df) - MONTH, MONTH):
            last = end - 1                                   # the decision bar
            entry, exit_ = close[last], close[last + MONTH]
            s_entry, s_exit = spy_close[last], spy_close[last + MONTH]
            base = close[last - LOOKBACK]
            if min(entry, exit_, s_entry, s_exit, base, close[last - SKIP]) <= 0:
                continue
            momentum = close[last - SKIP] / base - 1.0
            drift = None
            for i in range(end - GAP_WINDOW, end):
                avg = vol[i - VOL_AVG: i].mean()
                gap = open_[i] / close[i - 1] - 1.0
                if avg > 0 and gap >= GAP_MIN and vol[i] / avg >= VOL_SPIKE:
                    drift = gap if drift is None else max(drift, gap)
            rows.append(Row(
                str(df.index[last].date()), ticker, float(momentum), drift,
                float(exit_ / entry - 1.0), float(s_exit / s_entry - 1.0),
                bool(s_entry > spy_close[end - TREND_DAYS: end].mean()),
            ))
    return rows


# --- pickers (pure) ----------------------------------------------------------


def pick_momentum(rows: list[Row], n: int = TOP_N) -> list[Row]:
    eligible = [r for r in rows if r.momentum is not None]
    return sorted(eligible, key=lambda r: r.momentum, reverse=True)[:n]


def pick_drift(rows: list[Row], n: int = TOP_N) -> list[Row]:
    eligible = [r for r in rows if r.drift is not None]
    return sorted(eligible, key=lambda r: r.drift, reverse=True)[:n]


# --- running a strategy ------------------------------------------------------


@dataclass
class LabResult:
    name: str
    periods: int = 0
    active_periods: int = 0       # periods the strategy actually did something
    edge: float | None = None     # mean per-period edge over holding SPY
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    random_edge: float | None = None
    vs_random: float | None = None
    level: str = "unproven"       # after correcting for how many were tried
    per_period: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "per_period"}


def lab_level(n: int, mean: float, se: float, ci_low: float | None, tests_run: int) -> str:
    """earned / failing / unproven, corrected for the number of ideas tried."""
    k = max(1, tests_run)
    z_earned = NormalDist().inv_cdf(1.0 - FAMILY_ALPHA / k)
    z_failing = NormalDist().inv_cdf(1.0 - 0.05 / k)
    if (n >= MIN_PERIODS_EARNED and mean - z_earned * se > 0
            and ci_low is not None and not math.isnan(ci_low) and ci_low > 0):
        return "earned"
    if n >= 24 and mean + z_failing * se < 0:
        return "failing"
    return "unproven"


def run_strategy(
    rows: list[Row],
    name: str,
    picker=None,
    regime: bool = False,
    top_n: int = TOP_N,
    cost: float = DEFAULT_COST,
    tests_run: int = 1,
    seed: int = 7,
) -> LabResult:
    """One strategy, month by month, judged against simply holding SPY.

    picker=None means "hold SPY": combined with regime=True that is pure market
    timing. A month the strategy makes no picks, it holds SPY (edge 0). A month
    the regime filter says cash, it earns minus SPY's return, because that is
    exactly what it gave up.
    """
    by_date: dict[str, list[Row]] = {}
    for r in rows:
        by_date.setdefault(r.date, []).append(r)
    rng = random.Random(seed)
    edges: list[float] = []
    randoms: list[float] = []
    active = 0
    for d in sorted(by_date):
        today = by_date[d]
        if regime and not today[0].regime_on:
            edges.append(-today[0].spy_fwd)
            randoms.append(-today[0].spy_fwd)
            active += 1
            continue
        chosen = picker(today, top_n) if picker else []
        if not chosen:
            edges.append(0.0)
            randoms.append(0.0)
            continue
        active += 1
        k = len(chosen)
        edges.append(sum(r.excess for r in chosen) / k - cost)
        draws = [
            sum(r.excess for r in rng.sample(today, min(k, len(today)))) / min(k, len(today))
            for _ in range(RANDOM_DRAWS)
        ]
        randoms.append(sum(draws) / len(draws) - cost)

    res = LabResult(name=name, periods=len(edges), active_periods=active, per_period=edges)
    n = len(edges)
    if n < 2:
        return res
    mean = sum(edges) / n
    sd = math.sqrt(sum((e - mean) ** 2 for e in edges) / (n - 1))
    res.edge, res.se = mean, sd / math.sqrt(n)
    res.ci_low, res.ci_high = _block_bootstrap_ci(edges, seed)
    res.random_edge = sum(randoms) / n
    res.vs_random = mean - res.random_edge
    res.level = lab_level(n, mean, res.se, res.ci_low, tests_run)
    return res


def standard_strategies() -> list[tuple[str, object, bool]]:
    return [
        ("12-1 momentum", pick_momentum, False),
        ("momentum + regime filter", pick_momentum, True),
        ("regime filter only", None, True),
        ("gap-up drift (earnings proxy)", pick_drift, False),
    ]


def run_all(rows: list[Row], extra: list[LabResult] | None = None, cost: float = DEFAULT_COST,
            top_n: int = TOP_N) -> list[LabResult]:
    """Every standard strategy, each corrected for the full family size.

    `extra` are results computed elsewhere (the weekly Hedge and ridge runs)
    that count toward the family: leaving them out would understate how many
    ideas were tried, which is the very thing the correction exists to count.
    """
    strategies = standard_strategies()
    k = len(strategies) + len(extra or [])
    out = [run_strategy(rows, name, picker, regime, top_n, cost, tests_run=k)
           for name, picker, regime in strategies]
    return out


def relevel(result, tests_run: int) -> LabResult:
    """Wrap a backtest.Result as a LabResult judged by the same family rule."""
    r = LabResult(
        name="", periods=result.periods, active_periods=result.periods,
        edge=result.edge, se=result.se, ci_low=result.ci_low, ci_high=result.ci_high,
        random_edge=result.random_edge, vs_random=result.vs_random,
        per_period=list(result.per_period),
    )
    if result.edge is not None and result.se is not None:
        r.level = lab_level(result.periods, result.edge, result.se, result.ci_low, tests_run)
    return r


# --- the words ---------------------------------------------------------------


def _pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.2f}%"


VERDICT = {
    "earned": "EDGE (survives correcting for how many ideas were tried)",
    "failing": "WORSE than holding SPY",
    "unproven": "no proven edge",
}


def describe(results: list[LabResult], periods_per_year: dict[str, float] | None = None) -> list[str]:
    k = len(results)
    out = ["STRATEGY LAB: WHAT BEATS JUST HOLDING THE MARKET?", "-" * 70,
           f"{k} ideas tried, so each verdict is corrected for that: with {k} tries one",
           "will look good by luck. Edge is per period over holding SPY, after costs.", ""]
    out.append(f"{'strategy':<32}{'periods':>8}{'edge/period':>13}{'95% range':>22}")
    for r in results:
        if r.edge is None or r.periods < MIN_PERIODS_TO_REPORT:
            out.append(f"{r.name:<32}{r.periods:>8}{'too few periods':>35}")
            continue
        rng_txt = f"{_pct(r.ci_low)} to {_pct(r.ci_high)}"
        out.append(f"{r.name:<32}{r.periods:>8}{_pct(r.edge):>13}{rng_txt:>22}")
    out.append("")
    for r in results:
        if r.edge is None or r.periods < MIN_PERIODS_TO_REPORT:
            continue
        line = f"  {r.name}: {VERDICT[r.level]}"
        if r.vs_random is not None and r.level != "failing":
            line += f"; beyond random picks {_pct(r.vs_random)}"
        out.append(line)
    winners = [r for r in results if r.level == "earned"]
    out.append("")
    if winners:
        out.append("RESULT: " + ", ".join(r.name for r in winners) + " cleared the corrected bar.")
        out.append("A pass is a reason to test it further, not to bet on it yet.")
    else:
        out.append("RESULT: nothing cleared the corrected bar. The honest reading is that none of")
        out.append("these ideas has been shown to beat simply holding the market.")
    return out
