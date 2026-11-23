"""Government contracts: money awarded to listed companies, before most people notice.

Two public sources, both free and official:
  * USASpending.gov (every federal contract action, posted within days for civilian
    agencies). The Defense Department's records there are held back 90 days, so they
    are useless for trading and are skipped.
  * The Defense Department's daily contract announcements (every award of $7.5M+,
    published about 5 PM Eastern). defense.gov refuses cloud computers, so this one
    is read by the owner's always-on PC; elsewhere it reports `blocked`.

Pipeline (the "quant layer" is plain code; the trader only reads the result):
  1. fetch new contract actions; each is counted once (`seen`), on the day it was
     first SEEN here, never on its action date (point in time: nothing is backdated);
  2. map the recipient to a ticker (`RECIPIENTS`; a big award to an unknown name is
     listed as `unmatched` for the trader to resolve, never guessed);
  3. size it: the day's new money as a share of the company's market value, and
     against the company's own trailing 90 days of awards (a z-score once 20 days of
     history exist). A $50M award matters to a $2B company, not to Lockheed;
  4. feed health first: a source that answered nothing on a weekday, or far less than
     usual, is BROKEN and produces no signal. A broken scraper is never "no contracts".

Context for IDEA lines (setup word `contract`), never an order: StockSage places none.
Network lives in `fetch_*` and `market_caps`; everything else is pure and tested offline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

USASPENDING = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
DOD_RSS = "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"
DOD_LIST = "https://www.defense.gov/News/Contracts/"
MIN_AMOUNT = 5_000_000          # smaller actions are noise for a listed company
UNMATCHED_MIN = 50_000_000      # a big award to a name not in RECIPIENTS is shown for the trader to resolve
NOTABLE_SHARE = 0.005           # new money worth 0.5% of the company's market value in a day
Z_AT = 3.0                      # or 3 standard deviations above its own usual daily awards ...
Z_MIN_SHARE = 0.001             # ... when that is at least 0.1% of its value
BASELINE_DAYS = 90
MIN_BASELINE = 20               # business days of history before a z-score speaks
RECENT_DAYS = 3                 # notable awards stay in the brief this many days
BROKEN_SHARE = 0.2              # a weekday count under a fifth of the usual: the feed broke, not the government
SEEN_KEEP = 30_000

# Recipient names as the government writes them -> the listed parent. Subsidiaries are the
# hard part ("ELECTRIC BOAT" is General Dynamics); anything not here is never guessed.
RECIPIENTS = [
    (r"LOCKHEED", "LMT"), (r"\bRAYTHEON\b|\bRTX\b|PRATT & WHITNEY|COLLINS AEROSPACE", "RTX"),
    (r"GENERAL DYNAMICS|ELECTRIC BOAT|BATH IRON WORKS|GULFSTREAM|NATIONAL STEEL AND SHIPBUILDING", "GD"),
    (r"NORTHROP GRUMMAN", "NOC"), (r"\bBOEING\b|INSITU|AURORA FLIGHT", "BA"),
    (r"L3HARRIS|\bL-3\b|\bL3 \b|HARRIS CORP|AEROJET ROCKETDYNE", "LHX"),
    (r"HUNTINGTON INGALLS|NEWPORT NEWS SHIPBUILDING|INGALLS SHIPBUILDING", "HII"),
    (r"\bLEIDOS\b|\bDYNETICS\b", "LDOS"), (r"SCIENCE APPLICATIONS INTERNATIONAL|\bSAIC\b", "SAIC"),
    (r"BOOZ ALLEN", "BAH"), (r"\bCACI\b", "CACI"), (r"\bPARSONS\b", "PSN"), (r"\bKBR\b", "KBR"),
    (r"PALANTIR", "PLTR"), (r"TEXTRON|BELL TEXTRON|\bBELL HELICOPTER", "TXT"), (r"HONEYWELL", "HON"),
    (r"GENERAL ELECTRIC|\bGE AEROSPACE", "GE"), (r"MICROSOFT", "MSFT"), (r"AMAZON WEB SERVICES|AMAZON\.COM", "AMZN"),
    (r"\bORACLE\b", "ORCL"), (r"INTERNATIONAL BUSINESS MACHINES|\bIBM\b", "IBM"), (r"ACCENTURE", "ACN"),
    (r"MCKESSON", "MCK"), (r"CARDINAL HEALTH", "CAH"), (r"\bPFIZER\b", "PFE"), (r"MODERNA", "MRNA"),
    (r"MERCK SHARP|\bMERCK & CO", "MRK"), (r"\bHUMANA\b", "HUM"), (r"CENTENE|HEALTH NET FEDERAL", "CNC"),
    (r"\bOPTUM\b|UNITEDHEALTH", "UNH"), (r"\bFLUOR\b", "FLR"), (r"\bJACOBS\b", "J"), (r"\bAECOM\b", "ACM"),
    (r"TETRA TECH", "TTEK"), (r"\bV2X\b|VECTRUS|VERTEX AEROSPACE", "VVX"), (r"BWX ?T", "BWXT"),
    (r"KRATOS", "KTOS"), (r"AEROVIRONMENT", "AVAV"), (r"MERCURY SYSTEMS", "MRCY"), (r"CURTISS[- ]WRIGHT", "CW"),
    (r"OSHKOSH", "OSK"), (r"LEONARDO DRS|\bDRS\b", "DRS"), (r"\bMAXIMUS\b", "MMS"), (r"\bICF\b", "ICFI"),
    (r"CORECIVIC", "CXW"), (r"\bGEO GROUP|GEO SECURE", "GEO"), (r"MOTOROLA SOLUTIONS", "MSI"),
    (r"CISCO SYSTEMS", "CSCO"), (r"DELL (?:FEDERAL|TECHNOLOGIES|MARKETING)", "DELL"), (r"HEWLETT PACKARD ENTERPRISE", "HPE"),
    (r"\bHP INC\b", "HPQ"), (r"\bAT&T\b", "T"), (r"VERIZON", "VZ"), (r"LUMEN TECHNOLOGIES|CENTURYLINK", "LUMN"),
    (r"SPACE EXPLORATION TECHNOLOGIES", None),   # SpaceX: private, said so instead of guessed
    (r"\bAMENTUM\b", "AMTM"), (r"\bTRANSDIGM\b", "TDG"), (r"HEICO", "HEI"), (r"\bHEXCEL\b", "HXL"),
    (r"ROCKET LAB", "RKLB"), (r"\bPLANET LABS\b", "PL"), (r"BAE SYSTEMS", None), (r"ELBIT SYSTEMS", "ESLT"),
    (r"\bCUMMINS\b", "CMI"), (r"CATERPILLAR", "CAT"), (r"GENERAL MOTORS|GM DEFENSE", "GM"),
    (r"\bFORD MOTOR", "F"), (r"\bSIEMENS\b", None), (r"MCKINSEY|DELOITTE|GUIDEHOUSE|BOOZ & CO", None),
]
_COMPILED = [(re.compile(p), t) for p, t in RECIPIENTS]


def ticker_for(name: str) -> tuple[str | None, bool]:
    """(ticker, known): `known` is True when the name matched, even to a private company."""
    up = re.sub(r"\s+", " ", (name or "").upper())
    for rx, ticker in _COMPILED:
        if rx.search(up):
            return ticker, True
    return None, False


# --- fetching (network) --------------------------------------------------------------

def _post_json(url: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "StockSage research"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_text(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (StockSage personal feed reader)"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_civilian(start: date, end: date, post=None, pages: int = 4) -> list[dict]:
    """Federal contract actions of $5M+ with an action date in [start, end], biggest first.
    Defense Department rows are dropped (published 90 days late)."""
    post = post or _post_json
    rows = []
    for page in range(1, pages + 1):
        body = {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}]},
                "fields": ["Award ID", "Mod", "Recipient Name", "Transaction Amount", "Action Date",
                           "Awarding Agency", "Transaction Description"],
                "sort": "Transaction Amount", "order": "desc", "limit": 100, "page": page}
        got = post(USASPENDING, body)
        batch = got.get("results") or []
        for r in batch:
            amount = float(r.get("Transaction Amount") or 0)
            if amount < MIN_AMOUNT:
                return rows
            if "defense" in (r.get("Awarding Agency") or "").lower():
                continue
            rows.append({"source": "usaspending", "id": f"us:{r.get('Award ID')}:{r.get('Mod')}:{r.get('Action Date')}",
                         "recipient": (r.get("Recipient Name") or "").strip(), "amount": amount,
                         "agency": r.get("Awarding Agency") or "", "action_date": r.get("Action Date") or "",
                         "new_award": str(r.get("Mod") or "") in ("0", ""),
                         "what": re.sub(r"\s+", " ", (r.get("Transaction Description") or ""))[:140]})
        if not (got.get("page_metadata") or {}).get("hasNext"):
            break
    return rows


AGENCY_HEAD = re.compile(r"^(?:ARMY|NAVY|AIR FORCE|SPACE FORCE|MARINE CORPS|DEFENSE [A-Z ]+|[A-Z ]*AGENCY|"
                         r"U\.S\. [A-Z ]+COMMAND|[A-Z ]+COMMAND)$")
VERB = re.compile(r",\s+(?:is|was|are|were|has been|have been)\s+(?:jointly\s+|each\s+|being\s+)?"
                  r"(?:awarded|issued|modified|exercis|given|receiv)", re.S)
DOLLARS = re.compile(r"\$\s?([\d,]{4,}(?:\.\d+)?)")


def parse_dod_article(html: str, published: str = "") -> list[dict]:
    """One daily announcement page -> one row per award paragraph naming a company and an amount."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", re.sub(r"&nbsp;|&#160;", " ", text))
    agency, rows = "", []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if AGENCY_HEAD.match(para) and len(para) < 60:
            agency = para.title()
            continue
        m, money = VERB.search(para[:900]), DOLLARS.search(para)
        if not m or not money or not re.match(r"[A-Z0-9]", para):
            continue
        amount = float(money.group(1).replace(",", ""))
        if amount < MIN_AMOUNT:
            continue
        # several winners are separated by semicolons ("A, City, State; B, City, State; and C, ...");
        # "and" alone is part of names like "Lockheed Martin Rotary and Mission Systems"
        names = [re.sub(r"^and\s+", "", n.strip(" ;")).split(",")[0].strip()
                 for n in para[:m.start()].split(";") if len(n.strip(" ;")) > 2]
        # several companies sharing one ceiling are each counted at an equal share of it
        for name in names[:12]:
            rows.append({"source": "dod", "id": "dod:" + hashlib.sha1(f"{published}|{name}|{para[:200]}".encode()).hexdigest()[:16],
                         "recipient": name, "amount": amount / max(len(names), 1), "agency": agency or "Defense",
                         "action_date": published[:10], "new_award": "modif" not in para.lower()[:400],
                         "what": re.sub(r"\s+", " ", para[m.end():m.end() + 140]).strip()})
    return rows


