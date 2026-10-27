"""Options lab: does any S&P 500 options strategy beat simply holding the index?

There is no free history of option prices, so prices are rebuilt the way the
academic literature does it, then CHECKED against the one real record that is
free: Cboe's PutWrite index (^PUT, 1996 to now), a real strategy priced from
real quotes.

  * Black-Scholes on the S&P 500, volatility from the VIX family (VIX9D for
    one week, VIX for a month, VIX3M beyond) times a factor `a` FITTED EACH
    YEAR so a one-month at-the-money put-write replica earns what ^PUT earned
    that year. That anchors the size of the premium sellers collect (the
    "variance risk premium") to reality instead of assuming it.
  * A volatility smile: out-of-the-money puts are priced richer and calls a
    little cheaper than at the money (SKEW_PUT / SKEW_CALL). This part cannot
    be checked against free data, so every result is re-run with no smile and
    with double the smile; a strategy that only works under one smile
    assumption is not counted as working.
  * Costs on every option traded: COST_ABS of the index level per contract
    plus COST_REL of the premium (wider, in relative terms, for cheap
    far-from-the-money options, as in real markets).

Judgement uses the same machinery as `research`: alpha against holding the
S&P 500 (total return, dividends in), White's Reality Check across EVERY
strategy tried, development years to rank, and holdout years spent only on
survivors. The holdout includes 2018's volatility blow-up, 2020 and 2022.

What this cannot tell you: real fills, early assignment, margin calls,
taxes, and whether your account is allowed to place the trade. Each strategy
says which Robinhood options level it needs.

Pure maths except `load_data`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

TICKERS = ["^GSPC", "^SP500TR", "^VIX", "^VIX9D", "^VIX3M", "^IRX", "^PUT"]
START = "2006-08-01"          # VIX3M begins; S&P weekly options exist from 2005
SPLIT = "2016-01-01"          # development before, holdout from
DIV_YIELD = 0.018
# Smile, FITTED (fit_smile) so monthly replicas of Cboe's real 2%-OTM put-write
# (PUTY), 2%-OTM buy-write (BXY), 30-delta buy-write (BXMD) and 5% put
# protection (PPUT) indices each earn within ~0.5%/yr of the real thing, 2006-2026.
SKEW_PUT = 0.20               # IV rise per unit of standardised moneyness below the money
SKEW_CALL = 0.40              # IV fall per unit above the money
COST_ABS = 0.00005            # per contract, as a share of the index level (half-spread)
COST_REL = 0.02               # plus this share of the option's price
STOCK_COST = 0.0001           # 1 bp on stock traded
DEFAULT_A = 0.86
PASS_P = 0.10
_N = NormalDist()


# --- data -----------------------------------------------------------------------


def load_data(cache_path: Path | None = None, max_age_hours: float = 12.0) -> pd.DataFrame:
    import time

    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                pass
    import yfinance as yf

    raw = yf.download(TICKERS, start="2004-01-01", auto_adjust=False, progress=False)
    df = (raw["Close"] if "Close" in raw else raw).sort_index()
    missing = [t for t in ("^GSPC", "^SP500TR", "^VIX", "^IRX", "^PUT") if t not in df or df[t].dropna().empty]
    if missing:
        raise RuntimeError(f"no data for {', '.join(missing)}")
    if cache_path is not None:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path)
        except Exception:
            pass
    return df


# --- pricing ----------------------------------------------------------------------


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S: float, K: float, T: float, r: float, q: float, v: float, kind: str) -> float:
    if T <= 0:
        return max(K - S, 0.0) if kind == "P" else max(S - K, 0.0)
    sq = v * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * v * v) * T) / sq
    d2 = d1 - sq
    if kind == "C":
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def smile(atm: float, S: float, K: float, T: float, skew: float = 1.0,
          skew_put: float | None = None, skew_call: float | None = None) -> float:
    """Implied vol at strike K from the at-the-money level and the smile."""
    if T <= 0:
        return atm
    m = math.log(K / S) / (atm * math.sqrt(T))
    sp = SKEW_PUT if skew_put is None else skew_put
    sc = SKEW_CALL if skew_call is None else skew_call
    slope = (sp if m < 0 else sc) * skew
    return min(max(atm * (1.0 - slope * m), 0.4 * atm), 3.0 * atm)


def strike_for_delta(S: float, T: float, r: float, q: float, atm: float, delta: float, kind: str,
                     skew: float = 1.0, skew_put: float | None = None, skew_call: float | None = None) -> float:
    """Strike whose |delta| is `delta` under the smile (fixed-point on the vol)."""
    v = atm
    K = S
    for _ in range(4):
        sq = v * math.sqrt(T)
        d1 = _N.inv_cdf(delta * math.exp(q * T)) if kind == "C" else -_N.inv_cdf(delta * math.exp(q * T))
        F = S * math.exp((r - q) * T)
        K = F * math.exp(-(d1 * sq - 0.5 * sq * sq))
        v = smile(atm, S, K, T, skew, skew_put, skew_call)
    return K


# --- the market as arrays ------------------------------------------------------------


@dataclass
class Market:
    dates: pd.DatetimeIndex
    S: np.ndarray
    TR: np.ndarray
    vix: np.ndarray
    vix9: np.ndarray
    vix3m: np.ndarray
    r: np.ndarray
    a: np.ndarray = field(default=None)          # calibration factor per day
    skew: float = 1.0                            # multiplier for robustness re-runs
    skew_put: float = SKEW_PUT                   # fitted to real Cboe indices by fit_smile()
    skew_call: float = SKEW_CALL
    weekly: float = 1.0                          # extra factor for options under 10 days (fit_weekly)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, start: str | None = None) -> "Market":
        need = df[["^GSPC", "^SP500TR", "^VIX", "^IRX"]].dropna()
        idx = need.index
        f = df.reindex(idx).ffill()
        m = cls(idx, f["^GSPC"].to_numpy(float), f["^SP500TR"].to_numpy(float),
                f["^VIX"].to_numpy(float),
                f["^VIX9D"].to_numpy(float) if "^VIX9D" in f else np.full(len(idx), np.nan),
                f["^VIX3M"].to_numpy(float) if "^VIX3M" in f else np.full(len(idx), np.nan),
                f["^IRX"].to_numpy(float) / 100.0)
        m.a = np.full(len(idx), DEFAULT_A)
        return m

    def atm(self, i: int, days: float) -> float:
        """At-the-money vol for an option `days` calendar days out."""
        base = self._atm(i, days)
        return base * self.weekly if days < 10 else base

    def _atm(self, i: int, days: float) -> float:
        v30 = self.vix[i]
        v9, v93 = self.vix9[i], self.vix3m[i]
        if days < 30 and not np.isnan(v9):
            lo, hi, d0, d1 = v9, v30, 9.0, 30.0
        elif days > 30 and not np.isnan(v93):
            lo, hi, d0, d1 = v30, v93, 30.0, 93.0
        else:
            return self.a[i] * v30 / 100.0
        d = min(max(days, d0), d1)
        w = (d - d0) / (d1 - d0)
        var = ((1 - w) * lo * lo * d0 + w * hi * hi * d1) / d       # interpolate total variance
        return self.a[i] * math.sqrt(max(var, 1e-6)) / 100.0

    def price(self, i: int, K: float, exp_i: int, kind: str) -> float:
        days = (self.dates[exp_i] - self.dates[i]).days
        if days <= 0:
            return bs(self.S[i], K, 0.0, 0, 0, 0, kind)
        T = days / 365.0
        atm = self.atm(i, days)
        return bs(self.S[i], K, T, self.r[i], DIV_YIELD,
                  smile(atm, self.S[i], K, T, self.skew, self.skew_put, self.skew_call), kind)


def expiries(dates: pd.DatetimeIndex, tenor: str) -> np.ndarray:
    """Indexes of expiry days: third Friday of each month ('M') or last trading day of each week ('W')."""
    s = pd.Series(np.arange(len(dates)), index=dates)
    out = []
    if tenor == "W":
        for _, g in s.groupby([dates.isocalendar().year, dates.isocalendar().week]):
            out.append(int(g.iloc[-1]))
    else:
        for (y, mo), g in s.groupby([dates.year, dates.month]):
            third = pd.Timestamp(y, mo, 1) + pd.offsets.WeekOfMonth(week=2, weekday=4)
            on_or_before = g[g.index <= third]
            if len(on_or_before):
                out.append(int(on_or_before.iloc[-1]))
    return np.array(sorted(set(out)))


# --- strategies ----------------------------------------------------------------------


@dataclass
class Leg:
    kind: str        # "P" or "C"
    K: float
    units: float     # + long, - short (in index units)


@dataclass
class Strategy:
    name: str
    family: str
    tenor: str                 # "W" or "M"
    level: str                 # what Robinhood calls the permission it needs
    build: object              # (mkt, i, exp_i, nav) -> (stock_notional, [Leg]) or None to sit out
    hold_stock_when_off: bool = False


def _T(mkt, i, exp_i):
    return max((mkt.dates[exp_i] - mkt.dates[i]).days, 1) / 365.0


def _k(mkt, i, exp_i, delta, kind):
    T = _T(mkt, i, exp_i)
    days = (mkt.dates[exp_i] - mkt.dates[i]).days
    return strike_for_delta(mkt.S[i], T, mkt.r[i], DIV_YIELD, mkt.atm(i, days), delta, kind, mkt.skew,
                            mkt.skew_put, mkt.skew_call)


def short_put(delta, leverage=1.0):
    def build(mkt, i, e, nav):
        K = _k(mkt, i, e, delta, "P")
        return 0.0, [Leg("P", K, -leverage * nav / K)]
    return build


def short_put_at(moneyness):
    def build(mkt, i, e, nav):
        K = moneyness * mkt.S[i]
        return 0.0, [Leg("P", K, -nav / K)]
    return build


def covered_call_at(moneyness):
    def build(mkt, i, e, nav):
        return nav, [Leg("C", moneyness * mkt.S[i], -nav / mkt.S[i])]
    return build


def protective_put_at(moneyness):
    def build(mkt, i, e, nav):
        return nav, [Leg("P", moneyness * mkt.S[i], nav / mkt.S[i])]
    return build


def covered_call(delta):
    def build(mkt, i, e, nav):
        K = _k(mkt, i, e, delta, "C")
        return nav, [Leg("C", K, -nav / mkt.S[i])]
    return build


def collar(put_delta, call_delta):
    def build(mkt, i, e, nav):
        u = nav / mkt.S[i]
        return nav, [Leg("P", _k(mkt, i, e, put_delta, "P"), u), Leg("C", _k(mkt, i, e, call_delta, "C"), -u)]
    return build


def protective_put(delta):
    def build(mkt, i, e, nav):
        return nav, [Leg("P", _k(mkt, i, e, delta, "P"), nav / mkt.S[i])]
    return build


def strangle(delta):
    def build(mkt, i, e, nav):
        Kp, Kc = _k(mkt, i, e, delta, "P"), _k(mkt, i, e, delta, "C")
        u = nav / Kp
        return 0.0, [Leg("P", Kp, -u), Leg("C", Kc, -u)]
    return build


def iron_condor(short_delta, long_delta):
    def build(mkt, i, e, nav):
        sp, lp = _k(mkt, i, e, short_delta, "P"), _k(mkt, i, e, long_delta, "P")
        sc, lc = _k(mkt, i, e, short_delta, "C"), _k(mkt, i, e, long_delta, "C")
        u = nav / sp
        return 0.0, [Leg("P", sp, -u), Leg("P", lp, u), Leg("C", sc, -u), Leg("C", lc, u)]
    return build


def bull_put(short_delta, long_delta):
    def build(mkt, i, e, nav):
        sp, lp = _k(mkt, i, e, short_delta, "P"), _k(mkt, i, e, long_delta, "P")
        u = nav / sp
        return 0.0, [Leg("P", sp, -u), Leg("P", lp, u)]
    return build


def long_call(delta):
    def build(mkt, i, e, nav):
        return 0.0, [Leg("C", _k(mkt, i, e, delta, "C"), nav / mkt.S[i])]
    return build


def long_straddle(units_of_nav=1.0):
    def build(mkt, i, e, nav):
        u = units_of_nav * nav / mkt.S[i]
        K = mkt.S[i]
        return 0.0, [Leg("P", K, u), Leg("C", K, u)]
    return build


# Filters: decided at each roll from data known that day.
def _f_vix_above(level):
    return lambda mkt, i: mkt.vix[i] > level


def _f_contango(mkt, i):
    return not np.isnan(mkt.vix3m[i]) and mkt.vix[i] < mkt.vix3m[i]


def _f_uptrend(mkt, i):
    return i >= 200 and mkt.S[i] > mkt.S[i - 200:i].mean()


def _f_vix_rich(mkt, i):
    return i >= 252 and mkt.vix[i] > np.median(mkt.vix[i - 252:i])


def _f_vix_below(level):
    return lambda mkt, i: mkt.vix[i] < level


def gated(build, cond):
    def inner(mkt, i, e, nav):
        return build(mkt, i, e, nav) if cond(mkt, i) else None
    return inner


L2 = "Level 2 (covered calls, cash-secured puts, buying options)"
L3 = "Level 3 (spreads)"
NAKED = "Naked selling (margin, highest approval)"


def candidates() -> list[Strategy]:
    """Every model strategy tried. All of them count toward the luck correction,
    so this list only grows; nothing that failed is removed."""
    out: list[Strategy] = []
    add = out.append
    deltas = (0.50, 0.40, 0.30, 0.20, 0.15, 0.10, 0.05)
    for tenor in ("M", "W"):
        word = "monthly" if tenor == "M" else "weekly"
        for d in deltas:
            add(Strategy(f"cash-secured put {d:.2f} delta, {word}", "put-write", tenor, L2, short_put(d)))
            add(Strategy(f"covered call {d:.2f} delta, {word}", "buy-write", tenor, L2, covered_call(d),
                         hold_stock_when_off=True))
        for sd, ld in ((0.30, 0.10), (0.20, 0.05), (0.40, 0.20)):
            add(Strategy(f"bull put spread {sd:.2f}/{ld:.2f}, {word}", "credit spread", tenor, L3,
                         bull_put(sd, ld)))
        for sd, ld in ((0.16, 0.05), (0.10, 0.03), (0.20, 0.10)):
            add(Strategy(f"iron condor {sd:.2f}/{ld:.2f}, {word}", "iron condor", tenor, L3,
                         iron_condor(sd, ld)))
        for d in (0.30, 0.16, 0.10, 0.05):
            add(Strategy(f"short strangle {d:.2f} delta, {word}", "strangle", tenor, NAKED, strangle(d)))
    for pd_, cd in ((0.10, 0.25), (0.25, 0.25), (0.05, 0.15), (0.15, 0.30)):
        add(Strategy(f"collar: buy {pd_:.2f} put, sell {cd:.2f} call, monthly", "collar", "M", L2,
                     collar(pd_, cd)))
    for d in (0.40, 0.30, 0.20, 0.10, 0.05):
        add(Strategy(f"protective {d:.2f} put, monthly", "protection", "M", L2, protective_put(d)))
    for d in (0.90, 0.80, 0.65, 0.50):
        add(Strategy(f"call {d:.2f} delta instead of stock, monthly", "long calls", "M", L2, long_call(d)))
    for lev in (1.5, 2.0):
        for d in (0.30, 0.15):
            add(Strategy(f"cash-secured put {d:.2f}, {lev:g}x notional (margin), monthly",
                         "leveraged put-write", "M", NAKED, short_put(d, lev)))
    for lvl in (12, 13, 15):
        add(Strategy(f"long straddle only when VIX < {lvl}, monthly", "long volatility", "M", L2,
                     gated(long_straddle(0.5), _f_vix_below(lvl))))
    filters = (("VIX above 20", _f_vix_above(20)), ("VIX above 25", _f_vix_above(25)),
               ("VIX below VIX3M (calm curve)", _f_contango), ("S&P above 200-day average", _f_uptrend),
               ("VIX above its 1-year median", _f_vix_rich))
    for fname, cond in filters:
        for d in (0.30, 0.15):
            add(Strategy(f"cash-secured put {d:.2f}, monthly, only when {fname}", "filtered put-write", "M",
                         L2, gated(short_put(d), cond)))
            add(Strategy(f"covered call {d:.2f}, monthly, only when {fname}", "filtered buy-write", "M", L2,
                         gated(covered_call(d), cond), hold_stock_when_off=True))
        add(Strategy(f"short strangle 0.16, monthly, only when {fname}", "filtered strangle", "M", NAKED,
                     gated(strangle(0.16), cond)))
        add(Strategy(f"iron condor 0.16/0.05, monthly, only when {fname}", "filtered condor", "M", L3,
                     gated(iron_condor(0.16, 0.05), cond)))
    return out


# --- the real record: Cboe's strategy indices, priced from real quotes --------------------

CBOE_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{t}_History.csv"
CBOE_INDICES = {
    "PUT": "sell 1-month at-the-money puts (cash-secured)",
    "WPUT": "sell 1-week at-the-money puts (cash-secured)",
    "PUTY": "sell 1-month 2% out-of-the-money puts (cash-secured)",
    "BXM": "own the S&P 500, sell 1-month at-the-money calls",
    "BXMD": "own the S&P 500, sell 1-month 30-delta calls",
    "BXY": "own the S&P 500, sell 1-month 2% out-of-the-money calls",
    "BXMC": "own the S&P 500, sell calls only under conditions (conditional buy-write)",
    "PPUT": "own the S&P 500, buy 5% out-of-the-money puts",
    "CLLZ": "own the S&P 500, zero-cost put-spread collar",
    "CNDR": "sell an S&P 500 iron condor",
    "BFLY": "sell an S&P 500 iron butterfly",
    "CMBO": "own the S&P 500, covered combo (sell a call and a put)",
    "VXTH": "own the S&P 500, buy VIX calls as a tail hedge",
    "SPRO": "S&P 500 buffer protection",
    "CLL": "own the S&P 500, 95-110 collar",
}
OTHER_UNDERLYINGS = {        # Cboe index -> (fund to compare with, what it does)
    "BXNT": ("QQQ", "own the Nasdaq-100, sell 1-month at-the-money calls"),
    "BXNH": ("QQQ", "own the Nasdaq-100, sell calls on half"),
    "PUTR": ("IWM", "sell 1-month at-the-money Russell 2000 puts"),
    "BXR": ("IWM", "own the Russell 2000, sell 1-month at-the-money calls"),
    "BXRC": ("IWM", "own the Russell 2000, conditional covered calls"),
    "CLLR": ("IWM", "own the Russell 2000, zero-cost put-spread collar"),
}
REAL_START = "2008-09-01"     # every index above (CLL starts 2008-08) has history from here


def load_cboe(cache_path: Path | None = None, max_age_hours: float = 12.0, fetch=None) -> pd.DataFrame:
    """Daily levels of the Cboe indices above (columns), from Cboe's own files."""
    import io
    import time
    import urllib.request

    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                pass

    def get(url):
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    fetch = fetch or get
    cols = {}
    for t in list(CBOE_INDICES) + list(OTHER_UNDERLYINGS):
        try:
            raw = pd.read_csv(io.StringIO(fetch(CBOE_URL.format(t=t))))
        except Exception:
            continue
        raw.columns = [c.strip().upper() for c in raw.columns]
        val = [c for c in raw.columns if c != "DATE"][0]
        ser = pd.Series(pd.to_numeric(raw[val], errors="coerce").to_numpy(),
                        index=pd.to_datetime(raw["DATE"], format="%m/%d/%Y", errors="coerce")).dropna()
        cols[t] = ser[~ser.index.duplicated()].sort_index()
    if not cols:
        raise RuntimeError("could not download any Cboe index history")
    out = pd.DataFrame(cols)
    if cache_path is not None:
        try:
            out.to_parquet(cache_path)
        except Exception:
            pass
    return out


