"""Move-context learning: detect significant moves and remember *why*.

A move is "significant" when the daily return exceeds both an absolute
threshold and a multiple of the stock's own ATR (so a 3% day in a sleepy
staple registers, while the same day in a volatile chip name may not).
For each event we pull the freshest headlines, tag them against a reason
taxonomy (earnings, guidance, rating change, Fed/macro, legal, M&A, ...),
and persist the event. Over time this builds the tool's institutional
memory of what actually drives each name — browsable per ticker in the
dashboard and CLI.
"""

from __future__ import annotations

import re

from .data import MarketData
from .db import Database
from .indicators import atr

ABS_RETURN_THRESHOLD = 0.025   # 2.5% day minimum
ATR_MULTIPLE_THRESHOLD = 2.0   # and at least 2x its normal daily range

# Reason taxonomy: tag -> keyword patterns matched against headlines.
REASON_PATTERNS: dict[str, list[str]] = {
    "earnings": [r"earnings", r"\beps\b", r"quarterly results", r"revenue (beat|miss|top|fell)", r"profit"],
    "guidance": [r"guidance", r"outlook", r"forecast", r"raises? (its )?(full[- ]year|fy)", r"cuts? (its )?(full[- ]year|fy)"],
    "analyst_rating": [r"upgrade", r"downgrade", r"price target", r"initiat(es|ed) coverage", r"overweight", r"underweight"],
    "fed_macro": [r"\bfed\b", r"interest rate", r"inflation", r"\bcpi\b", r"\bjobs report\b", r"treasury yield", r"recession", r"tariff"],
    "merger_acquisition": [r"acqui(re|sition)", r"merger", r"takeover", r"buyout", r"\bdeal\b"],
    "legal_regulatory": [r"lawsuit", r"probe", r"investigation", r"antitrust", r"\bsec\b", r"\bftc\b", r"\bdoj\b", r"fine[sd]?", r"recall"],
    "product_news": [r"launch", r"unveil", r"new product", r"\bfda\b", r"approval", r"trial (results|data)", r"partnership"],
    "management": [r"\bceo\b", r"\bcfo\b", r"resign", r"steps? down", r"appoint"],
    "dividend_buyback": [r"dividend", r"buyback", r"repurchase", r"split"],
    "insider_activity": [r"insider", r"stake", r"13[df]", r"sold shares", r"bought shares"],
}


def tag_reasons(headlines: list[dict]) -> list[str]:
    """Map headlines onto the reason taxonomy; empty list = unexplained."""
    text = " ".join(h.get("title", "") for h in headlines).lower()
    reasons = []
    for tag, patterns in REASON_PATTERNS.items():
        if any(re.search(p, text) for p in patterns):
            reasons.append(tag)
    return reasons


def detect_significant_move(df) -> tuple[str, float, float | None] | None:
    """Check the most recent session. Returns (date, return, atr_multiple) or None."""
    if df is None or len(df) < 20:
        return None
    close = df["Close"]
    ret = float(close.iloc[-1] / close.iloc[-2] - 1.0)
    if abs(ret) < ABS_RETURN_THRESHOLD:
        return None
    atr_series = atr(df)
    atr_mult = None
    prev_atr = float(atr_series.iloc[-2]) if len(atr_series) >= 2 else float("nan")
    prev_close = float(close.iloc[-2])
    if prev_atr and prev_atr > 0 and prev_close > 0:
        atr_mult = abs(ret) / (prev_atr / prev_close)
        if atr_mult < ATR_MULTIPLE_THRESHOLD:
            return None
    date = str(df.index[-1].date())
    return date, ret, atr_mult


def capture_move_context(ticker: str, df, market: MarketData, db: Database) -> dict | None:
    """Detect + explain + persist a significant move for one ticker."""
    detection = detect_significant_move(df)
    if detection is None:
        return None
    date, ret, atr_mult = detection
    headlines = market.news(ticker)
    reasons = tag_reasons(headlines)
    db.record_move_event(ticker, date, ret, atr_mult, reasons, headlines)
    return {
        "ticker": ticker,
        "date": date,
        "return_pct": ret,
        "atr_multiple": atr_mult,
        "reasons": reasons or ["unexplained"],
        "headlines": headlines,
    }
