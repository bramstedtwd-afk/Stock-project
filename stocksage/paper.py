"""Practice trades in an Alpaca PAPER account: evidence without risking a cent.

Confidence earns the right to change size only by proof (`confidence.calibration`):
HIGH must beat LOW over 30+ graded examples at each level. Real trades arrive a few a
week, so the trader also places each scored idea as a practice trade here: a bracket
order (entry, the idea's stop and target) that Alpaca's paper engine fills and exits by
itself, every one risking the same 0.5% of the paper account, so levels and setups are
compared fairly in R (multiples of the planned risk).

Safety, by construction:
  * The address is PINNED to Alpaca's paper host. There is no setting that points this
    module at a live account, and `tests/test_paper.py` fails the build if the live host
    ever appears in the code.
  * Before any order it reads the account and refuses unless the account number starts
    with "PA" (Alpaca's paper accounts).
  * Keys come only from the environment: ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET, saved
    in the owner's .env with `.\\start.bat keys` (the trader's cloud key store cannot hold
    Alpaca's two-part key), never a file in the repository. The owner's watcher mirrors the
    trader's fresh scored ideas (`mirror`, memory `ideas_recent`) every 5 minutes. StockSage's
    own code never places an order in a real account.

`python -m stocksage.paper account | buy TICKER --level L --setup S --stop X --target Y |
close TICKER | status` (standard library only, for the trader's environment).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from datetime import datetime, timezone

PAPER_HOST = "https://paper-api.alpaca.markets"     # pinned: the only trading host this file knows
DATA_HOST = "https://data.alpaca.markets"
KEY_VAR, SECRET_VAR = "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET"
RISK = 0.005           # every practice trade risks 0.5% of the paper account: levels compare in R
MAX_POSITION = 0.20    # and never puts more than a fifth of it in one name
MIN_TO_JUDGE = 10
LEVELS = ("HIGH", "MEDIUM", "LOW")
CLIENT_RE = re.compile(r"^ss-(HIGH|MEDIUM|LOW)-([a-z]+)-")


class NotPaper(RuntimeError):
    """The keys do not open a paper account: nothing is sent."""


def configured() -> bool:
    return bool(os.environ.get(KEY_VAR, "").strip() and os.environ.get(SECRET_VAR, "").strip())


def _call(method: str, path: str, body: dict | None = None, host: str = PAPER_HOST, http=None):
    if host not in (PAPER_HOST, DATA_HOST):
        raise NotPaper("only Alpaca's paper and market-data hosts are allowed")
    if http is not None:
        return http(method, host + path, body)
    req = urllib.request.Request(host + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"APCA-API-KEY-ID": os.environ.get(KEY_VAR, "").strip(),
                                          "APCA-API-SECRET-KEY": os.environ.get(SECRET_VAR, "").strip(),
                                          "Content-Type": "application/json", "User-Agent": "StockSage paper"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def account(http=None) -> dict:
    """The paper account, or NotPaper if these keys open anything else."""
    acct = _call("GET", "/v2/account", http=http)
    if not str(acct.get("account_number", "")).startswith("PA"):
        raise NotPaper("these keys do not open an Alpaca PAPER account (its number must start with PA); "
                       "nothing was sent")
    return acct


def last_price(ticker: str, http=None) -> float:
    got = _call("GET", f"/v2/stocks/{ticker.upper()}/trades/latest", host=DATA_HOST, http=http)
    return float((got.get("trade") or {}).get("p") or 0)


def bracket(ticker: str, level: str, setup: str, stop: float, target: float, http=None,
            now: datetime | None = None) -> dict:
    """One practice trade: buy at market with the stop and target attached (whole shares;
    Alpaca brackets need them), sized so the stop costs RISK of the paper account."""
    level, setup = level.upper(), re.sub(r"[^a-z]", "", setup.lower()) or "other"
    if level not in LEVELS:
        raise ValueError("level must be HIGH, MEDIUM or LOW (a NO idea is not traded, even on paper)")
    acct = account(http)
    equity = float(acct.get("equity") or 0)
    price = last_price(ticker, http)
    if not price or not (stop < price < target):
        raise ValueError(f"need stop < price < target (stop {stop}, price {price}, target {target})")
    qty = math.floor(min(equity * RISK / (price - stop), equity * MAX_POSITION / price))
    if qty < 1:
        raise ValueError("the stop is too far for a whole share within the risk guide")
    now = now or datetime.now(timezone.utc)
    body = {"symbol": ticker.upper(), "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "gtc",
            "order_class": "bracket", "take_profit": {"limit_price": round(target, 2)},
            "stop_loss": {"stop_price": round(stop, 2)},
            "client_order_id": f"ss-{level}-{setup}-{ticker.upper()}-{now:%Y%m%d%H%M%S}"}
    return _call("POST", "/v2/orders", body, http=http)


MIRROR_FRESH_MIN = 120     # an idea older than this is not mirrored: the moment has passed
MIRROR_DRIFT = 0.015       # nor one whose price has moved more than 1.5% since it was written
MIRROR_CAP = 15            # practice trades a day at most


def _regular_session(now: datetime) -> bool:
    from zoneinfo import ZoneInfo

    et = now.astimezone(ZoneInfo("America/New_York"))
    return et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)


def mirror(ideas: list[dict], state: dict, now: datetime | None = None, http=None) -> list[str]:
    """Each fresh scored idea (memory `ideas_recent`) becomes one practice bracket, once.
    Market orders in the regular session only; `state["paper"]` remembers what was done."""
    now = now or datetime.now(timezone.utc)
    if not ideas or not _regular_session(now):
        return []
    book = state.setdefault("paper", {})
    done = book.setdefault("done", {})
    today = now.date().isoformat()
    sent_today = sum(1 for v in done.values() if v.get("day") == today and v.get("sent"))
    out = []
    for idea in ideas:
        key = f"{idea['ticker']}|{idea['at']}"
        if key in done:
            continue
        age = (now - datetime.fromisoformat(idea["at"])).total_seconds() / 60
        if age > MIRROR_FRESH_MIN:
            done[key] = {"day": today, "sent": False, "why": "too old"}
            continue
        if sent_today >= MIRROR_CAP:
            break
        try:
            price = last_price(idea["ticker"], http)
            if not price or abs(price / idea["price"] - 1) > MIRROR_DRIFT or not idea["stop"] < price < idea["target"]:
                done[key] = {"day": today, "sent": False, "why": f"price {price} moved from {idea['price']}"}
                continue
            bracket(idea["ticker"], idea["level"], idea["setup"], idea["stop"], idea["target"], http=http, now=now)
            done[key] = {"day": today, "sent": True}
            sent_today += 1
            out.append(f"practice trade: {idea['ticker']} [{idea['level']}] {idea['setup']}, stop {idea['stop']:g}, "
                       f"target {idea['target']:g}")
        except NotPaper:
            raise
        except Exception as exc:
            done[key] = {"day": today, "sent": False, "why": f"{type(exc).__name__}: {str(exc)[:80]}"}
    cut = (now.date().toordinal() - 7)
    book["done"] = {k: v for k, v in done.items() if datetime.fromisoformat(v["day"]).toordinal() >= cut}
    return out


def close(ticker: str, http=None) -> dict:
    account(http)
    return _call("DELETE", f"/v2/positions/{ticker.upper()}", http=http)


def trades(orders: list[dict]) -> list[dict]:
    """Closed practice trades from Alpaca's nested order list: entry, exit, R, level, setup."""
    out = []
    for o in orders or []:
        m = CLIENT_RE.match(str(o.get("client_order_id") or ""))
        if not m or o.get("status") != "filled" or not o.get("filled_avg_price"):
            continue
        entry = float(o["filled_avg_price"])
        stop = None
        exit_fill = exit_at = None
        for leg in o.get("legs") or []:
            if leg.get("stop_price"):
                stop = float(leg["stop_price"])
            if leg.get("status") == "filled" and leg.get("filled_avg_price"):
                exit_fill, exit_at = float(leg["filled_avg_price"]), leg.get("filled_at")
        if exit_fill is None or not stop or stop >= entry:
            continue
        out.append({"ticker": o.get("symbol"), "level": m.group(1), "setup": m.group(2), "entry": entry,
                    "exit": exit_fill, "r": round((exit_fill - entry) / (entry - stop), 2),
                    "opened": o.get("filled_at"), "closed": exit_at})
    return out


