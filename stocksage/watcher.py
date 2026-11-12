"""The trader watcher: wake the trading routine the moment something happens.

The routine runs hourly. Between runs nobody watches, so this runs on the
always-on computer, checks every minute, and fires the routine's API trigger
when one of these happens to something the trader holds:

  * MOVE    a stock moves unusually far for THAT stock today, measured against
            the market: its day's change minus SPY's, in steps of 1.5x its normal
            daily range (the ATR the brief carries; never under 1.5% nor over 8%;
            a flat 3% when the range is unknown). Funds are measured as they are.
            Crypto: 5%+ from a check-point. A position bought today counts from
            its buy price, not yesterday's close (the rally it was bought into
            is not news)
  * NEAR    a holding is close to its stop without having touched it (within
            half its normal daily range, 0.75%-2.5%, and at least half the room
            between entry and stop used up): time to decide before the STOP.
            Once per stop level a day
  * HALFWAY a holding is halfway from its entry to its target while its stop
            still sits below the entry: time to raise the stop. Once per target
            and stop level (a new STOP line re-arms it)
  * DROP    a position sits 5%+ below what it was bought at (once a day each)
  * MARKET  SPY moves 1.5%+ today (once per direction per day)
  * NEWS    a new company headline on a holding (Finnhub, every 5 minutes;
            not for index or sector funds, which get market news all day).
            The headline must name the company AND an event that can move a
            price (results, guidance, a deal, a lawsuit, a rating change...);
            round-ups, comparisons and fund-flow items never wake the trader.
            News-only wake-ups are held to 3 a day: on 2026-11-05 headlines
            used the whole day's allowance and price wake-ups had to wait
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

  * ENTRY   an idea the trader rated HIGH or MEDIUM and did not buy reaches the
            entry price its IDEA line named, while settled cash is free and the
            day's discipline allows a buy. Only the latest idea per ticker, at most
            one trading day old, at most six at a time; once per entry level; at
            most 2 wake-ups a day carry nothing but ENTRY. The trader re-scores it:
            the wake-up is a prompt to look, never a reason to buy
  * EVENT   a review before something scheduled: a held stock reports earnings
            after today's close or before the next open (asked once, in the last
            hour of the session, when a fractional position can still be sold), or
            a major release (CPI, the Fed's decision, payrolls, core PCE) is due:
            15-60 minutes ahead in the session, or the afternoon before a
            pre-market one. Once per event, only while something is held

Firing costs a full routine run, so it is rationed: never within the 10 minutes
before or the 15 after a scheduled run (minute :35 of each hour), at least 20
minutes apart, at most 10 a day. One exception: a STOP or LIMIT in the minutes
after a scheduled run fires as soon as that run has written its RUN line (it has
finished, so the two cannot overlap). The message is plain facts; the routine reads
it as information (the routine-fire-payload block), never as an instruction.

It grades itself: about an hour after each wake-up it writes an AFTER line with the
prices then and now, and the journal's wake-up scorecard (in the trade memory and the
phone report) says per kind whether the trader acted and whether the price kept
going. The scorecard may widen or narrow MOVE and NEAR and lower the headline
allowance, only inside journal.WAKE_TUNE_BOUNDS. STOP and LIMIT are never tuned,
loosened or gated; the cap of 10 a day and the 20-minute cooldown are fixed.

The watcher is the only stop a fractional position has, so its own failures
reach the owner's phone: no stock prices for 10 minutes in the session (once a
day), and a STOP or LIMIT the trader has not answered with a run (once each).
The watchdog checks the WATCH lines this writes every two hours.

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
MOVE_ATR_MULT, MOVE_MIN, MOVE_MAX = 1.5, 0.015, 0.08     # a MOVE step: 1.5x the stock's normal day, fenced
NEAR_ATR_MULT, NEAR_MIN, NEAR_MAX, NEAR_DEFAULT = 0.5, 0.0075, 0.025, 0.015   # how close to a stop is "near"
SCHEDULED_MINUTE, QUIET_BEFORE, QUIET_AFTER = 35, 10, 15
COOLDOWN = timedelta(minutes=20)
DAILY_CAP = 10
NEWS_CAP = 3                                    # wake-ups a day that carry nothing but headlines
ENTRY_CAP = 2                                   # wake-ups a day that carry nothing but an idea's entry
ENTRY_CHASE = 0.015                             # a breakout entry passed by more than this has run away
ENTRY_UNDER = 0.03                              # a pullback entry undercut this far (no stop named) is broken
ENTRY_MIN_CASH = 10.0                           # settled cash that counts as "free" (the playbook's reserve)
IDEA_LEVELS = ("HIGH", "MEDIUM")
IDEAS_MAX = 6                                   # ideas watched at once: each is a price lookup a minute
EVENTS_EVERY = timedelta(hours=2)               # how often the calendars are re-read
EVENT_AHEAD = (timedelta(minutes=15), timedelta(minutes=60))   # a release in the session: wake this far ahead
REVIEW_HOUR = 15                                # 3:00-3:59 PM ET: the last hour a fractional share can be sold
# The scheduled releases that move the whole market within seconds.
MAJOR = re.compile(r"\bCPI\b|Federal Funds Rate|FOMC Statement|Non-Farm Employment Change|Core PCE Price Index", re.I)
QUOTE_FAIL_TICKS = 10                           # minutes without one stock price before the phone is told
ANSWER_WITHIN = timedelta(minutes=20)           # a STOP/LIMIT wake-up should show a run by then
ANSWER_WITHIN_QUIET = timedelta(minutes=30)     # ...when it had to wait out a scheduled run first
NEWS_EVERY = timedelta(minutes=5)
MEMORY_EVERY = timedelta(minutes=5)             # new positions and moved stops reach the watcher fast
BRIEF_EVERY = timedelta(minutes=30)             # each stock's normal daily range comes from the brief
FOLLOW_AFTER = timedelta(minutes=60)            # the price check that grades a wake-up
FOLLOW_GIVE_UP = timedelta(hours=3)
BETA = "experimental-cc-routine-2026-04-01"
HEARTBEAT = timedelta(hours=2)
PULSE = timedelta(minutes=5)           # the "still alive" file in Drive; silent for 20 minutes is told
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
# A headline is worth a run only if it reports something that can move the price...
MATERIAL = re.compile(
    r"\b(earnings|results|revenue|profit|guidance|forecast|outlook|beats?|miss(?:es|ed)?|rais(?:es|ed)|cuts?|"
    r"lowers?|slash(?:es|ed)|warn(?:s|ed|ing)?|fda|approv\w+|recall\w*|merger|merg(?:es|ing)|acqui\w+|buys?|"
    r"takeover|buyout|spin-?off|lawsuit|sues?|sued|settle\w*|fine[ds]?|probe|investigat\w+|antitrust|subpoena|"
    r"sec|doj|ftc|downgrad\w+|upgrad\w+|price target|ceo|cfo|resign\w*|steps down|ousted|fired|offering|buyback|"
    r"repurchase|dividend|split|layoffs?|job cuts|bankrupt\w*|halt\w*|delist\w*|breach|hack\w*|outage|contract|"
    r"deal|partnership|strike|plung\w+|soar\w*|surg\w+|tumbl\w+|jumps?|sinks?|slump\w*|crash\w*)\b", re.I)
# ...and is not a round-up, a comparison, an opinion piece or a fund-flow item. Every pattern
# here is a real headline that woke the trader for nothing in its first week.
NOISE = re.compile(
    r"\bvs\.?(?!\w)|\bversus\b|zacks|motley|analyst blog|prediction|\bform 8|\betfs?\b|\?|\bwhich\b|"
    r"\bshould you\b|\bbetter\b|\b\d+ (?:stocks|reasons|things)\b|\btop \d+\b|stocks to (?:buy|watch|sell)|"
    r"here'?s (?:why|what)|what to know|-(?:backed|owned|linked|powered)\b|\bformer\b|\bshares of\b|"
    r"(?:sells|buys|acquires|trims|boosts|raises|cuts|lowers) (?:its )?(?:stake|position|holdings)", re.I)


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


def ranges_from_brief(brief: dict) -> dict:
    """Each stock's normal daily range (ATR as a share of its price) from the brief:
    its `ranges` map, and the cards under focus and candidates. Odd values are dropped."""
    out: dict[str, float] = {}
    pairs = list(((brief or {}).get("ranges") or {}).items())
    for key in ("focus", "candidates"):
        pairs += [(c.get("ticker"), c.get("atr_pct")) for c in (brief or {}).get(key) or [] if isinstance(c, dict)]
    for t, v in pairs:
        try:
            if t and 0.003 <= float(v) <= 0.25:
                out[str(t).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def read_ranges(state: dict, now: datetime, reader=None) -> dict:
    """The ranges, from the brief in Drive at most every 30 minutes; kept in the state
    file, so a failed read leaves the last known ones (and no ranges at all means the
    flat 3% rule)."""
    last = state.get("ranges_read")
    if reader is not None or not last or now - datetime.fromisoformat(last) >= BRIEF_EVERY:
        state["ranges_read"] = now.isoformat()
        try:
            if reader is None:
                from .drive_api import BRIEF_NAME, read_drive_text

                raw = read_drive_text(BRIEF_NAME)
            else:
                raw = reader()
            got = ranges_from_brief(json.loads(raw or "null") or {})
            if got:
                state["ranges"] = got
        except Exception as exc:
            log(f"could not read the brief's daily ranges ({type(exc).__name__}); keeping the last ones")
    return state.get("ranges") or {}


def tuning(memory: dict) -> dict:
    """The wake-up scorecard's settings from the trade memory, fenced a second time
    here: whatever the file says, nothing leaves journal.WAKE_TUNE_BOUNDS, and there
    is no setting at all for STOP or LIMIT."""
    from .journal import WAKE_TUNE_BOUNDS

    said = ((memory or {}).get("wakes") or {}).get("tuning") or {}
    out = {}
    for key, (lo, hi, default) in WAKE_TUNE_BOUNDS.items():
        try:
            out[key] = min(hi, max(lo, float(said.get(key, default))))
        except (TypeError, ValueError):
            out[key] = default
    return out


def move_step(ticker: str, ranges: dict | None = None, factor: float = 1.0) -> float:
    """How far a stock must move (against the market) for one MOVE step."""
    atr = (ranges or {}).get(ticker)
    base = MOVE_ATR_MULT * atr if atr else STOCK_MOVE
    return min(MOVE_MAX, max(MOVE_MIN, base * factor))


def near_band(ticker: str, stop: float, entry: float | None, ranges: dict | None = None,
              factor: float = 1.0) -> float:
    """How far above its stop a price still counts as near it: half a normal day,
    fenced, and never more than half the room the trade was given (a stop set 2%
    under the buy price is not "near" from the first minute)."""
    atr = (ranges or {}).get(ticker)
    band = min(NEAR_MAX, max(NEAR_MIN, (NEAR_ATR_MULT * atr if atr else NEAR_DEFAULT) * factor))
    if entry and entry > stop:
        band = min(band, 0.5 * (entry / stop - 1))
    return band


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


def _fresh(q: dict | None, now: datetime) -> dict | None:
    """A quote only counts if it was traded today (Eastern). Before the open the free
    quote still carries yesterday's close and yesterday's change: at 7:00 AM on
    2026-11-05 that woke the trader with 'MSFT +6.7% today' from the day before."""
    if not q or not q.get("at"):
        return q
    try:
        at = datetime.fromisoformat(str(q["at"]))
    except ValueError:
        return q
    return q if at.astimezone(ET).date() == now.astimezone(ET).date() else None


def check(state: dict, now: datetime, held: list[dict], quote, crypto_quote, news,
          account: dict | None = None, names=None, ranges: dict | None = None,
          tuned: dict | None = None) -> list[str]:
    """Plain-language events since the last check. Updates `state` so each fires once."""
    from .journal import DAILY_LOSS_LIMIT, is_crypto

    day = now.astimezone(ET).date().isoformat()
    if state.get("day") != day:
        state.update(day=day, moved={}, dropped=[], market=[], crypto_open={}, limit=False, stopped=[], targeted=[],
                     neared=[])
    events: list[str] = []
    in_hours = stock_hours(now)
    tuned = tuned or {}
    seen_at = state.setdefault("last_price", {})      # what a wake-up's later price check starts from
    live = state.setdefault("live", {})               # what the phone page shows: last price and day's change
    owned = {h["ticker"] for h in held}
    live["rows"] = {t: r for t, r in (live.get("rows") or {}).items() if t in owned}
    spy = None
    if in_hours:
        try:
            spy = _fresh(quote("SPY"), now)
            if spy:
                seen_at["SPY"] = spy["price"]
                live["spy"] = {"change": spy["day_change"], "at": now.isoformat()}
        except Exception:
            spy = None
    day_pnl = 0.0
    asked = answered = 0                        # stock quotes this minute: asked for, and came back
    for h in held:
        t, crypto = h["ticker"], is_crypto(h["ticker"])
        if not crypto and not in_hours:
            continue
        change = None
        asked += not crypto
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
                raw = quote(t)
                answered += raw is not None     # yesterday's price (a holiday) is still an answer
                q = _fresh(raw, now)
                price = q["price"] if q else None
                change = q["day_change"] if q else None
                bought_today = bool(h.get("lots")) and all(_opened_today(lot, now) for lot in h["lots"])
                move, since, versus = change, "today", None
                if bought_today and price and h.get("entry"):
                    move, since = price / h["entry"] - 1, "since it was bought today"
                elif change is not None and spy and t not in FUNDS:
                    versus = change - spy["day_change"]        # the stock's own move, the market's taken out
                measure = move if versus is None else versus
                if measure is not None:
                    step = move_step(t, ranges, tuned.get("MOVE", 1.0))
                    level = int((abs(measure) + 1e-9) // step) * (1 if measure > 0 else -1)
                    done = state["moved"].get(t, 0)
                    if level and (abs(level) > abs(done) or level * done < 0):
                        state["moved"][t] = level
                        normal = (ranges or {}).get(t)
                        events.append(f"MOVE: {t} {move:+.1%} {since}, now {_p(price)}"
                                      + (f" (bought at {_p(h['entry'])})" if h.get("entry") else "")
                                      + (f"; {versus:+.1%} against the market (SPY {spy['day_change']:+.1%})"
                                         if versus is not None else "")
                                      + (f"; its normal day is about {normal:.1%}" if normal else ""))
        except Exception:
            continue
        if price is None:
            continue
        seen_at[t] = price
        if crypto:
            day_open = state.setdefault("crypto_open", {}).setdefault(t, price)
        else:
            day_open = price / (1 + change) if change is not None and change > -1 else None
        live["rows"][t] = {"price": price, "at": now.isoformat(),
                           "change": price / day_open - 1 if crypto and day_open else change}
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
        entry = h.get("entry")
        if stop and price > stop and f"{t}@{stop:g}" not in state.setdefault("neared", []) and \
                price <= stop * (1 + near_band(t, stop, entry, ranges, tuned.get("NEAR", 1.0))):
            state["neared"].append(f"{t}@{stop:g}")
            events.append(f"NEAR: {t} at {_p(price)} is {price / stop - 1:.1%} above its stop {_p(stop)}"
                          + (f" (bought at {_p(entry)})" if entry else "")
                          + ": decide now whether the reason to hold still stands; a STOP wake-up follows if it "
                            "is touched")
        if target and entry and target > entry and price < target and not (stop and stop >= entry):
            key = f"{t}@{target:g}@{stop or 0:g}"
            if price >= entry + 0.5 * (target - entry) and key not in state.setdefault("halfway", []):
                state["halfway"] = (state["halfway"] + [key])[-60:]
                events.append(f"HALFWAY: {t} at {_p(price)} is halfway from its entry {_p(entry)} to its target "
                              f"{_p(target)}; " + (f"its stop is still {_p(stop)}" if stop else "it has no stop on record")
                              + ": raise the stop (STOP line) so a winner cannot turn into a loss")
        if h.get("entry") and t not in state["dropped"] and price / h["entry"] - 1 <= DROP:
            state["dropped"].append(t)
            events.append(f"DROP: {t} is {price / h['entry'] - 1:+.1%} below its entry {_p(h['entry'])} "
                          f"(now {_p(price)}): check its stop")
    if regular_session(now) and asked:
        # No price at all for the stocks held: nothing is watching their stops this minute.
        state["quote_fail"] = 0 if answered else state.get("quote_fail", 0) + 1
    elif not asked:
        state["quote_fail"] = 0
    if spy:
        direction = "up" if spy["day_change"] > 0 else "down"
        if abs(spy["day_change"]) >= MARKET_MOVE and direction not in state["market"]:
            state["market"].append(direction)
            events.append(f"MARKET: SPY {spy['day_change']:+.1%} today")
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
                    known = len(words) > 1             # no name known: do not filter on it (better a spare wake-up)
                    if fresh and not first_look and material(item["title"]) and \
                            (not known or about_company(item["title"], words)):
                        events.append(f"NEWS: {h['ticker']}: {item['title']} ({item['source']})")
        state["news_seen"] = list(seen)[-500:]
    return events


# --- ideas waiting for their entry price ---------------------------------------------

def watched_ideas(memory: dict, now: datetime, held: list[dict]) -> list[dict]:
    """The memory's `idea_watch` list (journal.idea_watch), fenced a second time here:
    HIGH and MEDIUM only, not held, not expired, never more than six."""
    today = now.astimezone(ET).date().isoformat()
    owned = {h["ticker"] for h in held}
    out = []
    for i in (memory or {}).get("idea_watch") or []:
        try:
            t, entry = str(i["ticker"]).upper(), float(i["entry"])
            if t in owned or entry <= 0 or i.get("level") not in IDEA_LEVELS or str(i.get("expires") or "") < today:
                continue
            stop = float(i["stop"]) if i.get("stop") else None
            out.append(dict(i, ticker=t, entry=entry, stop=stop if stop and stop < entry else None,
                            direction="above" if i.get("direction") == "above" else "below"))
        except (KeyError, TypeError, ValueError):
            continue
    return out[:IDEAS_MAX]


def cash_free(state: dict, memory: dict, now: datetime) -> tuple[bool, str]:
    """May the trader open something right now? Settled cash from its last RUN line, and
    not on a day the daily loss limit has stopped new buys."""
    from .confidence import todays_discipline

    if state.get("limit") or todays_discipline(memory, now).get("limit_hit"):
        return False, "the daily loss limit allows no new buys today"
    try:
        cash = float((memory.get("trader") or {}).get("cash"))
    except (TypeError, ValueError):
        return False, "settled cash is not known"
    return (True, "") if cash >= ENTRY_MIN_CASH else (False, "no settled cash is free")


def _idea_when(iso) -> str:
    at = _when(iso)
    return at.astimezone(ET).strftime("%a %I:%M %p ET") if at else "earlier"


def entry_events(state: dict, now: datetime, memory: dict, held: list[dict], quote, crypto_quote) -> list[str]:
    """ENTRY: a watched idea has reached the entry price it named. A breakout entry (above
    the price the idea was written at) counts from the entry to 1.5% past it; further and
    it has run away (the playbook does not chase). A pullback entry (below) counts from
    the entry down to the idea's stop (3% under the entry when it named none); lower and
    the idea is broken. Once per entry level, kept across days."""
    from .confidence import todays_discipline
    from .journal import is_crypto

    ideas = watched_ideas(memory, now, held)
    rows: dict = {}
    state["idea_rows"] = rows                         # for the phone page
    if not ideas:
        return []
    day = now.astimezone(ET).date().isoformat()
    free, why_not = cash_free(state, memory, now)
    if free and state.get("entry_fires_day") == day and state.get("entry_fires", 0) >= ENTRY_CAP:
        free, why_not = False, f"today's {ENTRY_CAP} entry wake-ups are used"
    sold_at_a_loss = set(todays_discipline(memory, now).get("stopped_today") or [])
    done = state.setdefault("entered", [])
    seen_at = state.setdefault("last_price", {})
    events = []
    for i in ideas:
        t, entry, crypto = i["ticker"], i["entry"], is_crypto(i["ticker"])
        row = {"entry": entry, "direction": i["direction"], "level": i["level"], "setup": i.get("setup"),
               "expires": i.get("expires"), "waiting": why_not}
        rows[t] = row
        if not crypto and not regular_session(now):
            continue                                  # a new stock position is opened in the regular session
        try:
            price = crypto_quote(t) if crypto else (_fresh(quote(t), now) or {}).get("price")
        except Exception:
            price = None
        if not price:
            continue
        row.update(price=price, at=now.isoformat())
        seen_at[t] = price
        key = f"{t}@{entry:g}"
        if key in done:
            row["waiting"] = "the trader has been told"
            continue
        if t in sold_at_a_loss:
            row["waiting"] = "sold at a loss today: not bought back today"
            continue
        if not free:
            continue
        if i["direction"] == "above":
            reached = entry <= price <= entry * (1 + ENTRY_CHASE)
        else:
            reached = (i["stop"] or entry * (1 - ENTRY_UNDER)) < price <= entry
        if reached:
            state["entered"] = done = (done + [key])[-60:]
            row["waiting"] = "the trader has been told"
            events.append(f"ENTRY: {t} at {_p(price)} has reached the entry {_p(entry)} of your [{i['level']}] "
                          f"{i.get('setup') or 'buy'} idea from {_idea_when(i.get('at'))}"
                          + (f" (its stop was {_p(i['stop'])})" if i["stop"] else "")
                          + ", and settled cash is free: re-score it with the confidence command and buy only if "
                            "it still qualifies")
    return events


# --- reviews before scheduled events ---------------------------------------------------

def _next_session(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _reporting(day) -> list[dict]:
    """Who reports on `day`: Finnhub's calendar with the key the watcher already has (the
    Nasdaq one answered "Access Denied" from the always-on PC on 2026-11-11), else the
    live wire's."""
    from . import finnhub
    from . import live as wire

    if finnhub.key():
        try:
            return [r for r in finnhub.earnings_calendar(day, day) if r["day"] == day.isoformat()]
        except Exception:
            pass
    return wire.earnings(day, limit=None)


