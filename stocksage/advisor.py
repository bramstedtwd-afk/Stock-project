"""Routine-facing research API: the brain as a day-trading analyst's aide.

Built for an agentic trading routine that runs every ~90 minutes:

  brief     one research packet per run — market mood, verdicts on the
            tickers the routine cares about, ranked buy candidates under a
            price cap with ATR-based stop/target at 2:1, and for every name
            the brain's memory: the model's own graded record on it, recent
            shocks with tagged reasons, and event-proneness. `--json` emits
            a machine-readable packet for the agent to reason over.

  log-call  record the routine's decision (acted OR passed) into the brain.
            Logged calls are graded like any suggestion once the horizon
            elapses, so the routine's own judgment builds per-ticker
            reliability, trains the weights, and shows up in the profit
            ledger. This closes the loop: every run is smarter because
            previous runs were graded.

If market data is unreachable, entries degrade to brain-known context
(record, shocks) with data="unavailable" instead of failing — the routine
combines them with its own live quotes.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from .db import SOURCE_OWNER, SOURCE_ROUTINE
from .engine import Engine
from .indicators import compute_features
from .learning import weighted_score
from .profit import paper_trades, profit_stats

log = logging.getLogger(__name__)

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")

STOP_ATR_MULT = 2.0     # stop distance: 2x daily ATR
REWARD_TO_RISK = 2.0    # target distance: 2x the stop distance

SIGNAL_GLOSS = {
    "trend_long": "long-term trend vs its ~200-day average",
    "trend_medium": "medium-term trend vs its 50-day average",
    "momentum_20d": "strength of the past month's move",
    "macd": "short-term momentum turning up or down",
    "rsi_reversion": "how stretched it is vs recent prices (snap-back potential)",
    "bollinger_reversion": "position within its recent trading band",
    "volume_confirmation": "whether volume is backing the latest move",
    "range_position": "where it sits in its 52-week range",
}


# Calls graded against the benchmark before the headline states a verdict.
# Below this an "edge" is a coin flip dressed up as evidence — and right
# after benchmark grading is switched on the covered count restarts near
# zero while graded_calls is already in the hundreds, which is exactly when
# a confident sentence would mislead most.
MIN_COVERED_FOR_VERDICT = 10


def market_edge(ledger: dict, buys: list) -> dict:
    """Per-call edge over simply holding SPY, expressed scale-free.

    Reported per call rather than in dollars for the same reason the rest of
    model_stats is: a fixed $1,000 paper stake means nothing next to a
    hundred-dollar account, but "beat the market by 0.8 points a call" holds
    at any size.
    """
    covered = ledger.get("covered_trades") or 0
    stake = buys[0].stake if buys else 0.0
    if not covered or not stake:
        return {"covered_trades": covered, "edge_vs_market_per_call": None,
                "benchmark_return_per_call": None}
    basis = covered * stake
    return {
        "covered_trades": covered,
        "edge_vs_market_per_call": round(ledger["edge_vs_market"] / basis, 4),
        "benchmark_return_per_call": round(ledger["benchmark_pnl"] / basis, 4),
        "own_return_per_call": round(ledger["covered_pnl"] / basis, 4),
    }


def scoreboard_headline(summary: dict, ledger: dict, buys: list) -> str:
    """The one sentence worth putting in front of the owner.

    Two traps this avoids. Beating the market while *losing* money is a real
    outcome — both fell, this fell less — and must never be phrased as a
    gain. And an edge from a handful of calls is noise, so below a minimum
    sample it reports the running figure without claiming a verdict.
    """
    graded = summary.get("evaluated") or 0
    if not graded:
        return ("No calls graded yet — nothing to judge this on. "
                "Check back after a week of daily runs.")
    edge = market_edge(ledger, buys)
    covered = edge["covered_trades"]
    per_call = edge["edge_vs_market_per_call"]
    if not covered or per_call is None:
        return (f"{graded} calls graded, but none measured against the market yet — "
                "no honest read on whether this beats an index fund.")
    if covered < MIN_COVERED_FOR_VERDICT:
        return (f"Too early to judge: {covered} of {graded} graded calls measured "
                f"against the market (running edge {per_call:+.2%} per call). "
                f"{MIN_COVERED_FOR_VERDICT} are needed before that means anything.")
    own, bench = edge["own_return_per_call"], edge["benchmark_return_per_call"]
    made = f"returned {own:+.2%} per call" if own >= 0 else f"lost {abs(own):.2%} per call"
    spy_did = f"returned {bench:+.2%}" if bench >= 0 else f"lost {abs(bench):.2%}"
    verdict = "beating the market by" if per_call >= 0 else "trailing the market by"
    return (f"Across {covered} graded calls these {made}, while SPY over the same "
            f"windows {spy_did} — {verdict} {abs(per_call):.2%} per call.")


def _congress_watch(summary: dict, top: int = 8) -> list[dict]:
    """Top tickers Congress is buying lately — brief-level context."""
    from .congress import notable_buys

    return [
        {
            "ticker": tk,
            "members": summary[tk]["members"],
            "net_buys": summary[tk]["net_buys"],
            "last_date": summary[tk]["last_date"],
        }
        for tk in notable_buys(summary, top=top)
    ]


def _congress_note(summary: dict, ticker: str) -> dict | None:
    info = summary.get(ticker)
    if not info or info.get("net_buys", 0) <= 0:
        return None
    return {
        "members": info["members"],
        "net_buys": info["net_buys"],
        "last_date": info["last_date"],
        "est_amount": info["est_amount"],
    }



def _owner_record(engine: Engine, ticker: str) -> dict:
    """How the OWNER's own trades in this name have graded out.

    Reported beside `model_record`, never merged into it: one answers "does
    the engine read this stock well", the other "do I trade this stock well".
    Blending them produces a number that answers neither.
    """
    from .db import SOURCE_OWNER

    calls, hit_rate, _ = engine.db.ticker_track_record(ticker, source=SOURCE_OWNER)
    return {
        "graded_calls": calls,
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
    }


def _entry_from_suggestion(
    s, engine: Engine, congress: dict | None = None, buying_power: float | None = None
) -> dict:
    price = s.price
    atr_pct = s.risk.get("atr_pct")
    stop = target = None
    if atr_pct and price and price > 0:
        stop_dist = STOP_ATR_MULT * atr_pct
        stop = round(price * (1 - stop_dist), 2)
        target = round(price * (1 + REWARD_TO_RISK * stop_dist), 2)
    # Dollar-based sizing: this is the number to actually act on. A $330
    # stock and a $9 stock are equally buyable at $12 — sizing by whole
    # shares is what makes expensive-but-good names look "unaffordable"
    # on a small account when they're not. Fractional/dollar entries are
    # the normal case here, not a fallback.
    size_hint_dollars = None
    est_shares = None
    if buying_power and buying_power > 0 and s.position_fraction > 0:
        size_hint_dollars = round(s.position_fraction * buying_power, 2)
        if price and price > 0:
            est_shares = round(size_hint_dollars / price, 4)
    ctx = engine._past_context(s.ticker)
    top_signals = sorted(s.signals.items(), key=lambda kv: -abs(kv[1]))[:3]
    earnings_blackout = s.earnings_days is not None and 0 <= s.earnings_days <= 3
    # One unambiguous flag: can the routine act on this name right now?
    if s.action in BUYISH:
        actionable = (
            s.position_fraction > 0
            and not earnings_blackout
            and engine.db.model_suggestions_enabled()
        )
    elif s.action in SELLISH:
        actionable = s.owned_shares > 0  # you can only sell what you hold
    else:
        actionable = False
    return {
        "ticker": s.ticker,
        "verdict": s.action,
        "actionable": actionable,
        "score": s.risk_adjusted_score,
        "price": round(price, 2) if price else None,
        "sector": s.sector,
        "stop": stop,
        "target": target,
        "reward_to_risk": f"{REWARD_TO_RISK:.0f}:1" if stop else None,
        "atr_pct": round(atr_pct, 4) if atr_pct else None,
        "annualized_vol": round(s.risk.get("annualized_vol", float("nan")), 3)
        if s.risk.get("annualized_vol") is not None
        else None,
        "model_record": {
            "graded_calls": ctx.graded_calls,
            "hit_rate": round(ctx.hit_rate, 3) if ctx.hit_rate is not None else None,
        },
        # The owner's own record on this name, kept strictly separate — it is
        # useful context, but it must never be read as the engine's accuracy.
        "owner_record": _owner_record(engine, s.ticker),
        "recent_shock": ctx.recent_event,
        "events_12mo": ctx.events_12mo,
        "earnings_days": s.earnings_days,
        "earnings_blackout": earnings_blackout,
        "congress_buying": _congress_note(congress or {}, s.ticker),
        "size_hint_pct": round(s.position_fraction * 100, 1),
        "size_hint_dollars": size_hint_dollars,
        "est_shares": est_shares,
        "top_signals": [
            {"name": n, "value": round(v, 3), "meaning": SIGNAL_GLOSS.get(n, n)}
            for n, v in top_signals
        ],
        "notes": s.notes,
    }


def _degraded_entry(ticker: str, engine: Engine) -> dict:
    ctx = engine._past_context(ticker)
    return {
        "ticker": ticker,
        "data": "unavailable",
        "verdict": None,
        "actionable": False,  # no live data -> routine must use its own quote
        "model_record": {
            "graded_calls": ctx.graded_calls,
            "hit_rate": round(ctx.hit_rate, 3) if ctx.hit_rate is not None else None,
        },
        # The owner's own record on this name, kept strictly separate — it is
        # useful context, but it must never be read as the engine's accuracy.
        "owner_record": _owner_record(engine, ticker),
        "recent_shock": ctx.recent_event,
        "events_12mo": ctx.events_12mo,
        "notes": ["Price data unreachable — brain context only; use live quotes."],
    }


def build_brief(
    engine: Engine,
    tickers: list[str] | None = None,
    max_price: float | None = None,
    top: int = 5,
    congress_summary: dict | None = None,
    holdings: list[str] | None = None,
    buying_power: float | None = None,
    positions: list[dict] | None = None,
) -> dict:
    """One research packet: verdicts on `tickers`, plus ranked candidates.

    `holdings` (tickers currently held) makes the per-name `actionable` flag
    correct for sells — a SELL on a name you hold is actionable; a sell
    signal on something you don't hold is only an avoid.

    `buying_power`, when known, turns each suggestion's abstract sizing
    fraction into an actual dollar amount (`size_hint_dollars`) and share
    count (`est_shares`) — the number to act on. Without it, only the
    percentage (`size_hint_pct`) is available and the routine has to do
    that math itself against its own account pull.
    """
    # Absorb any repo-carried brain snapshot first (idempotent; merges only
    # add), so a routine running in a fresh environment starts with the
    # accumulated knowledge before grading and scanning.
    from .brain import absorb_snapshot

    snapshot = None
    if str(engine.db.path) != ":memory:":
        try:
            snapshot = absorb_snapshot(db_path=engine.db.path)
        except Exception:  # a bad snapshot file must never block a trading run
            snapshot = {"error": "snapshot unreadable — continuing on local brain"}
    graded_now = engine.evaluate_pending()  # constant grading, every touchpoint

    # Congressional-buying context (light tilt, never a mechanical signal).
    if congress_summary is None:
        try:
            from .congress import CongressData

            congress_summary = CongressData().summary()
        except Exception:
            congress_summary = {}

    portfolio = None
    if holdings:
        from .robinhood import Holding, Portfolio

        portfolio = Portfolio(
            holdings=[Holding(t.upper(), 1.0, 0.0, 0.0, 0.0) for t in holdings],
            buying_power=0.0,
        )

    result = engine.scan(portfolio=portfolio, capture_context=False, record=False)
    by_ticker = {s.ticker: s for s in result.suggestions}

    requested = [t.strip().upper() for t in (tickers or []) if t.strip()]
    missing = [t for t in requested if t not in by_ticker]
    if missing:
        extra = engine.scan(
            tickers=missing, portfolio=portfolio, capture_context=False, record=False
        )
        by_ticker.update({s.ticker: s for s in extra.suggestions})

    focus = []
    for t in requested:
        s = by_ticker.get(t)
        focus.append(
            _entry_from_suggestion(s, engine, congress_summary, buying_power)
            if s
            else _degraded_entry(t, engine)
        )

    candidates = [
        s
        for s in result.suggestions
        if s.action in BUYISH
        and s.position_fraction > 0  # excludes earnings-blackout / zero-size names
        and (max_price is None or (s.price and s.price <= max_price))
    ]
    avoid = [s for s in result.suggestions if s.action in SELLISH][:top]

    summary = engine.db.performance_summary()
    buys, avoided = paper_trades(engine.db.evaluated_suggestions())
    ledger = profit_stats(buys, avoided)

    packet_actions = None
    if positions is not None and buying_power is not None:
        try:
            packet_actions = plan_for_account(
                engine, list(by_ticker.values()), positions, buying_power
            ).to_dict()
        except Exception as exc:  # a planning bug must never cost the whole brief
            log.warning("could not build today's actions: %s", exc)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actions": packet_actions,
        "snapshot_absorbed": snapshot,
        "graded_this_call": graded_now,
        "market_mood": engine.sector_trends(),
        "congress_watch": _congress_watch(congress_summary),
        "focus": focus,
        "candidates": [
            _entry_from_suggestion(s, engine, congress_summary, buying_power)
            for s in candidates[:top]
        ],
        "avoid": [
            {"ticker": s.ticker, "verdict": s.action, "score": s.risk_adjusted_score}
            for s in avoid
        ],
        "headline": scoreboard_headline(summary, ledger, buys),
        # When off, the engine still scans, grades and learns, but nothing it
        # picks is offered as actionable — the routine trades on its own live
        # analysis and treats these as research. Buy-side `actionable` is
        # forced false to match, so a single flag cannot disagree with the
        # cards beneath it.
        "model_suggestions_enabled": engine.db.model_suggestions_enabled(),
        "model_stats": {
            "graded_calls": summary["evaluated"],
            "hit_rate": round(summary["hit_rate"], 3)
            if summary["hit_rate"] is not None
            else None,
            "paper_profit_factor": round(ledger["profit_factor"], 2)
            if ledger["profit_factor"] is not None
            else None,
            # Scale-free: avg return per call, not a dollar figure (a fixed-
            # stake paper P&L would be meaningless next to a small account).
            "avg_return_per_call": round(ledger["return_per_trade"], 4)
            if ledger.get("return_per_trade") is not None
            else None,
            **market_edge(ledger, buys),
        },
        "scan_errors": len(result.errors),
    }


def ingest_fills(engine: Engine, orders: list[dict], horizon_days: int = 5) -> dict:
    """Turn actual executed orders into graded, weight-training brain calls.

    This is how the routine's real behavior compounds into memory: every
    filled buy/sell (from the Robinhood mirror) becomes a recorded call,
    backdated to when it actually executed, with the technical signals as
    they stood that day (no look-ahead). The normal grading loop then scores
    it against what price did next — so per-ticker reliability, the learned
    weights, and the profit ledger all learn from what you truly did, not
    from self-reported intentions. Idempotent: each order is ingested once.
    """
    import pandas as pd

    recorded = 0
    for o in orders:
        order_id = str(o.get("order_id") or "")
        if not order_id or engine.db.fill_ingested(order_id):
            continue
        side = (o.get("side") or "").lower()
        action = "BUY" if side == "buy" else "SELL" if side == "sell" else None
        price = float(o.get("price") or 0)
        ticker = (o.get("ticker") or "").upper()
        executed_at = str(o.get("executed_at") or "").replace("Z", "+00:00")
        if action is None or price <= 0 or not ticker or not executed_at:
            continue

        existing = engine.db.owner_call_for_fill(ticker, action, executed_at, price)
        if existing is not None:
            # Already a call — the ledger just never learned about it (see
            # brain.merge_brains). Record the link so this is settled for good,
            # but do not create a second call or count it as new.
            engine.db.mark_fill_ingested(order_id, existing)
            continue

        # Signals as they stood on the fill date (history sliced, no peeking).
        signals: dict[str, float] = {}
        df = engine.market.history(ticker)
        if df is not None and not df.empty:
            try:
                as_of = pd.Timestamp(executed_at).tz_localize(None).normalize()
                sliced = df.loc[:as_of]
                feats = compute_features(sliced) if len(sliced) else None
                if feats:
                    signals = feats
            except (ValueError, TypeError, KeyError):
                signals = {}

        direction = 1.0 if action == "BUY" else -1.0
        sid = engine.db.record_suggestion(
            ticker, action, round(direction * 0.35, 4), price, signals,
            horizon_days, created_at=executed_at, source=SOURCE_OWNER,
        )
        engine.db.mark_fill_ingested(order_id, sid)
        recorded += 1
    return {"fills_ingested": recorded}


def catch_up_from_drive(engine: Engine) -> dict | None:
    """Pull whatever the brain last learned anywhere — another device, an
    earlier stateless cloud run — from Drive and merge it in.

    Merging only ever adds, so this is safe and idempotent. Call it before
    grading or scanning so a machine with no local history at all (e.g. a
    fresh cloud session with a brand-new empty database) starts smart
    instead of from scratch. Returns merge stats, or None if Drive isn't
    configured or nothing has been published there yet. Never raises — a
    Drive hiccup must not block a run.
    """
    from .brain import STATE_DIR, import_brain
    from .drive_api import DriveAuthExpired, DriveNotConfigured, pull_brain_snapshot

    try:
        pulled = pull_brain_snapshot(STATE_DIR / "drive-pull.db")
    except DriveAuthExpired as exc:
        # Sync *was* working and has stopped. Silence here means the brain
        # quietly stops travelling between devices, so say it in full.
        log.warning("%s", exc)
        return None
    except DriveNotConfigured:
        return None  # never set up on this machine — normal, stay quiet
    except Exception as exc:
        log.warning("Drive catch-up failed: %s", exc)
        return None
    if pulled is None:
        return None
    return import_brain(pulled, db_path=engine.db.path)


AGENTIC_PLACEHOLDER = "{{AGENTIC_ACCOUNT}}"


def playbook_for_publishing() -> str:
    """The playbook with the owner's account number filled in.

    The repository copy carries a placeholder, not an account number: it is
    a public repo, and a brokerage account number is the owner's, not the
    project's. The real value lives in .env and is substituted only here, on
    the way to the owner's own private Drive folder.
    """
    from .brain import ROUTINE_PATH

    if not ROUTINE_PATH.exists():
        return ""
    text = ROUTINE_PATH.read_text(encoding="utf-8")
    account = (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    if account:
        text = text.replace(AGENTIC_PLACEHOLDER, account)
    return text


def _agentic_portfolio(client):
    """The portfolio of the account the routine actually trades.

    client.portfolio() returns Robinhood's DEFAULT account, which is not the
    one the routine acts on. That mismatch is not cosmetic: the brief's
    size_hint_dollars are a percentage of whatever buying power it is handed,
    so a personal account's balance produces dollar sizes many times what the
    agentic account can fund — on 2026-08-10 the brief suggested $110-$147
    entries against an account holding under a dollar.

    STOCKSAGE_AGENTIC_ACCOUNT names the right one. Falling back to the
    default when it is unset keeps single-account setups working, and the
    playbook independently tells the routine to size from its own live pull —
    but the brief should not be misleading in the first place.
    """
    account = (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    if account:
        try:
            scoped = client.portfolio_for(account)
        except Exception as exc:
            log.warning("could not read account %s: %s", account[-4:], exc)
            scoped = None
        if scoped is not None:
            return scoped
        log.warning(
            "STOCKSAGE_AGENTIC_ACCOUNT is set but that account returned "
            "nothing; falling back to the default account's portfolio"
        )
    return client.portfolio()



def plan_for_account(engine: Engine, suggestions: list, positions: list[dict],
                     cash: float, profile: str = "active", tax_note: str | None = None,
                     entries: str | None = None):
    """Today's ranked actions for the account the routine trades.

    One function used by the CLI, the dashboard and the published brief, so
    all three show the owner the same thing. Everything it needs comes from
    the brain: when each name was bought (the broker mirror), how much the
    model's edge can be believed, and the owner's on/off switch.
    """
    from .actions import build_plan, entry_dates_from_orders, model_trust

    # The daily scan covers the universe, the watchlist and the DEFAULT
    # account's holdings. A name held only in the account the routine trades
    # may be in none of those, and without a scan it has no ATR, so no stop,
    # so a stop breach would pass silently. Scan any held name that is missing.
    have = {s.ticker for s in suggestions}
    missing = [p["ticker"] for p in positions if p["ticker"] not in have]
    if missing:
        suggestions = list(suggestions) + engine.scan(
            tickers=missing, capture_context=False, record=False
        ).suggestions

    held = {p["ticker"] for p in positions}
    entered = entry_dates_from_orders(
        [dict(r) for r in engine.db.rh_orders()], held
    )
    reliability = {}
    for s in suggestions:
        graded, hit, _ = engine.db.ticker_track_record(s.ticker)
        reliability[s.ticker] = (graded, hit)
    graded = engine.db.evaluated_suggestions()
    return build_plan(
        positions, cash, suggestions, entered,
        # BUYING is gated on the buy side's record alone; the sell side is
        # reported beside it but cannot unlock a purchase.
        model_trust(graded, "buy"),
        sell_trust=model_trust(graded, "sell"),
        suggestions_enabled=engine.db.model_suggestions_enabled(),
        reliability=reliability,
        profile=profile,
        tax_note=tax_note,
        entries=entries or entries_policy(),
        backtest_level=engine.db.get_meta("backtest_level"),
    )


def entries_policy() -> str:
    """How model-driven BUYs appear: proven (default) | always | never."""
    chosen = (os.environ.get("STOCKSAGE_ENTRIES") or "").strip().lower()
    return chosen if chosen in ("proven", "always", "never") else "proven"


ROLE_LABELS = {"agentic": "Agentic", "personal": "Personal", "roth": "Roth IRA"}

# Which rules each kind of account gets. The owner's answers: the agentic
# account is day-traded (stops, targets, 10-day clock); the personal brokerage
# is run by hand with stop-loss exits and the size cap but no clock; the Roth
# is long-term.
# Override per role with STOCKSAGE_PROFILE_PERSONAL=core, etc.
PROFILE_DEFAULTS = {"agentic": "active", "personal": "stops", "roth": "core"}


def profile_for(role: str) -> str:
    chosen = (os.environ.get(f"STOCKSAGE_PROFILE_{role.upper()}") or "").strip().lower()
    return chosen if chosen in ("active", "stops", "core") else PROFILE_DEFAULTS[role]
CONCENTRATION_NOTE_AT = 0.15


def plans_for_accounts(engine: Engine, client, suggestions: list | None = None,
                       entries_override: str | None = None) -> dict:
    """A plan for every account under the login, each by its own rules.

    Each account gets the rules that fit how it is used (see PROFILE_DEFAULTS):
    "active" has stops, a 10-day exit and a tight size cap; "core" is a
    long-term account where those would be wrong, so only the concentration
    cap and the model's opinions apply. Tax context differs too
    (a Roth sale is tax-free; a taxable one realises a gain or loss).

    Also reports any single stock that is a large share of EVERYTHING owned,
    across accounts — the exposure no single account's plan can see.
    """
    from .actions import INDEX_FUNDS

    if suggestions is None:
        suggestions = engine.scan(capture_context=False, record=False).suggestions

    agentic = (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    try:
        accounts = list(client.accounts())
    except Exception as exc:
        log.warning("could not list accounts: %s", exc)
        accounts = []

    entries: list[tuple[str, str, object, str]] = []   # (role, label, portfolio, kind)
    if not accounts:
        pf = _agentic_portfolio(client)
        if pf is not None:
            entries.append(("agentic", "Account", pf, ""))
    else:
        default_number = None
        if not agentic:
            try:
                default = client.portfolio()
                default_number = default.account_number if default else None
            except Exception:
                default_number = None
        for acct in accounts:
            try:
                pf = client.portfolio_for(acct.number)
            except Exception as exc:
                log.warning("could not read account %s: %s", acct.number[-4:], exc)
                continue
            if pf is None:
                continue
            is_agentic = acct.number == (agentic or default_number)
            role = "agentic" if is_agentic else ("roth" if (acct.kind or "").lower() == "roth" else "personal")
            entries.append((role, acct.label, pf, acct.kind or ""))
        entries.sort(key=lambda e: (e[0] != "agentic", e[0]))

    # Scan every held name the daily scan missed ONCE, up front, so each
    # account's plan sees it and the caller gets one suggestion per ticker.
    have = {s.ticker for s in suggestions}
    held_all = {h.ticker for _, _, pf, _ in entries for h in pf.holdings}
    missing = sorted(held_all - have)
    if missing:
        suggestions = list(suggestions) + engine.scan(
            tickers=missing, capture_context=False, record=False
        ).suggestions

    out, combined, total = [], {}, 0.0
    for role, label, pf, kind in entries:
        positions = [
            {"ticker": h.ticker, "shares": h.shares, "avg_cost": h.avg_buy_price,
             "price": h.current_price, "equity": h.equity}
            for h in pf.holdings
        ]
        tax = ("Roth IRA: selling is tax-free." if role == "roth"
               else "Taxable account: selling realises a gain or loss.")
        # Sized from settled CASH, never buying power: on a margin account the
        # latter includes money that can be borrowed.
        plan = plan_for_account(
            engine, suggestions, positions, pf.sizing_cash,
            profile=profile_for(role), tax_note=tax, entries=entries_override,
        )
        if "margin" in kind.lower():
            plan.notes.append(
                "Margin account: every amount here is sized from your cash only, "
                "never from margin."
            )
        out.append({"role": role, "title": ROLE_LABELS[role], "label": label,
                    "manual": role != "agentic", "plan": plan})
        total += pf.sizing_cash + sum(p["equity"] for p in positions)
        for p in positions:
            combined[p["ticker"]] = combined.get(p["ticker"], 0.0) + p["equity"]

    notes = []
    if total > 0:
        for ticker, value in sorted(combined.items(), key=lambda kv: -kv[1]):
            if ticker not in INDEX_FUNDS and value / total >= CONCENTRATION_NOTE_AT and len(notes) < 3:
                notes.append(
                    f"{ticker} is {value / total:.0%} of everything you own across all your accounts."
                )
    return {"accounts": out, "overall_notes": notes,
            "suggestions": {s.ticker: s for s in suggestions}}


def desktop_sync_cycle(engine: Engine, client=None, broker: bool = True) -> dict:
    """The full desktop-side heartbeat that keeps the brain current.

    Pull the live Robinhood order/dividend history, turn any new fills into
    graded calls, then grade everything matured. Run this before every
    publish (and it's safe to run anytime) so the brain captures every real
    trade automatically — "grading every single run". Robinhood being absent
    degrades to just grading what's already recorded.

    broker=False skips the account entirely and only grades what is already
    recorded — for the extra scheduled publishes of the day, so the app opens
    one broker session instead of five (see stocksage/security.py).
    """
    from .robinhood import RobinhoodClient

    client = client or RobinhoodClient()
    stats = {
        "synced": None, "fills_ingested": 0, "graded": 0,
        "holdings": [], "buying_power": None,
    }
    if not broker:
        # Publishing several times a day means several broker sessions a day,
        # each a potential Robinhood sign-in alert. Letting the extra runs
        # publish without touching the account keeps the alert pattern
        # legible: one expected sign-in, and anything else is worth a look.
        stats["broker_skipped"] = True
        stats["graded"] = engine.evaluate_pending()
        return stats
    try:
        sync = client.sync_history(engine.db)
        if sync is not None:
            stats["synced"] = sync
            orders = [dict(r) for r in engine.db.rh_orders()]
            stats["fills_ingested"] = ingest_fills(engine, orders)["fills_ingested"]
        portfolio = _agentic_portfolio(client)
        if portfolio is not None:
            stats["holdings"] = [h.ticker for h in portfolio.holdings]
            stats["positions"] = [
                {"ticker": h.ticker, "shares": h.shares, "avg_cost": h.avg_buy_price,
                 "price": h.current_price, "equity": h.equity}
                for h in portfolio.holdings
            ]
            stats["buying_power"] = portfolio.sizing_cash
            stats["account_number"] = portfolio.account_number
    except Exception as exc:  # never let a broker hiccup block grading/publish
        stats["sync_error"] = str(exc)
    stats["graded"] = engine.evaluate_pending()
    return stats


def publish_brief(
    engine: Engine,
    folder: str,
    tickers: list[str] | None = None,
    max_price: float | None = None,
    top: int = 8,
    include_playbook: bool = True,
    include_snapshot: bool = True,
    holdings: list[str] | None = None,
    buying_power: float | None = None,
    positions: list[dict] | None = None,
) -> dict:
    """Write a fresh research packet into a folder for the routine to read.

    Point `folder` at a directory that Google Drive for Desktop (or any
    cloud client) syncs. The routine reads the freshest "StockSage Brief"
    file from Drive — no Python needed on its side. Idempotent per day: the
    dated filename means one file per day, overwritten on re-publish.
    """
    import json
    from datetime import date
    from pathlib import Path as _Path

    from .brain import ROUTINE_PATH, export_brain

    out_dir = _Path(folder).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    focus = tickers if tickers is not None else engine.db.watchlist()
    packet = build_brief(
        engine, tickers=focus, max_price=max_price, top=top, holdings=holdings,
        buying_power=buying_power, positions=positions,
    )

    brief_path = out_dir / f"StockSage Brief - {date.today().isoformat()}.json"
    brief_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    written = [str(brief_path)]

    if include_playbook and ROUTINE_PATH.exists():
        dest = out_dir / "StockSage Routine Playbook.md"
        dest.write_text(playbook_for_publishing(), encoding="utf-8")
        written.append(str(dest))
    if include_snapshot:
        # Export straight to the Drive folder — NOT via write_snapshot(),
        # which targets the repo-tracked brain/brain-snapshot.db. That path
        # is for the deliberate, user-invoked `brain snapshot` git-sharing
        # command; writing there on every automated publish run would dirty
        # the repo constantly and permanently block `update`.
        dest = out_dir / "brain-snapshot.db"
        export_brain(dest, db_path=engine.db.path)
        written.append(str(dest))

    return {"folder": str(out_dir), "written": written, "candidates": len(packet["candidates"])}


def publish_brief_via_api(
    engine: Engine,
    tickers: list[str] | None = None,
    max_price: float | None = None,
    top: int = 8,
    holdings: list[str] | None = None,
    buying_power: float | None = None,
    positions: list[dict] | None = None,
) -> dict:
    """Push research straight to Google Drive via the API — no desktop
    sync client, no admin rights, nothing installed on the machine beyond
    two Python packages already inside the virtual environment. Use this
    when Google Drive for Desktop can't be installed (locked-down/managed
    machine, no admin rights). See stocksage/drive_api.py for one-time
    setup. Files are updated in place under fixed names, so the routine
    never has to guess which of several dated files is current.
    """
    import json
    from pathlib import Path as _Path

    from .brain import STATE_DIR, export_brain
    from .drive_api import publish_via_api

    focus = tickers if tickers is not None else engine.db.watchlist()
    packet = build_brief(
        engine, tickers=focus, max_price=max_price, top=top, holdings=holdings,
        buying_power=buying_power, positions=positions,
    )
    playbook_text = playbook_for_publishing()
    # Same reasoning as publish_brief: export outside the repo tree so an
    # automated run never leaves the git working directory dirty.
    snap = export_brain(
        _Path(STATE_DIR) / "publish-snapshot.db", db_path=engine.db.path
    )
    result = publish_via_api(json.dumps(packet, indent=2), playbook_text, snap)
    result["candidates"] = len(packet["candidates"])
    result["actions"] = packet.get("actions")
    return result


def log_call(
    engine: Engine,
    ticker: str,
    action: str,
    price: float | None = None,
    note: str | None = None,
    horizon_days: int = 5,
) -> int:
    """Record a routine decision into the brain so it gets graded.

    Direction for grading comes from the action; signals (when data is
    reachable) let the weight-learning loop learn from this call too.
    Returns the suggestion id.
    """
    ticker = ticker.strip().upper()
    action = action.strip().upper()
    if action not in BUYISH + SELLISH:
        raise ValueError(f"action must be one of {BUYISH + SELLISH}, got {action!r}")

    df = engine.market.history(ticker)
    feats = compute_features(df) if df is not None else None
    if price is None:
        if df is None or df.empty:
            raise ValueError(
                f"no market data for {ticker} — pass the executed/quoted price explicitly"
            )
        price = float(df["Close"].iloc[-1])

    direction = 1.0 if action in BUYISH else -1.0
    magnitude = 0.5 if action.startswith("STRONG") else 0.3
    score = direction * magnitude
    if feats:
        model_view = weighted_score(feats, engine.weights_for(ticker, engine.db.load_weights()))
        # Keep the routine's direction; nudge magnitude by model agreement.
        score = direction * max(0.2, min(0.6, magnitude + 0.2 * direction * model_view))

    sid = engine.db.record_suggestion(
        ticker, action, round(score, 4), price, feats or {}, horizon_days,
        source=SOURCE_ROUTINE,
    )
    if note:
        engine.db.log_learning(sid, {"routine_note": note, "logged_at_price": price})
    return sid
