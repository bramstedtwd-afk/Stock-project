"""The trader watcher and Finnhub: events, rationing, the fire call. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from stocksage import finnhub, watcher

# Tue 2026-11-03 16:05 UTC = 11:05 AM ET: market open, 30 min from the :35 run.
NOW = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)
MEMORY = {"open": [{"ticker": "PLD", "dollars_in": 25.0, "entry": 125.0},
                   {"ticker": "PLD", "dollars_in": 25.0, "entry": 125.0},
                   {"ticker": "BTC-USD", "dollars_in": 10.0, "entry": 70000.0}]}


def quotes(table):
    def q(t):
        price, change = table[t]
        return {"symbol": t, "price": price, "day_change": change}
    return q


def no_news(t):
    return []


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")


def test_holdings_merge_lots_with_an_average_entry():
    held = {h["ticker"]: h for h in watcher.holdings(MEMORY)}
    assert held["PLD"]["entry"] == 125.0 and held["PLD"]["dollars"] == 50.0 and "BTC-USD" in held


def test_quiet_around_the_scheduled_runs_only_on_weekdays_in_their_hours():
    at = lambda h, m, d=3: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert watcher.near_scheduled_run(at(16, 30)) and watcher.near_scheduled_run(at(16, 45))
    assert not watcher.near_scheduled_run(at(16, 5)) and not watcher.near_scheduled_run(at(16, 55))
    assert not watcher.near_scheduled_run(at(11, 30)), "no run at 11:35 UTC"
    assert not watcher.near_scheduled_run(at(16, 30, d=7)), "Saturday"


def test_a_big_move_fires_once_per_step_with_plain_facts():
    sent = []
    state = {}
    post = lambda url, token, body: sent.append((url, token, body)) or {}  # noqa: E731
    table = {"PLD": (121.0, -0.034), "SPY": (750.0, -0.004)}
    text = watcher.tick(state, NOW, quote=quotes(table), crypto_quote=lambda t: 70000.0, news=no_news,
                        memory=MEMORY, post=post)
    assert "MOVE: PLD -3.4% today, now 121.00 (bought at 125.00)" in text
    assert sent[0][1] == "tok" and sent[0][2]["text"] == text
    later = NOW + timedelta(minutes=60)          # 12:05 ET: clear of the 11:35 and 12:35 runs
    assert watcher.tick(state, later, quote=quotes(table), crypto_quote=lambda t: 70000.0, news=no_news,
                        memory=MEMORY, post=post) is None, "same move, no second wake-up"
    table["PLD"] = (118.0, -0.062)
    text2 = watcher.tick(state, later + timedelta(minutes=1), quote=quotes(table), crypto_quote=lambda t: 70000.0,
                         news=no_news, memory=MEMORY, post=post)
    assert "MOVE: PLD -6.2%" in text2 and "DROP: PLD" in text2


def test_rationing_cooldown_cap_and_scheduled_runs():
    state = {"last_fire": (NOW - timedelta(minutes=5)).isoformat()}
    sent = []
    table = {"PLD": (130.0, 0.04), "SPY": (760.0, 0.002)}
    args = dict(quote=quotes(table), crypto_quote=lambda t: 70000.0, news=no_news, memory=MEMORY,
                post=lambda u, t, b: sent.append(b))
    assert watcher.tick(state, NOW, **args) is None and state["pending"], "waits out the cooldown, keeps the event"
    assert watcher.tick(state, NOW + timedelta(minutes=16), **args) is not None, "sends the held event later"
    assert sent and "MOVE: PLD +4.0%" in sent[0]["text"]
    near = {"fires_day": "2026-11-03", "fires_today": 0}
    table["PLD"] = (131.0, 0.04)
    assert watcher.tick(near, datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc), **args) is None
    assert near["pending"] == [], "the scheduled run covers it; nothing queued"
    capped = {"fires_day": "2026-11-03", "fires_today": watcher.DAILY_CAP}
    assert watcher.may_fire(capped, NOW)[0] is False


def test_market_move_and_news_and_crypto():
    state = {}
    table = {"PLD": (125.0, 0.0), "SPY": (740.0, -0.018)}
    headlines = [{"id": "1", "ticker": "PLD", "title": "Prologis raises guidance", "source": "Reuters",
                  "at": (NOW - timedelta(minutes=30)).isoformat()}]
    seen_first = watcher.check(state, NOW, watcher.holdings(MEMORY), quotes(table), lambda t: 70000.0,
                               lambda t: headlines)
    assert "MARKET: SPY -1.8% today" in seen_first
    assert not any(e.startswith("NEWS") for e in seen_first), "the first look only learns what is already out"
    headlines.insert(0, {"id": "2", "ticker": "PLD", "title": "Prologis buys warehouses", "source": "AP",
                         "at": (NOW + timedelta(minutes=5)).isoformat()})
    later = NOW + timedelta(minutes=6)
    events = watcher.check(state, later, watcher.holdings(MEMORY), quotes(table), lambda t: 74000.0,
                           lambda t: headlines)
    assert "NEWS: PLD: Prologis buys warehouses (AP)" in events
    assert any(e.startswith("MOVE: BTC-USD +5.7%") for e in events), "crypto: 5% from its check-point"
    assert not any("SPY" in e for e in events), "market move told once per direction"


def test_stocks_are_ignored_outside_their_hours_but_crypto_is_not():
    sunday = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    state = {"crypto_base": {"BTC-USD": 70000.0}}

    def boom(t):
        raise AssertionError("no stock quotes on a Sunday night")

    events = watcher.check(state, sunday, watcher.holdings(MEMORY), boom, lambda t: 65000.0, None)
    assert events == ["MOVE: BTC-USD -7.1% since the last check-point, now 65,000 (bought at 70,000)",
                      "DROP: BTC-USD is -7.1% below its entry 70,000 (now 65,000): check its stop"]


def test_fire_refuses_a_wrong_url_and_sends_the_documented_headers(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"type": "routine_fire"}'

    def urlopen(req, timeout):
        seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data))
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert watcher.fire("hello")["type"] == "routine_fire"
    assert seen["headers"]["Authorization"] == "Bearer tok" and seen["headers"]["Anthropic-beta"] == watcher.BETA
    assert seen["body"] == {"text": "hello"}
    monkeypatch.setenv(watcher.URL_VAR, "https://example.com/steal")
    with pytest.raises(RuntimeError, match="does not look like"):
        watcher.fire("hello")


def test_finnhub_parses_quotes_and_news_and_needs_a_key(monkeypatch):
    monkeypatch.delenv(finnhub.KEY_VAR, raising=False)
    with pytest.raises(RuntimeError):
        finnhub.quote("SPY", fetch=lambda u, k: "{}")
    monkeypatch.setenv(finnhub.KEY_VAR, "k")
    got = []

    def fetch(url, token):
        got.append((url, token))
        if "/quote" in url:
            return json.dumps({"c": 751.2, "dp": -0.55, "pc": 755.4, "h": 756, "l": 749, "t": 1793722000})
        return json.dumps([{"id": 9, "headline": "SPY flows", "source": "X", "datetime": 1793722000},
                           {"headline": ""}])

    q = finnhub.quote("spy", fetch=lambda u, k: fetch(u, k))
    assert q["price"] == 751.2 and q["day_change"] == -0.0055 and got[0][1] == "k" and "k" not in got[0][0]
    news = finnhub.company_news("SPY", fetch=lambda u, k: fetch(u, k))
    assert len(news) == 1 and news[0]["id"] == "9"


def test_the_test_wake_up_reports_a_bad_token_plainly(monkeypatch, capsys):
    import urllib.error

    def refuse(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(watcher, "_post", refuse)
    assert watcher.test() == 1
    assert "start.bat keys" in capsys.readouterr().out
    monkeypatch.setattr(watcher, "_post", lambda u, t, b: {"claude_code_session_url": "https://claude.ai/code/s"})
    assert watcher.test() == 0 and "claude.ai/code/s" in capsys.readouterr().out


def test_funds_never_wake_the_trader_for_news():
    state = {"news_seen": []}
    memory = {"open": [{"ticker": "SPY", "dollars_in": 59.0, "entry": 758.0}]}
    asked = []

    def news(t):
        asked.append(t)
        return [{"id": "x", "ticker": t, "title": "Stocks slip", "source": "X", "at": NOW.isoformat()}]

    events = watcher.check(state, NOW, watcher.holdings(memory), quotes({"SPY": (760.0, 0.002)}), lambda t: None, news)
    assert asked == [] and events == []
