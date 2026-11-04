"""Confidence: how did situations like this one actually turn out?

The trader's freedom scales with confidence, so confidence must mean something
measurable, not a feeling. It is built from evidence about SIMILAR situations,
each counted only when there is enough of it:

  1. Look-alikes (the strongest): the 150 most similar past setups by signal
     fingerprint (trend, momentum, RSI, volume, relative strength...) from
     StockSage's walk-forward history, averaged per date, measured against the
     market after costs over the next week (analogs.py). BACKED = the 95% lower
     bound clears zero.
  2. The trader's own record on this kind of trade: same setup, same play,
     same asset class, once a group has 10+ closed trades (journal.py).
  3. The market agrees: the tape's regime for stocks, bitcoin's trend for crypto.
  4. Live confirmations the trader sees right now (each one an independent
     reason): a fresh catalyst, strength vs its sector/market today, volume
     well above average.
  5. The trader's graded IDEAS (trades considered, taken or not, checked 5
     days later): cheaper evidence, used only while the setup has no 10-trade
     record, and weighted lower.

Brake: once the trades' own results have given back 10% of the account from
their best point, new trades are LOW size whatever the score, until they win
it back. Market moves of parked cash do not count: the scenario lab found a
brake on the account's value fired on every market dip and cost about 5% a
year in crash and choppy markets while protecting nothing. Selling is never
braked.

Discipline (the trade memory's `discipline`, rebuilt at every note; it resets
each trading day, so a bad day never turns into a timid week):
  DAILY LIMIT  account 4%+ down on the day: NO new buys until tomorrow.
  NO REVENGE   a name sold at a loss today: NO buying it back today.
  COOL-OFF     2 losing sales in a row, the latest today: the next buy is LOW,
               until a winning sale or tomorrow.

Score -> level -> what the trader may do (defaults, the owner set no hard caps):
  HIGH   (>= 4) : no size limit beyond settled cash; may add to a winner.
  MEDIUM (2-3)  : up to a quarter of the account.
  LOW    (0-1)  : a test position, about 5-8% of the account.
  NO     (< 0)  : do not open. Exits are never gated by confidence.

Confidence is graded too: every buy's TAG carries its level ("[HIGH] why..."),
and the scorecard groups trades by it. It is reported, not enforced: the
scenario lab found capping HIGH whenever HIGH trailed LOW cost 2-3% a year
with no drawdown benefit (10 trades of each is mostly luck).

Standard library only: `python -m stocksage.confidence TICKER --setup pullback`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

LEVELS = (("HIGH", 4), ("MEDIUM", 2), ("LOW", 0))
SIZE_GUIDE = {"HIGH": "no limit beyond settled cash; may add to a winner",
              "MEDIUM": "up to 25% of the account", "LOW": "a test position, 5-8% of the account",
              "NO": "do not open (exits are never gated)"}
SHORT_SIDE_SETUPS = {"inverse", "defensive", "bounce", "fear"}
MIN_GROUP = 10
MIN_IDEAS = 20
BRAKE_AT = -0.10          # account this far below its peak: new trades at LOW size until it recovers


def _adj(group: dict) -> float:
    v = group.get("avg_adj")
    return (group.get("avg_return") or 0.0) if v is None else v


def _lookalike_points(ev: dict | None) -> tuple[int, str]:
    if not ev or ev.get("edge") is None:
        return 0, "no look-alike history for this ticker"
    edge, lower = ev["edge"], ev.get("lower")
    upper = edge + (edge - lower) if lower is not None else None
    where = (f"{ev.get('n', '?')} look-alikes over {ev.get('dates', '?')} dates: {edge:+.2%} a week vs the market "
             f"after costs ({(ev.get('hit_rate') or 0):.0%} went this way)")
    if ev.get("backed"):
        return 3, where + " - history BACKS it"
    if upper is not None and upper < 0:
        return -3, where + " - history says it LOSES"
    return (1, where + " - leans right, not proven") if edge > 0 else (-1, where + " - leans wrong")


def _record_points(group: dict | None, label: str) -> tuple[int, str]:
    if not group or group.get("trades", 0) < MIN_GROUP:
        n = (group or {}).get("trades", 0)
        return 0, f"your {label}: {n} closed trades, too few to count"
    v = group.get("verdict", "")
    avg = group.get("avg_adj")
    avg = (group.get("avg_return") or 0.0) if avg is None else avg
    desc = f"your {label}: {group['trades']} trades, avg {avg:+.1%} vs the market, {v}"
    if v.startswith("working"):
        return 2, desc
    if v.startswith("losing"):
        return -3, desc
    return (1 if avg > 0 else -1), desc


def _idea_points(group: dict | None, setup: str) -> tuple[int, str]:
    """Graded ideas (trades considered, taken or not) are cheaper evidence than real
    trades: they count only when the setup has no 10-trade record, and weigh less."""
    if not group or group.get("ideas", 0) < MIN_IDEAS:
        return 0, f"your '{setup}' ideas: {(group or {}).get('ideas', 0)} graded, too few to count"
    v = group.get("verdict", "")
    desc = f"your '{setup}' ideas: {group['ideas']} graded, avg {group.get('avg_5d', 0):+.1%} over 5 days, {v}"
    if v.startswith("working"):
        return 1, desc
    if v.startswith("losing"):
        return -2, desc
    return 0, desc


def todays_discipline(memory: dict | None, now: datetime | None = None) -> dict:
    """The memory's discipline state, only if it was worked out for today (Eastern)."""
    from zoneinfo import ZoneInfo

    d = (memory or {}).get("discipline") or {}
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    return d if d.get("day") == today else {}


