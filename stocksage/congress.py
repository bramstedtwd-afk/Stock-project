"""Congress trade tracking — watch where lawmakers put their money.

Members of Congress disclose their stock trades (STOCK Act), and their
aggregate returns have historically outpaced the market, so recent
congressional *buying* is a useful contextual tilt — not a mechanical
signal. This module keeps it deliberately light: pull recent disclosures
from a free public source, aggregate net buying per ticker over a lookback
window, and surface the notable names as context in the research brief. It
never overrides the technical/graded model; it's one more lens the routine
can weigh.

Data: the public House/Senate Stock Watcher JSON dumps (no key required).
Everything is cached and fully defensive — an unreachable or reshaped feed
degrades to "no congress data", never an error.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

HOUSE_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
)
SENATE_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/"
    "all_transactions.json"
)
DEFAULT_CACHE_DIR = Path("~/.stocksage/cache")
CACHE_TTL_SECONDS = 24 * 3600  # disclosures move slowly; refresh daily
LOOKBACK_DAYS = 90


def parse_amount_range(text: str | None) -> float:
    """'$1,001 - $15,000' -> 8000.5 (midpoint). Best-effort, 0.0 on junk."""
    if not text:
        return 0.0
    nums = [
        float(n.replace(",", ""))
        for n in re.findall(r"[\d,]+", text)
        if n.replace(",", "").isdigit()
    ]
    return sum(nums) / len(nums) if nums else 0.0


def _to_iso(value: str | None) -> str:
    """Normalize a transaction date to YYYY-MM-DD; '' when unparseable."""
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def normalize_type(raw: str | None) -> str | None:
    t = (raw or "").lower()
    if "purchase" in t or t == "buy":
        return "buy"
    if "sale" in t or "sell" in t:
        return "sell"
    return None


def normalize(records: list[dict]) -> list[dict]:
    """Raw disclosure rows -> [{ticker, type, date, member, amount}]."""
    out = []
    for r in records:
        if not isinstance(r, dict):
            continue
        ticker = (r.get("ticker") or "").upper().strip()
        if not ticker or ticker in ("--", "N/A"):
            continue
        typ = normalize_type(r.get("type"))
        if typ is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "type": typ,
                "date": _to_iso(r.get("transaction_date") or r.get("disclosure_date")),
                "member": r.get("representative") or r.get("senator") or "",
                "amount": parse_amount_range(r.get("amount")),
            }
        )
    return out


def summarize(
    transactions: list[dict], today: date | None = None, lookback_days: int = LOOKBACK_DAYS
) -> dict[str, dict]:
    """Aggregate recent congressional activity per ticker."""
    ref = today or date.today()
    cutoff = (ref - timedelta(days=lookback_days)).isoformat()
    horizon = ref.isoformat()
    agg: dict[str, dict] = {}
    for t in transactions:
        d = t.get("date", "")
        if not d or d < cutoff or d > horizon:
            continue
        a = agg.setdefault(
            t["ticker"],
            {"buys": 0, "sells": 0, "members": set(), "est_amount": 0.0, "last_date": ""},
        )
        if t["type"] == "buy":
            a["buys"] += 1
            a["members"].add(t["member"])
            a["est_amount"] += t["amount"]
        else:
            a["sells"] += 1
        if d > a["last_date"]:
            a["last_date"] = d
    return {
        tk: {
            "buys": a["buys"],
            "sells": a["sells"],
            "net_buys": a["buys"] - a["sells"],
            "members": len(a["members"]),
            "est_amount": round(a["est_amount"]),
            "last_date": a["last_date"],
        }
        for tk, a in agg.items()
    }


def notable_buys(summary: dict[str, dict], min_buys: int = 2, top: int = 10) -> list[str]:
    """Tickers with real recent congressional accumulation, ranked."""
    items = [
        (tk, s)
        for tk, s in summary.items()
        if s["buys"] >= min_buys and s["net_buys"] > 0
    ]
    items.sort(
        key=lambda kv: (kv[1]["members"], kv[1]["net_buys"], kv[1]["est_amount"]),
        reverse=True,
    )
    return [tk for tk, _ in items[:top]]


class CongressData:
    """Cached, defensive access to the congressional-trade summary."""

    def __init__(self, cache_dir: str | Path | None = None, ttl: int = CACHE_TTL_SECONDS):
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    def _fetch(self, url: str) -> list[dict]:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            log.warning("congress fetch failed (%s): %s", url, exc)
            return []

    def transactions(self) -> list[dict]:
        cache = self.cache_dir / "congress_transactions.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < self.ttl:
            try:
                return json.loads(cache.read_text())
            except Exception:
                cache.unlink(missing_ok=True)
        rows = normalize(self._fetch(HOUSE_URL)) + normalize(self._fetch(SENATE_URL))
        # Cache the result even when empty (source unreachable or dead) so a
        # persistent failure is retried once per TTL window, not on every
        # single scan — without this, a permanently dead feed gets hit (and
        # logs a warning) on every scheduled publish run, all day, forever.
        try:
            cache.write_text(json.dumps(rows))
        except OSError:
            pass
        return rows

    def summary(self, today: date | None = None) -> dict[str, dict]:
        try:
            return summarize(self.transactions(), today=today)
        except Exception as exc:  # never let this break a scan or a brief
            log.warning("congress summary failed: %s", exc)
            return {}