def load_funds(tickers=("QQQ", "IWM")) -> pd.DataFrame:
    """Dividend-adjusted closes for the comparison funds (network)."""
    import yfinance as yf

    raw = yf.download(list(tickers), start="2005-01-01", auto_adjust=True, progress=False)
    return (raw["Close"] if "Close" in raw else raw).sort_index()


def own_underlying(levels: pd.DataFrame, funds: pd.DataFrame, irx: pd.Series, start: str = "2009-10-01",
                   split: str = SPLIT) -> list[dict]:
    """Each non-S&P Cboe index against holding its own index fund (dividends in)."""
    out = []
    for t, (fund, what) in OTHER_UNDERLYINGS.items():
        if t not in levels or fund not in funds:
            continue
        j = pd.concat([levels[t].rename("x"), funds[fund].rename("u")], axis=1).dropna().loc[start:]
        if len(j) < 500:
            continue
        r = j.pct_change().dropna()
        c = (irx.reindex(r.index).ffill().shift(1) / 100 / 252).fillna(0)
        row = {"index": t, "fund": fund, "what": what}
        for label, mask in (("all", np.ones(len(r), bool)), ("holdout", r.index >= pd.Timestamp(split))):
            rr, cc = r[mask], c[mask]
            ex, eu = rr["x"] - cc, rr["u"] - cc
            beta = float(np.cov(ex, eu)[0, 1] / eu.var())
            a = ex - beta * eu
            yrs = len(rr) / 252
            w = (1 + rr).cumprod()
            row[label] = {"cagr": float(w["x"].iloc[-1] ** (1 / yrs) - 1), "fund_cagr": float(w["u"].iloc[-1] ** (1 / yrs) - 1),
                          "sharpe": float(ex.mean() / ex.std() * math.sqrt(252)),
                          "fund_sharpe": float(eu.mean() / eu.std() * math.sqrt(252)),
                          "alpha": float(a.mean() * 252), "t": float(a.mean() / a.std() * math.sqrt(len(a)))}
        out.append(row)
    return out


