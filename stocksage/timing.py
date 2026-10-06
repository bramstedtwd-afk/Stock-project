"""Timing lab: does any rule for holding ONE fund beat simply holding it?

The question the owner asked: across sector and industry ETFs, is there a
strategy with an edge that shows up consistently, when does it work, and what
should we do from here.

Every candidate turns one fund's past closes into a daily exposure: 0 (all in
T-bills), 1 (fully in the fund), or more (borrowed money, like a 2x fund).
~40 candidates, from trend filters and momentum to volatility targeting,
dip-leveraging and short-term mean reversion, each at 1x and with leverage.

How it avoids fooling itself:

  * NO LOOK-AHEAD. Exposure decided at the close of day t earns day t+1.
    tests/test_timing.py scrambles future prices and checks earlier exposures
    do not move.
  * REAL COSTS. 5 bps per unit of turnover. Borrowed exposure pays the T-bill
    rate + 0.75% + a 0.9% fund fee per unit of leverage: calibrated so a
    simulated 2x SPY/QQQ lands within 0.15 point a year of the real SSO/QLD
    funds over 2006-2026. Idle money earns the T-bill rate.
  * LEVERAGE IS NOT SKILL. A rule that holds 2x for most of a rising market
    beats holding 1x without any timing ability. So each rule is also judged
    against the same fund held at a CONSTANT exposure with the same
    volatility ("risk-matched hold"). Beating that is timing skill; beating
    only the 1x hold is leverage.
  * MANY TRIES. ~40 rules x ~30 funds. The verdict uses White's Reality Check
    (from research.py) on the panel-average daily edge across funds, so the
    best rule is judged against the best that luck alone could produce.
  * OUT OF SAMPLE TWICE. Rules are ranked on the years before 2017 and the
    later years are only looked at afterwards; separately, they are ranked on
    the broad and sector funds and checked on the industry funds, which were
    never used to choose.

Pure maths over pandas/numpy; the network lives in `load_prices`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .research import reality_check

DAYS = 252
DEFAULT_COST = 0.0005          # per unit of exposure traded
BORROW_SPREAD = 0.0075         # over T-bills, per unit borrowed (calibrated vs SSO/QLD)
LEVER_FEE = 0.009              # leveraged-fund expense, per unit of extra exposure
SPLIT = "2017-01-01"
PASS_P = 0.10
RATE_TICKER = "^IRX"
WARMUP = 260                   # trading days of history every rule needs first
MIN_YEARS = 8.0

UNIVERSE = {
    # broad market
    "SPY": "broad", "QQQ": "broad", "DIA": "broad", "IWM": "broad", "MDY": "broad",
    "EFA": "broad", "EEM": "broad",
    # the nine original sector funds
    "XLB": "sector", "XLE": "sector", "XLF": "sector", "XLI": "sector", "XLK": "sector",
    "XLP": "sector", "XLU": "sector", "XLV": "sector", "XLY": "sector",
    # industries (the out-of-sample set: never used to choose a rule)
    "SMH": "industry", "SOXX": "industry", "IBB": "industry", "XBI": "industry",
    "IGV": "industry", "IYR": "industry", "IYT": "industry", "KRE": "industry",
    "KBE": "industry", "XHB": "industry", "ITB": "industry", "XME": "industry",
    "XOP": "industry", "XRT": "industry", "GDX": "industry", "IHI": "industry",
}


# --- data ---------------------------------------------------------------------


def load_prices(tickers: list[str] | None = None, cache_path: Path | None = None,
                max_age_hours: float = 12.0) -> tuple[pd.DataFrame, pd.Series]:
    """Adjusted closes for the universe and the T-bill rate (decimal a year)."""
    import time

    tickers = tickers or list(UNIVERSE)
    raw = None
    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                raw = pd.read_parquet(cache_path)
                if not all(t in raw for t in tickers):
                    raw = None
            except Exception:
                raw = None
    if raw is None:
        import yfinance as yf

        got = yf.download(sorted({*tickers, RATE_TICKER}), period="max",
                          auto_adjust=True, progress=False)
        raw = got["Close"] if "Close" in got else got
        if cache_path is not None:
            try:
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
                raw.to_parquet(cache_path)
            except Exception:
                pass
    missing = [t for t in tickers if t not in raw or raw[t].dropna().empty]
    if missing:
        raise RuntimeError(f"no price data for {', '.join(missing)}")
    rate = (raw[RATE_TICKER] / 100.0).clip(0.0, 0.2) if RATE_TICKER in raw else pd.Series(0.0, index=raw.index)
    return raw[tickers], rate


# --- rules: closes -> exposure decided at each close ---------------------------


def _sma(c, n):
    return c.rolling(n, min_periods=n).mean()


def _month_end(c: pd.Series) -> pd.Series:
    idx = c.index
    return pd.Series(np.r_[idx[1:].month != idx[:-1].month, True], index=idx)


def _monthly(signal: pd.Series, c: pd.Series) -> pd.Series:
    """Only act on the last trading day of each month; hold in between."""
    return signal.where(_month_end(c)).ffill()


def rsi(c: pd.Series, n: int = 2) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def _latch(enter: pd.Series, leave: pd.Series) -> pd.Series:
    """1 from an enter day until the next leave day."""
    state, out = 0.0, np.zeros(len(enter))
    en, lv = enter.to_numpy(bool), leave.to_numpy(bool)
    for i in range(len(out)):
        if state == 0.0 and en[i]:
            state = 1.0
        elif state == 1.0 and lv[i]:
            state = 0.0
        out[i] = state
    return pd.Series(out, index=enter.index)


def _hold_after(trigger: pd.Series, days: int) -> pd.Series:
    """1 on the trigger day and the next days-1 days."""
    t = trigger.astype(float)
    return (t.rolling(days, min_periods=1).max() > 0).astype(float)


def _vol(c, n=20):
    return c.pct_change().rolling(n, min_periods=n).std() * math.sqrt(DAYS)


@dataclass(frozen=True)
class Rule:
    name: str
    family: str
    fn: object          # closes, rate -> exposure Series (decided at each close)
    lever: float = 1.0  # most exposure it can take


def _trend(n, lev):
    return lambda c, rf: lev * (c > _sma(c, n)).astype(float)


def _trend_monthly(n, lev):
    return lambda c, rf: lev * _monthly((c > _sma(c, n)).astype(float), c)


def _trend_floor(n, lev):
    """Always in the fund: `lev` while above the n-day average (checked monthly), 1x below."""
    return lambda c, rf: 1.0 + (lev - 1.0) * _monthly((c > _sma(c, n)).astype(float), c).fillna(0.0)


def _cross(lev):
    return lambda c, rf: lev * (_sma(c, 50) > _sma(c, 200)).astype(float)


def _absmom(months, lev):
    def f(c, rf):
        back = c / c.shift(21 * months) - 1
        bills = rf * months / 12            # what T-bills paid over the same stretch, roughly
        return lev * _monthly((back > bills).astype(float), c)
    return f


def _voltarget(target, cap, with_trend=False):
    def f(c, rf):
        e = (target / _vol(c)).clip(upper=cap)
        if with_trend:
            e = e * (c > _sma(c, 200))
        return e
    return f


def _dip(level, lev):
    return lambda c, rf: pd.Series(np.where(c <= (1 - level) * c.rolling(252, min_periods=60).max(), lev, 1.0),
                                   index=c.index)


def _rsi2(enter=10, leave=70):
    return lambda c, rf: _latch(rsi(c, 2) < enter, rsi(c, 2) > leave)


def _rsi2_trend(enter=10, leave=70):
    return lambda c, rf: _latch((rsi(c, 2) < enter) & (c > _sma(c, 200)), rsi(c, 2) > leave)


def _after_red(n_red, hold):
    def f(c, rf):
        red = c.pct_change() < 0
        streak = red.rolling(n_red, min_periods=n_red).sum() == n_red
        return _hold_after(streak, hold)
    return f


def _tilt_after_red(n_red, hold, lev):
    def f(c, rf):
        red = c.pct_change() < 0
        streak = red.rolling(n_red, min_periods=n_red).sum() == n_red
        return 1.0 + (lev - 1.0) * _hold_after(streak, hold)
    return f


def rules() -> list[Rule]:
    out = []
    for n in (50, 100, 150, 200, 250):
        for lev in (1.0, 1.5, 2.0):
            out.append(Rule(f"Trend {n}d @{lev:g}x", "trend", _trend(n, lev), lev))
    for n in (100, 150, 200, 250):
        for lev in (1.0, 1.5, 2.0):
            out.append(Rule(f"Trend {n}d monthly @{lev:g}x", "trend monthly", _trend_monthly(n, lev), lev))
    for lev in (1.5, 2.0):
        out.append(Rule(f"{lev:g}x above 200d, 1x below (monthly)", "trend monthly",
                        _trend_floor(200, lev), lev))
    for lev in (1.0, 1.5, 2.0):
        out.append(Rule(f"Golden cross 50/200 @{lev:g}x", "trend", _cross(lev), lev))
        out.append(Rule(f"12-month momentum @{lev:g}x", "momentum", _absmom(12, lev), lev))
        out.append(Rule(f"6-month momentum @{lev:g}x", "momentum", _absmom(6, lev), lev))
    for target in (0.10, 0.15, 0.20):
        out.append(Rule(f"Vol target {target:.0%} (max 2x)", "vol target", _voltarget(target, 2.0), 2.0))
        out.append(Rule(f"Vol target {target:.0%} + trend 200d", "vol target", _voltarget(target, 2.0, True), 2.0))
    out.append(Rule("1.5x in 10%+ dips", "dip leverage", _dip(0.10, 1.5), 1.5))
    out.append(Rule("2x in 20%+ dips", "dip leverage", _dip(0.20, 2.0), 2.0))
    out.append(Rule("RSI(2) dip buy", "mean reversion", _rsi2()))
    out.append(Rule("RSI(2) dip buy above 200d", "mean reversion", _rsi2_trend()))
    out.append(Rule("After 3 red days, hold 5", "mean reversion", _after_red(3, 5)))
    out.append(Rule("1.5x for 5 days after 3 red days", "mean reversion", _tilt_after_red(3, 5, 1.5), 1.5))
    return out


# --- simulation ---------------------------------------------------------------


def daily_rf(rate: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Cash return earned on each day (the previous close's T-bill yield)."""
    gap = index.to_series().diff().dt.days.fillna(1).to_numpy()
    r = rate.reindex(index).ffill().fillna(0.0).shift(1).fillna(0.0).to_numpy()
    return pd.Series(r * gap / 365.0, index=index)