def read_events(state: dict, now: datetime, calendar=None, earnings=None) -> dict:
    """The week's major releases (the live wire's calendar) and who reports today and
    next session (`_reporting`), re-read every two hours in stock hours and kept in the state file
    (a failed read keeps the last answer; entries are looked up by date, so an old
    answer can only be silent, never wrong)."""
    last = state.get("events_read")
    injected = calendar is not None or earnings is not None
    if injected or (stock_hours(now) and (not last or now - datetime.fromisoformat(last) >= EVENTS_EVERY)):
        from . import live as wire

        state["events_read"] = now.isoformat()
        got = dict(state.get("events") or {})
        try:
            rows = calendar() if calendar else wire.calendar(now=now, first=0, last=6, impacts=("High",))
            got["releases"] = [{"at": r["at"], "title": r["title"], "forecast": r.get("forecast"),
                                "previous": r.get("previous")} for r in rows if MAJOR.search(r.get("title") or "")]
        except Exception as exc:
            log(f"could not read the economic calendar ({type(exc).__name__}); keeping the last one")
        today = now.astimezone(ET).date()
        by_day = dict(got.get("earnings") or {})
        for day in (today, _next_session(today)):
            try:
                rows = earnings(day) if earnings else _reporting(day)
                by_day[day.isoformat()] = {str(r["ticker"]).upper(): {"when": r.get("when"), "eps": r.get("eps_forecast")}
                                           for r in rows if r.get("ticker")}
            except Exception as exc:
                log(f"could not read the earnings calendar for {day} ({type(exc).__name__}); keeping the last one")
        got["earnings"] = {d: v for d, v in by_day.items() if d >= today.isoformat()}
        state["events"] = got
    return state.get("events") or {}


