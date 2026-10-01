"""The forward record: every call the sheet makes, graded against SPY.

Backtests are the place to find ideas and live results are the place to
believe them. The sheet's own calls (the rules, the model's sells, plain buys
and the buys history did not back, "ideas") are logged the day they are made
and graded after HORIZON trading bars against SPY on the same bars, using only
what the sheet could have known. Nothing is trained on any of this.

Honesty details:
  * One live call per (ticker, kind) at a time, so overlapping windows of the
    same name are not counted twice.
  * The uncertainty comes from the spread between two-week buckets, not between
    calls: names called the same week share the market's mood.
  * Whether a kind has "earned" anything is decided by `actions.trust_level`,
    the one definition of proof used everywhere.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from .actions import MIN_N_FAILING, trust_level

HORIZON = 10               # trading bars, the same as the active account's clock
BUCKET_DAYS = 14           # calls within a fortnight are treated as one observation
MIN_BUCKETS = 8
BENCH = "SPY"

SIGN = {"sell_rule": -1.0, "sell_model": -1.0, "trim": -1.0, "buy": 1.0, "idea": 1.0}
LABEL = {
    "sell_rule": "rule sells (stops, clock)", "sell_model": "model sells",
    "trim": "size-cap trims", "buy": "plain buys", "idea": "buy ideas history did not back",
}
_VERB_KIND = {("SELL", "risk rule"): "sell_rule", ("SELL", "model call"): "sell_model",
              ("TRIM", "risk rule"): "trim", ("BUY", "model call"): "buy"}


def calls_in(sheet) -> set[tuple[str, str]]:
    """(ticker, kind) for everything on the sheet worth keeping score of."""
    out: set[tuple[str, str]] = set()
    for acct in sheet.accounts:
        for d in acct.directives:
            kind = _VERB_KIND.get((d.verb, d.basis))
            if kind:
                out.add((d.ticker, kind))
        for ticker in acct.ideas:
            out.add((ticker, "idea"))
    return out


def record(db, sheet, today: date | None = None) -> int:
    """Log today's calls. Returns how many were new."""
    day = (today or date.today()).isoformat()
    added = 0
    for ticker, kind in sorted(calls_in(sheet)):
        live = db.conn.execute(
            "SELECT 1 FROM sheet_calls WHERE ticker = ? AND kind = ? AND evaluated = 0",
            (ticker, kind)).fetchone()
        if live:
            continue
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO sheet_calls (call_date, ticker, kind, horizon_days)"
            " VALUES (?, ?, ?, ?)", (day, ticker, kind, HORIZON))
        added += cur.rowcount
    db.conn.commit()
    return added


def _closes(df):
    if df is None or len(df) == 0 or "Close" not in df:
        return None
    return df["Close"].dropna()


def grade(db, market) -> int:
    """Grade every call whose horizon has passed. Returns how many were graded."""
    import pandas as pd

    pending = db.conn.execute(
        "SELECT id, call_date, ticker, horizon_days FROM sheet_calls WHERE evaluated = 0"
    ).fetchall()
    if not pending:
        return 0
    bench = _closes(market.history(BENCH))
    if bench is None:
        return 0
    graded = 0
    for row in pending:
        px = _closes(market.history(row["ticker"]))
        if px is None:
            continue
        start = pd.Timestamp(row["call_date"])
        idx = px.index.tz_localize(None) if getattr(px.index, "tz", None) else px.index
        i0 = int(idx.searchsorted(start, side="right")) - 1       # last bar on or before the call
        i1 = i0 + int(row["horizon_days"])
        if i0 < 0 or i1 >= len(px):
            continue
        d0, d1 = idx[i0], idx[i1]
        bidx = bench.index.tz_localize(None) if getattr(bench.index, "tz", None) else bench.index
        b0, b1 = bench[bidx <= d0], bench[bidx <= d1]
        if len(b0) == 0 or len(b1) == 0:
            continue
        ret = float(px.iloc[i1] / px.iloc[i0] - 1.0)
        bret = float(b1.iloc[-1] / b0.iloc[-1] - 1.0)
        db.conn.execute(
            "UPDATE sheet_calls SET evaluated = 1, ret = ?, bench_ret = ? WHERE id = ?",
            (ret, bret, row["id"]))
        graded += 1
    db.conn.commit()
    return graded


def scorecard(db) -> dict[str, dict]:
    """Per kind: how many graded, how many waiting, mean edge vs SPY and its verdict.

    Edge is in the direction of the call: a sell is right when the stock
    trails the market, so its edge is minus the excess return.
    """
    out: dict[str, dict] = {}
    for kind in SIGN:
        rows = db.conn.execute(
            "SELECT call_date, ret, bench_ret, evaluated FROM sheet_calls WHERE kind = ?",
            (kind,)).fetchall()
        done = [r for r in rows if r["evaluated"]]
        entry = {"graded": len(done), "waiting": len(rows) - len(done),
                 "edge": None, "se": None, "level": "unproven"}
        if done:
            buckets: dict[int, list[float]] = {}
            for r in done:
                day = datetime.strptime(r["call_date"], "%Y-%m-%d").toordinal() // BUCKET_DAYS
                buckets.setdefault(day, []).append(SIGN[kind] * (r["ret"] - r["bench_ret"]))
            means = [sum(v) / len(v) for v in buckets.values()]
            entry["edge"] = sum(means) / len(means)
            entry["buckets"] = len(means)
            if len(means) >= MIN_BUCKETS:
                mu = entry["edge"]
                var = sum((m - mu) ** 2 for m in means) / (len(means) - 1)
                entry["se"] = math.sqrt(var / len(means))
                entry["level"] = trust_level(len(done), mu, entry["se"])
        if rows:
            out[kind] = entry
    return out


def lines(db) -> list[str]:
    """The scorecard in plain words, or nothing if no call has been logged."""
    card = scorecard(db)
    if not card:
        return []
    out = [f"LIVE SCORECARD  Every call on this sheet is logged and graded against SPY after {HORIZON} trading days."]
    for kind, e in card.items():
        if e["edge"] is None:
            out.append(f"  {LABEL[kind]}: {e['waiting']} waiting to mature.")
            continue
        verdict = {"earned": "EARNED", "failing": "FAILING"}.get(e["level"], "too early to call")
        if e["graded"] < MIN_N_FAILING and e["level"] == "unproven":
            verdict = "too early to call"
        out.append(f"  {LABEL[kind]}: {e['graded']} graded, {e['edge'] * 100:+.2f}% vs the market "
                   f"per call ({verdict}); {e['waiting']} waiting.")
    return out


def update(engine, sheet, today: date | None = None) -> list[str]:
    """Log today's calls, grade the matured ones, return the scorecard lines.
    Never raises: bookkeeping must not break the sheet."""
    try:
        record(engine.db, sheet, today)
        grade(engine.db, engine.market)
        return lines(engine.db)
    except Exception:
        return []
