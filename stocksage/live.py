"""The live wire: what is happening right now and what is scheduled today.

Free sources, each optional (one failing never stops the others):
  * Economic calendar (Forex Factory's weekly JSON): today's and tomorrow's US
    releases with their time, forecast, previous and, once out, the actual.
    A number far from its forecast moves the whole market within seconds.
  * Earnings calendar (Nasdaq): who reports today and next session, before the
    open or after the close, and the consensus EPS, biggest companies first.
  * Headlines: MarketWatch top stories and WSJ Markets, last 12 hours.
  * Press releases (PR Newswire): companies' own announcements, often the very
    first source of a move (deals, guidance, FDA, buybacks).
  * The Fed's own press releases.
  * Prediction markets (Polymarket): real-money odds on Fed decisions and the
    economy, e.g. "December: 25 bp hike 72%". When those odds swing, rates and
    stocks follow.

Context, never a call. Personal use. Standard library only, so the trading
routine can run it every run: `python -m stocksage.live`.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
FEEDS = {
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "WSJ Markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
}
PRESS = "https://www.prnewswire.com/rss/news-releases-list.rss"
FED = "https://www.federalreserve.gov/feeds/press_all.xml"
ODDS = "https://gamma-api.polymarket.com/events?closed=false&limit=8&tag_slug={tag}&order=volume24hr&ascending=false"
ODDS_TAGS = ("fed", "economy")
MIN_ODDS_VOLUME = 5000
FRESH = timedelta(hours=12)
ET = timezone(timedelta(hours=-5))   # display only; the calendar carries its own offsets
NOTE = "Context, never a call: scheduled releases and earnings are when moves happen; odds swings move rates."


def _get(url: str) -> str:
    from . import news

    return news._get(url)


def _json(url: str, fetch=None):
    return json.loads((fetch or _get)(url))


def _when(text):
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _calendar_rows(fetch=None) -> list:
    """The weekly file, cached for an hour: its host refuses frequent downloads."""
    if fetch is not None:
        return _json(CALENDAR, fetch)
    import time

    try:
        from .journal import state_dir

        cache = state_dir() / "econ_calendar.json"
    except Exception:
        cache = None
    if cache is not None and cache.exists() and time.time() - cache.stat().st_mtime < 3600:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        rows = _json(CALENDAR)
    except Exception:
        if cache is not None and cache.exists() and time.time() - cache.stat().st_mtime < 6 * 86400:
            return json.loads(cache.read_text(encoding="utf-8"))
        raise
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows), encoding="utf-8")
        except OSError:
            pass
    return rows


def calendar(fetch=None, now: datetime | None = None, first: int = 0, last: int = 1,
             impacts=("High", "Medium")) -> list[dict]:
    """US releases of the given impact from `first` to `last` days ahead (Eastern dates)."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    out = []
    for e in _calendar_rows(fetch):
        if e.get("country") != "USD" or e.get("impact") not in impacts:
            continue
        at = _when(e.get("date"))
        if at is None or not (today + timedelta(days=first) <= at.astimezone(ET).date() <= today + timedelta(days=last)):
            continue
        out.append({"at": at.isoformat(), "et": at.astimezone(ET).strftime("%a %I:%M %p ET"),
                    "title": e.get("title", ""), "impact": e.get("impact"),
                    "forecast": e.get("forecast") or None, "previous": e.get("previous") or None,
                    "actual": e.get("actual") or None, "done": at <= now})
    return sorted(out, key=lambda e: e["at"])


def _cap(text: str) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", text or "") or 0)
    except ValueError:
        return 0.0


def earnings(day, fetch=None, limit: int = 15) -> list[dict]:
    """The biggest companies reporting on `day`, with when and what is expected."""
    data = _json(EARNINGS.format(day=day.isoformat()), fetch).get("data") or {}
    rows = sorted(data.get("rows") or [], key=lambda r: -_cap(r.get("marketCap")))
    when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}
    return [{"ticker": r.get("symbol", ""), "name": html.unescape(r.get("name", "")),
             "when": when.get(r.get("time"), "time not given"), "eps_forecast": r.get("epsForecast") or None,
             "market_cap_b": round(_cap(r.get("marketCap")) / 1e9, 1)} for r in rows[:limit]]


