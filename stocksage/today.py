"""StockSage Today: one blunt sheet, every account, kept current.

This is the page you read. It starts with the BOTTOM LINE per account in plain
imperatives (SELL ALL X, TRIM Y, BUY Z, or nothing), then the evidence under
each line: why, the news and trend context a trader would check, and how
look-alike setups did in the past.

How a call earns the word it gets:

  SELL / TRIM  risk rules (stop, 10-day clock, size cap) are stated as orders
               because they need no belief in the model. A model SELL on a
               holding is stated plainly too, with its look-alikes beside it.
  BUY          stated plainly ONLY when look-alike history backs it
               (analogs.Evidence.backed). Otherwise it is listed as an idea
               with the reason, never as an instruction. If the look-alike
               book has not been built, nothing is backed, so nothing is a BUY.

Context (news, sector mood, earnings, volatility) is shown but does NOT change
a call: none of it has been tested as a predictor here, so letting it veto or
promote a trade would be claiming more than has been shown. It is there so you
can overrule the sheet with your eyes open.

Nothing here places an order. Pure assembly: the network calls it makes (news,
history) are injected via the engine so tests run offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import analogs as analog_mod
from .actions import Action, Plan

HEADLINE_CHARS = 90
MAX_CONTEXT_TICKERS = 14
LAB_FILTER_NAME = "model buys confirmed by look-alikes"
DRIVE_NAME = "StockSage Today.txt"      # the phone-readable copy in Drive


@dataclass
class Directive:
    verb: str                    # SELL | TRIM | BUY
    ticker: str
    text: str
    basis: str                   # "risk rule" | "model call"
    urgency: int = 3
    evidence: str | None = None
    context: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class AccountSheet:
    role: str
    title: str
    label: str
    manual: bool
    directives: list[Directive] = field(default_factory=list)
    ideas: list[str] = field(default_factory=list)       # buys history does not back
    watch: list[str] = field(default_factory=list)
    quiet: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    plan: Plan | None = None                              # evidence-filtered, for alerts

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("plan", "directives")}
        d["directives"] = [x.to_dict() for x in self.directives]
        return d


@dataclass
class Sheet:
    generated_at: str
    market: list[str] = field(default_factory=list)
    accounts: list[AccountSheet] = field(default_factory=list)
    overall_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"generated_at": self.generated_at, "market": self.market,
                "accounts": [a.to_dict() for a in self.accounts],
                "overall_notes": self.overall_notes, "notes": self.notes}


# --- the look-alike gate on buying -------------------------------------------


def apply_evidence(plan: Plan, evidence_for) -> Plan:
    """A copy of the plan where only look-alike-BACKED buys stay buys.

    evidence_for(ticker, side) -> analogs.Evidence | None. A buy that history
    does not back becomes an unnumbered IDEA carrying the reason, so it can
    neither be mistaken for an instruction nor trigger an alert.
    """
    out = Plan(quiet=list(plan.quiet), notes=list(plan.notes),
               trust=dict(plan.trust), sell_trust=dict(plan.sell_trust))
    for a in plan.actions:
        if a.kind != "ENTER":
            out.actions.append(a)
            continue
        ev = evidence_for(a.ticker, "buy")
        if ev is not None and ev.backed:
            out.actions.append(a)
            continue
        why = ev.sentence() if ev is not None else (
            "Look-alike history has not been built yet (run .\\start.bat analogs)."
        )
        out.actions.append(Action(
            "IDEA", a.ticker, "WATCH", 4,
            f"Idea: {a.ticker} — the model likes it, but history does not back it",
            why, stop=a.stop, target=a.target, confidence="low",
        ))
    return out


# --- context ------------------------------------------------------------------


def _market_lines(engine) -> list[str]:
    lines = []
    try:
        spy = engine.market.history("SPY")
        if spy is not None and len(spy) > 200:
            close = spy["Close"]
            above = float(close.iloc[-1]) > float(close.iloc[-200:].mean())
            month = float(close.iloc[-1] / close.iloc[-22] - 1.0) * 100
            lines.append(
                f"SPY is {'above' if above else 'BELOW'} its 200-day average "
                f"({'uptrend' if above else 'downtrend'}), {month:+.1f}% over the past month."
            )
    except Exception:
        pass
    try:
        trends = engine.sector_trends()
        if len(trends) >= 4:
            ranked = sorted(trends.items(), key=lambda kv: kv[1], reverse=True)
            lines.append(
                f"Strongest sector: {ranked[0][0]} ({ranked[0][1]:+.2f}); "
                f"weakest: {ranked[-1][0]} ({ranked[-1][1]:+.2f})."
            )
    except Exception:
        pass
    return lines


def _news_line(engine, ticker: str) -> str | None:
    from .context import tag_reasons

    try:
        items = engine.market.news(ticker, limit=6) or []
    except Exception:
        return None
    if not items:
        return "News: nothing notable this week."
    themes = tag_reasons(items)
    head = (items[0].get("title") or "").strip()
    if len(head) > HEADLINE_CHARS:
        head = head[: HEADLINE_CHARS - 1] + "…"
    theme_txt = f"; themes: {', '.join(themes)}" if themes else ""
    return f"News: {len(items)} recent stories{theme_txt}. Latest: \"{head}\""


def _trend_lines(sug, sector_trends: dict[str, float]) -> list[str]:
    out = []
    if sug is None:
        return out
    if getattr(sug, "why", ""):
        out.append(f"Why: {sug.why}")
    if sug.earnings_days is not None and sug.earnings_days <= 14:
        out.append(f"Earnings in {sug.earnings_days} days: a big move either way is possible.")
    vol = (getattr(sug, "risk", None) or {}).get("annualized_vol")
    if vol:
        out.append(f"Volatility: about {vol * 100:.0f}% a year.")
    if sug.sector and sug.sector in sector_trends:
        out.append(f"Sector ({sug.sector}) trend score {sector_trends[sug.sector]:+.2f}.")
    return out


# --- building -----------------------------------------------------------------

_RISK_KINDS = {"EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "TRIM"}


def _money(x: float | None) -> str:
    return f"${x:,.0f}" if x is not None else ""


def _directive(a: Action) -> Directive | None:
    if a.kind == "EXIT_STOP":
        return Directive("SELL", a.ticker, f"SELL ALL {a.ticker} (~{_money(a.dollars)}) — hit its stop",
                         "risk rule", 1)
    if a.kind == "EXIT_TARGET":
        return Directive("SELL", a.ticker, f"SELL ALL {a.ticker} (~{_money(a.dollars)}) — reached its target",
                         "risk rule", 2)
    if a.kind == "EXIT_TIME":
        return Directive("SELL", a.ticker, f"SELL ALL {a.ticker} (~{_money(a.dollars)}) — held its 10 days, not working",
                         "risk rule", 2)
    if a.kind == "TRIM":
        return Directive("TRIM", a.ticker, f"TRIM {a.ticker} by about {_money(a.dollars)} — over the size cap",
                         "risk rule", 3)
    if a.kind == "EXIT_SIGNAL":
        return Directive("SELL", a.ticker, f"SELL ALL {a.ticker} (~{_money(a.dollars)}) — the model rates it SELL",
                         "model call", 4)
    if a.kind == "ENTER":
        return Directive("BUY", a.ticker,
                         f"BUY about {_money(a.dollars)} of {a.ticker}"
                         + (" (after the sale settles)" if a.needs_sale_first else ""),
                         "model call", 3)
    return None


def lab_status(db) -> str:
    """Has the look-alike filter itself passed an out-of-sample test?"""
    when = db.get_meta("lab_at")
    if not when:
        return ("The look-alike filter has not been tested out of sample yet "
                "(run .\\start.bat lab), so treat a BUY as a lead, not a verdict.")
    passed = LAB_FILTER_NAME in (db.get_meta("lab_winners") or "")
    when = when[:10]
    if passed:
        return f"The look-alike filter passed an out-of-sample test on {when}."
    return (f"The look-alike filter did NOT pass its out-of-sample test on {when}: "
            f"history agreeing with a call has not been shown to improve results, so treat a BUY as a lead.")


def build_sheet(engine, client, suggestions=None, book="auto", news: bool = True,
                now: datetime | None = None, plans: dict | None = None) -> Sheet:
    """Assemble the sheet for every account under the login."""
    from .advisor import plans_for_accounts

    now = now or datetime.now()
    result = plans or plans_for_accounts(engine, client, suggestions, entries_override="always")
    by_ticker = result.get("suggestions", {})
    if book == "auto":
        book = analog_mod.AnalogBook.load()

    ev_cache: dict[tuple[str, str], analog_mod.Evidence | None] = {}

    def evidence_for(ticker: str, side: str):
        key = (ticker, side)
        if key not in ev_cache:
            sug = by_ticker.get(ticker)
            ev_cache[key] = (
                book.evidence(sug.signals, side) if (book is not None and sug is not None and sug.signals)
                else None
            )
        return ev_cache[key]

    try:
        sector_trends = engine.sector_trends()
    except Exception:
        sector_trends = {}

    sheet = Sheet(generated_at=now.strftime("%a %d %b %Y, %H:%M"),
                  market=_market_lines(engine), overall_notes=list(result.get("overall_notes", [])))
    if book is None:
        sheet.notes.append(
            "Look-alike history is not built yet, so no BUY can be stated plainly. "
            "Build it once with .\\start.bat analogs (a few minutes).")
    elif book.stale:
        sheet.notes.append(
            f"Look-alike history is {book.age_days:.0f} days old; refresh it with .\\start.bat analogs.")

    news_budget = MAX_CONTEXT_TICKERS
    news_cache: dict[str, str | None] = {}
    for entry in result["accounts"]:
        filtered = apply_evidence(entry["plan"], evidence_for)
        acct = AccountSheet(role=entry["role"], title=entry["title"], label=entry["label"],
                            manual=entry["manual"], quiet=list(filtered.quiet),
                            notes=[n for n in filtered.notes], plan=filtered)
        for a in filtered.actions:
            d = _directive(a)
            if d is None:
                if a.kind == "IDEA":
                    acct.ideas.append(a.ticker)
                elif a.kind in ("HEADS_UP", "WATCH", "HOLD_PAST_TIME"):
                    acct.watch.append(a.headline)
                continue
            side = "buy" if d.verb == "BUY" else "sell"
            sug = by_ticker.get(d.ticker)
            ev = evidence_for(d.ticker, side) if d.basis == "model call" else None
            if ev is None and d.basis == "model call":
                d.evidence = ("Look-alikes: history has not been built yet.")
            elif ev is not None:
                d.evidence = ev.sentence()
            d.context.append(a.why)
            d.context += _trend_lines(sug, sector_trends)
            if news and news_budget > 0 and d.ticker not in news_cache:
                news_cache[d.ticker] = _news_line(engine, d.ticker)
                news_budget -= 1
            if news_cache.get(d.ticker):
                d.context.append(news_cache[d.ticker])
            acct.directives.append(d)
        acct.directives.sort(key=lambda d: d.urgency)
        sheet.accounts.append(acct)
    if any(d.verb == "BUY" for a in sheet.accounts for d in a.directives):
        sheet.notes.append(lab_status(engine.db))
    return sheet


# --- rendering ----------------------------------------------------------------


def _bottom_line(a: AccountSheet) -> list[str]:
    how = "you reply 'confirm' in the routine" if not a.manual else "you place these yourself"
    # The label (e.g. "Margin ••••1234") tells two accounts of the same role apart.
    head = f"{a.title.upper()}  {a.label}".rstrip()
    out = [f"{head}  ({how})"]
    sells = [d for d in a.directives if d.verb in ("SELL", "TRIM")]
    buys = [d for d in a.directives if d.verb == "BUY"]
    for d in sells + buys:
        tag = "RULE" if d.basis == "risk rule" else "MODEL"
        out.append(f"  {d.text}   [{tag}]")
    if not buys:
        if a.ideas:
            out.append(f"  BUY: nothing. Ideas history does not back: {', '.join(a.ideas)}.")
        else:
            out.append("  BUY: nothing.")
    if not sells:
        out.append("  SELL: nothing.")
    return out


def render_text(sheet: Sheet) -> str:
    from .actions import order_ticket  # noqa: F401  (kept for parity with the CLI)

    lines = [f"STOCKSAGE TODAY  —  {sheet.generated_at}", "=" * 66]
    for m in sheet.market:
        lines.append(f"MARKET  {m}")
    lines += ["", "BOTTOM LINE", "-" * 66]
    for a in sheet.accounts:
        lines += _bottom_line(a) + [""]
    for n in sheet.overall_notes:
        lines.append(f"ACROSS ALL ACCOUNTS  {n}")
    lines += ["", "DETAIL", "-" * 66]
    for a in sheet.accounts:
        lines.append(f"{a.title.upper()}  {a.label}")
        if not a.directives and not a.watch:
            lines.append("  Nothing to do.")
        for d in a.directives:
            lines.append(f"  {d.text}   [{d.basis}]")
            for c in d.context:
                lines.append(f"      · {c}")
            if d.evidence:
                lines.append(f"      · {d.evidence}")
            if a.manual and d.verb in ("SELL", "TRIM", "BUY"):
                lines.append(f"      DO IT: https://robinhood.com/us/en/stocks/{d.ticker}/")
        for w in a.watch:
            lines.append(f"  watch: {w}")
        if a.quiet:
            lines.append(f"  No action needed: {', '.join(a.quiet)}")
        for n in a.notes:
            lines.append(f"  {n}")
        lines.append("")
    for n in sheet.notes:
        lines.append(n)
    lines += ["", "StockSage never places orders. Context (news, sector) is shown for you to weigh; "
              "it does not change a call."]
    return "\n".join(lines)


# --- saving -------------------------------------------------------------------


def state_dir() -> Path:
    import os

    override = os.environ.get("STOCKSAGE_STATE")
    return Path(override).expanduser() if override else Path("~/.stocksage").expanduser()


def save_local(sheet: Sheet, text: str | None = None) -> Path:
    base = state_dir()
    base.mkdir(parents=True, exist_ok=True)
    (base / "today.json").write_text(json.dumps(sheet.to_dict(), indent=2), encoding="utf-8")
    path = base / "today.txt"
    path.write_text(text or render_text(sheet), encoding="utf-8")
    return path
