"""Discipline: daily loss limit, no revenge trades, cool-off, stated risk and R. Offline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stocksage import confidence as cf
from stocksage import journal as jr
from stocksage import watcher

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)          # Wed 1:00 PM ET
BACKED = {"n": 150, "dates": 60, "edge": 0.006, "lower": 0.002, "hit_rate": 0.58, "backed": True}
BAD_DAY = "\n".join([
    "RUN | 2026-11-03T20:00Z | 100.00 | 40.00 | yesterday's close",
    "TAG | 2026-11-03T15:00Z | BUY | AAA | SAFE | pullback | 1 | 10 | [MEDIUM] stop 9.50, pullback to the 21-day",
    "TAG | 2026-11-04T14:40Z | SELL | AAA | SAFE | stop | 1 | 9.40 | stopped out",
    "TAG | 2026-11-04T15:00Z | BUY | BBB | AGGRESSIVE | momentum | 2 | 20 | [HIGH] stop 19, breaking out on news",
    "TAG | 2026-11-04T16:00Z | SELL | BBB | AGGRESSIVE | stop | 2 | 18.90 | stopped out",
    "TAG | 2026-11-04T16:30Z | BUY | CCC | SAFE | breakout | 1 | 50 | [LOW] breakout, no stop named",
    "TAG | 2026-11-04T16:40Z | BUY | DDD | SAFE | pullback | 10 | 10 | [LOW] stop 8 wide",
    "RUN | 2026-11-04T17:40Z | 95.50 | 1.00 | rough morning",
])


def summary(text=BAD_DAY, now=NOW):
    tags = jr.parse_tags(text)
    return jr.summarize(jr.fills_from_tags(tags), tags, account="555000111", runs=jr.parse_runs(text), now=now)


def test_a_bad_day_hits_the_limit_blocks_revenge_and_cools_off():
    d = summary()["discipline"]
    assert d["day"] == "2026-11-04" and d["day_start_equity"] == 100.0 and d["day_change"] == -0.045
    assert d["limit_hit"] and "DAILY LIMIT HIT" in d["line"]
    assert d["stopped_today"] == ["AAA", "BBB"] and d["losses_in_a_row"] == 2 and d["cool_off"]
    assert d["realized_today"] == -2.8 and d["realized_today_new"] == -2.2, "AAA was bought yesterday"


def test_a_winning_sale_ends_the_cool_off_and_tomorrow_starts_clean():
    won = BAD_DAY + "\nTAG | 2026-11-04T17:50Z | SELL | DDD | SAFE | target | 10 | 10.50 | target hit"
    d = summary(won)["discipline"]
    assert d["losses_in_a_row"] == 0 and not d["cool_off"]
    tomorrow = summary(now=NOW + timedelta(days=1))["discipline"]
    assert not tomorrow["limit_hit"] and tomorrow["stopped_today"] == [] and not tomorrow["cool_off"]
    assert "Clear" in tomorrow["line"]


def test_risk_is_measured_from_the_stop_named_in_the_why():
    assert jr.stated_stop("[MEDIUM] stop 118.40, pullback") == 118.40
    assert jr.stated_stop("stop-loss at 0.95") == 0.95 and jr.stated_stop("stop under 42") == 42.0
    assert jr.stated_stop("no stop named") is None
    s = summary()
    rk = s["risk"]
    assert rk["buys"] == 4 and rk["with_stop"] == 3 and rk["without_stop"] == 1
    assert rk["over_guide"] == ["DDD"], "risking 20 of 100 on a SAFE trade"
    aaa = next(t for t in s["recent"] if t["ticker"] == "AAA")
    assert aaa["risk"] == 0.5 and aaa["r"] == -1.2
    assert any("named no stop" in x for x in s["lessons"]) and any("DDD" in x for x in s["lessons"])
    text = jr.render_text(s)
    assert "DISCIPLINE: DAILY LIMIT HIT" in text and "RISK (30 days): 4 buys, 3 with a stated stop" in text


def test_a_loss_far_past_the_stop_is_called_out_and_losses_within_plan_are_praised():
    blown = "\n".join(["RUN | 2026-11-03T20:00Z | 100 | 50 | x",
                       "TAG | 2026-11-04T14:40Z | BUY | EEE | SAFE | pullback | 1 | 10 | [LOW] stop 9.80",
                       "TAG | 2026-11-04T15:40Z | SELL | EEE | SAFE | thesis | 1 | 9.00 | gapped through"])
    assert any("more than 1.5x the planned risk" in x for x in summary(blown)["lessons"])
    lines = ["RUN | 2026-11-01T20:00Z | 100 | 50 | x"]
    for i, ticker in enumerate("FGH"):
        lines += [f"TAG | 2026-11-0{2 + i}T14:40Z | BUY | {ticker} | SAFE | pullback | 1 | 10 | [LOW] stop 9.50",
                  f"TAG | 2026-11-0{2 + i}T15:40Z | SELL | {ticker} | SAFE | stop | 1 | 9.50 | stopped"]
    assert any("discipline working" in x for x in summary("\n".join(lines))["lessons"])


def test_confidence_applies_the_limit_revenge_and_cool_off_only_on_the_same_day():
    s = summary()
    brief = {"evidence": {"ZZZ": {"buy": BACKED}, "AAA": {"buy": BACKED}}, "market": {"regime": {"label": "up"}}}
    limit = cf.for_trade("ZZZ", "pullback", confirmations=2, brief=brief, memory=s, now=NOW)
    assert limit["level"] == "NO" and any("DAILY LIMIT" in x for x in limit["reasons"])
    s["discipline"]["limit_hit"] = False
    assert cf.for_trade("AAA", "pullback", confirmations=2, brief=brief, memory=s, now=NOW)["level"] == "NO"
    cooled = cf.for_trade("ZZZ", "pullback", confirmations=2, brief=brief, memory=s, now=NOW)
    assert cooled["score"] >= 4 and cooled["level"] == "LOW" and any("COOL-OFF" in x for x in cooled["reasons"])
    tomorrow = cf.for_trade("ZZZ", "pullback", confirmations=2, brief=brief, memory=s, now=NOW + timedelta(days=1))
    assert tomorrow["level"] == "HIGH", "a bad day never turns into a timid week"


def test_selling_is_never_gated_by_discipline():
    s = summary()
    r = cf.score(side="sell", setup="stop", memory=s, ticker="AAA", now=NOW)
    assert not any(k in " ".join(r["reasons"]) for k in ("DAILY LIMIT", "NO REVENGE", "COOL-OFF"))


@pytest.fixture
def watcher_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(watcher.URL_VAR, "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire")
    monkeypatch.setenv(watcher.TOKEN_VAR, "tok")
    monkeypatch.setenv("STOCKSAGE_NTFY_TOPIC", "test-topic")


def test_the_watcher_wakes_the_trader_and_the_phone_on_the_daily_limit(watcher_env):
    now = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)
    memory = {"open": [{"ticker": "PLD", "dollars_in": 50.0, "entry": 125.0, "opened": "2026-11-02T15:00+00:00"}],
              "trader": {"equity": 100.0}}
    sent, pushed = [], []
    state = {"last_fire": (now - timedelta(minutes=5)).isoformat()}       # inside the cooldown
    args = dict(quote=lambda t: {"symbol": t, "price": 114.0, "day_change": 114 / 125 - 1},
                crypto_quote=lambda t: None, news=None, memory=memory,
                post=lambda u, t, b: sent.append(b), push=lambda url, data, headers: pushed.append((url, data)))
    text = watcher.tick(state, now, **args)
    assert text and "LIMIT: the account is about -4.4% today" in text and "no new buys until tomorrow" in text
    assert pushed and pushed[0][0].endswith("/test-topic") and b"daily loss limit" in pushed[0][1]
    assert b"4.4" not in pushed[0][1], "the phone message carries no amounts"
    assert watcher.tick(state, now + timedelta(minutes=25), **args) is None, "once a day"


def test_a_limit_in_the_quiet_window_waits_for_after_the_scheduled_run(watcher_env):
    near = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)
    memory = {"open": [{"ticker": "PLD", "dollars_in": 50.0, "entry": 125.0, "opened": "2026-11-02T15:00+00:00"}],
              "trader": {"equity": 100.0}}
    state, sent = {}, []
    args = dict(quote=lambda t: {"symbol": t, "price": 114.0, "day_change": 114 / 125 - 1},
                crypto_quote=lambda t: None, news=None, memory=memory,
                post=lambda u, t, b: sent.append(b), push=lambda *a: None)
    assert watcher.tick(state, near, **args) is None
    assert len(state["pending"]) == 1 and state["pending"][0].startswith("LIMIT")
    later = watcher.tick(state, datetime(2026, 11, 3, 16, 51, tzinfo=timezone.utc), **args)
    assert later and "LIMIT" in later


def test_a_position_bought_today_counts_from_its_entry_not_yesterdays_close(watcher_env):
    now = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)
    memory = {"open": [{"ticker": "PLD", "dollars_in": 50.0, "entry": 112.0, "opened": "2026-11-03T15:00+00:00"}],
              "trader": {"equity": 100.0}}
    events = watcher.check({}, now, watcher.holdings(memory),
                           lambda t: {"symbol": t, "price": 114.0, "day_change": 114 / 125 - 1},
                           lambda t: None, None, account=watcher.account_view(memory, now))
    assert not any(e.startswith("LIMIT") for e in events), "bought at 112 today, now 114: a gain today"
