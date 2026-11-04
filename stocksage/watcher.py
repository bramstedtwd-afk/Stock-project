"""The trader watcher: wake the trading routine the moment something happens.

The routine runs hourly. Between runs nobody watches, so this runs on the
always-on computer, checks every minute, and fires the routine's API trigger
when one of these happens to something the trader holds:

  * MOVE    a stock moves 3%+ today (crypto 5%+), and again each further 3%/5%;
            a position bought today counts from its buy price, not yesterday's
            close (the rally it was bought into is not news)
  * DROP    a position sits 5%+ below what it was bought at (once a day each)
  * MARKET  SPY moves 1.5%+ today (once per direction per day)
  * NEWS    a new company headline on a holding (Finnhub, every 5 minutes;
            not for index or sector funds, which get market news all day)
  * STOP    a holding touches the stop the trader named (its buy's why, or a later
            STOP line). Fractional shares cannot carry a resting stop order, so
            this is the stop: urgent, once per stop level a day, past the
            cooldown and cap, kept through the quiet window
  * TARGET  a holding reaches the target the trader named (once a day)
  * LIMIT   the account is 4%+ down on the day (today's moves on what is held,
            plus trades bought and sold today, against the last check-in
            before today): the daily loss limit. Once a day; it skips the cooldown and
            the daily cap, and also tells the owner's phone. The playbook then
            allows no new buys until tomorrow. StockSage still never trades:
            the owner's own switch is trade approvals ON in Robinhood.

Firing costs a full routine run, so it is rationed: never within the 10 minutes
before or the 15 after a scheduled run (minute :35 of each hour), at least 20
minutes apart, at most 10 a day. The message is plain facts; the routine reads
it as information (the routine-fire-payload block), never as an instruction.

Positions come from the trade memory (Drive's copy, re-read every 15 minutes);
prices from Finnhub (stocks) and OKX (crypto). No broker sign-in, ever.
Settings in .env: STOCKSAGE_TRADER_FIRE_URL, STOCKSAGE_TRADER_TOKEN,
STOCKSAGE_FINNHUB_KEY. Commands: `.\\start.bat watch-trader [install|off|once|status]`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
URL_VAR, TOKEN_VAR = "STOCKSAGE_TRADER_FIRE_URL", "STOCKSAGE_TRADER_TOKEN"
STOCK_MOVE, CRYPTO_MOVE, DROP, MARKET_MOVE = 0.03, 0.05, -0.05, 0.015
SCHEDULED_MINUTE, QUIET_BEFORE, QUIET_AFTER = 35, 10, 15
COOLDOWN = timedelta(minutes=20)
DAILY_CAP = 10
NEWS_EVERY = timedelta(minutes=5)
MEMORY_EVERY = timedelta(minutes=5)             # new positions and moved stops reach the watcher fast
BETA = "experimental-cc-routine-2026-04-01"
HEARTBEAT = timedelta(hours=2)
STARTUP_FILE = "StockSage Watcher.cmd"
# Brands people write instead of the listed name.
ALIASES = {"GOOGL": ["Google", "Alphabet", "YouTube"], "GOOG": ["Google", "Alphabet", "YouTube"],
           "META": ["Meta", "Facebook", "Instagram"], "BRK.B": ["Berkshire"], "JPM": ["JPMorgan"],
           "KO": ["Coca-Cola"], "PG": ["Procter"], "WMT": ["Walmart"], "UNH": ["UnitedHealth"]}
NAME_SKIP = {"the", "first", "american", "united", "general", "international", "global", "national", "inc", "corp",
             "group", "holdings", "company", "co"}
# Funds get general market news all day: a NEWS wake-up is for a company's own news only.
FUNDS = {"SPY", "VOO", "IVV", "VTI", "QQQ", "DIA", "IWM", "SGOV", "BIL", "SHV", "TLT", "IEF", "GLD", "SLV",
         "SH", "PSQ", "RWM", "SDS", "QID", "SPXU", "SQQQ", "TQQQ", "SSO", "QLD", "UPRO",
         "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB"}


def state_path() -> Path:
    from .journal import state_dir

    return state_dir() / "watcher_state.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        from .journal import state_dir

        with open(state_dir() / "watcher.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- when to watch and when firing is allowed -------------------------------------

def stock_hours(now: datetime) -> bool:
    """Weekdays 7:00 AM to 8:00 PM Eastern: pre-market, the session, after-hours."""
    et = now.astimezone(ET)
    return et.weekday() < 5 and 7 <= et.hour < 20


def regular_session(now: datetime) -> bool:
    """9:30 AM to 4:00 PM Eastern on weekdays: when a fractional position can be sold."""
    et = now.astimezone(ET)
    return et.weekday() < 5 and (et.hour, et.minute) >= (9, 30) and et.hour < 16


def near_scheduled_run(now: datetime) -> bool:
    """The trader runs at :35 past each hour from 12:35 to 23:35 UTC on weekdays."""
    utc = now.astimezone(timezone.utc)
    if utc.weekday() >= 5 or not 12 <= utc.hour <= 23:
        return False
    run = utc.replace(minute=SCHEDULED_MINUTE, second=0, microsecond=0)
    for r in (run - timedelta(hours=1), run, run + timedelta(hours=1)):
        if timedelta(0) <= r - utc <= timedelta(minutes=QUIET_BEFORE) or \
                timedelta(0) <= utc - r <= timedelta(minutes=QUIET_AFTER):
            return 12 <= r.hour <= 23
    return False


URGENT = ("LIMIT", "STOP")


def may_fire(state: dict, now: datetime, urgent: bool = False) -> tuple[bool, str]:
    if near_scheduled_run(now):
        return False, "a scheduled run is about to start or just ran"
    if urgent:
        return True, ""
    day = now.astimezone(ET).date().isoformat()
    if state.get("fires_day") == day and state.get("fires_today", 0) >= DAILY_CAP:
        return False, f"daily cap of {DAILY_CAP} wake-ups reached"
    last = state.get("last_fire")
    if last and now - datetime.fromisoformat(last) < COOLDOWN:
        return False, "the last wake-up was under 20 minutes ago"
    return True, ""


# --- what is held -----------------------------------------------------------------

def holdings(memory: dict) -> list[dict]:
    """Open positions from the trade memory, one per ticker, with the average entry."""
    by: dict[str, dict] = {}
    for lot in memory.get("open") or []:
        t = str(lot.get("ticker", "")).upper()
        if not t:
            continue
        cur = by.setdefault(t, {"ticker": t, "dollars": 0.0, "shares": 0.0, "lots": []})
        dollars, entry = float(lot.get("dollars_in") or 0), float(lot.get("entry") or 0)
        cur["dollars"] += dollars
        cur["shares"] += dollars / entry if entry else 0.0
        if entry:
            cur["lots"].append({"shares": dollars / entry, "entry": entry, "opened": lot.get("opened")})
        if lot.get("stop"):
            cur["stop"] = max(cur.get("stop") or 0.0, float(lot["stop"]))      # the most protective one
        if lot.get("target"):
            cur["target"] = min(cur.get("target") or float("inf"), float(lot["target"]))
    out = []
    for h in by.values():
        h["entry"] = round(h["dollars"] / h["shares"], 4) if h["shares"] else None
        out.append(h)
    return out


def read_memory(state: dict, now: datetime, reader=None) -> dict:
    """The trade memory, from Drive at most every 15 minutes, else the local copy."""
    from .journal import MEMORY_NAME, state_dir

    last = state.get("memory_read")
    if reader is not None or not last or now - datetime.fromisoformat(last) >= MEMORY_EVERY:
        try:
            if reader is None:
                from .drive_api import read_drive_text

                reader = read_drive_text
            raw = reader(MEMORY_NAME)
            if raw:
                (state_dir() / "watcher_memory.json").write_text(raw, encoding="utf-8")
                state["memory_read"] = now.isoformat()
        except Exception as exc:
            log(f"could not read the trade memory from Drive ({type(exc).__name__}); using the local copy")
    for name in ("watcher_memory.json", "trade_memory.json"):
        try:
            return json.loads((state_dir() / name).read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def account_view(memory: dict, now: datetime) -> dict | None:
    """What the daily limit is measured against: the last check-in before today and
    today's realized result, from the trade memory (only if worked out today)."""
    from .confidence import todays_discipline

    d = todays_discipline(memory, now)
    start = d.get("day_start_equity") or (memory.get("trader") or {}).get("equity")
    # Older positions sold today are left out: their loss from entry is not today's loss.
    return {"start": float(start), "realized": float(d.get("realized_today_new") or 0)} if start else None


