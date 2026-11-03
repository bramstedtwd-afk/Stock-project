"""What traders are talking about: the crowd's attention and mood, as context.

Four free sources, each optional (one failing never stops the others):
  * StockTwits trending: the most-discussed tickers right now, each with a
    one-paragraph summary of WHY it is being discussed.
  * StockTwits stream per ticker: the last 30 posts, how many are tagged
    bullish vs bearish, and how fast posts are arriving.
  * ApeWisdom: Reddit mention counts (wallstreetbets, stocks, investing, ...)
    now vs 24 hours ago, so a sudden surge stands out.
  * Crypto: the Crypto Fear & Greed index (alternative.me) and CoinGecko's
    trending coins.

How to read it: crowd attention reliably predicts BIGGER moves (volatility),
not their direction. A surge in mentions says "this will move, size it
smaller and use a stop", not "buy". Mood extremes are contrarian at best.
Context, never a call. Personal use. Standard library only, so the trading
routine can run it: `python -m stocksage.social PLTR AAPL`.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"
STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{t}.json"
APEWISDOM = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
CRYPTO_FEAR_GREED = "https://api.alternative.me/fng/?limit=2"
COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
MAX_TICKERS = 8
SURGE_MIN_MENTIONS = 20
SURGE_RATIO = 3.0
SURGE_RANK_JUMP = 5          # and it climbed the ranking: weekends quiet EVERY ticker, so ratio alone fires on Mondays
NOTE = ("Crowd attention predicts bigger moves, not their direction: treat a surge as "
        "'size smaller, use a stop', never as a buy signal. Context only.")


def _get(url: str) -> str:
    from . import news

    return news._get(url)


def _json(url: str, fetch=None):
    return json.loads((fetch or _get)(url))


def _when(text: str):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def trending(fetch=None, limit: int = 10) -> list[dict]:
    """StockTwits' most-discussed tickers, with its summary of why."""
    data = _json(STOCKTWITS_TRENDING, fetch)
    out = []
    for s in (data.get("symbols") or [])[:limit]:
        why = ((s.get("trends") or {}).get("summary") or "").strip()
        out.append({"ticker": s.get("symbol", ""), "name": s.get("title", ""),
                    "kind": s.get("instrument_class", ""), "score": s.get("trending_score"),
                    "why": why[:280] + ("…" if len(why) > 280 else "")})
    return out