LISTED = re.compile(r"\((?:NYSE American|NYSE Arca|NYSE|NASDAQ|Nasdaq|Cboe|OTCQX|OTCQB)\s*:\s*([A-Z][A-Z.]{0,5})\)")
PERSONAL = re.compile(r"\b(my|I|I'm|I've)\b.*\?|should i\b|grandkids|adult kids|stepmother|stepfather|wedding|"
                      r"\binherit|retire(ment|e)?\b.*\b(my|i)\b|moneyist|ask the|dear ", re.I)


def rss(url: str, source: str, fetch=None, now: datetime | None = None, fresh: timedelta = FRESH,
        listed_only: bool = False) -> list[dict]:
    """Fresh items from a feed. `listed_only` keeps press releases that name a US
    listing, tagging each with its ticker (wire feeds are full of foreign PR)."""
    now = now or datetime.now(timezone.utc)
    root = ElementTree.fromstring((fetch or _get)(url).encode("utf-8"))
    out = []
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        ticker = None
        if listed_only:
            m = LISTED.search(html.unescape(item.findtext("description") or "") + " " + title)
            if not m:
                continue
            ticker = m.group(1)
        elif PERSONAL.search(title):
            continue
        try:
            at = parsedate_to_datetime(item.findtext("pubDate") or "")
            at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if title and now - at <= fresh:
            row = {"title": title[:160], "source": source, "hours_ago": round((now - at).total_seconds() / 3600, 1)}
            if ticker:
                row["ticker"] = ticker
            out.append(row)
    return out


