"""Mass strategy research that is hard to fool.

The question is "does any rules-based algorithm beat simply holding the
market?", answered over ~20 years of liquid ETFs (no survivorship problem: a
sector or asset-class fund does not quietly vanish the way a delisted stock
does, and you can buy every one of them in Robinhood).

Every candidate is a plain rule that turns past prices into target weights:

    trend filters, dual momentum, Faber-style asset-class trend, sector and
    asset-class momentum rotation, inverse-volatility weighting, volatility
    targeting, short-term mean reversion.

How it avoids fooling itself:

  * NO LOOK-AHEAD. Weights decided at the close of day t earn day t+1's return
    (`simulate` shifts them). tests/test_research.py perturbs future prices and
    checks every candidate's earlier weights do not move.
  * COSTS on every unit of turnover.
  * ALPHA, NOT RETURN. A strategy that sits in cash half the time earns less
    than SPY and may still be better. So each is judged on alpha: its return
    over cash after removing the part explained by simply holding SPY.
  * MULTIPLE TESTING. ~60 candidates means a few look great by luck. The
    verdict comes from White's Reality Check (stationary bootstrap of the
    BEST-looking candidate's t-statistic across the whole family), not from the
    best candidate's own p-value. Every parameter variant counts.
  * A HOLDOUT. Candidates are judged on the development years only. The holdout
    years are looked at ONLY for those that pass; if none pass, they stay unspent.

Pure maths over pandas/numpy; the network lives in `load_prices`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

CASH = "SHY"
EQUITY, INTL, BONDS, LONG_BONDS, GOLD, REIT = "SPY", "EFA", "AGG", "TLT", "GLD", "VNQ"
ASSET_CLASSES = [EQUITY, INTL, "EEM", BONDS, LONG_BONDS, GOLD, REIT]
SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
ALL_TICKERS = sorted({CASH, *ASSET_CLASSES, *SECTORS})

EVAL_START = "2006-01-01"        # every asset has a year of history by here
SPLIT = "2017-01-01"             # development before, holdout from
MONTH = 21
DAYS = 252
DEFAULT_COST = 0.0005            # 5 bps per unit of one-way turnover
PASS_P = 0.10                    # corrected p-value a candidate must beat to touch the holdout


# --- data ---------------------------------------------------------------------


def load_prices(cache_path: Path | None = None, max_age_hours: float = 12.0) -> pd.DataFrame:
    """Adjusted closes (dividends included) for every ticker, cached on disk."""
    import time

    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                pass
    import yfinance as yf

    raw = yf.download(ALL_TICKERS, start="2003-01-01", auto_adjust=True, progress=False)
    prices = raw["Close"] if "Close" in raw else raw
    prices = prices.dropna(how="all").sort_index()
    missing = [t for t in ALL_TICKERS if t not in prices or prices[t].dropna().empty]
    if missing:
        raise RuntimeError(f"no price data for {', '.join(missing)}")
    if cache_path is not None:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            prices.to_parquet(cache_path)
        except Exception:
            pass
    return prices


# --- building blocks ----------------------------------------------------------


def month_end_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """True on the last trading day of each month (depends on dates only)."""
    s = pd.Series(index, index=index)
    return (s.dt.to_period("M") != s.shift(-1).dt.to_period("M")).to_numpy()


def hold_monthly(weights_at_month_end: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Rows decided on month-end days, carried forward until the next one."""
    return weights_at_month_end.reindex(index).ffill().fillna(0.0)


def mom(prices: pd.DataFrame, months: int) -> pd.DataFrame:
    return prices / prices.shift(MONTH * months) - 1.0


def sma(prices: pd.DataFrame, days: int) -> pd.DataFrame:
    return prices.rolling(days, min_periods=days).mean()


def _month_end_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[month_end_mask(frame.index)]


# --- candidate rules: prices -> target weights (cash is the remainder) ---------


