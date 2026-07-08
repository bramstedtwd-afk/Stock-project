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
