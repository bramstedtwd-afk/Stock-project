"""Command-line interface. `python -m stocksage --help` for the map.

Commands:
    daily       Full heartbeat: learn from matured calls, scan, suggest.
    suggest     Scan and print today's ranked suggestions (no recording).
    sectors     Sector trend scoreboard.
    portfolio   Your Robinhood holdings alongside current signals.
    moves       Browse the learned move-context history.
    performance Learning status: hit rate, weights, evaluated count.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import universe
from .db import Database
from .engine import Engine
from .envfile import load_env
from .robinhood import RobinhoodClient

DISCLAIMER = (
    "\n  StockSage is decision support, not financial advice. "
    "Every trade is your call.\n"
)


def _fmt_pct(x, digits: int = 1) -> str:
    return f"{x * 100:+.{digits}f}%" if x is not None else "n/a"


def _print_suggestions(suggestions, limit: int) -> None:
    print(f"\n{'TICKER':<7}{'ACTION':<13}{'SCORE':>7}{'PRICE':>10}{'SIZE':>7}  SECTOR")
    print("-" * 70)
    for s in suggestions[:limit]:
        size = f"{s.position_fraction:.0%}" if s.position_fraction else "-"
        print(
            f"{s.ticker:<7}{s.action:<13}{s.risk_adjusted_score:>7.2f}"
            f"{s.price:>10,.2f}{size:>7}  {s.sector or ''}"
        )
        if s.why:
            print(f"        {s.why}")
        for note in s.notes:
            print(f"        · {note}")
    print(DISCLAIMER)


def cmd_daily(args) -> int:
    engine = Engine()
    if engine.db.get_meta("bootstrap_done") is None:
        print("First run detected — bootstrapping from two years of history first")
        print("(pre-training signal weights + backfilling the move memory)...")
    print("Running daily cycle: evaluate -> learn -> scan -> suggest ...")
    result = engine.daily_run(with_robinhood=not args.no_robinhood)
    if result.bootstrap_stats:
        bs = result.bootstrap_stats
        print(
            f"\nBootstrapped: {bs['warmup_samples']} walk-forward training samples, "
            f"{bs['move_events_backfilled']} historical moves remembered "
            f"({bs['seconds']}s)."
        )
    from .briefing import briefing_lines, build_briefing

    print("\n--- Today's briefing " + "-" * 40)
    for line in briefing_lines(build_briefing(result, engine.db)):
        print(f"  {line}")
    print("-" * 61)
    print(f"\nGraded {result.evaluated_count} matured suggestions (weights updated).")
    if result.portfolio:
        print(
            f"Robinhood linked: {len(result.portfolio.holdings)} holdings, "
            f"${result.portfolio.buying_power:,.2f} buying power."
        )
        if result.rh_sync:
            print(
                f"History mirror: {result.rh_sync['orders_total']} orders "
                f"({result.rh_sync['orders_added']} new this run)."
            )
    else:
        print("Robinhood not linked this run (set credentials in .env to enable).")

    if result.sector_trends:
        print("\nSector trends (composite score, -1 bearish .. +1 bullish):")
        for name, score in sorted(result.sector_trends.items(), key=lambda kv: -kv[1]):
            bar = "#" * int(abs(score) * 20)
            print(f"  {name:<26}{score:+.2f}  {bar}")

    actionable = result.actionable
    print(f"\nActionable suggestions ({len(actionable)} of {len(result.suggestions)} scanned):")
    _print_suggestions(actionable, args.limit)

    if result.move_events:
        print("Significant moves explained today:")
        for ev in result.move_events:
            reasons = ", ".join(ev["reasons"])
            print(f"  {ev['ticker']:<7}{_fmt_pct(ev['return_pct'])}  [{reasons}]")
    if result.errors:
        print(f"\n({len(result.errors)} tickers skipped — data unavailable)")
    return 0


def cmd_suggest(args) -> int:
    engine = Engine()
    graded = engine.evaluate_pending()  # grade whenever touched, not just daily
    if graded:
        print(f"(Graded {graded} matured suggestions first — the model just got smarter.)")
    portfolio = None
    if not args.no_robinhood and RobinhoodClient.credentials_available():
        portfolio = RobinhoodClient().portfolio()
    tickers = args.tickers or None
    result = engine.scan(
        tickers=tickers, portfolio=portfolio, capture_context=False, record=False
    )
    pool = result.actionable if not args.all else result.suggestions
    _print_suggestions(pool, args.limit)
    return 0


def cmd_sectors(args) -> int:
    engine = Engine()
    trends = engine.sector_trends()
    if not trends:
        print("No sector data available (check network / try again).")
        return 1
    print("\nSector trend scoreboard (-1 bearish .. +1 bullish):")
    for name, score in sorted(trends.items(), key=lambda kv: -kv[1]):
        sector = universe.sector_by_name(name)
        bar = "#" * int(abs(score) * 20)
        print(f"  {name:<26}{sector.etf:<6}{score:+.2f}  {bar}")
    print()
    return 0


def cmd_portfolio(args) -> int:
    client = RobinhoodClient()
    portfolio = client.portfolio()
    if portfolio is None:
        print(
            "Could not load Robinhood portfolio. Set ROBINHOOD_USERNAME / "
            "ROBINHOOD_PASSWORD (and optionally ROBINHOOD_MFA_SECRET) — see .env.example."
        )
        return 1
    print(f"\nHoldings ({len(portfolio.holdings)}), buying power ${portfolio.buying_power:,.2f}:")
    engine = Engine()
    held = [h.ticker for h in portfolio.holdings]
    result = engine.scan(tickers=held, portfolio=portfolio, capture_context=False, record=False)
    by_ticker = {s.ticker: s for s in result.suggestions}
    print(f"\n{'TICKER':<7}{'SHARES':>9}{'AVG COST':>10}{'PRICE':>10}{'P/L':>9}  SIGNAL")
    print("-" * 60)
    for h in portfolio.holdings:
        pl = (h.current_price / h.avg_buy_price - 1.0) if h.avg_buy_price else None
        sig = by_ticker.get(h.ticker)
        print(
            f"{h.ticker:<7}{h.shares:>9.2f}{h.avg_buy_price:>10,.2f}"
            f"{h.current_price:>10,.2f}{_fmt_pct(pl):>9}  {sig.action if sig else 'n/a'}"
        )

    # Full-history insight (auto-synced during daily runs; sync here too).
    from .insights import model_alignment, trading_insights

    db = engine.db
    sync = client.sync_history(db)
    if sync:
        print(
            f"\nHistory mirror: {sync['orders_total']} orders "
            f"({sync['orders_added']} new), {sync['dividends_added']} new dividends."
        )
    orders = db.rh_orders()
    if orders:
        ti = trading_insights(orders, db.rh_dividends())
        print("\nWhat your history says")
        print("-" * 40)
        print(f"Realized P&L (FIFO): ${ti['realized_pnl']:+,.2f} over {ti['round_trips']} round trips")
        if ti["win_rate"] is not None:
            print(f"Your win rate      : {ti['win_rate']:.0%}")
        if ti["avg_held_days"] is not None:
            print(f"Avg holding time   : {ti['avg_held_days']:.0f} days")
        print(f"Dividends collected: ${ti['dividends_total']:,.2f}")
        if ti["best_name"]:
            print(f"Best / costliest   : {ti['best_name'][0]} ${ti['best_name'][1]:+,.2f}"
                  f"  /  {ti['worst_name'][0]} ${ti['worst_name'][1]:+,.2f}")
        align = model_alignment(orders, db.recent_suggestions(1000))
        if align["agreement_rate"] is not None:
            print(f"Model agreement    : {align['agreement_rate']:.0%} of covered trades")
    print(DISCLAIMER)
    return 0


def cmd_moves(args) -> int:
    db = Database()
    rows = db.move_events(ticker=args.ticker, limit=args.limit)
    if not rows:
        print("No move events recorded yet — run `daily` a few times to build history.")
        return 0
    for row in rows:
        reasons = ", ".join(json.loads(row["reasons"])) or "unexplained"
        print(f"\n{row['event_date']}  {row['ticker']:<7}{_fmt_pct(row['return_pct'])}  [{reasons}]")
        for h in json.loads(row["headlines"])[:3]:
            print(f"    - {h['title']}")
    print()
    return 0


def cmd_bootstrap(args) -> int:
    from .bootstrap import bootstrap
    from .data import MarketData

    print("Bootstrapping from two years of history (walk-forward, no look-ahead)...")
    print("Re-running later trains further on the freshest data — safe to repeat.")
    stats = bootstrap(MarketData(), Database())
    print(f"\nTraining samples applied : {stats['warmup_samples']}")
    print(f"Historical moves recorded: {stats['move_events_backfilled']}")
    print(f"Elapsed                  : {stats['seconds']}s")
    if stats["weights"]:
        print("\nSignal weights after bootstrap:")
        for name, w in sorted(stats["weights"].items(), key=lambda kv: -kv[1]):
            print(f"  {name:<24}{w:.3f}")
    return 0


def cmd_brain(args) -> int:
    from . import brain

    if args.brain_action == "export":
        path = brain.export_brain(args.path)
        print(f"Brain exported to {path.resolve()}")
        print("Move this file to any device and run:  ./start.sh brain import <file>")
    elif args.brain_action == "import":
        stats = brain.import_brain(args.path, replace=args.replace)
        if stats.get("replaced"):
            print("Brain replaced." + (f" Previous brain backed up to {stats['backup']}" if stats["backup"] else ""))
        else:
            print(
                f"Brains merged: +{stats['suggestions_added']} suggestions, "
                f"+{stats['move_events_added']} move events, "
                f"weights kept from the {stats['weights_taken_from']} brain "
                "(the one that learned most recently)."
            )
    elif args.brain_action == "snapshot":
        path = brain.write_snapshot()
        print(f"Brain snapshot written to {path}")
        print("Commit and push it — every environment that pulls the repo gets the knowledge:")
        print('  git add brain/brain-snapshot.db && git commit -m "brain snapshot" && git push')
    elif args.brain_action == "absorb":
        stats = brain.absorb_snapshot()
        if stats is None:
            print("No snapshot in the repo (brain/brain-snapshot.db) — nothing to absorb.")
        else:
            print(
                f"Snapshot absorbed: +{stats['suggestions_added']} suggestions, "
                f"+{stats['move_events_added']} move events (merges only ever add)."
            )
    elif args.brain_action == "sync":
        target = brain.sync_to_folder(args.path)
        print(f"Brain now lives at {target}")
        print("Run the same command with the same folder on your other devices —")
        print("they will all share this one brain.")
    elif args.brain_action == "pull-drive":
        from pathlib import Path

        from .drive_api import DriveNotConfigured, pull_brain_snapshot

        try:
            pulled = pull_brain_snapshot(Path("~/.stocksage/drive-pull.db").expanduser())
        except DriveNotConfigured as exc:
            print(f"Not set up yet: {exc}")
            return 1
        if pulled is None:
            print("No brain has been published to Drive yet — nothing to pull.")
            return 0
        stats = brain.import_brain(pulled)
        print(
            f"Pulled the shared brain from Drive and merged it in: "
            f"+{stats['suggestions_added']} suggestions, "
            f"+{stats['move_events_added']} move events, "
            f"weights kept from the {stats['weights_taken_from']} brain "
            "(the one that learned most recently). No Drive-for-Desktop "
            "install needed — this is the same no-install API path as publish-drive."
        )
    else:  # info
        info = brain.brain_info()
        print("\nBrain")
        print("-" * 40)
        for key, value in info.items():
            print(f"  {key:<18}{value}")
    return 0


def cmd_brief(args) -> int:
    import json as _json

    from .advisor import build_brief
    from .robinhood import RobinhoodClient

    engine = Engine()
    buying_power = None
    if RobinhoodClient.credentials_available():
        portfolio = RobinhoodClient().portfolio()
        if portfolio is not None:
            buying_power = portfolio.buying_power
    brief = build_brief(
        engine, tickers=args.tickers or None, max_price=args.max_price, top=args.top,
        buying_power=buying_power,
    )
    if args.json:
        print(_json.dumps(brief, indent=2))
        return 0
    print(f"\nResearch brief — {brief['as_of']} "
          f"(graded {brief['graded_this_call']} matured calls first)")
    stats = brief["model_stats"]
    print(
        f"Model record: {stats['graded_calls']} graded calls"
        + (f", {stats['hit_rate']:.0%} hit rate" if stats["hit_rate"] is not None else "")
        + (f", paper profit factor {stats['paper_profit_factor']}"
           if stats["paper_profit_factor"] is not None else "")
    )
    for section, entries in (("FOCUS", brief["focus"]), ("CANDIDATES", brief["candidates"])):
        if not entries:
            continue
        print(f"\n{section}:")
        for e in entries:
            if e.get("data") == "unavailable":
                print(f"  {e['ticker']:<7} (no data — brain context only)")
                continue
            rec = e["model_record"]
            rec_txt = (
                f"record {rec['hit_rate']:.0%} over {rec['graded_calls']}"
                if rec["hit_rate"] is not None else "no record yet"
            )
            size_txt = (
                f"  buy ~${e['size_hint_dollars']:,.2f} (~{e['est_shares']:g} sh)"
                if e.get("size_hint_dollars")
                else f"  size {e['size_hint_pct']}%"
            )
            print(
                f"  {e['ticker']:<7}{e['verdict']:<12}score {e['score']:+.2f}  "
                f"${e['price']:,.2f}  stop {e['stop']}  target {e['target']}  "
                f"({rec_txt}){size_txt}"
            )
            if e["recent_shock"]:
                sh = e["recent_shock"]
                print(f"          shock {sh['date']}: {sh['return_pct']:+.1%} "
                      f"[{', '.join(sh['reasons'])}]")
            if e.get("earnings_blackout"):
                print(f"          ⚠ earnings in {e['earnings_days']} day(s) — "
                      "new entry on hold until after the print")
    if brief["avoid"]:
        print("\nAVOID: " + ", ".join(f"{a['ticker']} ({a['verdict']})" for a in brief["avoid"]))
    if brief.get("congress_watch"):
        cw = ", ".join(
            f"{c['ticker']} ({c['members']} members)" for c in brief["congress_watch"][:6]
        )
        print(f"\nCONGRESS BUYING: {cw}")
    print(DISCLAIMER)
    return 0


def cmd_sync(args) -> int:
    from .advisor import desktop_sync_cycle

    engine = Engine()
    stats = desktop_sync_cycle(engine)
    if stats.get("sync_error"):
        print(f"Robinhood sync skipped: {stats['sync_error']}")
    elif stats["synced"]:
        print(
            f"Mirrored {stats['synced']['orders_total']} orders "
            f"({stats['synced']['orders_added']} new)."
        )
    else:
        print("Robinhood not linked — grading recorded calls only.")
    print(
        f"Captured {stats['fills_ingested']} new trades as graded calls, "
        f"graded {stats['graded']} matured calls. The brain just got smarter."
    )
    return 0


def _sync_and_focus(engine, explicit_tickers, broker: bool = True):
    """Capture+grade real trades, then build the ticker list the routine
    cares about most: what it holds, plus the watchlist, plus anything
    named explicitly. Shared by both publish paths."""
    from .advisor import desktop_sync_cycle

    sync = desktop_sync_cycle(engine, broker=broker)
    if sync.get("broker_skipped"):
        print("(Publishing without opening a Robinhood session — --no-broker.)")
    if sync["fills_ingested"] or sync["graded"]:
        print(
            f"(Captured {sync['fills_ingested']} new trades, "
            f"graded {sync['graded']} matured calls first.)"
        )
    focus = list(explicit_tickers or [])
    for t in engine.db.watchlist() + sync.get("holdings", []):
        if t not in focus:
            focus.append(t)
    return sync, focus or None


def cmd_publish(args) -> int:
    from .advisor import publish_brief

    engine = Engine()
    sync, focus = _sync_and_focus(engine, args.tickers, broker=not args.no_broker)
    result = publish_brief(
        engine, args.drive_folder, tickers=focus, max_price=args.max_price,
        holdings=sync.get("holdings"), buying_power=sync.get("buying_power"),
    )
    print(f"Published research to {result['folder']}:")
    for path in result["written"]:
        print(f"  · {path}")
    print(
        f"({result['candidates']} candidates in the brief.) "
        "If this folder is synced by Google Drive for Desktop, your routine "
        "can now read it. Schedule this alongside autopilot for fresh research each day."
    )
    return 0


def _pull_drive_brain(engine) -> None:
    """Best-effort catch-up before grading or scanning — see
    advisor.catch_up_from_drive for the mechanics. Silent no-op if Drive
    isn't configured or has nothing new; never blocks the run."""
    from .advisor import catch_up_from_drive

    stats = catch_up_from_drive(engine)
    if stats and (stats.get("suggestions_added") or stats.get("move_events_added")):
        print(
            f"(Caught up from Drive: +{stats['suggestions_added']} suggestions, "
            f"+{stats['move_events_added']} move events.)"
        )