def overlays(real: pd.DataFrame, spx_tr: pd.Series, cash: pd.Series, vix: pd.Series, vix3m: pd.Series,
             spx: pd.Series) -> dict[str, pd.Series]:
    """Switching rules between REAL return streams. The signal uses the previous
    close (shifted a day), so a switch is acted on with information already known."""
    r = real.pct_change()
    tr = spx_tr.pct_change()
    above = (spx > spx.rolling(200).mean()).shift(1).fillna(False).astype(bool)
    hi_vix = (vix > 20).shift(1).fillna(False).astype(bool)
    rich = (vix > vix.rolling(252).median()).shift(1).fillna(False).astype(bool)
    calm = (vix < vix3m).shift(1).fillna(False).astype(bool)
    out = {}
    for name, idx in (("PUT", "PUT"), ("BXMD", "BXMD"), ("PPUT", "PPUT")):
        if idx not in r:
            continue
        x = r[idx]
        out[f"{name} when VIX > 20, else S&P 500"] = x.where(hi_vix, tr)
        out[f"{name} when VIX above its 1-year median, else S&P 500"] = x.where(rich, tr)
        out[f"S&P 500 in uptrends, {name} in downtrends"] = tr.where(above, x)
        out[f"{name} in calm curve (VIX < VIX3M), else S&P 500"] = x.where(calm, tr)
        out[f"half S&P 500, half {name}"] = 0.5 * tr + 0.5 * x
    if "PUT" in r:
        out["PUT in uptrends, cash in downtrends"] = r["PUT"].where(above, cash)
    return out


