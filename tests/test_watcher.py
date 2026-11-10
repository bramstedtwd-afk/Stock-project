"""The trader watcher and Finnhub: events, rationing, the fire call. Offline."""

from __future__ import annotations

import json
import re
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
    headlines.insert(0, {"id": "3", "ticker": "PLD", "title": "Dow skyrockets 1,500 points after the election",
                         "source": "Yahoo", "at": (NOW + timedelta(minutes=5)).isoformat()})
    events = watcher.check(state, later, watcher.holdings(MEMORY), quotes(table), lambda t: 74000.0,
                           lambda t: headlines, names=lambda t: "Prologis, Inc.")
    assert "NEWS: PLD: Prologis buys warehouses (AP)" in events
    assert not any("Dow skyrockets" in e for e in events), "a market story tagged PLD is not PLD's news"
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


def test_wakes_and_heartbeats_are_recorded_for_the_dashboard(monkeypatch):
    from stocksage import journal as jr

    lines = []
    monkeypatch.setattr(jr, "append_note", lambda line, upload=True: lines.append(line) or line)
    state = {"fires_day": "2026-11-03", "fires_today": 2}
    watcher.heartbeat(state, NOW, MEMORY, force=True)
    watcher.heartbeat(state, NOW + timedelta(minutes=30), MEMORY)
    watcher.heartbeat(state, NOW + timedelta(hours=2, minutes=1), MEMORY)
    assert lines[0] == "WATCH | 2026-11-03T16:05Z | started, watching PLD, BTC-USD | 2 wake-up(s) today"
    assert len(lines) == 2, "one heartbeat every two hours"
    assert jr.append_note.__call__  # patched
    watcher.note("WAKE | 2026-11-03T16:05Z | MOVE: PLD -3.4% today")
    assert lines[-1].startswith("WAKE |")


def test_wake_and_watch_lines_are_valid_notes():
    from stocksage import journal as jr

    assert jr.append_note("WAKE | 2026-11-03T16:05Z | MOVE: PLD -3.4% today", upload=False)
    assert jr.append_note("WATCH | 2026-11-03T16:05Z | watching PLD | 0 wake-up(s) today", upload=False)
    with pytest.raises(ValueError):
        jr.append_note("WAKE | not a time | x", upload=False)


def test_a_touched_stop_wakes_the_trader_at_once_but_only_when_it_can_sell():
    memory = {"open": [{"ticker": "MSFT", "dollars_in": 7.0, "entry": 549.4, "opened": "2026-11-03T14:37+00:00",
                        "stop": 540.0, "target": 600.0}], "trader": {"equity": 100.0}}
    sent = []
    state = {"last_fire": (NOW - timedelta(minutes=5)).isoformat(), "fires_day": "2026-11-03",
             "fires_today": watcher.DAILY_CAP}                     # cooldown AND cap: a stop goes through both
    args = dict(quote=quotes({"MSFT": (538.9, -0.019), "SPY": (750.0, -0.004)}), crypto_quote=lambda t: None,
                news=None, memory=memory, post=lambda u, t, b: sent.append(b), push=lambda *a: None)
    text = watcher.tick(state, NOW, **args)
    assert text and "STOP: MSFT at 538.90 has touched its stop 540.00" in text
    assert watcher.tick(state, NOW + timedelta(minutes=2), **args) is None, "once per stop level a day"
    early = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)      # 8:00 AM ET: a fractional share cannot sell
    events = watcher.check({}, early, watcher.holdings(memory), args["quote"], lambda t: None, None)
    assert not any(e.startswith("STOP") for e in events)
    up = watcher.check({}, NOW, watcher.holdings(memory), quotes({"MSFT": (601.0, 0.02), "SPY": (750.0, 0.0)}),
                       lambda t: None, None)
    assert any(e.startswith("TARGET: MSFT") for e in up)


def test_a_position_bought_today_moves_from_its_buy_price_not_yesterdays_close():
    memory = {"open": [{"ticker": "GOOGL", "dollars_in": 7.0, "entry": 293.34, "opened": "2026-11-03T15:37+00:00"}]}
    rally = quotes({"GOOGL": (292.9, 0.046), "SPY": (750.0, 0.016)})
    assert not any(e.startswith("MOVE") for e in watcher.check({}, NOW, watcher.holdings(memory), rally,
                                                               lambda t: None, None)), "the rally it was bought into"
    fall = quotes({"GOOGL": (283.0, 0.01), "SPY": (750.0, 0.0)})
    assert any("MOVE: GOOGL -3.5% since it was bought today" in e
               for e in watcher.check({}, NOW, watcher.holdings(memory), fall, lambda t: None, None))


