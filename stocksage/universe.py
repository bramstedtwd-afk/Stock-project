"""The stock universe: top industries and the leading names in each.

The universe is a curated map of the 10 major GICS sectors to their ten most
influential, most liquid large-cap stocks, plus the SPDR sector ETF used to
measure the sector's own trend. Curation beats a dynamic screen here: these
lists are stable quarter to quarter, and a static list keeps the engine
deterministic and testable. Refresh the lists as leadership changes — the
rest of the engine adapts automatically because nothing else hardcodes
tickers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sector:
    name: str
    etf: str  # sector ETF used as the sector trend proxy
    tickers: tuple[str, ...]


SECTORS: tuple[Sector, ...] = (
    Sector(
        "Technology",
        "XLK",
        ("AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "QCOM", "TXN"),
    ),
    Sector(
        "Healthcare",
        "XLV",
        ("LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "PFE"),
    ),
    Sector(
        "Financials",
        "XLF",
        ("BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "SPGI", "AXP"),
    ),
    Sector(
        "Consumer Discretionary",
        "XLY",
        ("AMZN", "TSLA", "HD", "MCD", "BKNG", "NKE", "LOW", "SBUX", "TJX", "CMG"),
    ),
    Sector(
        "Communication Services",
        "XLC",
        ("GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA"),
    ),
    Sector(
        "Industrials",
        "XLI",
        ("CAT", "GE", "UNP", "HON", "RTX", "BA", "DE", "LMT", "UPS", "ETN"),
    ),
    Sector(
        "Consumer Staples",
        "XLP",
        ("PG", "COST", "KO", "PEP", "WMT", "PM", "MDLZ", "CL", "TGT", "KMB"),
    ),
    Sector(
        "Energy",
        "XLE",
        ("XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "WMB"),
    ),
    Sector(
        "Utilities",
        "XLU",
        ("NEE", "SO", "DUK", "CEG", "SRE", "AEP", "D", "EXC", "XEL", "PCG"),
    ),
    Sector(
        "Real Estate",
        "XLRE",
        ("PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "CBRE"),
    ),
)

MARKET_BENCHMARK = "SPY"


def all_tickers() -> list[str]:
    """Every stock ticker in the universe, deduplicated, order preserved."""
    seen: dict[str, None] = {}
    for sector in SECTORS:
        for t in sector.tickers:
            seen.setdefault(t, None)
    return list(seen)


def sector_of(ticker: str) -> Sector | None:
    for sector in SECTORS:
        if ticker in sector.tickers:
            return sector
    return None


def sector_by_name(name: str) -> Sector | None:
    for sector in SECTORS:
        if sector.name.lower() == name.lower():
            return sector
    return None
