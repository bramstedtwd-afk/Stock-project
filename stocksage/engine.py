"""The engine: scan → score → learn → suggest, in one orchestrator.

`daily_run` is the heartbeat: evaluate matured past suggestions (this is
where the weights learn), scan the whole universe with the freshly-updated
weights, capture move context, and record today's suggestions so they in
turn become tomorrow's training data. Run it once per day after the close —
manually, by cron, or from the dashboard's Refresh button.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from . import universe
from .context import capture_move_context
from .data import MarketData
from .db import Database
from .indicators import compute_features, risk_metrics
from .learning import blended_weights, update_weights, weighted_score
from .robinhood import Portfolio, RobinhoodClient
from .scoring import (
    SHOCK_LOOKBACK_DAYS,
    PastContext,
    Suggestion,
    apply_sector_caps,
    build_suggestion,
)

log = logging.getLogger(__name__)

SUGGESTION_HORIZON_DAYS = 5  # trading days until a suggestion is graded
# Only convictions worth acting on are recorded (and therefore learned from).
RECORD_THRESHOLD = 0.20
# If history still can't resolve the exact horizon after this many multiples
# of it in calendar days (data outage, delisting), grade with what we have.
STALE_GRADE_MULTIPLE = 3


@dataclass
class ScanResult:
    suggestions: list[Suggestion] = field(default_factory=list)
    move_events: list[dict] = field(default_factory=list)
    sector_trends: dict[str, float] = field(default_factory=dict)
    evaluated_count: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    portfolio: Portfolio | None = None
    errors: list[str] = field(default_factory=list)
    bootstrap_stats: dict | None = None  # set when this run did first-run bootstrap
    rh_sync: dict | None = None          # set when Robinhood history was mirrored

    @property
    def actionable(self) -> list[Suggestion]:
        return [s for s in self.suggestions if s.action != "HOLD"]


class Engine:
    def __init__(self, db: Database | None = None, market: MarketData | None = None):
        self.db = db or Database()
        self.market = market or MarketData()

    # --- learning loop ---

    def _resolve_outcome(
        self, row, now: datetime
    ) -> tuple[float, str | None] | None:
        """The stock's return over the suggestion's TRUE trading-day horizon.

        Returns (realized_return, exit_date) — exit_date is the bar the
        window closed on, for benchmark alignment — or None to leave the row
        pending. Grading at whatever bar happens to be latest would silently
        stretch a 5-day call into a 2-week one whenever runs are skipped,
        and the weights would learn from a horizon the signals were never
        scored for.
        """
        if row["price"] <= 0:
            return None
        created_date = row["created_at"][:10]
        df = self.market.history(row["ticker"])
        horizon = int(row["horizon_days"])
        if df is not None and not df.empty:
            entry_pos = int(df.index.searchsorted(pd.Timestamp(created_date), side="right")) - 1
            exit_pos = entry_pos + horizon
            if entry_pos >= 0 and exit_pos < len(df):
                exit_close = float(df["Close"].iloc[exit_pos])
                if exit_close > 0:
                    return exit_close / row["price"] - 1.0, str(df.index[exit_pos].date())
            # Post-call bars exist but the horizon hasn't been reached: wait,
            # unless the row is so old the data has clearly stopped coming.
            has_post_call_bars = len(df) - 1 > max(entry_pos, -1)
            created_dt = datetime.fromisoformat(row["created_at"])
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_days = (now - created_dt).days
            if has_post_call_bars and age_days < horizon * STALE_GRADE_MULTIPLE:
                return None
        # Degraded path (no usable history): the old latest-price grading.
        price_now = self.market.latest_price(row["ticker"])
        if price_now is None:
            return None
        return price_now / row["price"] - 1.0, None

    def _benchmark_return(
        self, created_date: str, exit_date: str | None
    ) -> float | None:
        """SPY's return over the same window, so calls are judged on edge
        over the market rather than on the tide that lifts every boat."""
        bench = self.market.history(universe.MARKET_BENCHMARK)
        if bench is None or bench.empty:
            return None
        entry_pos = int(bench.index.searchsorted(pd.Timestamp(created_date), side="right")) - 1
        if entry_pos < 0:
            return None
        if exit_date is None:
            exit_pos = len(bench) - 1
        else:
            exit_pos = int(bench.index.searchsorted(pd.Timestamp(exit_date), side="right")) - 1
        if exit_pos <= entry_pos:
            return None
        entry, exit_ = float(bench["Close"].iloc[entry_pos]), float(bench["Close"].iloc[exit_pos])
        if entry <= 0:
            return None
        return exit_ / entry - 1.0

    def evaluate_pending(self, now: datetime | None = None) -> int:
        """Grade matured suggestions; update global AND sector weights per grade.

        The ledger keeps the raw return (that is real money), but the weights
        learn from the SPY-excess return: a +2% week when the whole market
        rose 3% is a losing call, and rewarding it teaches permanent bullishness.
        """
        now = now or datetime.now(timezone.utc)
        weights = self.db.load_weights()
        sector_state: dict[str, dict[str, float]] = {}
        sector_grades: dict[str, int] = {}
        count = 0
        for row in self.db.pending_evaluations(now):
            outcome = self._resolve_outcome(row, now)
            if outcome is None:
                continue
            realized, exit_date = outcome
            benchmark = self._benchmark_return(row["created_at"][:10], exit_date)
            excess = realized - benchmark if benchmark is not None else realized
            predicted_up = row["score"] > 0
            hit = (realized > 0) == predicted_up
            self.db.mark_evaluated(row["id"], realized, hit, benchmark)
            signals = json.loads(row["signals"])
            weights, detail = update_weights(weights, signals, excess)
            detail["raw_return"] = realized
            detail["benchmark_return"] = benchmark
            self.db.log_learning(row["id"], detail)
            sector = universe.sector_of(row["ticker"])
            if sector:
                sw = sector_state.setdefault(
                    sector.name, self.db.load_sector_weights(sector.name)
                )
                sector_state[sector.name], _ = update_weights(sw, signals, excess)
                sector_grades[sector.name] = sector_grades.get(sector.name, 0) + 1
            count += 1
        if count:
            self.db.save_weights(weights)
            for name, sw in sector_state.items():
                if sw:
                    self.db.save_sector_weights(name, sw)
            for name, n in sector_grades.items():
                key = f"sector_grades:{name}"
                self.db.set_meta(key, str(int(self.db.get_meta(key, "0")) + n))
            log.info("evaluated %d matured suggestions; weights updated", count)
        return count

    def _past_context(self, ticker: str) -> PastContext:
        """What the brain remembers about this name, for the scoring model."""
        graded, hit_rate, _ = self.db.ticker_track_record(ticker)
        today = datetime.now(timezone.utc).date()
        recent = None
        events_12mo = 0
        for row in self.db.move_events(ticker, limit=100):
            try:
                event_date = datetime.strptime(row["event_date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            age = (today - event_date).days
            if age <= 365:
                events_12mo += 1
            if age <= SHOCK_LOOKBACK_DAYS and recent is None:
                recent = {
                    "date": row["event_date"],
                    "return_pct": row["return_pct"],
                    "reasons": json.loads(row["reasons"]),
                }
        return PastContext(
            graded_calls=graded,
            hit_rate=hit_rate,
            recent_event=recent,
            events_12mo=events_12mo,
        )

    def _days_to_earnings(self, ticker: str) -> int | None:
        """Calendar days until the next earnings print, if the data source
        exposes it. Never raises — earnings info is a bonus, not a dependency."""
        getter = getattr(self.market, "earnings_date", None)
        if getter is None:
            return None
        try:
            iso = getter(ticker)
        except Exception:
            return None
        if not iso:
            return None
        try:
            edate = datetime.strptime(iso[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return max(0, (edate - datetime.now(timezone.utc).date()).days)

    def weights_for(self, ticker: str, global_weights: dict[str, float]) -> dict[str, float]:
        """Effective weights for one name: global blended with its sector's."""
        sector = universe.sector_of(ticker)
        if sector is None:
            return global_weights
        grades = int(self.db.get_meta(f"sector_grades:{sector.name}", "0"))
        return blended_weights(
            global_weights, self.db.load_sector_weights(sector.name), grades
        )

    # --- scanning ---

    def sector_trends(self) -> dict[str, float]:
        """Each sector ETF's risk-unadjusted composite score (trend health)."""
        weights = self.db.load_weights()
        trends = {}
        for sector in universe.SECTORS:
            df = self.market.history(sector.etf)
            feats = compute_features(df) if df is not None else None
            if feats:
                trends[sector.name] = round(weighted_score(feats, weights), 4)
        return trends

    def scan(
        self,
        tickers: list[str] | None = None,
        portfolio: Portfolio | None = None,
        capture_context: bool = True,
        record: bool = True,
        extra_tickers: list[str] | None = None,
    ) -> ScanResult:
        result = ScanResult(weights=self.db.load_weights(), portfolio=portfolio)
        weights = result.weights
        if tickers is None:
            # Full coverage: the universe, the user's watchlists (local and
            # Robinhood), and every name they actually hold — a stock you
            # own is never unwatched.
            tickers = universe.all_tickers()
            extras = self.db.watchlist() + list(extra_tickers or [])
            if portfolio is not None:
                extras = extras + [h.ticker for h in portfolio.holdings]
            for t in extras:
                if t not in tickers:
                    tickers.append(t)

        # One batched request for everything the scan will touch (optional
        # market capability — fakes without it just serve per-ticker).
        prefetch = getattr(self.market, "prefetch", None)
        if callable(prefetch):
            etfs = [s.etf for s in universe.SECTORS]
            prefetch(list(tickers) + etfs + [universe.MARKET_BENCHMARK])

        etf_frames: dict[str, pd.DataFrame | None] = {}
        for ticker in tickers:
            df = self.market.history(ticker)
            if df is None or df.empty:
                result.errors.append(f"{ticker}: no data")
                continue
            sector = universe.sector_of(ticker)
            benchmark_df = None
            if sector is not None:
                if sector.etf not in etf_frames:
                    etf_frames[sector.etf] = self.market.history(sector.etf)
                benchmark_df = etf_frames[sector.etf]
            feats = compute_features(df, benchmark_df=benchmark_df)
            if not feats:
                result.errors.append(f"{ticker}: insufficient history")
                continue
            score = weighted_score(feats, self.weights_for(ticker, weights))
            risk = risk_metrics(df)
            owned = portfolio.shares_of(ticker) if portfolio else 0.0

            # Capture today's move first so a fresh shock informs today's call.
            if capture_context:
                event = capture_move_context(ticker, df, self.market, self.db)
                if event:
                    result.move_events.append(event)

            past = self._past_context(ticker)
            suggestion = build_suggestion(
                ticker,
                feats,
                score,
                risk,
                sector=sector.name if sector else None,
                owned_shares=owned,
                past=past,
            )
            # The earnings gate only changes buy-side sizing and the warning on
            # names already held, so the calendar is consulted only for those.
            # On a cold cache that is ~15 lookups per scan instead of one per
            # name in the universe — the difference between a fast scan and a
            # slow one when the calendar endpoint is unreachable.
            if suggestion.action in ("BUY", "STRONG BUY") or owned > 0:
                earnings_days = self._days_to_earnings(ticker)
                if earnings_days is not None:
                    suggestion = build_suggestion(
                        ticker,
                        feats,
                        score,
                        risk,
                        sector=sector.name if sector else None,
                        owned_shares=owned,
                        past=past,
                        earnings_days=earnings_days,
                    )
            result.suggestions.append(suggestion)

            if record and abs(suggestion.risk_adjusted_score) >= RECORD_THRESHOLD:
                self.db.record_suggestion(
                    ticker,
                    suggestion.action,
                    suggestion.risk_adjusted_score,
                    suggestion.price,
                    feats,
                    SUGGESTION_HORIZON_DAYS,
                )

        result.suggestions.sort(key=lambda s: s.risk_adjusted_score, reverse=True)
        apply_sector_caps(result.suggestions)
        return result

    # --- the daily heartbeat ---

    def daily_run(self, with_robinhood: bool = True) -> ScanResult:
        bootstrap_stats = None
        if self.db.get_meta("bootstrap_done") is None:
            from .bootstrap import bootstrap

            log.info("first run: bootstrapping from two years of history")
            bootstrap_stats = bootstrap(self.market, self.db)
        evaluated = self.evaluate_pending()
        portfolio = None
        rh_sync = None
        rh_watch: list[str] | None = None
        if with_robinhood:
            client = RobinhoodClient()
            portfolio = client.portfolio()  # None when creds absent/invalid
            if portfolio is not None:
                # Mirror the full account history; idempotent, only new
                # activity lands. Failures must never block the scan.
                try:
                    rh_sync = client.sync_history(self.db)
                except Exception as exc:
                    log.warning("Robinhood history sync failed: %s", exc)
                # Names starred in the Robinhood app get scanned too — the
                # watchlist there and the one here should feel like one list.
                try:
                    rh_watch = client.watchlist_tickers()
                except Exception as exc:
                    log.warning("Robinhood watchlist fetch failed: %s", exc)
        result = self.scan(portfolio=portfolio, extra_tickers=rh_watch)
        result.rh_sync = rh_sync
        result.evaluated_count = evaluated
        result.sector_trends = self.sector_trends()
        result.weights = self.db.load_weights()
        result.bootstrap_stats = bootstrap_stats
        self.db.set_meta("last_daily_run", datetime.now(timezone.utc).date().isoformat())
        # Device stamp: with a shared brain, every device can see who ran last.
        self.db.set_meta("last_device", socket.gethostname())
        self.db.set_meta(
            "last_device_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        return result