def test_headlines_must_name_the_company():
    words = watcher.name_words("GOOGL", "Alphabet Inc")
    assert watcher.about_company("Google is telling schools to take its AI", words)
    assert watcher.about_company("Alphabet stock rises 1.4%", words)
    assert not watcher.about_company("Three reasons stocks are moving higher after the midterms", words)
    ms = watcher.name_words("MSFT", "Microsoft Corp")
    assert watcher.about_company("Tech split after the sweep: Microsoft jumps", ms)
    assert not watcher.about_company("Dow skyrockets 1,500 points in best day since the tariff pause", ms)
    assert not watcher.about_company("msft lowercase is not the ticker", ["MSFT"])


def test_after_hours_events_wait_when_nothing_held_can_trade():
    evening = datetime(2026, 11, 3, 23, 58, tzinfo=timezone.utc)      # 6:58 PM ET, clear of the runs
    frac = {"open": [{"ticker": "MSFT", "dollars_in": 7.0, "entry": 549.4, "opened": "2026-11-02T14:37+00:00"}]}
    sent = []
    args = dict(quote=quotes({"MSFT": (530.0, -0.035), "SPY": (750.0, 0.0)}), crypto_quote=lambda t: None, news=None,
                post=lambda u, t, b: sent.append(b), push=lambda *a: None)
    assert watcher.tick({}, evening, memory=frac, **args) is None and not sent
    whole = {"open": [{"ticker": "MSFT", "dollars_in": 1100.0, "entry": 549.4, "opened": "2026-11-02T14:37+00:00"}]}
    assert watcher.tick({}, evening, memory=whole, **args), "two whole shares can be sold after hours"


def test_yesterdays_quote_before_the_open_wakes_nobody():
    seven_am = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    yesterday = "2026-11-04T21:00:00+00:00"
    stale = lambda t: {"symbol": t, "price": 555.8, "day_change": 0.067 if t != "SPY" else 0.017,  # noqa: E731
                       "at": yesterday}
    memory = {"open": [{"ticker": "MSFT", "dollars_in": 7.0, "entry": 549.4, "opened": "2026-11-04T14:37+00:00"}]}
    assert watcher.check({}, seven_am, watcher.holdings(memory), stale, lambda t: None, None) == []
    live = lambda t: {"symbol": t, "price": 530.0, "day_change": -0.035, "at": seven_am.isoformat()}  # noqa: E731
    assert any(e.startswith("MOVE: MSFT") for e in watcher.check({}, seven_am, watcher.holdings(memory), live,
                                                                  lambda t: None, None))


# --- 2026-11-09 audit: headlines, after-hours per position, and the watcher's own failures ---

def test_only_an_event_headline_is_worth_a_run_of_its_own():
    for title in ("Zacks Analyst Blog Highlights Microsoft, Alphabet and Nvidia",
                  "Oracle vs. Alphabet: Which Is the Better AI Cloud Stock to Buy Now?",
                  "Alphabet (GOOGL) Sees $35.0 Million in Net ETF Selling",
                  "TeraWulf Rises 4% After Q3 Beat and Google-Backed Anthropic Lease",
                  "3 Stocks to Buy Before Microsoft Reports Earnings",
                  "Is Microsoft Stock a Buy After Earnings?",
                  "Vanguard Group Inc. Buys Shares of 1,200 Prologis",
                  "Wellington Management raises stake in Microsoft",
                  "Microsoft: A Deep Dive Into the Cloud Business"):
        assert not watcher.material(title), title
    for title in ("Prologis raises guidance", "Microsoft beats on revenue, shares jump",
                  "Alphabet hit with $2 billion antitrust fine in EU", "FDA approves Lilly's new obesity pill",
                  "Microsoft CEO Nadella steps down", "Prologis downgraded to neutral at Goldman",
                  "Google to acquire Wiz for $32 billion", "Microsoft shares plunge after Azure outage"):
        assert watcher.material(title), title