def upcoming(now: datetime, held: list[dict], events: dict) -> list[dict]:
    """What is scheduled for what is held: each row has a `key` (once per event), a plain
    `text`, and `review` = the wake-up is due now. Also what the phone page lists."""
    from .journal import is_crypto

    if not events or not held:
        return []
    et = now.astimezone(ET)
    today, nxt = et.date(), _next_session(et.date())
    last_hour = regular_session(now) and et.hour == REVIEW_HOUR
    out = []
    for h in held:
        t = h["ticker"]
        if is_crypto(t) or t in FUNDS:
            continue
        for day in (today, nxt):
            e = ((events.get("earnings") or {}).get(day.isoformat()) or {}).get(t)
            when = (e or {}).get("when") or "time not given"
            # Already out (before today's open), or tomorrow afternoon's question (after tomorrow's close).
            if not e or (day == today and when == "before the open") or (day == nxt and when == "after the close"):
                continue
            on = "today" if day == today else "on " + day.strftime("%a %d %b")
            out.append({"key": f"earn:{t}:{day.isoformat()}", "review": last_hour,
                        "text": f"{t} reports earnings {when if when != 'time not given' else '(time not given)'} {on}"
                                + (f" (EPS expected {e['eps']})" if e.get("eps") else "")
                                + ": decide before the close whether to hold through it or sell, and say which in "
                                  "the RUN line"})
    open_next = datetime(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=ET)
    by_time: dict = {}
    for r in events.get("releases") or []:
        at = _when(r.get("at"))
        if at and at > now:
            by_time.setdefault(at, []).append(r)
    for at in sorted(by_time):
        rows = by_time[at]
        in_session = at.astimezone(ET).date() == today and regular_session(at)
        if in_session:
            review = regular_session(now) and EVENT_AHEAD[0] <= at - now <= EVENT_AHEAD[1]
        elif at <= open_next:
            review = last_hour                       # out before the next open: the review is this afternoon
        else:
            review = False
        first = rows[0]
        names = ", ".join(dict.fromkeys(r["title"] for r in rows))[:80]
        out.append({"key": f"macro:{at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')}", "review": review,
                    "market": True,
                    "text": f"market: {names} is due {at.astimezone(ET).strftime('%a %I:%M %p ET')}"
                            + (f" (forecast {first['forecast']}, previous {first.get('previous') or '-'})"
                               if first.get("forecast") else "")
                            + ": check every stop and how much is at risk before it; no new positions in the 15 "
                              "minutes before a release"})
    return out


