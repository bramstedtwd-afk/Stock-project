import numpy as np
import pandas as pd

from stocksage.indicators import (
    atr,
    compute_features,
    macd,
    risk_metrics,
    rsi,
    sma,
)
from tests.conftest import make_ohlcv


def test_sma_matches_manual():
    s = pd.Series(np.arange(1.0, 11.0))
    assert sma(s, 5).iloc[-1] == 8.0  # mean of 6..10


def test_rsi_bounds_and_direction(uptrend_df, downtrend_df):
    up = rsi(uptrend_df["Close"]).iloc[-1]
    down = rsi(downtrend_df["Close"]).iloc[-1]
    assert 0 <= up <= 100 and 0 <= down <= 100
    assert up > down


def test_rsi_all_gains_is_100():
    s = pd.Series(np.linspace(100, 200, 60))
    assert rsi(s).iloc[-1] == 100.0


def test_macd_positive_in_uptrend(uptrend_df):
    line, signal, hist = macd(uptrend_df["Close"])
    assert line.iloc[-1] > 0


def test_atr_positive(uptrend_df):
    val = atr(uptrend_df).iloc[-1]
    assert val > 0


def test_features_direction(uptrend_df, downtrend_df):
    up = compute_features(uptrend_df)
    down = compute_features(downtrend_df)
    assert up["trend_long"] > 0 > down["trend_long"]
    assert up["momentum_20d"] > down["momentum_20d"]
    # Every signal must be normalized.
    for feats in (up, down):
        for name, v in feats.items():
            assert -1.0 <= v <= 1.0, f"{name}={v} out of range"


def test_features_insufficient_history_returns_none():
    assert compute_features(make_ohlcv(days=10)) is None
    assert compute_features(None) is None


def test_risk_metrics_sane(uptrend_df):
    r = risk_metrics(uptrend_df)
    assert r["price"] > 0
    assert 0 < r["atr_pct"] < 0.2
    assert r["drawdown_52w"] <= 0
    assert r["annualized_vol"] > 0


def test_relative_strength_signal_only_with_benchmark(uptrend_df, downtrend_df, flat_df):
    from stocksage.indicators import compute_features

    solo = compute_features(uptrend_df)
    assert "relative_strength_20d" not in solo

    beating = compute_features(uptrend_df, benchmark_df=flat_df)
    lagging = compute_features(downtrend_df, benchmark_df=flat_df)
    assert beating["relative_strength_20d"] > 0
    assert lagging["relative_strength_20d"] < 0
    assert beating["relative_strength_20d"] > lagging["relative_strength_20d"]
