"""Buy-the-red-day backtests: put a fixed stake in on down days, sell on rules.

The question the owner asked: "if I buy $500 every red day and sell after a
green day once it is up X%, what would I have made?", across a grid of buy
and sell rules, on each ticker's full history.

How it avoids fooling itself:

  * LOT-LEVEL ACCOUNTING. Every $500 buy is its own lot with its own entry
    price. A lot can only be sold on a LATER day than it was bought.
  * NO LOOK-AHEAD. Every decision on day t reads closes up to t. Fills are at
    that close (`fill="close"`, a market-on-close order) or, stricter, at the
    NEXT day's open (`fill="next_open"`, so you only act on a finished day).
    tests/test_dipbuy.py perturbs future prices and checks earlier trades stay put.
  * COSTS. A slippage charge on every buy and sell (5 bps by default; the
    broker charges no commission). Taxes are NOT modelled: see `describe`.
  * SAME MONEY IN, EVERY RULE. Every buy is new money, so for one buy rule
    every sell rule puts in the same dollars on the same dates and the final
    values compare directly. Sale proceeds are parked either in cash earning
    the 13-week T-bill rate, or in SPY (the policy in STRATEGY.md: proceeds go
    to a broad index fund). A rule that sells early and sits in cash pays for
    the idle money honestly.
  * MONEY-WEIGHTED RETURN. With money going in on hundreds of dates, a single
    "% return" lies. The headline is the IRR (annual % return on the money
    actually put in, when it was put in), next to plain profit and the final
    value per dollar put in.
  * BENCHMARKS on the same ticker: the same buy days never sold (isolates the
    sell rule), $500 every trading day (isolates the red-day timing), and the
    same number of buys on random days (how much of the result is luck).
  * ERAS. Each combination is re-run inside each decade, so one lucky decade
    cannot carry the verdict. Hundreds of combinations are tried, so the best
    one looks good partly by chance; trust patterns that hold across tickers
    and decades, not the single top row.

Pure maths over pandas/numpy; the network lives in `load_prices`.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_TICKERS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
RATE_TICKER = "^IRX"            # 13-week T-bill yield, % a year
STAKE = 500.0
DEFAULT_COST = 0.0005           # 5 bps slippage per buy and per sell
YEAR_DAYS = 365.25


# --- data ---------------------------------------------------------------------


def load_prices(tickers: list[str], cache_path: Path | None = None,
                max_age_hours: float = 12.0) -> dict:
    """Adjusted open/close for every ticker plus the T-bill rate, cached on disk.

    Returns {"close": DataFrame, "open": DataFrame, "rate": Series (decimal/yr)}.
    """
    import time

    raw = None
    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                raw = pd.read_parquet(cache_path)
                if not all(t in raw["Close"] for t in tickers):
                    raw = None
            except Exception:
                raw = None
    if raw is None:
        import yfinance as yf

        raw = yf.download(sorted({*tickers, RATE_TICKER}), period="max",
                          auto_adjust=True, progress=False)
        if cache_path is not None:
            try:
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
                raw.to_parquet(cache_path)
            except Exception:
                pass
    close, open_ = raw["Close"], raw["Open"]
    missing = [t for t in tickers if t not in close or close[t].dropna().empty]
    if missing:
        raise RuntimeError(f"no price data for {', '.join(missing)}")
    rate = (close[RATE_TICKER] / 100.0) if RATE_TICKER in close else pd.Series(0.0, index=close.index)
    return {"close": close[tickers], "open": open_[tickers], "rate": rate}


def series_for(prices: dict, ticker: str) -> pd.DataFrame:
    """One ticker's clean history: close, open, cash rate, and SPY (where proceeds
    can be parked), on the ticker's own trading days."""
    c = prices["close"][ticker].dropna()
    c = c[c > 0]
    o = prices["open"][ticker].reindex(c.index)
    o = o.where(o > 0).fillna(c)
    rate = prices["rate"].reindex(c.index).ffill().fillna(0.0).clip(lower=0.0)
    out = pd.DataFrame({"close": c, "open": o, "rate": rate})
    if "SPY" in prices["close"]:
        sc = prices["close"]["SPY"].reindex(c.index)
        so = prices["open"]["SPY"].reindex(c.index)
        out["spy_close"] = sc
        out["spy_open"] = so.where(so > 0).fillna(sc)
    return out