@dataclass
class Row:
    name: str
    family: str
    level: str
    dev: dict
    holdout: dict | None
    full: dict
    alpha_dev: float
    t_dev: float
    p_dev: float
    alpha_full: float
    p_full: float
    passed: bool
    alpha_holdout: float | None = None
    t_holdout: float | None = None
    robust: bool | None = None          # model rows: survives a 50% weaker and stronger smile
    skew_alphas: dict = field(default_factory=dict)
    beats_spx: bool | None = None       # holdout CAGR and Sharpe both at least the S&P 500's

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["skew_alphas"] = {str(k): v for k, v in self.skew_alphas.items()}
        return d


# --- simulation -------------------------------------------------------------------------


def simulate(mkt: Market, strat: Strategy, start: str = START) -> pd.Series:
    """Daily net returns of one strategy. Positions are opened at the close of an
    expiry day using only that day's data and earn from the next day on."""
    exp = expiries(mkt.dates, strat.tenor)
    first = int(np.searchsorted(mkt.dates, pd.Timestamp(start)))
    exp = exp[exp >= first]
    nav = 1.0
    cash, stock_units, legs, exp_i = 1.0, 0.0, [], None
    values = np.full(len(mkt.dates), np.nan)
    roll_set = set(exp.tolist())
    nxt = {int(e): int(exp[k + 1]) for k, e in enumerate(exp[:-1])}
    for i in range(exp[0], len(mkt.dates)):
        if i > exp[0]:
            days = (mkt.dates[i] - mkt.dates[i - 1]).days
            cash *= 1.0 + mkt.r[i - 1] * days / 365.0
        opt_value = sum(lg.units * mkt.price(i, lg.K, exp_i, lg.kind) for lg in legs) if legs else 0.0
        nav = cash + stock_units * mkt.TR[i] + opt_value
        if i in roll_set and i not in nxt and legs:
            cash += opt_value                              # last expiry in the data: settle, open nothing
            legs = []
        if i in roll_set and i in nxt:
            cash += opt_value                              # settle (at expiry this is intrinsic)
            legs = []
            exp_i = nxt[i]
            built = strat.build(mkt, i, exp_i, nav)
            if built is None:
                stock_target = nav if strat.hold_stock_when_off else 0.0
                new_legs = []
            else:
                stock_target, new_legs = built
            new_units = stock_target / mkt.TR[i]
            cash -= (new_units - stock_units) * mkt.TR[i] + abs(new_units - stock_units) * mkt.TR[i] * STOCK_COST
            stock_units = new_units
            for lg in new_legs:
                p = mkt.price(i, lg.K, exp_i, lg.kind)
                cash -= lg.units * p + abs(lg.units) * (COST_ABS * mkt.S[i] + COST_REL * p)
            legs = new_legs
            nav = cash + stock_units * mkt.TR[i] + sum(lg.units * mkt.price(i, lg.K, exp_i, lg.kind) for lg in legs)
        values[i] = max(nav, 1e-9)
    s = pd.Series(values, index=mkt.dates).dropna()
    return s.pct_change().dropna()


