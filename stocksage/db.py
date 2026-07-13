"""SQLite persistence: suggestions, outcomes, learned weights, move context.

One file (default ~/.stocksage/stocksage.db) holds everything the engine
learns, so backing up your accumulated knowledge is a single file copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FALLBACK_DB_PATH = "~/.stocksage/stocksage.db"


def default_db_path() -> Path:
    """Resolved at call time so a .env-provided STOCKSAGE_DB is honored."""
    return Path(os.environ.get("STOCKSAGE_DB") or FALLBACK_DB_PATH).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,             -- STRONG BUY / BUY / HOLD / SELL / STRONG SELL
    score REAL NOT NULL,              -- composite score in [-1, 1]
    price REAL NOT NULL,              -- price when suggested
    signals TEXT NOT NULL,            -- JSON {signal_name: value}
    horizon_days INTEGER NOT NULL,
    evaluated INTEGER NOT NULL DEFAULT 0,
    realized_return REAL,             -- filled at evaluation time
    hit INTEGER                       -- 1 if direction was right, 0 if wrong
);

CREATE TABLE IF NOT EXISTS weights (
    signal TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS move_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_date TEXT NOT NULL,         -- trading day of the move
    return_pct REAL NOT NULL,
    atr_multiple REAL,                -- move size in ATRs; NULL if unknown
    reasons TEXT NOT NULL,            -- JSON list of tagged reasons
    headlines TEXT NOT NULL,          -- JSON list of {title, publisher, link}
    UNIQUE (ticker, event_date)
);

CREATE TABLE IF NOT EXISTS sector_weights (
    sector TEXT NOT NULL,
    signal TEXT NOT NULL,
    weight REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sector, signal)
);

CREATE TABLE IF NOT EXISTS rh_orders (
    order_id TEXT PRIMARY KEY,        -- Robinhood's own id: sync is idempotent
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,               -- buy / sell
    quantity REAL NOT NULL,
    price REAL NOT NULL,              -- average execution price
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rh_dividends (
    dividend_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    amount REAL NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    suggestion_id INTEGER NOT NULL,
    detail TEXT NOT NULL              -- JSON of the weight update applied
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the dashboard caches one Engine across
        # Streamlit reruns, which land on different threads. This is a
        # single-user app with serialized interactions, so cross-thread
        # access is safe; without this flag the second browser session dies
        # with "SQLite objects created in a thread can only be used in that
        # thread".
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive-only migrations so existing brains upgrade in place."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(suggestions)")}
        if "benchmark_return" not in cols:
            # Market (SPY) return over the same window as realized_return, so
            # a call can be judged against "would cash-in-the-index have won".
            self.conn.execute("ALTER TABLE suggestions ADD COLUMN benchmark_return REAL")

    def close(self) -> None:
        self.conn.close()

    # --- suggestions ---

    def record_suggestion(
        self,
        ticker: str,
        action: str,
        score: float,
        price: float,
        signals: dict[str, float],
        horizon_days: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO suggestions (created_at, ticker, action, score, price,"
            " signals, horizon_days) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), ticker, action, score, price, json.dumps(signals), horizon_days),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending_evaluations(self, as_of: datetime) -> list[sqlite3.Row]:
        """Suggestions whose evaluation horizon has elapsed."""
        rows = self.conn.execute(
            "SELECT * FROM suggestions WHERE evaluated = 0"
        ).fetchall()
        due = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
            except (ValueError, TypeError):
                # One malformed row (e.g. from a hand-edited or corrupted
                # merge) must not block grading of everything else.
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (as_of - created).days
            # Calendar-day cushion: horizon is in trading days.
            if age_days >= row["horizon_days"] * 1.5:
                due.append(row)
        return due

    def mark_evaluated(
        self,
        suggestion_id: int,
        realized_return: float,
        hit: bool,
        benchmark_return: float | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE suggestions SET evaluated = 1, realized_return = ?, hit = ?,"
            " benchmark_return = ? WHERE id = ?",
            (realized_return, int(hit), benchmark_return, suggestion_id),
        )
        self.conn.commit()

    def evaluated_suggestions(self) -> list[sqlite3.Row]:
        """The full graded record, chronological — feeds the profit ledger."""
        return self.conn.execute(
            "SELECT * FROM suggestions WHERE evaluated = 1 ORDER BY created_at, id"
        ).fetchall()

    def recent_suggestions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def performance_summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, AVG(hit) AS hit_rate, AVG(realized_return) AS avg_ret"
            " FROM suggestions WHERE evaluated = 1"
        ).fetchone()
        return {
            "evaluated": row["n"] or 0,
            "hit_rate": row["hit_rate"],
            "avg_return": row["avg_ret"],
        }

    # --- meta (small key/value state: bootstrap flag, last run, counters) ---

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- watchlist (extra tickers the user follows, beyond the universe) ---

    def watchlist(self) -> list[str]:
        raw = self.get_meta("watchlist")
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [t for t in items if isinstance(t, str)]

    def watchlist_add(self, ticker: str) -> list[str]:
        ticker = ticker.strip().upper()
        items = self.watchlist()
        if ticker and ticker not in items:
            items.append(ticker)
            self.set_meta("watchlist", json.dumps(items))
        return items

    def watchlist_remove(self, ticker: str) -> list[str]:
        ticker = ticker.strip().upper()
        items = [t for t in self.watchlist() if t != ticker]
        self.set_meta("watchlist", json.dumps(items))
        return items

    # --- learned weights ---

    def load_weights(self) -> dict[str, float]:
        return {
            r["signal"]: r["weight"]
            for r in self.conn.execute("SELECT signal, weight FROM weights")
        }

    def save_weights(self, weights: dict[str, float]) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO weights (signal, weight, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(signal) DO UPDATE SET weight = excluded.weight,"
            " updated_at = excluded.updated_at",
            [(s, w, now) for s, w in weights.items()],
        )
        self.conn.commit()

    def load_sector_weights(self, sector: str) -> dict[str, float]:
        return {
            r["signal"]: r["weight"]
            for r in self.conn.execute(
                "SELECT signal, weight FROM sector_weights WHERE sector = ?", (sector,)
            )
        }

    def save_sector_weights(self, sector: str, weights: dict[str, float]) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO sector_weights (sector, signal, weight, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(sector, signal) DO UPDATE SET weight = excluded.weight,"
            " updated_at = excluded.updated_at",
            [(sector, s, w, now) for s, w in weights.items()],
        )
        self.conn.commit()

    def sector_grade_counts(self) -> dict[str, int]:
        """Graded-call counters per sector (kept in meta as sector_grades:<name>)."""
        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'sector_grades:%'"
        ).fetchall()
        return {r["key"].split(":", 1)[1]: int(r["value"]) for r in rows}

    def ticker_track_record(self, ticker: str) -> tuple[int, float | None, float | None]:
        """The model's own graded record on one name: (calls, hit_rate, avg_return)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, AVG(hit) AS hit_rate, AVG(realized_return) AS avg_ret"
            " FROM suggestions WHERE evaluated = 1 AND ticker = ?",
            (ticker,),
        ).fetchone()
        return (row["n"] or 0, row["hit_rate"], row["avg_ret"])

    def log_learning(self, suggestion_id: int, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO learning_log (created_at, suggestion_id, detail) VALUES (?, ?, ?)",
            (_now(), suggestion_id, json.dumps(detail)),
        )
        self.conn.commit()

    # --- Robinhood history mirror ---

    def upsert_rh_orders(self, orders: list[dict]) -> int:
        """Idempotent insert of filled orders. Returns how many were new."""
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO rh_orders"
            " (order_id, ticker, side, quantity, price, executed_at)"
            " VALUES (:order_id, :ticker, :side, :quantity, :price, :executed_at)",
            orders,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def rh_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rh_orders ORDER BY executed_at, order_id"
        ).fetchall()

    def upsert_rh_dividends(self, dividends: list[dict]) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO rh_dividends (dividend_id, ticker, amount, paid_at)"
            " VALUES (:dividend_id, :ticker, :amount, :paid_at)",
            dividends,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def rh_dividends(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rh_dividends ORDER BY paid_at"
        ).fetchall()

    # --- move context ---

    def record_move_event(
        self,
        ticker: str,
        event_date: str,
        return_pct: float,
        atr_multiple: float | None,
        reasons: list[str],
        headlines: list[dict],
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO move_events (created_at, ticker, event_date,"
            " return_pct, atr_multiple, reasons, headlines)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                ticker,
                event_date,
                return_pct,
                atr_multiple,
                json.dumps(reasons),
                json.dumps(headlines),
            ),
        )
        self.conn.commit()

    def move_events(self, ticker: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if ticker:
            return self.conn.execute(
                "SELECT * FROM move_events WHERE ticker = ? ORDER BY event_date DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM move_events ORDER BY event_date DESC LIMIT ?", (limit,)
        ).fetchall()
