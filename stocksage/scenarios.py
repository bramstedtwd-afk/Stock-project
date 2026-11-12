"""Scenario lab: what the trader's rules and memory do in every kind of market.

The trader is Claude, so its judgement cannot be replayed. Everything it obeys
is code, though: confidence scoring, the discipline rules, the drawdown brake,
stops and targets, the trade memory's verdicts and lessons, idea grading. This
lab runs a RULE-FOLLOWING STAND-IN through simulated markets where the truth is
planted (which setups really make money in which kind of market) and checks
whether the real machinery learns it.

The stand-in recognises a setup when one appears (the trader's job; assumed
perfect here), scores it with `confidence.score`, sizes it by the level and by
the risk to its stop (playbook section 3a), sets a 2:1 target, exits on stop,
target or time, and writes the same TAG / IDEA / RUN lines the trader writes.
`journal.summarize` turns those lines into the memory every morning, exactly as
in production, and `journal.grade_ideas` grades the ideas weekly. Four checks a
day (open and three in-session runs), settled cash only, idle cash parked in
SPY with a third kept back (playbook section 4).

Worlds (each a sequence of market phases with their own setup edges):
  bull, meltup, bear (a 2022-style grind), crash (2020-shaped: a 35% fall in 23
  days, then the rebound), chop (sideways, mean-reverting), noise (NO setup has
  any edge and look-alike evidence is random: the null test).
Agents (ablations):
  park_only     no trading at all: idle cash parked by the playbook's rule (SPY,
                SGOV in a down tape, a third kept back). The yardstick: what
                trading ADDS is system minus park_only on the same market
  system        everything on (the live rules)
  no_learning   memory never consulted (sizes from look-alikes and regime only)
  no_discipline no daily limit / revenge rule / cool-off / brake
  tilt          no discipline AND doubles risk after each loss (the bad trader)

What this can and cannot say: it proves the RULES and the LEARNING LOOP behave
(right verdicts, how many trades it takes, what the guardrails cost and save).
It cannot say what edges real markets offer; the planted edges are assumptions,
stated in WORLDS. Results go to STRATEGY.md. Run: `.\\start.bat scenarios`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from . import confidence as cf
from . import journal as jr

STEPS = 4                                  # open, two midday runs, the close
STEP_TIMES = ((14, 35), (16, 35), (18, 35), (20, 55))      # UTC; ET 9:35 .. 3:55 in winter
STEP_VAR = (0.34, 0.22, 0.22, 0.22)       # share of a day's variance: the overnight gap is the biggest
START = datetime(2025, 1, 6, tzinfo=timezone.utc)
UNIVERSE = 40
CASH0 = 100.0
COST = 0.0005                              # each side: spread and slippage (no commissions at Robinhood)

# setup -> (play, hold days, stop in daily vols, idiosyncratic daily vol while held)
SETUPS = {
    "pullback": ("SAFE", 5, 1.5, 0.016), "breakout": ("SAFE", 5, 1.5, 0.018),
    "momentum": ("AGGRESSIVE", 2, 2.0, 0.030), "news": ("AGGRESSIVE", 1, 2.0, 0.035),
    "gap": ("AGGRESSIVE", 1, 2.0, 0.030), "bounce": ("AGGRESSIVE", 3, 2.0, 0.030),
    "social": ("AGGRESSIVE", 1, 2.0, 0.045), "inverse": ("SAFE", 5, 1.5, 0.0),
}
LEVEL_CAP = {"HIGH": 0.50, "MEDIUM": 0.25, "LOW": 0.07, "NO": 0.0}
RISK_GUIDE = {"SAFE": 0.01, "AGGRESSIVE": 0.03}
MIX = {"pullback": 3, "breakout": 2, "momentum": 2, "news": 1, "gap": 1, "bounce": 1, "social": 1}


@dataclass
class Phase:
    days: int
    drift: float                 # SPY mean return a day
    vol: float                   # SPY daily volatility
    edges: dict                  # setup -> extra daily drift while held (the planted truth)
    label: str = ""


def _e(**kw) -> dict:
    return {s: kw.get(s, 0.0) for s in SETUPS if s != "inverse"}


# Planted edges, a day while held (x hold days = a trade). Sized like real retail edges:
# a setup that "works" earns about 0.4-1% a trade before the market's own move.
BULL_EDGES = _e(pullback=0.0015, breakout=0.0005, momentum=0.002, news=0.004, gap=-0.005, social=-0.008)
BEAR_EDGES = _e(pullback=-0.0015, breakout=-0.002, momentum=-0.004, news=0.0, gap=-0.004, bounce=0.003,
                social=-0.01)
WORLDS = {
    "bull": [Phase(250, 0.0006, 0.008, BULL_EDGES, "steady bull")],
    "meltup": [Phase(250, 0.0012, 0.010, _e(pullback=0.0015, breakout=0.002, momentum=0.004, news=0.003,
                                              gap=0.0, social=0.0), "melt-up")],
    "bear": [Phase(40, 0.0004, 0.010, BULL_EDGES, "late bull"),
             Phase(210, -0.0013, 0.016, BEAR_EDGES, "2022-style grind down")],
    "crash": [Phase(60, 0.0006, 0.008, BULL_EDGES, "calm bull"),
              Phase(23, -0.018, 0.040, _e(pullback=-0.006, breakout=-0.006, momentum=-0.008, news=-0.004,
                                           gap=-0.006, bounce=-0.004, social=-0.01), "crash"),
              Phase(60, 0.006, 0.025, _e(pullback=0.002, breakout=0.001, momentum=0.003, bounce=0.006,
                                          gap=-0.002, social=-0.004), "rebound"),
              Phase(107, 0.0006, 0.010, BULL_EDGES, "back to bull")],
    "chop": [Phase(250, 0.0, 0.013, _e(pullback=0.0015, breakout=-0.002, momentum=-0.004, news=0.0,
                                       gap=0.003, bounce=0.003, social=-0.008), "sideways chop")],
    "noise": [Phase(250, 0.0004, 0.011, _e(), "no edge anywhere")],
}
EVIDENCE = {"noise": "random"}             # every other world: look-alikes are informative but noisy


@dataclass
class Agent:
    name: str = "system"
    learning: bool = True
    discipline: bool = True          # master switch for the four guardrails below
    brake: str | None = "trade"      # "trade": the trades' own P&L below its best (live); "abs": the account's value
    cooloff: bool = True
    limit: bool = True
    revenge: bool = True
    quiet_record: bool = False       # an undecided record ("no clear edge yet") adds no points; ideas speak instead
    context: bool = True             # the memory's per-regime record (setup in THIS kind of market) is used
    verdict_t: float | None = None   # the memory's t bar for "working"/"losing" (None: journal.VERDICT_T)
    tilt: bool = False
    park: bool = True
    trade: bool = True


AGENTS = {"park_only": Agent("park_only", trade=False),
          "system": Agent(), "no_learning": Agent("no_learning", learning=False),
          "no_discipline": Agent("no_discipline", discipline=False),
          "tilt": Agent("tilt", discipline=False, tilt=True),
          "account_brake": Agent("account_brake", brake="abs"), "no_brake": Agent("no_brake", brake=None),
          "no_cooloff": Agent("no_cooloff", cooloff=False), "no_limit": Agent("no_limit", limit=False),
          "no_revenge": Agent("no_revenge", revenge=False),
          "quiet_record": Agent("quiet_record", quiet_record=True), "no_context": Agent("no_context", context=False),
          "strict_2_5": Agent("strict_2_5", verdict_t=2.5), "loose_2_0": Agent("loose_2_0", verdict_t=2.0),
          "quiet_strict": Agent("quiet_strict", quiet_record=True, verdict_t=2.5)}
CORE_AGENTS = ["park_only", "system", "no_learning", "no_discipline", "tilt"]


@dataclass
class Result:
    world: str
    agent: str
    seed: int
    days: int
    ret: float = 0.0
    spy: float = 0.0
    max_dd: float = 0.0
    spy_dd: float = 0.0
    trades: int = 0
    win_rate: float | None = None
    avg_r: float | None = None
    levels: dict = field(default_factory=dict)
    blocked: dict = field(default_factory=dict)     # limit / revenge / cooloff / brake
    verdicts: dict = field(default_factory=dict)    # setup -> (trades, verdict, truth)
    cells: list = field(default_factory=list)       # (kind, setup, n, t, truth): every graded group
    trade_log: list = field(default_factory=list)   # (setup, stock, entry step, exit step, return)
    calibration: str = ""


def _tickers(n: int) -> list[str]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    for a in letters:
        for b in letters:
            t = "Q" + a + b                         # letters only: tickers with digits are refused
            if t not in ("QQQ",):
                out.append(t)
            if len(out) == n:
                return out
    return out


def _when(n_days: int) -> list[list[datetime]]:
    out, d = [], START
    while len(out) < n_days:
        if d.weekday() < 5:
            out.append([d.replace(hour=h, minute=m) for h, m in STEP_TIMES])
        d += timedelta(days=1)
    return out


def _stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def build_market(world: str, seed: int) -> dict:
    """Prices at every check, the planted setup events, and the truth behind them."""
    rng = np.random.default_rng(seed)
    phases = WORLDS[world]
    n = sum(p.days for p in phases)
    phase_of = np.concatenate([[i] * p.days for i, p in enumerate(phases)])
    w = np.sqrt(np.array(STEP_VAR))
    spy_r = np.zeros((n, STEPS))
    for d in range(n):
        p = phases[phase_of[d]]
        spy_r[d] = p.drift / STEPS + p.vol * w * rng.standard_normal(STEPS)
    tickers = _tickers(UNIVERSE)
    beta = rng.uniform(0.7, 1.4, UNIVERSE)
    base_vol = rng.uniform(0.012, 0.022, UNIVERSE)
    stock_r = beta[:, None, None] * spy_r[None] + base_vol[:, None, None] * w * rng.standard_normal((UNIVERSE, n, STEPS))
    names = list(MIX)
    weights = np.array([MIX[s] for s in names], dtype=float)
    events = []
    flat = stock_r.reshape(UNIVERSE, n * STEPS)
    flow = np.zeros_like(flat)                      # the planted drift alone: the truth a trade could earn
    for d in range(n - 6):
        p = phases[phase_of[d]]
        for _ in range(rng.poisson(3)):
            k, setup = int(rng.integers(UNIVERSE)), names[int(rng.choice(len(names), p=weights / weights.sum()))]
            step = int(rng.integers(STEPS - 1))       # never at the close: there is no run after it
            play, hold, _, vol = SETUPS[setup]
            start = d * STEPS + step + 1
            span = slice(start, min(start + hold * STEPS, n * STEPS))
            m = span.stop - span.start
            flat[k, span] += p.edges.get(setup, 0.0) / STEPS + vol * math.sqrt(1 / STEPS) * rng.standard_normal(m) * 0.6
            flow[k, span] += p.edges.get(setup, 0.0) / STEPS
            events.append({"day": d, "step": step, "k": k, "ticker": tickers[k], "setup": setup,
                           "edge": p.edges.get(setup, 0.0) * hold, "phase": p.label})
    spy = 100 * np.exp(np.cumsum(spy_r.reshape(-1))).reshape(n, STEPS)
    sh = 100 * np.exp(np.cumsum(-spy_r.reshape(-1) - 0.00004)).reshape(n, STEPS)
    prices = 50 * np.exp(np.cumsum(flat, axis=1)).reshape(UNIVERSE, n, STEPS)
    # inverse-fund setups: when SPY is below its 50-day average and falling, SH is a candidate
    closes = spy[:, -1]
    for d in range(55, n - 6):
        if closes[d - 1] < closes[d - 51:d - 1].mean() and closes[d - 1] < closes[d - 6] and rng.random() < 0.25:
            fwd = closes[min(d + 5, n - 1)] / closes[d - 1] - 1
            events.append({"day": d, "step": 0, "k": -1, "ticker": "SH", "setup": "inverse", "edge": -fwd,
                           "phase": phases[phase_of[d]].label})
    events.sort(key=lambda e: (e["day"], e["step"]))
    return {"n": n, "tickers": tickers, "prices": prices, "spy": spy, "sh": sh, "base_vol": base_vol, "flow": flow,
            "events": events, "phases": phases, "phase_of": phase_of, "when": _when(n)}


def _price(mkt: dict, ticker: str, d: int, s: int) -> float:
    if ticker == "SPY":
        return float(mkt["spy"][d, s])
    if ticker == "SH":
        return float(mkt["sh"][d, s])
    return float(mkt["prices"][mkt["tickers"].index(ticker), d, s])


def _regime(spy_closes: np.ndarray, d: int) -> str:
    """The same definition the memory groups by (journal.regime_of_closes), from closes before day d."""
    return jr.regime_of_closes(spy_closes[:d])


def _evidence(ev: dict, world: str, rng) -> dict | None:
    """Look-alikes: a noisy read of the planted weekly edge (random in the null world)."""
    if ev["ticker"] == "SH":
        return None
    if EVIDENCE.get(world) == "random":
        est = float(rng.normal(0, 0.01))
    else:
        est = ev["edge"] / max(SETUPS[ev["setup"]][1], 1) * 5 + float(rng.normal(0, 0.008))
    lower = est - 0.006
    return {"n": 150, "dates": 60, "edge": est, "lower": lower, "hit_rate": 0.5 + est * 5, "backed": lower > 0}


def _confirmations(ev: dict, world: str, rng) -> int:
    if EVIDENCE.get(world) == "random":
        return int(rng.integers(0, 4))
    lean = 1 if ev["edge"] > TRUTH_BAR else -1 if ev["edge"] < -TRUTH_BAR else 0
    return int(np.clip(rng.integers(0, 3) + (lean > 0), 0, 3))


def simulate(world: str, seed: int = 0, agent: Agent | None = None, mkt: dict | None = None,
             days: int | None = None) -> Result:
    """One agent through one world. Deterministic for a seed."""
    agent = agent or AGENTS["system"]
    if agent.verdict_t is None:
        return _simulate(world, seed, agent, mkt, days)
    saved, jr.VERDICT_T = jr.VERDICT_T, agent.verdict_t
    try:
        return _simulate(world, seed, agent, mkt, days)
    finally:
        jr.VERDICT_T = saved


def _simulate(world: str, seed: int, agent: Agent, mkt: dict | None, days: int | None) -> Result:
    mkt = mkt or build_market(world, seed)
    n = days or mkt["n"]
    rng = np.random.default_rng(seed + 7919)
    when, spy_closes = mkt["when"], mkt["spy"][:, -1]
    by_day: dict[int, list] = {}
    for ev in mkt["events"]:
        by_day.setdefault(ev["day"], []).append(ev)
    tags, fills, runs, ideas = [], [], [], []
    cash, parked, unsettled = CASH0, 0.0, []            # parked = SPY units; unsettled = [(day, amount)]
    pos: list[dict] = []
    res = Result(world, agent.name, seed, n, levels={"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO": 0},
                 blocked={"limit": 0, "revenge": 0, "cooloff": 0, "brake": 0})
    memory: dict = {}
    curve, losses_row = [], 0
    planted: dict[tuple, float] = {}                # (ticker, buy time) -> planted edge earned while held
    idea_truth: dict[str, list] = {}
    graded: dict = {}
    trip_cache: list = [-1, []]
    peak = CASH0

    def equity(d, s):
        return cash + sum(x for _, x in unsettled) + parked * _price(mkt, "SPY", d, s) + \
            sum(p["shares"] * _price(mkt, p["ticker"], d, s) for p in pos)

    def note(line: str, parse):
        got = parse(line)
        assert got, f"the lab wrote a line the journal refuses: {line}"
        return got[0]

    def sell(p, d, s, exit_word):
        nonlocal losses_row
        px = _price(mkt, p["ticker"], d, s) * (1 - COST)
        unsettled.append((d, p["shares"] * px))
        t = note(f"TAG | {_stamp(when[d][s])} | SELL | {p['ticker']} | {p['play']} | {exit_word} | "
                 f"{p['shares']:.6f} | {px:.4f} | {exit_word}", jr.parse_tags)
        tags.append(t)
        fills.append({"order_id": f"s{len(fills)}", "ticker": p["ticker"], "side": "sell", "quantity": p["shares"],
                      "price": px, "executed_at": t["at"].isoformat()})
        losses_row = losses_row + 1 if px < p["entry"] else 0
        if p["k"] >= 0:
            planted[(p["ticker"], p["at"])] = float(mkt["flow"][p["k"], p["opened"] + 1:d * STEPS + s + 1].sum())
            res.trade_log.append((p["setup"], p["k"], p["opened"], d * STEPS + s, px / p["entry"] - 1))
        pos.remove(p)

    for d in range(n):
        cash += sum(x for day, x in unsettled if day < d)          # yesterday's sales have settled
        unsettled = [(day, x) for day, x in unsettled if day >= d]
        regime = _regime(spy_closes, d)
        if agent.learning:
            memory = jr.summarize(fills, tags, spy=_spy_series(mkt, d), now=when[d][0], runs=runs, source="lab")
            if d % 5 == 0 and ideas:
                graded = _grade(ideas, mkt, d)
            memory["ideas"] = graded
        for s in range(STEPS):
            now = when[d][s]
            for p in list(pos):                                     # manage what is held first
                px = _price(mkt, p["ticker"], d, s)
                if px <= p["stop"]:
                    sell(p, d, s, "stop")
                elif px >= p["target"]:
                    sell(p, d, s, "target")
                elif (d * STEPS + s) - p["opened"] >= p["hold"] * STEPS:
                    sell(p, d, s, "time")
            eq = equity(d, s)
            runs.append(note(f"RUN | {_stamp(now)} | {eq:.2f} | {cash:.2f} | lab", jr.parse_runs))
            curve.append(eq)
            if agent.discipline:
                if trip_cache[0] != len(fills):
                    trip_cache[:] = [len(fills), jr.round_trips(jr.attach_tags(fills, tags), now=now)[0]]
                peak = max(peak, eq)
                dsc = dict(jr.discipline(trip_cache[1][-60:], runs[-12:], now))
                dsc["cool_off"] = dsc["cool_off"] and agent.cooloff
                dsc["limit_hit"] = dsc["limit_hit"] and agent.limit
                if not agent.revenge:
                    dsc["stopped_today"] = []
                memory = dict(memory, discipline=dsc, drawdown=round(eq / peak - 1, 4) if agent.brake == "abs" else None,
                              trading_drawdown=jr.trading_drawdown(trip_cache[1], eq) if agent.brake == "trade" else None)
                if agent.brake == "abs":                    # the old rule: the account's own value
                    memory.pop("trading_drawdown")
            for ev in [e for e in by_day.get(d, []) if e["step"] == s and agent.trade]:
                ticker, setup = ev["ticker"], ev["setup"]
                play, hold, stop_vols, _ = SETUPS[setup]
                lookalike = _evidence(ev, world, rng)
                mem = memory if agent.learning else {k: memory.get(k) for k in ("discipline", "drawdown",
                                                                                 "trading_drawdown")}
                if not agent.context:
                    mem = {k: v for k, v in mem.items() if k not in ("by_setup_regime", "regime_now")}
                if agent.quiet_record:
                    mem = dict(mem, **{g: {k: v for k, v in (mem.get(g) or {}).items()
                                           if not str(v.get("verdict", "")).startswith("no clear")}
                                       for g in ("by_setup", "by_play", "by_asset")})
                if not agent.discipline:
                    mem = {k: v for k, v in mem.items() if k not in ("discipline", "drawdown", "trading_drawdown")}
                r = cf.score(side="buy", setup=setup, asset="stocks", play=play, lookalike=lookalike, memory=mem,
                             regime=regime, confirmations=_confirmations(ev, world, rng), ticker=ticker, now=now)
                level = r["level"]
                reasons = " ".join(r["reasons"])
                for key, word in (("limit", "DAILY LIMIT"), ("revenge", "NO REVENGE"), ("cooloff", "COOL-OFF"),
                                  ("brake", "BRAKE")):
                    res.blocked[key] += word in reasons
                px = _price(mkt, ticker, d, s)
                ideas.append(note(f"IDEA | {_stamp(now)} | BUY | {ticker} | {setup} | {px:.4f} | [{level}] lab",
                                  jr.parse_ideas))
                if ev["k"] >= 0 and d + 5 < n:              # what the idea could earn by its 5-day grade
                    idea_truth.setdefault(setup, []).append(
                        float(mkt["flow"][ev["k"], d * STEPS + s + 1:(d + 5) * STEPS + STEPS].sum()))
                res.levels[level] += 1
                if level == "NO" or any(p["ticker"] == ticker for p in pos):
                    continue
                daily_vol = 0.011 if ticker == "SH" else float(mkt["base_vol"][mkt["tickers"].index(ticker)])
                dist = px * daily_vol * stop_vols
                risk = eq * RISK_GUIDE[play] * (2 ** min(losses_row, 3) if agent.tilt else 1)
                cap = eq * (LEVEL_CAP["HIGH"] if agent.tilt else LEVEL_CAP[level])
                spend = min(risk / dist * px, cap, cash + parked * _price(mkt, "SPY", d, s) * (1 - COST))
                if spend < 1.0:
                    continue
                if spend > cash:                                    # unpark SPY to pay (settles in the model)
                    need = (spend - cash) / (_price(mkt, "SPY", d, s) * (1 - COST))
                    parked -= need
                    cash += need * _price(mkt, "SPY", d, s) * (1 - COST)
                fill = px * (1 + COST)
                shares = spend / fill
                cash -= spend
                stop = fill - dist
                t = note(f"TAG | {_stamp(now)} | BUY | {ticker} | {play} | {setup} | {shares:.6f} | {fill:.4f} | "
                         f"[{level}] stop {stop:.4f} lab", jr.parse_tags)
                tags.append(t)
                fills.append({"order_id": f"b{len(fills)}", "ticker": ticker, "side": "buy", "quantity": shares,
                              "price": fill, "executed_at": t["at"].isoformat()})
                pos.append({"ticker": ticker, "play": play, "shares": shares, "entry": fill, "stop": stop,
                            "target": fill + 2 * dist, "hold": hold, "opened": d * STEPS + s, "k": ev["k"],
                            "setup": setup,
                            "at": t["at"].isoformat(timespec="minutes")})
            if s == STEPS - 1 and agent.park:                       # idle cash to SPY, a third kept back
                spy_px = _price(mkt, "SPY", d, s)
                if _regime(spy_closes, d + 1) == "down":            # playbook: a down tape parks in SGOV
                    cash += parked * spy_px * (1 - COST)
                    parked = 0.0
                else:
                    target = max(0.0, cash - eq / 3)
                    if target > 1:
                        parked += target / (spy_px * (1 + COST))
                        cash -= target
    for p in list(pos):
        sell(p, n - 1, STEPS - 1, "time")
    final = equity(n - 1, STEPS - 1)
    res.ret = round(final / CASH0 - 1, 4)
    res.spy = round(float(spy_closes[n - 1] / mkt["spy"][0, 0] - 1), 4)
    curve = np.array(curve)
    res.max_dd = round(float((curve / np.maximum.accumulate(curve) - 1).min()), 4)
    sc = mkt["spy"][:n].reshape(-1)
    res.spy_dd = round(float((sc / np.maximum.accumulate(sc) - 1).min()), 4)
    summary = jr.summarize(fills, tags, spy=_spy_series(mkt, n), now=when[n - 1][-1] + timedelta(hours=1),
                           runs=runs, source="lab")
    o = summary["overall"]
    res.trades, res.win_rate, res.avg_r = o.get("trades", 0), o.get("win_rate"), o.get("avg_r")
    # The truth for a trade verdict is the planted edge of the trades actually taken in that setup;
    # for an idea verdict, of every idea noted (ideas are not filtered by confidence).
    trips, _ = jr.round_trips(jr.attach_tags(fills, tags), spy=_spy_series(mkt, n), now=when[n - 1][-1])
    taken: dict[str, list] = {}
    for t in trips:
        if (t.ticker, t.opened) in planted:
            taken.setdefault(t.setup, []).append(planted[(t.ticker, t.opened)])
    for setup, st in summary["by_setup"].items():
        mean = _mean(taken.get(setup))
        res.verdicts[setup] = (st["trades"], st["verdict"], _label(taken.get(setup, [])))
        if setup != "inverse":                       # an inverse fund's truth is the market itself
            res.cells.append(("trades", setup, st["trades"], st.get("t"), mean))
    for setup, st in _grade(ideas, mkt, n).get("by_setup", {}).items():
        if setup != "inverse":
            res.cells.append(("ideas", setup, st["ideas"], st.get("t"), _mean(idea_truth.get(setup))))
    res.calibration = next((x for x in summary["lessons"] if "calibrated" in x), "")
    return res


def _days(mkt: dict, d: int):
    import pandas as pd

    return pd.DatetimeIndex([mkt["when"][i][0].replace(tzinfo=None, hour=0, minute=0) for i in range(d)])


def _spy_series(mkt: dict, d: int):
    """SPY's closes known before day d (yesterday's and earlier): what the memory sees."""
    import pandas as pd

    return pd.Series(mkt["spy"][:d, -1], index=_days(mkt, d))


def _grade(ideas: list, mkt: dict, d: int) -> dict:
    """Grade ideas with closes known up to day d only (no look-ahead)."""
    import pandas as pd

    idx = _days(mkt, d)

    def closes(t):
        if t == "SH":
            return pd.Series(mkt["sh"][:d, -1], index=idx)
        return pd.Series(mkt["prices"][mkt["tickers"].index(t), :d, -1], index=idx)

    spy = pd.Series(mkt["spy"][:d, -1], index=idx)
    return jr.grade_ideas(ideas, closes, spy=spy, now=mkt["when"][min(d, len(mkt["when"]) - 1)][0])


TRUTH_BAR = 0.0025                         # a planted edge under 0.25% a trade counts as none


def _mean(xs) -> float:
    return float(sum(xs) / len(xs)) if xs else 0.0


def _label(edges: list[float]) -> str:
    if not edges:
        return "none"
    m = sum(edges) / len(edges)
    return "works" if m > TRUTH_BAR else "loses" if m < -TRUTH_BAR else "none"


def _truth(mkt: dict, upto: int | None = None) -> dict:
    """Each setup's planted average edge per trade across the events that happened."""
    sums: dict[str, list] = {}
    for ev in mkt["events"]:
        if upto is None or ev["day"] < upto - 5:
            sums.setdefault(ev["setup"], []).append(ev["edge"])
    return {s: _label(v) for s, v in sums.items()}