# --- buy rules: closes -> bool mask of signal days -------------------------------


def _ret(close: pd.Series) -> pd.Series:
    return close.pct_change()


def red_any(close):
    return _ret(close) < 0


def red_1(close):
    return _ret(close) <= -0.01


def red_2(close):
    return _ret(close) <= -0.02


def red_streak2(close):
    red = _ret(close) < 0
    return red & red.shift(1, fill_value=False)


def red_in_correction(close):
    """A red day while the close is 10%+ under its 52-week high."""
    high = close.rolling(252, min_periods=60).max()
    return (_ret(close) < 0) & (close <= 0.90 * high)


def every_day(close):
    return pd.Series(True, index=close.index) & _ret(close).notna()


BUY_RULES = {
    "red": ("Every red day", red_any),
    "red1": ("Down 1%+ day", red_1),
    "red2": ("Down 2%+ day", red_2),
    "streak2": ("2nd+ red day in a row", red_streak2),
    "correction": ("Red day, 10%+ below 52-wk high", red_in_correction),
}
BASELINE_BUY = ("daily", "Every trading day (plain DCA)", every_day)


# --- sell rules ---------------------------------------------------------------


@dataclass(frozen=True)
class SellRule:
    name: str
    label: str
    kind: str               # hold | lot | greenday | position | time
    target: float = 0.0     # lot/position gain (net of costs) to sell at
    green: float = 0.0      # daily gain that triggers a greenday sale
    winners_only: bool = False
    stop: float = 0.0       # sell a lot this far under its entry (0 = none)
    max_days: int = 0       # sell a lot after this many trading days (0 = none)


SELL_RULES = [
    SellRule("hold", "Never sell", "hold"),
    SellRule("lot1", "Each buy: sell at +1% (green day)", "lot", target=0.01),
    SellRule("lot2", "Each buy: sell at +2% (green day)", "lot", target=0.02),
    SellRule("lot3", "Each buy: sell at +3% (green day)", "lot", target=0.03),
    SellRule("lot5", "Each buy: sell at +5% (green day)", "lot", target=0.05),
    SellRule("lot10", "Each buy: sell at +10% (green day)", "lot", target=0.10),
    SellRule("lot20", "Each buy: sell at +20% (green day)", "lot", target=0.20),
    SellRule("lot50", "Each buy: sell at +50% (green day)", "lot", target=0.50),
    SellRule("green1", "Sell everything on a +1% day", "greenday", green=0.01),
    SellRule("green2", "Sell everything on a +2% day", "greenday", green=0.02),
    SellRule("green3", "Sell everything on a +3% day", "greenday", green=0.03),
    SellRule("green2w", "Sell winning buys on a +2% day", "greenday", green=0.02, winners_only=True),
    SellRule("pos5", "Whole position: sell all at +5%", "position", target=0.05),
    SellRule("pos10", "Whole position: sell all at +10%", "position", target=0.10),
    SellRule("pos20", "Whole position: sell all at +20%", "position", target=0.20),
    SellRule("lot5s5", "Each buy: +5% target / -5% stop", "lot", target=0.05, stop=0.05),
    SellRule("lot10s10", "Each buy: +10% target / -10% stop", "lot", target=0.10, stop=0.10),
    SellRule("lot5t20", "Each buy: +5% or after 20 days", "lot", target=0.05, max_days=20),
    SellRule("lot10t60", "Each buy: +10% or after 60 days", "lot", target=0.10, max_days=60),
    SellRule("time20", "Each buy: sell after 20 days", "time", max_days=20),
]
SELL_BY_NAME = {s.name: s for s in SELL_RULES}


# --- the simulator ------------------------------------------------------------