def _discipline(level: str, reasons: list[str], d: dict, ticker: str) -> str:
    if d.get("limit_hit"):
        reasons.append(f"DAILY LIMIT: the account is {d.get('day_change', 0):+.1%} today (limit "
                       f"{d.get('limit', -0.04):.0%}): no new buys until tomorrow, exits and stops only")
        return "NO"
    if ticker and ticker.upper() in (d.get("stopped_today") or []):
        reasons.append(f"NO REVENGE: you sold {ticker.upper()} at a loss today; it can be bought again tomorrow")
        return "NO"
    if d.get("cool_off") and level in ("HIGH", "MEDIUM"):
        reasons.append(f"COOL-OFF: {d.get('losses_in_a_row')} losing sales in a row, the latest today: this buy is "
                       "test size; full size again after a winning sale or tomorrow")
        return "LOW"
    return level


def _decided(group: dict | None) -> bool:
    return str((group or {}).get("verdict", "")).startswith(("working", "losing"))


def _context_points(memory: dict, setup: str) -> tuple[int | None, str]:
    """This setup in THIS kind of market (the memory's regime today): the trades'
    verdict first, then the graded ideas', each only once it is decided. A setup
    can work in rising markets and fail in falling ones; the plain setup record
    averages that away."""
    reg = memory.get("regime_now")
    if not reg:
        return None, ""
    key = f"{setup}|{reg}"
    group = (memory.get("by_setup_regime") or {}).get(key)
    if _decided(group) and group.get("trades", 0) >= MIN_GROUP:
        return _record_points(group, f"'{setup}' trades in a {reg} market")
    ideas = ((memory.get("ideas") or {}).get("by_setup_regime") or {}).get(key)
    if _decided(ideas):
        return _idea_points(ideas, f"{setup}' in a {reg} market, '{setup}")[0], \
            f"your '{setup}' ideas in a {reg} market: {ideas['ideas']} graded, avg {ideas.get('avg_5d', 0):+.1%} " \
            f"over 5 days, {ideas['verdict']}"
    return None, ""