NO_EDGE = 0.001                            # a planted edge under 0.1% a trade: calling it either way is wrong


def judge_t(n: int, t: float | None, truth: float, bar: float, min_n: int) -> str:
    """A verdict at a given t bar: right, WRONG or undecided. `truth` is the planted
    edge the graded trades (or ideas) actually carried, on average."""
    if t is None or n < min_n or abs(t) < bar:
        return "undecided"
    return "right" if abs(truth) >= NO_EDGE and (t > 0) == (truth > 0) else "WRONG"


def verdict_quality(results: list, kind: str = "trades", bars=(2.0, 2.5, 3.0)) -> list[dict]:
    """How often the memory's verdicts are right, wrong or undecided at each t bar."""
    min_n = jr.MIN_TO_JUDGE if kind == "trades" else jr.MIN_IDEAS
    cells = [c for r in results if r.agent == "system" for c in r.cells if c[0] == kind]
    out = []
    for bar in bars:
        got = [judge_t(n, t, truth, bar, min_n) for _, _, n, t, truth in cells]
        decided = got.count("right") + got.count("WRONG")
        real = [g for g, c in zip(got, cells) if abs(c[4]) >= TRUTH_BAR]
        out.append({"bar": bar, "cells": len(cells), "right": got.count("right"), "wrong": got.count("WRONG"),
                    "undecided": got.count("undecided"),
                    "wrong_share": round(got.count("WRONG") / decided, 3) if decided else None,
                    "found": round(real.count("right") / len(real), 3) if real else None})
    return out


