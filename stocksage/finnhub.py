"""Finnhub (free key): live quotes and ticker-tagged company news.

Used by the trader watcher (a quote every minute for each holding, news every
few minutes) and by the live wire. Only with STOCKSAGE_FINNHUB_KEY set (free
account at finnhub.io, 60 calls a minute). The key travels in a request header,
never in a URL, so it cannot leak into logs. Standard library only.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

BASE = "https://finnhub.io/api/v1"
KEY_VAR = "STOCKSAGE_FINNHUB_KEY"


def key() -> str:
    return os.environ.get(KEY_VAR, "").strip()


def _get(url: str, token: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"X-Finnhub-Token": token, "User-Agent": "StockSage"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")


def _json(path: str, fetch=None):
    token = key()
    if not token:
        raise RuntimeError(f"{KEY_VAR} is not set")
    return json.loads((fetch or _get)(BASE + path, token))


def quote(symbol: str, fetch=None) -> dict | None:
    """Live price, change today and the previous close. None when unknown."""
    q = _json(f"/quote?symbol={symbol.upper()}", fetch)
    if not q or not q.get("c"):
        return None
    return {"symbol": symbol.upper(), "price": float(q["c"]), "prev_close": float(q.get("pc") or 0) or None,
            "day_change": round(float(q.get("dp") or 0) / 100, 4), "high": q.get("h"), "low": q.get("l"),
            "at": datetime.fromtimestamp(int(q.get("t") or 0), tz=timezone.utc).isoformat() if q.get("t") else None}


def company_news(symbol: str, days: int = 2, fetch=None, today: date | None = None) -> list[dict]:
    """The company's own news feed (tagged with the ticker), newest first."""
    today = today or date.today()
    rows = _json(f"/company-news?symbol={symbol.upper()}&from={(today - timedelta(days=days)).isoformat()}"
                 f"&to={today.isoformat()}", fetch) or []
    out = []
    for r in rows:
        if not r.get("headline"):
            continue
        at = datetime.fromtimestamp(int(r.get("datetime") or 0), tz=timezone.utc)
        out.append({"id": str(r.get("id") or r.get("url") or r["headline"]), "ticker": symbol.upper(),
                    "title": r["headline"][:160], "source": r.get("source", ""), "at": at.isoformat()})
    return sorted(out, key=lambda r: r["at"], reverse=True)


def company_name(symbol: str, fetch=None) -> str:
    """The company's name from its Finnhub profile ("Microsoft Corp"), or ''."""
    return str((_json(f"/stock/profile2?symbol={symbol.upper()}", fetch) or {}).get("name") or "")


EARNINGS_HOUR = {"bmo": "before the open", "amc": "after the close", "dmh": "during the session"}


def earnings_calendar(first: date, last: date, fetch=None) -> list[dict]:
    """Every company reporting from `first` to `last`: {ticker, day, when, eps_forecast},
    in the live wire's words for `when`. The watcher's pre-event review reads this (the
    Nasdaq calendar the live wire uses refuses some home connections)."""
    data = _json(f"/calendar/earnings?from={first.isoformat()}&to={last.isoformat()}", fetch) or {}
    out = []
    for r in data.get("earningsCalendar") or []:
        if r.get("symbol") and r.get("date"):
            eps = r.get("epsEstimate")
            out.append({"ticker": str(r["symbol"]).upper(), "day": str(r["date"])[:10],
                        "when": EARNINGS_HOUR.get(str(r.get("hour") or "").lower(), "time not given"),
                        "eps_forecast": f"{float(eps):.2f}" if isinstance(eps, (int, float)) else None})
    return out


def market_news(fetch=None, limit: int = 10) -> list[dict]:
    rows = _json("/news?category=general", fetch) or []
    out = []
    for r in rows[:limit]:
        if r.get("headline"):
            at = datetime.fromtimestamp(int(r.get("datetime") or 0), tz=timezone.utc)
            out.append({"title": r["headline"][:160], "source": r.get("source", ""), "at": at.isoformat()})
    return out