def returns_for(close: pd.Series, exposure, rf: pd.Series, cost: float = DEFAULT_COST) -> pd.Series:
    """Daily net returns of holding `exposure` (decided at each close) in one fund."""
    r = close.pct_change().fillna(0.0)
    e = pd.Series(exposure, index=close.index).astype(float).fillna(0.0)
    held = e.shift(1).fillna(0.0)
    borrowed = (held - 1.0).clip(lower=0.0)
    idle = (1.0 - held).clip(lower=0.0)
    gap = close.index.to_series().diff().dt.days.fillna(1).to_numpy()
    carry = borrowed * (rf + (BORROW_SPREAD + LEVER_FEE) * gap / 365.0)
    trade = held.diff().abs().fillna(held.abs())
    return held * r + idle * rf - carry - cost * trade


def constant(close: pd.Series, level: float, rf: pd.Series, cost: float = DEFAULT_COST) -> pd.Series:
    return returns_for(close, pd.Series(level, index=close.index), rf, cost)


def stats(ret: pd.Series, rf: pd.Series) -> dict:
    if len(ret) < 2:
        return {}
    yrs = (ret.index[-1] - ret.index[0]).days / 365.25
    w = (1 + ret).cumprod()
    ex = ret - rf.reindex(ret.index).fillna(0.0)
    sd = float(ex.std())
    return {
        "cagr": float(w.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 and w.iloc[-1] > 0 else -1.0,
        "vol": float(ret.std() * math.sqrt(DAYS)),
        "sharpe": float(ex.mean() / sd * math.sqrt(DAYS)) if sd > 0 else float("nan"),
        "maxdd": float((w / w.cummax() - 1).min()),
        "years": round(yrs, 1),
    }


def matched_level(strategy: pd.Series, hold: pd.Series) -> float:
    """The constant exposure whose volatility equals the strategy's."""
    hv = float(hold.std())
    return float(strategy.std()) / hv if hv > 0 else 1.0


# --- the whole run ------------------------------------------------------------


def _cagr_gap(a: pd.Series, b: pd.Series) -> float:
    yrs = (a.index[-1] - a.index[0]).days / 365.25
    ga, gb = float((1 + a).prod()), float((1 + b).prod())
    if yrs <= 0 or ga <= 0 or gb <= 0:
        return float("nan")
    return ga ** (1 / yrs) - gb ** (1 / yrs)


def evaluate_fund(close: pd.Series, rate: pd.Series, rule_list: list[Rule],
                  cost: float = DEFAULT_COST, split: str = SPLIT) -> dict:
    """Every rule on one fund: full period, before/after the split, risk-matched."""
    close = close.dropna()
    close = close[close > 0]
    rf_all = daily_rf(rate, close.index)
    start = close.index[min(WARMUP, len(close) - 1)]
    window = close.index >= start
    rf = rf_all[window]
    hold = constant(close, 1.0, rf_all, cost)[window]
    out = {"start": str(start.date()), "end": str(close.index[-1].date()),
           "hold": stats(hold, rf), "rules": {}, "series": {}}
    dev = hold.index < pd.Timestamp(split)
    for rule in rule_list:
        e = rule.fn(close, rate.reindex(close.index).ffill().fillna(0.0))
        ret = returns_for(close, e, rf_all, cost)[window]
        expo = pd.Series(e, index=close.index).shift(1).fillna(0.0)[window]
        row = {"full": stats(ret, rf), "in_market": float((expo > 0).mean()),
               "avg_exposure": float(expo.mean()),
               "turnover": float(expo.diff().abs().sum() / max(stats(ret, rf)["years"], 1e-9))}
        parts = {}
        for part, mask in (("full", np.ones(len(ret), bool)), ("dev", dev), ("holdout", ~dev)):
            if mask.sum() < DAYS:
                continue
            r_p, h_p = ret[mask], hold[mask]
            k = matched_level(r_p, h_p)
            m_p = constant(close, k, rf_all, cost)[window][mask]
            parts[part] = {"vs_hold": _cagr_gap(r_p, h_p), "vs_matched": _cagr_gap(r_p, m_p),
                           "level": k, "stats": stats(r_p, rf[mask])}
            if part == "full":
                out["series"][rule.name] = (r_p - m_p, r_p - h_p)
        row.update(parts)
        out["rules"][rule.name] = row
    return out


def _panel(per_fund: dict, names: list[str], which: int, mask_fn=None) -> pd.DataFrame:
    """Days x rules: the average daily edge across the funds trading that day."""
    cols = {}
    for name in names:
        series = [f["series"][name][which] for f in per_fund.values() if name in f["series"]]
        frame = pd.concat(series, axis=1)
        cols[name] = frame.mean(axis=1, skipna=True)
    out = pd.DataFrame(cols).dropna(how="all").fillna(0.0)
    return out if mask_fn is None else out[mask_fn(out.index)]


def regimes(spy: pd.Series) -> pd.DataFrame:
    """Market state known at the close BEFORE each day: SPY trend and volatility."""
    up = (spy > _sma(spy, 200)).shift(1)
    vol = _vol(spy).shift(1)
    calm = pd.cut(vol, [0, 0.15, 0.25, 10], labels=["calm", "normal", "stormy"])
    return pd.DataFrame({"trend": up.map({True: "SPY above 200d", False: "SPY below 200d"}),
                         "vol": calm.astype(str)}, index=spy.index)


ERAS = [("Dot-com bust", "2000-03-24", "2002-10-09"), ("2003-07 bull", "2002-10-10", "2007-10-09"),
        ("2008 crash", "2007-10-10", "2009-03-09"), ("2009-19 bull", "2009-03-10", "2019-12-31"),
        ("Covid crash", "2020-01-01", "2020-04-30"), ("2020-21 rebound", "2020-05-01", "2021-12-31"),
        ("2022 bear", "2022-01-01", "2022-12-31"), ("2023-26", "2023-01-01", "2099-12-31")]


def run(prices: pd.DataFrame, rate: pd.Series, draws: int = 1000, cost: float = DEFAULT_COST,
        split: str = SPLIT, rule_list: list[Rule] | None = None, groups: dict | None = None) -> dict:
    rule_list = rule_list or rules()
    groups = groups or {t: UNIVERSE.get(t, "other") for t in prices}
    names = [r.name for r in rule_list]
    per_fund = {}
    for t in prices:
        c = prices[t].dropna()
        if len(c) < WARMUP + DAYS * MIN_YEARS:
            continue
        per_fund[t] = evaluate_fund(c, rate, rule_list, cost, split)

    def panel_stats(funds, which, mask_fn=None):
        sub = {t: per_fund[t] for t in funds}
        p = _panel(sub, names, which, mask_fn)
        if len(p) < DAYS:              # e.g. no years after the split yet
            nan = float("nan")
            return p, {n: {"edge": nan, "t": nan, "p": nan} for n in names}
        t_stat, p_adj = reality_check(p.to_numpy(), draws=draws)
        return p, {n: {"edge": float(p[n].mean() * DAYS), "t": float(t_stat[i]), "p": float(p_adj[i])}
                   for i, n in enumerate(names)}

    funds = list(per_fund)
    choose = [t for t in funds if groups[t] in ("broad", "sector")]
    confirm = [t for t in funds if groups[t] == "industry"]
    before = lambda idx: idx < pd.Timestamp(split)  # noqa: E731
    after = lambda idx: idx >= pd.Timestamp(split)  # noqa: E731

    panel_all, verdict_all = panel_stats(funds, 0)
    _, verdict_dev = panel_stats(funds, 0, before)
    _, verdict_hold = panel_stats(funds, 0, after)
    _, verdict_choose = panel_stats(choose, 0) if choose else (None, {})
    _, verdict_confirm = panel_stats(confirm, 0) if confirm else (None, {})
    _, raw_all = panel_stats(funds, 1)

    # when does each rule help? average risk-matched edge by market state and era
    when = {}
    if "SPY" in prices:
        reg = regimes(prices["SPY"].dropna()).reindex(panel_all.index)
        for col in ("trend", "vol"):
            when[col] = {state: {n: float(panel_all.loc[reg[col] == state, n].mean() * DAYS) for n in names}
                         for state in sorted(reg[col].dropna().unique()) if state != "nan"}
    when["era"] = {}
    for label, a, b in ERAS:
        part = panel_all.loc[a:b]
        if len(part) > 20:
            yrs = (part.index[-1] - part.index[0]).days / 365.25
            when["era"][label] = {n: float(part[n].mean() * DAYS) for n in names} | {"_years": yrs}

    summary = []
    for rule in rule_list:
        n = rule.name
        rows = [per_fund[t]["rules"][n] for t in funds]
        summary.append({
            "name": n, "family": rule.family, "lever": rule.lever,
            "edge": verdict_all[n]["edge"], "p": verdict_all[n]["p"], "t": verdict_all[n]["t"],
            "edge_dev": verdict_dev[n]["edge"], "p_dev": verdict_dev[n]["p"],
            "edge_holdout": verdict_hold[n]["edge"], "p_holdout": verdict_hold[n]["p"],
            "edge_choose": verdict_choose.get(n, {}).get("edge"), "edge_confirm": verdict_confirm.get(n, {}).get("edge"),
            "p_confirm": verdict_confirm.get(n, {}).get("p"),
            "raw_edge": raw_all[n]["edge"],
            "funds_beating_hold": sum(r["full"]["vs_hold"] > 0 for r in rows if "vs_hold" in r.get("full", {})),
            "funds_beating_matched": sum(r["full"]["vs_matched"] > 0 for r in rows if "vs_matched" in r.get("full", {})),
            "funds_beating_both_halves": sum(
                ("dev" in r and "holdout" in r and r["dev"]["vs_matched"] > 0 and r["holdout"]["vs_matched"] > 0)
                for r in rows),
            "funds": len(rows),
            "median_vs_hold": float(np.nanmedian([r["full"]["vs_hold"] for r in rows])),
            "median_vs_matched": float(np.nanmedian([r["full"]["vs_matched"] for r in rows])),
            "median_maxdd": float(np.nanmedian([r["full"]["stats"]["maxdd"] for r in rows])),
            "passed": bool(verdict_dev[n]["edge"] > 0 and verdict_dev[n]["p"] < PASS_P),
        })
    summary.sort(key=lambda s: s["p_dev"])
    for f in per_fund.values():
        f.pop("series", None)
    return {"summary": summary, "funds": per_fund, "when": when, "groups": {t: groups[t] for t in funds},
            "split": split, "cost": cost, "n_rules": len(rule_list), "n_funds": len(funds),
            "choose": choose, "confirm": confirm}


# --- plain-language summary ---------------------------------------------------


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x * 100:+.{digits}f}%"


def describe(res: dict, top: int = 10) -> list[str]:
    lines = [f"{res['n_rules']} rules x {res['n_funds']} funds. Edge = yearly return over holding the same fund",
             "at the same risk (constant exposure with equal volatility). p = chance the best of all rules",
             f"looks this good by luck (White's Reality Check). Ranked on years before {res['split'][:4]}.",
             "vs 1x = typical fund's yearly growth minus plain holding. funds+ = funds ahead at the same risk.", ""]
    lines.append(f"  {'rule':<36} {'edge':>7} {'p':>5} {'pre':>7} {'post':>7} {'vs 1x':>7} {'funds+':>7}")
    for s in res["summary"][:top]:
        lines.append(f"  {s['name']:<36} {_pct(s['edge']):>7} {s['p']:>5.2f} {_pct(s['edge_dev']):>7} "
                     f"{_pct(s['edge_holdout']):>7} {_pct(s['median_vs_hold']):>7} "
                     f"{s['funds_beating_matched']:>3}/{s['funds']}")
    passed = [s["name"] for s in res["summary"] if s["passed"]]
    lines += ["", "Passed on the early years (corrected p < 0.10): " + (", ".join(passed) if passed else "none")]
    return lines