def cmd_publish_drive(args) -> int:
    from .advisor import publish_brief_via_api
    from .drive_api import DriveNotConfigured

    engine = Engine()
    _pull_drive_brain(engine)
    sync, focus = _sync_and_focus(engine, args.tickers, broker=not args.no_broker)
    try:
        result = publish_brief_via_api(
            engine, tickers=focus, max_price=args.max_price,
            holdings=sync.get("holdings"), buying_power=sync.get("buying_power"),
        )
    except DriveNotConfigured as exc:
        print(f"Not set up yet: {exc}")
        return 1
    print(f"Published directly to Google Drive ({result['candidates']} candidates):")
    for name in result["files"]:
        print(f"  · {name}  (in your Drive's StockSage folder)")
    print("No desktop app or sync client involved — this went straight to Drive's API.")
    return 0


def cmd_log_call(args) -> int:
    from .advisor import log_call

    engine = Engine()
    sid = log_call(engine, args.ticker, args.action, price=args.price, note=args.note)
    print(
        f"Logged call #{sid}: {args.action.upper()} {args.ticker.upper()}"
        + (f" @ ${args.price:,.2f}" if args.price else "")
        + " — it will be graded automatically and feed the model's learning."
    )
    return 0


def cmd_congress(args) -> int:
    from .congress import CongressData, notable_buys

    summary = CongressData().summary()
    if not summary:
        print("Congress-buying tracking is currently disabled — no reliable free data "
              "source is available (see stocksage/congress.py for details).")
        return 0
    tickers = notable_buys(summary, top=args.limit)
    if not tickers:
        print("No notable congressional buying in the recent window.")
        return 0
    print("\nWhere Congress is putting money (recent disclosed buys):")
    print(f"{'TICKER':<8}{'MEMBERS':>8}{'NET BUYS':>10}{'~$ EST':>12}  LAST")
    print("-" * 52)
    for tk in tickers:
        s = summary[tk]
        print(f"{tk:<8}{s['members']:>8}{s['net_buys']:>10}{s['est_amount']:>12,}  "
              f"{s['last_date']}")
    print("\n(Context only — a tilt to weigh, not a mechanical signal.)\n")
    return 0