def trend_spy(prices, n: int, monthly: bool = True):
    on = (prices[EQUITY] > sma(prices[[EQUITY]], n)[EQUITY]).astype(float)
    w = pd.DataFrame({EQUITY: on})
    return hold_monthly(_month_end_rows(w), prices.index) if monthly else w.fillna(0.0)


def dual_momentum(prices, months: int):
    m = mom(prices[[EQUITY, INTL, CASH]], months)
    rows = []
    for date, r in _month_end_rows(m).iterrows():
        w = {BONDS: 1.0}
        if r.notna().all():
            best = EQUITY if r[EQUITY] >= r[INTL] else INTL
            w = {best: 1.0} if r[best] > r[CASH] else {BONDS: 1.0}
        rows.append(pd.Series(w, name=date))
    return hold_monthly(pd.DataFrame(rows).fillna(0.0), prices.index)


def asset_trend(prices, n: int):
    """Faber-style: equal-weight each of five asset classes above its n-day average."""
    assets = [EQUITY, INTL, BONDS, GOLD, REIT]
    up = (prices[assets] > sma(prices[assets], n)).astype(float) / len(assets)
    return hold_monthly(_month_end_rows(up), prices.index)


def rotation(prices, universe: list[str], k: int, months: int, absolute: bool):
    m = mom(prices[universe + [CASH]], months)
    rows = []
    for date, r in _month_end_rows(m).iterrows():
        cand = r[universe].dropna()
        if absolute:
            cand = cand[cand > r[CASH]]
        top = cand.sort_values(ascending=False).index[:k]
        rows.append(pd.Series({t: 1.0 / k for t in top}, name=date))
    return hold_monthly(pd.DataFrame(rows).fillna(0.0), prices.index)


def inverse_vol(prices, with_trend: bool):
    rets = prices[ASSET_CLASSES].pct_change()
    vol = rets.rolling(60, min_periods=60).std()
    inv = 1.0 / vol
    w = inv.div(inv.sum(axis=1), axis=0)
    if with_trend:
        w = w * (prices[ASSET_CLASSES] > sma(prices[ASSET_CLASSES], 200))
    return hold_monthly(_month_end_rows(w.fillna(0.0)), prices.index)


def vol_target(prices, target: float):
    rv = prices[EQUITY].pct_change().rolling(20, min_periods=20).std() * math.sqrt(DAYS)
    w = pd.DataFrame({EQUITY: (target / rv).clip(upper=1.0)})
    return hold_monthly(_month_end_rows(w.fillna(0.0)), prices.index)


def rsi2_reversion(prices, entry: float, exit_at: float = 65.0):
    """Buy SPY after a sharp 2-day drop while its long trend is up; sell the bounce."""
    px = prices[EQUITY]
    delta = px.diff()
    up = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / down.replace(0, np.nan))
    trend_ok = (px > sma(prices[[EQUITY]], 200)[EQUITY]).to_numpy()
    r = rsi.to_numpy()
    pos = np.zeros(len(px))
    holding = False
    for i in range(len(px)):
        if not np.isnan(r[i]):
            if holding and r[i] > exit_at:
                holding = False
            elif not holding and r[i] < entry and trend_ok[i]:
                holding = True
        pos[i] = 1.0 if holding else 0.0
    return pd.DataFrame({EQUITY: pos}, index=prices.index)


@dataclass
class Strategy:
    name: str
    family: str
    build: object            # callable(prices) -> target weights

    def weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        return self.build(prices)


