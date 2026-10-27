"""Earnings drift: do stocks keep moving after a surprise, in which industries?

The best-documented stock anomaly is post-earnings-announcement drift: after a
company beats (or misses) expectations, its shares tend to keep drifting the
same way for weeks. This tests it on S&P 500 companies since 2002, industry by
industry, with yfinance's earnings history (date, analyst estimate, reported
EPS, surprise) and daily prices.

Honesty details:
  * TIMING. An announcement after 4pm moves the NEXT session; an unknown time is
    treated as after the close. The position is bought at the close of the
    session AFTER the reaction day, so nothing depends on fill-at-the-news.
  * THRESHOLDS from the past only: "a big surprise" means bigger than 80% of the
    surprises in the previous 365 days.
  * SURVIVORSHIP. The list is today's S&P 500, which leaves out companies that
    shrank, merged or failed. To limit the damage, a company's earnings count
    only AFTER it joined the index. A long-only result is still flattered.
  * The portfolio is investable in principle: equal weight in every live
    position, cash (T-bills) when there are none, judged against holding SPY,
    with the same luck correction and untouched later years as the other labs.

Network lives in the loaders; everything else is pure.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

UNIVERSE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SECTOR_ETF = {
    "Information Technology": "XLK", "Health Care": "XLV", "Financials": "XLF",
    "Consumer Discretionary": "XLY", "Communication Services": "XLC", "Industrials": "XLI",
    "Consumer Staples": "XLP", "Energy": "XLE", "Utilities": "XLU", "Real Estate": "XLRE",
    "Materials": "XLB",
}
START = "2003-01-01"
SPLIT = "2015-01-01"
PASS_P = 0.10


# --- data ---------------------------------------------------------------------------


def load_universe(cache_path: Path | None = None) -> pd.DataFrame:
    """Ticker, sector, industry and the date it joined the S&P 500."""
    if cache_path is not None and Path(cache_path).exists():
        return pd.read_csv(cache_path)
    u = pd.read_csv(UNIVERSE_URL)
    u = u.rename(columns={"Symbol": "ticker", "GICS Sector": "sector", "GICS Sub-Industry": "industry",
                          "Date added": "added"})[["ticker", "sector", "industry", "added"]]
    u["ticker"] = u["ticker"].str.replace(".", "-", regex=False)
    if cache_path is not None:
        u.to_csv(cache_path, index=False)
    return u


def load_events(tickers: list[str], cache_path: Path | None = None, pause: float = 0.25,
                get=None) -> pd.DataFrame:
    """Every earnings report yfinance knows for these tickers (resumable cache)."""
    done = pd.read_parquet(cache_path) if cache_path is not None and Path(cache_path).exists() else pd.DataFrame()
    have = set(done["ticker"]) if len(done) else set()
    if get is None:
        import yfinance as yf

        def get(t):
            return yf.Ticker(t).get_earnings_dates(limit=100)
    frames = [done] if len(done) else []
    for n, t in enumerate(tickers):
        if t in have:
            continue
        try:
            e = get(t)
        except Exception:
            e = None
        if e is not None and len(e):
            e = e.dropna(subset=["Reported EPS"]).copy()
            e["ticker"] = t
            e["when"] = e.index.astype(str)
            frames.append(e.reset_index(drop=True).rename(columns={
                "EPS Estimate": "estimate", "Reported EPS": "reported", "Surprise(%)": "surprise"}))
        else:
            frames.append(pd.DataFrame({"ticker": [t], "when": [None]}))
        if cache_path is not None and n % 25 == 0:
            pd.concat(frames, ignore_index=True).to_parquet(cache_path)
        time.sleep(pause)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if cache_path is not None:
        out.to_parquet(cache_path)
    return out


def load_prices(tickers: list[str], cache_path: Path | None = None, start: str = "2001-06-01") -> pd.DataFrame:
    if cache_path is not None and Path(cache_path).exists():
        return pd.read_parquet(cache_path)
    import yfinance as yf

    parts = []
    for k in range(0, len(tickers), 100):
        raw = yf.download(tickers[k:k + 100], start=start, auto_adjust=True, progress=False, threads=True)
        parts.append(raw["Close"] if "Close" in raw else raw)
        time.sleep(1.0)
    out = pd.concat(parts, axis=1).sort_index()
    out = out.loc[:, ~out.columns.duplicated()]
    if cache_path is not None:
        out.to_parquet(cache_path)
    return out


# --- events --------------------------------------------------------------------------


def reaction_day(when: pd.Timestamp, days: pd.DatetimeIndex) -> int | None:
    """Index of the first session whose close reflects the announcement."""
    local = when.tz_convert("America/New_York") if when.tzinfo else when
    date = pd.Timestamp(local.date())
    after_close = local.hour >= 16 or (local.hour == 0 and local.minute == 0)
    i = int(days.searchsorted(date, side="right" if after_close else "left"))
    return i if i < len(days) else None


def build_events(raw: pd.DataFrame, universe: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """One row per usable report: reaction day, the reaction (that day's return
    against SPY), the surprise, and the sector. Only reports after the company
    joined the index are kept."""
    days = prices.index
    spy = prices["SPY"].pct_change()
    info = universe.set_index("ticker")
    rows = []
    for r in raw.dropna(subset=["when"]).itertuples(index=False):
        t = r.ticker
        if t not in prices or t not in info.index:
            continue
        when = pd.Timestamp(r.when)
        added = pd.to_datetime(info.at[t, "added"], errors="coerce")
        if pd.notna(added) and when.tz_localize(None) < added if when.tzinfo else when < added:
            continue
        i = reaction_day(when, days)
        if i is None or i < 1:
            continue
        px = prices[t]
        if not (np.isfinite(px.iloc[i]) and np.isfinite(px.iloc[i - 1])):
            continue
        reaction = float(px.iloc[i] / px.iloc[i - 1] - 1 - spy.iloc[i])
        surprise = float(r.surprise) if r.surprise is not None and np.isfinite(r.surprise) else np.nan
        rows.append({"ticker": t, "sector": info.at[t, "sector"], "day": i, "date": days[i],
                     "surprise": surprise, "reaction": reaction})
    ev = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return ev


def past_rank(ev: pd.DataFrame, col: str, window_days: int = 365) -> pd.Series:
    """Where each event's value sits among the previous year's events (0-1), past only."""
    out = np.full(len(ev), np.nan)
    dates = ev["date"].to_numpy()
    vals = ev[col].to_numpy(dtype=float)
    lo = 0
    for k in range(len(ev)):
        while dates[lo] < dates[k] - np.timedelta64(window_days, "D"):
            lo += 1
        prior = vals[lo:k]
        prior = prior[np.isfinite(prior)]
        if len(prior) >= 50 and np.isfinite(vals[k]):
            out[k] = (prior < vals[k]).mean()
    return pd.Series(out, index=ev.index)


# --- portfolios ----------------------------------------------------------------------


@dataclass
class Signal:
    name: str
    group: str            # "all" or a sector name
    hold: int             # sessions held
    pick: object          # (events) -> boolean mask of events to buy


def portfolio(ev: pd.DataFrame, mask: pd.Series, prices: pd.DataFrame, hold: int, cash: pd.Series,
              delay: int = 1) -> pd.Series:
    """Daily return of holding, in equal weight, every chosen event from the close
    `delay` sessions after its reaction day for `hold` sessions. Cash when empty."""
    rets = prices.pct_change()
    n = len(prices.index)
    total = np.zeros(n)
    count = np.zeros(n)
    tick_pos = {t: k for k, t in enumerate(prices.columns)}
    R = rets.to_numpy()
    for e in ev[mask.fillna(False).to_numpy()].itertuples(index=False):
        start = e.day + delay + 1                       # first day earning a return
        end = min(start + hold, n)
        col = R[start:end, tick_pos[e.ticker]]
        ok = np.isfinite(col)
        total[start:end][ok] += col[ok]
        count[start:end][ok] += 1
    port = np.where(count > 0, total / np.maximum(count, 1), cash.reindex(prices.index).fillna(0).to_numpy())
    return pd.Series(port, index=prices.index)


def signals(sectors: list[str]) -> list[Signal]:
    """Every rule tried, fixed before looking at results; all count toward the luck correction."""
    def big_beat(ev):
        return ev["surprise_rank"] >= 0.8

    def strong_reaction(ev):
        return ev["reaction_rank"] >= 0.8

    def beat_and_up(ev):
        return (ev["surprise"] > 0) & (ev["reaction"] > 0)

    def both_top(ev):
        return (ev["surprise_rank"] >= 0.7) & (ev["reaction_rank"] >= 0.7)

    def big_miss(ev):
        return ev["surprise_rank"] <= 0.2

    def weak_reaction(ev):
        return ev["reaction_rank"] <= 0.2

    base = (("big beat (top 20% surprise)", big_beat), ("strong reaction (top 20% day-after move)", strong_reaction),
            ("beat AND rose", beat_and_up), ("top 30% on both", both_top),
            ("big miss (bottom 20%), contrarian", big_miss), ("weak reaction (bottom 20%), contrarian", weak_reaction))
    out = []
    for hold in (20, 60):
        for name, fn in base:
            out.append(Signal(f"{name}, held {hold} days", "all", hold, fn))
    for sec in sectors:
        for name, fn in (("big beat", big_beat), ("strong reaction", strong_reaction)):
            out.append(Signal(f"{sec}: {name}, held 60 days", sec, 60,
                              lambda ev, fn=fn, sec=sec: fn(ev) & (ev["sector"] == sec)))
    return out


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
    events: int
    holdout: dict | None = None
    alpha_holdout: float | None = None
    t_holdout: float | None = None
    beats_spy: bool | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run(ev: pd.DataFrame, prices: pd.DataFrame, cash: pd.Series, draws: int = 2000, seed: int = 11,
        split: str = SPLIT, start: str = START, sigs: list[Signal] | None = None) -> dict:
    from .optionslab import _alpha, _metrics
    from .research import reality_check

    ev = ev.copy()
    for col in ("surprise", "reaction"):           # other labs reuse this with their own event columns
        if col in ev:
            ev[f"{col}_rank"] = past_rank(ev, col)
    sigs = sigs if sigs is not None else signals(sorted(ev["sector"].dropna().unique()))
    days = prices.loc[start:].index
    spy = prices["SPY"].pct_change().reindex(days)
    c = cash.reindex(days).fillna(0.0)
    cols, counts = {}, {}
    for s in sigs:
        m = s.pick(ev)
        cols[s.name] = portfolio(ev, m, prices, s.hold, cash).reindex(days)
        counts[s.name] = int(m.fillna(False).sum())
    rets = pd.DataFrame(cols)
    dev = days < pd.Timestamp(split)

    def alphas(mask):
        return np.column_stack([_alpha(rets.loc[mask, n], spy[mask], c[mask])[0].to_numpy() for n in rets])

    t_dev, p_dev = reality_check(alphas(dev), draws, seed=seed)
    bench_hold = _metrics(spy[~dev], c[~dev])
    rows = []
    for k, s in enumerate(sigs):
        a_dev = _alpha(rets.loc[dev, s.name], spy[dev], c[dev])[0]
        passed = bool(a_dev.mean() > 0 and p_dev[k] < PASS_P)
        row = Row(s.name, s.group, _metrics(rets.loc[dev, s.name], c[dev]), _metrics(rets[s.name], c),
                  float(a_dev.mean() * 252), float(t_dev[k]), float(p_dev[k]), passed, counts[s.name])
        if passed:
            h = rets.loc[~dev, s.name]
            a_h = _alpha(h, spy[~dev], c[~dev])[0]
            row.holdout = _metrics(h, c[~dev])
            row.alpha_holdout = float(a_h.mean() * 252)
            row.t_holdout = float(a_h.mean() / a_h.std() * math.sqrt(len(a_h))) if a_h.std() > 0 else 0.0
            row.beats_spy = bool(row.holdout["sharpe"] >= bench_hold["sharpe"])
        rows.append(row)
    rows.sort(key=lambda r: r.p_dev)
    # How strong is the drift itself? Average 60-day move after the top vs bottom fifth (vs SPY).
    drift = {}
    R = prices.pct_change()
    for label, m in [] if "surprise_rank" not in ev else (("top fifth by surprise", ev["surprise_rank"] >= 0.8),
                     ("bottom fifth by surprise", ev["surprise_rank"] <= 0.2)):
        moves = []
        for e in ev[m.fillna(False)].itertuples(index=False):
            a, b = e.day + 2, min(e.day + 62, len(prices) - 1)
            if b <= a:
                continue
            stock = (1 + R[e.ticker].iloc[a:b].fillna(0)).prod() - 1
            mkt = (1 + R["SPY"].iloc[a:b].fillna(0)).prod() - 1
            moves.append(stock - mkt)
        drift[label] = (float(np.mean(moves)) if moves else float("nan"), len(moves))
    return {"rows": rows, "n": len(sigs), "events": len(ev), "start": str(days[0].date()),
            "end": str(days[-1].date()), "split": split, "drift": drift,
            "bench": {"dev": _metrics(spy[dev], c[dev]), "holdout": bench_hold}}


def strong(rows: list[Row]) -> list[Row]:
    from .drivers import holdout_bar

    bar = holdout_bar(rows)
    return [r for r in rows if r.passed and r.t_holdout is not None and r.t_holdout > bar and r.beats_spy]


def _pct(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def describe(res: dict, top: int = 15) -> list[str]:
    from .drivers import holdout_bar

    out = ["EARNINGS DRIFT: DO STOCKS KEEP MOVING AFTER A SURPRISE?", "=" * 80,
           f"{res['events']:,} earnings reports from today's S&P 500 companies (counted only after each joined),",
           f"{res['start']} to {res['end']}; {res['n']} rules ranked on years before {res['split'][:4]}, later years untouched.",
           "Bought at the close of the session AFTER the reaction; equal weight; cash when nothing qualifies."]
    for label, (avg, n) in res["drift"].items():
        out.append(f"  {label}: next 60 sessions vs SPY {_pct(avg)} on average ({n:,} reports)")
    b = res["bench"]["holdout"]
    out += ["", f"SPY from {res['split'][:4]}: Sharpe {b['sharpe']:.2f}, CAGR {_pct(b['cagr'])}. "
            f"Later-years bar: alpha t above {holdout_bar(res['rows']):.2f} AND Sharpe at least SPY's.",
            f"{'  rule (best first, development years)':<64}{'alpha':>8}{'t':>6}{'p':>6}{'reports':>9}"]
    for r in res["rows"][:top]:
        out.append(f"  {r.name[:61]:<62}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}{r.events:>9,}")
    surv = [r for r in res["rows"] if r.passed]
    if not surv:
        out.append("  None passed the development test, so the later years were never touched.")
    for r in surv:
        h = r.holdout
        out.append(f"  PASSED: {r.name[:58]}: later alpha {_pct(r.alpha_holdout)} (t {r.t_holdout:.1f}), "
                   f"Sharpe {h['sharpe']:.2f}, CAGR {_pct(h['cagr'])}")
    win = strong(res["rows"])
    out += ["", "RESULT: " + ("; ".join(w.name for w in win) + " cleared every bar. Paper-trade it first."
                              if win else "nothing cleared every bar.")]
    return out
