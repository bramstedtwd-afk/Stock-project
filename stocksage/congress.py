"""Congress trade tracking — watch where lawmakers put their money.

Members of Congress disclose their stock trades (STOCK Act), and their
aggregate returns have historically outpaced the market, so recent
congressional *buying* would be a useful contextual tilt — not a mechanical
signal, never something that overrides the technical/graded model.

Disabled as of 2026-07: the free source this used to read (the House/Senate
Stock Watcher JSON mirrors, including its GitHub-hosted copy) has been dead
since March 2021, and the alternatives checked since — Finnhub, Quiver
Quantitative — gate this specific dataset behind a paid plan. Rather than
hit a dead endpoint on every scan, `CongressData` now always reports "no
data" with zero network calls. The aggregation helpers below (`normalize`,
`summarize`, `notable_buys`) are kept because they're source-agnostic —
wiring in a real feed later is just implementing `CongressData.transactions`
again.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

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
    """No live source currently available (see module docstring).

    Kept as the stable entry point every call site already uses, so wiring
    in a real feed later is a one-method change here — nothing upstream
    needs to move.
    """

    def transactions(self) -> list[dict]:
        return []

    def summary(self, today: date | None = None) -> dict[str, dict]:
        return {}