def candidates() -> list[Strategy]:
    out: list[Strategy] = []
    add = out.append
    for n in (100, 150, 200, 250):
        add(Strategy(f"SPY above its {n}-day average (checked monthly)", "trend",
                     lambda p, n=n: trend_spy(p, n)))
    add(Strategy("SPY above its 200-day average (checked daily)", "trend",
                 lambda p: trend_spy(p, 200, monthly=False)))
    for m in (3, 6, 9, 12):
        add(Strategy(f"dual momentum, {m}-month look-back", "dual momentum",
                     lambda p, m=m: dual_momentum(p, m)))
    for n in (150, 210, 250):
        add(Strategy(f"five asset classes above {n}-day average", "asset trend",
                     lambda p, n=n: asset_trend(p, n)))
    for k in (1, 2, 3, 4):
        for m in (3, 6, 12):
            for absolute in (False, True):
                tag = "+cash filter" if absolute else ""
                add(Strategy(f"sector momentum top {k}, {m}-month{tag and ' ' + tag}", "sector rotation",
                             lambda p, k=k, m=m, a=absolute: rotation(p, SECTORS, k, m, a)))
    for k in (2, 3, 4):
        for m in (3, 6, 12):
            for absolute in (False, True):
                tag = "+cash filter" if absolute else ""
                add(Strategy(f"asset-class momentum top {k}, {m}-month{tag and ' ' + tag}", "asset rotation",
                             lambda p, k=k, m=m, a=absolute: rotation(p, ASSET_CLASSES, k, m, a)))
    for trend in (False, True):
        add(Strategy("inverse-volatility asset classes" + (" + trend filter" if trend else ""),
                     "risk parity", lambda p, t=trend: inverse_vol(p, t)))
    for target in (0.10, 0.15):
        add(Strategy(f"SPY volatility-targeted to {target:.0%}", "vol target",
                     lambda p, t=target: vol_target(p, t)))
    for entry in (5, 10, 20):
        add(Strategy(f"SPY 2-day RSI under {entry} in an uptrend", "mean reversion",
                     lambda p, e=entry: rsi2_reversion(p, e)))
    return out


def benchmarks() -> list[Strategy]:
    def static(weights):
        return lambda p: hold_monthly(
            pd.DataFrame([weights], index=[p.index[0]]), p.index)

    return [
        Strategy("hold SPY", "benchmark", static({EQUITY: 1.0})),
        Strategy("60% SPY / 40% bonds", "benchmark", static({EQUITY: 0.6, BONDS: 0.4})),
        Strategy("equal-weight asset classes", "benchmark",
                 static({t: 1.0 / len(ASSET_CLASSES) for t in ASSET_CLASSES})),
    ]


# --- simulation ---------------------------------------------------------------


def simulate(prices: pd.DataFrame, target: pd.DataFrame, cost: float = DEFAULT_COST,
             start: str = EVAL_START) -> pd.Series:
    """Daily net returns. Weights set at the close of t earn day t+1's return."""
    rets = prices.pct_change()
    w = target.reindex(prices.index).fillna(0.0)
    w = w.reindex(columns=prices.columns, fill_value=0.0)
    w[CASH] = w[CASH] + (1.0 - w.sum(axis=1)).clip(lower=0.0)     # unused weight earns the cash fund
    held = w.shift(1).fillna(0.0)                                  # decided yesterday, earned today
    held = held.loc[start:]
    r = rets.loc[start:]
    if ((held > 1e-12) & r.isna()).to_numpy().any():
        raise ValueError("a strategy held an asset that had no price that day")
    gross = (held * r.fillna(0.0)).sum(axis=1)
    turnover = held.diff().abs().sum(axis=1).fillna(0.0)
    return gross - cost * turnover


# --- statistics ---------------------------------------------------------------


def perf(r: pd.Series, cash: pd.Series) -> dict:
    ex = r - cash.reindex(r.index).fillna(0.0)
    years = len(r) / DAYS
    wealth = (1 + r).cumprod()
    dd = (wealth / wealth.cummax() - 1).min()
    sd = float(ex.std())
    cagr = float(wealth.iloc[-1] ** (1 / years) - 1) if years > 0 else float("nan")
    return {
        "cagr": cagr,
        "vol": float(r.std() * math.sqrt(DAYS)),
        "sharpe": float(ex.mean() / sd * math.sqrt(DAYS)) if sd > 0 else float("nan"),
        "maxdd": float(dd),
    }


