"""Crypto lab: what actually worked on real coin history (`python -m stocksage.cryptolab`).

The measurements behind `crypto.LESSONS` and the playbook's crypto rules:
  1. Trend filter on BTC/ETH (hold only above the N-day average; signal at the
     close, earn the next day; 0.5% per side for Robinhood's spread), split
     before/after 2021 so a lookback that only worked once is visible.
  2. Cross-coin momentum (hold last month's top 3 majors, weekly) vs holding all.
  3. What follows big up/down days and weeks, majors vs meme coins.
  4. Meme coins: distance from peak, beta to BTC, first year vs after.
  5. Crypto Fear & Greed buckets vs BTC 7/30 days later, both halves of history.

Caveats that apply to every number: the coins are today's survivors (dead
coins are missing, which flatters memes most); events in different coins
cluster on the same days, so the real sample is smaller than the counts; and
SHIB/PEPE/BONK/FLOKI are left out because Yahoo rounds their sub-cent prices
to zero. Needs yfinance (full environment). Read-only; trains nothing.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

COST = 0.005
MAJORS = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "LTC", "BCH", "DOT", "XLM", "AAVE", "ETC"]
MEMES = {"DOGE": "DOGE-USD", "WIF": "WIF-USD", "POPCAT": "POPCAT28782-USD", "PNUT": "PNUT-USD"}
SPLIT = "2021-01-01"


def stats(r: pd.Series) -> dict | None:
    r = r.dropna()
    if len(r) < 100:
        return None
    eq = (1 + r).cumprod()
    return {"cagr": float(eq.iloc[-1] ** (365 / len(r)) - 1), "sharpe": float(r.mean() / r.std() * np.sqrt(365)),
            "max_drop": float((eq / eq.cummax() - 1).min())}


def trend_filter(close: pd.Series, n: int, cost: float = COST) -> pd.Series:
    """Daily returns of holding only while yesterday's close was above its n-day average."""
    close = close.dropna()
    on = (close > close.rolling(n).mean()).astype(float).shift(1).fillna(0)
    return on * close.pct_change().fillna(0) - on.diff().abs().fillna(0) * cost


def after_moves(closes: dict[str, pd.Series], cond, horizons=(1, 7, 30)) -> dict | None:
    """Forward returns after every day where cond(returns, prices) holds, pooled across coins."""
    rows = []
    for p in closes.values():
        p = p.dropna()
        hits = np.where(np.asarray(cond(p.pct_change(), p), dtype=bool))[0]
        for i in hits:
            if i + max(horizons) < len(p):
                rows.append([p.iloc[i + h] / p.iloc[i] - 1 for h in horizons])
    if not rows:
        return None
    a = np.array(rows)
    return {"n": len(a), **{f"d{h}": {"median": float(np.median(a[:, k])), "up": float((a[:, k] > 0).mean())}
                            for k, h in enumerate(horizons)}}


def fear_greed_buckets(fg: pd.Series, btc: pd.Series, days: int = 30) -> pd.DataFrame:
    fwd = (btc.shift(-days) / btc - 1).reindex(fg.index)
    df = pd.DataFrame({"fg": fg, "fwd": fwd}).dropna()
    df["bucket"] = pd.cut(df.fg, [0, 20, 40, 60, 80, 100],
                          labels=["extreme fear", "fear", "neutral", "greed", "extreme greed"])
    return df.groupby("bucket", observed=True).fwd.agg(days="size", median="median",
                                                       up=lambda s: (s > 0).mean())


def load() -> tuple[pd.DataFrame, pd.Series | None]:  # pragma: no cover - network
    import yfinance as yf

    syms = {m: f"{m}-USD" for m in MAJORS} | MEMES
    px = yf.download(list(syms.values()), period="max", interval="1d", auto_adjust=True,
                     progress=False)["Close"].rename(columns={v: k for k, v in syms.items()}).iloc[:-1]
    fg = None
    try:
        from .crypto import _get

        rows = json.loads(_get("https://api.alternative.me/fng/?limit=0&format=json"))["data"]
        fg = pd.Series({pd.Timestamp(int(x["timestamp"]), unit="s").normalize(): int(x["value"]) for x in rows})
        fg = fg.sort_index()
    except Exception:
        pass
    return px, fg


def run() -> str:  # pragma: no cover - network
    px, fg = load()
    out = ["CRYPTO LAB (0.5% per side costs; survivors only; see module notes)", ""]
    out.append("1. Trend filter vs holding (Sharpe, max drop), before / after 2021")
    for coin in ("BTC", "ETH"):
        for n in (20, 50, 100, 200):
            r, hold = trend_filter(px[coin], n), px[coin].pct_change()
            for lab, sl in (("pre-2021", slice(None, "2020-12-31")), ("2021+", slice(SPLIT, None))):
                a, b = stats(r[sl]), stats(hold[sl])
                if a and b:
                    out.append(f"  {coin} {n:>3}d {lab:<8} trend sharpe {a['sharpe']:.2f} drop {a['max_drop']:+.0%}"
                               f" | hold sharpe {b['sharpe']:.2f} drop {b['max_drop']:+.0%}")
    out += ["", "2. After big moves (median, share up: next day / 7 days / 30 days)"]
    groups = {"majors": {c: px[c] for c in MAJORS}, "memes": {c: px[c] for c in MEMES}}
    tests = {"day up >15%": lambda r, p: r > 0.15, "day down >15%": lambda r, p: r < -0.15,
             "week up >50%": lambda r, p: p / p.shift(7) - 1 > 0.5, "week down >30%": lambda r, p: p / p.shift(7) - 1 < -0.3,
             "crosses above 50d": lambda r, p: (p > p.rolling(50).mean()) & (p.shift(1) <= p.shift(1).rolling(50).mean())}
    for g, closes in groups.items():
        for lab, cond in tests.items():
            res = after_moves(closes, cond)
            if res:
                out.append(f"  {g:<6} {lab:<18} n={res['n']:>4} " + " | ".join(
                    f"{res[k]['median']:+.1%} ({res[k]['up']:.0%})" for k in ("d1", "d7", "d30")))
    out += ["", "3. Meme coins vs BTC since 2024"]
    r = px.pct_change(fill_method=None)[SPLIT.replace("2021", "2024"):]
    for c in MEMES:
        s = r[[c, "BTC"]].dropna()
        if len(s) > 100:
            p = px[c].dropna()
            out.append(f"  {c:<7} beta {s[c].cov(s.BTC) / s.BTC.var():.1f}x BTC, {p.iloc[-1] / p.max() - 1:+.0%} from peak")
    if fg is not None:
        out += ["", "4. Crypto Fear & Greed vs BTC 30 days later (both halves)"]
        half = fg.index[len(fg) // 2]
        for lab, part in (("first half", fg[:half]), ("second half", fg[half:])):
            t = fear_greed_buckets(part, px["BTC"])
            out.append(f"  {lab}: " + "  ".join(f"{k} {v['median']:+.1%} ({v['up']:.0%} up)" for k, v in t.iterrows()))
    return "\n".join(out)


if __name__ == "__main__":  # pragma: no cover
    print(run())
