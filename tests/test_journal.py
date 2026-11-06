"""Trade memory: tags, round trips, honest verdicts. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from stocksage import journal as jr

NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)


def fill(oid, ticker, side, qty, price, at):
    return {"order_id": oid, "ticker": ticker, "side": side, "quantity": qty, "price": price, "executed_at": at}


def test_tags_parse_and_bad_lines_are_skipped():
    text = jr.TAGS_HEADER + "\n".join([
        "TAG | 2026-10-29T14:35Z | BUY | xyz | aggressive | Breakout | gap and go on volume",
        "TAG | 2026-10-29T15:00Z | SELL | XYZ | SAFE | target | hit it",
        "TAG | not a time | BUY | XYZ | SAFE | x | y",
        "TAG | 2026-10-29T15:00Z | HOLD | XYZ | SAFE | x | y",
        "TAG | 2026-10-29T15:00Z | BUY | ABC | YOLO | x | y",
        "just a note",
    ])
    tags = jr.parse_tags(text)
    assert [(t["ticker"], t["side"], t["play"], t["setup"]) for t in tags] == [
        ("XYZ", "buy", "AGGRESSIVE", "breakout"), ("XYZ", "sell", "SAFE", "target"), ("ABC", "buy", "untagged", "x")]


def test_a_tag_goes_to_the_nearest_fill_once_and_only_within_three_hours():
    tags = jr.parse_tags("TAG | 2026-10-29T14:00Z | BUY | XYZ | SAFE | dip | a")
    fills = [fill("1", "XYZ", "buy", 1, 10, "2026-10-29T14:02:00Z"),
             fill("2", "XYZ", "buy", 1, 10, "2026-10-29T14:30:00Z"),
             fill("3", "XYZ", "buy", 1, 10, "2026-10-29T19:00:00Z")]
    out = {f["order_id"]: f["tag"] for f in jr.attach_tags(fills, tags)}
    assert out["1"]["setup"] == "dip" and out["2"] is None and out["3"] is None


def test_round_trips_are_first_in_first_out_and_same_day_is_not_benchmarked():
    fills = [fill("1", "XYZ", "buy", 2, 10, "2026-10-26T14:00:00Z"),
             fill("2", "XYZ", "buy", 1, 12, "2026-10-29T14:00:00Z"),
             fill("3", "XYZ", "sell", 2.5, 11, "2026-10-29T18:00:00Z")]
    spy = pd.Series([100.0, 101.0, 102.0, 103.0], index=pd.bdate_range("2026-10-26", periods=4))
    trips, open_lots = jr.round_trips(jr.attach_tags(fills, []), spy=spy, now=NOW)
    first, second = trips
    assert first.pnl == 2.0 and first.style == "1-3 days" and first.vs_spy == round(0.10 - 0.03, 4)
    assert second.pnl == -0.5 and second.style == "same day" and second.vs_spy is None and second.hours == 4.0
    assert open_lots[0]["ticker"] == "XYZ" and abs(open_lots[0]["dollars_in"] - 6.0) < 1e-9


def test_no_verdict_on_a_handful_and_clear_losers_are_called_out():
    assert jr.verdict([0.05] * 3).startswith("too few")
    assert jr.verdict([-0.02, -0.03, -0.01, -0.02, -0.025, -0.015, -0.02, -0.03, -0.01, -0.02]).startswith("losing")
    assert jr.verdict([0.02, 0.03, 0.01, 0.02, 0.025, 0.015, 0.02, 0.03, 0.01, 0.02]).startswith("working")
    assert jr.verdict([0.05, -0.05] * 6) == "no clear edge yet"


def test_summary_groups_by_play_and_counts_today():
    tags = jr.parse_tags("\n".join([
        "TAG | 2026-10-29T14:00Z | BUY | AAA | AGGRESSIVE | momentum | a",
        "TAG | 2026-10-29T15:00Z | SELL | AAA | AGGRESSIVE | stop | b",
        "TAG | 2026-10-27T14:00Z | BUY | BBB | SAFE | pullback | c"]))
    fills = [fill("1", "AAA", "buy", 1, 20, "2026-10-29T14:01:00Z"),
             fill("2", "AAA", "sell", 1, 19, "2026-10-29T15:01:00Z"),
             fill("3", "BBB", "buy", 1, 50, "2026-10-27T14:01:00Z"),
             fill("4", "BBB", "sell", 1, 55, "2026-10-28T19:00:00Z"),
             fill("5", "CCC", "buy", 1, 5, "2026-10-29T16:00:00Z")]
    s = jr.summarize(fills, tags, now=NOW, account="555000111")
    assert s["account"] == "••••0111" and "555000111" not in json.dumps(s)
    assert s["by_play"]["AGGRESSIVE"]["pnl"] == -1.0 and s["by_play"]["SAFE"]["pnl"] == 5.0
    assert s["by_exit"]["stop"]["trades"] == 1 and s["by_style"]["same day"]["trades"] == 1
    assert s["today"] == {"orders": 3, "buys": 2}
    assert s["open"][0]["ticker"] == "CCC" and s["open"][0]["play"] == "untagged"
    assert any("Small sample" in line for line in s["lessons"])
    assert "BY PLAY" in jr.render_text(s)


def test_refresh_needs_an_agentic_account_and_saves_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))

    class Client:
        def __init__(self):
            self.asked = None

        def order_history(self, account_number=None):
            self.asked = account_number
            return [fill("1", "AAA", "buy", 1, 20, "2026-10-29T14:01:00Z")]

    class Market:
        def history(self, ticker):
            return None

    class Eng:
        market = Market()

    c = Client()
    assert jr.refresh(Eng(), c, upload=False, account="") is None
    s = jr.refresh(Eng(), c, upload=False, account="555000111", tags_text="")
    assert c.asked == "555000111" and s["open"][0]["ticker"] == "AAA"
    assert json.loads((tmp_path / "trade_memory.json").read_text())["account"] == "••••0111"


def test_tags_with_shares_and_price_rebuild_fills_after_the_last_confirmed_one():
    tags = jr.parse_tags("\n".join([
        "TAG | 2026-10-29T14:00Z | BUY | AAA | SAFE | pullback | 2 | 10.00 | old, already confirmed",
        "TAG | 2026-10-29T16:00Z | SELL | AAA | SAFE | target | 2 | 11.00 | hit the target",
        "TAG | 2026-10-29T17:00Z | BUY | BBB | AGGRESSIVE | gap | why without numbers"]))
    assert tags[0]["qty"] == 2 and tags[0]["price"] == 10.0 and tags[0]["note"] == "old, already confirmed"
    assert tags[2]["qty"] is None and tags[2]["note"] == "why without numbers"
    after = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    fills = jr.fills_from_tags(tags, after=after)
    assert [(f["ticker"], f["side"], f["price"]) for f in fills] == [("AAA", "sell", 11.0)]
    assert fills[0]["order_id"] == jr.fills_from_tags(tags, after=after)[0]["order_id"], "stable ids"


def test_heartbeat_lines_and_trader_state():
    runs = jr.parse_runs("RUN | 2026-10-29T14:40Z | 100.00 | 20.00 | bought AAA\n"
                         "RUN | bad | 1 | 1 | x\nRUN | 2026-10-29T19:40Z | 104.50 | 0 | sold AAA, +$1")
    state = jr.trader_state(runs, NOW)
    assert state["runs_today"] == 2 and state["equity"] == 104.5 and state["cash"] == 0.0
    assert state["equity_first"] == 100.0 and state["equity_peak"] == 104.5 and state["minutes_since"] == 20


def test_confidence_needs_thirty_trades_then_real_evidence():
    assert jr.confidence({"trades": 12})["status"] == "too early"
    base = {"trades": 30, "pnl": 5.0, "verdict": "working: keep doing it", "avg_vs_spy": 0.01}
    assert jr.confidence(base)["status"] == "earned"
    for bad in ({"pnl": -1.0}, {"verdict": "no clear edge yet"}, {"avg_vs_spy": -0.002}):
        assert jr.confidence({**base, **bad})["status"] == "not earned"


def test_the_cloud_keeps_confirmed_fills_and_adds_only_newer_tagged_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))

    class Eng:
        class market:
            @staticmethod
            def history(t):
                return None

    previous = {"account": "••••0111", "grades": ["LIVE SCORECARD x"],
                "fills": [fill("rh1", "AAA", "buy", 2, 10, "2026-10-29T14:00:30+00:00")]}
    tags = ("TAG | 2026-10-29T14:00Z | BUY | AAA | SAFE | pullback | 2 | 10 | strong uptrend dip\n"
            "TAG | 2026-10-29T16:00Z | SELL | AAA | SAFE | target | 2 | 11 | hit the target\n"
            "RUN | 2026-10-29T16:05Z | 102 | 22 | sold AAA")
    s = jr.refresh_without_broker(Eng(), upload=False, previous=previous, tags_text=tags)
    assert s["overall"]["trades"] == 1 and s["overall"]["pnl"] == 2.0
    trip = s["recent"][0]
    assert trip["buy_why"] == "strong uptrend dip" and trip["sell_why"] == "hit the target"
    assert s["account"] == "••••0111" and s["grades"] == ["LIVE SCORECARD x"]
    text = (tmp_path / "trades.txt").read_text(encoding="utf-8")
    assert "bought: strong uptrend dip" in text and "CONFIDENCE" in text and "NOT YET" in text
    again = jr.refresh_without_broker(Eng(), upload=False, previous=s, tags_text=tags)
    assert again["overall"]["trades"] == 1, "a second cloud run must not count the tagged sell twice"
    late_note = tags.replace("2026-10-29T14:00Z | BUY", "2026-10-29T14:06Z | BUY")
    s3 = jr.refresh_without_broker(Eng(), upload=False, previous=previous, tags_text=late_note)
    assert len(s3["fills"]) == 2 and not s3["open"], "a note stamped after the confirmed fill is the same buy"


def test_a_note_is_checked_before_it_is_stored():
    import pytest

    assert jr.append_note("RUN |  2026-10-29T15:35Z | 104.22 | 11.56 | sold JNJ", upload=False) == \
        "RUN | 2026-10-29T15:35Z | 104.22 | 11.56 | sold JNJ"
    assert jr.append_note("TAG | 2026-10-29T15:00Z | SELL | JNJ | SAFE | target | 0.1 | 274.82 | hit it", upload=False)
    for bad in ("hello", "TAG | nope | BUY | X | SAFE | x", "RUN | not a time | 1 | 1 | x"):
        with pytest.raises(ValueError):
            jr.append_note(bad, upload=False)


def test_free_text_setups_fold_into_the_playbook_words():
    assert jr.setup_word("take profit before weekend in selloff") == "profit"
    assert jr.setup_word("Breakout") == "breakout"
    assert jr.setup_word("stopped out") == "stop"
    assert jr.setup_word("pullbacks") == "pullback"
    assert jr.setup_word("vibes") == "vibes" and jr.setup_word("") == "unspecified"


def test_crypto_trades_come_from_notes_and_are_graded_apart_from_stocks(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))

    class Client:
        def order_history(self, account_number=None):
            return [fill("1", "AAA", "buy", 1, 20, "2026-10-28T14:01:00Z"),
                    fill("2", "AAA", "sell", 1, 21, "2026-10-29T15:01:00Z")]

    class Eng:
        class market:
            @staticmethod
            def history(t):
                return None

    tags = ("TAG | 2026-10-27T14:00Z | BUY | BTC-USD | AGGRESSIVE | pullback | 0.0002 | 70000 | dip in uptrend\n"
            "TAG | 2026-10-29T18:00Z | SELL | BTC-USD | AGGRESSIVE | target | 0.0002 | 77000 | hit the target")
    assert jr.is_crypto("btc-usd") and not jr.is_crypto("AAA")
    s = jr.refresh(Eng(), Client(), upload=False, account="555000111", tags_text=tags)
    assert s["by_asset"]["crypto"]["trades"] == 1 and s["by_asset"]["stocks"]["trades"] == 1
    assert "GRADES BY ASSET" in jr.render_text(s)
    cloud = jr.refresh_without_broker(Eng(), upload=False, previous=s, tags_text=tags)
    assert cloud["by_asset"]["crypto"]["trades"] == 1, "older crypto notes survive the cloud rebuild, counted once"
    assert cloud["overall"]["trades"] == 2


def test_broker_crypto_orders_correct_the_notes_and_never_leak_another_account(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    tags = jr.parse_tags(
        "TAG | 2026-10-27T14:00Z | BUY | BTC-USD | SAFE | trend | 0.0002 | 70000 | [MEDIUM] uptrend\n"
        "TAG | 2026-10-29T18:00Z | SELL | BTC-USD | SAFE | target | 0.0002 | 77000 | target")
    broker = [
        {"order_id": "c-1", "ticker": "BTC-USD", "side": "buy", "quantity": 0.000201, "price": 70120.0,
         "executed_at": "2026-10-27T14:02:00Z", "crypto_account": "agentic"},
        {"order_id": "c-2", "ticker": "ETH-USD", "side": "buy", "quantity": 1.0, "price": 3000.0,
         "executed_at": "2026-10-28T14:00:00Z", "crypto_account": "personal"},
        {"order_id": "c-3", "ticker": "SOL-USD", "side": "buy", "quantity": 0.5, "price": 150.0,
         "executed_at": "2026-10-28T15:00:00Z", "crypto_account": "agentic"}]
    out = jr.crypto_fills(tags, broker)
    assert [f["order_id"] for f in out][0] == "c-1" and out[0]["price"] == 70120.0, "broker corrects the note"
    assert {f["ticker"] for f in out} == {"BTC-USD"}, "without the account id, nothing unnoted is taken"
    with_id = jr.crypto_fills(tags, broker, account_id="agentic")
    assert {f["ticker"] for f in with_id} == {"BTC-USD", "SOL-USD"}, "the trading account's own orders, never personal"

    class Client:
        def order_history(self, account_number=None):
            return []

        def crypto_order_history(self):
            return broker

    class Eng:
        class market:
            @staticmethod
            def history(t):
                return None

    text = ("TAG | 2026-10-27T14:00Z | BUY | BTC-USD | SAFE | trend | 0.0002 | 70000 | [MEDIUM] uptrend\n"
            "TAG | 2026-10-29T18:00Z | SELL | BTC-USD | SAFE | target | 0.0002 | 77000 | target")
    s = jr.refresh(Eng(), Client(), upload=False, account="555000111", tags_text=text)
    again = jr.refresh_without_broker(Eng(), upload=False, previous=s, tags_text=text)
    assert s["overall"]["trades"] == 1 and again["overall"]["trades"] == 1, "the cloud never double-counts confirmed crypto"


def test_ideas_are_graded_against_the_market_and_grouped():
    days = pd.bdate_range("2026-10-01", periods=20)
    up = pd.Series([100 + i for i in range(20)], index=days, dtype=float)
    flat = pd.Series([100.0] * 20, index=days)
    text = "\n".join([f"IDEA | 2026-10-0{d}T15:00Z | BUY | UPP | pullback | 100 | [HIGH] dip" for d in range(1, 6)]
                     + ["IDEA | 2026-10-02T15:00Z | SELL | UPP | gap | 100 | fade", "IDEA | bad | BUY | X | y | 1 | z"])
    ideas = jr.parse_ideas(text)
    assert len(ideas) == 6 and ideas[0]["confidence"] == "HIGH"
    g = jr.grade_ideas(ideas, lambda t: up, spy=flat, now=NOW)
    assert g["graded"] == 6 and g["by_setup"]["pullback"]["right"] == 1.0 and g["by_setup"]["gap"]["avg_5d"] < 0
    assert g["by_confidence"]["HIGH"]["ideas"] == 5
    assert jr.append_note("IDEA | 2026-10-29T15:00Z | BUY | AAA | pullback | 10.5 | [LOW] maybe", upload=False)
    assert jr.append_note("DEPOSIT | 2026-10-29T15:00Z | 100", upload=False)


def test_the_account_is_compared_with_holding_spy_net_of_deposits_and_taxes_are_estimated():
    runs = jr.parse_runs("RUN | 2026-10-26T15:00Z | 100 | 10 | a\nRUN | 2026-10-29T15:00Z | 205 | 10 | b")
    deps = jr.parse_deposits("DEPOSIT | 2026-10-27T15:00Z | 100\nDEPOSIT | junk | x")
    spy = pd.Series([100.0, 101.0, 102.0, 103.0], index=pd.bdate_range("2026-10-26", periods=4))
    m = jr.vs_market(runs, spy, deps)
    assert m["account"] == 0.05 and m["spy"] == 0.03 and m["ahead"] == 0.02 and m["deposits"] == 100
    assert jr.drawdown(jr.parse_runs("RUN | 2026-10-26T15:00Z | 100 | 1 | a\nRUN | 2026-10-27T15:00Z | 90 | 1 | b")) == -0.1
    fills = [fill("1", "AAA", "buy", 1, 10, "2026-10-01T14:00:00Z"), fill("2", "AAA", "sell", 1, 8, "2026-10-05T14:00:00Z"),
             fill("3", "AAA", "buy", 1, 8, "2026-10-12T14:00:00Z"), fill("4", "BBB", "buy", 1, 10, "2026-10-01T14:00:00Z"),
             fill("5", "BBB", "sell", 1, 13, "2026-10-05T14:00:00Z")]
    s = jr.summarize(fills, [], now=NOW, account="555000111", runs=runs, deposits=deps)
    assert s["tax"]["realized"] == 1.0 and s["tax"]["wash_sale_losses"] == -2.0
    text = jr.render_text(s)
    assert "VS THE MARKET" in text and "TAX 2026" in text and "wash sales" in text


def test_a_note_mangled_by_the_shell_is_refused_with_the_fix():
    import pytest

    with pytest.raises(ValueError, match="single quotes"):
        jr.append_note("RUN | 2026-11-03T13:40Z | 105.85 | 0.82 | only /bin/bash.82 cash", upload=False)


def test_the_same_idea_noted_twice_counts_once():
    ideas = jr.parse_ideas("IDEA | 2026-11-03T16:38Z | BUY | MSFT | pullback | 515.81 | [LOW] pullback\n"
                           "IDEA | 2026-11-03T16:45Z | BUY | MSFT | pullback | 515.81 | test --help\n"
                           "IDEA | 2026-11-03T16:45Z | BUY | MSFT | bounce | 515.81 | different setup")
    assert [(i["setup"], i["note"]) for i in ideas] == [("pullback", "[LOW] pullback"), ("bounce", "different setup")]


def test_test_lines_are_not_graded_as_ideas():
    text = "\n".join([
        "IDEA | 2026-11-04T15:01Z | BUY | XLU | defensive | 43.05 | [NO] test check only, not a real idea",
        "IDEA | 2026-11-03T16:45Z | BUY | MSFT | pullback | 515.81 | test --help",
        "IDEA | 2026-11-05T13:45Z | BUY | XOM | pullback | 171.75 | [LOW] dup check",
        "IDEA | 2026-11-04T15:01Z | BUY | AMD | earnings | 499.01 | [LOW] passed: earnings eased AI fears"])
    assert [i["ticker"] for i in jr.parse_ideas(text)] == ["AMD"]


def test_the_trader_s_own_notes_keep_the_market_and_the_idea_grades(tmp_path, monkeypatch):
    """Each note rebuilds the memory on the trader's light path (no prices). It used to drop
    SPY and the idea grades, so confidence between two full runs was blind to both."""
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    days = pd.bdate_range("2026-08-03", periods=70)
    spy = pd.Series([600 + i for i in range(70)], index=days)
    tags = ("TAG | 2026-10-28T14:00Z | BUY | AAA | SAFE | pullback | 1 | 10 | [LOW] stop 9.5 target 11\n"
            "TAG | 2026-11-02T15:00Z | SELL | AAA | SAFE | target | 1 | 11 | hit\n"
            "IDEA | 2026-10-28T14:00Z | BUY | BBB | pullback | 20 | [LOW] x\n"
            "RUN | 2026-10-28T14:05Z | 100 | 50 | v32: bought AAA\n"
            "RUN | 2026-11-02T15:05Z | 101 | 61 | v32: sold AAA")
    full = jr.summarize(jr.fills_from_tags(jr.parse_tags(tags)), jr.parse_tags(tags), spy=spy,
                        runs=jr.parse_runs(tags), now=datetime(2026, 11, 3, 15, tzinfo=timezone.utc))
    full["ideas"] = {"graded": 25, "by_setup": {"pullback": {"ideas": 25, "avg_5d": 0.004, "right": 0.6,
                                                             "verdict": "no clear edge yet"}},
                     "by_confidence": {}, "recent": []}
    assert len(full["spy_closes"]) == 70 and full["regime_now"] == "up" and full["trader"]["playbook"] == 32
    light = jr.refresh_without_broker(None, upload=False, previous=full, tags_text=tags)
    assert light["spy_closes"] and light["regime_now"] == "up", "SPY came from the previous memory"
    assert light["vs_market"]["spy"] is not None and light["recent"][0]["vs_spy"] is not None
    assert light["ideas"]["graded"] == 25, "idea grades carried forward, not wiped"


def test_stop_lines_move_the_stop_and_targets_are_read_from_the_why():
    assert jr.stated_target("[LOW] stop 520 target 600, pullback") == 600.0
    text = ("TAG | 2026-11-04T14:37Z | BUY | MSFT | SAFE | pullback | 0.0127 | 549.40 | [LOW] stop 520, test\n"
            "STOP | 2026-11-04T15:01Z | MSFT | 540 | 590 | raised after a 6 percent day\n"
            "STOP | 2026-11-04T15:02Z | PLD | 126.5\n"
            "STOP | 2026-11-04T15:03Z | bad ticker 9 | x")
    stops = jr.parse_stops(text)
    assert [(s["ticker"], s["stop"], s["target"]) for s in stops] == [("MSFT", 540.0, 590.0), ("PLD", 126.5, None)]
    tags = jr.parse_tags(text)
    s = jr.summarize(jr.fills_from_tags(tags), tags, stops=stops, now=datetime(2026, 11, 4, 16, tzinfo=timezone.utc))
    lot = s["open"][0]
    assert lot["stop"] == 540.0 and lot["target"] == 590.0
    assert "stop 540, target 590" in jr.render_text(s)
    assert jr.append_note("STOP | 2026-11-04T15:01Z | MSFT | 540", upload=False)


def test_runs_that_name_no_playbook_say_so():
    s = jr.summarize([], [], runs=jr.parse_runs("RUN | 2026-11-04T15:00Z | 100 | 10 | held"),
                     now=datetime(2026, 11, 4, 16, tzinfo=timezone.utc))
    assert s["trader"]["playbook"] is None and "playbook version not stated" in jr.render_text(s)


def test_a_failed_read_never_overwrites_the_trade_history(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))

    def broken(name):
        raise RuntimeError("Drive said 503")

    with pytest.raises(jr.MemoryUnreadable):
        jr._read_previous_memory(reader=broken, wait=0)
    with pytest.raises(jr.MemoryUnreadable):
        jr._read_previous_memory(reader=lambda n: '{"fills": [', wait=0)
    assert jr._read_previous_memory(reader=lambda n: None, wait=0) is None, "no file yet is not an error"
    monkeypatch.setattr(jr, "_read_previous_memory", lambda: (_ for _ in ()).throw(jr.MemoryUnreadable("x")))
    published = []
    monkeypatch.setattr(jr, "_save_and_publish", lambda summary, upload: published.append(summary))
    with pytest.raises(jr.MemoryUnreadable):
        jr.refresh_without_broker(None, upload=True, tags_text="RUN | 2026-11-05T15:50Z | 104 | 1 | v34: x")
    assert not published


def test_a_short_robinhood_read_keeps_every_confirmed_fill():
    previous = {"fills": [fill("rh-pld", "PLD", "buy", 0.2, 127.25, "2026-10-20T19:53:42+00:00"),
                          fill("rh-msft", "MSFT", "buy", 0.01, 549.4, "2026-11-04T14:37:55+00:00"),
                          fill("tag-x", "ZZZ", "buy", 1, 10, "2026-11-04T14:37:55+00:00")]}
    short = [fill("rh-msft", "MSFT", "buy", 0.01, 549.4, "2026-11-04T14:37:55+00:00"),
             fill("rh-msft2", "MSFT", "sell", 0.01, 536.3, "2026-11-05T15:50:00+00:00")]
    got = jr.keep_confirmed(short, previous)
    assert [f["order_id"] for f in got] == ["rh-pld", "rh-msft", "rh-msft2"], "PLD kept, tag fill not promoted"


def test_a_new_agentic_account_keeps_the_record_but_not_the_old_positions(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    old_fills = [fill("o1", "AAA", "buy", 1, 10, "2026-10-01T15:00:00+00:00"),
                 fill("o2", "AAA", "sell", 1, 11, "2026-10-05T15:00:00+00:00"),
                 fill("o3", "BBB", "buy", 1, 20, "2026-10-06T15:00:00+00:00")]          # still held there
    previous = {"account": "••••0111", "fills": old_fills}
    new_fills = [fill("n1", "CCC", "buy", 2, 5, "2026-11-09T15:00:00+00:00")]
    fills, archived = jr.carry_over(new_fills, previous, "999999999")
    assert [f["order_id"] for f in fills] == ["n1"] and {f["order_id"] for f in archived} == {"o1", "o2", "o3"}
    assert all(f["account"] == "0111" for f in archived)
    s = jr.summarize(fills, [], account="999999999", archived=archived,
                     now=datetime(2026, 11, 9, 16, tzinfo=timezone.utc))
    assert s["overall"]["trades"] == 1, "the old account's closed trade stays in the record"
    assert [o["ticker"] for o in s["open"]] == ["CCC"], "only the new account's holdings are open"
    assert [o["ticker"] for o in s["old_account_open"]] == ["BBB"]
    assert "LEFT IN AN EARLIER ACCOUNT" in jr.render_text(s)
    again, archived2 = jr.carry_over(new_fills, s, "999999999")
    assert len(archived2) == 3 and [f["order_id"] for f in again] == ["n1"], "the archive travels on"
    light = jr.refresh_without_broker(None, upload=False, previous=s, tags_text="")
    assert light["overall"]["trades"] == 1 and len(light["archived_fills"]) == 3


def test_the_same_account_still_merges_its_confirmed_fills():
    previous = {"account": "••••0111", "fills": [fill("o1", "AAA", "buy", 1, 10, "2026-10-01T15:00:00+00:00")]}
    fills, archived = jr.carry_over([], previous, "555000111")
    assert [f["order_id"] for f in fills] == ["o1"] and archived == []


def test_switch_account_saves_the_new_number_and_says_what_is_left(monkeypatch, capsys):
    import argparse

    from stocksage import cli, firstrun

    saved = {}
    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "555000111")
    monkeypatch.setattr(firstrun, "set_agentic_account", lambda a: saved.setdefault("n", a) and a)
    assert cli.cmd_switch_account(argparse.Namespace(number="999999999")) == 0
    out = capsys.readouterr().out
    assert saved["n"] == "999999999" and "••••9999" in out and ".\\start.bat journal" in out
    assert cli.cmd_switch_account(argparse.Namespace(number="999999999")) == 0
    assert "already set" in capsys.readouterr().out