def factor_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """What a passive investor earns without any rule: SPY, and an equal mix of
    the asset classes. A rule only has alpha if it beats BOTH, because holding a
    bit of bonds and gold earns their premium with no skill at all."""
    rets = prices.pct_change()
    return pd.DataFrame({"spy": rets[EQUITY], "mix": rets[ASSET_CLASSES].mean(axis=1)})


def alpha_series(r: pd.Series, factors: pd.DataFrame, cash: pd.Series) -> tuple[pd.Series, np.ndarray]:
    """Daily return over cash with the passive factors' contribution removed.

    Returns the alpha series (intercept + noise) and the betas used.
    """
    c = cash.reindex(r.index).fillna(0.0)
    f = factors.reindex(r.index).sub(c, axis=0).fillna(0.0).to_numpy()
    ex = (r - c).to_numpy()
    design = np.column_stack([np.ones(len(ex)), f])
    coef, *_ = np.linalg.lstsq(design, ex, rcond=None)
    betas = coef[1:]
    return pd.Series(ex - f @ betas, index=r.index), betas


def _stationary_indices(t: int, draws: int, avg_block: float, rng: np.random.Generator) -> np.ndarray:
    restart = rng.random((draws, t)) < 1.0 / avg_block
    restart[:, 0] = True
    start = rng.integers(0, t, size=(draws, t))
    pos = np.arange(t)
    last = np.maximum.accumulate(np.where(restart, pos, 0), axis=1)
    return (np.take_along_axis(start, last, axis=1) + pos - last) % t


def reality_check(d: np.ndarray, draws: int = 1000, avg_block: float = 30.0,
                  seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """White's Reality Check on a (days x candidates) matrix of alpha series.

    Returns each candidate's studentised t and its FAMILY-CORRECTED p-value: the
    chance that the best of all these candidates would look at least this good
    if none had any edge. A candidate's own naive p-value is meaningless
    when 60 were tried; this one is not.
    """
    t_len, k = d.shape
    obs = d.mean(axis=0)
    rng = np.random.default_rng(seed)
    boot = np.empty((draws, k))
    chunk = 50
    for i in range(0, draws, chunk):
        n = min(chunk, draws - i)
        idx = _stationary_indices(t_len, n, avg_block, rng)
        boot[i:i + n] = d[idx].mean(axis=1)
    sd = boot.std(axis=0, ddof=1)
    sd[sd == 0] = np.nan
    t_obs = obs / sd
    t_boot = (boot - obs) / sd
    best = np.nanmax(t_boot, axis=1)
    p_adj = np.array([float((best >= t).mean()) if not np.isnan(t) else 1.0 for t in t_obs])
    return t_obs, p_adj


# --- the whole run ------------------------------------------------------------


@dataclass
class Row:
    name: str
    family: str
    dev: dict
    alpha_dev: float
    t_dev: float
    p_dev: float
    alpha_full: float
    t_full: float
    p_full: float
    passed: bool
    holdout: dict | None = None
    alpha_holdout: float | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run(prices: pd.DataFrame, cost: float = DEFAULT_COST, draws: int = 1000,
        seed: int = 11, split: str = SPLIT, strategies: list[Strategy] | None = None) -> dict:
    strategies = strategies if strategies is not None else candidates()
    cash_all = prices[CASH].pct_change()
    factors = factor_returns(prices)
    returns = {s.name: simulate(prices, s.weights(prices), cost) for s in strategies}
    bench = {s.name: simulate(prices, s.weights(prices), cost) for s in benchmarks()}
    frame = pd.DataFrame(returns)
    dev_mask = frame.index < pd.Timestamp(split)
    spy_window = bench["hold SPY"]

    def alphas(rows: pd.DataFrame) -> tuple[np.ndarray, list[float]]:
        cols, betas = [], []
        for name in rows:
            a, b = alpha_series(rows[name], factors, cash_all)
            cols.append(a.to_numpy())
            betas.append(b)
        return np.column_stack(cols), betas

    d_dev, _ = alphas(frame[dev_mask])
    d_full, _ = alphas(frame)
    t_dev, p_dev = reality_check(d_dev, draws, seed=seed)
    t_full, p_full = reality_check(d_full, draws, seed=seed)

    rows = []
    for i, s in enumerate(strategies):
        passed = bool(d_dev[:, i].mean() > 0 and p_dev[i] < PASS_P)
        row = Row(
            s.name, s.family, perf(frame.loc[dev_mask, s.name], cash_all),
            float(d_dev[:, i].mean() * DAYS), float(t_dev[i]), float(p_dev[i]),
            float(d_full[:, i].mean() * DAYS), float(t_full[i]), float(p_full[i]), passed,
        )
        if passed:                                    # the holdout is spent only on survivors
            hold = frame.loc[~dev_mask, s.name]
            a, _ = alpha_series(hold, factors, cash_all)
            row.holdout = perf(hold, cash_all)
            row.alpha_holdout = float(a.mean() * DAYS)
        rows.append(row)
    rows.sort(key=lambda r: r.p_dev)
    return {
        "rows": rows,
        "n_candidates": len(strategies),
        "benchmarks": {n: {"full": perf(r, cash_all),
                           "dev": perf(r[r.index < pd.Timestamp(split)], cash_all),
                           "holdout": perf(r[r.index >= pd.Timestamp(split)], cash_all)}
                       for n, r in bench.items()},
        "start": str(frame.index[0].date()), "end": str(frame.index[-1].date()), "split": split,
        "cost": cost, "spy_window": perf(spy_window, cash_all),
    }


# --- the words ----------------------------------------------------------------


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{digits}f}%"