@dataclass
class Result:
    buys: int = 0
    sells: int = 0
    new_money: float = 0.0
    final_value: float = 0.0
    profit: float = 0.0
    multiple: float = 0.0          # final value per dollar of new money
    irr: float = float("nan")      # money-weighted annual return
    win_rate: float = float("nan")  # of closed lots
    avg_closed: float = float("nan")  # average return of a closed lot
    median_days: float = float("nan")  # trading days a closed lot was held
    longest_wait: int = 0          # most trading days any buy was held (sold or still open)
    open_lots: int = 0
    open_underwater: int = 0
    max_tied: float = 0.0          # most money (cost basis) ever in the stock at once
    exposure: float = 0.0          # average share of the account in the stock
    worst_underwater: float = 0.0  # worst open-position value vs its cost
    short_term_sales: int = 0      # lots sold within a year (taxed as income)
    years: float = 0.0
    start: str = ""
    end: str = ""
    wealth: list = field(default_factory=list, repr=False)   # [(date, value, new money)] monthly

    def to_dict(self, with_curve: bool = False) -> dict:
        d = asdict(self)
        if not with_curve:
            d.pop("wealth")
        out = {}
        for k, v in d.items():
            if isinstance(v, (float, np.floating)):
                v = float(v)
                v = v if math.isfinite(v) else None
            out[k] = v
        return out


def irr(flow_years: np.ndarray, flows: np.ndarray, final_year: float, final_value: float) -> float:
    """Annual rate r with sum(flow_i * (1+r)^(T - t_i)) == final_value.

    `flows` are the positive amounts of new money put in at `flow_years`.
    """
    if len(flows) == 0 or final_value <= 0:
        return float("nan") if len(flows) == 0 else -1.0
    span = final_year - flow_years

    def gap(r):
        return float(np.sum(flows * np.power(1.0 + r, span))) - final_value

    lo, hi = -0.99, 10.0
    if gap(lo) > 0:
        return lo
    if gap(hi) < 0:
        return hi
    for _ in range(200):
        mid = (lo + hi) / 2
        if gap(mid) > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-9:
            break
    return (lo + hi) / 2


