"""The social pulse: trending, Reddit surges, crowd mood, crypto fear/greed. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from stocksage import social

NOW = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)

PAGES = {
    social.STOCKTWITS_TRENDING: {"symbols": [
        {"symbol": "AAA", "title": "Alpha Inc", "instrument_class": "Stock", "trending_score": 9.1,
         "trends": {"summary": "Earnings beat but guidance cut; traders split."}}]},
    social.STOCKTWITS_STREAM.format(t="AAA"): {"messages": [
        {"created_at": "2026-11-03T01:50:00Z", "entities": {"sentiment": {"basic": "Bullish"}}},
        {"created_at": "2026-11-03T01:20:00Z", "entities": {"sentiment": {"basic": "Bullish"}}},
        {"created_at": "2026-11-03T00:50:00Z", "entities": {"sentiment": {"basic": "Bearish"}}},
        {"created_at": "2026-11-03T00:50:00Z", "entities": {"sentiment": None}}]},
    social.APEWISDOM: {"results": [
        {"ticker": "AAA", "name": "Alpha &amp; Co", "mentions": 200, "mentions_24h_ago": 20, "rank": 1, "rank_24h_ago": 30},
        {"ticker": "SPY", "name": "S&amp;P fund", "mentions": 150, "mentions_24h_ago": 30, "rank": 2, "rank_24h_ago": 3},
        {"ticker": "TINY", "name": "Tiny", "mentions": 9, "mentions_24h_ago": 1, "rank": 40, "rank_24h_ago": 90}]},
    social.CRYPTO_FEAR_GREED: {"data": [{"value": "26", "value_classification": "Fear"}, {"value": "37"}]},
    social.COINGECKO_TRENDING: {"coins": [{"item": {"symbol": "BTC", "name": "Bitcoin", "market_cap_rank": 1,
                                                    "data": {"price_change_percentage_24h": {"usd": -1.7}}}}]},
}


def fake(url):
    if url not in PAGES:
        raise RuntimeError("no such page")
    return json.dumps(PAGES[url])


def test_pulse_reads_every_source():
    p = social.pulse(["aaa", "AAA", "ZZZ"], fetch=fake, now=NOW)
    assert p["stocktwits_trending"][0]["why"].startswith("Earnings beat")
    s = p["tickers"]["AAA"]
    assert (s["bullish"], s["bearish"], s["bull_share"]) == (2, 1, 0.67)
    assert s["posts_per_hour"] == 4.0 and s["newest_minutes"] == 10
    assert "ZZZ" not in p["tickers"], "a ticker whose page fails is just missing"
    assert p["crypto"]["fear_greed"] == 26 and p["crypto"]["fear_greed_yesterday"] == 37
    assert p["crypto"]["trending"][0] == {"symbol": "BTC", "name": "Bitcoin", "rank": 1, "d1": -0.017}
    assert p["reddit_top"][0]["name"] == "Alpha & Co"


def test_a_surge_must_climb_the_ranking_not_just_beat_a_quiet_sunday():
    rows = {r["ticker"]: r for r in social.reddit(fetch=fake)}
    assert rows["AAA"]["surging"] and rows["AAA"]["change"] == 10.0
    assert not rows["SPY"]["surging"], "5x mentions but the same rank: every ticker was quiet the day before"
    assert not rows["TINY"]["surging"], "too few mentions to mean anything"


def test_every_source_down_still_gives_a_packet_and_text():
    def down(url):
        raise RuntimeError("offline")

    p = social.pulse(["AAA"], fetch=down, now=NOW)
    assert p["stocktwits_trending"] == [] and p["reddit_top"] == [] and p["tickers"] == {} and p["crypto"] == {}
    assert "not their direction" in social.render(p)


def test_render_marks_surges_and_mood():
    text = social.render(social.pulse(["AAA"], fetch=fake, now=NOW))
    assert "AAA" in text and "SURGING" in text and "67% bullish" in text and "Fear & Greed: 26 Fear" in text


def test_tests_never_reach_the_network():
    p = social.pulse(["AAA"], now=NOW)
    assert p["stocktwits_trending"] == [] and p["crypto"] == {}
