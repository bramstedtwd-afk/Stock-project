import numpy as np

from stocksage.context import detect_significant_move, tag_reasons
from tests.conftest import make_ohlcv


def test_tag_reasons_taxonomy():
    headlines = [
        {"title": "Acme beats earnings expectations, raises full-year guidance"},
        {"title": "Analyst upgrades Acme to overweight with $200 price target"},
    ]
    reasons = tag_reasons(headlines)
    assert "earnings" in reasons
    assert "guidance" in reasons
    assert "analyst_rating" in reasons


def test_tag_reasons_empty_when_no_match():
    assert tag_reasons([{"title": "A quiet day on Wall Street"}]) == []


def test_detect_significant_move_fires_on_spike():
    df = make_ohlcv(days=100, daily_vol=0.006, seed=5)
    # Inject a -6% final day: far beyond both thresholds.
    df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 0.94
    detection = detect_significant_move(df)
    assert detection is not None
    date, ret, atr_mult = detection
    assert ret < -0.05
    assert atr_mult is None or atr_mult >= 2.0


def test_detect_ignores_normal_day():
    df = make_ohlcv(days=100, daily_vol=0.006, seed=6)
    df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 1.001
    assert detect_significant_move(df) is None


def test_detect_requires_history():
    assert detect_significant_move(make_ohlcv(days=5)) is None
    assert detect_significant_move(None) is None