# --- calibration against the real put-write index -------------------------------------


def calibrate(df: pd.DataFrame, mkt: Market) -> pd.Series:
    """Per-year factor `a` so the one-month at-the-money put-write replica earns what
    Cboe's real ^PUT index earned that year (log return). Years without ^PUT keep
    the default. Prices are reconstructed, not strategies tuned: every candidate uses
    the same factor."""
    put = df["^PUT"].reindex(mkt.dates).ffill()
    replica = Strategy("replica", "x", "M", L2, short_put(0.50))
    years = sorted(set(mkt.dates.year))
    factors = {}
    for y in years:
        # Run past year end so December's position is held exactly as a full run holds it.
        lo, hi = pd.Timestamp(y - 1, 11, 1), pd.Timestamp(y + 1, 1, 31)
        win = (mkt.dates >= lo) & (mkt.dates <= hi)
        sub_idx = np.where(win)[0]
        if len(sub_idx) < 120:
            continue
        p = put.iloc[sub_idx].dropna()
        before, during = p[p.index.year < y], p[p.index.year == y]
        if len(during) < 100 or len(before) == 0:
            continue
        target = math.log(during.iloc[-1] / before.iloc[-1])      # same span as the replica's year

        in_year = mkt.dates[sub_idx].year == y
        prev = factors.get(y - 1)

        def gap(a):
            # Days before the year keep last year's fitted factor, exactly as a
            # full-length run will price them; only the year itself is varied.
            mkt.a[sub_idx] = np.where(in_year, a, prev if prev is not None else a)
            sub = Market(mkt.dates[sub_idx], mkt.S[sub_idx], mkt.TR[sub_idx], mkt.vix[sub_idx],
                         mkt.vix9[sub_idx], mkt.vix3m[sub_idx], mkt.r[sub_idx], mkt.a[sub_idx], 1.0)
            rets = simulate(sub, replica, start=str(sub.dates[0].date()))
            rets = rets[rets.index.year == y]
            return float(np.log1p(rets).sum()) - target

        a0, a1 = 0.6, 1.2
        for _ in range(18):
            mid = 0.5 * (a0 + a1)
            if gap(mid) < 0:
                a0 = mid
            else:
                a1 = mid
        factors[y] = 0.5 * (a0 + a1)
    series = pd.Series({y: factors.get(y, np.nan) for y in years}).ffill().bfill().fillna(DEFAULT_A)
    mkt.a = np.array([series[d.year] for d in mkt.dates])
    return series