def judge(verdict: str, truth: str) -> str:
    """Was the memory's verdict right? Undecided is not wrong; a wrong call is."""
    if verdict.startswith("working"):
        return "right" if truth == "works" else "WRONG"
    if verdict.startswith("losing"):
        return "right" if truth == "loses" else "WRONG"
    return "undecided"


def trades_to_prove(edge: float, sd: float) -> int:
    """Closed trades the memory needs before its t-test says 'working' (t >= 2)."""
    return math.ceil((2 * sd / edge) ** 2) if edge > 0 else 10 ** 9


# --- the context table: what the rules make of each situation ------------------------

BACKED = {"n": 150, "dates": 60, "edge": 0.006, "lower": 0.002, "hit_rate": 0.58, "backed": True}
LEANS = {"n": 150, "dates": 60, "edge": 0.002, "lower": -0.003, "hit_rate": 0.53, "backed": False}
LOSES = {"n": 150, "dates": 60, "edge": -0.008, "lower": -0.012, "hit_rate": 0.41, "backed": False}
WORKING = {"trades": 14, "avg_return": 0.02, "verdict": "working: keep doing it"}
LOSING = {"trades": 12, "avg_return": -0.015, "verdict": "losing money: do less of this"}


def context_table() -> list[tuple[str, str, str]]:
    """(situation, level, why) for the situations the trader meets most."""
    now = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
    day = "2026-11-04"
    rows = [
        ("Look-alikes back it, setup record working, market up, 2 confirmations",
         dict(setup="pullback", lookalike=BACKED, memory={"by_setup": {"pullback": WORKING}}, regime="up",
              confirmations=2)),
        ("Look-alikes back it, no record yet, market mixed", dict(setup="pullback", lookalike=BACKED, regime="mixed")),
        ("Nothing known (new name, no record)", dict(setup="news")),
        ("Look-alikes lean right, market down", dict(setup="breakout", lookalike=LEANS, regime="down")),
        ("Look-alikes say it loses, 3 confirmations", dict(setup="momentum", lookalike=LOSES, regime="up",
                                                            confirmations=3)),
        ("Setup record losing, look-alikes back it", dict(setup="gap", lookalike=BACKED,
                                                          memory={"by_setup": {"gap": LOSING}}, regime="up")),
        ("Inverse fund in a falling market", dict(setup="inverse", regime="down", confirmations=2)),
        ("Bounce after a crash, look-alikes back it", dict(setup="bounce", lookalike=BACKED, regime="down",
                                                           confirmations=2)),
        ("Crypto trend buy, bitcoin above its 50-day", dict(setup="trend", asset="crypto",
                                                            crypto_flags={"btc_uptrend": True}, confirmations=2)),
        ("Crypto trend buy, bitcoin below its 50-day", dict(setup="trend", asset="crypto",
                                                            crypto_flags={"btc_uptrend": False})),
        ("Crypto fear buy in extreme fear", dict(setup="fear", asset="crypto", crypto_flags={"extreme_fear": True})),
        ("Strong setup, account 12% below its peak (brake)",
         dict(setup="pullback", lookalike=BACKED, regime="up", confirmations=2, memory={"drawdown": -0.12})),
        ("Strong setup after 2 losing sales today (cool-off)",
         dict(setup="pullback", lookalike=BACKED, regime="up", confirmations=2,
              memory={"discipline": {"day": day, "cool_off": True, "losses_in_a_row": 2}})),
        ("Buying back a name stopped out today", dict(setup="pullback", lookalike=BACKED, regime="up",
                                                      confirmations=2, ticker="AAA",
                                                      memory={"discipline": {"day": day, "stopped_today": ["AAA"]}})),
        ("Strong setup, account down 4.5% today (daily limit)",
         dict(setup="pullback", lookalike=BACKED, regime="up", confirmations=2,
              memory={"discipline": {"day": day, "limit_hit": True, "day_change": -0.045, "limit": -0.04}})),
        ("Same strong setup the morning after the bad day",
         dict(setup="pullback", lookalike=BACKED, regime="up", confirmations=2,
              memory={"discipline": {"day": "2026-11-03", "limit_hit": True, "cool_off": True}})),
    ]
    out = []
    for label, kw in rows:
        r = cf.score(side="buy", now=now, **kw)
        gate = next((x for x in r["reasons"] if x.startswith(("DAILY", "NO REVENGE", "COOL", "BRAKE"))), "")
        points = [x.strip() for x in r["reasons"] if x[:1] in "+-" and not x.startswith(("+0", "-0"))]
        short = [p.split("  ", 1)[-1].split(" - ")[-1] for p in points[:3]]
        why = gate.split(":")[0] + ": " + r["size"] if gate else "; ".join(short)
        out.append((label, f"{r['level']} ({r['score']:+d})", (why or r["size"])[:100]))
    return out


