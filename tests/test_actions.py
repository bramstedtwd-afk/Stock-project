"""The plan the owner acts on. A wrong line here is a wrong trade, so the
rules are tested one at a time, then as invariants over many accounts.

Fixtures are synthetic on purpose: this repository is public.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from stocksage import actions
from stocksage.actions import (
    ENTRY_FLOOR, MAX_POSITION_FRACTION, TARGET_POSITIONS, build_plan,
    entry_dates_from_orders, format_plan, model_trust, trading_days_between,
)
from stocksage.scoring import Suggestion

TODAY = date(2026, 9, 30)          # a Wednesday
MONDAY_WEEK_AGO = date(2026, 9, 21)  # 7 trading days before TODAY
LONG_AGO = date(2026, 9, 10)       # 14 trading days before TODAY
TRUSTED = {"level": "earned", "n": 150, "edge": 0.01, "se": 0.002}
UNPROVEN = {"level": "unproven", "n": 12, "edge": 0.002, "se": 0.01}
FAILING = {"level": "failing", "n": 251, "edge": -0.0076, "se": 0.0015}


def sug(ticker, action="BUY", score=0.55, price=100.0, atr=0.02, earnings=40,
        why="a solid long-term uptrend", stop=None):
    return Suggestion(
        ticker=ticker, action=action, score=score, risk_adjusted_score=score,
        price=price, signals={}, risk={"atr_pct": atr},
        stop_price=stop if stop is not None else round(price * (1 - 2 * atr), 2),
        earnings_days=earnings, why=why, position_fraction=0.05,
    )


def pos(ticker, shares=1.0, cost=100.0, price=100.0):
    return {"ticker": ticker, "shares": shares, "avg_cost": cost,
            "price": price, "equity": round(shares * price, 2)}


def plan_for(positions, suggestions, cash=50.0, entered=None, trust=TRUSTED, **kw):
    return build_plan(
        positions, cash, suggestions,
        entered if entered is not None else {p["ticker"]: MONDAY_WEEK_AGO for p in positions},
        trust, today=TODAY, **kw,
    )


def kinds(plan):
    return [(a.kind, a.ticker) for a in plan.actions]


# ---------------------------------------------------------------- exits

def test_a_stop_breach_is_urgent_and_sells_everything():
    # cost 100, atr 2% -> stop 96. Price 95 is through it.
    plan = plan_for([pos("AAA", shares=2, cost=100, price=95)], [sug("AAA", atr=0.02)])
    first = plan.actions[0]
    assert (first.kind, first.urgency, first.side) == ("EXIT_STOP", 1, "SELL")
    assert first.shares == 2 and first.dollars == 190.0


def test_a_price_above_the_stop_is_left_alone():
    plan = plan_for([pos("AAA", cost=100, price=97)], [sug("AAA", atr=0.02)])
    assert ("EXIT_STOP", "AAA") not in kinds(plan)


def test_the_target_is_banked():
    # stop 96 -> target 108
    plan = plan_for([pos("AAA", cost=100, price=109)], [sug("AAA", atr=0.02)])
    assert ("EXIT_TARGET", "AAA") in kinds(plan)


def test_ten_trading_days_triggers_the_time_exit_nine_does_not():
    nine = date(2026, 9, 17)   # 9 trading days before TODAY
    ten = date(2026, 9, 16)
    assert trading_days_between(nine, TODAY) == 9
    held = [pos("AAA", cost=100, price=100)]
    s = [sug("AAA", action="HOLD", score=0.0)]
    assert ("EXIT_TIME", "AAA") not in kinds(plan_for(held, s, entered={"AAA": nine}))
    assert ("EXIT_TIME", "AAA") in kinds(plan_for(held, s, entered={"AAA": ten}))


def test_a_position_past_ten_days_that_is_still_working_is_held_not_dumped():
    held = [pos("AAA", cost=100, price=104)]
    plan = plan_for(held, [sug("AAA", action="STRONG BUY")], entered={"AAA": LONG_AGO})
    assert ("HOLD_PAST_TIME", "AAA") in kinds(plan)
    assert ("EXIT_TIME", "AAA") not in kinds(plan)


def test_past_ten_days_and_underwater_is_sold_even_if_the_model_likes_it():
    held = [pos("AAA", cost=100, price=98)]
    plan = plan_for(held, [sug("AAA", action="BUY")], entered={"AAA": LONG_AGO})
    assert ("EXIT_TIME", "AAA") in kinds(plan)


def test_an_oversized_position_is_trimmed_to_the_cap():
    # AAA 80 of 100 total equity -> 80% held; cap 25 -> trim ~55
    held = [pos("AAA", shares=1, cost=80, price=80)]
    plan = plan_for(held, [sug("AAA", price=80)], cash=20.0)
    trim = next(a for a in plan.actions if a.kind == "TRIM")
    assert trim.dollars == pytest.approx(80 - MAX_POSITION_FRACTION * 100, abs=0.01)


def test_a_position_being_sold_in_full_is_not_also_trimmed():
    held = [pos("AAA", shares=1, cost=100, price=90)]  # through the stop, and 90% of account
    plan = plan_for(held, [sug("AAA", atr=0.02)], cash=10.0)
    assert [k for k, t in kinds(plan) if t == "AAA" and k in ("TRIM", "EXIT_STOP")] == ["EXIT_STOP"]


def test_a_sell_rating_is_advice_not_an_order_and_never_masks_a_stop():
    held = [pos("AAA", shares=1, cost=100, price=90)]
    plan = plan_for(held, [sug("AAA", action="STRONG SELL", atr=0.02)])
    assert ("EXIT_SIGNAL", "AAA") not in kinds(plan)
    assert ("EXIT_STOP", "AAA") in kinds(plan)

    calm = plan_for([pos("BBB", price=100)], [sug("BBB", action="SELL", score=-0.5)], cash=200)
    signal = next(a for a in calm.actions if a.kind == "EXIT_SIGNAL")
    assert signal.urgency == actions.FYI and "model's skill" in signal.why


def test_a_warning_appears_just_before_the_stop():
    # stop 96; price 97 is 1.03% above it
    plan = plan_for([pos("AAA", cost=100, price=97)], [sug("AAA", atr=0.02)])
    assert ("HEADS_UP", "AAA") in kinds(plan)


def test_a_calm_position_is_listed_as_needing_nothing():
    plan = plan_for([pos("AAA", shares=1, cost=100, price=102)], [sug("AAA", action="HOLD")],
                    cash=600)  # ~15% of the account: under the cap
    assert plan.quiet == ["AAA"] and not [a for a in plan.actions if a.ticker == "AAA"]


def test_exits_still_fire_when_the_model_is_trailing_the_market():
    """Stops are risk rules; they need no belief in the model's skill."""
    plan = plan_for([pos("AAA", cost=100, price=90)], [sug("AAA")], trust=FAILING)
    assert ("EXIT_STOP", "AAA") in kinds(plan)


