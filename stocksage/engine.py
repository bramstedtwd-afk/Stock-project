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

from . import universe
from .context import capture_move_context
from .data import MarketData
from .db import Database
from .indicators import compute_features, risk_metrics
from .learning import blended_weights, update_weights, weighted_score
from .robinhood import Portfolio, RobinhoodClient
from .scoring import SHOCK_LOOKBACK_DAYS, PastContext, Suggestion, build_suggestion

log = logging.getLogger(__name__)

SUGGESTION_HORIZON_DAYS = 5  # trading days until a suggestion is graded
# Only convictions worth acting on are recorded (and therefore learned from).
RECORD_THRESHOLD = 0.20


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

    def evaluate_pending(self, now: datetime | None = None) -> int:
        """Grade matured suggestions; update global AND sector weights per grade."""
        now = now or datetime.now(timezone.utc)
        weights = self.db.load_weights()
        sector_state: dict[str, dict[str, float]] = {}
        sector_grades: dict[str, int] = {}
        count = 0
        for row in self.db.pending_evaluations(now):
            price_now = self.market.latest_price(row["ticker"])
            if price_now is None or row["price"] <= 0:
                continue
            realized = price_now / row["price"] - 1.0
            predicted_up = row["score"] > 0
            hit = (realized > 0) == predicted_up
            self.db.mark_evaluated(row["id"], realized, hit)
            signals = json.loads(row["signals"])
            weights, detail = update_weights(weights, signals, realized)
            self.db.log_learning(row["id"], detail)
            sector = universe.sector_of(row["ticker"])
            if sector:
                sw = sector_state.setdefault(
                    sector.name, self.db.load_sector_weights(sector.name)
                )
                sector_state[sector.name], _ = update_weights(sw, signals, realized)
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
    ) -> ScanResult:
        result = ScanResult(weights=self.db.load_weights(), portfolio=portfolio)
        weights = result.weights
        if tickers is None:
            # Full coverage: the universe, the user's watchlist, and every
            # name they actually hold — a stock you own is never unwatched.
            tickers = universe.all_tickers()
            extras = self.db.watchlist()
            if portfolio is not None:
                extras = extras + [h.ticker for h in portfolio.holdings]
            for t in extras:
                if t not in tickers:
                    tickers.append(t)
        for ticker in tickers:
            df = self.market.history(ticker)
            if df is None or df.empty:
                result.errors.append(f"{ticker}: no data")
                continue
            feats = compute_features(df)
            if not feats:
                result.errors.append(f"{ticker}: insufficient history")
                continue
            score = weighted_score(feats, self.weights_for(ticker, weights))
            risk = risk_metrics(df)
            sector = universe.sector_of(ticker)
            owned = portfolio.shares_of(ticker) if portfolio else 0.0

            # Capture today's move first so a fresh shock informs today's call.
            if capture_context:
                event = capture_move_context(ticker, df, self.market, self.db)
                if event:
                    result.move_events.append(event)

            suggestion = build_suggestion(
                ticker,
                feats,
                score,
                risk,
                sector=sector.name if sector else None,
                owned_shares=owned,
                past=self._past_context(ticker),
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
        result = self.scan(portfolio=portfolio)
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