# --- the watcher on a crash day ------------------------------------------------------

def watcher_rehearsal(seed: int = 1) -> list[str]:
    """A 3% SPY selloff, minute by minute, with two holdings: what wakes the trader, when."""
    from . import watcher

    rng = np.random.default_rng(seed)
    open_ = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
    minutes = 390
    path = np.cumsum(rng.normal(-0.03 / minutes, 0.0009, minutes))
    spy = 700 * np.exp(path)
    names = {"AAA": 1.6, "BBB": 1.1}
    px = {t: 100 * np.exp(b * path + np.cumsum(rng.normal(0, 0.0012, minutes))) for t, b in names.items()}
    memory = {"open": [{"ticker": "AAA", "dollars_in": 40.0, "entry": 100.0, "opened": "2026-11-02T15:00+00:00"},
                       {"ticker": "BBB", "dollars_in": 40.0, "entry": 100.0, "opened": "2026-11-02T15:00+00:00"}],
              "trader": {"equity": 100.0}}
    import os

    state, sent = {}, []
    quiet = watcher.log
    saved = {v: os.environ.get(v) for v in (watcher.URL_VAR, watcher.TOKEN_VAR)}
    os.environ[watcher.URL_VAR] = "https://api.anthropic.com/v1/claude_code/routines/rehearsal/fire"
    os.environ[watcher.TOKEN_VAR] = "rehearsal"            # nothing is sent: `post` below swallows it
    clock = [open_]

    def heard(msg: str) -> None:
        if msg.startswith("held back"):
            et = clock[0].astimezone(watcher.ET).strftime("%I:%M %p")
            sent.append(f"{et} ET: (left to the scheduled run) " + msg.split(": ", 1)[-1])

    watcher.log = heard
    try:
        for i in range(minutes):
            now = clock[0] = open_ + timedelta(minutes=i)

            def quote(t, i=i):
                series = spy if t == "SPY" else px[t]
                return {"symbol": t, "price": float(series[i]), "day_change": float(series[i] / 100 - 1)
                        if t != "SPY" else float(series[i] / 700 - 1)}

            text = watcher.tick(state, now, quote=quote, crypto_quote=lambda t: None, news=None, memory=memory,
                                post=lambda u, t, b: None, push=lambda *a: None)
            if text:
                et = now.astimezone(watcher.ET).strftime("%I:%M %p")
                sent.append(f"{et} ET: " + "; ".join(x[2:] for x in text.splitlines()[1:-1]))
    finally:
        watcher.log = quiet
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return sent


