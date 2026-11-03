"""The live wire: calendar, earnings, wires, press releases, Fed, odds. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from stocksage import live

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)   # Tue 10:00 AM ET


def rss(items):
    body = "".join(f"<item><title>{t}</title><description>{d}</description><pubDate>{p}</pubDate></item>"
                   for t, d, p in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


PAGES = {
    live.CALENDAR: json.dumps([
        {"title": "JOLTS Job Openings", "country": "USD", "date": "2026-11-03T10:00:00-05:00", "impact": "Medium",
         "forecast": "7.15M", "previous": "7.08M"},
        {"title": "ISM Services PMI", "country": "USD", "date": "2026-11-04T10:00:00-05:00", "impact": "High",
         "forecast": "52.0", "previous": "51.8"},
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
         "impact": "High", "forecast": "65K", "previous": "50K"},
        {"title": "Bank Holiday", "country": "CAD", "date": "2026-11-03T00:00:00-05:00", "impact": "High"},
        {"title": "Low thing", "country": "USD", "date": "2026-11-03T11:00:00-05:00", "impact": "Low"}]),
    live.EARNINGS.format(day="2026-11-03"): json.dumps({"data": {"rows": [
        {"symbol": "SMOL", "name": "Small &amp; Co", "time": "time-pre-market", "marketCap": "$1,000,000", "epsForecast": "$0.10"},
        {"symbol": "AMD", "name": "Advanced Micro Devices", "time": "time-after-hours", "marketCap": "$824,073,390,000",
         "epsForecast": "$1.68"}]}}),
    live.EARNINGS.format(day="2026-11-04"): json.dumps({"data": {"rows": []}}),
    live.FEEDS["MarketWatch"]: rss([("Nasdaq tumbles as AI worries mount", "", "Tue, 03 Nov 2026 13:00:00 GMT"),
                                    ("My stepmother wants $40,000 back. Should I pay?", "", "Tue, 03 Nov 2026 13:00:00 GMT"),
                                    ("Old story", "", "Sun, 01 Nov 2026 13:00:00 GMT")]),
    live.FEEDS["WSJ Markets"]: rss([("Nasdaq tumbles as AI worries mount", "", "Tue, 03 Nov 2026 12:00:00 GMT")]),
    live.PRESS: rss([("Oracle Announces Pricing of Notes", "/PRNewswire/ -- Oracle (NYSE: ORCL) today", "Tue, 03 Nov 2026 14:30:00 GMT"),
                     ("Xinhua Silk Road: something", "no listing here", "Tue, 03 Nov 2026 14:30:00 GMT")]),
    live.FED: rss([("Federal Reserve issues FOMC statement", "", "Mon, 02 Nov 2026 19:00:00 GMT")]),
    live.ODDS.format(tag="fed"): json.dumps([{"id": "1", "title": "Fed Decision in December?", "volume24hr": 600000,
                                              "endDate": "2026-12-09T00:00:00Z", "markets": [
        {"groupItemTitle": "No change", "outcomePrices": "[\"0.255\", \"0.745\"]", "active": True, "closed": False},
        {"groupItemTitle": "25 bps increase", "outcomePrices": "[\"0.725\", \"0.275\"]", "active": True, "closed": False}]},
        {"id": "2", "title": "Tiny market", "volume24hr": 10, "markets": []}]),
    live.ODDS.format(tag="economy"): json.dumps([{"id": "1", "title": "Fed Decision in December?", "volume24hr": 600000,
                                                  "markets": []}]),
}


def fake(url):
    if url not in PAGES:
        raise RuntimeError("no page")
    return PAGES[url]


def test_the_wire_reads_every_source():
    w = live.wire(["amd"], fetch=fake, now=NOW)
    assert [e["title"] for e in w["calendar"]] == ["JOLTS Job Openings", "ISM Services PMI"]
    assert w["calendar"][0]["done"] and not w["calendar"][1]["done"]
    assert [e["title"] for e in w["week_ahead"]] == ["Non-Farm Employment Change"]
    assert w["earnings_today"][0]["ticker"] == "AMD" and w["earnings_today"][0]["when"] == "after the close"
    assert w["your_tickers_reporting"][0]["ticker"] == "AMD"
    assert [h["title"] for h in w["headlines"]] == ["Nasdaq tumbles as AI worries mount"], "deduped, no advice column"
    assert w["press_releases"] == [{"title": "Oracle Announces Pricing of Notes", "source": "PR Newswire",
                                    "hours_ago": 0.5, "ticker": "ORCL"}]
    assert w["fed"][0]["title"].startswith("Federal Reserve")
    assert w["odds"] == [{"question": "Fed Decision in December?", "ends": "2026-12-09",
                          "top": [{"outcome": "25 bps increase", "chance": 0.725}, {"outcome": "No change", "chance": 0.255}]}]
    text = live.render(w)
    assert "YOUR TICKERS REPORT EARNINGS" in text and "25 bps increase 72%" in text and "ORCL:" in text


def test_every_source_down_still_renders():
    def down(url):
        raise RuntimeError("offline")

    w = live.wire(["AMD"], fetch=down, now=NOW)
    assert w["calendar"] == [] and w["odds"] == [] and "LIVE WIRE" in live.render(w)


def test_tests_never_reach_the_network(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    w = live.wire([], now=NOW)
    assert w["headlines"] == [] and w["calendar"] == []
