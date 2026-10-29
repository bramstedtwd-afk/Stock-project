"""Trade memory: tags, round trips, honest verdicts. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

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
    s = jr.summarize(fills, tags, now=NOW, account="000111222")
    assert s["account"] == "••••1222" and "000111222" not in json.dumps(s)
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
    s = jr.refresh(Eng(), c, upload=False, account="000111222", tags_text="")
    assert c.asked == "000111222" and s["open"][0]["ticker"] == "AAA"
    assert json.loads((tmp_path / "trade_memory.json").read_text())["account"] == "••••1222"