def simulate(df: pd.DataFrame, buy_mask, rule: SellRule, stake: float = STAKE,
             cost: float = DEFAULT_COST, fill: str = "close", dest: str = "cash",
             curve: bool = False) -> Result:
    """Run one buy mask + sell rule over one ticker's history.

    `df` has columns close, open, rate (decimal a year) on trading days, and
    for dest == "spy" also spy_close / spy_open (NaN where SPY did not exist).
    Every buy is NEW money, so every sell rule on the same buy days puts in
    exactly the same dollars on the same dates and their final values compare
    directly. Sale proceeds go to `dest`: "cash" (earns the T-bill rate) or
    "spy" (bought at the same fill; held in cash until SPY exists).
    Signals are read from the close of day t; trades fill at that close, or at
    the open of day t+1 when fill == "next_open".
    """
    close = df["close"].to_numpy(float)
    opn = df["open"].to_numpy(float)
    rate = df["rate"].to_numpy(float)
    to_spy = dest == "spy"
    if to_spy:
        s_close = df["spy_close"].to_numpy(float)
        s_open = df["spy_open"].to_numpy(float)
    dates = df.index
    n = len(close)
    buy = np.asarray(buy_mask, dtype=bool)
    if len(buy) != n:
        raise ValueError("buy mask must match the price history")
    day_ret = np.empty(n)
    day_ret[0] = np.nan
    day_ret[1:] = close[1:] / close[:-1] - 1.0
    cal_days = (dates - dates[0]).days.to_numpy()
    years = cal_days / YEAR_DAYS
    gap_days = np.diff(cal_days, prepend=0)
    next_open = fill == "next_open"

    cap = int(buy.sum()) + 1
    shares = np.zeros(cap)
    basis = np.zeros(cap)        # dollars paid including the cost
    entry_day = np.zeros(cap, dtype=int)
    is_open = np.zeros(cap, dtype=bool)
    n_lots = 0

    cash = 0.0
    spy_shares = 0.0
    new_money = 0.0
    flow_y, flow_v = [], []
    closed_ret, closed_days = [], []
    short_term = 0
    max_tied = 0.0
    worst_uw = 0.0
    expo_sum, expo_n = 0.0, 0
    wealth = []
    pending_sell = None          # lot indices to sell at the next open
    pending_buy = False

    def park(amount, t, at_open):
        nonlocal cash, spy_shares
        if to_spy:
            px = s_open[t] if at_open else s_close[t]
            if np.isfinite(px) and px > 0:
                spy_shares += amount / (px * (1.0 + cost))
                return
        cash += amount

    def do_sell(idx, px, t, at_open):
        nonlocal short_term
        proceeds = shares[idx] * px * (1.0 - cost)
        closed_ret.extend((proceeds / basis[idx] - 1.0).tolist())
        closed_days.extend((t - entry_day[idx]).tolist())
        short_term += int(np.sum(cal_days[t] - cal_days[entry_day[idx]] < 365))
        is_open[idx] = False
        park(float(proceeds.sum()), t, at_open)

    def do_buy(px, t):
        nonlocal new_money, n_lots
        new_money += stake
        flow_y.append(years[t])
        flow_v.append(stake)
        shares[n_lots] = stake / (px * (1.0 + cost))
        basis[n_lots] = stake
        entry_day[n_lots] = t
        is_open[n_lots] = True
        n_lots += 1

    for t in range(n):
        # cash earns the T-bill rate for the calendar days since the last bar
        if t > 0 and cash > 0:
            cash *= 1.0 + rate[t - 1] * gap_days[t] / 365.0
            if to_spy and np.isfinite(s_close[t]) and s_close[t] > 0:
                spy_shares += cash / (s_close[t] * (1.0 + cost))   # sweep once SPY exists
                cash = 0.0

        # orders decided at yesterday's close fill at today's open
        if next_open:
            if pending_sell is not None and len(pending_sell):
                do_sell(pending_sell, opn[t], t, True)
            if pending_buy:
                do_buy(opn[t], t)
            pending_sell, pending_buy = None, False

        # --- sell decision at today's close (lots bought on an earlier day only)
        live = np.flatnonzero(is_open[:n_lots])
        if live.size and rule.kind != "hold":
            live = live[entry_day[live] < t]
        if live.size and rule.kind != "hold":
            value = shares[live] * close[t] * (1.0 - cost)
            gain = value / basis[live] - 1.0
            green = day_ret[t] > 0
            sell = np.zeros(live.size, dtype=bool)
            if rule.kind == "lot":
                if rule.target > 0:
                    sell |= green & (gain >= rule.target)
                if rule.stop > 0:
                    sell |= gain <= -rule.stop
                if rule.max_days > 0:
                    sell |= (t - entry_day[live]) >= rule.max_days
            elif rule.kind == "time":
                sell |= (t - entry_day[live]) >= rule.max_days
            elif rule.kind == "greenday":
                if day_ret[t] >= rule.green:
                    sell[:] = gain > 0 if rule.winners_only else True
            elif rule.kind == "position":
                if green and value.sum() / basis[live].sum() - 1.0 >= rule.target:
                    sell[:] = True
            idx = live[sell]
            if idx.size:
                if next_open:
                    pending_sell = idx
                else:
                    do_sell(idx, close[t], t, False)

        # --- buy decision at today's close
        if buy[t]:
            if next_open:
                pending_buy = t + 1 < n
            else:
                do_buy(close[t], t)

        # --- bookkeeping at today's close
        live = np.flatnonzero(is_open[:n_lots])
        pos_value = float(shares[live].sum() * close[t]) if live.size else 0.0
        pos_cost = float(basis[live].sum()) if live.size else 0.0
        max_tied = max(max_tied, pos_cost)
        if pos_cost > 0:
            worst_uw = min(worst_uw, pos_value / pos_cost - 1.0)
        parked = cash + (spy_shares * s_close[t] if to_spy and spy_shares else 0.0)
        total = pos_value + parked
        if total > 0:
            expo_sum += pos_value / total
            expo_n += 1
        if curve and (t == n - 1 or dates[t].month != dates[min(t + 1, n - 1)].month):
            wealth.append((dates[t].strftime("%Y-%m-%d"), round(total, 2), round(new_money, 2)))

    live = np.flatnonzero(is_open[:n_lots])
    final_px = close[-1] * (1.0 - cost)          # mark open lots as if sold today
    open_value = float(shares[live].sum() * final_px)
    parked = cash + (spy_shares * s_close[-1] * (1.0 - cost) if to_spy and spy_shares else 0.0)
    final_value = parked + open_value
    open_gain = shares[live] * final_px / basis[live] - 1.0 if live.size else np.array([])

    r = Result()
    r.buys = n_lots
    r.sells = len(closed_ret)
    r.new_money = round(new_money, 2)
    r.final_value = round(final_value, 2)
    r.profit = round(final_value - new_money, 2)
    r.multiple = final_value / new_money if new_money > 0 else float("nan")
    r.irr = irr(np.array(flow_y), np.array(flow_v), years[-1], final_value) if flow_v else float("nan")
    if closed_ret:
        cr = np.array(closed_ret)
        r.win_rate = float(np.mean(cr > 0))
        r.avg_closed = float(np.mean(cr))
        r.median_days = float(np.median(closed_days))
    ages = [int(max(closed_days))] if closed_days else []
    if live.size:
        ages.append(int(n - 1 - entry_day[live].min()))
    r.longest_wait = max(ages) if ages else 0
    r.open_lots = int(live.size)
    r.open_underwater = int(np.sum(open_gain < 0))
    r.max_tied = round(max_tied, 2)
    r.exposure = expo_sum / expo_n if expo_n else 0.0
    r.worst_underwater = worst_uw
    r.short_term_sales = short_term
    r.years = float(years[-1])
    r.start, r.end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")
    r.wealth = wealth
    return r


