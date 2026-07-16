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

# --- past-context adjustments ---
# The model consults its own graded record on a name before trusting itself.
RELIABILITY_MIN_CALLS = 5          # need this many grades before self-adjusting
RELIABILITY_FLOOR = 0.60           # worst-case confidence shrink
RELIABILITY_CEIL = 1.25            # best-case confidence boost
SHOCK_LOOKBACK_DAYS = 5            # a big move this recent tempers conviction
SHOCK_DAMPING = 0.75
EVENT_PRONE_THRESHOLD = 10         # outsized moves in 12mo that flag a name jumpy
EVENT_PRONE_SIZING = 0.75
# A new entry this close to an earnings print risks gapping through the stop.
EARNINGS_BLACKOUT_DAYS = 3

# Risk budget per position: cap the suggested allocation of investable cash.
MAX_POSITION_FRACTION = 0.10
# ATR multiple used for the protective stop suggestion.
STOP_ATR_MULTIPLE = 2.0


@dataclass
class PastContext:
    """What the brain remembers about a name, fed into its next suggestion."""

    graded_calls: int = 0
    hit_rate: float | None = None      # model's own accuracy on this name
    recent_event: dict | None = None   # {date, return_pct, reasons} within lookback
    events_12mo: int = 0               # outsized moves in the past year


def reliability_multiplier(graded_calls: int, hit_rate: float | None) -> float:
    """Confidence scale from the model's own record on this name.

    Neutral (1.0) at a 50% hit rate; a name it keeps reading correctly earns
    up to +25% conviction, a name it keeps misreading loses up to 40%. Below
    the minimum sample size it stays neutral — five calls is opinion, not
    evidence, but it beats never checking.
    """
    if graded_calls < RELIABILITY_MIN_CALLS or hit_rate is None:
        return 1.0
    return min(RELIABILITY_CEIL, max(RELIABILITY_FLOOR, 0.6 + 0.8 * hit_rate))


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
    earnings_days: int | None = None  # calendar days to next earnings, if known
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
    past: PastContext | None = None,
    earnings_days: int | None = None,
) -> Suggestion:
    vol = risk.get("annualized_vol")
    vol_mult = risk_multiplier(vol)
    mult = vol_mult
    price = risk.get("price", float("nan"))
    atr_pct = risk.get("atr_pct")

    notes: list[str] = []
    if vol_mult < 0.6:
        notes.append(
            f"High volatility ({vol:.0%} annualized) — conviction reduced accordingly."
        )
    sizing_mult = 1.0
    if past is not None:
        rel = reliability_multiplier(past.graded_calls, past.hit_rate)
        if rel != 1.0:
            mult *= rel
            hits = round(past.hit_rate * past.graded_calls)
            verdict = "boosted" if rel > 1.0 else "reduced"
            notes.append(
                f"Model's own record on {ticker}: {hits}/{past.graded_calls} calls "
                f"right — confidence {verdict} x{rel:.2f}."
            )
        if past.recent_event:
            mult *= SHOCK_DAMPING
            ev = past.recent_event
            reasons = ", ".join(ev.get("reasons") or ["unexplained"])
            notes.append(
                f"Recent shock {ev['date']}: {ev['return_pct']:+.1%} [{reasons}] — "
                "conviction tempered while it settles."
            )
        if past.events_12mo >= EVENT_PRONE_THRESHOLD:
            sizing_mult = EVENT_PRONE_SIZING
            notes.append(
                f"Event-prone name ({past.events_12mo} outsized moves in 12mo) — "
                "suggested size reduced."
            )

    ras = score * mult
    action = classify(ras)
    drawdown = risk.get("drawdown_52w")
    if drawdown is not None and drawdown < -0.30:
        notes.append(f"Trading {abs(drawdown):.0%} below its 52-week high.")
    if owned_shares > 0 and action in ("SELL", "STRONG SELL"):
        notes.append("You currently hold this position — sell signal is actionable.")
    if owned_shares == 0 and action in ("SELL", "STRONG SELL"):
        notes.append("You do not hold this — treat as an avoid, not a trade.")

    earnings_soon = (
        earnings_days is not None and 0 <= earnings_days <= EARNINGS_BLACKOUT_DAYS
    )

    stop = None
    fraction = 0.0
    if action in ("BUY", "STRONG BUY"):
        if earnings_soon:
            # Don't open a fresh position right before a print — a surprise
            # can gap straight through the protective stop.
            notes.append(
                f"Earnings in {earnings_days} day(s) — new entry on hold; a "
                "surprise can gap through the stop. Revisit after the print."
            )
        else:
            fraction = round(position_size(ras, atr_pct) * sizing_mult, 4)
            if atr_pct is not None and math.isfinite(atr_pct) and math.isfinite(price):
                stop = round(price * (1.0 - STOP_ATR_MULTIPLE * atr_pct), 2)
                notes.append(f"Suggested protective stop near ${stop:,.2f} (2x ATR).")
    elif earnings_soon and owned_shares > 0:
        notes.append(
            f"You hold this and earnings are in {earnings_days} day(s) — decide "
            "before the print whether to hold through the risk or trim."
        )

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
        earnings_days=earnings_days,
        notes=notes,
    )