def _headline(title):
    def news(t):
        return [{"id": title, "ticker": t, "title": title, "source": "X", "at": NOW.isoformat()}]
    return news


def test_headlines_alone_get_three_wake_ups_a_day_and_never_block_a_price_wake_up():
    memory = {"open": [{"ticker": "PLD", "dollars_in": 25.0, "entry": 125.0, "opened": "2026-11-02T14:37+00:00"}]}
    flat, sent = quotes({"PLD": (125.0, 0.0), "SPY": (750.0, 0.0)}), []
    args = dict(crypto_quote=lambda t: None, memory=memory, post=lambda u, t, b: sent.append(b), push=lambda *a: None)
    state = {"news_seen": [], "news_fires_day": "2026-11-03", "news_fires": watcher.NEWS_CAP - 1}
    assert "NEWS: PLD: Prologis raises guidance" in watcher.tick(state, NOW, quote=flat,
                                                                 news=_headline("Prologis raises guidance"), **args)
    assert state["news_fires"] == watcher.NEWS_CAP and state["fires_today"] == 1
    later = NOW + timedelta(hours=1)                                # 12:05 ET: past the cooldown, clear of the run
    assert watcher.tick(state, later, quote=flat, news=_headline("Prologis cuts its dividend"), **args) is None
    assert state["pending"] == [] and len(sent) == 1, "the fourth headline waits for the hourly run"
    down = quotes({"PLD": (120.0, -0.04), "SPY": (750.0, 0.0)})
    text = watcher.tick(state, later + timedelta(minutes=1), quote=down, news=no_news, **args)
    assert text and "MOVE: PLD" in text and state["fires_today"] == 2
    assert state["news_fires"] == watcher.NEWS_CAP, "a price wake-up is not counted as a headline"


def test_a_noise_headline_wakes_nobody():
    memory = {"open": [{"ticker": "PLD", "dollars_in": 25.0, "entry": 125.0, "opened": "2026-11-02T14:37+00:00"}]}
    events = watcher.check({"news_seen": []}, NOW, watcher.holdings(memory), quotes({"PLD": (125.0, 0.0),
                           "SPY": (750.0, 0.0)}), lambda t: None, _headline("Is Prologis a Buy After Earnings?"))
    assert events == []


def test_after_hours_each_position_is_judged_on_whether_it_can_be_sold():
    evening = datetime(2026, 11, 3, 23, 58, tzinfo=timezone.utc)      # 6:58 PM ET, clear of the runs
    mixed = {"open": [{"ticker": "MSFT", "dollars_in": 7.0, "entry": 549.4, "opened": "2026-11-02T14:37+00:00"},
                      {"ticker": "NVDA", "dollars_in": 1100.0, "entry": 200.0, "opened": "2026-11-02T14:37+00:00"}]}
    sent = []
    text = watcher.tick({}, evening, memory=mixed, crypto_quote=lambda t: None, news=None,
                        quote=quotes({"MSFT": (530.0, -0.035), "NVDA": (193.0, -0.035), "SPY": (750.0, 0.0)}),
                        post=lambda u, t, b: sent.append(b), push=lambda *a: None)
    assert text and "MOVE: NVDA" in text, "five whole shares can be sold after hours"
    assert "MSFT" not in text, "a fraction of a share cannot: one whole-share holding used to let it through"
    assert watcher._tradable_now(watcher.holdings(MEMORY)) == {"BTC-USD"}


def _pushes(monkeypatch):
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "t")
    got = []
    return got, lambda u, d, h: got.append((h["Title"], d.decode()))


