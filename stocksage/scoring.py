"""Turn a composite score plus risk metrics into an actionable suggestion.

Philosophy: maximize growth *subject to* staying safe. The raw score decides
direction; risk metrics gate and size it. A hot momentum name with 60%
annualized volatility gets a smaller suggested position than a steady
compounder with the same score — that is what keeps a growth-seeking tool
from quietly becoming a lottery ticket dispenser.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

STRONG_BUY_THRESHOLD = 0.45
BUY_THRESHOLD = 0.20
SELL_THRESHOLD = -0.20
STRONG_SELL_THRESHOLD = -0.45

# Volatility above this gets fully penalized; below ~15% is considered calm.
HIGH_VOL = 0.60
CALM_VOL = 0.15

# Risk budget per position: cap the suggested allocation of investable cash.
MAX_POSITION_FRACTION = 0.10
# ATR multiple used for the protective stop suggestion.
STOP_ATR_MULTIPLE = 2.0


@dataclass
class Suggestion:
    ticker: str
    action: str
    score: float
    risk_adjusted_score: float
    price: float
    signals: dict[str, float]
    risk: dict[str, float]
    sector: str | None = None
    position_fraction: float = 0.0  # suggested fraction of investable cash
    stop_price: float | None = None
    owned_shares: float = 0.0
    notes: list[str] = field(default_factory=list)


def risk_multiplier(annualized_vol: float | None) -> float:
    """Scale in (0.35, 1.0]: calm names keep their score, volatile ones shrink."""
    if annualized_vol is None or not math.isfinite(annualized_vol):
        return 0.7  # unknown risk earns a haircut, not a pass
    if annualized_vol <= CALM_VOL:
        return 1.0
    if annualized_vol >= HIGH_VOL:
        return 0.35
    span = HIGH_VOL - CALM_VOL
    return 1.0 - 0.65 * (annualized_vol - CALM_VOL) / span


def classify(risk_adjusted_score: float) -> str:
    if risk_adjusted_score >= STRONG_BUY_THRESHOLD:
        return "STRONG BUY"
    if risk_adjusted_score >= BUY_THRESHOLD:
        return "BUY"
    if risk_adjusted_score <= STRONG_SELL_THRESHOLD:
        return "STRONG SELL"
    if risk_adjusted_score <= SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def position_size(risk_adjusted_score: float, atr_pct: float | None) -> float:
    """Suggested fraction of investable cash for a buy-side idea.

    Grows with conviction, shrinks with daily volatility (ATR%), and is
    hard-capped so no single suggestion dominates the portfolio.
    """
    if risk_adjusted_score < BUY_THRESHOLD:
        return 0.0
    conviction = min(risk_adjusted_score / STRONG_BUY_THRESHOLD, 1.0)
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        atr_scale = 0.5
    else:
        # 1% daily ATR is normal; 4%+ is wild.
        atr_scale = max(0.25, min(1.0, 0.01 / atr_pct))
    return round(MAX_POSITION_FRACTION * conviction * atr_scale, 4)


def build_suggestion(
    ticker: str,
    signals: dict[str, float],
    score: float,
    risk: dict[str, float],
    sector: str | None = None,
    owned_shares: float = 0.0,
) -> Suggestion:
    vol = risk.get("annualized_vol")
    mult = risk_multiplier(vol)
    ras = score * mult
    action = classify(ras)
    price = risk.get("price", float("nan"))
    atr_pct = risk.get("atr_pct")

    notes: list[str] = []
    if mult < 0.6:
        notes.append(
            f"High volatility ({vol:.0%} annualized) — conviction reduced accordingly."
        )
    drawdown = risk.get("drawdown_52w")
    if drawdown is not None and drawdown < -0.30:
        notes.append(f"Trading {abs(drawdown):.0%} below its 52-week high.")
    if owned_shares > 0 and action in ("SELL", "STRONG SELL"):
        notes.append("You currently hold this position — sell signal is actionable.")
    if owned_shares == 0 and action in ("SELL", "STRONG SELL"):
        notes.append("You do not hold this — treat as an avoid, not a trade.")

    stop = None
    fraction = 0.0
    if action in ("BUY", "STRONG BUY"):
        fraction = position_size(ras, atr_pct)
        if atr_pct is not None and math.isfinite(atr_pct) and math.isfinite(price):
            stop = round(price * (1.0 - STOP_ATR_MULTIPLE * atr_pct), 2)
            notes.append(f"Suggested protective stop near ${stop:,.2f} (2x ATR).")

    return Suggestion(
        ticker=ticker,
        action=action,
        score=round(score, 4),
        risk_adjusted_score=round(ras, 4),
        price=price,
        signals=signals,
        risk=risk,
        sector=sector,
        position_fraction=fraction,
        stop_price=stop,
        owned_shares=owned_shares,
        notes=notes,
    )
