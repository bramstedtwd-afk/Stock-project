"""The scenario lab and what it fixed: market-adjusted verdicts, the calibration cap. Offline."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd

from stocksage import confidence as cf
from stocksage import journal as jr
from stocksage import scenarios as sc


def test_markets_are_reproducible_and_carry_the_planted_truth():
    a, b = sc.build_market("bull", 3), sc.build_market("bull", 3)
    assert (a["prices"] == b["prices"]).all() and len(a["events"]) == len(b["events"]) > 300
    assert {e["setup"] for e in a["events"]} >= {"pullback", "momentum", "gap"}
    assert sc.build_market("crash", 0)["spy"][:, -1].min() < 80, "the crash phase really crashes"


def test_the_stand_in_writes_lines_the_journal_accepts_and_parking_never_trades():
    r = sc.simulate("bull", 1, days=40)              # every line is parsed back; a refused one fails here
    assert r.trades > 10 and r.days == 40 and sum(r.levels.values()) > 50
    park = sc.simulate("bull", 1, sc.AGENTS["park_only"], days=40)
    assert park.trades == 0 and park.levels == {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO": 0}


def test_the_memory_learns_a_strong_planted_edge(monkeypatch):
    edges = sc._e(pullback=0.01, breakout=-0.01)
    monkeypatch.setitem(sc.WORLDS, "lesson", [sc.Phase(120, 0.0, 0.008, edges, "test")])
    r = sc.simulate("lesson", 2)
    n, verdict, truth = r.verdicts["pullback"]
    assert truth == "works" and verdict.startswith("working"), r.verdicts
    assert all(sc.judge(v, t) != "WRONG" for _, v, t in r.verdicts.values())


def test_guardrails_switch_off_cleanly():
    off = sc.simulate("crash", 0, sc.AGENTS["no_discipline"], days=90)
    assert off.blocked == {"limit": 0, "revenge": 0, "cooloff": 0, "brake": 0}
    on = sc.simulate("crash", 0, days=90)
    assert on.blocked["brake"] + on.blocked["cooloff"] > 0


def test_verdict_quality_counts_right_wrong_and_undecided():
    assert sc.judge_t(30, 2.5, 0.006, 2.0, 10) == "right"
    assert sc.judge_t(30, -2.5, 0.0, 2.0, 10) == "WRONG", "an edge called where none was planted"
    assert sc.judge_t(30, 2.5, -0.004, 2.0, 10) == "WRONG"
    assert sc.judge_t(30, 2.5, 0.006, 3.0, 10) == "undecided" and sc.judge_t(5, 9.0, 0.01, 2.0, 10) == "undecided"
    r = sc.Result("w", "system", 0, 1, cells=[("trades", "a", 30, 2.5, 0.006), ("trades", "b", 30, -2.2, 0.0)])
    q = sc.verdict_quality([r], "trades", bars=(2.0, 3.0))
    assert q[0]["right"] == 1 and q[0]["wrong"] == 1 and q[1]["undecided"] == 2 and q[0]["found"] == 1.0


def test_the_brake_can_measure_the_trades_alone():
    trips = [jr.Trip("A", 10, 3.0, 0.3, "2026-11-02T15:00", "2026-11-02T16:00", 1, "same day", "SAFE", "x", "y", None),
             jr.Trip("B", 10, -12.0, -0.5, "2026-11-03T15:00", "2026-11-03T16:00", 1, "same day", "SAFE", "x", "y", None)]
    assert jr.trading_drawdown(trips, 100.0) == -0.12, "12 lost from the trades' best point"
    assert jr.trading_drawdown([], 100.0) == 0.0 and jr.trading_drawdown(trips, None) is None


def test_the_context_table_states_what_the_rules_do():
    rows = {label: level for label, level, _ in sc.context_table()}
    assert rows["Strong setup, account down 4.5% today (daily limit)"].startswith("NO")
    assert rows["Buying back a name stopped out today"].startswith("NO")
    assert rows["Same strong setup the morning after the bad day"].startswith("HIGH")
    assert rows["Crypto trend buy, bitcoin below its 50-day"].startswith("NO")


def test_the_watcher_rehearsal_reaches_the_limit_and_leaves_no_settings_behind(monkeypatch):
    monkeypatch.delenv("STOCKSAGE_TRADER_FIRE_URL", raising=False)
    lines = sc.watcher_rehearsal()
    assert any("LIMIT" in x for x in lines) and any("left to the scheduled run" in x for x in lines)
    assert "STOCKSAGE_TRADER_FIRE_URL" not in os.environ


def test_a_zero_edge_trade_in_a_falling_market_is_judged_against_the_market():
    """The lab caught this: on raw returns a falling market made a no-edge setup 'losing'."""
    days = pd.bdate_range("2026-10-01", periods=40)
    spy = pd.Series([100 * 0.99 ** i for i in range(40)], index=days)
    lines = []
    for i in range(12):
        a, b = days[2 * i], days[2 * i + 2]
        px_a, px_b = 50 * 0.99 ** (2 * i), 50 * 0.99 ** (2 * i + 2) * (1.001 if i % 2 else 0.999)
        lines += [f"TAG | {a:%Y-%m-%d}T19:00Z | BUY | ZZ{chr(65 + i)} | SAFE | news | 1 | {px_a:.4f} | [LOW] x",
                  f"TAG | {b:%Y-%m-%d}T19:00Z | SELL | ZZ{chr(65 + i)} | SAFE | time | 1 | {px_b:.4f} | x"]
    tags = jr.parse_tags("\n".join(lines))
    s = jr.summarize(jr.fills_from_tags(tags), tags, spy=spy, now=datetime(2026, 11, 30, tzinfo=timezone.utc))
    news = s["by_setup"]["news"]
    assert news["avg_return"] < -0.015, "every trade lost about 2% with the market"
    assert abs(news["avg_adj"]) < 0.002 and not news["verdict"].startswith("losing")


def test_the_brake_follows_the_trades_not_the_market_and_calibration_is_reported_not_enforced():
    backed = {"n": 150, "dates": 60, "edge": 0.006, "lower": 0.002, "hit_rate": 0.58, "backed": True}
    strong = dict(setup="pullback", lookalike=backed, regime="up", confirmations=2)
    market_dip = {"drawdown": -0.15, "trading_drawdown": -0.01}
    assert cf.score(memory=market_dip, **strong)["level"] == "HIGH", "a market dip of parked cash is not a brake"
    r = cf.score(memory={"drawdown": -0.02, "trading_drawdown": -0.11}, **strong)
    assert r["level"] == "LOW" and any("BRAKE" in x for x in r["reasons"])
    assert cf.score(memory={"drawdown": -0.12}, **strong)["level"] == "LOW", "an older memory still brakes"
    uncalibrated = {"by_confidence": {"HIGH": {"trades": 12, "avg_adj": -0.002}, "LOW": {"trades": 15, "avg_adj": 0.001}}}
    assert cf.score(memory=uncalibrated, **strong)["level"] == "HIGH"
