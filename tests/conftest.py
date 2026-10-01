import numpy as np
import pandas as pd
import pytest


def make_ohlcv(
    days: int = 300,
    start_price: float = 100.0,
    daily_drift: float = 0.0,
    daily_vol: float = 0.01,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic but realistic OHLCV history for offline tests."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(daily_drift, daily_vol, days)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, daily_vol / 3, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, daily_vol / 2, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, daily_vol / 2, days)))
    volume = rng.integers(1_000_000, 5_000_000, days).astype(float)
    idx = pd.bdate_range(end="2026-07-07", periods=days)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    return make_ohlcv(daily_drift=0.003, daily_vol=0.008, seed=1)


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    return make_ohlcv(daily_drift=-0.003, daily_vol=0.008, seed=2)


@pytest.fixture
def flat_df() -> pd.DataFrame:
    return make_ohlcv(daily_drift=0.0, daily_vol=0.005, seed=3)


@pytest.fixture(autouse=True)
def _state_dir_is_a_temp_dir(monkeypatch, tmp_path_factory):
    """Anything that saves under ~/.stocksage (today's sheet, the look-alike
    cache) goes to a scratch folder, so no test writes to the real one."""
    import os

    if "STOCKSAGE_STATE" not in os.environ:
        monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path_factory.mktemp("state")))