def test_exits_still_fire_when_suggestions_are_switched_off():
    plan = plan_for([pos("AAA", cost=100, price=90)], [sug("AAA")], suggestions_enabled=False)
    assert ("EXIT_STOP", "AAA") in kinds(plan)


def test_a_holding_the_scan_did_not_cover_gets_time_exit_but_no_invented_stop():
    plan = plan_for([pos("AAA", cost=100, price=50)], [], entered={"AAA": LONG_AGO})
    assert kinds(plan) == [("EXIT_TIME", "AAA")]


def test_the_most_urgent_action_is_first():
    held = [pos("OLD", cost=100, price=100), pos("HIT", cost=100, price=90)]
    plan = plan_for(held, [sug("OLD", action="HOLD"), sug("HIT")],
                    entered={"OLD": LONG_AGO, "HIT": MONDAY_WEEK_AGO})
    assert [a.urgency for a in plan.actions] == sorted(a.urgency for a in plan.actions)
    assert plan.actions[0].kind == "EXIT_STOP"


# ---------------------------------------------------------------- entries

def test_a_funded_entry_is_sized_in_dollars_inside_the_cap():
    plan = plan_for([], [sug("NEW", price=50)], cash=100.0)
    enter = next(a for a in plan.actions if a.kind == "ENTER")
    assert ENTRY_FLOOR <= enter.dollars <= MAX_POSITION_FRACTION * 100.0
    assert enter.shares == pytest.approx(enter.dollars / 50, rel=1e-3)
    assert enter.stop < 50 < enter.target


def test_cash_below_the_floor_watches_instead_of_deploying_scraps():
    plan = plan_for([], [sug("NEW")], cash=ENTRY_FLOOR - 0.01)
    assert [a.kind for a in plan.actions] == ["WATCH"]
    assert plan.actions[0].dollars is None


def test_a_sale_can_fund_an_entry_and_says_it_must_settle_first():
    held = [pos("AAA", shares=1, cost=100, price=90)]   # stopped out, frees $90
    plan = plan_for(held, [sug("AAA"), sug("NEW", price=40)], cash=1.0)
    enter = next(a for a in plan.actions if a.kind == "ENTER")
    assert enter.needs_sale_first and "settles" in enter.headline
    assert plan.actions[0].kind == "EXIT_STOP"


