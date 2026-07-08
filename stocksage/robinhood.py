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

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            import robin_stocks.robinhood as rh

            rh.logout()
        except Exception:
            pass
        self._logged_in = False
