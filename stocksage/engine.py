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
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import universe
from .context import capture_move_context
from .data import MarketData
from .db import Database
from .indicators import compute_features, risk_metrics
from .learning import update_weights, weighted_score
from .robinhood import Portfolio, RobinhoodClient
from .scoring import Suggestion, build_suggestion

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

    @property
    def actionable(self) -> list[Suggestion]:
        return [s for s in self.suggestions if s.action != "HOLD"]


class Engine:
    def __init__(self, db: Database | None = None, market: MarketData | None = None):
        self.db = db or Database()
        self.market = market or MarketData()

    # --- learning loop ---

    def evaluate_pending(self, now: datetime | None = None) -> int:
        """Grade matured suggestions and apply one weight update per grade."""
        now = now or datetime.now(timezone.utc)
        weights = self.db.load_weights()
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
            count += 1
        if count:
            self.db.save_weights(weights)
            log.info("evaluated %d matured suggestions; weights updated", count)
        return count

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
        for ticker in tickers or universe.all_tickers():
            df = self.market.history(ticker)
            if df is None or df.empty:
                result.errors.append(f"{ticker}: no data")
                continue
            feats = compute_features(df)
            if not feats:
                result.errors.append(f"{ticker}: insufficient history")
                continue
            score = weighted_score(feats, weights)
            risk = risk_metrics(df)
            sector = universe.sector_of(ticker)
            owned = portfolio.shares_of(ticker) if portfolio else 0.0
            suggestion = build_suggestion(
                ticker,
                feats,
                score,
                risk,
                sector=sector.name if sector else None,
                owned_shares=owned,
            )
            result.suggestions.append(suggestion)

            if capture_context:
                event = capture_move_context(ticker, df, self.market, self.db)
                if event:
                    result.move_events.append(event)

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
        if with_robinhood:
            client = RobinhoodClient()
            portfolio = client.portfolio()  # None when creds absent/invalid
        result = self.scan(portfolio=portfolio)
        result.evaluated_count = evaluated
        result.sector_trends = self.sector_trends()
        result.weights = self.db.load_weights()
        result.bootstrap_stats = bootstrap_stats
        self.db.set_meta("last_daily_run", datetime.now(timezone.utc).date().isoformat())
        return result
