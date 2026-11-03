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

Brake: once the account is 10% below its highest check-in, new trades are
LOW size whatever the score, until it recovers. Selling is never braked.

Score -> level -> what the trader may do (defaults, the owner set no hard caps):
  HIGH   (>= 4) : no size limit beyond settled cash; may add to a winner.
  MEDIUM (2-3)  : up to a quarter of the account.
  LOW    (0-1)  : a test position, about 5-8% of the account.
  NO     (< 0)  : do not open. Exits are never gated by confidence.

Confidence is graded too: every buy's TAG carries its level ("[HIGH] why..."),
the scorecard groups trades by it, and if HIGH trades do not beat LOW ones
after 10 of each, the trader sizes HIGH like MEDIUM until they do.

Standard library only: `python -m stocksage.confidence TICKER --setup pullback`.
"""

from __future__ import annotations

import json

LEVELS = (("HIGH", 4), ("MEDIUM", 2), ("LOW", 0))
SIZE_GUIDE = {"HIGH": "no limit beyond settled cash; may add to a winner",
              "MEDIUM": "up to 25% of the account", "LOW": "a test position, 5-8% of the account",
              "NO": "do not open (exits are never gated)"}
SHORT_SIDE_SETUPS = {"inverse", "defensive", "bounce", "fear"}
MIN_GROUP = 10
MIN_IDEAS = 20
BRAKE_AT = -0.10          # account this far below its peak: new trades at LOW size until it recovers


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
    avg = group.get("avg_return") or 0.0
    desc = f"your {label}: {group['trades']} trades, avg {avg:+.1%}, {v}"
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


def score(*, side: str = "buy", setup: str = "", asset: str = "stocks", play: str = "",
          lookalike: dict | None = None, memory: dict | None = None, regime: str | None = None,
          crypto_flags: dict | None = None, confirmations: int = 0) -> dict:
    """Points from each kind of evidence, the level, and the reasons in plain words."""
    parts: list[tuple[int, str]] = []
    if asset == "stocks":
        parts.append(_lookalike_points(lookalike))
    memory = memory or {}
    setup_group = (memory.get("by_setup") or {}).get(setup)
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
    dd = memory.get("drawdown")
    if side == "buy" and dd is not None and dd <= BRAKE_AT and level in ("HIGH", "MEDIUM"):
        level = "LOW"
        reasons.append(f"BRAKE: the account is {dd:.1%} below its peak, so new trades are LOW size until it recovers")
    return {"level": level, "score": total, "size": SIZE_GUIDE[level], "reasons": reasons}


def evidence_for(brief: dict, ticker: str, side: str = "buy") -> dict | None:
    return ((brief.get("evidence") or {}).get(ticker.upper()) or {}).get(side)


def for_trade(ticker: str, setup: str, *, side: str = "buy", play: str = "", confirmations: int = 0,
              brief: dict | None = None, memory: dict | None = None) -> dict:
    """Score a trade from the brief (look-alikes, regime, crypto flags) and the trade memory."""
    brief, memory = brief or {}, memory or {}
    from .journal import is_crypto, setup_word

    asset = "crypto" if is_crypto(ticker) else "stocks"
    out = score(side=side, setup=setup_word(setup), asset=asset, play=play.upper(),
                lookalike=evidence_for(brief, ticker, side), memory=memory,
                regime=((brief.get("market") or {}).get("regime") or {}).get("label"),
                crypto_flags=(brief.get("crypto") or {}).get("flags"), confirmations=confirmations)
    out["ticker"] = ticker.upper()
    return out


def render(r: dict) -> str:
    return "\n".join([f"CONFIDENCE {r.get('ticker', '')}: {r['level']} (score {r['score']:+d}) -> {r['size']}",
                      *("  " + x for x in r["reasons"]),
                      f"  Tag the buy's why starting with [{r['level']}] so this level gets graded."])


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
