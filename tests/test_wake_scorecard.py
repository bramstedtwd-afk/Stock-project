"""The watcher that learns: wake-up scorecard, per-stock MOVE sizes, NEAR and HALFWAY. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from stocksage import journal as jr
from stocksage import watcher

# Tue 2026-11-03 16:05 UTC = 11:05 AM ET: market open, 30 min from the :35 run.
NOW = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)


def quotes(table):
    def q(t):
        price, change = table[t]
        return {"symbol": t, "price": price, "day_change": change}
    return q


def lot(ticker, entry, stop=None, target=None, opened="2026-10-30T14:37+00:00"):
    out = {"ticker": ticker, "dollars_in": 50.0, "entry": entry, "opened": opened}
    if stop:
        out["stop"] = stop
    if target:
        out["target"] = target
    return out


def check(state, table, memory, when=NOW, **kw):
    return watcher.check(state, when, watcher.holdings(memory), quotes(table), lambda t: None, None, **kw)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")


# --- 2. per-stock MOVE sizes, against the market ----------------------------------------

def test_a_move_step_follows_the_stocks_normal_day_inside_its_fence():
    ranges = {"AMD": 0.04, "KO": 0.006, "MEME": 0.20}
    assert watcher.move_step("AMD", ranges) == pytest.approx(0.06), "1.5x a 4% day"
    assert watcher.move_step("KO", ranges) == watcher.MOVE_MIN, "never tighter than 1.5%"
    assert watcher.move_step("MEME", ranges) == watcher.MOVE_MAX, "never wider than 8%"
    assert watcher.move_step("NEW", ranges) == watcher.STOCK_MOVE, "no range known: the flat 3%"
    assert watcher.move_step("NEW", ranges, factor=9) == watcher.MOVE_MAX, "tuning cannot leave the fence"


def test_a_jumpy_stock_needs_a_bigger_move_and_a_calm_one_a_smaller():
    memory = {"open": [lot("AMD", 500.0), lot("KO", 70.0)]}
    table = {"AMD": (520.0, 0.04), "KO": (71.4, 0.02), "SPY": (750.0, 0.0)}
    events = check({}, table, memory, ranges={"AMD": 0.04, "KO": 0.01})
    assert not any(e.startswith("MOVE: AMD") for e in events), "+4% is a normal day for AMD (step 6%)"
    move = next(e for e in events if e.startswith("MOVE: KO"))
    assert "+2.0% today" in move and "its normal day is about 1.0%" in move
    assert any(e.startswith("MOVE: AMD") for e in check({}, table, memory)), "without ranges: the flat 3%"


def test_a_move_is_measured_against_the_market_but_a_fund_as_it_is():
    memory = {"open": [lot("MSFT", 500.0), lot("XLK", 200.0)]}
    with_market = {"MSFT": (520.0, 0.04), "XLK": (208.0, 0.04), "SPY": (770.0, 0.025)}
    events = check({}, with_market, memory)
    assert not any(e.startswith("MOVE: MSFT") for e in events), "+4% on a +2.5% day is +1.5% of its own"
    assert any(e.startswith("MOVE: XLK +4.0%") for e in events), "a fund IS the market: measured as it is"
    assert any(e.startswith("MARKET: SPY +2.5%") for e in events), "the market's own move still wakes once"
    alone = dict(with_market, SPY=(750.0, 0.002))
    move = next(e for e in check({}, alone, memory) if e.startswith("MOVE: MSFT"))
    assert "+3.8% against the market (SPY +0.2%)" in move
    no_spy = {"MSFT": (520.0, 0.04), "XLK": (200.0, 0.0)}
    assert any(e.startswith("MOVE: MSFT +4.0% today") for e in check({}, no_spy, memory)), "no SPY quote: raw move"


def test_ranges_come_from_the_brief_and_a_failed_read_keeps_the_last_ones():
    brief = {"ranges": {"amd": 0.041, "BAD": "x", "HUGE": 3.0}, "candidates": [{"ticker": "JNJ", "atr_pct": 0.0247}],
             "focus": [{"ticker": "PLD", "atr_pct": None}]}
    assert watcher.ranges_from_brief(brief) == {"AMD": 0.041, "JNJ": 0.0247}
    state = {}
    assert watcher.read_ranges(state, NOW, reader=lambda: json.dumps(brief))["AMD"] == 0.041

    def down():
        raise OSError("drive is away")

    assert watcher.read_ranges(state, NOW + timedelta(hours=1), reader=down) == {"AMD": 0.041, "JNJ": 0.0247}
    assert watcher.read_ranges({}, NOW, reader=down) == {}, "none ever read: the flat rule applies"


def test_the_brief_carries_a_range_for_every_holding_and_spy(tmp_path):
    from stocksage import advisor
    from tests.conftest import make_ohlcv

    (tmp_path / "trade_memory.json").write_text(json.dumps(
        {"open": [{"ticker": "AMD"}, {"ticker": "BTC-USD"}, {"ticker": "GONE"}]}), encoding="utf-8")

    class Market:
        def history(self, t):
            return None if t == "GONE" else make_ohlcv(days=120, seed=7)

    class Scanned:
        risk = {"atr_pct": 0.0213}

    class Engine:
        market = Market()

    got = advisor.daily_ranges(Engine(), {"JNJ": Scanned()})
    assert got["JNJ"] == 0.0213 and set(got) == {"JNJ", "SPY", "AMD"}, "crypto and unknown tickers are left out"
    assert 0.003 < got["AMD"] < 0.25
    kept = advisor.carry_forward_picks({"candidates": [1], "ranges": {}}, {"ranges": {"AMD": 0.04}})
    assert kept["ranges"] == {"AMD": 0.04}, "a run with no prices keeps the last ranges"


# --- 3. NEAR and HALFWAY ----------------------------------------------------------------

def test_near_fires_before_the_stop_once_per_stop_level_a_day():
    memory = {"open": [lot("ORCL", 132.0, stop=124.0)]}
    state = {}
    far = {"ORCL": (127.0, -0.01), "SPY": (750.0, 0.0)}
    assert not any(e.startswith("NEAR") for e in check(state, far, memory)), "2.4% above the stop: not near"
    close = {"ORCL": (125.2, -0.02), "SPY": (750.0, 0.0)}
    events = check(state, close, memory)
    near = next(e for e in events if e.startswith("NEAR: ORCL"))
    assert "at 125.20 is 1.0% above its stop 124.00 (bought at 132.00)" in near
    assert not any(e.startswith("STOP") for e in events), "near is not touched"
    assert not any(e.startswith("NEAR") for e in check(state, close, memory, when=NOW + timedelta(minutes=5)))
    moved = {"open": [lot("ORCL", 132.0, stop=124.8)]}
    assert any(e.startswith("NEAR") for e in check(state, close, moved, when=NOW + timedelta(minutes=6))), \
        "a moved stop is a new level"
    tomorrow = NOW + timedelta(days=1)
    assert any(e.startswith("NEAR") for e in check(state, close, memory, when=tomorrow)), "a new day asks again"


def test_near_never_replaces_or_delays_the_stop_itself():
    memory = {"open": [lot("ORCL", 132.0, stop=124.0)], "trader": {"equity": 100.0}}
    sent = []
    state = {"last_fire": (NOW - timedelta(minutes=5)).isoformat(), "fires_day": "2026-11-03",
             "fires_today": watcher.DAILY_CAP}
    args = dict(crypto_quote=lambda t: None, news=None, memory=memory, post=lambda u, t, b: sent.append(b),
                push=lambda *a: None)
    close = quotes({"ORCL": (125.2, -0.02), "SPY": (750.0, 0.0)})
    assert watcher.tick(state, NOW, quote=close, **args) is None, "NEAR obeys the cap and the cooldown"
    assert any(e.startswith("NEAR") for e in state["pending"])
    touched = quotes({"ORCL": (123.9, -0.03), "SPY": (750.0, 0.0)})
    text = watcher.tick(state, NOW + timedelta(minutes=1), quote=touched, **args)
    assert text and "STOP: ORCL at 123.90 has touched its stop 124.00" in text, "the stop goes out past both"
    assert watcher.URGENT == ("LIMIT", "STOP"), "only exits and the daily brake skip the rations"


def test_near_is_judged_by_the_stocks_range_and_the_room_the_trade_was_given():
    assert watcher.near_band("X", 100.0, 120.0) == watcher.NEAR_DEFAULT
    assert watcher.near_band("X", 100.0, 120.0, {"X": 0.04}) == pytest.approx(0.02), "half a 4% day"
    assert watcher.near_band("X", 100.0, 120.0, {"X": 0.2}) == watcher.NEAR_MAX
    assert watcher.near_band("X", 100.0, 102.0, {"X": 0.04}) == pytest.approx(0.01), \
        "a stop 2% under the buy price: near only once half that room is gone"
    assert watcher.near_band("X", 100.0, 120.0, {"X": 0.04}, factor=50) == watcher.NEAR_MAX
    fresh = {"open": [lot("SPY", 760.0, stop=740.0)]}       # the parked-cash guard stop, 2.7% under
    assert not any(e.startswith("NEAR") for e in check({}, {"SPY": (756.0, -0.005)}, fresh))
    before_open = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)
    memory = {"open": [lot("ORCL", 132.0, stop=124.0)]}
    assert not any(e.startswith("NEAR") for e in check({}, {"ORCL": (124.5, -0.02), "SPY": (750.0, 0.0)}, memory,
                                                         when=before_open)), "a fractional share cannot sell yet"


def test_halfway_asks_for_a_higher_stop_once_per_target_and_stop_level():
    memory = {"open": [lot("AMD", 500.0, stop=480.0, target=560.0)]}
    state = {}
    assert not any(e.startswith("HALFWAY") for e in check(state, {"AMD": (529.0, 0.01), "SPY": (750.0, 0.0)}, memory))
    table = {"AMD": (531.0, 0.012), "SPY": (750.0, 0.0)}
    half = next(e for e in check(state, table, memory) if e.startswith("HALFWAY: AMD"))
    assert "halfway from its entry 500.00 to its target 560.00; its stop is still 480.00: raise the stop" in half
    assert not any(e.startswith("HALFWAY") for e in check(state, table, memory, when=NOW + timedelta(days=1))), \
        "said once: the trader decided, tomorrow is not news"
    higher = {"open": [lot("AMD", 500.0, stop=490.0, target=560.0)]}
    assert any(e.startswith("HALFWAY") for e in check(state, table, higher, when=NOW + timedelta(days=1))), \
        "a stop moved but still under the entry: one more reminder at the new level"
    safe = {"open": [lot("AMD", 500.0, stop=505.0, target=560.0)]}
    assert not any(e.startswith("HALFWAY") for e in check({}, table, safe)), "stop already above the entry: done"
    at_target = {"AMD": (561.0, 0.02), "SPY": (750.0, 0.0)}
    events = check({}, at_target, memory)
    assert any(e.startswith("TARGET") for e in events) and not any(e.startswith("HALFWAY") for e in events)


def test_after_hours_near_and_halfway_wait_like_any_event_a_fraction_cannot_act_on():
    assert watcher._event_ticker("NEAR: ORCL at 125.20 is 1.0% above its stop") == "ORCL"
    assert watcher._event_ticker("HALFWAY: AMD at 531.00 is halfway") == "AMD"


# --- 1. the wake-up scorecard -----------------------------------------------------------

NOTES = "\n".join([
    "WAKE | 2026-11-03T16:05Z | MOVE: PLD -3.4% today, now 121.00 (bought at 125.00); -3.0% against the market "
    "(SPY -0.4%); NEWS: PLD: Prologis cuts guidance; shares slump (Reuters); NEWS: PLD: Prologis CFO resigns (AP)",
    "TAG | 2026-11-03T16:09Z | SELL | PLD | SAFE | stop | 0.4 | 120.80 | guidance cut",
    "RUN | 2026-11-03T16:11Z | 100.00 | 60.00 | v37: wake-up: sold PLD",
    "AFTER | 2026-11-03T16:05Z | 61 | PLD 121 118.5; SPY 750 751",
    "WAKE | 2026-11-03T18:05Z | STOP: MSFT at 538.90 has touched its stop 540.00: sell, or say in the RUN line why not",
    "RUN | 2026-11-03T18:12Z | 100.00 | 60.00 | v37: wake-up: MSFT recovered above the stop, held",
    "AFTER | 2026-11-03T18:05Z | 60 | MSFT 538.9 545",
    "WAKE | 2026-11-03T19:05Z | MARKET: SPY -1.8% today; HALFWAY: AMD at 531.00 is halfway from its entry 500.00",
    "STOP | 2026-11-03T19:10Z | AMD | 505 | 560 | raised to above the entry",
    "RUN | 2026-11-03T19:12Z | 100.00 | 60.00 | v37: wake-up: raised the AMD stop",
    "WAKE | 2026-11-03T20:20Z | LIMIT: the account is about -4.2% today (-4.20: holdings -4.20, sold +0.00)",
])
LATER = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_wake_lines_split_into_events_even_when_a_headline_holds_a_semicolon():
    first, stop, third, limit = jr.parse_wakes(NOTES)
    assert [(e["kind"], e["ticker"], e["direction"]) for e in first["events"]] == \
        [("MOVE", "PLD", -1), ("NEWS", "PLD", 0)], "two headlines on one stock are one reason"
    assert stop["events"][0]["direction"] == -1
    assert [(e["kind"], e["ticker"], e["direction"]) for e in third["events"]] == \
        [("MARKET", "SPY", -1), ("HALFWAY", "AMD", 1)]
    assert limit["events"][0]["ticker"] is None
    up_against = jr.parse_wakes("WAKE | 2026-11-03T16:05Z | MOVE: X +1.0% today, now 5.00; -3.0% against the market "
                                "(SPY +4.0%)")
    assert up_against[0]["events"][0]["direction"] == -1, "the direction is the move against the market"


def test_each_wake_up_is_graded_on_whether_the_trader_acted_and_the_price_kept_going():
    card = jr.wake_scorecard(NOTES, now=LATER)
    by = card["by_kind"]
    assert list(by) == ["STOP", "LIMIT", "HALFWAY", "MOVE", "MARKET", "NEWS"]
    assert by["MOVE"]["traded"] == 1 and by["MOVE"]["continued"] == 1 and by["MOVE"]["useful"] == 1
    assert by["NEWS"]["acted"] == 1 and by["NEWS"]["continued"] == 1, "the price moved 2% after the headline"
    assert by["STOP"]["answered"] == 1 and by["STOP"]["acted"] == 0 and by["STOP"]["reversed"] == 1
    assert by["HALFWAY"]["adjusted"] == 1 and by["HALFWAY"]["traded"] == 0 and by["HALFWAY"]["priced"] == 0
    assert by["MARKET"]["acted"] == 1, "a market wake-up is answered by any order or stop change"
    assert by["LIMIT"]["answered"] == 0 and by["LIMIT"]["priced"] == 0
    assert by["MOVE"]["verdict"].startswith("too few to judge")
    assert card["recent"][0]["kind"] == "LIMIT" and card["wakes"] == 4
    old = jr.wake_scorecard(NOTES, now=LATER + timedelta(days=90))
    assert old["wakes"] == 0 and old["by_kind"] == {}, "only the last 60 days"


def _many(kind_line, n, after=None, tag=None):
    lines = []
    for i in range(n):
        at = (datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc) + timedelta(hours=2 * i)).strftime("%Y-%m-%dT%H:%MZ")
        lines.append(f"WAKE | {at} | {kind_line}")
        if after:
            lines.append(f"AFTER | {at} | 60 | {after}")
        if tag:
            lines.append(f"TAG | {at[:-4]}{int(at[-4:-1]) + 5:02d}Z | SELL | {tag} | SAFE | stop | 1 | 10 | acted")
    return "\n".join(lines)


def test_tuning_moves_only_inside_its_fence_and_only_with_ten_checked_wake_ups():
    noise = _many("MOVE: PLD -3.4% today, now 121.00", 12, after="PLD 121 121.1")
    few = _many("MOVE: PLD -3.4% today, now 121.00", 9, after="PLD 121 121.1")
    good = _many("MOVE: PLD -3.4% today, now 121.00", 12, after="PLD 121 118")
    unchecked = _many("MOVE: PLD -3.4% today, now 121.00", 30)
    when = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    assert jr.wake_scorecard(noise, now=when)["tuning"]["MOVE"] == 1.5, "noise: a bigger move is needed"
    assert jr.wake_scorecard(few, now=when)["tuning"]["MOVE"] == 1.0, "nine is not enough to change anything"
    assert jr.wake_scorecard(good, now=when)["tuning"]["MOVE"] == 0.9
    assert jr.wake_scorecard(unchecked, now=when)["tuning"]["MOVE"] == 1.0, "no price check, no evidence"
    headlines = jr.wake_scorecard(_many("NEWS: PLD: Prologis raises guidance (AP)", 12, after="PLD 121 121.2"),
                                  now=when)
    assert headlines["tuning"]["NEWS_CAP"] == 1
    assert jr.wake_scorecard(_many("NEAR: PLD at 121.00 is 1.0% above its stop 119.80", 12, after="PLD 121 123"),
                             now=when)["tuning"]["NEAR"] == 0.75
    for card in (jr.wake_scorecard(noise, now=when), headlines, jr.wake_scorecard("", now=when)):
        for key, value in card["tuning"].items():
            lo, hi, _default = jr.WAKE_TUNE_BOUNDS[key]
            assert lo <= value <= hi


def test_stop_and_limit_are_graded_but_can_never_be_tuned_loosened_or_gated():
    assert not set(jr.NEVER_TUNED) & set(jr.WAKE_TUNE_BOUNDS)
    stops = _many("STOP: PLD at 119.00 has touched its stop 119.80", 15, after="PLD 119 125")
    limits = _many("LIMIT: the account is about -4.2% today", 15)
    card = jr.wake_scorecard(stops + "\n" + limits.replace("T1", "T0"), now=datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert card["by_kind"]["STOP"]["verdict"] == "mostly noise", "graded honestly..."
    assert card["tuning"] == {"MOVE": 1.0, "NEAR": 1.0, "NEWS_CAP": 3}, "...and nothing about it changes"
    # Whatever a memory file claims, the watcher re-fences it and has no stop or limit setting to read.
    wild = {"wakes": {"tuning": {"MOVE": 99, "NEAR": -5, "NEWS_CAP": 50, "STOP": 0, "LIMIT": 0, "COOLDOWN": 0}},
            "open": [lot("MSFT", 549.4, stop=540.0)], "trader": {"equity": 100.0}}
    tuned = watcher.tuning(wild)
    assert tuned == {"MOVE": 1.5, "NEAR": 0.75, "NEWS_CAP": 3}
    assert watcher.tuning({"wakes": {"tuning": {"MOVE": "x"}}})["MOVE"] == 1.0 and watcher.tuning({})["NEAR"] == 1.0
    sent = []
    state = {"last_fire": (NOW - timedelta(minutes=5)).isoformat(), "fires_day": "2026-11-03",
             "fires_today": watcher.DAILY_CAP}
    text = watcher.tick(state, NOW, quote=quotes({"MSFT": (538.9, -0.019), "SPY": (750.0, -0.004)}),
                        crypto_quote=lambda t: None, news=None, memory=wild, post=lambda u, t, b: sent.append(b),
                        push=lambda *a: None)
    assert text and "STOP: MSFT at 538.90 has touched its stop 540.00" in text
    assert watcher.DAILY_CAP == 10 and watcher.COOLDOWN == timedelta(minutes=20), "the fixed rations"


def test_the_learned_headline_allowance_can_only_be_lower_than_three():
    memory = {"open": [lot("PLD", 125.0)], "wakes": {"tuning": {"NEWS_CAP": 1}}}
    state = {"news_seen": [], "news_fires_day": "2026-11-03", "news_fires": 1}
    item = [{"id": "n1", "ticker": "PLD", "title": "Prologis raises guidance", "source": "AP",
             "at": NOW.isoformat()}]
    args = dict(quote=quotes({"PLD": (125.0, 0.0), "SPY": (750.0, 0.0)}), crypto_quote=lambda t: None,
                news=lambda t: item, post=lambda u, t, b: {}, names=lambda t: "Prologis, Inc.")
    assert watcher.tick(state, NOW, memory=memory, **args) is None, "one headline wake-up a day was learned"
    item[0]["id"] = "n2"
    fresh = {"news_seen": [], "news_fires_day": "2026-11-03", "news_fires": 1}
    assert watcher.tick(fresh, NOW, memory={"open": memory["open"]}, **args), "unlearned: still three a day"


def test_the_watcher_checks_the_price_an_hour_after_each_wake_up(monkeypatch):
    lines = []
    monkeypatch.setattr(watcher, "note", lambda line, post=None: lines.append(line) or True)
    memory = {"open": [lot("PLD", 125.0)]}
    table = {"PLD": (121.0, -0.034), "SPY": (750.0, -0.004)}
    args = dict(crypto_quote=lambda t: None, news=None, memory=memory, post=lambda u, t, b: {})
    state = {}
    assert watcher.tick(state, NOW, quote=quotes(table), **args)
    assert state["followups"][0]["prices"] == {"PLD": 121.0}
    table["PLD"] = (119.0, -0.05)
    watcher.tick(state, NOW + timedelta(minutes=30), quote=quotes(table), **args)
    assert not any(x.startswith("AFTER") for x in lines), "not yet an hour"
    watcher.tick(state, NOW + timedelta(minutes=61), quote=quotes(table), **args)
    after = [x for x in lines if x.startswith("AFTER")]
    assert after == ["AFTER | 2026-11-03T16:05Z | 61 | PLD 121 119"] and state["followups"] == []
    assert jr.append_note(after[0], upload=False), "a valid note line"
    card = jr.wake_scorecard("\n".join(lines), now=NOW + timedelta(hours=2))
    assert card["by_kind"]["MOVE"]["continued"] == 1, "the journal reads the watcher's own lines"
    with pytest.raises(ValueError):
        jr.append_note("AFTER | 2026-11-03T16:05Z | soon | PLD", upload=False)


def test_a_price_check_that_cannot_get_a_price_gives_up_after_three_hours(monkeypatch):
    lines = []
    monkeypatch.setattr(watcher, "note", lambda line, post=None: lines.append(line) or True)
    state = {"followups": [{"at": "2026-11-03T16:05Z", "iso": NOW.isoformat(), "prices": {"PLD": 121.0, "SPY": 750.0}}]}

    def only_spy(t):
        if t == "PLD":
            raise OSError("no quote")
        return {"price": 752.0, "day_change": 0.0}

    assert watcher.follow_up(state, NOW + timedelta(hours=2), only_spy, lambda t: None) == [] and state["followups"]
    done = watcher.follow_up(state, NOW + timedelta(hours=3, minutes=1), only_spy, lambda t: None)
    assert done == ["AFTER | 2026-11-03T16:05Z | 181 | SPY 750 752"] and state["followups"] == []


def test_the_scorecard_is_in_the_memory_and_the_phone_report():
    spy = pd.Series([100.0, 101.0], index=pd.bdate_range("2026-11-02", periods=2))
    summary = jr.summarize([], [], spy=spy, now=LATER, runs=jr.parse_runs(NOTES),
                           wakes=jr.wake_scorecard(NOTES, now=LATER))
    assert summary["wakes"]["by_kind"]["MOVE"]["wakes"] == 1
    json.dumps(summary["wakes"])
    report = jr.render_text(summary)
    assert "WAKE-UPS GRADED (last 60 days" in report
    assert "MOVE      1 wake-ups, trader acted on 1, price kept going in 1 of 1 checked: too few to judge" in report
    assert "headline wake-ups 3 a day. STOP and LIMIT are never tuned." in report
    assert "WAKE-UPS GRADED" not in jr.render_text(jr.summarize([], [], spy=spy, now=LATER)), "nothing to show yet"


def test_the_light_path_rebuilds_the_scorecard_from_the_notes_alone():
    previous = {"account": "••••1234", "fills": [], "archived_fills": []}
    summary = jr.refresh_without_broker(None, upload=False, previous=previous, tags_text=NOTES)
    assert summary["wakes"]["by_kind"]["STOP"]["wakes"] == 1