def pre_events(state: dict, now: datetime, held: list[dict], events: dict) -> list[str]:
    """EVENT: one review before each scheduled event, while something is held."""
    out = []
    for row in upcoming(now, held, events):
        if row["review"] and row["key"] not in state.setdefault("evented", []):
            state["evented"] = (state["evented"] + [row["key"]])[-60:]
            out.append("EVENT: " + row["text"])
    return out


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


def material(title: str) -> bool:
    """Does the headline report something that can move the price? Of the first 28
    wake-ups about 20 were headlines, most of them comparisons ("Oracle vs. Alphabet:
    which is the better stock"), blog round-ups and fund-flow notes. The hourly run
    reads the news anyway; only an event is worth a run of its own."""
    return bool(MATERIAL.search(title or "")) and not NOISE.search(title or "")


def message(events: list[str], now: datetime) -> str:
    head = f"StockSage watcher, {now.astimezone(ET).strftime('%a %I:%M %p ET')}. Wake-up reason(s):"
    return "\n".join([head, *("- " + e for e in events),
                      "Check these positions first (live quotes and news), then run as usual."])


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def note(line: str, post=None) -> bool:
    """Put the watcher's activity in the trade notes file, where the owner's
    dashboard and phone report read it. Skipped in tests (post injected) and
    never fatal: the watcher keeps watching if Drive is unreachable."""
    if post is not None:
        return True
    try:
        from .journal import append_note

        append_note(line[:600])
        return True
    except Exception as exc:
        log(f"could not record the note in Drive ({type(exc).__name__})")
        return False


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
    ok = note(f"WATCH | {_utc(now)} | {'started, ' if force else ''}watching {held} | {fires} wake-up(s) today")
    # The watchdog reads these lines to know the watcher is alive: one that did not reach
    # Drive is tried again in ten minutes, not in two hours.
    state["last_heartbeat"] = (now if ok else now - HEARTBEAT + timedelta(minutes=10)).isoformat()