def describe(res: dict, top: int = 12) -> list[str]:
    n, rows = res["n_candidates"], res["rows"]
    out = [
        "STRATEGY RESEARCH: DOES ANY RULE BEAT JUST HOLDING THE MARKET?",
        "-" * 78,
        f"{n} candidate rules on liquid ETFs, {res['start']} to {res['end']}. Judged on development",
        f"years before {res['split']}; later years are touched only by rules that pass.",
        f"Costs {res['cost'] * 1e4:.0f} bps per unit of turnover. 'alpha' = yearly return over cash after removing what",
        "plain SPY and a passive mix of asset classes would have earned anyway. 'p' is corrected",
        "for having tried every candidate: the chance the best of them looks this good by luck.",
        "",
        f"{'benchmark':<34}{'CAGR':>8}{'Sharpe':>8}{'worst drop':>12}",
    ]
    for name, b in res["benchmarks"].items():
        f = b["full"]
        out.append(f"{name:<34}{_pct(f['cagr']):>8}{f['sharpe']:>8.2f}{_pct(f['maxdd'], 0):>12}")
    out += ["", f"{'candidate (best first, development years)':<58}{'alpha':>8}{'t':>6}{'p':>7}"]
    for r in rows[:top]:
        out.append(f"{r.name[:57]:<58}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>7.2f}")
    survivors = [r for r in rows if r.passed]
    out.append("")
    if survivors:
        out.append(f"{len(survivors)} passed the development test (corrected p < {PASS_P:.2f}). "
                   "Their untouched holdout years:")
        for r in survivors:
            h = r.holdout
            out.append(f"  {r.name[:56]:<57} alpha {_pct(r.alpha_holdout)}  Sharpe {h['sharpe']:.2f}  "
                       f"worst drop {_pct(h['maxdd'], 0)}")
    else:
        out.append("None passed the development test, so the holdout years were never touched.")
    best_full = min(rows, key=lambda r: r.p_full)
    out += ["", "All years together (more data, but nothing is held back):",
            f"  best candidate: {best_full.name}",
            f"  alpha {_pct(best_full.alpha_full)} a year, family-corrected p = {best_full.p_full:.2f}"]
    return out
