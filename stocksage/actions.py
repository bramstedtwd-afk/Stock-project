"""What to do today: the rules, applied, in plain language.

StockSage already knew things; the owner still had to work out what to *do*
about them. This module turns the scan and the account into a short ranked
list of actions — sell this, trim that, enter this one with this much — using
the SAME rules the trading routine's playbook states, so the app and the
routine cannot disagree about what a stop breach or a full position is.

It never places anything. An action is a proposal; the order itself stays a
human "confirm" in the routine. That gate is not a limitation to be
automated away.

Two kinds of action rest on different foundations, and the plan keeps them
apart on purpose:

  * EXITS (stop hit, target hit, 10 days elapsed, over-concentrated) are
    risk rules. They need no belief that the model has skill, so they fire
    regardless of how the model is performing.
  * ENTRIES depend on the model being right. They are always shown, but each
    carries the model's measured track record against the market beside it,
    and a model that is measurably trailing the market is labelled low
    confidence rather than quietly presented as a green light.

Pure functions, no I/O: the whole thing is testable offline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

# --- the rules (mirrors ROUTINE.md; change both together) -------------------
ENTRY_FLOOR = 15.0          # never open a position smaller than this
MIN_ORDER = 1.0             # Robinhood rejects fractional orders under $1
MAX_POSITION_FRACTION = 0.25  # no single name over 25% of total equity
TIME_EXIT_DAYS = 10         # trading days before a position must justify itself
TARGET_POSITIONS = 4        # 3-4 names; four is the most the 25% cap allows
NEAR_STOP = 0.015           # within 1.5% of the stop is worth a warning
STOP_ATR_MULT = 2.0
EARNINGS_BLACKOUT_DAYS = 5
MAX_WATCH = 3
CORE_TARGET_POSITIONS = 12   # long-term accounts hold more names than the active one

# Broad index funds are the thing you hold INSTEAD of picking, so the
# single-name concentration cap does not apply to them (core accounts only).
INDEX_FUNDS = frozenset({
    "VTI", "VOO", "SPY", "IVV", "QQQ", "VT", "VXUS", "SCHB", "ITOT", "SPLG",
    "VUG", "VTV", "SCHX", "SCHG", "FXAIX", "VFIAX", "FSKAX", "VTSAX",
})

# --- how much the model has earned ------------------------------------------
MIN_N_FAILING = 30    # enough measured calls to say it is trailing the market
MIN_N_EARNED = 100    # enough to say it is beating it
Z_FAILING = 1.64
Z_EARNED = 2.33       # stricter: trust is slower to grant than to withdraw

BUYISH = ("BUY", "STRONG BUY")
SELLISH = ("SELL", "STRONG SELL")

# Urgency: lower is sooner.
URGENT, SOON, ROUTINE, FYI = 1, 2, 3, 4


@dataclass
class Action:
    kind: str            # EXIT_STOP | EXIT_TARGET | EXIT_TIME | TRIM | EXIT_SIGNAL
                         # | ENTER | WATCH | HEADS_UP | HOLD_PAST_TIME
    ticker: str
    side: str            # SELL | BUY | WATCH
    urgency: int
    headline: str
    why: str
    dollars: float | None = None
    shares: float | None = None
    stop: float | None = None
    target: float | None = None
    confidence: str | None = None   # high | medium | low (entries only)
    needs_sale_first: bool = False  # funded by a sale that must settle first

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    quiet: list[str] = field(default_factory=list)   # held, nothing to do
    notes: list[str] = field(default_factory=list)   # plain-language context
    trust: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            actions=[Action(**a) for a in data.get("actions", [])],
            quiet=list(data.get("no_action_needed", [])),
            notes=list(data.get("notes", [])),
            trust=dict(data.get("model_trust", {})),
        )

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "no_action_needed": self.quiet,
            "notes": self.notes,
            "model_trust": self.trust,
        }


# --- model trust -------------------------------------------------------------


def model_trust(rows) -> dict:
    """How much the model's measured edge over the market can be believed.

    Uses only calls that were actually measured against SPY — a call with no
    benchmark says nothing about edge. The standard error here treats calls
    as independent, which they are not (they cluster on the same days and
    sectors), so it is optimistic; that is why "earned" demands both a large
    sample and a stricter threshold than "failing" does.
    """
    edges = [
        r["realized_return"] - r["benchmark_return"]
        for r in rows
        if r["benchmark_return"] is not None and r["realized_return"] is not None
    ]
    n = len(edges)
    if n < 2:
        return {"level": "unproven", "n": n, "edge": None, "se": None}
    mean = sum(edges) / n
    var = sum((e - mean) ** 2 for e in edges) / (n - 1)
    se = math.sqrt(var / n)
    return {"level": trust_level(n, mean, se), "n": n, "edge": mean, "se": se}


def trust_level(n: int, mean: float, se: float) -> str:
    """failing / unproven / earned, from a mean edge and its standard error.

    The single definition of "proof" in StockSage. The live gate and the
    backtest both call it, so what counts as evidence cannot quietly differ
    between the place that tests the model and the place that acts on it.
    """
    if n >= MIN_N_EARNED and mean - Z_EARNED * se > 0:
        return "earned"
    if n >= MIN_N_FAILING and mean + Z_FAILING * se < 0:
        return "failing"
    return "unproven"


def trust_sentence(trust: dict) -> str:
    n, edge, level = trust.get("n", 0), trust.get("edge"), trust.get("level")
    if edge is None or n < MIN_N_FAILING:
        return (
            f"Model track record: too early to judge — only {n} calls measured "
            f"against the market."
        )
    pct = f"{edge * 100:+.2f}%"
    if level == "failing":
        return (
            f"Model track record: TRAILING the market by {pct} per call over {n} "
            f"calls, and that gap is bigger than chance. Treat entries as ideas, "
            f"not signals."
        )
    if level == "earned":
        return (
            f"Model track record: beating the market by {pct} per call over {n} "
            f"calls, beyond what chance explains."
        )
    return (
        f"Model track record: {pct} per call versus the market over {n} calls — "
        f"not yet distinguishable from luck either way."
    )


# --- helpers -----------------------------------------------------------------


def trading_days_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end` (holidays ignored)."""
    if end <= start:
        return 0
    days, day = 0, start
    from datetime import timedelta

    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _atr_pct(s) -> float | None:
    risk = getattr(s, "risk", None) or {}
    value = risk.get("atr_pct")
    return float(value) if value else None


