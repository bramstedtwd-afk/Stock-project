"""Technical indicators, implemented directly on pandas for zero heavy deps.

Every function takes an OHLCV DataFrame (columns: Open, High, Low, Close,
Volume; DatetimeIndex ascending) and returns Series/floats aligned to it.
`compute_features` is the one entry point the rest of the engine uses: it
distills the raw history into the normalized signal set the scoring model
consumes, each signal scaled to roughly [-1, 1] where positive = bullish.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# Minimum history to compute the slowest signal (200-day trend) sensibly.
MIN_HISTORY_ROWS = 60


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window, min_periods=window).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # When there were no losses at all, RSI is 100 by definition.
    out = out.where(avg_loss != 0.0, 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(close, window)
    std = close.rolling(window, min_periods=window).std()
    return mid - num_std * std, mid, mid + num_std * std


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(min(hi, max(lo, x)))


def compute_features(df: pd.DataFrame) -> dict[str, float] | None:
    """Distill OHLCV history into normalized signals in [-1, 1].

    Returns None when there isn't enough history to be meaningful.
    Signal names are stable identifiers: the learning module keys its
    adaptive weights on them, so renaming one resets its learned weight.
    """
    if df is None or len(df) < MIN_HISTORY_ROWS:
        return None
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            raise ValueError(f"missing column {col!r} in OHLCV frame")

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    price = float(close.iloc[-1])
    if not np.isfinite(price) or price <= 0:
        return None

    features: dict[str, float] = {}

    # --- Trend: price vs long/medium moving averages ---
    sma50 = sma(close, 50).iloc[-1]
    sma200 = sma(close, 200).iloc[-1] if len(close) >= 200 else np.nan
    trend_ref = sma200 if np.isfinite(sma200) else sma50
    if np.isfinite(trend_ref) and trend_ref > 0:
        # +/-20% versus the long average saturates the signal.
        features["trend_long"] = _clip((price / trend_ref - 1.0) / 0.20)
    if np.isfinite(sma50) and sma50 > 0:
        features["trend_medium"] = _clip((price / sma50 - 1.0) / 0.10)

    # --- Momentum: 20-day rate of change ---
    if len(close) > 21:
        roc20 = price / float(close.iloc[-21]) - 1.0
        features["momentum_20d"] = _clip(roc20 / 0.10)

    # --- MACD histogram, normalized by price ---
    _, _, hist = macd(close)
    features["macd"] = _clip(float(hist.iloc[-1]) / (price * 0.01))

    # --- RSI as a mean-reversion signal: oversold -> bullish ---
    rsi_val = float(rsi(close).iloc[-1])
    if np.isfinite(rsi_val):
        features["rsi_reversion"] = _clip((50.0 - rsi_val) / 30.0)

    # --- Bollinger position: near lower band -> bullish reversion ---
    lower, mid, upper = bollinger(close)
    band_width = float(upper.iloc[-1] - lower.iloc[-1])
    if np.isfinite(band_width) and band_width > 0:
        pos = (price - float(mid.iloc[-1])) / (band_width / 2.0)
        features["bollinger_reversion"] = _clip(-pos)

    # --- Volume surge in the direction of the day's move ---
    vol20 = float(volume.rolling(20, min_periods=20).mean().iloc[-1])
    if np.isfinite(vol20) and vol20 > 0 and len(close) >= 2:
        surge = float(volume.iloc[-1]) / vol20 - 1.0
        day_direction = np.sign(price - float(close.iloc[-2]))
        features["volume_confirmation"] = _clip(day_direction * min(surge, 2.0) / 2.0)

    # --- Position in the 52-week range ---
    window = min(len(close), 252)
    lo52 = float(close.iloc[-window:].min())
    hi52 = float(close.iloc[-window:].max())
    if hi52 > lo52:
        features["range_position"] = _clip(2.0 * (price - lo52) / (hi52 - lo52) - 1.0)

    return features


def risk_metrics(df: pd.DataFrame) -> dict[str, float]:
    """Risk stats used to size and gate suggestions (not learned signals)."""
    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    atr_val = float(atr(df).iloc[-1])
    atr_pct = atr_val / price if price > 0 else float("nan")
    window = min(len(close), 252)
    recent = close.iloc[-window:]
    drawdown = price / float(recent.max()) - 1.0
    daily_ret = close.pct_change().iloc[-min(len(close), 63):]
    ann_vol = float(daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 5 else float("nan")
    return {
        "price": price,
        "atr_pct": atr_pct,
        "drawdown_52w": drawdown,
        "annualized_vol": ann_vol,
    }
