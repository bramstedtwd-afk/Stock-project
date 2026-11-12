"""The watcher's wider view: ENTRY wake-ups for good ideas that were not bought,
EVENT reviews before earnings and major releases, and the phone page's live
overview. Offline: prices, calendars and the trade memory are all handed in."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from stocksage import journal, mobile, watcher

# Tue 2026-11-03 16:05 UTC = 11:05 AM ET: market open, clear of the :35 runs.
NOW = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)
AFTERNOON = datetime(2026, 11, 3, 20, 5, tzinfo=timezone.utc)      # 3:05 PM ET, the review hour


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")
    monkeypatch.setenv(mobile.KEY_VAR, "k" * 32)
    return tmp_path


def quotes(table):
    def q(t):
        price, change = table[t]
        return {"symbol": t, "price": price, "day_change": change}
    return q


def idea(ticker="NVDA", entry=141.0, direction="above", level="HIGH", stop=137.5, expires="2026-11-04", **kw):
    return dict({"ticker": ticker, "entry": entry, "direction": direction, "price": 139.0, "level": level,
                 "setup": "breakout", "at": "2026-11-03T15:40+00:00", "expires": expires, "stop": stop}, **kw)


def memory(ideas=(), cash=40.0, held=(("PLD", 125.0),), **extra):
    return dict({"open": [{"ticker": t, "dollars_in": 25.0, "entry": e} for t, e in held],
                 "trader": {"cash": cash, "equity": 100.0}, "idea_watch": list(ideas)}, **extra)


def run(state, now, table, mem, sent=None, events=None):
    def post(url, token, body):
        if sent is not None:
            sent.append(body)
        return {}

    return watcher.tick(state, now, quote=quotes(table), crypto_quote=lambda t: table.get(t, (None,))[0], news=None,
                        memory=mem, post=post, events=events)


CALM = {"PLD": (125.2, 0.001), "SPY": (750.0, 0.001)}


# --- which ideas are watched (journal) ------------------------------------------------

TAGS = "\n".join([
    "IDEA | 2026-11-03T15:40Z | BUY | NVDA | breakout | 139.00 | [HIGH] entry 141 stop 137.50 target 150, base on volume",
    "IDEA | 2026-11-03T15:40Z | BUY | COST | pullback | 912.00 | [MEDIUM] entry below 905, stop 890, back to the 21-day",
    "IDEA | 2026-11-03T15:40Z | BUY | LOWQ | breakout | 20.00 | [LOW] entry 21, thin",
    "IDEA | 2026-11-03T15:40Z | BUY | OLDF | momentum | 50.00 | [HIGH] strong, no level named",
    "IDEA | 2026-11-03T15:40Z | SELL | TSLA | reversal | 300.00 | [HIGH] entry 295",
    "IDEA | 2026-10-29T15:40Z | BUY | STALE | breakout | 10.00 | [HIGH] entry 11",
    "IDEA | 2026-11-03T15:40Z | BUY | HELD | breakout | 10.00 | [HIGH] entry 11",
    "IDEA | 2026-11-03T15:40Z | BUY | GOT | breakout | 10.00 | [HIGH] entry 11",
    "TAG | 2026-11-03T15:50Z | BUY | GOT | SAFE | breakout | 1 | 10.5 | [HIGH] stop 9.5",
    "IDEA | 2026-11-03T14:00Z | BUY | FLIP | breakout | 10.00 | [HIGH] entry 11",
    "IDEA | 2026-11-03T15:00Z | BUY | FLIP | breakout | 10.20 | [LOW] entry 11, faded",
])


def test_only_fresh_high_and_medium_buy_ideas_with_an_entry_level_are_watched():
    got = {i["ticker"]: i for i in journal.idea_watch(TAGS, open_tickers=["HELD"], now=NOW)}
    assert set(got) == {"NVDA", "COST"}
    assert got["NVDA"]["entry"] == 141.0 and got["NVDA"]["direction"] == "above" and got["NVDA"]["stop"] == 137.5
    assert got["COST"]["entry"] == 905.0 and got["COST"]["direction"] == "below" and got["COST"]["level"] == "MEDIUM"
    assert got["NVDA"]["expires"] == "2026-11-04", "the day it was written and one trading day more"


def test_an_idea_expires_after_the_next_trading_day_and_a_friday_idea_lasts_through_monday():
    friday = "IDEA | 2026-11-06T15:40Z | BUY | NVDA | breakout | 139 | [HIGH] entry 141"
    monday, tuesday = datetime(2026, 11, 9, 15, tzinfo=timezone.utc), datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
    assert journal.idea_watch(friday, now=monday) and not journal.idea_watch(friday, now=tuesday)


def test_old_idea_lines_still_parse_and_grade_as_before():
    old = "IDEA | 2026-11-03T15:40Z | BUY | OLDF | momentum | 50.00 | [HIGH] strong, no level named"
    assert journal.parse_ideas(old)[0]["confidence"] == "HIGH" and journal.idea_watch(old, now=NOW) == []
    assert journal.stated_entry("entry at 12.5, stop 11") == 12.5 and journal.stated_entry("re-entry later") is None


def test_the_memory_carries_the_watch_list_on_the_light_path_too():
    summary = {"open": [{"ticker": "HELD"}]}
    journal._with_ideas(summary, None, TAGS)
    assert isinstance(summary["idea_watch"], list)        # dated lines: none fresh today, but the key is there


# --- ENTRY wake-ups -------------------------------------------------------------------

def test_a_breakout_idea_reaching_its_entry_wakes_the_trader_once():
    state, sent = {}, []
    mem = memory([idea()])
    assert run(state, NOW, dict(CALM, NVDA=(140.2, 0.01)), mem, sent) is None, "below the entry: nothing"
    text = run(state, NOW + timedelta(minutes=1), dict(CALM, NVDA=(141.3, 0.02)), mem, sent)
    assert "ENTRY: NVDA at 141.30 has reached the entry 141.00 of your [HIGH] breakout idea" in text
    assert "its stop was 137.50" in text and "buy only if it still qualifies" in text
    assert state["followups"][0]["prices"] == {"NVDA": 141.3}, "price-checked an hour later like any wake-up"
    later = NOW + timedelta(minutes=30)
    assert run(state, later, dict(CALM, NVDA=(141.5, 0.02)), mem, sent) is None, "once per entry level"
    assert len(sent) == 1


def test_a_breakout_that_has_run_away_is_not_chased_but_fires_if_it_comes_back():
    state = {}
    mem = memory([idea()])
    assert run(state, NOW, dict(CALM, NVDA=(146.0, 0.05)), mem) is None, "3.5% past the entry: no chasing"
    assert "ENTRY: NVDA" in run(state, NOW + timedelta(minutes=1), dict(CALM, NVDA=(142.0, 0.02)), mem)


def test_a_pullback_idea_fires_at_its_entry_but_not_through_its_stop():
    mem = memory([idea("COST", entry=905.0, direction="below", level="MEDIUM", stop=890.0, setup="pullback")])
    assert run({}, NOW, dict(CALM, COST=(909.0, -0.003)), mem) is None
    assert run({}, NOW, dict(CALM, COST=(889.0, -0.02)), mem) is None, "through the idea's own stop: broken"
    assert "ENTRY: COST at 904.00" in run({}, NOW, dict(CALM, COST=(904.0, -0.01)), mem)


@pytest.mark.parametrize("mem, why", [
    (memory([idea()], cash=3.0), "no settled cash is free"),
    (memory([idea()], cash=None), "cash unknown"),
    (memory([idea(level="LOW")]), "LOW is never watched, whatever the file says"),
    (memory([idea(expires="2026-11-02")]), "expired"),
    (memory([idea()], held=(("PLD", 125.0), ("NVDA", 140.0))), "already held"),
    (memory([idea()], discipline={"day": "2026-11-03", "limit_hit": True}), "daily limit: no new buys"),
    (memory([idea()], discipline={"day": "2026-11-03", "stopped_today": ["NVDA"]}), "no revenge trade"),
])
def test_no_entry_wake_up_when_a_buy_is_not_possible_or_not_wanted(mem, why):
    table = dict(CALM, NVDA=(141.3, 0.004))
    assert run({}, NOW, table, mem) is None, why


def test_ideas_cannot_use_up_the_day_and_never_more_than_six_are_watched():
    state, sent = {}, []
    names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
    mem = memory([idea(n, entry=10.0, stop=None) for n in names])
    assert len(watcher.watched_ideas(mem, NOW, [])) == watcher.IDEAS_MAX == 6
    table = dict(CALM, **{n: (9.0, 0.0) for n in names})
    at = NOW
    for n in names[:4]:                                  # one idea at a time reaches its entry, hours apart
        table[n] = (10.05, 0.0)
        run(state, at, table, mem, sent)
        at += timedelta(minutes=60)
    assert len(sent) == watcher.ENTRY_CAP == 2, "at most two wake-ups a day for entries alone"
    assert state["fires_today"] == 2 and watcher.DAILY_CAP == 10 and watcher.COOLDOWN == timedelta(minutes=20)


def test_an_entry_obeys_the_cooldown_and_is_asked_again_after_a_scheduled_run():
    mem = memory([idea()])
    table = dict(CALM, NVDA=(141.3, 0.004))
    cooling = {"last_fire": (NOW - timedelta(minutes=5)).isoformat()}
    assert run(cooling, NOW, table, mem) is None and cooling["pending"], "waits like any non-urgent event"
    state = {}
    quiet = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)               # 5 minutes before the :35 run
    assert run(state, quiet, table, mem) is None and state["entered"] == [], "dropped, and not marked as said"
    assert "ENTRY: NVDA" in run(state, quiet + timedelta(minutes=25), table, mem), "still there after the run"


def test_a_stock_idea_waits_for_the_regular_session_and_a_crypto_idea_does_not():
    evening = datetime(2026, 11, 3, 23, 5, tzinfo=timezone.utc)              # 6:05 PM ET
    assert run({}, evening, dict(CALM, NVDA=(141.3, 0.004)), memory([idea()])) is None
    mem = memory([idea("ETH-USD", entry=3000.0, stop=2900.0, setup="trend", expires="2026-11-09")], held=())
    text = run({}, datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc), {"ETH-USD": (3010.0, 0.0)}, mem)   # Friday night
    assert text and "ENTRY: ETH-USD at 3,010" in text


def test_the_scorecard_grades_entry_and_event_wake_ups_and_never_tunes_them():
    notes = "\n".join([
        "WAKE | 2026-11-03T16:06Z | ENTRY: NVDA at 141.30 has reached the entry 141.00 of your [HIGH] breakout idea; "
        "EVENT: market: CPI m/m is due Thu 08:30 AM ET",
        "TAG | 2026-11-03T16:12Z | BUY | NVDA | SAFE | breakout | 0.1 | 141.4 | [HIGH] stop 137.5",
        "RUN | 2026-11-03T16:15Z | 100 | 26 | v38: wake-up: bought NVDA at its entry",
        "AFTER | 2026-11-03T16:06Z | 61 | NVDA 141.3 143.1; SPY 750 751"])
    card = journal.wake_scorecard(notes, now=NOW + timedelta(hours=2))
    assert card["by_kind"]["ENTRY"]["traded"] == 1 and card["by_kind"]["ENTRY"]["continued"] == 1
    assert card["by_kind"]["EVENT"]["wakes"] == 1 and card["by_kind"]["EVENT"]["answered"] == 1
    assert set(card["tuning"]) == {"MOVE", "NEAR", "NEWS_CAP"} and journal.NEVER_TUNED == ("STOP", "LIMIT")


# --- EVENT reviews --------------------------------------------------------------------

EVENTS = {"earnings": {"2026-11-03": {"PLD": {"when": "after the close", "eps": "$1.42"},
                                      "AMD": {"when": "before the open", "eps": None}},
                       "2026-11-04": {"MSFT": {"when": "before the open", "eps": "$3.10"},
                                      "ORCL": {"when": "after the close", "eps": None}}},
          "releases": [{"at": "2026-11-04T13:30:00+00:00", "title": "CPI m/m", "forecast": "0.3%", "previous": "0.2%"},
                       {"at": "2026-11-04T13:30:00+00:00", "title": "Core CPI m/m", "forecast": "0.3%", "previous": "0.3%"},
                       {"at": "2026-11-03T19:00:00+00:00", "title": "Federal Funds Rate", "forecast": "4.00%",
                        "previous": "4.25%"}]}
HELD4 = (("PLD", 125.0), ("AMD", 200.0), ("MSFT", 500.0), ("ORCL", 250.0))
TABLE4 = dict(CALM, AMD=(200.0, 0.001), MSFT=(500.0, 0.001), ORCL=(250.0, 0.001))


def test_earnings_reviews_come_in_the_last_hour_once_per_report():
    state, sent = {}, []
    mem = memory(held=HELD4)
    assert run(state, NOW, TABLE4, mem, sent, EVENTS) is None, "11 AM: the scheduled runs have it; not yet"
    text = run(state, AFTERNOON, TABLE4, mem, sent, EVENTS)
    assert "EVENT: PLD reports earnings after the close today (EPS expected $1.42)" in text
    assert "EVENT: MSFT reports earnings before the open on Wed 04 Nov" in text
    assert "AMD reports" not in text, "reported before today's open: already out"
    assert "ORCL reports" not in text, "after tomorrow's close: tomorrow afternoon's question"
    assert "hold through it or sell" in text
    assert run(state, AFTERNOON + timedelta(minutes=50), TABLE4, mem, sent, EVENTS) is None and len(sent) == 1


def test_a_release_before_the_open_is_reviewed_the_afternoon_before():
    text = run({}, AFTERNOON, CALM, memory(), events=EVENTS)
    assert "EVENT: market: CPI m/m, Core CPI m/m is due Wed 08:30 AM ET (forecast 0.3%, previous 0.2%)" in text
    assert watcher._event_ticker("EVENT: market: CPI m/m is due") is None
    assert watcher._event_ticker("EVENT: PLD reports earnings") == "PLD"


def test_a_release_in_the_session_is_reviewed_15_to_60_minutes_ahead():
    events = {"releases": EVENTS["releases"][2:]}                             # the Fed at 2:00 PM ET
    early = datetime(2026, 11, 3, 17, 30, tzinfo=timezone.utc)               # 90 minutes ahead
    state = {}
    assert run(state, early, CALM, memory(), events=events) is None
    text = run(state, datetime(2026, 11, 3, 18, 5, tzinfo=timezone.utc), CALM, memory(), events=events)
    assert "EVENT: market: Federal Funds Rate is due Tue 02:00 PM ET" in text
    assert run({}, datetime(2026, 11, 3, 18, 50, tzinfo=timezone.utc), CALM, memory(), events=events) is None, \
        "10 minutes ahead is too late to review"


def test_no_review_when_nothing_is_held_or_no_calendar_was_read():
    assert run({}, AFTERNOON, CALM, memory(held=()), events=EVENTS) is None
    assert run({}, AFTERNOON, TABLE4, memory(held=HELD4), events=None) is None
    assert run({}, AFTERNOON, TABLE4, memory(held=HELD4), events={}) is None


def test_the_calendars_are_read_through_the_live_wire_and_kept_when_a_read_fails():
    state = {}
    rows = [{"at": "2026-11-04T13:30:00+00:00", "title": "CPI m/m", "forecast": "0.3%", "previous": "0.2%"},
            {"at": "2026-11-04T15:00:00+00:00", "title": "ISM Services PMI"},
            {"at": "2026-11-05T13:30:00+00:00", "title": "Unemployment Claims"}]
    days = []

    def earnings(day):
        days.append(day.isoformat())
        return [{"ticker": "PLD", "when": "after the close", "eps_forecast": "$1.42"}]

    got = watcher.read_events(state, NOW, calendar=lambda: rows, earnings=earnings)
    assert [r["title"] for r in got["releases"]] == ["CPI m/m"], "only the releases that move everything"
    assert days == ["2026-11-03", "2026-11-04"] and got["earnings"]["2026-11-03"]["PLD"]["when"] == "after the close"

    def broken():
        raise OSError("offline")

    again = watcher.read_events(state, NOW + timedelta(hours=3), calendar=broken, earnings=lambda d: broken())
    assert again["releases"] == got["releases"] and again["earnings"] == got["earnings"]


def test_a_broken_idea_list_never_costs_a_stop_its_wake_up():
    mem = memory(held=(("PLD", 125.0),))
    mem["open"][0]["stop"] = 124.0
    mem["idea_watch"] = [{"ticker": None}, "nonsense", {"ticker": "X", "entry": "abc"}]
    text = run({}, NOW, dict(CALM, PLD=(123.5, -0.012)), mem, events={"earnings": "garbled", "releases": 7})
    assert text and "STOP: PLD" in text


# --- the phone page's live overview ---------------------------------------------------

def _watched(tmp_path, now=NOW):
    mem = memory([idea()], held=(("PLD", 125.0),))
    mem["open"][0].update(stop=121.0, target=135.0)
    state = {}
    run(state, now, dict(CALM, PLD=(126.0, 0.012), SPY=(750.0, 0.004), NVDA=(139.6, 0.0)), mem, events=EVENTS)
    state["events"] = EVENTS                     # run_once keeps the calendars it read in the state file
    watcher.save_state(state)
    (tmp_path / "watcher_memory.json").write_text(json.dumps(mem), encoding="utf-8")
    return state


def test_the_live_overview_shows_distances_moves_ideas_and_the_calendar(_state):
    _watched(_state)
    text = mobile.live_text(now=NOW + timedelta(seconds=40))
    assert "the watcher's last check: Tue 11:05 AM ET, under a minute ago" in text
    assert "Market (SPY) +0.4% today" in text
    assert "PLD  126.00  +1.2% today (+0.8% vs the market)" in text
    assert "stop 121.00 is 4.0% below; target 135.00 is 7.1% above" in text
    assert "NVDA  [HIGH] breakout: entry 141.00 is 1.0% above (now 139.60)" in text
    assert "PLD reports earnings after the close today" in text and "CPI m/m" in text
    assert "$" not in text.replace("EPS expected $1.42", ""), "prices and percent only: no dollar amounts"


def test_the_live_overview_says_when_the_watcher_has_stopped_checking(_state):
    _watched(_state)
    assert "THE WATCHER HAS NOT CHECKED FOR 12 MINUTES" in mobile.live_text(now=NOW + timedelta(minutes=12))
    assert ".\\start.bat watch-trader install" in mobile.live_text(now=NOW + timedelta(minutes=12))


def test_no_watcher_here_means_no_live_block_and_the_sheet_still_shows(_state):
    (_state / "today.txt").write_text("STOCKSAGE TODAY", encoding="utf-8")
    assert mobile.live_text() == ""
    page = mobile.render_page()
    assert "STOCKSAGE TODAY" in page and 'id="live"' not in page
    (_state / "watcher_state.json").write_text("{not json", encoding="utf-8")
    assert "STOCKSAGE TODAY" in mobile.render_page()


def test_the_live_path_needs_the_key_is_get_only_and_is_plain_text(_state):
    _watched(_state, now=datetime.now(timezone.utc))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mobile.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, method="GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, method=method), timeout=5) as r:
                return r.status, r.read().decode(), r.headers["Content-Type"]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers["Content-Type"]
        except ConnectionError:               # Windows sometimes drops a refused POST before the 405 is read
            return 405, "", ""

    try:
        status, body, kind = get(f"/live?k={'k' * 32}")
        assert status == 200 and body.startswith("LIVE") and kind.startswith("text/plain")
        assert get("/live")[0] == 403 and get("/live?k=wrong")[0] == 403 and get(f"/livex?k={'k' * 32}")[0] == 403
        assert get(f"/live?k={'k' * 32}", "POST")[0] == 405
        status, page, _ = get(f"/?k={'k' * 32}")
        assert status == 200 and 'id="live"' in page and "textContent" in page and "innerHTML" not in page
    finally:
        srv.shutdown()
        srv.server_close()


def test_who_reports_comes_from_the_keyed_calendar_and_falls_back_to_the_live_wire(monkeypatch):
    from datetime import date

    from stocksage import finnhub, live

    payload = json.dumps({"earningsCalendar": [
        {"symbol": "pld", "date": "2026-11-03", "hour": "amc", "epsEstimate": 1.42},
        {"symbol": "AMD", "date": "2026-11-03", "hour": "bmo", "epsEstimate": None},
        {"symbol": "ODD", "date": "2026-11-03", "hour": ""}, {"date": "2026-11-03"}]})
    monkeypatch.setenv(finnhub.KEY_VAR, "key")
    rows = finnhub.earnings_calendar(date(2026, 11, 3), date(2026, 11, 3), fetch=lambda url, token: payload)
    assert rows[0] == {"ticker": "PLD", "day": "2026-11-03", "when": "after the close", "eps_forecast": "1.42"}
    assert [r["when"] for r in rows[1:]] == ["before the open", "time not given"]

    monkeypatch.setenv(finnhub.KEY_VAR, "key")
    monkeypatch.setattr(finnhub, "earnings_calendar", lambda first, last: rows)
    assert [r["ticker"] for r in watcher._reporting(date(2026, 11, 3))] == ["PLD", "AMD", "ODD"]

    def refused(first, last):
        raise OSError("403")

    monkeypatch.setattr(finnhub, "earnings_calendar", refused)
    monkeypatch.setattr(live, "earnings", lambda day, limit=15: [{"ticker": "WIRE", "when": "after the close"}])
    assert watcher._reporting(date(2026, 11, 3))[0]["ticker"] == "WIRE"


def test_an_event_held_back_by_a_scheduled_run_is_logged_once_not_every_minute(monkeypatch):
    lines = []
    monkeypatch.setattr(watcher, "log", lines.append)
    state, mem = {}, memory([idea()])
    table = dict(CALM, NVDA=(141.3, 0.004))
    quiet = datetime(2026, 11, 3, 16, 26, tzinfo=timezone.utc)
    for minute in range(6):
        assert run(state, quiet + timedelta(minutes=minute), table, mem) is None
    assert sum("held back" in x for x in lines) == 1
    assert "ENTRY: NVDA" in run(state, quiet + timedelta(minutes=30), table, mem) and "held_said" not in state
