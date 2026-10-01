"""The brain: everything StockSage has learned, as a portable object.

The brain is one SQLite file (learned weights, graded track record, move
memory). This module makes it travel:

    export      snapshot the brain to a single file you can send anywhere
    import      bring a brain file in — MERGE by default, so knowledge from
                two devices compounds instead of one overwriting the other
    sync        park the brain in a cloud-synced folder (Dropbox, iCloud,
                OneDrive, ...) so every device shares one mind automatically
    info        where the brain lives and what it knows

Merge semantics (chosen so a merge can only add knowledge, never lose it):
  - move events   union (unique per ticker+date)
  - suggestions   union (deduped on created_at+ticker+action) — the graded
                  track record from both devices is kept
  - weights       the whole vector from whichever brain learned most
                  recently (vectors are normalized together; mixing single
                  weights from two vectors would corrupt both)
  - meta          bootstrap flag kept if either side has it; counters take
                  the larger value; timestamps take the later one

Robinhood credentials are deliberately NOT part of the brain — they never
leave the device they were entered on. The brain DOES mirror the owner's
real trade history, which is why the repo-carried snapshot is scrubbed of it
before it is written (scrub_personal_data): a merge between the owner's own
devices should carry everything, but a file committed to a public repository
must carry knowledge only.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

from .db import SOURCE_OWNER, Database, default_db_path
from .envfile import save_env

BRAIN_FILENAME = "stocksage.db"

# Cloud-synced folders we know how to find, per platform. Checked in order;
# only existing folders are offered.
_CLOUD_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Dropbox", "~/Dropbox"),
    ("iCloud Drive", "~/Library/Mobile Documents/com~apple~CloudDocs"),
    ("OneDrive", "~/OneDrive"),
    ("Google Drive", "~/Google Drive/My Drive"),
    ("Google Drive", "~/Google Drive"),
    ("Syncthing", "~/Sync"),
)


def detect_cloud_folders(home: Path | None = None) -> list[tuple[str, Path]]:
    """Cloud-sync folders present on this machine: [(display name, path)]."""
    home = home or Path.home()
    found = []
    for name, raw in _CLOUD_CANDIDATES:
        candidate = home / Path(raw).expanduser().relative_to(Path.home()) \
            if raw.startswith("~") else Path(raw)
        if candidate.is_dir() and all(candidate != p for _, p in found):
            found.append((name, candidate))
    return found


def is_shared(db_path: str | Path | None = None) -> bool:
    """True when the brain lives outside the default local location."""
    current = Path(db_path).expanduser() if db_path else default_db_path()
    return current != Path("~/.stocksage/stocksage.db").expanduser()


def export_brain(dest: str | Path | None = None, db_path: str | Path | None = None) -> Path:
    """Consistent snapshot of the brain to a single file."""
    src = Database(db_path)  # ensures the file and schema exist
    dest = Path(dest).expanduser() if dest else Path(
        f"stocksage-brain-{date.today().isoformat()}.db"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        src.conn.backup(out)  # atomic, safe while the source is in use
        out.commit()
    finally:
        out.close()
        src.close()
    return dest


def merge_brains(dest_path: str | Path, src_path: str | Path) -> dict:
    """Merge the brain at src_path INTO dest_path. Returns what was added."""
    dest = Database(dest_path)  # creates/migrates schema if needed
    conn = dest.conn
    conn.execute("ATTACH DATABASE ? AS src", (str(Path(src_path).expanduser()),))
    stats = {}
    try:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO move_events"
            " (created_at, ticker, event_date, return_pct, atr_multiple, reasons, headlines)"
            " SELECT created_at, ticker, event_date, return_pct, atr_multiple,"
            "        reasons, headlines FROM src.move_events"
        )
        stats["move_events_added"] = conn.total_changes - before

        # The Robinhood mirror is part of the brain too. Guard on table
        # presence so brains exported by older versions still merge cleanly.
        src_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM src.sqlite_master WHERE type = 'table'"
            )
        }
        if "rh_orders" in src_tables:
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO rh_orders"
                " (order_id, ticker, side, quantity, price, executed_at)"
                " SELECT order_id, ticker, side, quantity, price, executed_at"
                " FROM src.rh_orders"
            )
            stats["rh_orders_added"] = conn.total_changes - before
        if "rh_dividends" in src_tables:
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO rh_dividends (dividend_id, ticker, amount, paid_at)"
                " SELECT dividend_id, ticker, amount, paid_at FROM src.rh_dividends"
            )
            stats["rh_dividends_added"] = conn.total_changes - before
        if "sector_weights" in src_tables:
            # Same recency rule as global weights: whole vectors, newest wins.
            for (sector,) in conn.execute(
                "SELECT DISTINCT sector FROM src.sector_weights"
            ).fetchall():
                src_ts = conn.execute(
                    "SELECT MAX(updated_at) FROM src.sector_weights WHERE sector = ?",
                    (sector,),
                ).fetchone()[0]
                dest_ts = conn.execute(
                    "SELECT MAX(updated_at) FROM sector_weights WHERE sector = ?",
                    (sector,),
                ).fetchone()[0]
                if src_ts and (dest_ts is None or src_ts > dest_ts):
                    conn.execute(
                        "DELETE FROM sector_weights WHERE sector = ?", (sector,)
                    )
                    conn.execute(
                        "INSERT INTO sector_weights (sector, signal, weight, updated_at)"
                        " SELECT sector, signal, weight, updated_at"
                        " FROM src.sector_weights WHERE sector = ?",
                        (sector,),
                    )

        before = conn.total_changes
        src_sugg_cols = {
            r[1] for r in conn.execute("PRAGMA src.table_info(suggestions)")
        }
        # Older brains predate benchmark_return; merge them as NULL.
        bench_expr = (
            "s.benchmark_return" if "benchmark_return" in src_sugg_cols else "NULL"
        )
        # Whose call it was must travel with the call. Without this every
        # device sync quietly re-labelled the owner's real trades as the
        # model's own, undoing the separation the track record depends on.
        source_expr = "s.source" if "source" in src_sugg_cols else "'model'"
        conn.execute(
            "INSERT INTO suggestions"
            " (created_at, ticker, action, score, price, signals, horizon_days,"
            "  evaluated, realized_return, hit, benchmark_return, source)"
            " SELECT s.created_at, s.ticker, s.action, s.score, s.price, s.signals,"
            f"        s.horizon_days, s.evaluated, s.realized_return, s.hit,"
            f"        {bench_expr}, {source_expr}"
            " FROM src.suggestions s"
            " WHERE NOT EXISTS (SELECT 1 FROM suggestions d"
            "   WHERE d.created_at = s.created_at AND d.ticker = s.ticker"
            "     AND d.action = s.action)"
        )
        stats["suggestions_added"] = conn.total_changes - before

        # The record of which Robinhood fills have already become calls must
        # travel with the calls themselves. Without it a fresh device that
        # merges this brain and then syncs the same broker history sees every
        # order as new and turns each one into a SECOND call — doubling the
        # owner's record and announcing "captured N new trades" as news.
        if "ingested_fills" in src_tables:
            before = conn.total_changes
            # Suggestion ids are per-database, so the link has to be rebuilt:
            # find the same call (time, ticker, action) on this side.
            conn.execute(
                "INSERT OR IGNORE INTO ingested_fills"
                " (order_id, suggestion_id, ingested_at)"
                " SELECT f.order_id, d.id, f.ingested_at"
                " FROM src.ingested_fills f"
                " JOIN src.suggestions s ON s.id = f.suggestion_id"
                " JOIN suggestions d ON d.created_at = s.created_at"
                "   AND d.ticker = s.ticker AND d.action = s.action"
            )
            stats["ingested_fills_added"] = conn.total_changes - before

        # Weights travel as a whole vector: take whichever learned last.
        src_ts = conn.execute("SELECT MAX(updated_at) FROM src.weights").fetchone()[0]
        dest_ts = conn.execute("SELECT MAX(updated_at) FROM weights").fetchone()[0]
        stats["weights_taken_from"] = "local"
        if src_ts and (dest_ts is None or src_ts > dest_ts):
            conn.execute("DELETE FROM weights")
            conn.execute(
                "INSERT INTO weights (signal, weight, updated_at)"
                " SELECT signal, weight, updated_at FROM src.weights"
            )
            stats["weights_taken_from"] = "imported"

        for row in conn.execute("SELECT key, value FROM src.meta").fetchall():
            key, incoming = row["key"], row["value"]
            current = dest.get_meta(key)
            if current is None:
                dest.set_meta(key, incoming)
            elif key == "warmup_samples":
                dest.set_meta(key, str(max(int(current), int(incoming))))
            elif incoming > current:  # ISO dates compare correctly as strings
                dest.set_meta(key, incoming)
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")
        dest.close()
    return stats


def import_brain(
    src: str | Path, db_path: str | Path | None = None, replace: bool = False
) -> dict:
    """Bring a brain file in. Merge by default; replace=True swaps it wholesale."""
    src = Path(src).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"no brain file at {src}")
    sqlite3.connect(str(src)).execute("SELECT 1 FROM sqlite_master").fetchone()  # sanity
    target = Path(db_path).expanduser() if db_path else default_db_path()
    if replace:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup = target.with_suffix(".db.pre-import-backup")
            shutil.copy2(target, backup)
        shutil.copy2(src, target)
        return {"replaced": True, "backup": str(backup) if backup else None}
    return merge_brains(target, src)


def sync_to_folder(folder: str | Path, db_path: str | Path | None = None) -> Path:
    """Move the brain into a (cloud-synced) folder and point StockSage at it.

    If a brain already lives in the folder (e.g. placed there by another
    device), the local brain is merged into it — nothing is lost.
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not an existing folder")
    target = folder / BRAIN_FILENAME
    local = Path(db_path).expanduser() if db_path else default_db_path()

    if target.exists() and local.exists() and target != local:
        merge_brains(target, local)
    elif local.exists() and target != local:
        shutil.copy2(local, target)
    else:
        Database(target).close()  # fresh brain directly in the folder

    save_env({"STOCKSAGE_DB": str(target)})
    if local.exists() and target != local:
        local.rename(local.with_suffix(".db.moved-to-sync"))
    return target