# --- the watcher's trigger sets, old against new ------------------------------------------

TRIGGER_SETS = {
    # what could wake the trader before 2026-11-11 (MOVE was a flat 3%)
    "before": {"kinds": {"MOVE", "DROP", "MARKET", "TARGET"}, "ranges": False, "wider": False},
    # session 1: MOVE sized per stock, NEAR a stop, HALFWAY to a target
    "session 1": {"kinds": {"MOVE", "DROP", "MARKET", "TARGET", "NEAR", "HALFWAY"}, "ranges": True, "wider": False},
    # session 2: an idea's ENTRY and the pre-EVENT review as well
    "session 2": {"kinds": {"MOVE", "DROP", "MARKET", "TARGET", "NEAR", "HALFWAY", "ENTRY", "EVENT"},
                  "ranges": True, "wider": True},
}
REHEARSAL_STOCKS = (("CALM", 0.012, 0.8), ("MID", 0.025, 1.1), ("JUMPY", 0.045, 1.4), ("WILD", 0.07, 1.7))
REHEARSAL_IDEAS = (("IDEAA", 0.02), ("IDEAB", 0.035))


def _trigger_day(rng, day: int) -> dict:
    """One session, minute by minute: SPY, four holdings of very different temperament
    (each with the stop and target a 2:1 trade would carry), two watched ideas with a
    breakout entry, and sometimes an earnings report tonight or a release at 2 PM."""
    minutes = 390
    kind = rng.choice(["normal", "normal", "normal", "down", "selloff", "rally"])
    drift = {"normal": 0.0, "down": -0.012, "selloff": -0.03, "rally": 0.015}[kind]
    spy_r = rng.normal(drift / minutes, 0.008 / math.sqrt(minutes), minutes)
    spy_path = np.cumsum(spy_r)
    date = datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(days=7 * (day // 5) + day % 5)
    open_ = date.replace(hour=14, minute=30)
    series, held, ranges = {"SPY": 700 * np.exp(spy_path)}, [], {"SPY": 0.009}
    for name, atr, beta in REHEARSAL_STOCKS:
        entry = 100.0
        stop, target = entry * (1 - 1.5 * atr), entry * (1 + 3 * atr)
        start = max(stop * 1.012, entry * math.exp(rng.normal(0.3 * atr, 1.1 * atr)))
        start = min(start, target * 0.985)
        path = beta * spy_path + np.cumsum(rng.normal(0, 0.8 * atr / math.sqrt(minutes), minutes))
        series[name] = start * np.exp(path)
        ranges[name] = atr
        held.append({"ticker": name, "dollars_in": 25.0, "entry": entry, "stop": round(stop, 2),
                     "target": round(target, 2), "opened": (date - timedelta(days=3)).isoformat()})
    ideas = []
    for name, atr in REHEARSAL_IDEAS:
        path = 1.2 * spy_path + np.cumsum(rng.normal(0, 0.8 * atr / math.sqrt(minutes), minutes))
        series[name] = 50 * np.exp(path)
        ranges[name] = atr
        ideas.append({"ticker": name, "entry": round(50 * (1 + float(rng.uniform(0.3, 1.0)) * atr), 2),
                      "direction": "above", "price": 50.0, "level": "HIGH" if name.endswith("A") else "MEDIUM",
                      "setup": "breakout", "stop": round(50 * (1 - 1.5 * atr), 2),
                      "at": (open_ - timedelta(hours=18)).isoformat(), "expires": date.date().isoformat()})
    events: dict = {"earnings": {}, "releases": []}
    if rng.random() < 0.25:
        events["earnings"][date.date().isoformat()] = {"MID": {"when": "after the close", "eps": None}}
    if rng.random() < 0.2:
        events["releases"].append({"at": date.replace(hour=19).isoformat(), "title": "Federal Funds Rate",
                                   "forecast": None, "previous": None})
    return {"kind": kind, "open": open_, "series": series, "ranges": ranges, "events": events,
            "memory": {"open": held, "trader": {"equity": 100.0, "cash": 40.0}, "idea_watch": ideas}}


def trigger_rehearsal(days: int = 60, seed: int = 7) -> dict:
    """The watcher's old and new trigger sets on the SAME minute-by-minute prices: how
    often each wakes the trader, whether the cap and the cooldown hold, how much warning
    comes before a stop is touched, and whether a stop's own wake-up got slower. Nobody
    trades in here (a touched stop stays held), so it measures what the watcher says,
    not what the trader earns. CPU only; nothing is sent."""
    import os

    from . import watcher

    rng = np.random.default_rng(seed)
    worlds = [_trigger_day(rng, d) for d in range(days)]
    quiet = watcher.log
    saved = {v: os.environ.get(v) for v in (watcher.URL_VAR, watcher.TOKEN_VAR)}
    os.environ[watcher.URL_VAR] = "https://api.anthropic.com/v1/claude_code/routines/rehearsal/fire"
    os.environ[watcher.TOKEN_VAR] = "rehearsal"            # nothing is sent: `post` below swallows it
    out = {"days": days, "day_kinds": {k: sum(w["kind"] == k for w in worlds) for k in ("normal", "down", "selloff", "rally")},
           "sets": {}}
    left = [0]

    def heard(msg: str) -> None:
        if msg.startswith("held back"):
            left[0] += msg.count("; ") + 1

    watcher.log = heard
    try:
        for label, cfg in TRIGGER_SETS.items():
            row = {"wakes": 0, "max_day": 0, "cap_days": 0, "kinds": {}, "left": 0, "min_gap": None,
                   "stops": 0, "stop_delay": [], "warned": 0, "lead": [], "halfway_reached": 0, "halfway_told": 0,
                   "gave_back": 0, "gave_back_told": 0, "entries_reached": 0, "entries_told": 0, "events": 0}
            for w in worlds:
                state, sent = {}, []
                series, memory = w["series"], w["memory"]
                left[0] = 0
                for i in range(390):
                    now = w["open"] + timedelta(minutes=i)

                    def quote(t, i=i, series=series):
                        return {"symbol": t, "price": float(series[t][i]), "day_change": float(series[t][i] / series[t][0] - 1)}

                    text = watcher.tick(state, now, quote=quote, crypto_quote=lambda t: None, news=None,
                                        memory=memory, post=lambda u, t, b: None, push=lambda *a: None,
                                        ranges=w["ranges"] if cfg["ranges"] else None,
                                        events=w["events"] if cfg["wider"] else None, kinds=cfg["kinds"])
                    if text:
                        sent.append((i, [x[2:] for x in text.splitlines()[1:-1]]))
                row["wakes"] += len(sent)
                row["max_day"] = max(row["max_day"], len(sent))
                row["cap_days"] += len(sent) >= watcher.DAILY_CAP
                row["left"] += left[0]
                gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])
                        if not any(e.startswith(watcher.URGENT) for e in b[1])]
                if gaps:
                    row["min_gap"] = min(gaps) if row["min_gap"] is None else min(row["min_gap"], min(gaps))
                for _i, evs in sent:
                    for e in evs:
                        row["kinds"][e.split(":")[0]] = row["kinds"].get(e.split(":")[0], 0) + 1
                row["events"] += sum(e.startswith("EVENT") for _i, evs in sent for e in evs)

                def first(i_from, ticker, prefixes, sent=sent):
                    return next((i for i, evs in sent if i >= i_from
                                 and any(e.startswith(prefixes) and f": {ticker} " in e for e in evs)), None)

                for lot in memory["open"]:
                    t, px = lot["ticker"], series[lot["ticker"]]
                    touch = next((i for i in range(390) if px[i] <= lot["stop"]), None)
                    if touch is not None:
                        row["stops"] += 1
                        told = first(touch, t, ("STOP",))
                        if told is not None:
                            row["stop_delay"].append(told - touch)
                        early = next((i for i, evs in sent if i < touch
                                      and any(e.startswith(("NEAR", "MOVE", "DROP")) and f": {t} " in e for e in evs)), None)
                        if early is not None:
                            row["warned"] += 1
                            row["lead"].append(touch - early)
                    half = lot["entry"] + 0.5 * (lot["target"] - lot["entry"])
                    reached = next((i for i in range(390) if half <= px[i] < lot["target"]), None)
                    if reached is not None and px[0] < half:
                        row["halfway_reached"] += 1
                        told = first(reached, t, ("HALFWAY",))
                        row["halfway_told"] += told is not None
                        back = next((i for i in range(reached, 390) if px[i] <= lot["entry"]), None)
                        if back is not None:
                            row["gave_back"] += 1
                            row["gave_back_told"] += told is not None and told < back
                for idea in memory["idea_watch"]:
                    px = series[idea["ticker"]]
                    hit = next((i for i in range(390) if idea["entry"] <= px[i] <= idea["entry"] * (1 + watcher.ENTRY_CHASE)), None)
                    row["entries_reached"] += hit is not None
                    row["entries_told"] += hit is not None and first(hit, idea["ticker"], ("ENTRY",)) is not None
            out["sets"][label] = row
    finally:
        watcher.log = quiet
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out


