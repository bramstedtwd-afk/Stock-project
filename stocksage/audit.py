"""Look inside the brain, and mend what is provably broken.

The brain is the one asset that cannot be rebuilt: months of graded calls.
When its numbers look wrong ("57% hit rate but it trails the market"), the
owner needs facts about what is actually inside it, not a guess — and a
repair that only touches what can be proven wrong, with a backup first.

    audit    read-only. Never opens the file for writing, so running it can
             never make anything worse.
    repair   dry-run unless told to apply. Applies only the fixes that are
             provable from the data itself (exact duplicate calls), after a
             consistent backup of the whole brain.

What audit deliberately does NOT do is decide the harder question — whether
the learned weights should be rebuilt — because that is a judgement about
the owner's learning system and should be made from these numbers, not
automatically.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .db import SOURCE_MODEL, SOURCE_OWNER, default_db_path

# A 5-day move this large is a data accident (split, bad exit price), not a
# market. Matches engine.IMPLAUSIBLE_RETURN, repeated here so the audit can
# be read on its own.
IMPLAUSIBLE = 0.60


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def audit_brain(db_path: str | Path | None = None) -> dict:
    """Facts about what is inside the brain. Read-only."""
    path = Path(db_path).expanduser() if db_path else default_db_path()
    if not path.exists():
        raise FileNotFoundError(f"no brain at {path}")
    conn = _open_readonly(path)
    try:
        cols = _columns(conn, "suggestions")
        has_source = "source" in cols
        has_bench = "benchmark_return" in cols
        source_expr = "source" if has_source else f"'{SOURCE_MODEL}'"
        bench_expr = "benchmark_return" if has_bench else "NULL"

        rows = conn.execute(
            f"SELECT id, created_at, ticker, action, price, evaluated,"
            f" realized_return, hit, {bench_expr} AS bench, {source_expr} AS source"
            f" FROM suggestions"
        ).fetchall()

        by_source: dict[str, int] = {}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1

        graded = [r for r in rows if r["evaluated"] and r["realized_return"] is not None]
        model = [r for r in graded if r["source"] == SOURCE_MODEL]
        with_bench = [r for r in model if r["bench"] is not None]
        without_bench = [r for r in model if r["bench"] is None]

        def avg(rs):
            return _mean([r["realized_return"] for r in rs])

        def edge(rs):
            return _mean([r["realized_return"] - r["bench"] for r in rs])

        # Exact duplicate owner calls: same name, side, instant and price.
        dup_groups: dict[tuple, list[int]] = {}
        for r in rows:
            if r["source"] == SOURCE_OWNER:
                key = (r["created_at"], r["ticker"], r["action"], round(r["price"], 2))
                dup_groups.setdefault(key, []).append(r["id"])
        dup_groups = {k: v for k, v in dup_groups.items() if len(v) > 1}

        # What the weights actually learned from.
        raw_trained = benchmarked = legacy = 0
        if _has(conn, "learning_log"):
            for (detail,) in conn.execute("SELECT detail FROM learning_log"):
                try:
                    d = json.loads(detail)
                except ValueError:
                    continue
                if "skipped" in d:
                    continue
                if "benchmark_return" not in d:
                    legacy += 1
                elif d["benchmark_return"] is None:
                    raw_trained += 1
                else:
                    benchmarked += 1

        orders_total = unledgered = 0
        if _has(conn, "rh_orders"):
            orders_total = conn.execute("SELECT COUNT(*) FROM rh_orders").fetchone()[0]
            if _has(conn, "ingested_fills"):
                unledgered = conn.execute(
                    "SELECT COUNT(*) FROM rh_orders WHERE order_id NOT IN"
                    " (SELECT order_id FROM ingested_fills)"
                ).fetchone()[0]
            else:
                unledgered = orders_total

        return {
            "path": str(path),
            "total_calls": len(rows),
            "by_source": by_source,
            "model_graded": len(model),
            "model_with_benchmark": len(with_bench),
            "model_without_benchmark": len(without_bench),
            "avg_return_with_benchmark": avg(with_bench),
            "avg_return_without_benchmark": avg(without_bench),
            "avg_edge_with_benchmark": edge(with_bench),
            "implausible_returns": sum(
                1 for r in graded if abs(r["realized_return"]) > IMPLAUSIBLE
            ),
            "duplicate_groups": len(dup_groups),
            "duplicate_rows": sum(len(v) - 1 for v in dup_groups.values()),
            "updates_market_relative": benchmarked,
            "updates_raw_return": raw_trained,
            "updates_untracked": legacy,
            "orders_total": orders_total,
            "orders_without_ledger": unledgered,
        }
    finally:
        conn.close()


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def describe(a: dict) -> list[str]:
    """The audit in plain language, findings first."""
    out: list[str] = []
    findings: list[str] = []

    if a["duplicate_rows"]:
        findings.append(
            f"{a['duplicate_rows']} of your owner trades are recorded twice "
            f"(identical name, side, time and price). This is what a device "
            f"move did before the ledger travelled with the brain. They do "
            f"not train the model, but they double your own track record. "
            f"Fix: brain repair --apply"
        )
    if a["updates_raw_return"]:
        total = a["updates_market_relative"] + a["updates_raw_return"]
        findings.append(
            f"{a['updates_raw_return']} of {total} weight updates learned from "
            f"RAW returns because no market benchmark was available. In a "
            f"rising market that rewards every long call, so the learned "
            f"weights can lean bullish in a way that looks like skill. This "
            f"is the one to decide on — send this report before changing anything."
        )
    elif a["updates_untracked"]:
        findings.append(
            f"{a['updates_untracked']} weight updates were made before the "
            f"learning log recorded which benchmark they used, so how they were "
            f"graded cannot be told from the log."
        )
    if a["model_without_benchmark"]:
        findings.append(
            f"{a['model_without_benchmark']} graded model calls have no market "
            f"benchmark, averaging {_pct(a['avg_return_without_benchmark'])} per "
            f"call, versus {_pct(a['avg_return_with_benchmark'])} for the "
            f"{a['model_with_benchmark']} that do. If the first number looks "
            f"too good to be true, it is: those calls were graded before "
            f"market-relative grading existed."
        )
    if a["implausible_returns"]:
        findings.append(
            f"{a['implausible_returns']} graded calls show a move over 60% in "
            f"days — a data accident (split, bad exit price), not a market."
        )
    if a["orders_without_ledger"] and not a["duplicate_rows"]:
        findings.append(
            f"{a['orders_without_ledger']} synced orders have no ledger entry. "
            f"Harmless now (they are recognised by content) and fixed by "
            f"brain repair --apply."
        )

    out.append("BRAIN AUDIT\n" + "-" * 62)
    out.append(f"File: {a['path']}")
    sources = ", ".join(f"{n} {k}" for k, n in sorted(a["by_source"].items()))
    out.append(f"Calls on record: {a['total_calls']}  ({sources})")
    out.append(
        f"Model calls graded: {a['model_graded']}  "
        f"({a['model_with_benchmark']} measured against the market, "
        f"{a['model_without_benchmark']} not)"
    )
    if a["model_with_benchmark"]:
        out.append(
            f"Honest read (market-measured calls only): average "
            f"{_pct(a['avg_return_with_benchmark'])} per call, "
            f"{_pct(a['avg_edge_with_benchmark'])} versus SPY"
        )
    out.append(
        f"Weight updates: {a['updates_market_relative']} market-relative, "
        f"{a['updates_raw_return']} raw-return, {a['updates_untracked']} untracked"
    )
    out.append("")
    if findings:
        out.append("FINDINGS")
        for i, f in enumerate(findings, 1):
            out.append(f"  {i}. {f}")
    else:
        out.append("Nothing wrong found.")
    return out


# --- repair -----------------------------------------------------------------


def repair_duplicates(db_path: str | Path | None = None, apply: bool = False) -> dict:
    """Remove exact duplicate owner calls and rebuild the fills ledger.

    Only touches what the data itself proves wrong: owner calls identical in
    name, side, instant and price. Keeps the earliest of each (the original,
    already graded) and points the ledger at it. Dry-run unless apply=True,
    and an applied run starts with a consistent backup of the whole brain.
    """
    path = Path(db_path).expanduser() if db_path else default_db_path()
    if not path.exists():
        raise FileNotFoundError(f"no brain at {path}")

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        cols = _columns(conn, "suggestions")
        if "source" not in cols:
            return {"groups": 0, "rows_removed": 0, "ledger_linked": 0,
                    "backup": None, "applied": False}

        groups: dict[tuple, list[int]] = {}
        for r in conn.execute(
            "SELECT id, created_at, ticker, action, price FROM suggestions"
            " WHERE source = ? ORDER BY id", (SOURCE_OWNER,)
        ):
            key = (r["created_at"], r["ticker"], r["action"], round(r["price"], 2))
            groups.setdefault(key, []).append(r["id"])
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        extra_ids = [i for ids in dups.values() for i in ids[1:]]

        result = {
            "groups": len(dups),
            "rows_removed": len(extra_ids),
            "ledger_linked": 0,
            "backup": None,
            "applied": False,
        }
        if not apply:
            return result

        backup = path.with_name(
            f"{path.name}.pre-repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        dest = sqlite3.connect(str(backup))
        try:
            conn.backup(dest)  # consistent even if the file is in use
        finally:
            dest.close()
        result["backup"] = str(backup)

        with conn:  # one transaction: all of it or none of it
            for ids in dups.values():
                keep, drop = ids[0], ids[1:]
                marks = ",".join("?" * len(drop))
                if _has(conn, "ingested_fills"):
                    conn.execute(
                        f"UPDATE ingested_fills SET suggestion_id = ?"
                        f" WHERE suggestion_id IN ({marks})", (keep, *drop)
                    )
                if _has(conn, "learning_log"):
                    conn.execute(
                        f"DELETE FROM learning_log WHERE suggestion_id IN ({marks})",
                        drop,
                    )
                conn.execute(f"DELETE FROM suggestions WHERE id IN ({marks})", drop)

            if _has(conn, "rh_orders") and _has(conn, "ingested_fills"):
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO ingested_fills"
                    " (order_id, suggestion_id, ingested_at)"
                    " SELECT o.order_id, MIN(s.id), datetime('now')"
                    " FROM rh_orders o JOIN suggestions s"
                    "   ON s.source = ? AND s.ticker = o.ticker"
                    "  AND s.action = CASE o.side WHEN 'buy' THEN 'BUY' ELSE 'SELL' END"
                    "  AND s.created_at = REPLACE(o.executed_at, 'Z', '+00:00')"
                    "  AND ABS(s.price - o.price) < 0.005"
                    " WHERE o.order_id NOT IN (SELECT order_id FROM ingested_fills)"
                    " GROUP BY o.order_id",
                    (SOURCE_OWNER,),
                )
                result["ledger_linked"] = conn.total_changes - before
        result["applied"] = True
        return result
    finally:
        conn.close()
