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

CREATE TABLE IF NOT EXISTS ingested_fills (
    order_id TEXT PRIMARY KEY,        -- Robinhood order already turned into a graded call
    suggestion_id INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
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


# Who made a call. The model's record must be measurable on its own — mixing
# the owner's real trades into it produces a number that describes neither.
SOURCE_MODEL = "model"    # the engine's own scan
SOURCE_OWNER = "owner"    # an actual Robinhood fill, ingested after the fact
SOURCE_ROUTINE = "routine"  # a decision the trading routine logged


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
        if "source" not in cols:
            # Who made this call: the model's own scan, the owner's actual
            # broker fill, or a routine decision. Without this the scoreboard
            # measures a blend of two decision-makers and reads as if it were
            # the model's record.
            self.conn.execute(
                f"ALTER TABLE suggestions ADD COLUMN source TEXT NOT NULL"
                f" DEFAULT '{SOURCE_MODEL}'"
            )
            # Existing rows are recoverable: every ingested fill recorded the
            # suggestion it created, so the split applies retroactively.
            if self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='ingested_fills'"
            ).fetchone():
                self.conn.execute(
                    f"UPDATE suggestions SET source = '{SOURCE_OWNER}' WHERE id IN"
                    " (SELECT suggestion_id FROM ingested_fills)"
                )

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
        created_at: str | None = None,
        source: str = SOURCE_MODEL,
    ) -> int:
        """Record a call. `created_at` may be backdated (e.g. an actual fill
        time) so its grading horizon is measured from when it really happened.
        `source` keeps the model's record separable from the owner's."""
        cur = self.conn.execute(
            "INSERT INTO suggestions (created_at, ticker, action, score, price,"
            " signals, horizon_days, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at or _now(), ticker, action, score, price,
             json.dumps(signals), horizon_days, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_ungradeable(self, suggestion_id: int) -> None:
        """Close a call that can never be honestly graded.

        Used when the entry date predates all available price history: the
        true horizon return is unknowable, and falling back to today's price
        would book a multi-year holding period as a five-day call. Marked
        evaluated so it stops being retried, with a NULL return so it is
        excluded from every average, the ledger, and the weight updates.
        """
        self.conn.execute(
            "UPDATE suggestions SET evaluated = 1, realized_return = NULL,"
            " hit = NULL WHERE id = ?",
            (suggestion_id,),
        )
        self.conn.commit()

    def fill_ingested(self, order_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM ingested_fills WHERE order_id = ?", (order_id,)
        ).fetchone() is not None

    def mark_fill_ingested(self, order_id: str, suggestion_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ingested_fills (order_id, suggestion_id, ingested_at)"
            " VALUES (?, ?, ?)",
            (order_id, suggestion_id, _now()),
        )
        self.conn.commit()

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

    def evaluated_suggestions(self, source: str | None = SOURCE_MODEL) -> list[sqlite3.Row]:
        """The graded record, chronological — feeds the profit ledger.

        Defaults to the model's own calls: a ledger blending the model's
        picks with the owner's real trades describes neither. Pass
        source=None for everything, or SOURCE_OWNER for the owner's record.
        Rows with a NULL return are ungradeable and never included.
        """
        sql = (
            "SELECT * FROM suggestions WHERE evaluated = 1"
            " AND realized_return IS NOT NULL"
        )
        params: tuple = ()
        if source is not None:
            sql += " AND source = ?"
            params = (source,)
        return self.conn.execute(sql + " ORDER BY created_at, id", params).fetchall()

    def recent_suggestions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def performance_summary(self, source: str | None = SOURCE_MODEL) -> dict:
        """Graded-record headline numbers, the model's own by default."""
        sql = (
            "SELECT COUNT(*) AS n, AVG(hit) AS hit_rate, AVG(realized_return) AS avg_ret"
            " FROM suggestions WHERE evaluated = 1 AND realized_return IS NOT NULL"
        )
        params: tuple = ()
        if source is not None:
            sql += " AND source = ?"
            params = (source,)
        row = self.conn.execute(sql, params).fetchone()
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

    # --- model suggestions on/off ---

    def model_suggestions_enabled(self) -> bool:
        """Whether the model's picks are offered to the routine as actionable.

        Off is a real, useful state: the engine keeps scanning, grading and
        learning, but the routine treats its candidates as research only and
        trades on its own live analysis. That is the honest thing to do while
        the model's measured edge is negative — it keeps building a track
        record without acting on one that hasn't earned trust yet.
        """
        return (self.get_meta("model_suggestions", "on") or "on").lower() != "off"

    def set_model_suggestions(self, enabled: bool) -> None:
        self.set_meta("model_suggestions", "on" if enabled else "off")

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

    def ticker_track_record(
        self, ticker: str, source: str | None = SOURCE_MODEL
    ) -> tuple[int, float | None, float | None]:
        """The model's own graded record on one name: (calls, hit_rate, avg_return).

        Model-only by default — this drives the reliability multiplier, which
        is meant to answer "does the model read THIS name well", not "do the
        model and the owner combined".
        """
        sql = (
            "SELECT COUNT(*) AS n, AVG(hit) AS hit_rate, AVG(realized_return) AS avg_ret"
            " FROM suggestions WHERE evaluated = 1 AND realized_return IS NOT NULL"
            " AND ticker = ?"
        )
        params: tuple = (ticker,)
        if source is not None:
            sql += " AND source = ?"
            params = (ticker, source)
        row = self.conn.execute(sql, params).fetchone()
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