def pulse(state: dict, now: datetime, publish=None) -> bool:
    """Overwrite the small pulse file in Drive every `PULSE` while stocks can trade, so the
    trader's hourly run (and the watchdog) can tell within 20 minutes that this computer
    has gone dark. Never fatal; a failed write is tried again in two minutes."""
    last = state.get("last_pulse")
    if not stock_hours(now) or (last and now - datetime.fromisoformat(last) < PULSE):
        return False
    try:
        if publish is None:
            from .drive_api import publish_text as publish
        from .journal import PULSE_NAME

        publish(PULSE_NAME, _utc(now))
        state["last_pulse"] = now.isoformat()
        return True
    except Exception as exc:
        log(f"could not write the pulse to Drive ({type(exc).__name__})")
        state["last_pulse"] = (now - PULSE + timedelta(minutes=2)).isoformat()
        return False


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


def _tradable_now(held: list[dict]) -> set[str]:
    """What can be sold outside 9:30-4:00: crypto, and stock positions of a whole share or
    more (extended hours take whole shares only)."""
    from .journal import is_crypto

    return {h["ticker"] for h in held if is_crypto(h["ticker"]) or (h.get("shares") or 0) >= 1}


def _can_trade_now(held: list[dict]) -> bool:
    return bool(_tradable_now(held))