def fetch_dod(get=None, articles: int = 2) -> list[dict]:
    """The latest daily announcements (RSS first, then the listing page)."""
    get = get or _get_text
    links: list[tuple[str, str]] = []
    try:
        rss = get(DOD_RSS)
        for item in re.findall(r"<item>(.*?)</item>", rss, re.S):
            link = re.search(r"<link>(.*?)</link>", item, re.S)
            when = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            if link:
                links.append((link.group(1).strip(), _rss_date(when.group(1).strip() if when else "")))
    except Exception as exc:
        log.info("DoD RSS unavailable (%s); trying the listing page", exc)
    if not links:
        page = get(DOD_LIST)
        links = [(("https://www.defense.gov" + u) if u.startswith("/") else u, "")
                 for u in dict.fromkeys(re.findall(r'href="([^"]*/News/Contracts/Contract/Article/\d+/[^"]*)"', page))]
    rows = []
    for url, when in links[:articles]:
        rows += parse_dod_article(get(url), when or datetime.now(timezone.utc).date().isoformat())
    return rows


def _rss_date(text: str) -> str:
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).date().isoformat()
    except Exception:
        return ""


def market_caps(tickers: list[str], cache_path: Path | None = None, lookup=None, max_age_days: int = 7) -> dict:
    """Market value per ticker (cached a week). `lookup(ticker)` is injectable for tests."""
    cache = {}
    if cache_path is not None and Path(cache_path).exists():
        try:
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    today = datetime.now(timezone.utc).date()
    if lookup is None:
        def lookup(t):
            import yfinance as yf

            return float(yf.Ticker(t).fast_info["market_cap"])
    changed = False
    for t in tickers:
        hit = cache.get(t)
        if hit and (today - date.fromisoformat(hit["at"])).days <= max_age_days:
            continue
        try:
            value = lookup(t)
        except Exception:
            value = None
        if value:
            cache[t] = {"cap": float(value), "at": today.isoformat()}
            changed = True
    if changed and cache_path is not None:
        try:
            Path(cache_path).write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return {t: cache[t]["cap"] for t in tickers if t in cache}