def _real_gap(mkt: Market, strat: Strategy, real: pd.Series) -> float:
    r = simulate(mkt, strat)
    real = real.reindex(r.index).ffill().pct_change()
    k = r.index.intersection(real.dropna().index)
    return float((np.log1p(r[k]).mean() - np.log1p(real[k]).mean()) * 252)


CBOE_REPLICAS = {
    "PUTY": ("M", lambda: short_put_at(0.98)),
    "BXY": ("M", lambda: covered_call_at(1.02)),
    "BXMD": ("M", lambda: covered_call(0.30)),
    "PPUT": ("M", lambda: protective_put_at(0.95)),
}


def fit_smile(mkt: Market, cboe: pd.DataFrame, grid_put=(0.0, 0.1, 0.2, 0.3, 0.4, 0.6),
              grid_call=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5)) -> dict:
    """Pick the smile that makes replicas of Cboe's real OTM indices earn what they earned."""
    best = None
    for sp in grid_put:
        for sc in grid_call:
            mkt.skew_put, mkt.skew_call = sp, sc
            gaps = {n: _real_gap(mkt, Strategy(n, "x", t, L2, mk()), cboe[n])
                    for n, (t, mk) in CBOE_REPLICAS.items() if n in cboe}
            err = sum(g * g for g in gaps.values())
            if best is None or err < best[0]:
                best = (err, sp, sc, gaps)
    mkt.skew_put, mkt.skew_call = best[1], best[2]
    return {"skew_put": best[1], "skew_call": best[2], "gaps": best[3]}


def fit_weekly(mkt: Market, wput: pd.Series) -> float:
    """Weekly vol factor so the weekly at-the-money put-write matches Cboe's real WPUT."""
    strat = Strategy("weekly replica", "x", "W", L2, short_put_at(1.0))
    lo, hi = 0.6, 1.2
    for _ in range(12):
        mkt.weekly = 0.5 * (lo + hi)
        if _real_gap(mkt, strat, wput) > 0:
            hi = mkt.weekly
        else:
            lo = mkt.weekly
    mkt.weekly = 0.5 * (lo + hi)
    return mkt.weekly


def validate(df: pd.DataFrame, mkt: Market) -> dict:
    """How closely the replica tracks the real index month by month."""
    rep = simulate(mkt, Strategy("replica", "x", "M", L2, short_put(0.50)))
    wealth = (1 + rep).cumprod()
    put = df["^PUT"].reindex(wealth.index).ffill()
    mm = wealth.resample("ME").last().pct_change().dropna()
    pp = put.resample("ME").last().pct_change().dropna()
    k = mm.index.intersection(pp.index)
    return {"months": len(k), "corr": float(np.corrcoef(mm[k], pp[k])[0, 1]),
            "tracking_error": float((mm[k] - pp[k]).std() * math.sqrt(12)),
            "replica_vol": float(mm[k].std() * math.sqrt(12)), "index_vol": float(pp[k].std() * math.sqrt(12))}


# --- evaluation --------------------------------------------------------------------------


def _metrics(r: pd.Series, cash: pd.Series) -> dict:
    from .research import perf

    m = perf(r, cash)
    m["worst_month"] = float((1 + r).resample("ME").prod().sub(1).min()) if len(r) else float("nan")
    return m


def _alpha(r: pd.Series, bench: pd.Series, cash: pd.Series):
    from .research import alpha_series

    return alpha_series(r, pd.DataFrame({"spx": bench}), cash)


