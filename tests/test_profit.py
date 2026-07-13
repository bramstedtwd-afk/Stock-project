import pytest

from stocksage.db import Database
from stocksage.profit import paper_trades, profit_stats


def graded_db():
    db = Database(":memory:")
    calls = [
        ("AAA", "STRONG BUY", 0.5, 0.06),   # win  +60
        ("BBB", "BUY", 0.3, -0.02),         # loss -20
        ("CCC", "BUY", 0.25, 0.03),         # win  +30
        ("DDD", "SELL", -0.4, -0.05),       # avoid call: saved +50
        ("EEE", "STRONG SELL", -0.6, 0.02), # avoid call that missed: -20 avoided
    ]
    for ticker, action, score, realized in calls:
        sid = db.record_suggestion(ticker, action, score, 100.0, {"x": score}, 5)
        db.mark_evaluated(sid, realized, (realized > 0) == (score > 0))
    return db


def test_ledger_math():
    db = graded_db()
    buys, avoided = paper_trades(db.evaluated_suggestions(), stake=1000.0)
    assert [round(t.pnl) for t in buys] == [60, -20, 30]
    assert [round(t.cumulative) for t in buys] == [60, 40, 70]
    assert [round(t.pnl) for t in avoided] == [50, -20]


def test_stats():
    db = graded_db()
    buys, avoided = paper_trades(db.evaluated_suggestions(), stake=1000.0)
    stats = profit_stats(buys, avoided)
    assert stats["trades"] == 3
    assert stats["total_pnl"] == pytest.approx(70.0)
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["profit_factor"] == pytest.approx(90 / 20)
    assert stats["risk_avoided"] == pytest.approx(30.0)
    assert stats["best"].ticker == "AAA" and stats["worst"].ticker == "BBB"
    assert stats["return_per_trade"] == pytest.approx(70 / 3000)


def test_empty_ledger():
    buys, avoided = paper_trades([])
    stats = profit_stats(buys, avoided)
    assert stats["trades"] == 0 and stats["total_pnl"] == 0
    assert stats["win_rate"] is None and stats["profit_factor"] is None


def test_stake_env_override(monkeypatch):
    from stocksage.profit import default_stake

    monkeypatch.setenv("STOCKSAGE_STAKE", "2500")
    assert default_stake() == 2500.0
    monkeypatch.setenv("STOCKSAGE_STAKE", "not-a-number")
    assert default_stake() == 1000.0


def test_benchmark_comparison_only_counts_covered_trades():
    db = Database(":memory:")
    # Two buys graded against SPY (one beat it, one lagged), one legacy buy without.
    for ticker, realized, bench in (("AAA", 0.06, 0.02), ("BBB", 0.01, 0.03)):
        sid = db.record_suggestion(ticker, "BUY", 0.5, 100.0, {"x": 0.5}, 5)
        db.mark_evaluated(sid, realized, realized > 0, benchmark_return=bench)
    sid = db.record_suggestion("OLD", "BUY", 0.5, 100.0, {"x": 0.5}, 5)
    db.mark_evaluated(sid, 0.04, True)  # older brains carry no benchmark

    buys, avoided = paper_trades(db.evaluated_suggestions(), stake=1000.0)
    stats = profit_stats(buys, avoided)
    assert stats["covered_trades"] == 2
    assert stats["covered_pnl"] == pytest.approx(70.0)
    assert stats["benchmark_pnl"] == pytest.approx(50.0)
    assert stats["edge_vs_market"] == pytest.approx(20.0)
    # The headline P&L still includes every trade, covered or not.
    assert stats["total_pnl"] == pytest.approx(110.0)