# --- the quant layer (pure) ----------------------------------------------------------

def _business_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def feed_health(name: str, count: int, counts: dict, today: date, error: str | None = None) -> dict:
    """Is this source working? `count` = rows it answered now, `counts` = the most it answered on
    earlier days. A weekday with nothing, or far less than usual, is a broken feed, not a quiet
    government."""
    if error:
        blocked = any(x in error for x in ("403", "Forbidden", "Access Denied"))
        return {"status": "blocked" if blocked else "error", "rows": 0, "why": error[:160]}
    past = [counts.get(d.isoformat()) for d in _business_days(today, 20)]
    past = [c for c in past if c is not None]
    usual = statistics.median(past) if len(past) >= 5 else None
    out = {"status": "ok", "rows": count, "usual": usual}
    if usual is None:
        out["status"] = "new"
    elif today.weekday() < 5 and usual >= 3 and count < BROKEN_SHARE * usual:
        out["status"] = "broken"
        out["why"] = f"{count} rows answered against a usual {usual:g}: treated as a broken feed, not as no contracts"
    return out


def score(new: list[dict], caps: dict, daily: dict, today: date) -> list[dict]:
    """Today's new money per ticker, sized against the company's value and its own history.
    `daily[ticker][day] = amount` holds earlier days (today's must not be in it yet)."""
    by: dict[str, dict] = {}
    for r in new:
        t = r.get("ticker")
        if not t:
            continue
        g = by.setdefault(t, {"ticker": t, "amount": 0.0, "awards": [], "sources": set()})
        g["amount"] += r["amount"]
        g["awards"].append(r)
        g["sources"].add(r["source"])
    out = []
    for t, g in by.items():
        cap = caps.get(t)
        share = g["amount"] / cap if cap else None
        hist = [daily.get(t, {}).get(d.isoformat(), 0.0) for d in _business_days(today, BASELINE_DAYS)]
        seen_days = len([d for d in daily.get(t, {}) if d < today.isoformat()])
        z = None
        if seen_days >= MIN_BASELINE:
            sd = statistics.pstdev(hist)
            z = round((g["amount"] - statistics.mean(hist)) / sd, 1) if sd > 0 else None
        notable = bool(share is not None and (share >= NOTABLE_SHARE or (z is not None and z >= Z_AT and share >= Z_MIN_SHARE)))
        top = max(g["awards"], key=lambda r: r["amount"])
        out.append({"ticker": t, "amount": round(g["amount"]), "share_of_value": round(share, 5) if share else None,
                    "z": z, "notable": notable, "awards": len(g["awards"]), "sources": sorted(g["sources"]),
                    "biggest": {k: top.get(k) for k in ("recipient", "amount", "agency", "what", "new_award", "action_date")}})
    return sorted(out, key=lambda r: (r["notable"], r["share_of_value"] or 0), reverse=True)


