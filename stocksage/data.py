"""Market data access with local caching and graceful degradation.

All price data comes from Yahoo Finance via `yfinance` — free, no API key.
Downloads are cached on disk (parquet, default ~/.stocksage/cache) with a
freshness window so repeated runs within a session don't hammer the source
and the dashboard stays fast. Every consumer takes this MarketData object by
injection, which is also what makes the engine testable offline.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("~/.stocksage/cache")
CACHE_TTL_SECONDS = 4 * 3600  # refresh price history at most every 4 hours
HISTORY_PERIOD = "1y"


def parse_news_items(raw: list, limit: int) -> list[dict]:
    """Normalize yfinance news entries across its schema generations.

    Old schema: flat {title, publisher, link, providerPublishTime}.
    New schema (>=0.2.50): nested {content: {title, provider: {displayName},
    canonicalUrl: {url}, pubDate}}. Anything unrecognizable is skipped —
    a schema surprise must never kill a scan.
    """
    items = []
    for entry in raw[:limit]:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", entry)
        if not isinstance(content, dict):
            continue
        provider = content.get("provider")
        publisher = (
            provider.get("displayName", "")
            if isinstance(provider, dict)
            else content.get("publisher", "")
        )
        canonical = content.get("canonicalUrl")
        link = (
            canonical.get("url", "")
            if isinstance(canonical, dict)
            else content.get("link", "")
        )
        items.append(
            {
                "title": content.get("title") or "",
                "publisher": publisher or "",
                "link": link or "",
                "published": content.get("pubDate", content.get("providerPublishTime", "")),
            }
        )
    return [i for i in items if i["title"]]


class MarketData:
    def __init__(self, cache_dir: str | Path | None = None, ttl: int = CACHE_TTL_SECONDS):
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    # --- price history ---

    def history(self, ticker: str, period: str = HISTORY_PERIOD) -> pd.DataFrame | None:
        """Daily OHLCV history, ascending by date. None when unavailable."""
        cached = self._read_cache(ticker, period)
        if cached is not None:
            return cached
        df = self._download(ticker, period)
        if df is not None:
            self._write_cache(ticker, period, df)
        else:
            # Fall back to stale cache rather than nothing at all.
            stale = self._read_cache(ticker, period, ignore_ttl=True)
            if stale is not None:
                log.warning("using stale cache for %s (download failed)", ticker)
                return stale
        return df

    def histories(self, tickers: list[str], period: str = HISTORY_PERIOD) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for t in tickers:
            df = self.history(t, period)
            if df is not None and not df.empty:
                out[t] = df
        return out

    def latest_price(self, ticker: str) -> float | None:
        df = self.history(ticker)
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])

    # --- news ---

    def news(self, ticker: str, limit: int = 8) -> list[dict]:
        """Recent headlines: [{title, publisher, link, published}]."""
        try:
            import yfinance as yf

            raw = yf.Ticker(ticker).news or []
        except Exception as exc:  # network or schema failures must not kill a scan
            log.warning("news fetch failed for %s: %s", ticker, exc)
            return []
        return parse_news_items(raw, limit)

    # --- internals ---

    def _cache_path(self, ticker: str, period: str) -> Path:
        safe = ticker.replace("/", "_")
        return self.cache_dir / f"{safe}_{period}.parquet"

    def _read_cache(self, ticker: str, period: str, ignore_ttl: bool = False) -> pd.DataFrame | None:
        path = self._cache_path(ticker, period)
        if not path.exists():
            return None
        if not ignore_ttl and time.time() - path.stat().st_mtime > self.ttl:
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _write_cache(self, ticker: str, period: str, df: pd.DataFrame) -> None:
        try:
            df.to_parquet(self._cache_path(ticker, period))
        except Exception as exc:
            log.debug("cache write failed for %s: %s", ticker, exc)

    def _download(self, ticker: str, period: str) -> pd.DataFrame | None:
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:
            log.warning("download failed for %s: %s", ticker, exc)
            return None
        if df is None or df.empty:
            return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df.sort_index()