def _judge(rets: pd.DataFrame, tr: pd.Series, cash: pd.Series, split: str, draws: int, seed: int,
           meta: dict[str, tuple[str, str]]) -> list[Row]:
    """Rank on development years with the luck correction; spend holdout only on survivors."""
    from .research import reality_check

    days = rets.index
    dev = days < pd.Timestamp(split)

    def alphas(mask):
        return np.column_stack([_alpha(rets.loc[mask, n], tr[mask], cash[mask])[0].to_numpy() for n in rets])

    t_dev, p_dev = reality_check(alphas(dev), draws, seed=seed)
    _, p_full = reality_check(alphas(np.ones(len(days), bool)), draws, seed=seed)
    bench_hold = _metrics(tr[~dev], cash[~dev])
    rows = []
    for k, name in enumerate(rets.columns):
        fam, level = meta[name]
        a_dev = _alpha(rets.loc[dev, name], tr[dev], cash[dev])[0]
        a_full = _alpha(rets[name], tr, cash)[0]
        passed = bool(a_dev.mean() > 0 and p_dev[k] < PASS_P)
        row = Row(name, fam, level, _metrics(rets.loc[dev, name], cash[dev]), None, _metrics(rets[name], cash),
                  float(a_dev.mean() * 252), float(t_dev[k]), float(p_dev[k]), float(a_full.mean() * 252),
                  float(p_full[k]), passed)
        if passed:
            h = rets.loc[~dev, name]
            a_h = _alpha(h, tr[~dev], cash[~dev])[0]
            row.holdout = _metrics(h, cash[~dev])
            row.alpha_holdout = float(a_h.mean() * 252)
            row.t_holdout = float(a_h.mean() / a_h.std() * math.sqrt(len(a_h))) if a_h.std() > 0 else 0.0
            row.beats_spx = bool(row.holdout["cagr"] >= bench_hold["cagr"]
                                 and row.holdout["sharpe"] >= bench_hold["sharpe"])
        rows.append(row)
    rows.sort(key=lambda r: r.p_dev)
    return rows


def _cash(mkt: Market, days: pd.DatetimeIndex) -> pd.Series:
    gap = pd.Series(mkt.dates, index=mkt.dates).diff().dt.days
    c = pd.Series(mkt.r, index=mkt.dates).shift(1) * gap / 365.0
    return c.reindex(days).fillna(0.0)


def run(df: pd.DataFrame, cboe: pd.DataFrame | None = None, draws: int = 2000, seed: int = 11,
        strategies: list[Strategy] | None = None, split: str = SPLIT, calibrate_prices: bool = True) -> dict:
    mkt = Market.from_frame(df)
    factors = calibrate(df, mkt) if calibrate_prices else None
    weekly = fit_weekly(mkt, cboe["WPUT"]) if (calibrate_prices and cboe is not None and "WPUT" in cboe) else None
    check = validate(df, mkt) if calibrate_prices else None
    replica_gaps = ({n: _real_gap(mkt, Strategy(n, "x", t, L2, mk()), cboe[n])
                     for n, (t, mk) in CBOE_REPLICAS.items() if n in cboe}
                    if (calibrate_prices and cboe is not None) else {})

    # Part 1: the model family (calibrated prices).
    strategies = strategies if strategies is not None else candidates()
    rets = pd.DataFrame({s.name: simulate(mkt, s) for s in strategies}).dropna()
    days = rets.index
    tr = pd.Series(mkt.TR, index=mkt.dates).pct_change().reindex(days)
    cash = _cash(mkt, days)
    meta = {s.name: (s.family, s.level) for s in strategies}
    model_rows = _judge(rets, tr, cash, split, draws, seed, meta)
    by_name = {s.name: s for s in strategies}
    for row in model_rows:
        if row.passed:
            for mult in (0.5, 1.5):
                mkt.skew = mult
                alt = simulate(mkt, by_name[row.name]).reindex(days).fillna(0)
                row.skew_alphas[mult] = float(_alpha(alt, tr, cash)[0].mean() * 252)
            mkt.skew = 1.0
            row.robust = all(v > 0 for v in row.skew_alphas.values())

    # Part 2: the real record and switching rules between real streams.
    real_rows, real_n = [], 0
    if cboe is not None and len(cboe.columns):
        lv = cboe.copy()
        if "PUT" not in lv and "^PUT" in df:
            lv["PUT"] = df["^PUT"]
        lv = lv.reindex(mkt.dates).ffill().loc[REAL_START:]
        lv = lv[[c for c in CBOE_INDICES if c in lv and lv[c].notna().all()]]
        spx_tr = pd.Series(mkt.TR, index=mkt.dates).loc[REAL_START:]
        rdays = lv.index[1:]
        rcash = _cash(mkt, rdays)
        streams = {f"real: {c}": lv[c].pct_change() for c in lv}
        vix = pd.Series(mkt.vix, index=mkt.dates)
        v3 = pd.Series(mkt.vix3m, index=mkt.dates)
        spx = pd.Series(mkt.S, index=mkt.dates)
        ov = overlays(lv, spx_tr, rcash.reindex(lv.index).fillna(0), vix.loc[REAL_START:], v3.loc[REAL_START:],
                      spx.loc[REAL_START:])
        streams.update({f"rule: {k}": v for k, v in ov.items()})
        real = pd.DataFrame(streams).reindex(rdays).fillna(0.0)
        rmeta = {n: ("real index" if n.startswith("real:") else "rule on real indices",
                     "see the index rules" if n.startswith("real:") else "switching between real streams")
                 for n in real}
        real_rows = _judge(real, spx_tr.pct_change().reindex(rdays), rcash, split, draws, seed, rmeta)
        real_n = real.shape[1]

    bench = {"S&P 500 (total return)": tr}
    dev = days < pd.Timestamp(split)
    return {
        "rows": model_rows, "real_rows": real_rows, "n_candidates": len(strategies), "n_real": real_n,
        "start": str(days[0].date()), "end": str(days[-1].date()), "split": split, "validation": check,
        "weekly_factor": weekly, "replica_gaps": replica_gaps,
        "factors": {int(k): round(float(v), 3) for k, v in (factors.items() if factors is not None else [])},
        "benchmarks": {n: {"full": _metrics(b.fillna(0), cash), "dev": _metrics(b[dev].fillna(0), cash[dev]),
                           "holdout": _metrics(b[~dev].fillna(0), cash[~dev])} for n, b in bench.items()},
    }


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{digits}f}%"