def packet(state_path: Path | None = None, now: datetime | None = None, post=None, get=None, caps_lookup=None,
           caps_path: Path | None = None, days: int = 4, dod: bool = True) -> dict:
    """The brief's `contracts`: feed health, notable awards (last few days) and big unmatched names."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    state = {}
    if state_path is not None and Path(state_path).exists():
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    seen = set(state.get("seen") or [])
    daily = state.get("daily") or {}
    counts = state.get("counts") or {}
    recent = [r for r in state.get("recent") or [] if r.get("seen", "") >= (today - timedelta(days=RECENT_DAYS)).isoformat()]
    sources, new = {}, []
    feeds = [("usaspending", lambda: fetch_civilian(today - timedelta(days=days), today, post=post))]
    if dod:
        feeds.append(("dod", lambda: fetch_dod(get=get)))
    for name, fetch in feeds:
        try:
            rows, error = fetch(), None
        except Exception as exc:
            rows, error = [], f"{type(exc).__name__}: {exc}"
        fresh = [r for r in rows if r["id"] not in seen]
        c = counts.setdefault(name, {})
        if not error:                                       # rows the source answered (not just new ones):
            c[today.isoformat()] = max(c.get(today.isoformat(), 0), len(rows))   # stable from run to run
        sources[name] = feed_health(name, len(rows), {k: v for k, v in c.items() if k != today.isoformat()},
                                    today, error)
        sources[name]["new"] = len(fresh)
        if sources[name]["status"] in ("broken", "blocked", "error"):
            continue                                        # a broken feed never becomes a signal
        for r in fresh:
            r["ticker"], r["known"] = ticker_for(r["recipient"])
            r["seen"] = today.isoformat()
        new += fresh
        seen.update(r["id"] for r in fresh)
    tickers = sorted({r["ticker"] for r in new if r.get("ticker")})
    caps = market_caps(tickers, caps_path, lookup=caps_lookup) if tickers else {}
    scored = score(new, caps, daily, today)
    for r in new:
        if r.get("ticker"):
            day = daily.setdefault(r["ticker"], {})
            day[today.isoformat()] = day.get(today.isoformat(), 0.0) + r["amount"]
    for s in scored:
        if s["notable"]:
            recent.append(dict(s, seen=today.isoformat()))
    names: dict[str, dict] = {}
    for r in new:
        if r.get("known"):
            continue
        g = names.setdefault(r["recipient"], {"recipient": r["recipient"], "amount": 0.0, "awards": 0,
                                              "agency": r["agency"], "what": r["what"], "source": r["source"]})
        g["amount"] += r["amount"]
        g["awards"] += 1
    unmatched = sorted((g for g in names.values() if g["amount"] >= UNMATCHED_MIN), key=lambda g: -g["amount"])[:8]
    state = {"seen": list(seen)[-SEEN_KEEP:], "daily": _trim(daily, today), "counts": counts,
             "recent": recent[-40:]}
    if state_path is not None:
        try:
            Path(state_path).write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            log.warning("contracts state not saved: %s", exc)
    return {"as_of": now.isoformat(timespec="minutes"), "sources": sources,
            "notable": sorted(recent, key=lambda r: (r["seen"], r["share_of_value"] or 0), reverse=True)[:10],
            "today": [s for s in scored if not s["notable"]][:8], "unmatched": unmatched,
            "how": "Context for IDEA lines (setup `contract`), never an order. share_of_value = the day's new money "
                   "/ market value; z = against the company's own last 90 business days. A source marked broken "
                   "or blocked gave no signal."}


def _trim(daily: dict, today: date) -> dict:
    cut = (today - timedelta(days=BASELINE_DAYS * 2)).isoformat()
    return {t: {d: v for d, v in days.items() if d >= cut} for t, days in daily.items()}


def render(p: dict) -> str:
    lines = ["GOVERNMENT CONTRACTS (context for IDEA lines, setup word `contract`)"]
    for name, h in p["sources"].items():
        lines.append(f"  feed {name}: {h['status']}" + (f" ({h.get('why')})" if h.get("why") else
                                                         f", {h.get('new', 0)} new of {h.get('rows', 0)}"))
    if not p["notable"]:
        lines.append("  Nothing notable in the last few days.")
    for r in p["notable"]:
        b = r["biggest"]
        lines.append(f"  {r['ticker']}: ${r['amount'] / 1e6:,.0f}M seen {r['seen']} = "
                     f"{(r['share_of_value'] or 0) * 100:.2f}% of its value" + (f", z {r['z']:+.1f}" if r.get("z") else "")
                     + f" ({b['agency']}: {b['what'][:70]})")
    for r in p["unmatched"]:
        lines.append(f"  unmatched: {r['recipient']} ${r['amount'] / 1e6:,.0f}M from {r['agency']}: is it listed?")
    return "\n".join(lines)