def score(*, side: str = "buy", setup: str = "", asset: str = "stocks", play: str = "",
          lookalike: dict | None = None, memory: dict | None = None, regime: str | None = None,
          crypto_flags: dict | None = None, confirmations: int = 0, ticker: str = "",
          now: datetime | None = None) -> dict:
    """Points from each kind of evidence, the level, and the reasons in plain words."""
    parts: list[tuple[int, str]] = []
    if asset == "stocks":
        parts.append(_lookalike_points(lookalike))
    memory = memory or {}
    setup_group = (memory.get("by_setup") or {}).get(setup)
    pts, why = _context_points(memory, setup)
    if pts is None:
        pts, why = _record_points(setup_group, f"'{setup}' trades")
    if pts == 0 and (setup_group or {}).get("trades", 0) < MIN_GROUP:
        ipts, iwhy = _idea_points(((memory.get("ideas") or {}).get("by_setup") or {}).get(setup), setup)
        if ipts:
            pts, why = ipts, iwhy
    if pts == 0 and play:
        pts, why = _record_points((memory.get("by_play") or {}).get(play), f"{play} trades")
    parts.append((pts, why))
    if asset == "crypto":
        parts.append(_record_points((memory.get("by_asset") or {}).get("crypto"), "crypto trades"))
    if side == "buy":
        if asset == "crypto":
            f = crypto_flags or {}
            if setup == "fear":
                parts.append((1, "extreme fear") if f.get("extreme_fear") else (-1, "not extreme fear"))
            elif setup != "bounce":
                up = f.get("btc_uptrend")
                parts.append((1, "bitcoin in an uptrend") if up else (-2, "bitcoin below its 50-day average")
                             if up is False else (0, "bitcoin trend unknown"))
        elif setup not in SHORT_SIDE_SETUPS:
            r = (regime or "").lower()
            parts.append((1, "market regime up") if r == "up" else (-1, "market regime down") if r == "down"
                         else (0, f"market regime {r or 'unknown'}"))
    c = max(0, min(int(confirmations or 0), 3))
    parts.append((1 if c >= 2 else 0, f"{c} live confirmation(s) (catalyst / relative strength / volume)"))
    total = sum(p for p, _ in parts)
    level = next((name for name, floor in LEVELS if total >= floor), "NO")
    reasons = [f"{p:+d}  {w}" for p, w in parts]
    dd = memory["trading_drawdown"] if "trading_drawdown" in memory else memory.get("drawdown")
    if side == "buy" and dd is not None and dd <= BRAKE_AT and level in ("HIGH", "MEDIUM"):
        level = "LOW"
        reasons.append(f"BRAKE: your trades have given back {-dd:.1%} of the account from their best point, so new "
                       "trades are LOW size until they win it back")
    if side == "buy":
        level = _discipline(level, reasons, todays_discipline(memory, now), ticker)
    return {"level": level, "score": total, "size": SIZE_GUIDE[level], "reasons": reasons}


def evidence_for(brief: dict, ticker: str, side: str = "buy") -> dict | None:
    return ((brief.get("evidence") or {}).get(ticker.upper()) or {}).get(side)


def for_trade(ticker: str, setup: str, *, side: str = "buy", play: str = "", confirmations: int = 0,
              brief: dict | None = None, memory: dict | None = None, now: datetime | None = None) -> dict:
    """Score a trade from the brief (look-alikes, regime, crypto flags) and the trade memory."""
    brief, memory = brief or {}, memory or {}
    from .journal import is_crypto, setup_word

    asset = "crypto" if is_crypto(ticker) else "stocks"
    out = score(side=side, setup=setup_word(setup), asset=asset, play=play.upper(),
                lookalike=evidence_for(brief, ticker, side), memory=memory,
                regime=((brief.get("market") or {}).get("regime") or {}).get("label"),
                crypto_flags=(brief.get("crypto") or {}).get("flags"), confirmations=confirmations,
                ticker=ticker, now=now)
    out["ticker"] = ticker.upper()
    return out


def render(r: dict) -> str:
    return "\n".join([f"CONFIDENCE {r.get('ticker', '')}: {r['level']} (score {r['score']:+d}) -> {r['size']}",
                      *("  " + x for x in r["reasons"]),
                      f"  Start the buy's why with [{r['level']}] and name its stop ('stop 118.40'): the level and the "
                      "risk both get graded."])


def _drive_json(name: str) -> dict:
    try:
        from .drive_api import read_drive_text

        return json.loads(read_drive_text(name) or "{}")
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    """`python -m stocksage.confidence TICKER --setup pullback [--play SAFE] [--confirm 2] [--sell]`."""
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="python -m stocksage.confidence")
    p.add_argument("ticker")
    p.add_argument("--setup", default="")
    p.add_argument("--play", default="")
    p.add_argument("--confirm", type=int, default=0, help="live confirmations you see: catalyst, strength, volume (0-3)")
    p.add_argument("--sell", action="store_true")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    from .drive_api import BRIEF_NAME
    from .journal import MEMORY_NAME

    r = for_trade(a.ticker, a.setup, side="sell" if a.sell else "buy", play=a.play, confirmations=a.confirm,
                  brief=_drive_json(BRIEF_NAME), memory=_drive_json(MEMORY_NAME))
    print(render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