def cmd_watch(args) -> int:
    db = Database()
    if args.watch_action == "add":
        for t in args.tickers:
            db.watchlist_add(t)
    elif args.watch_action == "remove":
        for t in args.tickers:
            db.watchlist_remove(t)
    items = db.watchlist()
    if items:
        print("Watchlist (scanned daily, on top of the universe and your holdings):")
        for t in items:
            print(f"  ⭐ {t}")
    else:
        print("Watchlist is empty — `watch add TICKER` to follow extra names.")
    return 0


def cmd_profit(args) -> int:
    from .profit import default_stake, paper_trades, profit_stats

    db = Database()
    buys, avoided = paper_trades(db.evaluated_suggestions())
    stats = profit_stats(buys, avoided)
    stake = default_stake()
    print(f"\nPaper ledger (${stake:,.0f} per idea) — what following StockSage would earn")
    print("-" * 68)
    if not buys and not avoided:
        print("No graded calls yet — the ledger fills in as suggestions mature.")
        return 0
    print(f"Buy-side trades   : {stats['trades']}")
    print(f"Paper P&L         : ${stats['total_pnl']:+,.2f}")
    if stats["win_rate"] is not None:
        print(f"Win rate          : {stats['win_rate']:.0%}")
    if stats["profit_factor"] is not None:
        print(f"Profit factor     : {stats['profit_factor']:.2f}  (>1 = wins pay for losses)")
    if stats["avg_win"] is not None and stats["avg_loss"] is not None:
        print(f"Avg win / loss    : ${stats['avg_win']:+,.2f} / ${stats['avg_loss']:+,.2f}")
    print(f"Risk avoided      : ${stats['risk_avoided']:+,.2f} across {stats['avoid_calls']} avoid calls")
    if stats.get("edge_vs_market") is not None:
        verdict = "ahead of" if stats["edge_vs_market"] >= 0 else "behind"
        n = stats["covered_trades"]
        print(
            f"Vs. SPY           : ${abs(stats['edge_vs_market']):,.2f} {verdict} parking "
            f"the same stakes in SPY (across {n} trade{'s' if n != 1 else ''})"
        )
    if stats["best"]:
        b, w = stats["best"], stats["worst"]
        print(f"Best / worst call : {b.ticker} ${b.pnl:+,.2f}  /  {w.ticker} ${w.pnl:+,.2f}")
    print(DISCLAIMER)
    return 0


