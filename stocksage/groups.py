"""Groups lab: rules across industries, countries and styles, plus the calendar.

Funds instead of single stocks on purpose: a fund that tracks an industry or a
country does not vanish from the data when its members fail, so the test is
free of the survivorship that flatters stock lists.

  GROUPS   ~40 US industry funds, ~25 country funds, 9 style/size funds.
  RULES    within each group: 12-1, 6- and 3-month momentum (best few), short-term
           reversal (last month's losers), nearness to the 52-week high, lowest
           volatility; each with and without a "beat cash" filter.
  CALENDAR turn of the month, "sell in May" (hold November-April), the session
           before a market holiday; on SPY, small caps and each group.

Alpha is measured against BOTH holding SPY and holding the whole group in equal
weight, so a rule only scores if it beats owning the group passively. One luck
correction covers every rule in every group; later years are spent only on
survivors, with the same rising bar as the other labs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .research import CASH, hold_monthly, month_end_mask, simulate

GROUPS = {
    "US industries": ["XBI", "XHB", "XRT", "KRE", "KBE", "XME", "XOP", "XES", "KIE", "XPH", "XSD", "IBB", "IYT",
                      "ITA", "ITB", "IHI", "SOXX", "IGV", "IYR", "IYZ", "IHF", "IAI", "IAT", "IEO", "IEZ", "GDX",
                      "OIH", "PBJ", "PEJ", "IYW", "IYE", "IYM", "IYK", "IYC", "IYJ", "IDU", "IYH", "IYF", "XLU", "XLP"],
    "countries": ["EWA", "EWC", "EWD", "EWG", "EWH", "EWI", "EWJ", "EWK", "EWL", "EWN", "EWO", "EWP", "EWQ",
                  "EWS", "EWU", "EWW", "EWY", "EWT", "EWZ", "EZA", "ECH", "EWM", "SPY", "EIS", "THD"],
    "styles": ["IWD", "IWF", "IWN", "IWO", "IWM", "MDY", "IJR", "RSP", "SPY"],
}
START = "2007-06-01"
SPLIT = "2016-01-01"
PASS_P = 0.10
MONTH = 21


def all_tickers() -> list[str]:
    return sorted({t for g in GROUPS.values() for t in g} | {"SPY", CASH})


def load_prices(cache_path: Path | None = None) -> pd.DataFrame:
    if cache_path is not None and Path(cache_path).exists():
        return pd.read_parquet(cache_path)
    import yfinance as yf

    raw = yf.download(all_tickers(), start="2004-01-01", auto_adjust=True, progress=False)
    px = (raw["Close"] if "Close" in raw else raw).sort_index().dropna(how="all")
    if cache_path is not None:
        px.to_parquet(cache_path)
    return px


# --- rules: prices -> target weights -------------------------------------------------


def _rank_pick(prices, members, score: pd.DataFrame, k: int, higher_better: bool, beat_cash: bool):
    me = month_end_mask(prices.index)
    cash_mom = prices[CASH] / prices[CASH].shift(MONTH * 12) - 1
    rows = []
    for date in prices.index[me]:
        s = score.loc[date, members].dropna()
        if len(s) < max(k + 2, 5):
            rows.append(pd.Series(dtype=float, name=date))
            continue
        s = s.sort_values(ascending=not higher_better)
        top = list(s.index[:k])
        if beat_cash:
            m12 = prices.loc[date, top] / prices[top].shift(MONTH * 12).loc[date] - 1
            top = [t for t in top if m12.get(t, -1) > cash_mom.get(date, 0)]
        rows.append(pd.Series({t: 1.0 / k for t in top}, name=date))
    return hold_monthly(pd.DataFrame(rows).fillna(0.0), prices.index)


def momentum(months: int, skip: int = 0):
    def score(prices):
        return prices.shift(MONTH * skip) / prices.shift(MONTH * months) - 1
    return score


def reversal(prices):
    return prices / prices.shift(MONTH) - 1            # lower = last month's losers


def near_high(prices):
    return prices / prices.rolling(252, min_periods=200).max()


def low_vol(prices):
    return prices.pct_change().rolling(63, min_periods=50).std()


@dataclass
class Rule:
    name: str
    group: str
    build: object


def group_rules() -> list[Rule]:
    out = []
    for group, members in GROUPS.items():
        k = 3 if len(members) > 15 else 2
        specs = (("12-1 month momentum", momentum(12, 1), True), ("6-month momentum", momentum(6), True),
                 ("3-month momentum", momentum(3), True), ("last month's losers (reversal)", reversal, False),
                 ("nearest their 52-week high", near_high, True), ("lowest volatility", low_vol, False))
        for label, fn, hi in specs:
            for beat in (False, True):
                tag = " + beat-cash filter" if beat else ""
                out.append(Rule(f"{group}: top {k} by {label}{tag}", group,
                                lambda p, m=members, fn=fn, hi=hi, k=k, beat=beat: _rank_pick(p, m, fn(p), k, hi, beat)))
    return out


def _calendar_weights(prices, members, mask: pd.Series):
    w = pd.DataFrame(0.0, index=prices.index, columns=members)
    have = prices[members].notna()
    n = have.sum(axis=1).replace(0, np.nan)
    for t in members:
        w[t] = np.where(mask & have[t], 1.0 / n, 0.0)
    return w.fillna(0.0)


def turn_of_month(index: pd.DatetimeIndex) -> pd.Series:
    """True for positions held over the last session of a month and the first three of the next.
    The weight set at a close earns the next session, so we hold from the close two sessions
    before month end through the close of the third session."""
    me = month_end_mask(index)
    pos = np.arange(len(index))
    last = pd.Series(np.where(me, pos, np.nan), index=index).bfill().to_numpy()
    prev_last = pd.Series(np.where(me, pos, np.nan), index=index).ffill().shift(1).to_numpy()
    to_end = last - pos
    since = pos - prev_last
    return pd.Series((to_end <= 1) | (since <= 2), index=index).fillna(False)


def sell_in_may(index: pd.DatetimeIndex) -> pd.Series:
    nxt = pd.Series(index, index=index).shift(-1).fillna(index[-1])
    return pd.Series(pd.DatetimeIndex(nxt).month.isin([11, 12, 1, 2, 3, 4]), index=index)


def pre_holiday(index: pd.DatetimeIndex) -> pd.Series:
    """Hold over the session BEFORE a weekday market holiday (gap > 1 day not explained by a weekend)."""
    d = pd.Series(index, index=index)
    gap = (d.shift(-1) - d).dt.days
    weekday_gap = (gap > 1) & ~((d.dt.weekday == 4) & (gap == 3))
    # weight set at close t earns t+1: to earn the pre-holiday session (t+1), set at t.
    return weekday_gap.shift(-1).fillna(False).astype(bool)


def calendar_rules() -> list[Rule]:
    out = []
    sets = {"SPY": ["SPY"], "small caps": ["IWM"], **{g: m for g, m in GROUPS.items()}}
    for gname, members in sets.items():
        for label, fn in (("turn of the month", turn_of_month), ("November-April only", sell_in_may),
                          ("session before a holiday", pre_holiday)):
            out.append(Rule(f"{gname}: {label}", gname,
                            lambda p, m=members, fn=fn: _calendar_weights(p, [t for t in m if t in p], fn(p.index))))
    return out


# --- judging ---------------------------------------------------------------------------


@dataclass
class Row:
    name: str
    group: str
    dev: dict
    full: dict
    alpha_dev: float
    t_dev: float
    p_dev: float
    passed: bool
    holdout: dict | None = None
    alpha_holdout: float | None = None
    t_holdout: float | None = None
    beats_spy: bool | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run(prices: pd.DataFrame, draws: int = 2000, seed: int = 11, split: str = SPLIT, start: str = START,
        rule_list: list[Rule] | None = None, cost: float = 0.0005) -> dict:
    from .optionslab import _metrics
    from .research import alpha_series, reality_check

    rule_list = rule_list if rule_list is not None else group_rules() + calendar_rules()
    rets = pd.DataFrame({r.name: simulate(prices, r.build(prices), cost, start=start) for r in rule_list})
    days = rets.index
    cash = prices[CASH].pct_change().reindex(days).fillna(0.0)
    spy = prices["SPY"].pct_change().reindex(days)
    group_mix = {g: prices[[t for t in m if t in prices]].pct_change().mean(axis=1).reindex(days)
                 for g, m in GROUPS.items()}
    group_mix["SPY"] = spy
    group_mix["small caps"] = prices["IWM"].pct_change().reindex(days)
    dev = days < pd.Timestamp(split)

    def factors(r: Rule):
        return pd.DataFrame({"spy": spy, "group": group_mix[r.group]})

    def alpha(r, mask):
        return alpha_series(rets.loc[mask, r.name], factors(r)[mask], cash[mask])[0]

    t_dev, p_dev = reality_check(np.column_stack([alpha(r, dev).to_numpy() for r in rule_list]), draws, seed=seed)
    bench_hold = _metrics(spy[~dev].fillna(0), cash[~dev])
    rows = []
    for k, r in enumerate(rule_list):
        a_dev = alpha(r, dev)
        passed = bool(a_dev.mean() > 0 and p_dev[k] < PASS_P)
        row = Row(r.name, r.group, _metrics(rets.loc[dev, r.name], cash[dev]), _metrics(rets[r.name], cash),
                  float(a_dev.mean() * 252), float(t_dev[k]), float(p_dev[k]), passed)
        if passed:
            a_h = alpha(r, ~dev)
            row.holdout = _metrics(rets.loc[~dev, r.name], cash[~dev])
            row.alpha_holdout = float(a_h.mean() * 252)
            row.t_holdout = float(a_h.mean() / a_h.std() * math.sqrt(len(a_h))) if a_h.std() > 0 else 0.0
            row.beats_spy = bool(row.holdout["sharpe"] >= bench_hold["sharpe"])
        rows.append(row)
    rows.sort(key=lambda r: r.p_dev)
    return {"rows": rows, "n": len(rule_list), "start": str(days[0].date()), "end": str(days[-1].date()),
            "split": split, "bench": bench_hold}


def strong(rows: list[Row]) -> list[Row]:
    from .drivers import holdout_bar

    bar = holdout_bar(rows)
    return [r for r in rows if r.passed and r.t_holdout is not None and r.t_holdout > bar and r.beats_spy]


def _pct(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def describe(res: dict, top: int = 15) -> list[str]:
    from .drivers import holdout_bar

    out = ["GROUPS LAB: INDUSTRIES, COUNTRIES, STYLES AND THE CALENDAR", "=" * 80,
           f"{res['n']} rules, {res['start']} to {res['end']}, ranked on years before {res['split'][:4]}. "
           "Alpha is against SPY AND the group held in equal weight.",
           f"Later-years bar: alpha t above {holdout_bar(res['rows']):.2f} and Sharpe at least SPY's "
           f"({res['bench']['sharpe']:.2f}).",
           f"{'  rule (best first, development years)':<72}{'alpha':>8}{'t':>6}{'p':>6}"]
    for r in res["rows"][:top]:
        out.append(f"  {r.name[:69]:<70}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}")
    surv = [r for r in res["rows"] if r.passed]
    if not surv:
        out.append("  None passed the development test, so the later years were never touched.")
    for r in surv:
        h = r.holdout
        out.append(f"  PASSED: {r.name[:60]}: later alpha {_pct(r.alpha_holdout)} (t {r.t_holdout:.1f}), "
                   f"Sharpe {h['sharpe']:.2f}, CAGR {_pct(h['cagr'])}")
    win = strong(res["rows"])
    out += ["", "RESULT: " + ("; ".join(w.name for w in win) + " cleared every bar. Paper-trade it first."
                              if win else "nothing cleared every bar.")]
    return out