def headlines(fetch=None, now: datetime | None = None, limit: int = 12) -> list[dict]:
    items, seen = [], set()
    for source, url in FEEDS.items():
        try:
            items += rss(url, source, fetch, now)
        except Exception:
            continue
    out = []
    for it in sorted(items, key=lambda i: i["hours_ago"]):
        key = re.sub(r"\W+", " ", it["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out[:limit]


def odds(fetch=None, limit: int = 6) -> list[dict]:
    """Real-money odds on the Fed and the economy: each event's likeliest outcomes."""
    events, seen = [], set()
    for tag in ODDS_TAGS:
        try:
            for e in _json(ODDS.format(tag=tag), fetch):
                if e.get("id") not in seen and (e.get("volume24hr") or 0) >= MIN_ODDS_VOLUME:
                    seen.add(e.get("id"))
                    events.append(e)
        except Exception:
            continue
    out = []
    for e in sorted(events, key=lambda e: -(e.get("volume24hr") or 0))[:limit]:
        outcomes = []
        for m in e.get("markets") or []:
            if m.get("closed") or not m.get("active", True):
                continue
            try:
                yes = float(json.loads(m.get("outcomePrices") or "[]")[0])
            except (ValueError, IndexError, TypeError):
                continue
            outcomes.append({"outcome": m.get("groupItemTitle") or m.get("question") or "", "chance": round(yes, 3)})
        outcomes.sort(key=lambda o: -o["chance"])
        if outcomes:
            out.append({"question": e.get("title", ""), "ends": str(e.get("endDate", ""))[:10], "top": outcomes[:3]})
    return out


def wire(tickers=(), fetch=None, now: datetime | None = None) -> dict:
    """Everything above in one packet; `tickers` get their earnings flagged."""
    now = now or datetime.now(timezone.utc)
    out: dict = {"as_of": now.isoformat(timespec="minutes"), "note": NOTE}

    def safe(key, fn, empty):
        try:
            out[key] = fn()
        except Exception:
            out[key] = empty

    today = now.astimezone(ET).date()
    nxt = today + timedelta(days=3 if today.weekday() == 4 else 2 if today.weekday() == 5 else 1)
    safe("calendar", lambda: calendar(fetch, now), [])
    safe("week_ahead", lambda: calendar(fetch, now, first=2, last=7, impacts=("High",)), [])
    safe("earnings_today", lambda: earnings(today, fetch), [])
    safe("earnings_next", lambda: earnings(nxt, fetch), [])
    safe("headlines", lambda: headlines(fetch, now), [])
    safe("press_releases", lambda: rss(PRESS, "PR Newswire", fetch, now, timedelta(hours=6), listed_only=True)[:10], [])
    safe("fed", lambda: rss(FED, "Federal Reserve", fetch, now, timedelta(days=3))[:5], [])
    safe("odds", lambda: odds(fetch), [])
    mine = {t.upper() for t in tickers if t}
    out["company_news"] = []
    try:                 # ticker-tagged company news (Finnhub), only with its free key
        from . import finnhub

        if finnhub.key() or fetch is not None:
            for t in sorted(x for x in mine if not x.endswith("-USD"))[:8]:
                for item in finnhub.company_news(t, days=1, fetch=fetch)[:3]:
                    hours = (now - datetime.fromisoformat(item["at"])).total_seconds() / 3600
                    if hours <= 24:
                        out["company_news"].append({**item, "hours_ago": round(hours, 1)})
    except Exception:
        pass
    out["your_tickers_reporting"] = [dict(e, day=label) for label, key in (("today", "earnings_today"), ("next session", "earnings_next"))
                                     for e in out[key] if e["ticker"] in mine]
    return out


def render(w: dict) -> str:
    lines = [f"LIVE WIRE ({w.get('as_of', '')})", w.get("note", NOTE)]
    if w.get("your_tickers_reporting"):
        lines += ["", "YOUR TICKERS REPORT EARNINGS:"] + [
            f"  {e['ticker']} {e['day']}, {e['when']} (EPS expected {e['eps_forecast'] or 'n/a'})"
            for e in w["your_tickers_reporting"]]
    if w.get("calendar"):
        lines += ["", "Economic releases (today and tomorrow):"]
        for e in w["calendar"]:
            got = f", ACTUAL {e['actual']}" if e.get("actual") else ""
            lines.append(f"  {e['et']:<17} [{e['impact']}] {e['title']} (forecast {e['forecast'] or '-'}, "
                         f"previous {e['previous'] or '-'}{got})")
    if w.get("week_ahead"):
        lines += ["", "Big releases later this week:"] + [f"  {e['et']:<17} {e['title']} (forecast {e['forecast'] or '-'})"
                                                          for e in w["week_ahead"]]
    for key, label in (("earnings_today", "Reporting today"), ("earnings_next", "Reporting next session")):
        if w.get(key):
            lines += ["", f"{label} (biggest first):"]
            lines += [f"  {e['ticker']:<6} {e['when']:<16} EPS est {e['eps_forecast'] or '-':<8} {e['name'][:40]}"
                      for e in w[key][:10]]
    if w.get("odds"):
        lines += ["", "Prediction-market odds (real money):"]
        for o in w["odds"]:
            lines.append(f"  {o['question']} -> " + ", ".join(f"{t['outcome']} {t['chance']:.0%}" for t in o["top"]))
    if w.get("company_news"):
        lines += ["", "Company news on your names (last 24h):"] + [
            f"  {n['hours_ago']:>4}h  {n['ticker']}: {n['title']} ({n['source']})" for n in w["company_news"]]
    for key, label in (("headlines", "Headlines"), ("press_releases", "Listed companies' press releases (last 6h)"),
                       ("fed", "Federal Reserve (last 3 days)")):
        if w.get(key):
            lines += ["", f"{label}:"] + [f"  {h['hours_ago']:>4}h  {(h['ticker'] + ': ') if h.get('ticker') else ''}{h['title']}"
                                          for h in w[key]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m stocksage.live [TICKERS] [--json]`: what is happening right now."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    w = wire([a for a in args if not a.startswith("--")])
    print(json.dumps(w, indent=2) if "--json" in args else render(w))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
