"""Look-alikes: how did setups like this one actually do?

A recommendation is easier to trust, and easier to distrust, when it can point
at history: "150 past setups with this signal fingerprint beat the market by
0.6% a week". This keeps every (signal fingerprint -> what happened next)
sample from the walk-forward history in a compact book and, for any current
call, finds its nearest neighbours.

Three things keep it from being a confidence trick:

  * CLUSTERING. Neighbours from one rally are not independent evidence. The
    result is averaged within each DATE first, and the uncertainty comes from
    the spread between dates, not between individual stocks.
  * DIRECTION. A sell call is right when the stock trails the market, so its
    edge is the negative of the excess return.
  * IT IS A GUIDE, NOT A TEST. Neighbours found in history that was also used
    to learn from are in-sample. Whether requiring look-alike backing actually
    improves picks is measured separately and out of sample, as a strategy in
    the lab ("model buys confirmed by look-alikes"), and shown beside the call.

Pure maths over numpy; no network.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

K_NEIGHBOURS = 150
MIN_DATES = 15           # distinct dates among the neighbours, or it is one episode
Z_BACKED = 1.96          # a 95% lower bound must clear zero, after costs
DEFAULT_COST = 0.001
STALE_AFTER_DAYS = 10


@dataclass
class Evidence:
    side: str
    n: int = 0                     # neighbours used
    dates: int = 0                 # distinct dates they span
    edge: float | None = None      # mean per-date edge in the direction of the bet, after cost
    lower: float | None = None     # its 95% lower bound
    hit_rate: float | None = None  # share of neighbours that went the bet's way
    backed: bool = False
    reason: str = ""

    def sentence(self) -> str:
        if self.edge is None:
            return f"Look-alikes: {self.reason}"
        verdict = "BACKS" if self.backed else "does NOT back"
        return (
            f"Look-alikes: {self.n} past setups like this one made "
            f"{self.edge * 100:+.2f}% a week in this call's direction, against the "
            f"market and after costs ({self.hit_rate * 100:.0f}% went this way; "
            f"95% floor {self.lower * 100:+.2f}%). History {verdict} this call."
        )


def evidence_arrays(
    x_hist: np.ndarray,
    dates_hist: np.ndarray,
    excess_hist: np.ndarray,
    vector: np.ndarray,
    side: str,
    k: int = K_NEIGHBOURS,
    cost: float = DEFAULT_COST,
) -> Evidence:
    """Evidence from already-sliced history. The slice IS the no-look-ahead rule:
    callers pass only samples whose outcomes were known at decision time."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    n_hist = len(x_hist)
    if n_hist < max(30, k // 3):
        return Evidence(side, reason="not enough history yet to find look-alikes.")
    k = min(k, n_hist)
    dist = ((x_hist - vector) ** 2).sum(axis=1)
    idx = np.argpartition(dist, k - 1)[:k] if k < n_hist else np.arange(n_hist)
    sign = 1.0 if side == "buy" else -1.0
    outcome = sign * excess_hist[idx]
    unique_dates, inverse = np.unique(dates_hist[idx], return_inverse=True)
    if len(unique_dates) < MIN_DATES:
        return Evidence(
            side, n=k, dates=len(unique_dates),
            reason=f"its closest matches all come from only {len(unique_dates)} "
                   f"different dates, which is one episode, not a pattern.",
        )
    sums = np.bincount(inverse, weights=outcome)
    counts = np.bincount(inverse)
    per_date = sums / counts
    mean = float(per_date.mean()) - cost
    se = float(per_date.std(ddof=1) / math.sqrt(len(per_date)))
    lower = mean - Z_BACKED * se
    return Evidence(
        side, n=k, dates=len(unique_dates), edge=mean, lower=lower,
        hit_rate=float((outcome > 0).mean()), backed=bool(lower > 0),
        reason="",
    )


class AnalogBook:
    """Every walk-forward sample, ready to be searched."""

    def __init__(self, names, x, dates, excess, built_at: float | None = None):
        self.names = list(names)
        self.x = np.asarray(x, dtype=float)
        self.dates = np.asarray(dates)
        self.excess = np.asarray(excess, dtype=float)
        self.built_at = built_at if built_at is not None else time.time()

    def __len__(self) -> int:
        return len(self.x)

    @classmethod
    def from_samples(cls, samples) -> "AnalogBook":
        names = sorted({k for s in samples for k in s.features})
        x = np.array([[s.features.get(n, 0.0) for n in names] for s in samples], dtype=float)
        return cls(names, x, [s.date for s in samples], [s.excess for s in samples])

    def vector(self, features: dict[str, float]) -> np.ndarray:
        return np.array([features.get(n, 0.0) for n in self.names], dtype=float)

    def evidence(self, features: dict[str, float], side: str, before: str | None = None,
                 k: int = K_NEIGHBOURS, cost: float = DEFAULT_COST) -> Evidence:
        """Evidence for a call. `before` restricts to samples dated earlier than it."""
        mask = self.dates < before if before else slice(None)
        return evidence_arrays(self.x[mask], self.dates[mask], self.excess[mask],
                               self.vector(features), side, k, cost)

    @property
    def age_days(self) -> float:
        return (time.time() - self.built_at) / 86400.0

    @property
    def stale(self) -> bool:
        return self.age_days > STALE_AFTER_DAYS

    # --- persistence -----------------------------------------------------

    def save(self, path: Path | None = None) -> Path:
        path = Path(path) if path else default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path, names=np.array(self.names), x=self.x, dates=self.dates,
            excess=self.excess, built_at=np.array([self.built_at]),
        )
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "AnalogBook | None":
        path = Path(path) if path else default_path()
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as f:
                return cls(f["names"].tolist(), f["x"], f["dates"], f["excess"],
                           float(f["built_at"][0]))
        except Exception:
            return None   # a damaged cache is "not built yet", never a crash


def default_path() -> Path:
    override = os.environ.get("STOCKSAGE_STATE")
    base = Path(override).expanduser() if override else Path("~/.stocksage").expanduser()
    return base / "analogs.npz"


def store_if_better(book: AnalogBook, path: Path | None = None) -> bool:
    """Save `book` unless it would replace a bigger, still-fresh one.

    A quick 2-year backtest must not overwrite the 5-year book a longer run
    built: more history is more look-alikes. Stale books are always replaced.
    """
    current = AnalogBook.load(path)
    if current is not None and not current.stale and len(book) < len(current):
        return False
    book.save(path)
    return True