# --- repo-carried snapshot: knowledge travels the same channel as code ------

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "brain" / "brain-snapshot.db"
ROUTINE_PATH = Path(__file__).resolve().parent.parent / "ROUTINE.md"
# Outside the repo tree on purpose: automated exports (publish/publish-drive)
# must never write here — only SNAPSHOT_PATH (inside the repo) is meant to
# be git-committed, and only via the deliberate `brain snapshot` command.
STATE_DIR = Path("~/.stocksage").expanduser()


# What the repo snapshot must never carry. The brain holds a full mirror of
# the owner's Robinhood account — every fill with its ticker, share count,
# execution price and timestamp — and the calls derived from those fills.
# That is a personal financial record, and this one file is committed to a
# public repository. Learned knowledge travels; the account does not.
PRIVATE_TABLES = ("rh_orders", "rh_dividends", "ingested_fills")


def scrub_personal_data(path: str | Path) -> dict:
    """Strip the owner's real account activity out of a brain file.

    Keeps everything that is knowledge — learned weights, move memory, the
    model's own graded calls — and drops everything that is a record of what
    this person actually traded. VACUUM afterwards is not tidiness: deleted
    rows survive in the file's free pages, and a snapshot with recoverable
    trade history in its slack space is not scrubbed.
    """
    path = Path(path).expanduser()
    conn = sqlite3.connect(str(path))
    removed: dict[str, int] = {}
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in PRIVATE_TABLES:
            if table in present:
                before = conn.total_changes
                conn.execute(f"DELETE FROM {table}")
                removed[table] = conn.total_changes - before
        if "suggestions" in present:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
            if "source" in cols:
                # Owner-sourced calls ARE the real fills, re-shaped: same
                # ticker, price and date. Dropping the mirror but keeping
                # these would publish the trade history anyway.
                before = conn.total_changes
                conn.execute("DELETE FROM suggestions WHERE source = ?", (SOURCE_OWNER,))
                removed["suggestions"] = conn.total_changes - before
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    return removed


