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
import sys

from . import universe
from .db import Database
from .engine import Engine
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
        for note in s.notes:
            print(f"        · {note}")
    print(DISCLAIMER)


def cmd_daily(args) -> int:
    engine = Engine()
    print("Running daily cycle: evaluate -> learn -> scan -> suggest ...")
    result = engine.daily_run(with_robinhood=not args.no_robinhood)
    print(f"\nGraded {result.evaluated_count} matured suggestions (weights updated).")
    if result.portfolio:
        print(
            f"Robinhood linked: {len(result.portfolio.holdings)} holdings, "
            f"${result.portfolio.buying_power:,.2f} buying power."
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


def cmd_performance(args) -> int:
    db = Database()
    summary = db.performance_summary()
    weights = db.load_weights()
    print("\nLearning status")
    print("-" * 40)
    print(f"Suggestions graded : {summary['evaluated']}")
    if summary["evaluated"]:
        print(f"Direction hit rate : {summary['hit_rate']:.0%}")
        print(f"Avg realized return: {_fmt_pct(summary['avg_return'], 2)}")
    if weights:
        print("\nCurrent signal weights (learned):")
        for name, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<24}{w:.3f}  {'#' * int(w * 40)}")
    else:
        print("\nWeights still at uniform defaults — grading begins after the first")
        print("suggestions mature (~1 week of daily runs).")
    print()
    return 0


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

    p = sub.add_parser("performance", help="learning status and signal weights")
    p.set_defaults(func=cmd_performance)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