def paced(df: pd.DataFrame, buy_mask, per_day: float = STAKE,
          cost: float = DEFAULT_COST) -> dict:
    """Is it worth WAITING for signal days? The same savings arrive every trading
    day and wait in T-bills; on a signal day all waiting cash buys the stock,
    which is then held. Every rule gets identical deposits on identical dates,
    so unlike a per-buy IRR the time spent waiting in cash is charged."""
    close = df["close"].to_numpy(float)
    rate = df["rate"].to_numpy(float)
    buy = np.asarray(buy_mask, dtype=bool)
    cal_days = (df.index - df.index[0]).days.to_numpy()
    gap = np.diff(cal_days, prepend=0)
    cash = shares = 0.0
    waited, wait_sum, buys = 0, 0, 0
    for t in range(len(close)):
        if t > 0:
            cash *= 1.0 + rate[t - 1] * gap[t] / 365.0
        cash += per_day
        waited += 1
        if buy[t]:
            shares += cash / (close[t] * (1.0 + cost))
            cash = 0.0
            wait_sum += waited - 1
            waited, buys = 0, buys + 1
    final = shares * close[-1] * (1.0 - cost) + cash
    years = cal_days / YEAR_DAYS
    n = len(close)
    return {"irr": irr(years, np.full(n, per_day), years[-1], final),
            "final_value": round(final, 2), "new_money": round(per_day * n, 2),
            "buys": buys, "cash_left": round(cash, 2)}


def random_masks(signal_days: int, n: int, draws: int, seed: int = 7) -> list[np.ndarray]:
    """Masks with the same number of buy days, placed at random (never day 0)."""
    rng = np.random.default_rng(seed)
    k = min(signal_days, n - 1)
    out = []
    for _ in range(draws):
        m = np.zeros(n, dtype=bool)
        m[1 + rng.choice(n - 1, size=k, replace=False)] = True
        out.append(m)
    return out