def _confidence(s, trust_level: str, reliability: tuple[int, float | None]) -> str:
    """Conviction x track record on this name x the model's track record."""
    graded, hit = reliability
    points = 0
    if abs(getattr(s, "risk_adjusted_score", 0.0)) >= 0.5:
        points += 1
    if graded >= 8 and hit is not None and hit >= 0.6:
        points += 1
    if trust_level == "failing":
        return "low"
    if points == 2 and trust_level == "earned":
        return "high"
    if points >= 1:
        return "medium"
    return "low"


# --- the plan ----------------------------------------------------------------


def build_plan(
    positions: list[dict],
    cash: float,
    suggestions: list,
    entry_dates: dict[str, date],
    trust: dict,
    today: date | None = None,
    suggestions_enabled: bool = True,
    reliability: dict[str, tuple[int, float | None]] | None = None,
    profile: str = "active",
    tax_note: str | None = None,
) -> Plan:
    """Rank what to do, given the account and the scan.

    profile "active" is the small account the routine day-trades: stops,
    targets and the 10-day exit all apply. "core" is a long-term account you
    manage by hand: those would be wrong there (a 10-day exit on a Roth index
    fund is nonsense), so only the concentration cap and the model's own
    opinions are applied.

    positions: [{ticker, shares, avg_cost, price, equity}] for the account the
    routine trades. cash: settled buying power there. entry_dates: when each
    held name was last bought (for the time exit).
    """
    today = today or date.today()
    active = profile == "active"
    reliability = reliability or {}
    by_ticker = {s.ticker: s for s in suggestions}
    plan = Plan(trust=trust)
    plan.notes.append(trust_sentence(trust))

    total_equity = cash + sum(p["equity"] for p in positions)
    exited: set[str] = set()
    freed = 0.0

    for p in positions:
        t = p["ticker"]
        price, cost, shares, equity = p["price"], p["avg_cost"], p["shares"], p["equity"]
        s = by_ticker.get(t)
        atr = _atr_pct(s) if s else None
        stop = round(cost * (1 - STOP_ATR_MULT * atr), 2) if (atr and active) else None
        target = round(cost + 2 * (cost - stop), 2) if stop else None
        entered = entry_dates.get(t)
        age = trading_days_between(entered, today) if (entered and active) else None
        pnl = (price / cost - 1) if cost else 0.0
        acted = False

        if stop is not None and price <= stop:
            plan.actions.append(Action(
                "EXIT_STOP", t, "SELL", URGENT,
                f"SELL all of {t} ({_money(equity)}) — it hit its stop",
                f"Price {_money(price)} is at or below the stop at {_money(stop)}, "
                f"set two normal daily swings under your {_money(cost)} entry. "
                f"Falling through it means the move against you is bigger than "
                f"this stock's usual noise.",
                dollars=round(equity, 2), shares=round(shares, 6), stop=stop, target=target,
            ))
            exited.add(t)
            freed += equity
            acted = True
        elif target is not None and price >= target:
            plan.actions.append(Action(
                "EXIT_TARGET", t, "SELL", SOON,
                f"SELL all of {t} ({_money(equity)}) — it reached its target",
                f"Price {_money(price)} is at or above the target {_money(target)} "
                f"(+{pnl * 100:.1f}% on your {_money(cost)} entry). Banking it frees "
                f"the money for the next setup.",
                dollars=round(equity, 2), shares=round(shares, 6), stop=stop, target=target,
            ))
            exited.add(t)
            freed += equity
            acted = True
        elif age is not None and age >= TIME_EXIT_DAYS:
            trending = s is not None and s.action in BUYISH and price > cost
            if trending:
                plan.actions.append(Action(
                    "HOLD_PAST_TIME", t, "WATCH", FYI,
                    f"{t} is past {TIME_EXIT_DAYS} trading days but still working — hold",
                    f"{age} trading days in, +{pnl * 100:.1f}%, and the model still rates "
                    f"it {s.action}. Allowed to run, re-checked every day.",
                    stop=stop, target=target,
                ))
            else:
                plan.actions.append(Action(
                    "EXIT_TIME", t, "SELL", SOON,
                    f"SELL all of {t} ({_money(equity)}) — it has had its {TIME_EXIT_DAYS} days",
                    f"{age} trading days held, {pnl * 100:+.1f}%, and it has neither hit its "
                    f"target nor is clearly still trending. If a thesis has not played out "
                    f"in twice the model's own grading window, the edge has gone stale; "
                    f"this frees the money for a better setup.",
                    dollars=round(equity, 2), shares=round(shares, 6), stop=stop, target=target,
                ))
                exited.add(t)
                freed += equity
            acted = True

        capped = not (profile == "core" and t in INDEX_FUNDS)
        if t not in exited and total_equity > 0 and capped:
            over = equity - MAX_POSITION_FRACTION * total_equity
            if over >= MIN_ORDER:
                plan.actions.append(Action(
                    "TRIM", t, "SELL", ROUTINE,
                    f"TRIM {t} by about {_money(over)} — it is over {MAX_POSITION_FRACTION:.0%} of the account",
                    f"{t} is {equity / total_equity:.0%} of everything you hold. One bad "
                    f"earnings gap in a position this size can undo weeks of gains; "
                    f"the cap exists so no single name can.",
                    dollars=round(over, 2), shares=round(over / price, 6) if price else None,
                    stop=stop, target=target,
                ))
                freed += over
                acted = True

        if t not in exited and s is not None and s.action in SELLISH:
            plan.actions.append(Action(
                "EXIT_SIGNAL", t, "SELL", FYI,
                f"Consider selling {t} — the model now rates it {s.action}",
                (s.why or "The signals have turned against it.")
                + " This one rests on the model's skill, not a risk rule.",
                dollars=round(equity, 2), shares=round(shares, 6),
            ))
            acted = True

        if (t not in exited and stop is not None and price > stop
                and (price - stop) / price <= NEAR_STOP):
            plan.actions.append(Action(
                "HEADS_UP", t, "WATCH", ROUTINE,
                f"Heads up: {t} is within {(price - stop) / price * 100:.1f}% of its stop",
                f"Price {_money(price)} vs stop {_money(stop)}. Nothing to do yet; if it "
                f"touches the stop the next plan will say sell.",
                stop=stop, target=target,
            ))
            acted = True

        if not acted:
            plan.quiet.append(t)

    # ---- entries -------------------------------------------------------------
    if not suggestions_enabled:
        plan.notes.append(
            "Model suggestions are switched off, so no new entries are proposed."
        )
    else:
        kept = len(positions) - len(exited)
        slots = max(0, (TARGET_POSITIONS if active else CORE_TARGET_POSITIONS) - kept)
        held = {p["ticker"] for p in positions}
        ranked = sorted(
            (
                s for s in suggestions
                if s.action in BUYISH
                and s.ticker not in held
                and (s.earnings_days is None or s.earnings_days > EARNINGS_BLACKOUT_DAYS)
            ),
            key=lambda s: s.risk_adjusted_score, reverse=True,
        )
        budget = cash + freed
        cap = MAX_POSITION_FRACTION * total_equity
        picked = ranked[:slots] if slots else []
        failing = trust.get("level") == "failing"
        watch = 0
        for i, s in enumerate(picked):
            size = min(cap, budget / (len(picked) - i)) if budget > 0 else 0.0
            stop = s.stop_price
            target = round(s.price + 2 * (s.price - stop), 2) if stop else None
            conf = _confidence(s, trust.get("level", "unproven"),
                               reliability.get(s.ticker, (0, None)))
            if failing:
                # The gate. A model measurably trailing the market has not
                # earned a numbered BUY, however good the setup looks; it is
                # shown as an idea, outside the list of things to do.
                if watch < MAX_WATCH:
                    plan.actions.append(Action(
                        "IDEA", s.ticker, "WATCH", FYI,
                        f"Idea: {s.ticker} — the model rates it {s.action}, but it has not earned your trust",
                        (s.why or f"The model rates it {s.action}.")
                        + f" Stop {_money(stop) if stop else 'n/a'}, target "
                        f"{_money(target) if target else 'n/a'} if you ever took it.",
                        stop=stop, target=target, confidence="low",
                    ))
                    watch += 1
            elif size >= ENTRY_FLOOR:
                needs_sale = size > cash
                plan.actions.append(Action(
                    "ENTER", s.ticker, "BUY", ROUTINE,
                    f"BUY about {_money(size)} of {s.ticker}"
                    + (" — once the sale settles" if needs_sale else ""),
                    (s.why or f"The model rates it {s.action}.")
                    + f" Stop {_money(stop) if stop else 'n/a'}, target "
                    f"{_money(target) if target else 'n/a'} (2:1).",
                    dollars=round(size, 2),
                    shares=round(size / s.price, 4) if s.price else None,
                    stop=stop, target=target, confidence=conf,
                    needs_sale_first=needs_sale,
                ))
                budget -= size
            elif watch < MAX_WATCH:
                plan.actions.append(Action(
                    "WATCH", s.ticker, "WATCH", FYI,
                    f"Watching {s.ticker} — a {s.action} the account cannot fund yet",
                    f"Cash ({_money(cash)}) is below the {_money(ENTRY_FLOOR)} minimum "
                    f"entry, so nothing is proposed. Deploying scraps would just "
                    f"fragment the account into positions too small to matter.",
                    stop=stop, target=target, confidence=conf,
                ))
                watch += 1
        if failing and picked:
            plan.notes.append(
                "Because the model is trailing the market, its buy ideas are listed "
                "as ideas, not recommendations. Over the same calls the plain market "
                "did better than the model's picks, so cash can reasonably wait or "
                "sit in a broad index fund — your call, not advice."
            )
        if slots == 0 and ranked:
            plan.notes.append(
                f"All {TARGET_POSITIONS} position slots are in use, so new ideas wait "
                f"until something is sold."
            )

    if tax_note:
        for a in plan.actions:
            if a.side == "SELL":
                a.why += " " + tax_note
    # Stable sort on urgency ONLY: within an urgency the order already means
    # something (entries are ranked best-first), and breaking ties by ticker
    # would put the weakest idea above the strongest whenever it sorts first.
    plan.actions.sort(key=lambda a: a.urgency)
    if not plan.actions:
        plan.notes.append("Nothing to do today. Holding and waiting is a decision too.")
    return plan


