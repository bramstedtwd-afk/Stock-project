"""The forecast ledger: is the trader better than the prediction market's price?

Prediction markets (Kalshi, Polymarket, Robinhood's event contracts) price questions
about the Fed, the economy and politics. Before any money goes there, the trader writes
its own probability next to the market's, and this grades both once the question is
settled:

  FORECAST | UTC time | question id | my chance | market chance | the question
  RESOLVED | UTC time | question id | YES or NO

Chances may be written as 0.65 or 65%. A Polymarket question uses its id `pm:<slug>`
and is settled automatically from Polymarket's public data once it closes; any other id
(a Kalshi ticker, a Robinhood event) is settled by a RESOLVED line.

Scoring (one row per question per day, the day's last forecast):
  * Brier score (squared error, lower is better) for the trader and for the market.
    `edge` = market's Brier minus the trader's: above 0 means the trader was closer.
  * Paper betting: whenever the trader differs from the market by 5+ points, one
    contract is "bought" at the market's price on the trader's side, minus Kalshi's
    fee (0.07 x price x (1 - price)). The money result is what a real bettor would
    have made, the test that matters.
The verdict needs 50 settled forecasts and uses the same t bar as everything else.
A record, never an order: StockSage places nothing.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com/markets?slug={slug}"
MIN_TO_JUDGE = 50
BET_GAP = 0.05
FEE = 0.07
RECHECK_HOURS = 6
MAX_LOOKUPS = 10


def _ts(text: str):
    try:
        t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _chance(text: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(%?)\s*", text or "")
    if not m:
        return None
    v = float(m.group(1)) / (100 if m.group(2) or float(m.group(1)) > 1 else 1)
    return v if 0.0 <= v <= 1.0 else None


def parse_forecasts(text: str) -> list[dict]:
    """FORECAST lines, the last one per (question, UTC day)."""
    out: dict[tuple, dict] = {}
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 5 or parts[0].upper() != "FORECAST":
            continue
        at, qid, mine, market = _ts(parts[1]), parts[2], _chance(parts[3]), _chance(parts[4])
        if at is None or not qid or mine is None or market is None:
            continue
        out[(qid, at.date())] = {"at": at, "id": qid, "mine": mine, "market": market,
                                 "question": " | ".join(parts[5:])[:160]}
    return sorted(out.values(), key=lambda f: f["at"])


def parse_resolved(text: str) -> dict[str, int]:
    out = {}
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) >= 4 and parts[0].upper() == "RESOLVED" and _ts(parts[1]) and parts[3].upper() in ("YES", "NO"):
            out[parts[2]] = 1 if parts[3].upper() == "YES" else 0
    return out


def polymarket_outcome(slug: str, fetch=None) -> int | None:
    """1 (YES) / 0 (NO) once Polymarket has closed and settled the market, else None."""
    if fetch is None:
        def fetch(url):
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "StockSage research"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
    got = fetch(GAMMA.format(slug=slug))
    market = got[0] if isinstance(got, list) and got else got if isinstance(got, dict) else None
    if not market or not market.get("closed"):
        return None
    try:
        yes = float(json.loads(market.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None
    return 1 if yes >= 0.99 else 0 if yes <= 0.01 else None


def settle(forecasts: list[dict], resolved: dict[str, int], auto: dict | None = None, fetch=None,
           now: datetime | None = None) -> dict:
    """Outcomes for every forecast question: RESOLVED lines first, then Polymarket's own
    settlement for `pm:` ids (at most MAX_LOOKUPS asked per call, each again after
    RECHECK_HOURS). `auto` = {id: {"outcome", "checked"}} carried in the memory."""
    now = now or datetime.now(timezone.utc)
    auto = dict(auto or {})
    asked = 0
    for qid in dict.fromkeys(f["id"] for f in forecasts):
        if qid in resolved or not qid.startswith("pm:") or (auto.get(qid) or {}).get("outcome") is not None:
            continue
        last = _ts((auto.get(qid) or {}).get("checked", "") or "1970-01-01T00:00:00+00:00")
        if last and (now - last).total_seconds() < RECHECK_HOURS * 3600 or asked >= MAX_LOOKUPS:
            continue
        asked += 1
        try:
            outcome = polymarket_outcome(qid[3:], fetch)
        except Exception:
            outcome = None
        auto[qid] = {"outcome": outcome, "checked": now.isoformat(timespec="minutes")}
    return auto


def score(forecasts: list[dict], resolved: dict[str, int], auto: dict | None = None) -> dict:
    """Brier for the trader and the market, the paper betting result, and a verdict."""
    outcome = dict({k: v["outcome"] for k, v in (auto or {}).items() if v.get("outcome") is not None}, **resolved)
    rows, bets = [], []
    for f in forecasts:
        o = outcome.get(f["id"])
        if o is None:
            continue
        mine, mkt = (f["mine"] - o) ** 2, (f["market"] - o) ** 2
        rows.append(mkt - mine)
        gap = f["mine"] - f["market"]
        if abs(gap) >= BET_GAP:
            q = f["market"]
            fee = FEE * q * (1 - q)
            bets.append(((o - q) if gap > 0 else (q - o)) - fee)
    n = len(rows)
    t = None
    if n >= 2:
        sd = (sum((x - sum(rows) / n) ** 2 for x in rows) / (n - 1)) ** 0.5
        t = round((sum(rows) / n) / (sd / math.sqrt(n)), 2) if sd > 0 else None
    from .journal import VERDICT_T

    if n < MIN_TO_JUDGE:
        verdict = f"too few to judge ({n} of {MIN_TO_JUDGE} settled)"
    elif t is not None and t >= VERDICT_T:
        verdict = "working: the trader beats the market's price"
    elif t is not None and t <= -VERDICT_T:
        verdict = "losing: the market's price beats the trader"
    else:
        verdict = "no clear edge yet"
    open_ids = sorted({f["id"] for f in forecasts if outcome.get(f["id"]) is None})
    return {"forecasts": len(forecasts), "settled": n, "open": len(open_ids),
            "edge": round(sum(rows) / n, 4) if n else None, "t": t, "verdict": verdict,
            "bets": len(bets), "bet_pnl_per_contract": round(sum(bets) / len(bets), 4) if bets else None,
            "bet_pnl_total": round(sum(bets), 2) if bets else None}


def lines(s: dict | None) -> list[str]:
    if not s or not s.get("forecasts"):
        return []
    out = ["", f"FORECASTS (prediction-market questions: you vs the market's price): {s['forecasts']} written, "
               f"{s['settled']} settled, {s['open']} still open"]
    if s["settled"]:
        out.append(f"  Brier edge {s['edge']:+.4f} a question (above 0 = closer than the market): {s['verdict']}")
    if s.get("bets"):
        out.append(f"  Paper bets where you differed by 5+ points: {s['bets']}, {s['bet_pnl_per_contract']:+.3f} "
                   f"a $1 contract after fees ({s['bet_pnl_total']:+.2f} in all)")
    return out
