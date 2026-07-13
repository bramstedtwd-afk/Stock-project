"""Robinhood connection — READ-ONLY by deliberate design.

v1 pulls your holdings and buying power so suggestions are portfolio-aware
(sell signals on names you own are flagged actionable; position sizing knows
your cash). It never places, modifies, or cancels orders. Auto-trading should
only be considered after the learning engine has a measured live track
record — and even then behind an explicit, separate opt-in.

Credentials come from the environment (see .env.example):
    ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, ROBINHOOD_MFA_SECRET (optional TOTP)
They are read at call time and never written to disk by this module.
Requires: pip install robin_stocks pyotp
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Holding:
    ticker: str
    shares: float
    avg_buy_price: float
    current_price: float
    equity: float


@dataclass
class Portfolio:
    holdings: list[Holding]
    buying_power: float

    def shares_of(self, ticker: str) -> float:
        for h in self.holdings:
            if h.ticker == ticker:
                return h.shares
        return 0.0

    @property
    def total_equity(self) -> float:
        return sum(h.equity for h in self.holdings)


def extract_watchlist_symbols(payload) -> list[str]:
    """Pull ticker symbols out of a robin_stocks watchlist response.

    The shape has changed across robin_stocks versions (list of entries,
    or {"results": [...]}); anything without a recognizable symbol is
    skipped — a schema surprise must never break the daily scan.
    """
    if isinstance(payload, dict):
        entries = payload.get("results", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        return []
    symbols = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol") or entry.get("object_symbol")
        if isinstance(symbol, str) and symbol.strip():
            symbols.append(symbol.strip().upper())
    return symbols


class RobinhoodClient:
    """Thin, defensive wrapper around robin_stocks."""

    def __init__(self):
        self._logged_in = False

    @staticmethod
    def credentials_available() -> bool:
        return bool(os.environ.get("ROBINHOOD_USERNAME") and os.environ.get("ROBINHOOD_PASSWORD"))

    def login(self) -> bool:
        if self._logged_in:
            return True
        if not self.credentials_available():
            log.info("Robinhood credentials not set; running without portfolio link.")
            return False
        try:
            import robin_stocks.robinhood as rh
        except ImportError:
            log.warning("robin_stocks not installed — run: pip install robin_stocks pyotp")
            return False
        mfa_code = None
        secret = os.environ.get("ROBINHOOD_MFA_SECRET")
        if secret:
            try:
                import pyotp

                mfa_code = pyotp.TOTP(secret).now()
            except ImportError:
                log.warning("pyotp not installed; attempting login without TOTP code")
        try:
            rh.login(
                os.environ["ROBINHOOD_USERNAME"],
                os.environ["ROBINHOOD_PASSWORD"],
                mfa_code=mfa_code,
                store_session=True,
            )
            self._logged_in = True
            return True
        except Exception as exc:
            log.error("Robinhood login failed: %s", exc)
            return False

    def portfolio(self) -> Portfolio | None:
        if not self.login():
            return None
        import robin_stocks.robinhood as rh

        try:
            raw = rh.build_holdings() or {}
            profile = rh.profiles.load_account_profile() or {}
        except Exception as exc:
            log.error("failed to fetch Robinhood holdings: %s", exc)
            return None

        holdings = []
        for ticker, info in raw.items():
            try:
                holdings.append(
                    Holding(
                        ticker=ticker,
                        shares=float(info.get("quantity", 0) or 0),
                        avg_buy_price=float(info.get("average_buy_price", 0) or 0),
                        current_price=float(info.get("price", 0) or 0),
                        equity=float(info.get("equity", 0) or 0),
                    )
                )
            except (TypeError, ValueError):
                log.warning("skipping malformed holding entry for %s", ticker)
        try:
            buying_power = float(profile.get("buying_power", 0) or 0)
        except (TypeError, ValueError):
            buying_power = 0.0
        return Portfolio(holdings=holdings, buying_power=buying_power)

    # --- watchlists (read-only) ---

    def watchlist_tickers(self) -> list[str] | None:
        """Every symbol across the account's Robinhood watchlists.

        Read-only: names you star in the Robinhood app get scanned daily
        without retyping them here. None when not linked or the fetch fails.
        """
        if not self.login():
            return None
        import robin_stocks.robinhood as rh

        try:
            raw = rh.account.get_all_watchlists() or {}
        except Exception as exc:
            log.warning("failed to fetch Robinhood watchlists: %s", exc)
            return None
        names = [
            w.get("display_name") or w.get("name")
            for w in (raw.get("results") if isinstance(raw, dict) else raw) or []
            if isinstance(w, dict)
        ]
        symbols: list[str] = []
        for name in filter(None, names):
            try:
                items = rh.account.get_watchlist_by_name(name) or {}
            except Exception as exc:
                log.warning("failed to fetch watchlist %r: %s", name, exc)
                continue
            symbols.extend(extract_watchlist_symbols(items))
        return sorted(set(symbols))

    # --- full account history (orders + dividends) ---

    def _symbol_for_instrument(self, url: str) -> str | None:
        """Resolve an instrument URL to a ticker, cached per client."""
        if not url:
            return None
        if not hasattr(self, "_symbol_cache"):
            self._symbol_cache: dict[str, str | None] = {}
        if url not in self._symbol_cache:
            try:
                import robin_stocks.robinhood as rh

                self._symbol_cache[url] = rh.stocks.get_symbol_by_url(url) or None
            except Exception as exc:
                log.warning("symbol lookup failed for %s: %s", url, exc)
                self._symbol_cache[url] = None
        return self._symbol_cache[url]

    def order_history(self) -> list[dict] | None:
        """Every filled stock order, normalized for the brain's mirror."""
        if not self.login():
            return None
        import robin_stocks.robinhood as rh

        try:
            raw = rh.orders.get_all_stock_orders() or []
        except Exception as exc:
            log.error("failed to fetch Robinhood order history: %s", exc)
            return None
        orders = []
        for o in raw:
            try:
                if o.get("state") != "filled":
                    continue
                ticker = self._symbol_for_instrument(o.get("instrument", ""))
                if not ticker:
                    continue
                orders.append(
                    {
                        "order_id": o["id"],
                        "ticker": ticker,
                        "side": o.get("side", ""),
                        "quantity": float(o.get("cumulative_quantity") or 0),
                        "price": float(o.get("average_price") or 0),
                        "executed_at": o.get("last_transaction_at") or o.get("updated_at") or "",
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed order: %s", exc)
        return orders

    def dividend_history(self) -> list[dict] | None:
        """Every dividend payment, normalized for the brain's mirror."""
        if not self.login():
            return None
        import robin_stocks.robinhood as rh

        try:
            raw = rh.account.get_dividends() or []
        except Exception as exc:
            log.error("failed to fetch Robinhood dividends: %s", exc)
            return None
        dividends = []
        for d in raw:
            try:
                if d.get("state") not in ("paid", "reinvested"):
                    continue
                ticker = self._symbol_for_instrument(d.get("instrument", ""))
                if not ticker:
                    continue
                dividends.append(
                    {
                        "dividend_id": d["id"],
                        "ticker": ticker,
                        "amount": float(d.get("amount") or 0),
                        "paid_at": d.get("paid_at") or d.get("payable_date") or "",
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed dividend: %s", exc)
        return dividends

    def sync_history(self, db) -> dict | None:
        """Mirror the full account history into the brain. Idempotent —
        run it as often as you like; only new activity is added."""
        orders = self.order_history()
        dividends = self.dividend_history()
        if orders is None and dividends is None:
            return None
        from datetime import datetime, timezone

        stats = {
            "orders_added": db.upsert_rh_orders(orders or []),
            "orders_total": len(db.rh_orders()),
            "dividends_added": db.upsert_rh_dividends(dividends or []),
        }
        db.set_meta(
            "last_rh_sync", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        return stats

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            import robin_stocks.robinhood as rh

            rh.logout()
        except Exception:
            pass
        self._logged_in = False