def test_ten_minutes_without_a_price_reaches_the_phone_once_a_day(monkeypatch):
    got, push = _pushes(monkeypatch)
    memory = {"open": [{"ticker": "PLD", "dollars_in": 25.0, "entry": 125.0, "opened": "2026-11-02T14:37+00:00"}]}
    state: dict = {}

    def dead(t):
        raise OSError("401")

    args = dict(crypto_quote=lambda t: None, news=None, memory=memory, post=lambda u, t, b: None, push=push)
    for minute in range(watcher.QUOTE_FAIL_TICKS - 1):
        watcher.tick(state, NOW + timedelta(minutes=minute), quote=dead, **args)
    assert got == [], "a few bad minutes are not an alarm"
    for minute in range(watcher.QUOTE_FAIL_TICKS - 1, watcher.QUOTE_FAIL_TICKS + 15):
        watcher.tick(state, NOW + timedelta(minutes=minute), quote=dead, **args)
    assert [title for title, _ in got] == ["StockSage: the watcher cannot get prices"]
    assert not re.search(r"\$|\d{4,}", got[0][1]), "no amounts in a push"
    watcher.tick(state, NOW + timedelta(minutes=40), quote=quotes({"PLD": (125.0, 0.0), "SPY": (750.0, 0.0)}), **args)
    assert state["quote_fail"] == 0, "one answered price clears the count"
    none = lambda t: None                                             # noqa: E731
    evening = datetime(2026, 11, 3, 23, 58, tzinfo=timezone.utc)
    quiet: dict = {}
    for minute in range(30):
        watcher.tick(quiet, evening + timedelta(minutes=minute), quote=none, **args)
    assert not quiet.get("quote_fail"), "thin after-hours quotes are not an outage"


STOPPED = {"open": [{"ticker": "MSFT", "dollars_in": 7.0, "entry": 549.4, "opened": "2026-11-02T14:37+00:00",
                     "stop": 540.0}], "trader": {"equity": 100.0, "last_run": "2026-11-03T15:35+00:00"}}
AT_STOP = {"MSFT": (538.9, -0.019), "SPY": (750.0, -0.004)}


def test_a_stop_the_trader_never_answered_reaches_the_phone(monkeypatch):
    got, push = _pushes(monkeypatch)
    state: dict = {}
    args = dict(quote=quotes(AT_STOP), crypto_quote=lambda t: None, news=None, memory=STOPPED,
                post=lambda u, t, b: None, push=push)
    assert "STOP: MSFT" in watcher.tick(state, NOW, **args)
    assert state["urgent"]["at"] == NOW.isoformat()
    watcher.tick(state, NOW + timedelta(minutes=10), **args)
    assert [t for t, _ in got] == ["StockSage: a stop was touched"], "ten minutes is still time to answer"
    watcher.tick(state, NOW + watcher.ANSWER_WITHIN + timedelta(minutes=1), **args)
    assert [t for t, _ in got][-1] == "StockSage: the trader did not answer"
    assert "look at that account yourself" in got[-1][1] and not re.search(r"\$|\d{4,}", got[-1][1])
    watcher.tick(state, NOW + timedelta(minutes=40), **args)
    assert len(got) == 2 and "urgent" not in state, "said once"


def test_a_stop_the_trader_answered_says_nothing_more(monkeypatch):
    got, push = _pushes(monkeypatch)
    state: dict = {}
    args = dict(quote=quotes(AT_STOP), crypto_quote=lambda t: None, news=None, post=lambda u, t, b: None, push=push)
    assert watcher.tick(state, NOW, memory=STOPPED, **args)
    ran = dict(STOPPED, trader={"equity": 100.0, "last_run": (NOW + timedelta(minutes=4)).isoformat()})
    watcher.tick(state, NOW + timedelta(minutes=6), memory=ran, **args)
    assert "urgent" not in state
    watcher.tick(state, NOW + timedelta(minutes=45), memory=ran, **args)
    assert [t for t, _ in got] == ["StockSage: a stop was touched"]


def test_a_stop_whose_wake_up_could_not_be_sent_still_reaches_the_phone(monkeypatch):
    got, push = _pushes(monkeypatch)
    state: dict = {}

    def refused(url, token, body):
        raise OSError("429")

    args = dict(quote=quotes(AT_STOP), crypto_quote=lambda t: None, news=None, memory=STOPPED, post=refused,
                push=push)
    assert watcher.tick(state, NOW, **args) is None
    watcher.tick(state, NOW + watcher.ANSWER_WITHIN + timedelta(minutes=1), **args)
    assert "StockSage: the trader did not answer" in [t for t, _ in got]


