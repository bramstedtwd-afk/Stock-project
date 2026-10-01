"""Does the model have an edge? A walk-forward test that cannot flatter it.

The live scoreboard answers this slowly (five days per data point). This
replays history through the same scoring code and answers it now — but only
if the replay is honest, and the easy ways to make it dishonest are exactly
the ways a backtest usually lies:

  * LOOK-AHEAD. Features at date d see only bars up to d, and the weights at
    d have learned only from outcomes that were already RESOLVED by d (a pick
    made at d-step finishes at d). Re-using the brain's current weights would
    grade the model on data it was trained on; this re-learns from scratch,
    as the live system did.
  * COSTS. Every pick pays a round-trip cost, because a signal that only
    works before spreads is not a signal.
  * LUCK. Picks are compared with random picks from the same names on the
    same dates, and the result carries a block-bootstrap interval, not just
    a mean.
  * OVERLAP. Samples are taken once per horizon, so periods do not share
    days and the standard error is not understated by double counting.

The verdict uses actions.trust_level, the same definition the live gate acts
on, so "proven" means one thing everywhere.

collect_samples touches the network; everything else is pure and offline.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np

from . import universe
from .actions import trust_level
from .indicators import MIN_HISTORY_ROWS, compute_features
from .learning import update_weights, weighted_score
from .scoring import BUY_THRESHOLD, SELL_THRESHOLD, risk_multiplier

DEFAULT_TOP_N = 5
DEFAULT_COST = 0.001        # 10 bps round trip per pick: spread + slippage
HORIZON = 5                 # trading days; matches the live grading horizon
STEP = HORIZON              # one sample per horizon -> no overlapping periods
ETA = 0.05                  # same gentle rate bootstrap warm-up uses
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_BLOCK = 4         # periods; keeps autocorrelation in the interval
RANDOM_DRAWS = 200
MIN_PERIODS_TO_REPORT = 20  # below this, numbers look meaningful and are not


@dataclass
class Sample:
    date: str                     # the bar the decision was made on
    ticker: str
    features: dict[str, float]
    excess: float                 # forward return minus SPY's, same window
    vol: float | None             # annualised, from data up to `date` only


@dataclass
class Result:
    periods: int = 0
    no_signal_periods: int = 0
    picks: int = 0
    edge: float | None = None            # mean per-period edge after costs
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    hit_rate: float | None = None        # periods where the picks beat SPY
    random_edge: float | None = None     # same dates, random names, same costs
    universe_edge: float | None = None   # equal-weight everything
    vs_random: float | None = None       # model minus random: the skill, net of luck
    level: str = "unproven"
    per_period: list[float] = field(default_factory=list)
    picks_log: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("per_period", "picks_log")}


# --- data (network) ----------------------------------------------------------


def collect_samples(market, tickers=None, period: str = "2y") -> list[Sample]:
    """Every (date, ticker) decision point with its features and outcome.

    Mirrors bootstrap.warmup_learning's loop: features see only bars up to the
    decision point, and the outcome is the SPY-excess return `HORIZON` bars on.
    """
    tickers = tickers or universe.all_tickers()
    spy = market.history(universe.MARKET_BENCHMARK, period=period)
    if spy is None or spy.empty:
        raise RuntimeError("no SPY history, so there is nothing to measure against")
    out: list[Sample] = []
    for ticker in tickers:
        df = market.history(ticker, period=period)
        if df is None or len(df) < MIN_HISTORY_ROWS + HORIZON:
            continue
        close = df["Close"]
        spy_close = spy["Close"].reindex(df.index, method="ffill")
        sector = universe.sector_of(ticker)
        etf = market.history(sector.etf, period=period) if sector else None
        rets = np.log(close).diff()
        for end in range(MIN_HISTORY_ROWS, len(df) - HORIZON, STEP):
            bench = etf.loc[: df.index[end - 1]] if etf is not None else None
            feats = compute_features(df.iloc[:end], benchmark_df=bench)
            if not feats:
                continue
            entry, exit_ = float(close.iloc[end - 1]), float(close.iloc[end - 1 + HORIZON])
            s_entry, s_exit = float(spy_close.iloc[end - 1]), float(spy_close.iloc[end - 1 + HORIZON])
            if min(entry, exit_, s_entry, s_exit) <= 0:
                continue
            window = rets.iloc[max(1, end - 60): end].dropna()
            vol = float(window.std() * math.sqrt(252)) if len(window) >= 20 else None
            out.append(Sample(
                str(df.index[end - 1].date()), ticker, feats,
                (exit_ / entry - 1.0) - (s_exit / s_entry - 1.0), vol,
            ))
    return out


# --- evaluation (pure) -------------------------------------------------------


def _block_bootstrap_ci(values: list[float], seed: int) -> tuple[float, float]:
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < BOOTSTRAP_BLOCK * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    starts_max = n - BOOTSTRAP_BLOCK + 1
    blocks = math.ceil(n / BOOTSTRAP_BLOCK)
    means = np.empty(BOOTSTRAP_DRAWS)
    for i in range(BOOTSTRAP_DRAWS):
        idx = rng.integers(0, starts_max, blocks)
        sample = np.concatenate([arr[s: s + BOOTSTRAP_BLOCK] for s in idx])[:n]
        means[i] = sample.mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def evaluate(
    samples: list[Sample],
    top_n: int = DEFAULT_TOP_N,
    cost: float = DEFAULT_COST,
    learn: bool = True,
    seed: int = 7,
    side: str = "buy",
) -> Result:
    """Walk forward through time, picking as the live system would.

    side="buy" tests the names the model likes (did they beat the market?).
    side="sell" tests the names it rates SELL (did they trail it?). The two
    are different claims — a model can be useless at picking winners and still
    be good at flagging names to get out of — and the sell side is the one the
    app mostly ACTS on, since exits are risk rules fed by it. The edge is
    reported in the direction of the bet: positive means the call was right.

    At each date: first learn from outcomes that are already resolved (those
    from the previous date, which finished exactly now), then score every
    name, take the best `top_n` that clear the BUY threshold, and record what
    they went on to do. Weights never see the outcome of a pick that has not
    finished yet.
    """
    by_date: dict[str, list[Sample]] = {}
    for s in samples:
        by_date.setdefault(s.date, []).append(s)
    dates = sorted(by_date)

    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    sign = 1.0 if side == "buy" else -1.0
    rng = random.Random(seed)
    weights: dict[str, float] = {}
    model_edges: list[float] = []
    random_edges: list[float] = []
    universe_edges: list[float] = []
    picks_total = 0
    no_signal = 0
    previous: list[Sample] = []
    picks_log: dict[str, list[str]] = {}

    for d in dates:
        today = by_date[d]
        if learn:
            for s in previous:           # resolved as of today, not before
                weights, _ = update_weights(weights, s.features, s.excess, eta=ETA)
        previous = today

        scored = sorted(
            ((weighted_score(s.features, weights) * risk_multiplier(s.vol), s) for s in today),
            key=lambda x: x[0], reverse=(side == "buy"),
        )
        if side == "buy":
            chosen = [s for score, s in scored if score >= BUY_THRESHOLD][:top_n]
        else:
            chosen = [s for score, s in scored if score <= SELL_THRESHOLD][:top_n]
        if not chosen:
            no_signal += 1
            continue
        k = len(chosen)
        picks_total += k
        picks_log[d] = [s.ticker for s in chosen]
        model_edges.append(sign * sum(s.excess for s in chosen) / k - cost)
        universe_edges.append(sign * sum(s.excess for s in today) / len(today))
        draws = [
            sign * sum(s.excess for s in rng.sample(today, min(k, len(today)))) / min(k, len(today))
            for _ in range(RANDOM_DRAWS)
        ]
        random_edges.append(sum(draws) / len(draws) - cost)

    result = Result(periods=len(model_edges), no_signal_periods=no_signal, picks=picks_total,
                    per_period=model_edges, picks_log=picks_log)
    n = len(model_edges)
    if n < 2:
        return result
    mean = sum(model_edges) / n
    sd = math.sqrt(sum((e - mean) ** 2 for e in model_edges) / (n - 1))
    result.edge, result.se = mean, sd / math.sqrt(n)
    result.ci_low, result.ci_high = _block_bootstrap_ci(model_edges, seed)
    result.hit_rate = sum(1 for e in model_edges if e > 0) / n
    result.random_edge = sum(random_edges) / n
    result.universe_edge = sum(universe_edges) / n
    result.vs_random = mean - result.random_edge
    result.level = trust_level(n, mean, result.se)
    return result


# --- the words ---------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.2f}%"


def describe(r: Result, years: str = "2y", side: str = "buy") -> list[str]:
    title = ("BACKTEST: DO ITS BUY PICKS BEAT THE MARKET?" if side == "buy"
             else "BACKTEST: DO ITS SELL CALLS TRAIL THE MARKET?")
    out = [title, "-" * 62,
           f"History: {years}, decided once per {HORIZON} trading days, no overlap.",
           "Weights re-learned as time passed, using only outcomes already known",
           "at each date. Every pick pays a round-trip cost.", ""]
    if r.edge is None or r.periods < MIN_PERIODS_TO_REPORT:
        out.append(
            f"Only {r.periods} usable period(s) — too few to say anything "
            f"(at least {MIN_PERIODS_TO_REPORT} are needed before a number means "
            f"something). Use a longer history."
        )
        return out
    out += [
        f"Periods tested:           {r.periods}  ({r.picks} picks, {r.no_signal_periods} periods with no signal)",
        f"Model, per period:        {_pct(r.edge)} {'versus SPY' if side == 'buy' else '(positive = the sells were right)'}, after costs",
        f"  95% interval:           {_pct(r.ci_low)} to {_pct(r.ci_high)}",
        f"  right in:               {r.hit_rate * 100:.0f}% of periods",
        f"Random picks, same dates: {_pct(r.random_edge)}",
        f"Skill beyond luck:        {_pct(r.vs_random)}",
        f"Whole universe, equal:    {_pct(r.universe_edge)}",
        "",
    ]
    lo, hi = r.ci_low, r.ci_high
    if r.level == "failing":
        out.append("VERDICT: NO EDGE — the model trails the market by more than chance explains.")
    elif r.level == "earned":
        out.append("VERDICT: EDGE FOUND — beating the market beyond what chance explains.")
    elif lo is not None and not math.isnan(lo) and lo > 0:
        out.append("VERDICT: PROMISING BUT UNPROVEN — the interval is above zero, but "
                   "there are too few periods to trust it yet. Try a longer history.")
    elif hi is not None and not math.isnan(hi) and hi < 0:
        out.append("VERDICT: LEANING NEGATIVE — below the market, though not yet by more than chance.")
    else:
        out.append("VERDICT: INCONCLUSIVE — the interval spans zero: no evidence of an edge, "
                   "and none against one.")
    if r.vs_random is not None and r.vs_random <= 0:
        out.append("The model's picks did no better than picking names at random.")
    return out