def trigger_report(out: dict) -> list[str]:
    days = out["days"]
    lines = [f"{days} simulated sessions ({', '.join(f'{v} {k}' for k, v in out['day_kinds'].items())}), four holdings "
             "from calm to wild, two watched ideas; every set sees the same prices. Nobody trades here."]
    for label, r in out["sets"].items():
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(r["kinds"].items(), key=lambda kv: -kv[1]))
        delay = f"{np.median(r['stop_delay']):.0f} min (worst {max(r['stop_delay'])})" if r["stop_delay"] else "n/a"
        lead = f"{np.median(r['lead']):.0f} min" if r["lead"] else "n/a"
        lines += [f"{label}: {r['wakes'] / days:.1f} wake-ups a day (most in a day {r['max_day']}, days at the cap "
                  f"{r['cap_days']}); shortest gap between two ordinary wake-ups "
                  f"{r['min_gap'] if r['min_gap'] is not None else 'n/a'} min; {r['left'] / days:.1f} events a day left "
                  "to a scheduled run",
                  f"    reasons given: {kinds}",
                  f"    stops touched {r['stops']}: STOP wake-up after {delay}; warned beforehand {r['warned']} "
                  f"({r['warned'] / r['stops']:.0%}), typically {lead} ahead" if r["stops"] else "    no stop touched",
                  f"    halfway to target reached {r['halfway_reached']}, said {r['halfway_told']}; of {r['gave_back']} "
                  f"that then fell back to the entry, {r['gave_back_told']} had been told to raise the stop first"]
        if r["entries_reached"] and "ENTRY" in TRIGGER_SETS[label]["kinds"]:
            lines.append(f"    ideas that reached their entry {r['entries_reached']}, trader told {r['entries_told']}; "
                         f"pre-event reviews {r['events']}")
    return lines


