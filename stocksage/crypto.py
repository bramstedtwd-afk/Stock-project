"""The crypto market at a glance, for the trader's crypto test. Context, never a call.

Free sources, each optional (one failing never stops the others):
  * OKX: BTC and ETH daily closes, for where each sits against its 50- and
    100-day average; and perpetual-futures funding rates (below).
  * CoinGecko: majors' 1/7/30-day moves, the whole market (total value, bitcoin's share), and the
    meme-coin sector (the most-traded meme coins; the sector move is their
    median). Three calls, spaced out: the free tier refuses bursts.
  * alternative.me: the Crypto Fear & Greed index.
  * OKX funding rates: i.e. whether leveraged traders are
    crowded long (they pay) or short. Crowded sides get liquidated in cascades:
    that is how most of crypto's violent moves happen.
  * DefiLlama: total stablecoin supply. Growing supply = new money ready to
    buy crypto; shrinking = money leaving.

`LESSONS` are what StockSage measured on real price history (cryptolab.py,
2026-11): the trader's crypto rules come from them. Standard library only, so
the trading routine can run it: `python -m stocksage.crypto`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

GECKO = "https://api.coingecko.com/api/v3"
MARKETS = GECKO + "/coins/markets?vs_currency=usd&ids={ids}&price_change_percentage=24h,7d,30d"
MEME_MARKETS = (GECKO + "/coins/markets?vs_currency=usd&category=meme-token&order=volume_desc"
                "&per_page=10&price_change_percentage=24h,7d,30d")
CANDLES = "https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT&bar=1Dutc&limit=100"
GLOBAL = GECKO + "/global"
FEAR_GREED = "https://api.alternative.me/fng/?limit=2"
FUNDING = "https://www.okx.com/api/v5/public/funding-rate?instId={sym}-USDT-SWAP"
STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"

MAJORS = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP", "dogecoin": "DOGE",
          "cardano": "ADA", "chainlink": "LINK", "avalanche-2": "AVAX", "litecoin": "LTC"}
TREND_COINS = ("BTC", "ETH")
CROWDED_LONG = 0.0003        # per 8h funding (about 33% a year): longs are paying up
CROWDED_SHORT = -0.0001

LESSONS = [
    "Trend: BTC and ETH held only while above their 50-day average beat holding on risk-adjusted terms "
    "with smaller crashes, before AND after 2021 (BTC 2021+: max drop -61% vs -77%). The 20-day failed "
    "after 2021 and the 100-day was mixed. Trade BTC/ETH long only while above the 50-day average.",
    "Chasing: buying a major coin after a +15% or +30% day, or when it first crosses above its 50-day "
    "average, was a coin flip afterwards. Buying last month's best coins lost to just holding them all.",
    "Crashes bounce, briefly: after a major fell >15% in a day it rose the next day 64% of the time "
    "(median +3.5%), and after a -30% week rose over the next 7 days 65% of the time; 30 days later, "
    "no edge. Bounce trades are 1-7 days, with a stop under the low.",
    "Meme coins move 1.5-3x bitcoin both ways (DOGE 1.5x, WIF 2x, POPCAT 2.4x). Every meme coin with "
    "clean data is 80-98% below its peak; new ones jumped in their first year then lost 63-95% "
    "(DOGE is the exception). The meme coins we can name are the survivors: most go to zero.",
    "Fear & Greed: extreme fear (<20) was followed by a higher BTC 30 days later 59-69% of the time in "
    "both halves of 2018-2026. Extreme greed flipped between halves: not reliable.",
    "Weekends are calmer for BTC (Sat 1.7%/day vs ~3% on weekdays), but crashes can still happen. "
    "BTC's move yesterday does not predict altcoins today (no lag to trade).",
]


def _get(url: str) -> str:
    from . import news

    return news._get(url)


_last_gecko = [0.0]
GECKO_SPACING = 2.5          # seconds between CoinGecko calls: its free tier refuses bursts (HTTP 429)


def _json(url: str, fetch=None):
    if fetch is not None:
        return json.loads(fetch(url))
    import time

    if url.startswith(GECKO):
        wait = _last_gecko[0] + GECKO_SPACING - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    for attempt in (0, 1):
        try:
            return json.loads(_get(url))
        except Exception as exc:
            if attempt or "429" not in str(exc):
                raise
            time.sleep(15)
        finally:
            if url.startswith(GECKO):
                _last_gecko[0] = time.monotonic()


def _pct(v):
    return round(v / 100, 4) if isinstance(v, (int, float)) else None


def majors(fetch=None) -> list[dict]:
    rows = _json(MARKETS.format(ids=",".join(MAJORS)), fetch)
    out = []
    for r in rows:
        out.append({"symbol": MAJORS.get(r.get("id"), str(r.get("symbol", "")).upper()),
                    "robinhood": MAJORS.get(r.get("id"), str(r.get("symbol", "")).upper()) + "-USD",
                    "price": r.get("current_price"), "rank": r.get("market_cap_rank"),
                    "d1": _pct(r.get("price_change_percentage_24h_in_currency")),
                    "d7": _pct(r.get("price_change_percentage_7d_in_currency")),
                    "d30": _pct(r.get("price_change_percentage_30d_in_currency"))})
    return out


def trend(symbol: str, fetch=None) -> dict:
    """Where a coin sits against its 50- and 100-day averages of completed daily closes
    (OKX's USDT market, newest first; the still-open day is the live price)."""
    rows = _json(CANDLES.format(sym=symbol), fetch).get("data") or []
    if len(rows) < 51:
        return {}
    last = float(rows[0][4])
    done = [float(r[4]) for r in rows if len(r) < 9 or r[8] == "1"]
    if rows[0][8:9] == ["1"]:
        done = done[1:]                 # a completed newest row is the price itself, not history
    ma50 = sum(done[:50]) / 50
    out = {"symbol": symbol, "price": round(last, 2), "vs_50d": round(last / ma50 - 1, 4), "above_50d": last > ma50}
    if len(done) >= 99:
        ma100 = sum(done[:99]) / len(done[:99])
        out.update(vs_100d=round(last / ma100 - 1, 4), above_100d=last > ma100)
    return out


def meme_sector(fetch=None) -> dict:
    out: dict = {}
    try:
        out["most_traded"] = [{"symbol": str(r.get("symbol", "")).upper(), "name": r.get("name", ""),
                               "rank": r.get("market_cap_rank"),
                               "d1": _pct(r.get("price_change_percentage_24h_in_currency")),
                               "d7": _pct(r.get("price_change_percentage_7d_in_currency")),
                               "d30": _pct(r.get("price_change_percentage_30d_in_currency"))}
                              for r in _json(MEME_MARKETS, fetch)]
    except Exception:
        pass
    for k in ("d1", "d7"):            # the sector's move = the median of its most-traded coins
        vals = sorted(m[k] for m in out.get("most_traded", []) if m.get(k) is not None)
        if vals:
            out[k] = vals[len(vals) // 2]
    return out


def whole_market(fetch=None) -> dict:
    d = _json(GLOBAL, fetch).get("data") or {}
    return {"total_value_usd": (d.get("total_market_cap") or {}).get("usd"),
            "d1": _pct(d.get("market_cap_change_percentage_24h_usd")),
            "btc_share": _pct((d.get("market_cap_percentage") or {}).get("btc"))}


def fear_greed(fetch=None) -> dict:
    days = _json(FEAR_GREED, fetch).get("data") or []
    if not days:
        return {}
    out = {"value": int(days[0]["value"]), "label": days[0].get("value_classification", "")}
    if len(days) > 1:
        out["yesterday"] = int(days[1]["value"])
    return out


def funding(symbol: str, fetch=None) -> dict:
    rows = _json(FUNDING.format(sym=symbol), fetch).get("data") or []
    if not rows:
        return {}
    rate = float(rows[0]["fundingRate"])
    crowd = "longs crowded" if rate >= CROWDED_LONG else "shorts crowded" if rate <= CROWDED_SHORT else "balanced"
    return {"symbol": symbol, "per_8h": rate, "yearly": round(rate * 3 * 365, 3), "crowd": crowd}


def stablecoins(fetch=None) -> dict:
    rows = _json(STABLES, fetch)
    vals = [float((r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0) for r in rows]
    vals = [v for v in vals if v > 0]
    if len(vals) < 31:
        return {}
    return {"total_usd": round(vals[-1]), "d30": round(vals[-1] / vals[-31] - 1, 4)}


def packet(fetch=None, now: datetime | None = None) -> dict:
    """Everything above plus the measured lessons and a few plain flags."""
    now = now or datetime.now(timezone.utc)
    out: dict = {"as_of": now.isoformat(timespec="minutes"), "lessons": LESSONS}

    def safe(key, fn):
        try:
            out[key] = fn()
        except Exception:
            out[key] = {} if key not in ("majors",) else []

    safe("majors", lambda: majors(fetch))
    out["trend"] = {}
    for sym in TREND_COINS:
        try:
            t = trend(sym, fetch)
            if t:
                out["trend"][sym] = t
        except Exception:
            continue
    safe("whole_market", lambda: whole_market(fetch))
    safe("meme_sector", lambda: meme_sector(fetch))
    safe("fear_greed", lambda: fear_greed(fetch))
    out["funding"] = {}
    for sym in ("BTC", "ETH"):
        try:
            f = funding(sym, fetch)
            if f:
                out["funding"][sym] = f
        except Exception:
            continue
    safe("stablecoins", lambda: stablecoins(fetch))
    btc_ok = (out["trend"].get("BTC") or {}).get("above_50d")
    fg = (out.get("fear_greed") or {}).get("value")
    meme7 = (out.get("meme_sector") or {}).get("d7")
    out["flags"] = {
        "btc_uptrend": btc_ok,
        "eth_uptrend": (out["trend"].get("ETH") or {}).get("above_50d"),
        "extreme_fear": fg is not None and fg < 20,
        "meme_window": bool(btc_ok) and meme7 is not None and meme7 > 0,
    }
    return out


def _p(v):
    return "n/a" if v is None else f"{v:+.1%}"


def render(p: dict) -> str:
    lines = [f"CRYPTO ({p.get('as_of', '')}) - context, never a call", ""]
    f = p.get("flags") or {}
    yes = lambda b: "YES" if b else ("no" if b is not None else "unknown")  # noqa: E731
    lines += [f"BTC above its 50-day average (long trades allowed): {yes(f.get('btc_uptrend'))}",
              f"ETH above its 50-day average: {yes(f.get('eth_uptrend'))}",
              f"Extreme fear (<20, historically a decent time to buy BTC for weeks): {yes(f.get('extreme_fear'))}",
              f"Meme window (BTC uptrend AND most-traded memes up over 7 days): {yes(f.get('meme_window'))}", ""]
    for sym, t in (p.get("trend") or {}).items():
        extra = f", {_p(t.get('vs_100d'))} vs 100-day" if "vs_100d" in t else ""
        lines.append(f"{sym} ${t['price']:,.2f}: {_p(t['vs_50d'])} vs 50-day{extra}")
    w = p.get("whole_market") or {}
    if w:
        lines.append(f"Whole crypto market {_p(w.get('d1'))} today; bitcoin is {w.get('btc_share', 0):.0%} of it")
    fg = p.get("fear_greed") or {}
    if fg:
        lines.append(f"Fear & Greed {fg['value']} ({fg['label']}), yesterday {fg.get('yesterday', '?')}")
    for sym, fu in (p.get("funding") or {}).items():
        lines.append(f"{sym} futures funding {fu['per_8h']:+.4%} per 8h ({fu['yearly']:+.0%}/yr): {fu['crowd']}")
    st = p.get("stablecoins") or {}
    if st:
        lines.append(f"Stablecoin supply ${st['total_usd'] / 1e9:,.0f}B, {_p(st['d30'])} over 30 days "
                     "(growing = fresh money ready to buy)")
    if p.get("majors"):
        lines += ["", "Majors (1d / 7d / 30d):"]
        for m in p["majors"]:
            lines.append(f"  {m['robinhood']:<9} {_p(m['d1']):>7} {_p(m['d7']):>7} {_p(m['d30']):>7}")
    ms = p.get("meme_sector") or {}
    if ms:
        lines += ["", f"Meme coins: typical {_p(ms.get('d1'))} today, {_p(ms.get('d7'))} this week; most traded (1d / 7d / 30d):"]
        for m in ms.get("most_traded", [])[:8]:
            lines.append(f"  {m['symbol']:<9} {_p(m['d1']):>7} {_p(m['d7']):>7} {_p(m['d30']):>7}")
    lines += ["", "What StockSage measured:"] + [f"  - {x}" for x in p.get("lessons", LESSONS)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m stocksage.crypto [--json]`: the crypto market right now."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    p = packet()
    print(json.dumps(p, indent=2) if "--json" in args else render(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
