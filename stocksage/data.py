"""Market data access with local caching and graceful degradation.

All price data comes from Yahoo Finance via `yfinance` — free, no API key.
Downloads are cached on disk (parquet, default ~/.stocksage/cache) with a
freshness window so repeated runs within a session don't hammer the source
and the dashboard stays fast. Every consumer takes this MarketData object by
injection, which is also what makes the engine testable offline.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("~/.stocksage/cache")
CACHE_TTL_SECONDS = 4 * 3600  # refresh price history at most every 4 hours
HISTORY_PERIOD = "1y"

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE_HOUR = 16  # 4pm ET

# Earnings dates change rarely; a stale date is still a useful date.
EARNINGS_CACHE_TTL_SECONDS = 3 * 24 * 3600
# A "no date found" answer is usually the source being unreachable rather
# than a real absence, so it is retried much sooner than a found date.
EARNINGS_MISS_TTL_SECONDS = 6 * 3600


def drop_partial_bar(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop today's bar while the US market is still open.

    Every signal is defined on completed daily bars: a mid-session scan would
    otherwise judge momentum on a price that will still change and compare
    half a day's volume against full-day averages. After the close (or on a
    frame that ends on an earlier day) this is a no-op.
    """
    if df is None or df.empty:
        return df
    now = now or datetime.now(MARKET_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    now_et = now.astimezone(MARKET_TZ)
    last_day = pd.Timestamp(df.index[-1]).date()
    if last_day == now_et.date() and now_et.hour < MARKET_CLOSE_HOUR:
        return df.iloc[:-1]
    return df


def next_future_earnings(dates: list[str], today: date | None = None) -> str | None:
    """The soonest earnings date on/after today from a list of ISO dates."""
    ref = (today or date.today()).isoformat()
    future = sorted(d[:10] for d in dates if d and d[:10] >= ref)
    return future[0] if future else None


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
        """Daily OHLCV history of completed bars, ascending by date.

        None when unavailable. While the market is open, the in-progress
        session is excluded (see drop_partial_bar).
        """
        cached = self._read_cache(ticker, period)
        if cached is not None:
            return drop_partial_bar(cached)
        df = self._download(ticker, period)
        if df is not None:
            self._write_cache(ticker, period, df)
        else:
            # Fall back to stale cache rather than nothing at all.
            stale = self._read_cache(ticker, period, ignore_ttl=True)
            if stale is not None:
                log.warning("using stale cache for %s (download failed)", ticker)
                return drop_partial_bar(stale)
        return drop_partial_bar(df)

    def prefetch(self, tickers: list[str], period: str = HISTORY_PERIOD) -> int:
        """Fill the cache for many tickers in one batched download.

        One request for the whole universe instead of one per name — the
        difference between a seconds-long and a minutes-long daily scan on a
        cold cache. Purely an optimization: anything the batch misses is
        picked up by the per-ticker path in history(). Returns how many
        tickers were fetched.
        """
        missing = [t for t in tickers if self._read_cache(t, period) is None]
        if not missing:
            return 0
        try:
            import yfinance as yf

            raw = yf.download(
                missing,
                period=period,
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception as exc:
            log.warning("batch download failed (%s); falling back to per-ticker", exc)
            return 0
        if raw is None or raw.empty:
            return 0
        fetched = 0
        for ticker in missing:
            try:
                df = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = self._normalize_frame(df)
            except (KeyError, TypeError, ValueError):
                continue
            if df is None or df.empty:
                continue
            self._write_cache(ticker, period, df)
            fetched += 1
        return fetched

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

    # --- earnings calendar ---

    def earnings_date(self, ticker: str, today: date | None = None) -> str | None:
        """Next scheduled earnings date (ISO) at or after today, or None.

        Best-effort from yfinance; any failure returns None so a scan is
        never blocked by a missing/renamed calendar field.

        Answers are cached on disk for days, including the "no date found"
        answer. Earnings dates move rarely, and an uncached lookup costs a
        full network round trip per name — which on a machine where the
        calendar endpoint is slow or unreachable is what turns a ten-second
        scan into a ten-minute one.
        """
        cache = self._earnings_cache()
        if ticker in cache:
            return cache[ticker][0] or None
        value = self._fetch_earnings_date(ticker, today)
        cache[ticker] = [value or "", time.time()]
        self._save_earnings_cache(cache)
        return value

    def _fetch_earnings_date(self, ticker: str, today: date | None = None) -> str | None:
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).get_earnings_dates(limit=12)
            candidates = [str(idx.date()) for idx in df.index] if df is not None else []
        except Exception as exc:
            log.debug("earnings lookup failed for %s: %s", ticker, exc)
            return None
        return next_future_earnings(candidates, today)

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

    def _earnings_cache_path(self) -> Path:
        return self.cache_dir / "earnings_dates.json"

    def _earnings_cache(self, now: float | None = None) -> dict[str, list]:
        """Cached answers as {ticker: [iso_date_or_empty, fetched_epoch]}.

        Entries past their TTL are dropped on read, so a caller that finds a
        ticker present can trust it. Misses expire far sooner than hits: a
        found date is stable for days, but "no date found" is usually the
        calendar source being unreachable, and caching that for days keeps
        the earnings blackout switched off long after the cause is gone.
        """
        now = time.time() if now is None else now
        path = self._earnings_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        fresh: dict[str, list] = {}
        for ticker, entry in data.items():
            # Tolerate the older flat {ticker: "date"} format written before
            # per-entry timestamps existed.
            if isinstance(entry, str):
                value, fetched = entry, now
            elif isinstance(entry, list) and len(entry) == 2:
                value, fetched = entry[0], entry[1]
            else:
                continue
            if not isinstance(value, str) or not isinstance(fetched, (int, float)):
                continue
            ttl = EARNINGS_CACHE_TTL_SECONDS if value else EARNINGS_MISS_TTL_SECONDS
            if now - fetched < ttl:
                fresh[ticker] = [value, fetched]
        return fresh

    def _save_earnings_cache(self, cache: dict[str, list]) -> None:
        try:
            self._earnings_cache_path().write_text(json.dumps(cache), encoding="utf-8")
        except OSError as exc:
            log.debug("earnings cache write failed: %s", exc)

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

    @staticmethod
    def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame | None:
        if df is None or df.empty:
            return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
        if df.empty:
            return None
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df.sort_index()

    def _download(self, ticker: str, period: str) -> pd.DataFrame | None:
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:
            log.warning("download failed for %s: %s", ticker, exc)
            return None
        if df is None or df.empty:
            return None
        return self._normalize_frame(df)