# --- the report ----------------------------------------------------------------------

def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def run(seeds: int = 5, worlds: list[str] | None = None, agents: list[str] | None = None,
        days: int | None = None, workers: int | None = None) -> dict:
    worlds = worlds or list(WORLDS)
    agents = agents or list(AGENTS)
    jobs = [(w, seed, tuple(agents), days) for w in worlds for seed in range(seeds)]
    results = []
    if workers and workers > 1 and len(jobs) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            for batch in pool.map(_job, jobs):
                results += batch
    else:
        for job in jobs:
            results += _job(job)
    return {"results": results, "seeds": seeds, "worlds": worlds, "agents": agents}


def _job(job) -> list[Result]:
    w, seed, agents, days = job
    mkt = build_market(w, seed)
    return [simulate(w, seed, AGENTS[a], mkt=mkt, days=days) for a in agents]


def _paired(rs: list, world: str, agent: str, base: str, attr: str) -> float | None:
    """Median over seeds of (agent - base) on the same market."""
    a = {r.seed: getattr(r, attr) for r in rs if r.world == world and r.agent == agent}
    b = {r.seed: getattr(r, attr) for r in rs if r.world == world and r.agent == base}
    diffs = [a[k] - b[k] for k in a if k in b]
    return float(np.median(diffs)) if diffs else None