def test_a_heartbeat_that_did_not_reach_drive_is_tried_again_in_ten_minutes(monkeypatch):
    from stocksage import journal as jr

    lines, drive = [], {"up": False}

    def append(line, upload=True):
        if not drive["up"]:
            raise OSError("drive is down")
        lines.append(line)
        return line

    monkeypatch.setattr(jr, "append_note", append)
    state: dict = {}
    watcher.heartbeat(state, NOW, MEMORY, force=True)
    watcher.heartbeat(state, NOW + timedelta(minutes=5), MEMORY)
    assert lines == []
    drive["up"] = True
    watcher.heartbeat(state, NOW + timedelta(minutes=11), MEMORY)
    assert len(lines) == 1, "the watchdog reads these lines to know the watcher is alive"
    watcher.heartbeat(state, NOW + timedelta(minutes=30), MEMORY)
    assert len(lines) == 1


def test_status_does_not_show_yesterdays_wake_ups_as_todays(capsys):
    watcher.save_state({"fires_day": "2020-01-01", "fires_today": 7, "news_fires_day": "2020-01-01",
                        "news_fires": 3, "quote_fail": watcher.QUOTE_FAIL_TICKS})
    watcher.status()
    out = capsys.readouterr().out
    assert f"wake-ups today: 0 of {watcher.DAILY_CAP} (0 of {watcher.NEWS_CAP} for headlines alone)" in out
    assert "PRICES: none for" in out and ".\\start.bat keys" in out


# --- A stop in the minutes after a scheduled run (2026-11-09) ---------------------------

AFTER_RUN = datetime(2026, 11, 3, 16, 40, tzinfo=timezone.utc)      # five minutes after the :35 run


def _stop_args(push):
    return dict(quote=quotes(AT_STOP), crypto_quote=lambda t: None, news=None, push=push)


def test_a_stop_right_after_a_scheduled_run_waits_only_until_that_run_has_finished(monkeypatch):
    got, push = _pushes(monkeypatch)
    sent: list = []
    state: dict = {}
    args = dict(_stop_args(push), post=lambda u, t, b: sent.append(b))
    watcher.tick(state, AFTER_RUN, memory=STOPPED, **args)           # last RUN line is 15:35: still running
    assert not sent and any("STOP: MSFT" in e for e in state["pending"]), "held while the run may be going"
    done = dict(STOPPED, trader={"equity": 100.0, "last_run": "2026-11-03T16:37+00:00"})
    assert "STOP: MSFT" in watcher.tick(state, AFTER_RUN + timedelta(minutes=1), memory=done, **args)
    assert len(sent) == 1 and not state.get("pending"), "fired at :41, not at :50"


def test_a_finished_run_does_not_open_the_quiet_window_for_ordinary_events():
    done = {"trader": {"last_run": "2026-11-03T16:36+00:00"}}
    assert watcher.scheduled_run_done(AFTER_RUN, done)
    assert not watcher.scheduled_run_done(AFTER_RUN, {"trader": {"last_run": "2026-11-03T15:36+00:00"}})
    assert not watcher.scheduled_run_done(AFTER_RUN, {}), "no RUN line known: wait as before"
    before = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)
    assert not watcher.scheduled_run_done(before, done), "the minutes BEFORE a run stay quiet"
    assert not watcher.scheduled_run_done(datetime(2026, 11, 3, 16, 55, tzinfo=timezone.utc), done)
    saturday = datetime(2026, 11, 7, 16, 40, tzinfo=timezone.utc)
    assert not watcher.scheduled_run_done(saturday, {"trader": {"last_run": "2026-11-07T16:36+00:00"}})
    state: dict = {}
    ok, why = watcher.may_fire(state, AFTER_RUN, urgent=False)
    assert not ok, "a plain move still waits out the quiet window"


def test_a_held_stop_is_dropped_once_the_run_sold_the_position(monkeypatch):
    got, push = _pushes(monkeypatch)
    sent: list = []
    state: dict = {}
    args = dict(_stop_args(push), post=lambda u, t, b: sent.append(b))
    watcher.tick(state, AFTER_RUN, memory=STOPPED, **args)
    assert state["pending"] and state.get("urgent")
    sold = {"open": [], "trader": {"equity": 100.0, "last_run": "2026-11-03T16:37+00:00"}}
    watcher.tick(state, AFTER_RUN + timedelta(minutes=1), memory=sold, **args)
    assert not sent and not state.get("pending") and "urgent" not in state
    watcher.tick(state, AFTER_RUN + timedelta(minutes=45), memory=sold, **args)
    assert "StockSage: the trader did not answer" not in [t for t, _ in got]
