import pytest

from stocksage.learning import (
    MIN_WEIGHT_FRACTION,
    initial_weights,
    normalize,
    update_weights,
    weighted_score,
)


def test_initial_weights_uniform():
    w = initial_weights(["a", "b", "c", "d"])
    assert all(v == pytest.approx(0.25) for v in w.values())


def test_update_rewards_correct_expert():
    weights = initial_weights(["good", "bad"])
    signals = {"good": 1.0, "bad": -1.0}
    new, detail = update_weights(weights, signals, realized_return=0.05)
    assert new["good"] > weights["good"]
    assert new["bad"] < weights["bad"]
    assert sum(new.values()) == pytest.approx(1.0)


def test_update_symmetric_for_down_moves():
    weights = initial_weights(["bull", "bear"])
    signals = {"bull": 1.0, "bear": -1.0}
    new, _ = update_weights(weights, signals, realized_return=-0.05)
    assert new["bear"] > new["bull"]


def test_noise_moves_skip_update():
    weights = initial_weights(["a", "b"])
    new, detail = update_weights(weights, {"a": 1.0, "b": -1.0}, realized_return=0.001)
    assert new == weights
    assert "skipped" in detail


def test_weight_floor_prevents_extinction():
    weights = initial_weights(["good", "bad"])
    signals = {"good": 1.0, "bad": -1.0}
    for _ in range(200):
        weights, _ = update_weights(weights, signals, realized_return=0.05)
    floor = MIN_WEIGHT_FRACTION / len(weights)
    assert weights["bad"] >= floor * 0.99
    assert sum(weights.values()) == pytest.approx(1.0)


def test_new_signal_admitted_at_uniform():
    weights = {"a": 1.0}
    new, _ = update_weights(weights, {"a": 0.5, "brand_new": 0.5}, realized_return=0.03)
    assert "brand_new" in new


def test_weighted_score_bounds_and_direction():
    signals = {"a": 0.8, "b": 0.4}
    score = weighted_score(signals, {"a": 0.5, "b": 0.5})
    assert score == pytest.approx(0.6)
    assert weighted_score({}, {}) == 0.0
    # missing weights fall back to uniform
    assert weighted_score({"x": 1.0, "y": -1.0}, {}) == pytest.approx(0.0)


def test_normalize_handles_degenerate():
    w = normalize({"a": 0.0, "b": 0.0})
    assert sum(w.values()) == pytest.approx(1.0)
