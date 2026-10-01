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
from pathlib import Path

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
    elif args.brain_action == "audit":
        from . import audit

        try:
            report = audit.audit_brain()
        except FileNotFoundError as exc:
            print(exc)
            return 1
        print("\n".join(audit.describe(report)))
    elif args.brain_action == "repair":
        from . import audit

        try:
            res = audit.repair_duplicates(apply=args.apply)
        except FileNotFoundError as exc:
            print(exc)
            return 1
        if not res["groups"] and not res["applied"]:
            print("No duplicate owner calls found — nothing to repair.")
        elif not args.apply:
            print(
                f"Found {res['rows_removed']} duplicate owner calls "
                f"({res['groups']} trades recorded more than once)."
            )
            print("Nothing has been changed. To remove them (a backup is made first):")
            print("  .\\start.bat brain repair --apply      (Windows)")
            print("  ./start.sh brain repair --apply        (Mac/Linux)")
        else:
            print(f"Backup saved: {res['backup']}")
            print(
                f"Removed {res['rows_removed']} duplicate calls; relinked "
                f"{res['ledger_linked']} fills in the ledger."
            )
            print("Run the audit again to confirm:  .\\start.bat brain audit")
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


def _sync_and_focus(engine, explicit_tickers, broker: bool = True, client=None):
    """Capture+grade real trades, then build the ticker list the routine
    cares about most: what it holds, plus the watchlist, plus anything
    named explicitly. Shared by both publish paths."""
    from .advisor import desktop_sync_cycle

    sync = desktop_sync_cycle(engine, client=client, broker=broker)
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
            positions=sync.get("positions"),
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


def _keep_lookalikes(samples) -> None:
    """Remember what a backtest just replayed, so today's sheet can cite it."""
    try:
        from .analogs import AnalogBook, store_if_better

        store_if_better(AnalogBook.from_samples(samples))
    except Exception:
        pass     # a cache that can't be written must never fail the command