def _event_ticker(event: str) -> str | None:
    m = re.match(r"(?:MOVE|DROP|TARGET|NEWS|STOP|NEAR|HALFWAY|ENTRY|EVENT): ([A-Z][A-Z0-9.\-]*)(?![a-z])", event)
    return m.group(1) if m else None


def _when(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def scheduled_run_done(now: datetime, memory: dict) -> bool:
    """In the quiet minutes AFTER a scheduled run: has that run already written its RUN
    line? Then it is over, a wake-up cannot overlap it, and a stop touched since has
    nobody looking at it until the quiet window ends."""
    utc = now.astimezone(timezone.utc)
    run = utc.replace(minute=SCHEDULED_MINUTE, second=0, microsecond=0)
    if run > utc:
        run -= timedelta(hours=1)
    if run.weekday() >= 5 or not 12 <= run.hour <= 23 or utc - run > timedelta(minutes=QUIET_AFTER):
        return False
    ran = _when((memory.get("trader") or {}).get("last_run"))
    return bool(ran and ran >= run)


def _self_checks(state: dict, now: datetime, memory: dict, push=None) -> None:
    """The watcher's own failures go to the owner's phone, because a fractional position
    has no other stop: (1) no stock price for ten minutes in the session, once a day;
    (2) a STOP or LIMIT that no trader run has followed (the wake-up did not go out, the
    routine is out of usage, its connector is signed out...), once per event."""
    day = now.astimezone(ET).date().isoformat()
    if state.get("quote_fail", 0) >= QUOTE_FAIL_TICKS and state.get("quote_alert_day") != day:
        state["quote_alert_day"] = day
        log(f"no stock price for {state['quote_fail']} minutes: stops are not being watched")
        phone("StockSage: the watcher cannot get prices", "The watcher has had no stock prices for "
              f"{state['quote_fail']} minutes, so it cannot see a stop being touched. The hourly trader still "
              "checks stops on each run. If this repeats, the price key may have expired.", push)
    waiting = state.get("urgent")
    if waiting:
        seen, due = _when(waiting.get("at")), _when(waiting.get("due"))
        ran = _when((memory.get("trader") or {}).get("last_run"))
        if not seen or not due or (ran and ran > seen):
            state.pop("urgent", None)
        elif now >= due:
            state.pop("urgent", None)
            minutes = int((now - seen).total_seconds() // 60)
            log(f"a stop or the daily limit was hit {minutes} minutes ago and no trader run has followed")
            phone("StockSage: the trader did not answer", "A stop or the daily loss limit was hit in the agentic "
                  f"account {minutes} minutes ago and the trader has not run since. Open Robinhood and look at "
                  "that account yourself.", push)


def follow_up(state: dict, now: datetime, quote, crypto_quote, post=None) -> list[str]:
    """Grade the watcher's own wake-ups: about an hour after each one, write an AFTER
    line with every named ticker's price then and now. The journal's scorecard reads
    it. Never fatal, never a wake-up; a check that cannot get a price is dropped after
    three hours."""
    from .journal import is_crypto

    written, waiting = [], []
    for f in state.get("followups") or []:
        at = _when(f.get("iso"))
        if at is None or not f.get("prices"):
            continue
        if now - at < FOLLOW_AFTER:
            waiting.append(f)
            continue
        pairs = []
        for t, then in f["prices"].items():
            try:
                if is_crypto(t):
                    price = crypto_quote(t)
                else:
                    price = (quote(t) or {}).get("price")
            except Exception:
                price = None
            if price and then:
                pairs.append(f"{t} {then:.6g} {price:.6g}")
        gave_up = now - at >= FOLLOW_GIVE_UP
        if len(pairs) < len(f["prices"]) and not gave_up:
            waiting.append(f)
            continue
        if pairs:
            line = f"AFTER | {f['at']} | {int((now - at).total_seconds() // 60)} | " + "; ".join(pairs)
            if note(line, post=post) or gave_up:
                written.append(line)
            else:
                waiting.append(f)
    state["followups"] = waiting[-20:]
    return written


def tick(state: dict, now: datetime, *, quote, crypto_quote, news, memory: dict, post=None,
         push=None, names=None, ranges: dict | None = None, events: dict | None = None,
         kinds=None) -> str | None:
    """One check. Returns the message that was sent, or None. `events` is the calendar
    packet from read_events (None: no pre-event reviews). `kinds` is only for the
    scenario lab, which replays older trigger sets on the same prices: the wake-up kinds
    allowed to speak. STOP and LIMIT cannot be switched off this way or any other."""
    from .journal import is_crypto

    held = holdings(memory)
    tuned = tuning(memory)
    scheduled = events
    events = check(state, now, held, quote, crypto_quote, news, account=account_view(memory, now), names=names,
                   ranges=ranges, tuned=tuned)
    state["checked"] = now.isoformat()
    try:                                        # the wider view must never cost a STOP its wake-up
        events += entry_events(state, now, memory, held, quote, crypto_quote)
        events += pre_events(state, now, held, scheduled or {})
    except Exception as exc:
        log(f"idea and calendar checks failed ({type(exc).__name__}: {exc})")
    if kinds is not None:
        events = [e for e in events if e.startswith(URGENT) or e.split(":", 1)[0] in kinds]
    if not near_scheduled_run(now):
        state.pop("held_said", None)
    _self_checks(state, now, memory, push)
    try:
        follow_up(state, now, quote, crypto_quote, post=post)
    except Exception as exc:                    # grading must never cost a wake-up
        log(f"wake-up price check failed ({type(exc).__name__})")
    if events and not regular_session(now):
        # Outside 9:30-4:00 a fractional stock position cannot be sold: a wake-up about it
        # can only look, so it is left to the next scheduled run. Decided per position
        # (one whole-share holding used to let every other position's headlines through).
        can = _tradable_now(held) | {t for t in map(_event_ticker, events) if t and is_crypto(t)}
        later = [e for e in events if not e.startswith(URGENT)
                 and (_event_ticker(e) not in can if _event_ticker(e) else not can)]
        if later:
            log("outside 9:30-4:00 and these cannot be traded now (fractional shares): left to the next "
                "scheduled run: " + "; ".join(later))
            events = [e for e in events if e not in later]
    if any(e.startswith("STOP") for e in events):
        phone("StockSage: a stop was touched", "A position in the agentic account touched the stop the trader "
              "set. The trader is being woken to act on it.", push)
    if any(e.startswith("LIMIT") for e in events):
        log(events[0])
        phone("StockSage: daily loss limit", "The agentic account hit its daily loss limit. The trader is being "
              "told: no new buys today, exits and stops only. Your switch: Robinhood trade approvals ON.", push)
    if any(e.startswith(URGENT) for e in events) and not state.get("urgent"):
        wait = ANSWER_WITHIN_QUIET if near_scheduled_run(now) else ANSWER_WITHIN
        state["urgent"] = {"at": now.isoformat(), "due": (now + wait).isoformat()}
    day = now.astimezone(ET).date().isoformat()
    pending = state.get("pending", []) + events
    owned = {h["ticker"] for h in held}
    sold = [e for e in pending if e.startswith("STOP") and _event_ticker(e) not in owned]
    if sold:                                    # a held-back stop the trader has since sold: nothing to wake for
        pending = [e for e in pending if e not in sold]
        log("no longer held, so no wake-up: " + "; ".join(sold))
        if not any(e.startswith(URGENT) for e in pending):
            state.pop("urgent", None)
        state["pending"] = pending
    if not pending:
        return None
    news_only = all(e.startswith("NEWS") for e in pending)
    news_fires = state.get("news_fires", 0) if state.get("news_fires_day") == day else 0
    news_cap = min(NEWS_CAP, int(tuned.get("NEWS_CAP", NEWS_CAP)))    # the scorecard can only lower it
    if news_only and news_fires >= news_cap:
        # Headlines may not use up the day's wake-ups: the hourly run reads the news anyway.
        if events:
            log(f"headline allowance of {news_cap} wake-ups used today: left to the next scheduled run: "
                + "; ".join(events))
        state["pending"] = []
        return None
    urgent = any(e.startswith(URGENT) for e in pending)
    ok, why = may_fire(state, now, urgent=urgent)
    if not ok and urgent and scheduled_run_done(now, memory):
        # The scheduled run has written its RUN line, so it is over: a stop or the daily
        # limit does not wait out the rest of the quiet window (it used to, up to 15 minutes).
        ok = True
        log("the scheduled run has finished: not waiting out the quiet window")
    if not ok:
        if near_scheduled_run(now):
            # Said once per quiet window: a HALFWAY or ENTRY that is asked again after the run
            # comes back every minute until then (the lab counted 25 log lines for one event).
            said = state.setdefault("held_said", [])
            fresh = [e for e in events if e.split(" at ")[0][:40] not in said]
            if fresh:
                state["held_said"] = (said + [e.split(" at ")[0][:40] for e in fresh])[-40:]
                log(f"held back (scheduled run covers it): {'; '.join(fresh)}")
            # the scheduled run sees the same prices; a LIMIT is still sent once it has run
            state["pending"] = [e for e in pending if e.startswith(URGENT)][-5:]
            # HALFWAY is said once per level, not once a day: one dropped here was never
            # said, so it is asked again after the run unless the run raised the stop.
            unsaid = {_event_ticker(e) for e in pending if e.startswith("HALFWAY")}
            if unsaid:
                state["halfway"] = [k for k in state.get("halfway") or [] if k.split("@")[0] not in unsaid]
            # The same for an idea's entry: the scheduled run does not know it was reached.
            unasked = {_event_ticker(e) for e in pending if e.startswith("ENTRY")}
            if unasked:
                state["entered"] = [k for k in state.get("entered") or [] if k.split("@")[0] not in unasked]
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
    state["fires_today"] = state.get("fires_today", 0) + 1 if state.get("fires_day") == day else 1
    state.update(fires_day=day, last_fire=now.isoformat(), pending=[])
    if news_only:
        state.update(news_fires_day=day, news_fires=news_fires + 1)
    if all(e.startswith("ENTRY") for e in pending):
        state.update(entry_fires_day=day,
                     entry_fires=(state.get("entry_fires", 0) if state.get("entry_fires_day") == day else 0) + 1)
    if urgent:                                 # the clock for an answer runs from the wake-up that went out
        state["urgent"] = {"at": now.isoformat(), "due": (now + ANSWER_WITHIN).isoformat()}
    log(f"woke the trader: {'; '.join(pending)}")
    note(f"WAKE | {_utc(now)} | " + "; ".join(pending), post=post)
    seen_at = state.get("last_price") or {}
    named = {_event_ticker(e) or "SPY" for e in pending}
    then = {t: seen_at[t] for t in sorted(named) if seen_at.get(t)}
    if then:                                    # the price check that will grade this wake-up
        state["followups"] = (state.get("followups") or []) + [{"at": _utc(now), "iso": now.isoformat(),
                                                                "prices": then}]
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
    if state.get("urgent"):
        state.pop("memory_read", None)          # waiting for the trader's answer: look for its run every minute
    memory = read_memory(state, now, reader=memory_reader)
    try:
        scheduled = read_events(state, now)
    except Exception as exc:                    # a calendar must never stop a check
        log(f"calendars not read ({type(exc).__name__})")
        scheduled = state.get("events") or {}
    sent = tick(state, now, quote=quote, crypto_quote=cq, news=news, memory=memory, names=_names(state),
                ranges=read_ranges(state, now), events=scheduled)
    heartbeat(state, now, memory, force=state.pop("starting", False))
    pulse(state, now)
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
    today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    fires = s.get("fires_today", 0) if s.get("fires_day") == today else 0
    headlines = s.get("news_fires", 0) if s.get("news_fires_day") == today else 0
    print(f"  wake-ups today: {fires} of {DAILY_CAP} ({headlines} of {NEWS_CAP} for headlines alone); "
          f"last: {s.get('last_fire') or 'never'}")
    if s.get("quote_fail", 0) >= QUOTE_FAIL_TICKS:
        print(f"  PRICES: none for {s['quote_fail']} minutes. Stops are not being watched. Run .\\start.bat keys "
              "to check the price key.")
    try:
        from .journal import is_crypto, state_dir

        memory = json.loads((state_dir() / "watcher_memory.json").read_text(encoding="utf-8"))
        tuned, ranges = tuning(memory), s.get("ranges") or {}
        stocks = [h["ticker"] for h in holdings(memory) if not is_crypto(h["ticker"])]
        if stocks:
            print("  MOVE wakes at (against the market; from each stock's normal day, 3% when unknown): "
                  + ", ".join(f"{t} {move_step(t, ranges, tuned['MOVE']):.1%}" for t in stocks))
        print(f"  learned settings: MOVE x{tuned['MOVE']:.2f}, NEAR x{tuned['NEAR']:.2f}, headline wake-ups "
              f"{min(NEWS_CAP, int(tuned['NEWS_CAP']))} a day (STOP and LIMIT are never tuned)")
        now = datetime.now(timezone.utc)
        ideas = watched_ideas(memory, now, holdings(memory))
        print("  ideas watched for their entry price: "
              + (", ".join(f"{i['ticker']} [{i['level']}] {i['direction']} {_p(i['entry'])}" for i in ideas)
                 or "none (an IDEA line needs a HIGH or MEDIUM level and 'entry <price>', and lasts one trading day)"))
        coming = upcoming(now, holdings(memory), s.get("events") or {})
        print("  coming up for what is held: " + ("; ".join(r["text"].split(":")[0] if not r.get("market")
                                                             else r["text"].split(": check")[0] for r in coming)
                                                  or "nothing on the calendars"))
    except Exception:
        pass
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