ERAS = [("1980s", "1980-01-01", "1989-12-31"), ("1990s", "1990-01-01", "1999-12-31"),
        ("2000s", "2000-01-01", "2009-12-31"), ("2010s", "2010-01-01", "2019-12-31"),
        ("2020s", "2020-01-01", "2099-12-31")]
MIN_ERA_YEARS = 5.0


def run_ticker(df: pd.DataFrame, cost: float = DEFAULT_COST, fill: str = "close",
               dest: str = "cash", draws: int = 100, eras: bool = True,
               curves: bool = True) -> dict:
    """Every buy rule x sell rule on one ticker, plus baselines and decade splits."""
    out = {"start": df.index[0].strftime("%Y-%m-%d"), "end": df.index[-1].strftime("%Y-%m-%d"),
           "years": round((df.index[-1] - df.index[0]).days / YEAR_DAYS, 1),
           "fill": fill, "dest": dest, "cost": cost, "combos": [], "baselines": {}, "eras": {}}
    close = df["close"]
    masks = {k: fn(close).fillna(False).to_numpy() for k, (_, fn) in BUY_RULES.items()}

    daily = simulate(df, every_day(close).to_numpy(), SELL_BY_NAME["hold"], cost=cost,
                     fill=fill, curve=curves)
    out["baselines"]["daily_hold"] = daily.to_dict(with_curve=curves)
    out["baselines"]["paced_daily"] = paced(df, every_day(close).to_numpy(), cost=cost)
    for bkey, mask in masks.items():
        out["baselines"][f"paced_{bkey}"] = paced(df, mask, cost=cost)
        for rule in SELL_RULES:
            res = simulate(df, mask, rule, cost=cost, fill=fill, dest=dest, curve=curves and rule.name in CURVE_RULES)
            out["combos"].append({"buy": bkey, "sell": rule.name, **res.to_dict(with_curve=bool(res.wealth))})
        # luck check: the same number of buys on random days, never sold
        if draws:
            rand = [simulate(df, m, SELL_BY_NAME["hold"], cost=cost, fill=fill, dest=dest).irr
                    for m in random_masks(int(mask.sum()), len(df), draws)]
            rand = np.array([x for x in rand if math.isfinite(x)])
            hold_irr = next(c["irr"] for c in out["combos"] if c["buy"] == bkey and c["sell"] == "hold")
            out["baselines"][f"random_{bkey}"] = {
                "draws": int(rand.size), "median_irr": float(np.median(rand)),
                "p5": float(np.percentile(rand, 5)), "p95": float(np.percentile(rand, 95)),
                "beat_share": float(np.mean(rand < hold_irr)) if hold_irr is not None else None,
            }
    if eras:
        for name, a, b in ERAS:
            sub = df.loc[a:b]
            if len(sub) < 2 or (sub.index[-1] - sub.index[0]).days / YEAR_DAYS < MIN_ERA_YEARS:
                continue
            rows = []
            sclose = sub["close"]
            sub_daily = simulate(sub, every_day(sclose).to_numpy(), SELL_BY_NAME["hold"], cost=cost, fill=fill, dest=dest)
            for bkey, (_, fn) in BUY_RULES.items():
                m = fn(sclose).fillna(False).to_numpy()
                for rule in SELL_RULES:
                    res = simulate(sub, m, rule, cost=cost, fill=fill, dest=dest)
                    rows.append({"buy": bkey, "sell": rule.name, "irr": res.to_dict()["irr"],
                                 "profit": res.profit, "multiple": res.to_dict()["multiple"],
                                 "new_money": res.new_money})
            pace = {b: paced(sub, fn(sclose).fillna(False).to_numpy(), cost=cost)["irr"]
                    for b, (_, fn) in BUY_RULES.items()}
            pace["daily"] = paced(sub, every_day(sclose).to_numpy(), cost=cost)["irr"]
            out["eras"][name] = {"start": sub.index[0].strftime("%Y-%m-%d"),
                                 "end": sub.index[-1].strftime("%Y-%m-%d"),
                                 "daily_irr": sub_daily.to_dict()["irr"], "rows": rows,
                                 "paced": pace}
    return out