def write_snapshot(db_path: str | Path | None = None) -> Path:
    """Export the brain into the repo (brain/brain-snapshot.db).

    Commit + push it and every environment that pulls the repo carries the
    knowledge too. Credentials were never in the brain; the owner's real
    trade history is, so it is scrubbed out here — see scrub_personal_data.
    """
    path = export_brain(SNAPSHOT_PATH, db_path=db_path)
    scrub_personal_data(path)
    return path


def absorb_snapshot(db_path: str | Path | None = None) -> dict | None:
    """Merge the repo snapshot (if present) into the local brain.

    Merging only ever adds and is idempotent, so this is safe to run on
    every routine call — it's how a fresh environment starts smart.
    Returns merge stats, or None when no snapshot file exists.
    """
    if not SNAPSHOT_PATH.exists():
        return None
    return import_brain(SNAPSHOT_PATH, db_path=db_path)


def brain_info(db_path: str | Path | None = None) -> dict:
    db = Database(db_path)
    try:
        counts = {
            table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("suggestions", "move_events", "weights")
        }
        return {
            "path": str(db.path),
            "size_kb": round(db.path.stat().st_size / 1024, 1) if db.path.exists() else 0,
            "suggestions": counts["suggestions"],
            "move_events": counts["move_events"],
            "signals_weighted": counts["weights"],
            "warmup_samples": db.get_meta("warmup_samples", "0"),
            "last_daily_run": db.get_meta("last_daily_run"),
            "last_device": db.get_meta("last_device"),
            "last_device_at": db.get_meta("last_device_at"),
            "shared": is_shared(db.path),
        }
    finally:
        db.close()