def cmd_suggestions(args) -> int:
    """Turn the model's picks on or off as actionable for the routine."""
    db = Database()
    if args.state in ("on", "off"):
        db.set_model_suggestions(args.state == "on")
    on = db.model_suggestions_enabled()
    print(f"\nModel suggestions: {'ON' if on else 'OFF'}")
    if on:
        print("  The routine may act on the engine's picks (still confirm-gated).")
    else:
        print("  The engine keeps scanning, grading and learning, but the routine")
        print("  treats its picks as research only and trades its own analysis.")
        print("  Sells on names you hold stay actionable — turning suggestions")
        print("  off must never trap you in a position.")
    print(DISCLAIMER)
    return 0


def cmd_performance(args) -> int:
    db = Database()
    summary = db.performance_summary()
    weights = db.load_weights()
    print("\nLearning status")
    print("-" * 40)
    warmup = db.get_meta("warmup_samples")
    if warmup:
        print(f"Historical samples : {warmup} (bootstrap walk-forward)")
    print(f"Suggestions graded : {summary['evaluated']}")
    if summary["evaluated"]:
        print(f"Direction hit rate : {summary['hit_rate']:.0%}")
        print(f"Avg realized return: {_fmt_pct(summary['avg_return'], 2)}")
    if weights:
        print("\nCurrent signal weights (learned):")
        for name, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<24}{w:.3f}  {'#' * int(w * 40)}")
    sector_grades = db.sector_grade_counts()
    if sector_grades:
        from .learning import MIN_SECTOR_GRADES

        print("\nPer-sector learning (sector weights vote after "
              f"{MIN_SECTOR_GRADES} graded calls):")
        for name, n in sorted(sector_grades.items(), key=lambda kv: -kv[1]):
            status = "ACTIVE" if n >= MIN_SECTOR_GRADES else f"{n}/{MIN_SECTOR_GRADES}"
            print(f"  {name:<26}{n:>4} graded  [{status}]")
    else:
        print("\nWeights still at uniform defaults — grading begins after the first")
        print("suggestions mature (~1 week of daily runs).")
    print()
    return 0


