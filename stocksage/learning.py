"""Adaptive signal weighting — how StockSage learns from its own record.

Each signal (trend, momentum, RSI reversion, ...) is treated as an expert
voting on direction. Weights start equal and are updated with the classic
multiplicative-weights (Hedge) algorithm every time a past suggestion's
horizon elapses: experts that pointed in the realized direction gain weight,
experts that pointed the wrong way lose it. Hedge has a worst-case regret
guarantee, degrades gracefully in noise, and — unlike a retrained black-box
model — every weight remains inspectable, so you can always see *what* the
tool currently believes works.

Weights are floored so no signal can be permanently silenced: markets are
regime-driven, and a signal that fails for a year may lead the next one.
"""

from __future__ import annotations

import math

# Learning rate: one contradiction costs an expert ~18% of its weight.
DEFAULT_ETA = 0.20
# No expert may fall below this share of uniform weight.
MIN_WEIGHT_FRACTION = 0.10
# Realized moves smaller than this are noise; skip the update entirely.
NOISE_RETURN_THRESHOLD = 0.005


def initial_weights(signal_names: list[str]) -> dict[str, float]:
    if not signal_names:
        return {}
    w = 1.0 / len(signal_names)
    return {name: w for name in signal_names}


def normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return initial_weights(list(weights))
    normalized = {k: v / total for k, v in weights.items()}
    # Enforce the floor, then renormalize the remainder above it.
    floor = MIN_WEIGHT_FRACTION / len(normalized)
    clipped = {k: max(v, floor) for k, v in normalized.items()}
    total = sum(clipped.values())
    return {k: v / total for k, v in clipped.items()}


def update_weights(
    weights: dict[str, float],
    signals: dict[str, float],
    realized_return: float,
    eta: float = DEFAULT_ETA,
) -> tuple[dict[str, float], dict]:
    """One Hedge step from a resolved suggestion.

    `signals` is the signal snapshot stored when the suggestion was made;
    `realized_return` the return over the suggestion's horizon. Returns the
    new weights plus a human-readable detail dict for the learning log.
    Signals present in the snapshot but not yet weighted are admitted at
    uniform weight before the update (lets new signals join seamlessly).
    """
    if abs(realized_return) < NOISE_RETURN_THRESHOLD:
        return dict(weights), {"skipped": "realized move within noise threshold"}

    merged = dict(weights)
    uniform = 1.0 / max(len(signals), 1)
    for name in signals:
        merged.setdefault(name, uniform)

    direction = 1.0 if realized_return > 0 else -1.0
    detail: dict = {"realized_return": realized_return, "updates": {}}
    updated: dict[str, float] = {}
    for name, w in merged.items():
        vote = signals.get(name, 0.0)
        # Loss in [0, 1]: 0 when the expert fully agreed with the outcome,
        # 1 when it fully opposed it. Neutral votes cost 0.5.
        loss = (1.0 - direction * _sign_strength(vote)) / 2.0
        new_w = w * math.exp(-eta * loss)
        updated[name] = new_w
        detail["updates"][name] = {"vote": vote, "loss": round(loss, 4)}

    final = normalize(updated)
    detail["weights_after"] = {k: round(v, 4) for k, v in final.items()}
    return final, detail


def _sign_strength(vote: float) -> float:
    """Clamp a signal vote to [-1, 1] (defensive; signals should already be)."""
    return max(-1.0, min(1.0, vote))


# A sector needs this many graded calls before its own weights get a vote.
MIN_SECTOR_GRADES = 10


def blended_weights(
    global_w: dict[str, float],
    sector_w: dict[str, float],
    sector_grades: int,
    min_grades: int = MIN_SECTOR_GRADES,
    blend: float = 0.5,
) -> dict[str, float]:
    """Global weights blended with a sector's own learned weights.

    Sectors reward different signals (mean reversion in Utilities is not
    momentum in Tech). Each sector accumulates its own Hedge-learned vector;
    once it has enough graded calls to be evidence rather than noise, it
    gets an equal vote alongside the global vector.
    """
    if not sector_w or sector_grades < min_grades:
        return dict(global_w)
    names = set(global_w) | set(sector_w)
    if not names:
        return {}
    uniform = 1.0 / len(names)
    merged = {
        n: (1.0 - blend) * global_w.get(n, uniform) + blend * sector_w.get(n, uniform)
        for n in names
    }
    return normalize(merged)


def weighted_score(signals: dict[str, float], weights: dict[str, float]) -> float:
    """Composite score in [-1, 1] from the current weight vector.

    Only weights for signals actually present are used, renormalized so a
    ticker missing one signal isn't structurally penalized.
    """
    if not signals:
        return 0.0
    merged = dict(weights) if weights else {}
    uniform = 1.0 / len(signals)
    active = {name: merged.get(name, uniform) for name in signals}
    total = sum(active.values())
    if total <= 0:
        return 0.0
    return sum(signals[name] * w for name, w in active.items()) / total
