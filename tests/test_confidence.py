"""Confidence from similar situations: look-alikes, own record, regime, live confirmations. Offline."""

from __future__ import annotations

from stocksage import confidence as cf
from stocksage import journal as jr

BACKED = {"n": 150, "dates": 60, "edge": 0.006, "lower": 0.002, "hit_rate": 0.58, "backed": True}
LOSES = {"n": 150, "dates": 60, "edge": -0.008, "lower": -0.012, "hit_rate": 0.41, "backed": False}
WORKING = {"trades": 14, "avg_return": 0.02, "verdict": "working: keep doing it"}
LOSING = {"trades": 12, "avg_return": -0.015, "verdict": "losing money: do less of this"}


def test_backed_history_plus_a_working_setup_in_an_up_market_is_high():
    r = cf.score(setup="pullback", lookalike=BACKED, memory={"by_setup": {"pullback": WORKING}},
                 regime="up", confirmations=2)
    assert r["level"] == "HIGH" and r["score"] == 7 and "no limit" in r["size"]


def test_history_that_loses_or_a_losing_setup_blocks_the_trade():
    assert cf.score(setup="gap", lookalike=LOSES, regime="up", confirmations=3)["level"] == "NO"
    r = cf.score(setup="gap", lookalike=BACKED, memory={"by_setup": {"gap": LOSING}}, regime="down")
    assert r["level"] == "NO" and any("losing" in x for x in r["reasons"])


def test_no_evidence_at_all_is_a_test_position_not_a_guess():
    r = cf.score(setup="news")
    assert r["level"] == "LOW" and "test position" in r["size"]


def test_small_groups_do_not_count_and_play_is_the_fallback():
    r = cf.score(setup="breakout", play="SAFE", lookalike=BACKED,
                 memory={"by_setup": {"breakout": {"trades": 3}}, "by_play": {"SAFE": WORKING}})
    assert r["score"] == 5 and r["level"] == "HIGH", "3-trade setup group ignored; the SAFE play's record counts"
    assert any("SAFE trades" in x for x in r["reasons"])


def test_short_side_setups_are_not_punished_for_a_falling_market():
    plain = cf.score(setup="pullback", lookalike=BACKED, regime="down")
    inverse = cf.score(setup="inverse", lookalike=BACKED, regime="down")
    assert inverse["score"] == plain["score"] + 1


def test_crypto_uses_bitcoins_trend_and_its_own_record():
    up = cf.score(asset="crypto", setup="trend", crypto_flags={"btc_uptrend": True})
    down = cf.score(asset="crypto", setup="trend", crypto_flags={"btc_uptrend": False})
    fear = cf.score(asset="crypto", setup="fear", crypto_flags={"btc_uptrend": False, "extreme_fear": True})
    assert up["score"] == 1 and down["level"] == "NO" and fear["score"] == 1
    losing = cf.score(asset="crypto", setup="trend", crypto_flags={"btc_uptrend": True},
                      memory={"by_asset": {"crypto": LOSING}})
    assert losing["level"] == "NO"


def test_for_trade_reads_the_brief_and_memory():
    brief = {"evidence": {"AAA": {"buy": BACKED, "sell": LOSES}}, "market": {"regime": {"label": "up"}},
             "crypto": {"flags": {"btc_uptrend": True}}}
    r = cf.for_trade("aaa", "Pullback in an uptrend", play="safe", confirmations=2, brief=brief,
                     memory={"by_setup": {"pullback": WORKING}})
    assert r["ticker"] == "AAA" and r["level"] == "HIGH"
    assert cf.for_trade("BTC-USD", "trend", brief=brief)["score"] == 1
    assert "[HIGH]" in cf.render(r)


def test_the_scorecard_grades_confidence_and_says_when_it_is_not_calibrated():
    lines = []
    for i in range(10):
        d = f"2026-10-{10 + i:02d}"
        lines += [f"TAG | {d}T14:00Z | BUY | H{chr(65 + i)} | SAFE | pullback | 1 | 10 | [HIGH] strong",
                  f"TAG | {d}T15:00Z | SELL | H{chr(65 + i)} | SAFE | target | 1 | {9.9 - 0.01 * i:.2f} | x",
                  f"TAG | {d}T14:00Z | BUY | L{chr(65 + i)} | SAFE | pullback | 1 | 10 | [low] weak",
                  f"TAG | {d}T15:00Z | SELL | L{chr(65 + i)} | SAFE | target | 1 | {10.2 + 0.01 * i:.2f} | x"]
    tags = jr.parse_tags("\n".join(lines))
    assert tags[0]["confidence"] == "HIGH" and tags[2]["confidence"] == "LOW"
    s = jr.summarize(jr.fills_from_tags(tags), tags, account="555000111")
    assert s["by_confidence"]["HIGH"]["trades"] == 10 and s["by_confidence"]["LOW"]["trades"] == 10
    assert any("not calibrated" in x for x in s["lessons"])
    assert "GRADES BY CONFIDENCE" in jr.render_text(s)