CURVE_RULES = {"hold", "lot5", "lot10", "green2", "pos10"}


def era_consistency(res: dict) -> dict:
    """For each combo: in how many decades it beat the same buys held forever (by IRR)."""
    tally = {}
    for era in res["eras"].values():
        held = {r["buy"]: r["irr"] for r in era["rows"] if r["sell"] == "hold"}
        for r in era["rows"]:
            key = (r["buy"], r["sell"])
            won, seen = tally.get(key, (0, 0))
            if r["irr"] is not None and held.get(r["buy"]) is not None:
                won += int(r["irr"] > held[r["buy"]])
                seen += 1
            tally[key] = (won, seen)
    return {f"{b}|{s}": v for (b, s), v in tally.items()}


def run(prices: dict, tickers: list[str], cost: float = DEFAULT_COST, fill: str = "close",
        dest: str = "cash", draws: int = 100, eras: bool = True, curves: bool = True) -> dict:
    out = {"tickers": {}, "buy_rules": {k: v[0] for k, v in BUY_RULES.items()},
           "sell_rules": {s.name: s.label for s in SELL_RULES}, "fill": fill, "dest": dest, "cost": cost,
           "stake": STAKE}
    for t in tickers:
        res = run_ticker(series_for(prices, t), cost=cost, fill=fill, dest=dest, draws=draws,
                         eras=eras, curves=curves)
        res["consistency"] = era_consistency(res)
        out["tickers"][t] = res
    return out


# --- plain-language summary ---------------------------------------------------


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x * 100:+.{digits}f}%"


def describe(res: dict, top: int = 5) -> list[str]:
    lines = [f"Buy ${res['stake']:.0f} on signal days; fills at "
             f"{'the next open' if res['fill'] == 'next_open' else 'the close'}, "
             f"{res['cost'] * 1e4:.0f} bps slippage each way, idle cash earns T-bills.",
             "IRR = annual return on the money actually put in. Taxes not included.", ""]
    for t, r in res["tickers"].items():
        combos = [c for c in r["combos"] if c["irr"] is not None]
        daily = r["baselines"]["daily_hold"]
        lines.append(f"== {t}  {r['start']} to {r['end']} ({r['years']} yrs) ==")
        lines.append(f"  Benchmark, $500 every day and never sell: IRR {_pct(daily['irr'])}, "
                     f"{daily['multiple']:.2f}x the money put in")
        pd_ = r["baselines"].get("paced_daily")
        if pd_:
            lines.append("  Waiting for the signal (same savings every day, held in T-bills until a signal day):")
            for bkey, label in res["buy_rules"].items():
                p = r["baselines"][f"paced_{bkey}"]
                lines.append(f"    {label:<34} IRR {_pct(p['irr'])} vs investing daily {_pct(pd_['irr'])}")
        for bkey, label in res["buy_rules"].items():
            hold = next(c for c in r["combos"] if c["buy"] == bkey and c["sell"] == "hold")
            rnd = r["baselines"].get(f"random_{bkey}")
            luck = (f"; random days {_pct(rnd['median_irr'])} (beats {rnd['beat_share'] * 100:.0f}% of them)"
                    if rnd else "")
            lines.append(f"  {label:<34} never sold: IRR {_pct(hold['irr'])}{luck}")
        sellers = [c for c in combos if c["sell"] != "hold"]
        lines.append(f"  Best {top} that sell, by IRR (of {len(sellers)}; "
                     f"'beat hold' = beat the same buys never sold):")
        for c in sorted(sellers, key=lambda c: -c["irr"])[:top]:
            won, seen = r["consistency"].get(f"{c['buy']}|{c['sell']}", (0, 0))
            lines.append(f"    {res['buy_rules'][c['buy']]:<32} + {res['sell_rules'][c['sell']]:<36} "
                         f"IRR {_pct(c['irr'])}  profit ${c['profit']:,.0f} on ${c['new_money']:,.0f}  "
                         f"beat hold in {won}/{seen} decades")
        lines.append("")
    return lines