def test_full_slots_hold_new_ideas_back():
    held = [pos(f"P{i}", price=10, cost=10) for i in range(TARGET_POSITIONS)]
    plan = plan_for(held, [sug("NEW")] + [sug(h["ticker"], action="HOLD") for h in held],
                    cash=500)
    assert not [a for a in plan.actions if a.kind == "ENTER"]
    assert any("slots are in use" in n for n in plan.notes)


def test_earnings_blackout_and_held_names_are_never_proposed_as_entries():
    plan = plan_for([pos("HELD", price=10, cost=10)],
                    [sug("HELD"), sug("EARN", earnings=3), sug("OK", price=20)], cash=200)
    entered = {a.ticker for a in plan.actions if a.kind == "ENTER"}
    assert entered == {"OK"}


def test_switching_suggestions_off_removes_entries_and_says_so():
    plan = plan_for([], [sug("NEW")], cash=200, suggestions_enabled=False)
    assert not plan.actions and any("switched off" in n for n in plan.notes)


def test_the_best_ranked_idea_is_taken_first():
    plan = plan_for([], [sug("LOW", score=0.35), sug("TOP", score=0.9)], cash=200)
    assert [a.ticker for a in plan.actions if a.kind == "ENTER"][0] == "TOP"


# ---------------------------------------------------------------- confidence & trust

def test_a_model_trailing_the_market_can_never_produce_high_confidence():
    plan = plan_for([], [sug("NEW", score=0.95)], cash=200, trust=FAILING,
                    reliability={"NEW": (20, 0.95)})
    assert next(a for a in plan.actions if a.kind == "ENTER").confidence == "low"


def test_high_confidence_needs_conviction_a_good_name_record_and_an_earned_model():
    best = plan_for([], [sug("NEW", score=0.9)], cash=200, trust=TRUSTED,
                    reliability={"NEW": (12, 0.8)})
    assert next(a for a in best.actions if a.kind == "ENTER").confidence == "high"
    unproven = plan_for([], [sug("NEW", score=0.9)], cash=200, trust=UNPROVEN,
                        reliability={"NEW": (12, 0.8)})
    assert next(a for a in unproven.actions if a.kind == "ENTER").confidence == "medium"


def _rows(edges):
    return [{"realized_return": e + 0.0025, "benchmark_return": 0.0025} for e in edges]


def test_trust_is_failing_only_when_the_gap_is_bigger_than_chance():
    steady_loss = [-0.01 + (i % 5 - 2) * 0.002 for i in range(120)]
    assert model_trust(_rows(steady_loss))["level"] == "failing"
    noisy = [(-0.01 if i % 2 else 0.01) for i in range(120)]  # mean 0, huge spread
    assert model_trust(_rows(noisy))["level"] == "unproven"


def test_trust_is_earned_slowly():
    wins = [0.02 + (i % 5 - 2) * 0.002 for i in range(150)]
    assert model_trust(_rows(wins))["level"] == "earned"
    assert model_trust(_rows(wins[:60]))["level"] == "unproven", "too few calls to grant trust"


def test_a_handful_of_calls_proves_nothing_either_way():
    assert model_trust(_rows([-0.05] * 10))["level"] == "unproven"
    assert model_trust([])["level"] == "unproven"


def test_unbenchmarked_calls_do_not_count_towards_trust():
    rows = _rows([-0.01] * 40) + [{"realized_return": 0.9, "benchmark_return": None}] * 500
    assert model_trust(rows)["n"] == 40


def test_the_trust_sentence_never_calls_a_loss_a_gain():
    from stocksage.actions import trust_sentence

    assert "TRAILING" in trust_sentence(FAILING)
    assert "beating" not in trust_sentence(FAILING)
    assert "too early" in trust_sentence({"level": "unproven", "n": 5, "edge": 0.1})


# ---------------------------------------------------------------- invariants