def _parse_when(text: str):
    """Turn what the owner types into a timestamp: '8:30am', '08:30',
    '2026-08-19 08:30', or a full ISO string. Bare times mean today, local."""
    from datetime import datetime

    raw = text.strip().lower().replace(".", "")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M%p", "%Y-%m-%dt%H:%M"):
        try:
            return datetime.strptime(raw, fmt).astimezone()
        except ValueError:
            pass
    for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        today = datetime.now().astimezone()
        return today.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
        )
    try:
        return datetime.fromisoformat(text.strip()).astimezone()
    except ValueError:
        return None


def _local(iso: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %H:%M")
    except ValueError:
        return iso


def cmd_security(args) -> int:
    from . import security

    if args.signin:
        when = _parse_when(args.signin)
        if when is None:
            print(
                f"Could not read the time {args.signin!r}. Try 8:30am, 08:30, "
                "or 2026-08-19 08:30."
            )
            return 1
        verdict = security.explain_signin(when.isoformat())
        headline = (
            "THAT SIGN-IN WAS STOCKSAGE."
            if verdict["ours"]
            else "THAT SIGN-IN WAS NOT STOCKSAGE."
        )
        print(f"\n{headline}\n{verdict['note']}\n")
        if not verdict["ours"]:
            print(_INTRUDER_STEPS)
        return 0

    print("\nACCOUNT SECURITY\n" + "-" * 60)
    worst = "ok"
    for finding in security.audit():
        mark = {"ok": "OK  ", "risk": "RISK", "critical": "!!  "}[finding["level"]]
        print(f"{mark} {finding['name']}: {finding['detail']}")
        if finding["fix"]:
            print(f"       fix: {finding['fix']}")
        if finding["level"] != "ok" and worst == "ok":
            worst = finding["level"]

    events = security.recent_access(limit=args.limit)
    print("\nBROKER SESSIONS THIS APP OPENED (newest first, your local time)")
    if not events:
        print(
            "  (none recorded yet — this log starts from the next run. Until it "
            "has a few days in it, Robinhood's own device list is the source of truth.)"
        )
    else:
        for e in events:
            label = {
                security.FRESH_LOGIN: "signed in   (Robinhood alerts you)",
                security.SESSION_REUSED: "reused token (silent, no alert)",
                security.LOGIN_FAILED: "login FAILED",
            }.get(e.get("event"), e.get("event", "?"))
            print(f"  {_local(e['at']):<18} {label:<36} via {e.get('trigger', '?')}")

    print(
        "\nTo check a sign-in alert you got:\n"
        "  start.bat security --signin 8:30am\n"
        "\nTo see it from Robinhood's side (the only place a sign-in StockSage\n"
        "did NOT make will show up):\n" + _INTRUDER_STEPS
    )
    return 0 if worst != "critical" else 1


_INTRUDER_STEPS = """  1. Open the Robinhood app -> Account -> Menu (three bars) -> Settings
     -> Security and privacy -> Devices. Every device with an active session
     is listed. Log out anything you do not recognise.
  2. Same screen: 'Login history' / 'Recent activity' shows time, device and
     location for each sign-in. Compare against the list above.
  3. If anything is unrecognised, in this order: change your Robinhood
     password, re-enrol two-factor (this invalidates the old TOTP seed), then
     re-link StockSage from the dashboard's Portfolio tab.
  4. StockSage can only read your account — it cannot place, change or cancel
     an order, so nothing it does can move money."""


def _add_no_broker(parser) -> None:
    parser.add_argument(
        "--no-broker", action="store_true",
        help="publish without opening a Robinhood session (fewer sign-in "
        "alerts when you publish several times a day)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stocksage", description="Personal learning market intelligence engine."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("daily", help="full daily cycle: learn, scan, suggest")
    p.add_argument("--no-robinhood", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("suggest", help="scan and rank without recording")
    p.add_argument("tickers", nargs="*", help="specific tickers (default: whole universe)")
    p.add_argument("--all", action="store_true", help="include HOLDs")
    p.add_argument("--no-robinhood", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("sectors", help="sector trend scoreboard")
    p.set_defaults(func=cmd_sectors)

    p = sub.add_parser("portfolio", help="Robinhood holdings with live signals")
    p.set_defaults(func=cmd_portfolio)

    p = sub.add_parser("moves", help="browse learned move-context history")
    p.add_argument("--ticker")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_moves)

    p = sub.add_parser(
        "bootstrap", help="(re)train weights and move memory from two years of history"
    )
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("brain", help="export/import/sync everything StockSage has learned")
    brain_sub = p.add_subparsers(dest="brain_action", required=True)
    b = brain_sub.add_parser("export", help="snapshot the brain to a portable file")
    b.add_argument("path", nargs="?", help="destination file (default: ./stocksage-brain-<date>.db)")
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser("import", help="merge (default) or replace with a brain file")
    b.add_argument("path")
    b.add_argument("--replace", action="store_true", help="swap wholesale instead of merging")
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser(
        "snapshot", help="export the brain into the repo so git carries the knowledge"
    )
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser(
        "absorb", help="merge the repo's brain snapshot into this machine's brain"
    )
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser("sync", help="keep the brain in a cloud-synced folder")
    b.add_argument("path", help="folder synced by Dropbox/iCloud/OneDrive/...")
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser(
        "pull-drive",
        help="pull the shared brain from Google Drive (no-install path) and merge it in",
    )
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser("info", help="where the brain lives and what it knows")
    b.set_defaults(func=cmd_brain)

    p = sub.add_parser(
        "brief", help="research packet for an agent/routine (use --json for machines)"
    )
    p.add_argument("tickers", nargs="*", help="tickers the routine holds or is eyeing")
    p.add_argument("--max-price", type=float, help="only suggest candidates at/under this price")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser(
        "publish", help="write a research packet + playbook into a Drive-synced folder"
    )
    p.add_argument("drive_folder", help="folder synced by Google Drive for Desktop")
    p.add_argument("tickers", nargs="*", help="focus tickers (default: your watchlist)")
    p.add_argument("--max-price", type=float)
    _add_no_broker(p)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser(
        "publish-drive",
        help="push research straight to Google Drive's API — no desktop app, "
        "no admin rights needed (one-time browser sign-in instead)",
    )
    p.add_argument("tickers", nargs="*", help="focus tickers (default: your watchlist)")
    p.add_argument("--max-price", type=float)
    _add_no_broker(p)
    p.set_defaults(func=cmd_publish_drive)

    p = sub.add_parser(
        "sync", help="capture real trades as graded calls + grade matured ones"
    )
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser(
        "log-call", help="record a trading decision so the brain grades it later"
    )
    p.add_argument("ticker")
    p.add_argument("action", help="BUY / SELL / STRONG BUY / STRONG SELL")
    p.add_argument("--price", type=float, help="executed or quoted price (else latest close)")
    p.add_argument("--note", help="one-line reasoning, kept in the learning log")
    p.set_defaults(func=cmd_log_call)

    p = sub.add_parser("congress", help="where Congress is putting money lately")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_congress)

    p = sub.add_parser("watch", help="manage the watchlist (extra tickers scanned daily)")
    p.add_argument("watch_action", choices=["add", "remove", "list"])
    p.add_argument("tickers", nargs="*")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("profit", help="paper P&L ledger: what following the calls would earn")
    p.set_defaults(func=cmd_profit)

    p = sub.add_parser("performance", help="learning status and signal weights")
    p.set_defaults(func=cmd_performance)

    p = sub.add_parser(
        "suggestions", help="turn the model's picks on/off for the routine"
    )
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p.set_defaults(func=cmd_suggestions)

    p = sub.add_parser(
        "security",
        help="who has been signing in to your broker account, and how exposed "
        "your stored login is",
    )
    p.add_argument(
        "--signin", metavar="TIME",
        help="was the Robinhood sign-in alert at this time StockSage? e.g. 8:30am",
    )
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_security)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()  # pick up .env automatically; real environment still wins
    args = build_parser().parse_args(argv)
    # Name the command in the environment so any broker session opened during
    # it is attributable in the access log — "was that 8:30 sign-in me?" needs
    # an answer, not a guess (stocksage/security.py).
    os.environ.setdefault("STOCKSAGE_TRIGGER", getattr(args, "command", None) or "cli")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        # yfinance logs a red ERROR per failed symbol, and retries mean one
        # delisted or unrecognized ticker in a watchlist prints the same
        # scary line a dozen times on a run that otherwise succeeded. We
        # already catch these, fall back to cached data, and report them
        # ourselves as scan errors — so its chatter is pure noise to the
        # owner. -v still shows everything.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
