"""Recent news on what you own: the last day first, then the week.

Two free sources, merged and de-duplicated:
  * Google News search (RSS), by company name: reliable and dated. Its terms
    allow personal, non-commercial use in a feed reader, which is what this is.
  * yfinance's news feed, when it answers (it often returns nothing).

Headlines are context. They never change a call on the sheet. Network calls are
injectable (`fetch`) so tests run offline.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from xml.etree import ElementTree

GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
DAY = timedelta(hours=24)
WEEK = timedelta(days=7)
JUNK = re.compile(r"stock price, news, quote|stock quote|price target and stock forecast|stock forecasts?\b|"
                  r"historical prices|stock chart|\bquote\b.*\bhistory\b|how much a \$|price prediction|"
                  r"investors heavily search|tokenized|broke (out )?(above|below) the \d+-day|"
                  r"should you buy|is it time to buy|top \d+ stocks", re.I)
SUFFIX = re.compile(r"[,.]?\s+(incorporated|inc|corporation|corp|company|co|ltd|plc|holdings|group|"
                    r"class [a-c]|common stock|n\.v\.|s\.a\.|ag|se)\.?$", re.I)
HEADLINE_CHARS = 110


def clean_name(name: str) -> str:
    name = (name or "").strip()
    for _ in range(3):
        name = SUFFIX.sub("", name).strip()
    return name


def _get(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (StockSage personal feed reader)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def google_news(query: str, fetch=None) -> list[dict]:
    """Headlines for a search, newest first: {title, publisher, link, published (UTC datetime)}."""
    try:
        xml = (fetch or _get)(GOOGLE_RSS.format(q=quote_plus(query)))
        root = ElementTree.fromstring(xml)
    except Exception:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            continue
        out.append({"title": title, "publisher": source, "link": item.findtext("link") or "", "published": when})
    return sorted(out, key=lambda x: x["published"], reverse=True)


def _from_yfinance(items: list[dict]) -> list[dict]:
    out = []
    for it in items or []:
        raw = it.get("published")
        when = None
        try:
            if isinstance(raw, (int, float)):
                when = datetime.fromtimestamp(raw, tz=timezone.utc)
            elif raw:
                when = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            when = None
        if when is not None and it.get("title"):
            out.append({"title": it["title"], "publisher": it.get("publisher", ""), "link": it.get("link", ""),
                        "published": when})
    return out


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:80]


def holding_news(ticker: str, name: str | None, market=None, now: datetime | None = None,
                 fetch=None, limit: int = 4) -> list[dict]:
    """Up to `limit` headlines from the last 7 days about one holding, newest first."""
    now = now or datetime.now(timezone.utc)
    name = clean_name(name or "") or ticker
    query = f'"{name}" when:7d' if name != ticker else f"{ticker} stock when:7d"
    items = google_news(query, fetch)
    if market is not None:
        try:
            items += _from_yfinance(market.news(ticker, limit=8))
        except Exception:
            pass
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        k = _key(it["title"])
        if not k or k in seen or JUNK.search(it["title"]) or now - it["published"] > WEEK:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def age(when: datetime, now: datetime) -> str:
    secs = max((now - when).total_seconds(), 0)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def short(title: str, n: int = HEADLINE_CHARS) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


def headline_id(ticker: str, title: str) -> str:
    return hashlib.sha256(f"{ticker}|{_key(title)}".encode()).hexdigest()[:12]


# --- company names (cached, so each is looked up once) ------------------------------


def lookup_name(ticker: str) -> str:
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    return info.get("longName") or info.get("shortName") or ""


def company_names(tickers: list[str], cache_path: Path | None = None, lookup=None) -> dict[str, str]:
    cache = {}
    if cache_path is not None and Path(cache_path).exists():
        try:
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    lookup = lookup or lookup_name
    changed = False
    for t in tickers:
        if t in cache:
            continue
        try:
            cache[t] = lookup(t) or ""
        except Exception:
            cache[t] = ""
        changed = True
    if changed and cache_path is not None:
        try:
            Path(cache_path).write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except Exception:
            pass
    return {t: cache.get(t, "") for t in tickers}


# --- words for the sheet and for pushes -------------------------------------------------


def sheet_lines(news: dict[str, list[dict]], moves: dict[str, float], now: datetime,
                per_ticker: int = 2) -> list[str]:
    """The NEWS ON WHAT YOU OWN section: every holding, freshest first."""
    def freshness(t):
        items = news.get(t) or []
        return items[0]["published"] if items else datetime(1970, 1, 1, tzinfo=timezone.utc)

    out = []
    for t in sorted(news, key=freshness, reverse=True):
        move = moves.get(t)
        head = f"{t}" + (f" ({move * 100:+.1f}% last session)" if move is not None else "")
        items = news.get(t) or []
        if not items:
            out.append(f"  {head}: no news in the last 7 days.")
            continue
        out.append(f"  {head}:")
        for it in items[:per_ticker]:
            tag = "TODAY" if now - it["published"] <= DAY else "week"
            src = f" ({it['publisher']})" if it.get("publisher") else ""
            out.append(f"    [{tag} {age(it['published'], now)}] {short(it['title'])}{src}")
    return out


def push_lines(news: dict[str, list[dict]], now: datetime, seen: set[str], only: list[str] | None = None,
               max_lines: int = 8) -> tuple[list[str], set[str]]:
    """Headlines for a phone push: the last day only, new ones first, one per holding.
    Returns the lines and the ids now shown (to remember, so tomorrow says what is new)."""
    rows = []
    for t, items in news.items():
        if only is not None and t not in only:
            continue
        fresh = [it for it in items if now - it["published"] <= DAY]
        if not fresh:
            continue
        it = fresh[0]
        hid = headline_id(t, it["title"])
        rows.append((hid not in seen, it["published"], t, it, hid))
    rows.sort(key=lambda r: (not r[0], -r[1].timestamp()))
    lines, shown = [], set()
    for is_new, _, t, it, hid in rows[:max_lines]:
        lines.append(f"{'NEW ' if is_new else ''}{t} ({age(it['published'], now)}): {short(it['title'], 90)}")
        shown.add(hid)
    return lines, shown
