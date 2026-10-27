"""News on what you own: fresh, dated, de-duplicated, and in every alert."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stocksage import news, notify
from stocksage.actions import Action, Plan
from stocksage.db import Database

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t} - {src}</title><link>https://x/{i}</link>"
        f"<pubDate>{(NOW - age).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate><source>{src}</source></item>"
        for i, (t, src, age) in enumerate(items))
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'


FEED = rss([("Texas Instruments beats on revenue, raises outlook", "Reuters", timedelta(hours=3)),
            ("Texas Instruments (TXN) Stock Price, News, Quote &amp; History", "Yahoo", timedelta(hours=1)),
            ("Analyst downgrades Texas Instruments", "MarketWatch", timedelta(days=3)),
            ("Texas Instruments beats on revenue, raises outlook", "Other", timedelta(hours=4)),
            ("Old story", "Paper", timedelta(days=12))])


def test_google_news_is_parsed_dated_and_newest_first():
    items = news.google_news("x", fetch=lambda url: FEED)
    assert items[0]["title"] == "Texas Instruments (TXN) Stock Price, News, Quote & History"
    assert items[1]["publisher"] == "Reuters" and not items[1]["title"].endswith("Reuters")
    assert items[1]["published"] == NOW - timedelta(hours=3)


def test_holding_news_drops_quote_pages_duplicates_and_stale_stories():
    got = news.holding_news("TXN", "Texas Instruments Incorporated", None, NOW, fetch=lambda url: FEED)
    titles = [g["title"] for g in got]
    assert titles == ["Texas Instruments beats on revenue, raises outlook", "Analyst downgrades Texas Instruments"]


def test_the_search_uses_the_plain_company_name():
    urls = []
    news.holding_news("TMO", "Thermo Fisher Scientific Inc.", None, NOW, fetch=lambda u: urls.append(u) or FEED)
    assert "Thermo+Fisher+Scientific%22" in urls[0] and "Inc" not in urls[0]


def test_yfinance_headlines_are_merged_when_it_answers():
    class Market:
        def news(self, ticker, limit=8):
            return [{"title": "TXN wins big contract", "publisher": "Y",
                     "published": (NOW - timedelta(hours=1)).isoformat()}]

    got = news.holding_news("TXN", "Texas Instruments", Market(), NOW, fetch=lambda url: FEED)
    assert got[0]["title"] == "TXN wins big contract"


def test_a_dead_feed_is_just_no_news():
    def down(url):
        raise OSError("no network")
    assert news.holding_news("TXN", "x", None, NOW, fetch=down) == []


def test_company_names_are_looked_up_once_and_cached(tmp_path):
    calls = []
    cache = tmp_path / "names.json"
    news.company_names(["TXN"], cache, lambda t: calls.append(t) or "Texas Instruments Inc")
    assert news.company_names(["TXN"], cache, lambda t: calls.append(t) or "x") == {"TXN": "Texas Instruments Inc"}
    assert calls == ["TXN"]


def test_the_sheet_section_lists_every_holding_freshest_first():
    items = news.holding_news("TXN", "Texas Instruments", None, NOW, fetch=lambda url: FEED)
    lines = news.sheet_lines({"TXN": items, "VTI": []}, {"TXN": -0.021}, NOW)
    assert lines[0] == "  TXN (-2.1% last session):"
    assert "[TODAY 3h ago] Texas Instruments beats" in lines[1] and "(Reuters)" in lines[1]
    assert "[week 3d ago]" in lines[2]
    assert lines[-1] == "  VTI: no news in the last 7 days."


def test_push_lines_show_the_last_day_and_mark_what_is_new():
    items = news.holding_news("TXN", "Texas Instruments", None, NOW, fetch=lambda url: FEED)
    lines, shown = news.push_lines({"TXN": items}, NOW, seen=set())
    assert lines == ["NEW TXN (3h ago): Texas Instruments beats on revenue, raises outlook"]
    again, _ = news.push_lines({"TXN": items}, NOW, seen=shown)
    assert again[0].startswith("TXN (3h ago)"), "already shown, so not NEW"


# ------------------------------------------------------------ in the alerts

class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, url, data, headers):
        self.sent.append((data.decode(), headers))


def _news():
    return {"TXN": news.holding_news("TXN", "Texas Instruments", None, NOW, fetch=lambda url: FEED), "VTI": []}


def test_the_daily_all_clear_carries_todays_news_on_holdings(monkeypatch):
    monkeypatch.delenv("STOCKSAGE_NEWS_IN_ALERTS", raising=False)
    rec, db = Recorder(), Database(":memory:")
    assert notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, NOW, news=_news())
    body = rec.sent[0][0]
    assert body.startswith("Nothing needs doing today")
    assert "News on your holdings, last 24 hours:\nNEW TXN (3h ago): Texas Instruments beats" in body
    assert "$" not in body


def test_tomorrows_message_no_longer_calls_it_new():
    db = Database(":memory:")
    notify.alert_accounts(db, [("Agentic", Plan())], "t", Recorder(), NOW, news=_news())
    rec = Recorder()
    notify.alert_accounts(db, [("Agentic", Plan())], "t", rec, NOW + timedelta(hours=20), news=_news())   # next morning
    assert "\nTXN (" in rec.sent[0][0] and "NEW TXN" not in rec.sent[0][0]


def test_a_quiet_news_day_says_so():
    rec = Recorder()
    notify.alert_accounts(Database(":memory:"), [("Agentic", Plan())], "t", rec, NOW, news={"VTI": []})
    assert "No fresh news on your holdings in the last 24 hours." in rec.sent[0][0]


def test_an_urgent_alert_carries_the_latest_news_on_that_stock_only():
    rec = Recorder()
    stop = Action("EXIT_STOP", "TXN", "SELL", 1, "h", "w", dollars=100.0)
    many = _news()
    many["AAA"] = [{"title": "Unrelated", "publisher": "", "link": "", "published": NOW - timedelta(hours=1)}]
    early = NOW.replace(hour=5)
    notify.alert_accounts(Database(":memory:"), [("Agentic", Plan(actions=[stop]))], "t", rec, early, news=many)
    body = rec.sent[0][0]
    assert "Latest news on these:" in body and "TXN" in body and "Unrelated" not in body


def test_news_in_alerts_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("STOCKSAGE_NEWS_IN_ALERTS", "off")
    rec = Recorder()
    notify.alert_accounts(Database(":memory:"), [("Agentic", Plan())], "t", rec, NOW, news=_news())
    assert "News on your holdings" not in rec.sent[0][0]


def test_a_huge_news_day_stays_under_the_push_size_limit():
    big = {f"T{i}": [{"title": "x" * 200, "publisher": "", "link": "", "published": NOW - timedelta(hours=1)}]
           for i in range(50)}
    rec = Recorder()
    notify.alert_accounts(Database(":memory:"), [("Agentic", Plan())], "t", rec, NOW, news=big)
    assert len(rec.sent[0][0].encode()) <= notify.MAX_BODY + 100


@pytest.fixture(autouse=True)
def _hour(monkeypatch):
    monkeypatch.delenv("STOCKSAGE_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("STOCKSAGE_ALL_CLEAR", raising=False)