def verdict_lines(rows: list[Row], model: bool) -> list[str]:
    survivors = [r for r in rows if r.passed]
    if not survivors:
        return ["  None passed the development test, so the holdout years were never touched."]
    out = []
    for r in survivors:
        h = r.holdout
        tags = []
        if model:
            tags.append("holds under a weaker and stronger smile" if r.robust else "DEPENDS ON THE SMILE")
        tags.append("beat the S&P 500 on return AND Sharpe" if r.beats_spx else "did not beat the S&P 500")
        out.append(f"  {r.name[:56]:<57} holdout alpha {_pct(r.alpha_holdout)} (t {r.t_holdout:.1f}), "
                   f"CAGR {_pct(h['cagr'])}, Sharpe {h['sharpe']:.2f}, worst drop {_pct(h['maxdd'], 0)}")
        out.append(f"      {'; '.join(tags)}. Needs: {r.level}")
    return out


def strong(rows: list[Row], model: bool) -> list[Row]:
    """The pre-declared bar for 'a strategy that works': passes the corrected
    development test, then in the untouched holdout has alpha t > 2 AND at least
    the S&P 500's return and Sharpe; model strategies must also survive the smile."""
    return [r for r in rows if r.passed and r.t_holdout is not None and r.t_holdout > 2
            and r.beats_spx and (r.robust or not model)]


def describe(res: dict, top: int = 15) -> list[str]:
    v = res.get("validation") or {}
    gaps = res.get("replica_gaps") or {}
    b = res["benchmarks"]["S&P 500 (total return)"]
    out = ["OPTIONS LAB: DOES ANY S&P 500 OPTIONS STRATEGY BEAT HOLDING THE INDEX?", "=" * 80,
           f"Holding the S&P 500, {res['start']} to {res['end']}: CAGR {_pct(b['full']['cagr'])}, "
           f"Sharpe {b['full']['sharpe']:.2f}, worst drop {_pct(b['full']['maxdd'], 0)}. "
           f"From {res['split'][:4]}: CAGR {_pct(b['holdout']['cagr'])}, Sharpe {b['holdout']['sharpe']:.2f}.",
           "Bar for 'works': passes the luck-corrected test on years before "
           f"{res['split'][:4]}, then in later untouched years has alpha t > 2 and at least the",
           "S&P 500's return AND Sharpe (and, for modelled strategies, survives a different smile).", ""]
    out += [f"PART 1. THE REAL RECORD: {res['n_real']} Cboe strategy indices (real quotes) and switching rules"]
    out += [f"{'  strategy (best first, development years)':<64}{'alpha':>8}{'t':>6}{'p':>6}{'CAGR':>8}"]
    for r in res["real_rows"][:top]:
        out.append(f"  {r.name[:61]:<62}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}{_pct(r.full['cagr']):>8}")
    out += verdict_lines(res["real_rows"], model=False) + [""]
    out += [f"PART 2. THE MODEL: {res['n_candidates']} strategies, prices rebuilt from the VIX family"]
    if v:
        out.append(f"  Check: replica of Cboe's real put-write index, correlation {v['corr']:.2f}, "
                   f"tracking error {v['tracking_error'] * 100:.1f}%/yr.")
    if gaps:
        out.append("  Smile fitted to real OTM indices, yearly return gap: "
                   + ", ".join(f"{k} {g * 100:+.1f}%" for k, g in gaps.items())
                   + (f"; weekly factor {res['weekly_factor']:.2f} (fitted to WPUT)." if res.get("weekly_factor") else "."))
    out += [f"{'  strategy (best first, development years)':<64}{'alpha':>8}{'t':>6}{'p':>6}{'drop':>8}"]
    for r in res["rows"][:top]:
        out.append(f"  {r.name[:61]:<62}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}{_pct(r.full['maxdd'], 0):>8}")
    out += verdict_lines(res["rows"], model=True) + [""]
    if res.get("other"):
        out.append("PART 3. OTHER INDEXES (real Cboe indices vs holding the same index fund)")
        for o in res["other"]:
            a = o["all"]
            out.append(f"  {o['index']:<5} {o['what'][:52]:<53} CAGR {_pct(a['cagr'])} vs {o['fund']} "
                       f"{_pct(a['fund_cagr'])}, alpha {_pct(a['alpha'])} (t {a['t']:+.1f})")
        out.append("")
    winners = strong(res["real_rows"], False) + strong(res["rows"], True)
    if winners:
        out.append("RESULT: " + "; ".join(w.name for w in winners) + " cleared every bar.")
        out.append("That is a reason to paper-trade it on the scorecard, not to bet on it yet.")
    else:
        out.append("RESULT: nothing cleared every bar. On this evidence no S&P 500 options strategy")
        out.append("beats simply holding the index; the ones that pass early do it by taking less risk,")
        out.append("and earn less than the index in later years.")
    return out
