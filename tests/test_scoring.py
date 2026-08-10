from stocksage.scoring import (
    build_suggestion,
    classify,
    position_size,
    risk_multiplier,
)


def test_classify_bands():
    assert classify(0.6) == "STRONG BUY"
    assert classify(0.3) == "BUY"
    assert classify(0.0) == "HOLD"
    assert classify(-0.3) == "SELL"
    assert classify(-0.6) == "STRONG SELL"


def test_risk_multiplier_monotone():
    calm = risk_multiplier(0.10)
    mid = risk_multiplier(0.35)
    wild = risk_multiplier(0.90)
    assert calm == 1.0
    assert calm > mid > wild
    assert wild == 0.35
    assert risk_multiplier(None) < 1.0  # unknown risk is penalized


def test_position_size_caps_and_scales():
    assert position_size(0.1, 0.01) == 0.0  # below buy threshold
    big = position_size(0.9, 0.01)
    small = position_size(0.9, 0.04)
    assert 0 < small < big <= 0.10


def test_build_suggestion_buy_path():
    risk = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}
    s = build_suggestion("TEST", {"trend_long": 0.9}, score=0.8, risk=risk, sector="Technology")
    assert s.action in ("BUY", "STRONG BUY")
    assert s.position_fraction > 0
    assert s.stop_price is not None and s.stop_price < 100.0


def test_build_suggestion_sell_note_depends_on_ownership():
    risk = {"price": 50.0, "atr_pct": 0.02, "drawdown_52w": -0.4, "annualized_vol": 0.30}
    owned = build_suggestion("T", {"trend_long": -0.9}, -0.8, risk, owned_shares=10)
    not_owned = build_suggestion("T", {"trend_long": -0.9}, -0.8, risk, owned_shares=0)
    assert any("actionable" in n for n in owned.notes)
    assert any("avoid" in n for n in not_owned.notes)
    assert owned.position_fraction == 0.0


def test_high_volatility_downgrades_action():
    calm_risk = {"price": 100.0, "atr_pct": 0.01, "drawdown_52w": 0.0, "annualized_vol": 0.10}
    wild_risk = {"price": 100.0, "atr_pct": 0.05, "drawdown_52w": 0.0, "annualized_vol": 0.90}
    calm = build_suggestion("A", {"x": 0.5}, 0.5, calm_risk)
    wild = build_suggestion("B", {"x": 0.5}, 0.5, wild_risk)
    assert calm.risk_adjusted_score > wild.risk_adjusted_score


def test_earnings_blackout_zeroes_size_but_keeps_call():
    risk = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}
    s = build_suggestion(
        "TEST", {"trend_long": 0.9}, score=0.8, risk=risk, earnings_days=2
    )
    assert s.action in ("BUY", "STRONG BUY")  # the read is unchanged...
    assert s.position_fraction == 0.0          # ...but no new entry into the print
    assert s.stop_price is None
    assert any("Earnings in 2 day" in n for n in s.notes)


def test_earnings_far_away_does_not_gate():
    risk = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}
    s = build_suggestion(
        "TEST", {"trend_long": 0.9}, score=0.8, risk=risk, earnings_days=20
    )
    assert s.position_fraction > 0


def test_earnings_warns_when_you_already_hold_into_a_print():
    """A print you're holding through is a decision, even with no buy signal."""
    risk = {"price": 100.0, "atr_pct": 0.015, "drawdown_52w": -0.05, "annualized_vol": 0.20}
    s = build_suggestion(
        "TEST", {"trend_long": -0.9}, score=-0.5, risk=risk,
        owned_shares=10.0, earnings_days=1,
    )
    assert any("earnings are in 1 day" in n for n in s.notes)


def test_why_sentence_names_drivers_and_tension():
    from stocksage.scoring import why_sentence

    why = why_sentence(
        "AAPL", "BUY", {"trend_long": 0.8, "momentum_20d": 0.6, "rsi_reversion": -0.5}
    )
    assert "AAPL" in why
    assert "uptrend" in why
    assert "caution" in why  # the opposing signal is disclosed, not hidden

    hold = why_sentence("MSFT", "HOLD", {"trend_long": 0.1})
    assert "MSFT" in hold and "no reason to act" in hold


def test_every_suggestion_carries_a_why():
    risk = {"price": 50.0, "atr_pct": 0.02, "drawdown_52w": -0.10, "annualized_vol": 0.30}
    s = build_suggestion("XYZ", {"trend_long": -0.9, "momentum_20d": -0.7}, score=-0.7, risk=risk)
    assert s.why and "XYZ" in s.why


def test_sector_caps_halve_third_idea_per_sector():
    from stocksage.scoring import apply_sector_caps

    risk = {"price": 100.0, "atr_pct": 0.01, "drawdown_52w": 0.0, "annualized_vol": 0.20}
    ideas = [
        build_suggestion(t, {"trend_long": 0.9}, score=0.8, risk=risk, sector="Technology")
        for t in ("AAA", "BBB", "CCC")
    ]
    full = ideas[0].position_fraction
    assert full > 0
    apply_sector_caps(ideas)  # already sorted best-first (equal here)
    assert ideas[0].position_fraction == full
    assert ideas[1].position_fraction == full
    assert ideas[2].position_fraction == round(full / 2, 4)
    assert any("size halved" in n for n in ideas[2].notes)