ROBINHOOD_STOCK_URL = "https://robinhood.com/us/en/stocks/{t}/"
_CONTEXT_KINDS = ("WATCH", "HEADS_UP", "HOLD_PAST_TIME", "IDEA")


def order_ticket(a: Action) -> str | None:
    """What to type into Robinhood for an action you place yourself."""
    if a.side not in ("SELL", "BUY"):
        return None
    where = ROBINHOOD_STOCK_URL.format(t=a.ticker)
    if a.side == "SELL" and a.kind != "TRIM":
        how = "Sell -> All shares, market order"
    elif a.side == "SELL":
        how = f"Sell -> ${a.dollars:,.2f}, market order" if a.dollars else "Sell"
    else:
        how = f"Buy -> ${a.dollars:,.2f}, market order" if a.dollars else "Buy"
    return f"{where}  ->  {how}"


def format_plan(plan: Plan, manual: bool = False) -> list[str]:
    """The plan as the owner reads it — numbered, plain, nothing to decode.

    manual=True is for accounts you place orders in yourself: each action gets
    the exact page and order to enter, so acting is a copy, not a decision.
    """
    lines = []
    doing = [a for a in plan.actions if a.kind not in _CONTEXT_KINDS]
    context = [a for a in plan.actions if a.kind in ("WATCH", "HEADS_UP", "HOLD_PAST_TIME")]
    ideas = [a for a in plan.actions if a.kind == "IDEA"]

    if doing:
        for i, a in enumerate(doing, 1):
            tag = f"  [{a.confidence} confidence]" if a.confidence else ""
            lines.append(f"{i}. {a.headline}{tag}")
            lines.append(f"     {a.why}")
            if a.shares and a.side == "SELL" and a.kind == "TRIM":
                lines.append(f"     About {a.shares:g} shares.")
            ticket = order_ticket(a) if manual else None
            if ticket:
                lines.append(f"     DO IT: {ticket}")
            lines.append("")
    else:
        lines += ["Nothing to do today.", ""]
    if ideas:
        lines.append("Ideas (not recommendations)")
        for a in ideas:
            lines.append(f"  · {a.headline}")
        lines.append("")
    if context:
        lines.append("Worth knowing")
        for a in context:
            lines.append(f"  · {a.headline}")
        lines.append("")
    if plan.quiet:
        lines.append(f"No action needed: {', '.join(plan.quiet)}")
        lines.append("")
    for note in plan.notes:
        lines.append(note)
    return lines


FOOTER = (
    "StockSage never places orders. Your routine proposes each one and "
    "waits for your 'confirm'; anything marked manual you place yourself."
)


def entry_dates_from_orders(orders, held_tickers) -> dict[str, date]:
    """Most recent BUY fill for each name currently held."""
    latest: dict[str, date] = {}
    for o in orders:
        if (o["side"] or "").lower() != "buy" or o["ticker"] not in held_tickers:
            continue
        try:
            when = datetime.fromisoformat(str(o["executed_at"]).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if o["ticker"] not in latest or when > latest[o["ticker"]]:
            latest[o["ticker"]] = when
    return latest
