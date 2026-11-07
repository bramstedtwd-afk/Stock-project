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


_LOCAL_HOSTS = ("localhost", "::1", "0.0.0.0")


def _is_local(host) -> bool:
    return isinstance(host, str) and (host in _LOCAL_HOSTS or host.startswith("127."))


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch):
    """No test may reach the internet. On the owner's always-on PC the tests once ran with
    the real .env loaded and pushed fake "SELL AAA" alerts to the owner's phone (2026-11-06).
    Every outside connection now fails loudly; localhost (the phone page's own server) works."""
    import socket

    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def blocked(host):
        return RuntimeError(f"tests are offline: blocked a connection to {host!r}")

    def allowed(sock, address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        # A UDP connect sends nothing (mobile.lan_ip uses one to learn the LAN address).
        return _is_local(host) or sock.family == getattr(socket, "AF_UNIX", None) or sock.type == socket.SOCK_DGRAM

    def connect(self, address, *a, **k):
        if not allowed(self, address):
            raise blocked(address[0] if isinstance(address, tuple) else address)
        return real_connect(self, address, *a, **k)

    def connect_ex(self, address, *a, **k):
        if not allowed(self, address):
            raise blocked(address[0] if isinstance(address, tuple) else address)
        return real_connect_ex(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host is not None and not _is_local(host if isinstance(host, str) else host.decode()):
            raise blocked(host)
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch, tmp_path):
    """Tests never see the owner's .env, Drive token or any secret in the environment.
    `load_env()` (run by the CLI entry point) would otherwise copy the real .env into the
    process for every later test, and `save_env()` / `leave` would write or delete it."""
    import os

    from stocksage import drive_api, envfile

    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / "test.env")
    monkeypatch.setattr(drive_api, "CREDENTIALS_PATH", tmp_path / "drive_credentials.json")
    monkeypatch.setattr(drive_api, "TOKEN_PATH", tmp_path / "drive_token.json")
    for key in list(os.environ):
        if key.startswith(("STOCKSAGE_", "ROBINHOOD_")) and key != "STOCKSAGE_STATE":
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def _state_dir_is_a_temp_dir(monkeypatch, tmp_path_factory):
    """Anything that saves under ~/.stocksage (today's sheet, the look-alike
    cache) goes to a scratch folder, so no test writes to the real one."""
    import os

    if "STOCKSAGE_STATE" not in os.environ:
        monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def _no_economic_data_downloads(monkeypatch):
    """The drivers lab talks to FRED/ALFRED; no test may. Tests that need data inject it."""
    from stocksage import drivers

    _real = {"fred": drivers.load_fred, "rt": drivers.load_realtime}

    def fred(*a, **k):
        if k.get("fetch") is not None:
            return _real["fred"](*a, **k)
        raise RuntimeError("offline in tests")

    def rt(*a, **k):
        if k.get("fetch") is not None:
            return _real["rt"](*a, **k)
        raise RuntimeError("offline in tests")

    monkeypatch.setattr(drivers, "load_fred", fred)
    monkeypatch.setattr(drivers, "load_realtime", rt)


@pytest.fixture(autouse=True)
def _no_news_downloads(monkeypatch):
    """Headlines come from Google News and yfinance; no test may reach either."""
    from stocksage import news

    def offline(*a, **k):
        raise RuntimeError("offline in tests")

    monkeypatch.setattr(news, "_get", offline)
    monkeypatch.setattr(news, "lookup_name", lambda t: "")
    from stocksage import crypto

    monkeypatch.setattr(crypto, "GECKO_SPACING", 0)    # social/crypto go through news._get; no waiting offline