def sentiment(ticker: str, fetch=None, now: datetime | None = None) -> dict:
    """The last posts on one ticker: bullish/bearish tags and how busy it is."""
    data = _json(STOCKTWITS_STREAM.format(t=ticker.upper()), fetch)
    msgs = data.get("messages") or []
    times = [t for t in (_when(m.get("created_at")) for m in msgs) if t]
    tags = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in msgs]
    bull, bear = tags.count("Bullish"), tags.count("Bearish")
    now = now or datetime.now(timezone.utc)
    span_h = max((max(times) - min(times)).total_seconds() / 3600, 0.1) if len(times) > 1 else None
    return {"ticker": ticker.upper(), "posts": len(msgs), "bullish": bull, "bearish": bear,
            "bull_share": round(bull / (bull + bear), 2) if bull + bear else None,
            "posts_per_hour": round(len(times) / span_h, 1) if span_h else None,
            "newest_minutes": int((now - max(times)).total_seconds() // 60) if times else None}


def reddit(fetch=None, limit: int = 10) -> list[dict]:
    """Reddit's most-mentioned tickers over 24 hours vs the 24 hours before."""
    data = _json(APEWISDOM, fetch)
    out = []
    for r in (data.get("results") or [])[:limit]:
        now_m, before = int(r.get("mentions") or 0), int(r.get("mentions_24h_ago") or 0)
        rank, was = r.get("rank"), r.get("rank_24h_ago")
        climbed = was is None or (isinstance(rank, int) and isinstance(was, int) and was - rank >= SURGE_RANK_JUMP)
        out.append({"ticker": r.get("ticker", ""), "name": html.unescape(r.get("name", "")),
                    "mentions": now_m, "mentions_24h_ago": before,
                    "change": round(now_m / before, 1) if before else None,
                    "rank": rank, "rank_24h_ago": was,
                    "surging": now_m >= SURGE_MIN_MENTIONS and now_m >= SURGE_RATIO * max(before, 1) and climbed})
    return out


def crypto(fetch=None) -> dict:
    """Crypto Fear & Greed (0 = extreme fear, 100 = extreme greed) and trending coins."""
    out: dict = {}
    try:
        days = _json(CRYPTO_FEAR_GREED, fetch).get("data") or []
        if days:
            out["fear_greed"] = int(days[0]["value"])
            out["fear_greed_label"] = days[0].get("value_classification", "")
            if len(days) > 1:
                out["fear_greed_yesterday"] = int(days[1]["value"])
    except Exception:
        pass
    try:
        coins = _json(COINGECKO_TRENDING, fetch).get("coins") or []
        out["trending"] = []
        for c in coins[:7]:
            item = c.get("item") or {}
            ch = ((item.get("data") or {}).get("price_change_percentage_24h") or {}).get("usd")
            out["trending"].append({"symbol": item.get("symbol", ""), "name": item.get("name", ""),
                                    "rank": item.get("market_cap_rank"),
                                    "d1": round(ch / 100, 4) if isinstance(ch, (int, float)) else None})
    except Exception:
        pass
    return out


def pulse(tickers=(), fetch=None, now: datetime | None = None) -> dict:
    """Everything above in one packet. A source that fails is simply missing."""
    now = now or datetime.now(timezone.utc)
    out: dict = {"as_of": now.isoformat(timespec="minutes"), "note": NOTE}
    for key, fn in (("stocktwits_trending", trending), ("reddit_top", reddit)):
        try:
            out[key] = fn(fetch)
        except Exception:
            out[key] = []
    out["reddit_surging"] = [r["ticker"] for r in out["reddit_top"] if r["surging"]]
    out["tickers"] = {}
    for t in list(dict.fromkeys(t.upper() for t in tickers if t))[:MAX_TICKERS]:
        try:
            out["tickers"][t] = sentiment(t, fetch, now)
        except Exception:
            continue
    out["crypto"] = crypto(fetch)
    return out


def render(p: dict) -> str:
    lines = [f"SOCIAL PULSE ({p.get('as_of', '')})", p.get("note", NOTE), ""]
    if p.get("stocktwits_trending"):
        lines.append("Most discussed on StockTwits:")
        for s in p["stocktwits_trending"]:
            lines.append(f"  {s['ticker']:<8} {s['why'] or s['name']}")
    if p.get("reddit_top"):
        lines += ["", "Most mentioned on Reddit (24h, vs the day before):"]
        for r in p["reddit_top"]:
            surge = "  << SURGING" if r["surging"] else ""
            was = f", rank {r['rank_24h_ago']} -> {r['rank']}" if r.get("rank_24h_ago") else ""
            lines.append(f"  {r['ticker']:<8} {r['mentions']:>5} mentions (was {r['mentions_24h_ago']}{was}){surge}")
    if p.get("tickers"):
        lines += ["", "Crowd mood on your tickers (last 30 StockTwits posts):"]
        for t, s in p["tickers"].items():
            mood = f"{s['bull_share']:.0%} bullish of tagged" if s.get("bull_share") is not None else "no tagged posts"
            pace = f", {s['posts_per_hour']} posts/hour" if s.get("posts_per_hour") is not None else ""
            lines.append(f"  {t:<8} {mood} ({s['bullish']} bull / {s['bearish']} bear){pace}")
    c = p.get("crypto") or {}
    if c:
        lines += [""]
        if "fear_greed" in c:
            prev = f" (yesterday {c['fear_greed_yesterday']})" if "fear_greed_yesterday" in c else ""
            lines.append(f"Crypto Fear & Greed: {c['fear_greed']} {c.get('fear_greed_label', '')}{prev}")
        if c.get("trending"):
            lines.append("Trending coins: " + ", ".join(
                c_["symbol"] + (f" {c_['d1']:+.1%}" if c_.get("d1") is not None else "") for c_ in c["trending"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m stocksage.social PLTR AAPL [--json]`: the crowd right now."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    p = pulse([a for a in args if not a.startswith("--")])
    print(json.dumps(p, indent=2) if as_json else render(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