def cmd_research(args) -> int:
    """Test ~60 rules honestly: does anything beat just holding the market?"""
    import json
    from datetime import datetime, timezone

    from . import research, today
    from .db import Database

    print("Testing every rule on 20 years of ETF history (about a minute)...\n")
    cache = today.state_dir() / "research_prices.parquet"
    try:
        prices = research.load_prices(cache)
    except Exception as exc:
        print(f"Could not get the price history: {exc}")
        return 1
    res = research.run(prices, draws=args.draws)
    print("\n".join(research.describe(res, top=args.top)))
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        base = today.state_dir()
        base.mkdir(parents=True, exist_ok=True)
        report = {"at": stamp, **{k: v for k, v in res.items() if k != "rows"},
                  "rows": [r.to_dict() for r in res["rows"]]}
        (base / "research.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        db = Database()
        db.set_meta("research_at", stamp)
        db.set_meta("research_survivors", ",".join(r.name for r in res["rows"] if r.passed))
        db.close()
        print(f"\n(Saved to {base / 'research.json'})")
    except OSError:
        pass
    return 0


def cmd_alerts(args) -> int:
    """Set up (or test, or turn off) phone alerts through the free ntfy app."""
    import secrets

    from . import envfile, notify

    topic = notify.topic_from_env()
    if args.action == "off":
        envfile.save_env({"STOCKSAGE_NTFY_TOPIC": ""})
        os.environ.pop("STOCKSAGE_NTFY_TOPIC", None)
        print("Phone alerts are off. Nothing will be sent until you run  .\\start.bat alerts  again.")
        return 0

    if args.action == "test":
        if not topic:
            print("Alerts are not set up yet. Run  .\\start.bat alerts  first.")
            return 1
        try:
            notify._post(notify.NTFY_URL.format(topic=topic),
                         b"If you can read this, StockSage alerts work.",
                         {"Title": "StockSage test", "Tags": "white_check_mark"})
        except Exception as exc:
            print(f"The test did not go through ({exc}). Check this computer's internet and try again.")
            return 1
        print("Sent. It should appear on your phone within a few seconds.")
        print("If it did not: open the ntfy app, make sure you are subscribed to the topic below.")
        print(f"  topic: {topic}")
        return 0

    created = False
    if not topic:
        topic = "stocksage-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(16))
        envfile.save_env({"STOCKSAGE_NTFY_TOPIC": topic})
        created = True
    print("PHONE ALERTS" + ("  (just created)" if created else "  (already set up)"))
    print("=" * 50)
    print("1. On your phone, install the free app  ntfy  (App Store or Google Play).")
    print("2. Open it, tap  +  (Subscribe to topic), and type exactly:")
    print(f"\n      {topic}\n")
    print("   Leave 'Use another server' off. Allow notifications when it asks.")
    print("3. Back here, send yourself a test:")
    print("\n      .\\start.bat alerts test\n")
    print("What you will get: a loud buzz right away if a stop is breached, and ONE quiet")
    print("daily message with everything else. Messages name a ticker, an action and the")
    print("account type only: never an amount, a balance or an account number.")
    print("Treat the topic name like a password: anyone who knows it can read your alerts.")
    return 0


def cmd_analogs(args) -> int:
    """Build the look-alike history that lets a BUY be stated plainly."""
    from . import backtest
    from .analogs import AnalogBook
    from .data import MarketData

    period = f"{args.years}y"
    print(f"Building look-alike history from {args.years} years of the whole universe "
          "(a few minutes)...")
    market = MarketData()
    try:
        market.prefetch(universe.all_tickers(), period=period)
        samples = backtest.collect_samples(market, period=period)
    except RuntimeError as exc:
        print(f"Could not build it: {exc}")
        return 1
    path = AnalogBook.from_samples(samples).save()
    print(f"Done: {len(samples):,} past setups saved ({path}).\n"
          "Now  .\\start.bat today  can state a BUY plainly when history backs it.")
    return 0


def _refresh_today(engine, client, upload: bool = True, rebuild_book: bool = False) -> dict:
    """Build, save, publish and alert on today's sheet. Never raises.

    One scan feeds all of it. Returns {"sheet", "text", "error"} so callers
    that want to show it can; a scheduler can ignore the result.
    """
    out = {"sheet": None, "text": None, "error": None}
    try:
        from . import today
        from .analogs import AnalogBook
        from .notify import alert_accounts, topic_from_env

        if client is None:
            return out
        if rebuild_book:
            book = AnalogBook.load()
            if book is None or book.stale:
                try:
                    from . import backtest

                    market = engine.market
                    market.prefetch(universe.all_tickers(), period="5y")
                    book = AnalogBook.from_samples(backtest.collect_samples(market, period="5y"))
                    book.save()
                except Exception as exc:       # keep going with whatever book exists
                    out["error"] = f"look-alike refresh failed: {exc}"
        sheet = today.build_sheet(engine, client, track=True)
        text = today.render_text(sheet)
        today.save_local(sheet, text)
        out.update(sheet=sheet, text=text)
        if upload:
            try:
                from .drive_api import publish_text

                publish_text(today.DRIVE_NAME, text)
            except Exception as exc:           # Drive is optional for the sheet
                out["error"] = f"could not upload the sheet to Drive: {exc}"
        if topic_from_env():
            alert_accounts(engine.db, [(a.title, a.plan) for a in sheet.accounts if a.plan])
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def cmd_actions(args) -> int:
    """Today's sheet: blunt sells and buys for every account."""
    from . import today
    from .advisor import desktop_sync_cycle
    from .robinhood import RobinhoodClient

    engine = Engine()
    client = RobinhoodClient()
    sync = desktop_sync_cycle(engine, client=client)
    if sync.get("positions") is None:
        print(
            "Robinhood is not linked on this computer, so StockSage cannot see "
            "what you hold.\nLink it from the dashboard's Portfolio tab, then "
            "run this again."
        )
        if sync.get("sync_error"):
            print(f"(Last attempt said: {sync['sync_error']})")
        return 1

    done = _refresh_today(engine, client, upload=False)
    if done["sheet"] is None:
        print("No accounts could be read - run  .\\start.bat doctor  to see why."
              + (f"\n({done['error']})" if done["error"] else ""))
        return 1
    print(done["text"])
    print(f"\n(Saved to {today.state_dir() / 'today.txt'})")
    return 0


def cmd_backtest(args) -> int:
    """Replay history honestly and say whether the model has an edge."""
    import json
    from datetime import datetime, timezone

    from . import backtest
    from .data import MarketData
    from .db import Database

    period = f"{args.years}y"
    print(
        f"Replaying {args.years} years of history through the live scoring code.\n"
        "This downloads data for the whole universe and takes a few minutes.\n"
    )
    market = MarketData()
    try:
        market.prefetch(universe.all_tickers(), period=period)
        samples = backtest.collect_samples(market, period=period)
    except RuntimeError as exc:
        print(f"Could not run the backtest: {exc}")
        return 1
    _keep_lookalikes(samples)
    result = backtest.evaluate(samples, top_n=args.top, cost=args.cost / 100.0)
    sells = backtest.evaluate(samples, top_n=args.top, cost=args.cost / 100.0, side="sell")
    print("\n".join(backtest.describe(result, period)))
    print("\n" + "\n".join(backtest.describe(sells, period, side="sell")))

    state = Path("~/.stocksage").expanduser()
    try:
        state.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report = {"at": stamp, "years": args.years, "top_n": args.top,
                  "cost_pct": args.cost, **result.to_dict(), "sell_side": sells.to_dict()}
        (state / "backtest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        db = Database()
        db.set_meta("backtest_level", result.level)
        db.set_meta("backtest_sell_level", sells.level)
        db.set_meta("backtest_at", stamp)
        db.close()
        print(f"\n(Saved to {state / 'backtest.json'})")
    except OSError:
        pass
    return 0


def cmd_lab(args) -> int:
    """Try several ideas honestly and say whether any beats holding the market."""
    import json
    from datetime import datetime, timezone

    from . import backtest, lab
    from . import today as today_mod
    from .data import MarketData
    from .db import Database

    period = f"{args.years}y"
    print(
        f"Testing seven ideas over {args.years} years of history, each judged against\n"
        "simply holding SPY and corrected for how many were tried.\n"
        "This downloads data for the whole universe and takes several minutes.\n"
    )
    market = MarketData()
    try:
        market.prefetch(universe.all_tickers(), period=period)
        weekly = backtest.collect_samples(market, period=period)
        monthly = lab.collect_monthly(market, period=period)
    except RuntimeError as exc:
        print(f"Could not run the lab: {exc}")
        return 1
    _keep_lookalikes(weekly)

    cost = args.cost / 100.0
    hedge = backtest.evaluate(weekly, cost=cost)
    ridge = backtest.evaluate_ridge(weekly, cost=cost)
    # The app states a BUY plainly only when look-alike history backs it, so
    # that filter is itself one of the ideas under test (and counts toward the
    # family size that corrects the verdicts).
    confirmed = backtest.evaluate(weekly, cost=cost, confirm_with_analogs=True)
    family = 4 + 3
    results = lab.run_all(monthly, extra=[hedge, ridge, confirmed], cost=cost)
    for name, weekly_result in (("current model (weekly)", hedge), ("ridge challenger (weekly)", ridge),
                                (today_mod.LAB_FILTER_NAME + " (weekly)", confirmed)):
        wrapped = lab.relevel(weekly_result, family)
        wrapped.name = name
        results.append(wrapped)
    print("\n".join(lab.describe(results)))

    state = Path("~/.stocksage").expanduser()
    try:
        state.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (state / "lab.json").write_text(
            json.dumps({"at": stamp, "years": args.years, "cost_pct": args.cost,
                        "results": [r.to_dict() for r in results]}, indent=2),
            encoding="utf-8")
        winners = [r.name.removesuffix(" (weekly)") for r in results if r.level == "earned"]
        db = Database()
        db.set_meta("lab_winners", ",".join(winners))
        db.set_meta("lab_at", stamp)
        db.close()
        print(f"\n(Saved to {state / 'lab.json'})")
    except OSError:
        pass
    return 0


def _record_publish(engine, error: str | None = None) -> None:
    """Remember whether the last publish worked, inside the brain.

    Publishing runs unattended five times a day, and when it stops nothing on
    this machine says so — the first anyone knew of a four-day outage was the
    trading routine finding a stale brief on the other end. Doctor reads these
    two keys so the desktop can say it itself. Bookkeeping only: it must never
    be able to make a publish fail.
    """
    from datetime import datetime, timezone

    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if error is None:
            engine.db.set_meta("last_publish_ok", now)
        else:
            engine.db.set_meta("last_publish_error", f"{now}|{error[:300]}")
    except Exception:
        pass


def cmd_publish_drive(args) -> int:
    from .advisor import publish_brief_via_api
    from .drive_api import DriveNotConfigured

    from .robinhood import RobinhoodClient

    engine = Engine()
    _pull_drive_brain(engine)
    client = None if args.no_broker else RobinhoodClient()
    sync, focus = _sync_and_focus(engine, args.tickers, broker=not args.no_broker, client=client)
    try:
        result = publish_brief_via_api(
            engine, tickers=focus, max_price=args.max_price,
            holdings=sync.get("holdings"), buying_power=sync.get("buying_power"),
            positions=sync.get("positions"),
        )
    except DriveNotConfigured as exc:
        _record_publish(engine, error=str(exc))
        print(f"Not set up yet: {exc}")
        return 1
    except Exception as exc:
        # Still crash loudly (the scheduler's log keeps the traceback), but
        # leave a note the owner will actually see: doctor reads it.
        _record_publish(engine, error=f"{type(exc).__name__}: {exc}")
        raise
    _record_publish(engine)
    done = _refresh_today(engine, client if sync.get("positions") is not None else None,
                          rebuild_book=True)
    if done["error"]:
        print(f"(Today's sheet: {done['error']})")
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
        "  .\\start.bat security --signin 8:30am\n"
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


def cmd_leave(args) -> int:
    from . import offboard

    targets = [t for t in offboard.sensitive_targets() + offboard.shortcut_targets()
               if t.exists]
    print("\nLEAVING THIS MACHINE\n" + "-" * 62)
    if not targets:
        print("StockSage has nothing stored on this computer.")
    else:
        print("This will delete, from this computer only:\n")
        for t in targets:
            print(f"  · {t.what}")
            print(f"      {t.path}")
            print(f"      {t.why}")
    print("\nIt will also remove every StockSage scheduled job here, so nothing")
    print("runs or signs in to your account again on this machine.\n")

    brain = next(t for t in offboard.sensitive_targets() if t.what == "the brain")
    if brain.exists and offboard.brain_is_shared():
        print("Your brain lives in a cloud-synced folder, so it is not stored on")
        print("this computer and will follow you to the next one. It is left alone.\n")
    elif brain.exists and not args.keep_brain and not args.forget_brain:
        print("Your brain is on this machine — every graded call it has learned.")
        print("That took months and cannot be rebuilt. Choose one:\n")
        print("  --keep-brain <file>   save it somewhere first (a USB stick, OneDrive)")
        print("  --forget-brain        delete it; you accept losing what it learned")
        print("\nNothing has been changed.")
        return 1

    if not args.yes:
        print("Nothing has been changed. Re-run with --yes to do it:\n")
        keep = f" --keep-brain {args.keep_brain}" if args.keep_brain else (
            " --forget-brain" if args.forget_brain else "")
        print(f"  .\\start.bat leave{keep} --yes      (Windows)")
        print(f"  ./start.sh leave{keep} --yes        (Mac/Linux)")
        return 1

    result = offboard.offboard(keep_brain_at=args.keep_brain)
    if result.brain_saved_to:
        print(f"Brain saved to {result.brain_saved_to} — take that file with you.\n")
    if result.brain_left_in_sync_folder:
        print(f"Brain left in your synced folder ({result.brain_left_in_sync_folder}) —")
        print("it follows you; deleting it here would delete it everywhere.\n")
    elif args.keep_brain and result.failed:
        print("Could NOT save the brain, so nothing was deleted:")
        for f in result.failed:
            print(f"  · {f}")
        return 1
    for item in result.unscheduled:
        print(f"  removed  {item}")
    for item in result.removed:
        print(f"  deleted  {item}")
    for item in result.failed:
        print(f"  FAILED   {item}")
    print("\n" + offboard.REVOCATION_STEPS)
    if result.failed:
        print("\nSome items could not be removed — delete them by hand, then do "
              "the revocations above regardless.")
        return 1
    return 0


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def cmd_setup(args) -> int:
    """Guided first run on a new machine."""
    from . import firstrun

    print("\nSETTING UP STOCKSAGE ON THIS MACHINE\n" + "-" * 62)

    # 1. The brain — the only thing that cannot be recreated from scratch.
    if firstrun.brain_is_populated():
        print("Brain: already carries what it learned. Nothing to import.\n")
    else:
        print("This machine's brain is empty. If you exported one from your old")
        print("computer, or downloaded brain-snapshot.db from your Drive, it can")
        print("be brought in now — otherwise StockSage relearns from scratch.\n")
        candidates = firstrun.find_brain_files()
        chosen = None
        if candidates:
            print("Found:")
            for i, path in enumerate(candidates[:5], 1):
                size = path.stat().st_size / 1024
                print(f"  {i}. {path}  ({size:,.0f} KB)")
            pick = _ask("\nNumber to import, a full path, or Enter to skip: ")
            if pick.isdigit() and 1 <= int(pick) <= len(candidates[:5]):
                chosen = candidates[int(pick) - 1]
            elif pick:
                chosen = Path(pick.strip('"'))
        else:
            pick = _ask("No brain file found. Full path to one, or Enter to skip: ")
            if pick:
                chosen = Path(pick.strip('"'))
        if chosen:
            try:
                stats = firstrun.import_brain_file(chosen)
                added = stats.get("suggestions_added", 0)
                moves = stats.get("move_events_added", 0)
                print(f"\nImported: +{added} graded calls, +{moves} remembered moves.\n")
            except Exception as exc:
                print(f"\nCouldn't import that file: {exc}")
                print("Setup continues — you can retry later with 'brain import'.\n")

    # 2. The one setting that has to be in .env.
    import os

    if (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip():
        print("Routine account: already set.\n")
    else:
        print("Which Robinhood account does your trading routine act on?")
        print("(Account number, digits only. Leave blank if you don't use the")
        print("routine — you can add it later.)")
        answer = _ask("Account number: ")
        if answer:
            try:
                stored = firstrun.set_agentic_account(answer)
                print(f"Saved to .env — published briefs will name ••••{stored[-4:]}.\n")
            except ValueError as exc:
                print(f"{exc} — skipping; add it later with 'setup'.\n")

    # 3. Things only the owner can do, in the right place.
    steps = {s.key: s for s in firstrun.remaining_steps()}
    print("-" * 62)
    if not steps["robinhood"].done:
        print("\nSTILL TO DO — link Robinhood")
        print("  Run the launcher with no arguments to open StockSage, go to the")
        print("  Portfolio tab, and type your login into the form there. Type it")
        print("  into that form and nowhere else — not into this terminal, and")
        print("  never into a chat. Expect a sign-in approval on your phone;")
        print("  this is a new device.")
    if not steps["drive"].done:
        print("\nOPTIONAL — Google Drive publishing (only for the trading routine)")
        print("  See SETUP.md. Get 'publish-drive' working by hand once before")
        print("  you schedule it, or you get failing jobs with no visible error.")
    print("\nChecking everything else...\n")

    from .doctor import run_doctor

    return run_doctor()


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
    b = brain_sub.add_parser(
        "audit", help="read-only report on what is actually inside the brain"
    )
    b.set_defaults(func=cmd_brain)
    b = brain_sub.add_parser(
        "repair", help="remove duplicate owner calls (dry run unless --apply)"
    )
    b.add_argument("--apply", action="store_true", help="actually do it (backs up first)")
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

    p = sub.add_parser(
        "backtest",
        help="replay history honestly and say whether the model has an edge",
    )
    p.add_argument("--years", type=int, default=2, choices=[1, 2, 3, 5, 10],
                   help="how much history (more = a firmer answer, slower)")
    p.add_argument("--top", type=int, default=5, help="picks per period")
    p.add_argument("--cost", type=float, default=0.10,
                   help="round-trip cost per pick, in percent (default 0.10)")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser(
        "lab",
        help="try several strategies honestly: does anything beat just holding the market?",
    )
    p.add_argument("--years", type=int, default=5, choices=[3, 5, 10],
                   help="how much history (more = a firmer answer; monthly ideas need 5+)")
    p.add_argument("--cost", type=float, default=0.10,
                   help="round-trip cost per pick, in percent (default 0.10)")
    p.set_defaults(func=cmd_lab)

    p = sub.add_parser(
        "actions", aliases=["today"],
        help="today's sheet: SELL and BUY calls for every account, in plain language",
    )
    p.add_argument("tickers", nargs="*", help="extra tickers to look at")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser(
        "research",
        help="test about 60 rule-based strategies on 20 years of ETFs, corrected for luck",
    )
    p.add_argument("--draws", type=int, default=2000, help="bootstrap draws (more = steadier p-values)")
    p.add_argument("--top", type=int, default=12, help="how many candidates to list")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser(
        "alerts", help="set up phone alerts (free ntfy app): urgent now, everything else once a day"
    )
    p.add_argument("action", nargs="?", default="setup", choices=["setup", "test", "off"])
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser(
        "analogs",
        help="build the look-alike history that lets a BUY be stated plainly",
    )
    p.add_argument("--years", type=int, default=5, choices=[3, 5, 10],
                   help="how much history (more look-alikes = firmer evidence)")
    p.set_defaults(func=cmd_analogs)

    p = sub.add_parser(
        "setup", help="guided first run on a new machine: brain, settings, checks"
    )
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser(
        "leave",
        help="you are done with this computer: remove every scheduled job, "
        "credential, token and log StockSage stored on it",
    )
    p.add_argument("--keep-brain", metavar="FILE",
                   help="save the brain here before deleting it")
    p.add_argument("--forget-brain", action="store_true",
                   help="delete the brain too, accepting the loss")
    p.add_argument("--yes", action="store_true", help="actually do it")
    p.set_defaults(func=cmd_leave)

    return parser


class _DropYfinance(logging.Filter):
    """Drop yfinance's own log records at the handler.

    Setting the yfinance logger's level is not enough: it manages that level
    itself (its debug-mode helpers assign DEBUG and NOTSET), so a level we
    set at startup can be thrown away later in the run and the wall of
    "possibly delisted" errors comes back. A filter on our handler cannot be
    undone by anything yfinance does to its logger.

    These are not lost information — a name that cannot be priced is
    reported in plain language at the end of the command instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("yfinance")


def _silence_yfinance() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(_DropYfinance())
    # Belt and braces: the level alone is unreliable, but it costs nothing
    # and stops the records being formatted at all while it holds.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _report_unpriceable() -> None:
    """One plain line about names this run had to skip entirely."""
    from .data import unpriceable

    missing = unpriceable()
    if not missing:
        return
    names = ", ".join(missing)
    print(
        f"\nNote: no price data for {names} — delisted, renamed, or not carried "
        f"by Yahoo Finance."
    )
    print(
        "      Left out of this run's analysis. If you no longer follow "
        f"{missing[0]}:"
    )
    print(f"        .\\start.bat watch remove {missing[0]}      (Windows)")
    print(f"        ./start.sh watch remove {missing[0]}        (Mac/Linux)")
    print(
        "      If you hold it at your broker it is scanned automatically and "
        "cannot be removed from the watchlist — this note is just telling you "
        "it has no signals today."
    )


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
        _silence_yfinance()
    try:
        return args.func(args)
    finally:
        _report_unpriceable()


if __name__ == "__main__":
    sys.exit(main())