def _opened_today(lot: dict, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(lot.get("opened"))).astimezone(ET).date() == now.astimezone(ET).date()
    except (TypeError, ValueError):
        return False


def crypto_price(pair: str, fetch=None) -> float | None:
    from .crypto import _json

    sym = pair.upper().replace("-USD", "")
    rows = _json(f"https://www.okx.com/api/v5/market/ticker?instId={sym}-USDT", fetch).get("data") or []
    return float(rows[0]["last"]) if rows else None


# --- events -----------------------------------------------------------------------

def _p(x: float) -> str:
    """A price as people write it: 65,000 / 125.40 / 0.000012."""
    return f"{x:,.0f}" if x >= 1000 else f"{x:,.2f}" if x >= 1 else f"{x:.6g}"


def check(state: dict, now: datetime, held: list[dict], quote, crypto_quote, news,
          account: dict | None = None, names=None) -> list[str]:
    """Plain-language events since the last check. Updates `state` so each fires once."""
    from .journal import DAILY_LOSS_LIMIT, is_crypto

    day = now.astimezone(ET).date().isoformat()
    if state.get("day") != day:
        state.update(day=day, moved={}, dropped=[], market=[], crypto_open={}, limit=False, stopped=[], targeted=[])
    events: list[str] = []
    in_hours = stock_hours(now)
    day_pnl = 0.0
    for h in held:
        t, crypto = h["ticker"], is_crypto(h["ticker"])
        if not crypto and not in_hours:
            continue
        change = None
        try:
            if crypto:
                price = crypto_quote(t)
                base = state.setdefault("crypto_base", {}).get(t)
                if price is not None and (base is None or abs(price / base - 1) >= CRYPTO_MOVE):
                    state["crypto_base"][t] = price
                    if base is not None:
                        events.append(f"MOVE: {t} {price / base - 1:+.1%} since the last check-point, now {_p(price)}"
                                      + (f" (bought at {_p(h['entry'])})" if h.get("entry") else ""))
            else:
                q = quote(t)
                price = q["price"] if q else None
                change = q["day_change"] if q else None
                bought_today = bool(h.get("lots")) and all(_opened_today(lot, now) for lot in h["lots"])
                move, since = change, "today"
                if bought_today and price and h.get("entry"):
                    move, since = price / h["entry"] - 1, "since it was bought today"
                if move is not None:
                    level = int(abs(move) // STOCK_MOVE) * (1 if move > 0 else -1)
                    done = state["moved"].get(t, 0)
                    if level and (abs(level) > abs(done) or level * done < 0):
                        state["moved"][t] = level
                        events.append(f"MOVE: {t} {move:+.1%} {since}, now {_p(price)}"
                                      + (f" (bought at {_p(h['entry'])})" if h.get("entry") else ""))
        except Exception:
            continue
        if price is None:
            continue
        if crypto:
            day_open = state.setdefault("crypto_open", {}).setdefault(t, price)
        else:
            day_open = price / (1 + change) if change is not None and change > -1 else None
        for lot in h.get("lots") or []:
            basis = lot["entry"] if _opened_today(lot, now) else day_open
            if basis:
                day_pnl += lot["shares"] * (price - basis)
        stop, target = h.get("stop"), h.get("target")
        if not crypto and not regular_session(now):
            stop = target = None                # a fractional position cannot be sold now: tell the run that can
        if stop and price <= stop and f"{t}@{stop:g}" not in state.setdefault("stopped", []):
            state["stopped"].append(f"{t}@{stop:g}")
            events.insert(0, f"STOP: {t} at {_p(price)} has touched its stop {_p(stop)}"
                             + (f" (bought at {_p(h['entry'])})" if h.get("entry") else "")
                             + ": sell, or say in the RUN line why not")
        if target and price >= target and t not in state.setdefault("targeted", []):
            state["targeted"].append(t)
            events.append(f"TARGET: {t} at {_p(price)} has reached its target {_p(target)}: take the profit "
                          "or raise the stop (STOP line)")
        if h.get("entry") and t not in state["dropped"] and price / h["entry"] - 1 <= DROP:
            state["dropped"].append(t)
            events.append(f"DROP: {t} is {price / h['entry'] - 1:+.1%} below its entry {_p(h['entry'])} "
                          f"(now {_p(price)}): check its stop")
    if in_hours:
        try:
            spy = quote("SPY")
            if spy:
                direction = "up" if spy["day_change"] > 0 else "down"
                if abs(spy["day_change"]) >= MARKET_MOVE and direction not in state["market"]:
                    state["market"].append(direction)
                    events.append(f"MARKET: SPY {spy['day_change']:+.1%} today")
        except Exception:
            pass
    if account and account.get("start") and not state.get("limit"):
        total = day_pnl + account.get("realized", 0.0)
        if total / account["start"] <= DAILY_LOSS_LIMIT:
            state["limit"] = True
            events.insert(0, f"LIMIT: the account is about {total / account['start']:+.1%} today "
                             f"({total:+.2f}: holdings {day_pnl:+.2f}, sold {account.get('realized', 0.0):+.2f}); "
                             f"the daily loss limit is {DAILY_LOSS_LIMIT:.0%}. Playbook: no new buys until "
                             "tomorrow; manage exits and stops")
    last_news = state.get("news_checked")
    if news is not None and (not last_news or now - datetime.fromisoformat(last_news) >= NEWS_EVERY):
        state["news_checked"] = now.isoformat()
        first_look = "news_seen" not in state      # the first check only learns what is already out
        seen = set(state.get("news_seen", []))
        for h in held:
            if is_crypto(h["ticker"]) or h["ticker"] in FUNDS:
                continue
            try:
                items = news(h["ticker"])
            except Exception:
                continue
            words = name_words(h["ticker"], names(h["ticker"]) if names else "")
            for item in items[:8]:
                fresh = now - datetime.fromisoformat(item["at"]) <= timedelta(hours=2)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    known = len(words) > 1             # no name known: do not filter (better a spare wake-up)
                    if fresh and not first_look and (not known or about_company(item["title"], words)):
                        events.append(f"NEWS: {h['ticker']}: {item['title']} ({item['source']})")
        state["news_seen"] = list(seen)[-500:]
    return events


# --- firing -----------------------------------------------------------------------

def phone(title: str, text: str, push=None) -> None:
    """A push to the owner's phone (ntfy), if alerts are set up. Role and verb only:
    ntfy topics are not private, so no amounts or account numbers."""
    from . import notify

    topic = notify.topic_from_env()
    if not topic:
        return
    try:
        (push or notify._post)(notify.NTFY_URL.format(topic=topic), text.encode("utf-8"),
                               {"Title": title, "Priority": "high", "Tags": "warning"})
    except Exception as exc:
        log(f"could not alert the phone ({type(exc).__name__})")


def name_words(ticker: str, name: str = "") -> list[str]:
    """Words that mark a headline as about THIS company: the ticker, the first real word
    of its name, and known brands. Finnhub tags market-wide stories ("Dow jumps 1,500
    points") with every big ticker; three such wake-ups were wasted on 2026-11-04."""
    words = [ticker.upper()] + ALIASES.get(ticker.upper(), [])
    for w in re.findall(r"[A-Za-z][A-Za-z&'.-]+", name or ""):
        if w.lower().strip(".") not in NAME_SKIP and len(w) >= 3:
            words.append(w.strip("."))
            break
    return words


def about_company(title: str, words: list[str]) -> bool:
    for w in words:
        flags = 0 if w.isupper() and len(w) <= 5 else re.I     # a ticker must match in capitals
        if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", title or "", flags):
            return True
    return False


def message(events: list[str], now: datetime) -> str:
    head = f"StockSage watcher, {now.astimezone(ET).strftime('%a %I:%M %p ET')}. Wake-up reason(s):"
    return "\n".join([head, *("- " + e for e in events),
                      "Check these positions first (live quotes and news), then run as usual."])


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def note(line: str, post=None) -> None:
    """Put the watcher's activity in the trade notes file, where the owner's
    dashboard and phone report read it. Skipped in tests (post injected) and
    never fatal: the watcher keeps watching if Drive is unreachable."""
    if post is not None:
        return
    try:
        from .journal import append_note

        append_note(line[:600])
    except Exception as exc:
        log(f"could not record the note in Drive ({type(exc).__name__})")


def heartbeat(state: dict, now: datetime, memory: dict, force: bool = False) -> None:
    """A WATCH line when the watcher starts and every two hours while it watches."""
    last = state.get("last_heartbeat")
    if not force and last and now - datetime.fromisoformat(last) < HEARTBEAT:
        return
    if not force and not stock_hours(now):
        return
    held = ", ".join(h["ticker"] for h in holdings(memory)) or "nothing held"
    day = now.astimezone(ET).date().isoformat()
    fires = state.get("fires_today", 0) if state.get("fires_day") == day else 0
    state["last_heartbeat"] = now.isoformat()
    note(f"WATCH | {_utc(now)} | {'started, ' if force else ''}watching {held} | {fires} wake-up(s) today")


def _post(url: str, token: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": BETA, "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def fire(text: str, post=None) -> dict:
    url, token = os.environ.get(URL_VAR, "").strip(), os.environ.get(TOKEN_VAR, "").strip()
    if not url or not token:
        raise RuntimeError("the trader's API trigger is not set up (run .\\start.bat keys)")
    if not url.startswith("https://api.anthropic.com/") or not url.rstrip("/").endswith("/fire"):
        raise RuntimeError("the saved fire URL does not look like the routine's /fire endpoint")
    return (post or _post)(url, token, {"text": text[:4000]})


def tick(state: dict, now: datetime, *, quote, crypto_quote, news, memory: dict, post=None,
         push=None, names=None) -> str | None:
    """One check. Returns the message that was sent, or None."""
    events = check(state, now, holdings(memory), quote, crypto_quote, news, account=account_view(memory, now),
                   names=names)
    if any(e.startswith("STOP") for e in events):
        phone("StockSage: a stop was touched", "A position in the agentic account touched the stop the trader "
              "set. The trader is being woken to act on it.", push)
    if any(e.startswith("LIMIT") for e in events):
        log(events[0])
        phone("StockSage: daily loss limit", "The agentic account hit its daily loss limit. The trader is being "
              "told: no new buys today, exits and stops only. Your switch: Robinhood trade approvals ON.", push)
    pending = state.get("pending", []) + events
    if not pending:
        return None
    ok, why = may_fire(state, now, urgent=any(e.startswith(URGENT) for e in pending))
    if not ok:
        if near_scheduled_run(now):
            if events:
                log(f"held back (scheduled run covers it): {'; '.join(events)}")
            # the scheduled run sees the same prices; a LIMIT is still sent once it has run
            state["pending"] = [e for e in pending if e.startswith(URGENT)][-5:]
        else:
            state["pending"] = pending[-10:]
            log(f"waiting to wake the trader ({why}): {'; '.join(events) or 'earlier events'}")
        return None
    text = message(pending, now)
    try:
        fire(text, post=post)
    except Exception as exc:
        state["pending"] = pending[-10:]
        log(f"could not wake the trader: {exc}")
        return None
    day = now.astimezone(ET).date().isoformat()
    state["fires_today"] = state.get("fires_today", 0) + 1 if state.get("fires_day") == day else 1
    state.update(fires_day=day, last_fire=now.isoformat(), pending=[])
    log(f"woke the trader: {'; '.join(pending)}")
    note(f"WAKE | {_utc(now)} | " + "; ".join(pending), post=post)
    return text


def _sources():
    from . import finnhub

    def quote(t):
        return finnhub.quote(t)

    def news(t):
        return finnhub.company_news(t, days=1)

    return quote, crypto_price, news


def _names(state: dict):
    """Company names from Finnhub, looked up once per ticker and kept in the state file."""
    from . import finnhub

    cache = state.setdefault("names", {})

    def name(t):
        if t not in cache:
            try:
                cache[t] = finnhub.company_name(t)
            except Exception:
                return ""                       # try again next time
        return cache[t]

    return name


def run_once(memory_reader=None) -> str | None:
    from .envfile import load_env

    load_env()
    state = load_state()
    now = datetime.now(timezone.utc)
    quote, cq, news = _sources()
    memory = read_memory(state, now, reader=memory_reader)
    sent = tick(state, now, quote=quote, crypto_quote=cq, news=news, memory=memory, names=_names(state))
    heartbeat(state, now, memory, force=state.pop("starting", False))
    save_state(state)
    return sent


def serve() -> int:
    from . import finnhub
    from .envfile import load_env

    load_env()
    missing = [v for v in (URL_VAR, TOKEN_VAR, finnhub.KEY_VAR) if not os.environ.get(v, "").strip()]
    if missing:
        log("not watching: missing " + ", ".join(missing) + ". Run .\\start.bat keys to add them.")
        return 1
    log("watching the trader's positions every minute (close this window to stop)")
    state = load_state()
    state["starting"] = True
    save_state(state)
    while True:
        try:
            run_once()
        except Exception as exc:          # one bad minute must never stop the watcher
            log(f"check failed: {type(exc).__name__}: {exc}")
        time.sleep(60)


def status() -> int:
    from . import finnhub
    from .envfile import load_env

    load_env()
    s = load_state()
    print("Trader watcher")
    for var in (URL_VAR, TOKEN_VAR, finnhub.KEY_VAR):
        print(f"  {var}: {'set' if os.environ.get(var, '').strip() else 'MISSING'}")
    print(f"  wake-ups today: {s.get('fires_today', 0) if s.get('fires_day') else 0}; last: {s.get('last_fire') or 'never'}")
    print(f"  starts at sign-in: {'yes' if (startup_dir() / STARTUP_FILE).exists() else 'no'}"
          if sys.platform.startswith("win") else "")
    try:
        from .journal import state_dir

        tail = (state_dir() / "watcher.log").read_text(encoding="utf-8").splitlines()[-8:]
        print("  recent activity:" if tail else "  recent activity: none yet")
        for line in tail:
            print("    " + line)
    except OSError:
        print("  recent activity: none yet (is the watcher window open?)")
    return 0


def test() -> int:
    """Fire one wake-up to prove the URL and token work. It starts one real trading run."""
    from .envfile import load_env

    load_env()
    now = datetime.now(timezone.utc)
    text = (f"StockSage watcher TEST, {now.astimezone(ET).strftime('%a %I:%M %p ET')}: the owner is checking that "
            "wake-ups reach you. No position event. Run as usual and mention 'wake-up test received' in the RUN line.")
    try:
        res = fire(text)
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "403" in msg:
            msg += " (the token is wrong or was regenerated: run .\\start.bat keys and paste the newest one)"
        print(f"Wake-up FAILED: {msg}")
        log(f"test wake-up failed: {msg}")
        return 1
    print("Wake-up sent. The trader is starting a run now.")
    if res.get("claude_code_session_url"):
        print(f"Watch it here: {res['claude_code_session_url']}")
    log("test wake-up sent")
    return 0


def startup_dir() -> Path:
    from .mobile import startup_dir as sd

    return sd()


def startup_script() -> str:
    from .mobile import PROJECT_ROOT

    bat = PROJECT_ROOT / "start.bat"
    return ("@echo off\r\n"
            "rem Starts the StockSage trader watcher at sign-in. Remove with: .\\start.bat watch-trader off\r\n"
            f'start "StockSage watcher" /min "{bat}" watch-trader serve\r\n')


def install() -> int:
    if not sys.platform.startswith("win"):
        print("Automatic start is set up for Windows. Elsewhere, keep `./start.sh watch-trader serve` running.")
        return 1
    try:
        startup_dir().mkdir(parents=True, exist_ok=True)
        (startup_dir() / STARTUP_FILE).write_bytes(startup_script().encode("utf-8"))
    except OSError as exc:
        print(f"Could not set it to start automatically: {exc}")
        return 1
    import subprocess

    from .mobile import PROJECT_ROOT

    try:
        subprocess.Popen(["cmd", "/c", "start", "StockSage watcher", "/min", str(PROJECT_ROOT / "start.bat"),
                          "watch-trader", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    print("The trader watcher now starts by itself when you sign in, and is starting now in a small window.")
    return 0


def off() -> int:
    (startup_dir() / STARTUP_FILE).unlink(missing_ok=True)
    print("The watcher will no longer start by itself. (Close its window to stop the one running now.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    action = (argv if argv is not None else sys.argv[1:]) or ["status"]
    if action[0] == "once":
        sent = run_once()
        print(sent or "Nothing worth waking the trader for right now.")
        return 0
    return {"serve": serve, "install": install, "off": off, "status": status, "test": test}.get(action[0], status)()


if __name__ == "__main__":
    raise SystemExit(main())