def _group(rows: list[dict]) -> dict:
    from .journal import VERDICT_T

    rs = [r["r"] for r in rows]
    n = len(rs)
    avg = sum(rs) / n
    t = None
    if n >= 2:
        sd = (sum((x - avg) ** 2 for x in rs) / (n - 1)) ** 0.5
        t = round(avg / (sd / math.sqrt(n)), 2) if sd > 0 else None
    if n < MIN_TO_JUDGE:
        verdict = f"too few to judge ({n} of {MIN_TO_JUDGE} trades)"
    elif t is not None and t >= VERDICT_T:
        verdict = "working: keep doing it"
    elif t is not None and t <= -VERDICT_T:
        verdict = "losing money: do less of this"
    else:
        verdict = "no clear edge yet"
    return {"trades": n, "avg_r": round(avg, 2), "t": t, "won": round(sum(x > 0 for x in rs) / n, 2),
            "verdict": verdict}


def summarize(closed: list[dict]) -> dict:
    by_level = {lvl: _group([r for r in closed if r["level"] == lvl]) for lvl in LEVELS
                if any(r["level"] == lvl for r in closed)}
    by_setup = {s: _group([r for r in closed if r["setup"] == s]) for s in sorted({r["setup"] for r in closed})}
    return {"closed": len(closed), "by_confidence": by_level, "by_setup": by_setup,
            "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "recent": sorted(closed, key=lambda r: str(r.get("closed")), reverse=True)[:8]}


def record(http=None) -> dict:
    """The paper record for the trade memory (journal calls this where the keys exist)."""
    account(http)
    orders = _call("GET", "/v2/orders?status=closed&limit=500&nested=true&direction=desc", http=http)
    return summarize(trades(orders if isinstance(orders, list) else []))


def lines(p: dict | None) -> list[str]:
    if not p or not p.get("closed"):
        return []
    out = ["", f"PRACTICE TRADES (Alpaca paper account, each risking the same 0.5%; results in R): {p['closed']} closed"]
    for name, groups in (("by level", p.get("by_confidence") or {}), ("by setup", p.get("by_setup") or {})):
        for k, g in groups.items():
            out.append(f"  {name} {k:<10}{g['trades']:>4} trades, won {g['won'] * 100:.0f}%, avg {g['avg_r']:+.2f}R: "
                       f"{g['verdict']}")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="python -m stocksage.paper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("account")
    b = sub.add_parser("buy")
    b.add_argument("ticker")
    b.add_argument("--level", required=True)
    b.add_argument("--setup", required=True)
    b.add_argument("--stop", type=float, required=True)
    b.add_argument("--target", type=float, required=True)
    c = sub.add_parser("close")
    c.add_argument("ticker")
    sub.add_parser("status")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    if not configured():
        print(f"Paper trading is not set up here: {KEY_VAR} and {SECRET_VAR} are missing from this environment.")
        return 1
    try:
        if a.cmd == "account":
            acct = account()
            print(f"Paper account ••••{str(acct['account_number'])[-4:]}: equity {float(acct['equity']):,.2f}, "
                  f"cash {float(acct['cash']):,.2f}")
        elif a.cmd == "buy":
            o = bracket(a.ticker, a.level, a.setup, a.stop, a.target)
            print(f"Paper bracket sent: {o.get('symbol')} x{o.get('qty')} ({o.get('status')}), "
                  f"stop {a.stop}, target {a.target}, id {o.get('client_order_id')}")
        elif a.cmd == "close":
            close(a.ticker)
            print(f"Paper position in {a.ticker.upper()} closed.")
        else:
            print("\n".join(lines(record())[1:]) or "No closed practice trades yet.")
    except NotPaper as exc:
        print(f"REFUSED: {exc}")
        return 2
    except Exception as exc:
        print(f"Paper call failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