def _med(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(np.median(xs)) if xs else None


def report(out: dict) -> str:
    rs, worlds, agents = out["results"], out["worlds"], out["agents"]
    lines = ["STOCKSAGE SCENARIO LAB",
             f"{out['seeds']} random markets per world, one year each; every agent trades the SAME markets.",
             "A rule-following stand-in, not Claude: it shows what the rules and the memory do, assuming",
             "the setups are recognised. Planted edges are assumptions (scenarios.WORLDS). Medians over markets.", ""]
    lines.append(f"{'world':<8}{'agent':<15}{'return':>8}{'SPY':>8}{'worst DD':>10}{'trades':>8}{'win':>6}{'avg R':>7}")
    for w in worlds:
        for a in agents:
            g = [r for r in rs if r.world == w and r.agent == a]
            if not g or a not in CORE_AGENTS:
                continue
            lines.append(f"{w:<8}{a:<15}{_pct(_med([r.ret for r in g])):>8}{_pct(_med([r.spy for r in g])):>8}"
                         f"{_pct(min(r.max_dd for r in g)):>10}{_med([r.trades for r in g]) or 0:>8.0f}"
                         f"{(_med([r.win_rate for r in g]) or 0) * 100:>5.0f}%{_med([r.avg_r for r in g]) or 0:>+7.2f}")
        lines.append("")
    if "park_only" in agents and "system" in agents:
        lines.append("WHAT TRADING ADDS over just parking the cash (system minus park_only, same markets)")
        for w in worlds:
            lines.append(f"  {w:<8} return {_pct(_paired(rs, w, 'system', 'park_only', 'ret'))}, "
                         f"worst drawdown {_pct(_paired(rs, w, 'system', 'park_only', 'max_dd'))}")
        lines.append("")
    others = [a for a in agents if a not in ("system", "park_only")]
    if "system" in agents and others:
        lines.append("WHAT EACH RULE IS WORTH: (variant minus system) on the same markets; + return / + drawdown")
        lines.append("means the variant did better than the live rules")
        lines.append(f"  {'variant':<16}" + "".join(f"{w:>15}" for w in worlds))
        for a in others:
            cells = []
            for w in worlds:
                r_, d_ = _paired(rs, w, a, "system", "ret"), _paired(rs, w, a, "system", "max_dd")
                cells.append(f"{_pct(r_):>7}/{_pct(d_):>7}" if r_ is not None else f"{'':>15}")
            lines.append(f"  {a:<16}" + "".join(f"{c:>15}" for c in cells))
        lines.append("  (each cell: return change / worst-drawdown change)")
        lines.append("")
    for kind in ("trades", "ideas"):
        q = verdict_quality(rs, kind)
        if not q or not q[0]["cells"]:
            continue
        lines.append(f"HOW RIGHT THE MEMORY'S VERDICTS ARE ({kind}, system agent, every setup in every market)")
        for row in q:
            lines.append(f"  t >= {row['bar']:.1f}: right {row['right']}, wrong {row['wrong']}, undecided "
                         f"{row['undecided']} of {row['cells']}; wrong share {_pct(row['wrong_share'])}; "
                         f"real edges found {_pct(row['found'])}")
        lines.append("")
    blocked = {k: sum(r.blocked[k] for r in rs if r.agent == "system") for k in ("limit", "revenge", "cooloff", "brake")}
    lines += ["GUARDRAILS (system agent, all markets): buys stopped or cut by",
              "  " + ", ".join(f"{k} {v}" for k, v in blocked.items()), ""]
    lines += ["HOW MANY CLOSED TRADES TO PROVE AN EDGE (t >= 2)"]
    for edge, sd in ((0.02, 0.04), (0.01, 0.04), (0.005, 0.03), (0.003, 0.03)):
        lines.append(f"  {edge:.1%} a trade with {sd:.0%} noise: {trades_to_prove(edge, sd)} trades")
    lines += ["", "WHAT THE RULES MAKE OF EACH SITUATION (confidence level, score)"]
    for label, level, why in context_table():
        lines.append(f"  {label:<62} {level:<11} {why}")
    lines += ["", "THE WATCHER ON A SELLOFF (two holdings, minute by minute)"]
    lines += ["  " + x for x in watcher_rehearsal()] or ["  (no wake-ups)"]
    lines += ["", "THE WATCHER'S TRIGGER SETS, OLD AGAINST NEW"]
    lines += ["  " + x for x in trigger_report(trigger_rehearsal())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="scenarios")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--world", action="append")
    p.add_argument("--agent", action="append", help="default: all; " + ", ".join(AGENTS))
    p.add_argument("--watcher", action="store_true", help="only the watcher's trigger sets, old against new (seconds)")
    p.add_argument("--days", type=int, default=60, help="sessions for --watcher")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    import os

    if a.watcher:
        print("THE WATCHER'S TRIGGER SETS, OLD AGAINST NEW")
        print("\n".join("  " + x for x in trigger_report(trigger_rehearsal(days=a.days))))
        return 0

    print(f"Running {a.seeds} markets per world; this takes a few minutes...", flush=True)
    print(report(run(a.seeds, a.world, a.agent, workers=max(1, (os.cpu_count() or 2) - 1))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