@pytest.mark.parametrize(
    "cash,n_positions,price",
    list(itertools.product([0.0, 1.31, 14.99, 15.0, 60.0, 400.0], [0, 1, 3, 4], [5.0, 120.0, 900.0])),
)
def test_money_can_never_exceed_the_budget_or_the_cap(cash, n_positions, price):
    held = [pos(f"H{i}", shares=1, cost=price, price=price) for i in range(n_positions)]
    candidates = [sug(f"N{i}", score=0.9 - i * 0.05, price=price) for i in range(6)]
    suggestions = [sug(h["ticker"], action="HOLD", price=price) for h in held] + candidates
    plan = plan_for(held, suggestions, cash=cash)

    total = cash + sum(h["equity"] for h in held)
    freed = sum(a.dollars for a in plan.actions
                if a.side == "SELL" and a.kind in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "TRIM"))
    buys = [a for a in plan.actions if a.kind == "ENTER"]
    assert sum(a.dollars for a in buys) <= cash + freed + 0.011
    for a in buys:
        assert ENTRY_FLOOR <= a.dollars <= MAX_POSITION_FRACTION * total + 0.011
    kept = n_positions - len([a for a in plan.actions if a.kind in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME")])
    assert kept + len(buys) <= max(TARGET_POSITIONS, kept)


def test_no_name_ever_gets_two_conflicting_orders():
    held = [pos("AAA", shares=3, cost=100, price=90)]
    plan = plan_for(held, [sug("AAA", action="STRONG SELL", atr=0.02)], cash=10)
    sides = {a.side for a in plan.actions if a.ticker == "AAA" and a.side != "WATCH"}
    assert sides <= {"SELL"}


# ---------------------------------------------------------------- the words

def test_the_text_is_numbered_plain_and_never_claims_to_place_orders():
    held = [pos("AAA", shares=2, cost=100, price=90)]
    text = "\n".join(format_plan(plan_for(held, [sug("AAA"), sug("NEW")], cash=100)))
    assert text.startswith("TODAY'S ACTIONS")
    assert "1. SELL all of AAA" in text
    assert "never places orders" in text
    assert "confirm" in text
    for banned in ("I have placed", "order placed", "executed"):
        assert banned not in text


def test_a_quiet_day_says_so_plainly():
    text = "\n".join(format_plan(plan_for([], [], cash=5)))
    assert "Nothing to do today" in text


def test_the_text_never_contains_an_account_number():
    import re

    text = "\n".join(format_plan(plan_for([pos("AAA", price=90, cost=100)], [sug("AAA")], cash=50)))
    assert not re.search(r"\b\d{9}\b", text)


# ---------------------------------------------------------------- helpers

def test_entry_dates_use_the_latest_buy_and_ignore_sells_and_strangers():
    orders = [
        {"ticker": "AAA", "side": "buy", "executed_at": "2026-09-01T14:00:00Z"},
        {"ticker": "AAA", "side": "buy", "executed_at": "2026-09-21T14:00:00Z"},
        {"ticker": "AAA", "side": "sell", "executed_at": "2026-09-25T14:00:00Z"},
        {"ticker": "ZZZ", "side": "buy", "executed_at": "2026-09-22T14:00:00Z"},
        {"ticker": "AAA", "side": "buy", "executed_at": "not a date"},
    ]
    assert entry_dates_from_orders(orders, {"AAA"}) == {"AAA": date(2026, 9, 21)}


def test_weekends_are_not_trading_days():
    assert trading_days_between(date(2026, 9, 25), date(2026, 9, 28)) == 1
    assert trading_days_between(date(2026, 9, 30), date(2026, 9, 30)) == 0


# ---------------------------------------------------------------- the command

def test_plan_round_trips_through_the_brief_json():
    plan = plan_for([pos("AAA", cost=100, price=90)], [sug("AAA"), sug("NEW", price=40)], cash=100)
    again = actions.Plan.from_dict(plan.to_dict())
    assert [a.to_dict() for a in again.actions] == [a.to_dict() for a in plan.actions]
    assert again.trust == plan.trust and again.notes == plan.notes


def test_the_actions_command_explains_an_unlinked_account(monkeypatch, capsys, tmp_path):
    from stocksage import cli

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(cli, "_sync_and_focus", lambda *a, **k: ({"positions": None}, None))
    assert cli.main(["actions"]) == 1
    out = capsys.readouterr().out
    assert "not linked" in out and "Portfolio tab" in out


def test_the_actions_command_prints_the_plan_and_alerts(monkeypatch, capsys, tmp_path):
    from stocksage import cli, notify
    from stocksage.actions import Plan

    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    plan = plan_for([pos("AAA", cost=100, price=90)], [sug("AAA")], cash=50)
    monkeypatch.setattr(cli, "_sync_and_focus", lambda *a, **k: (
        {"positions": [pos("AAA", cost=100, price=90)], "buying_power": 50.0}, None))
    monkeypatch.setattr("stocksage.advisor.build_brief", lambda *a, **k: {"actions": plan.to_dict()})
    sent = []
    monkeypatch.setattr(notify, "alert_if_new",
                        lambda db, p, *a, **k: sent.append(Plan.from_dict(p.to_dict())) or True)
    assert cli.main(["actions"]) == 0
    out = capsys.readouterr().out
    assert "TODAY'S ACTIONS" in out and "SELL all of AAA" in out
    assert len(sent) == 1
