"""Why the market moves, and whether knowing why helps.

Two different questions, kept apart on purpose:

  EXPLAIN  Which forces moved the S&P 500 in a given month? Regress monthly
           returns on the SAME month's changes in rates, credit spreads, oil,
           the dollar, volatility, jobless claims, consumer sentiment and
           news-based uncertainty, then attribute each month's move to them.
           This is hindsight: it says why, it does not say what is next.

  PREDICT  Does a force that was KNOWN BEFOREHAND tell you anything about the
           next stretch? Each one is lagged by its real publication delay (a
           month's unemployment rate is not known until the next month), then
           tested as a gate on two real return streams: the S&P 500 and Cboe's
           real put-write index (the lowest-risk options strategy the options lab
           measured). Judged with the same luck correction and holdout as every
           other lab here, against a bar built for LOWER RISK: better Sharpe,
           smaller worst drop, and alpha that survives the correction.

"News" here means the newspaper-based Economic Policy Uncertainty and Equity
Market Uncertainty indexes (Baker, Bloom and Davis; daily since 1985), which
count uncertainty words in US newspapers. Archives of individual headlines
going back decades are not freely available, so these indexes are the news.

Data: FRED (St. Louis Fed), free, no key. Monthly economic series are revised
after release and FRED serves the latest revision, so their tests are slightly
optimistic; the daily market and news series are not revised.

Pure maths except `load_fred`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"


@dataclass(frozen=True)
class Series:
    sid: str
    name: str
    lag_days: int          # calendar days from the observation date until it is public
    kind: str              # "level" (look at its level) or "growth" (look at its % change)
    risk_up: bool          # True if a HIGHER reading is the textbook risk-off sign


SERIES = [
    # News-based (daily newspaper counts)
    Series("USEPUINDXD", "news: economic policy uncertainty", 1, "level", True),
    Series("WLEMUINDXD", "news: equity market uncertainty", 1, "level", True),
    # Markets (daily)
    Series("BAA10Y", "credit spread (Baa corporate minus 10-year)", 1, "level", True),
    Series("T10Y3M", "yield curve (10-year minus 3-month)", 1, "level", False),
    Series("T10Y2Y", "yield curve (10-year minus 2-year)", 1, "level", False),
    Series("DGS10", "10-year Treasury yield", 1, "level", True),
    Series("DCOILWTICO", "oil price", 1, "growth", True),
    Series("DFF", "Fed funds rate", 1, "level", True),
    # Weekly
    Series("NFCI", "financial conditions (Chicago Fed)", 7, "level", True),
    Series("ICSA", "initial jobless claims", 6, "growth", True),
    # Monthly economy and the consumer (dated the 1st of the month they describe)
    Series("UMCSENT", "consumer sentiment (Michigan)", 31, "level", False),
    Series("UNRATE", "unemployment rate", 38, "level", True),
    Series("CPIAUCSL", "inflation (CPI)", 45, "growth", True),
    Series("RSAFS", "retail sales", 45, "growth", False),
    Series("INDPRO", "industrial production", 47, "growth", False),
    Series("PERMIT", "building permits", 48, "growth", False),
    Series("M2SL", "money supply (M2)", 55, "growth", False),
]
BY_ID = {s.sid: s for s in SERIES}
START = "1997-01-01"
SPLIT = "2012-01-01"
PASS_P = 0.10


# --- data -----------------------------------------------------------------------


def load_fred(cache_path: Path | None = None, max_age_hours: float = 12.0, fetch=None,
              ids: list[str] | None = None) -> pd.DataFrame:
    """Raw FRED observations, one column per series, indexed by OBSERVATION date."""
    import io
    import time
    import urllib.request

    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_hours * 3600:
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                pass

    def get(url):
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    fetch = fetch or get
    cols = {}
    for sid in ids or [s.sid for s in SERIES]:
        try:
            raw = pd.read_csv(io.StringIO(fetch(FRED_URL.format(sid=sid))))
        except Exception:
            continue
        date_col = raw.columns[0]
        val = pd.to_numeric(raw[raw.columns[1]], errors="coerce")
        cols[sid] = pd.Series(val.to_numpy(), index=pd.to_datetime(raw[date_col], errors="coerce")).dropna()
    if not cols:
        raise RuntimeError("could not download any FRED series")
    out = pd.DataFrame(cols).sort_index()
    if cache_path is not None:
        try:
            out.to_parquet(cache_path)
        except Exception:
            pass
    return out


ALFRED_URL = "https://alfred.stlouisfed.org/graph/alfredgraph.csv?id={ids}&vintage_date={dates}"
REALTIME_IDS = ["UMCSENT", "UNRATE", "CPIAUCSL", "RSAFS", "INDPRO", "PERMIT", "M2SL", "ICSA", "NFCI"]
ALFRED_BATCH = 12          # ALFRED returns at most this many vintages per request


def load_realtime(cache_path: Path | None = None, max_age_days: float = 7.0, fetch=None,
                  ids: list[str] | None = None, start: str = "1996-01-01", end: str | None = None) -> pd.DataFrame:
    """Revised series AS THEY WERE PUBLISHED, from ALFRED (FRED's archive of past
    releases): on each 1st and 15th, the latest value then known and its change
    over the previous three months then known. Columns `SID` and `SID__g`,
    indexed by the date the figures were public."""
    import io
    import time
    import urllib.request

    if cache_path is not None and Path(cache_path).exists():
        if time.time() - Path(cache_path).stat().st_mtime < max_age_days * 86400:
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                pass

    def get(url):
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")

    fetch = fetch or get
    vintages = pd.date_range(start, end or pd.Timestamp.today().normalize(), freq="SMS")
    rows: dict[pd.Timestamp, dict] = {}
    for sid in ids or REALTIME_IDS:
        back = 13 if BY_ID[sid].lag_days < 15 else 3          # weekly: 13 weeks; monthly: 3 months
        for k in range(0, len(vintages), ALFRED_BATCH):
            batch = vintages[k:k + ALFRED_BATCH]
            url = ALFRED_URL.format(ids=",".join([sid] * len(batch)),
                                    dates=",".join(d.strftime("%Y-%m-%d") for d in batch))
            try:
                frame = pd.read_csv(io.StringIO(fetch(url)))
            except Exception:
                continue
            for col in frame.columns[1:]:
                vals = pd.to_numeric(frame[col], errors="coerce").dropna()
                if len(vals) <= back:
                    continue
                when = pd.Timestamp(col.rsplit("_", 1)[-1])
                row = rows.setdefault(when, {})
                row[sid] = float(vals.iloc[-1])
                row[f"{sid}__g"] = float(vals.iloc[-1] / vals.iloc[-1 - back] - 1.0) if vals.iloc[-1 - back] else np.nan
    if not rows:
        raise RuntimeError("could not download any ALFRED vintages")
    out = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    if cache_path is not None:
        try:
            out.to_parquet(cache_path)
        except Exception:
            pass
    return out


def readings(raw: pd.DataFrame, calendar: pd.DatetimeIndex, realtime: pd.DataFrame | None = None) -> pd.DataFrame:
    """The number each rule looks at, as it could have been known on each day:
    the level for 'level' series, the 3-month change for 'growth' series. Revised
    series come from `realtime` (as first published) whenever it is given."""
    known = as_known(raw, calendar)
    out = {}
    for sid, meta in BY_ID.items():
        if realtime is not None and sid in realtime:
            col = realtime[f"{sid}__g"] if meta.kind == "growth" else realtime[sid]
            col = col.dropna()
            col.index = col.index + pd.Timedelta(days=1)       # public on the vintage date; usable the day after
            out[sid] = col.reindex(col.index.union(calendar)).ffill().reindex(calendar)
        elif sid in known:
            x = known[sid]
            out[sid] = x / x.shift(63) - 1.0 if meta.kind == "growth" else x
    return pd.DataFrame(out, index=calendar)


def as_known(raw: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Each series as it was KNOWN on each trading day: an observation becomes
    usable only `lag_days` after its date, and the latest usable value carries."""
    out = {}
    for sid in raw:
        meta = BY_ID.get(sid)
        s = raw[sid].dropna()
        if meta is None or s.empty:
            continue
        s.index = s.index + pd.Timedelta(days=meta.lag_days)
        s = s[~s.index.duplicated(keep="last")]
        out[sid] = s.reindex(s.index.union(calendar)).ffill().reindex(calendar)
    return pd.DataFrame(out, index=calendar)


def feature(reading: pd.Series, window: int = 252 * 10) -> pd.Series:
    """Where a reading sits against its own trailing history: a percentile in [0, 1]
    using only past days."""
    return reading.rolling(window, min_periods=252 * 3).rank(pct=True)


# --- EXPLAIN: what moved each month ------------------------------------------------------


EXPLAIN_IDS = ["DGS10", "BAA10Y", "DCOILWTICO", "USEPUINDXD", "ICSA", "UMCSENT", "T10Y3M"]


def _month_changes(raw: pd.DataFrame, vix: pd.Series | None) -> pd.DataFrame:
    """Same-month changes of each force (hindsight, for explanation only)."""
    m = raw.resample("ME").last()
    ch = pd.DataFrame(index=m.index)
    for sid in EXPLAIN_IDS:
        if sid not in m:
            continue
        meta = BY_ID[sid]
        if meta.kind == "growth" or sid in ("ICSA", "USEPUINDXD"):
            ch[meta.name] = np.log(m[sid] / m[sid].shift(1))
        else:
            ch[meta.name] = m[sid].diff()
    if vix is not None:
        ch["volatility (VIX)"] = vix.resample("ME").last().diff()
    return ch


@dataclass
class Explanation:
    r2: float
    months: int
    betas: dict
    attribution: pd.DataFrame        # month x force contribution (in return units)
    residual: pd.Series

    def month_story(self, month: pd.Timestamp, actual: float, top: int = 3) -> str:
        row = self.attribution.loc[month].dropna()
        row = row.reindex(row.abs().sort_values(ascending=False).index)[:top]
        parts = [f"{name} {'+' if v >= 0 else ''}{v * 100:.1f}%" for name, v in row.items()]
        rest = self.residual.get(month, float("nan"))
        return (f"{month:%b %Y}: S&P {actual * 100:+.1f}%. Biggest pushes: " + ", ".join(parts)
                + (f"; unexplained {rest * 100:+.1f}%" if not math.isnan(rest) else "") + ".")


def explain(raw: pd.DataFrame, spx_tr: pd.Series, vix: pd.Series | None = None,
            start: str = START) -> Explanation:
    """How much of each month's S&P 500 move lines up with same-month moves in
    the forces, and which force did most in each month."""
    ret = spx_tr.resample("ME").last().pct_change()
    X = _month_changes(raw, vix).loc[start:]
    data = pd.concat([ret.rename("ret"), X], axis=1).loc[start:].dropna()
    y = data["ret"].to_numpy()
    cols = [c for c in data.columns if c != "ret"]
    Z = data[cols]
    mu, sd = Z.mean(), Z.std().replace(0, 1)
    Zs = ((Z - mu) / sd).to_numpy()
    A = np.column_stack([np.ones(len(y)), Zs])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    fit = A @ coef
    r2 = float(1 - ((y - fit) ** 2).sum() / ((y - y.mean()) ** 2).sum())
    contrib = pd.DataFrame(Zs * coef[1:], index=data.index, columns=cols)
    residual = pd.Series(y - fit, index=data.index)
    betas = {c: float(b) for c, b in zip(cols, coef[1:])}
    return Explanation(r2, len(y), betas, contrib, residual)


# --- PREDICT: does a force known in advance help? -------------------------------------------


@dataclass
class Rule:
    name: str
    base: str            # "S&P 500" or "put-write"
    signal: pd.Series    # True on days the rule is IN the base (decided with yesterday's data)
    off: str             # what it holds when out: "cash"


def rules(read: pd.DataFrame, vix: pd.Series | None = None) -> list[Rule]:
    """Every gate tried. The family is fixed before looking at results and all of
    it counts toward the luck correction. `read` comes from `readings`."""
    feats = {sid: feature(read[sid]) for sid in read if sid in BY_ID}
    if vix is not None:
        feats["VIX"] = vix.rolling(2520, min_periods=756).rank(pct=True)
    names = {sid: BY_ID[sid].name for sid in BY_ID}
    names["VIX"] = "VIX"
    risk_up = {sid: BY_ID[sid].risk_up for sid in BY_ID}
    risk_up["VIX"] = True
    out = []
    composite_parts = []
    for sid, f in feats.items():
        risky = f if risk_up[sid] else 1.0 - f        # 1 = the textbook danger end
        composite_parts.append(risky)
        for base in ("S&P 500", "put-write"):
            # Textbook direction: stand aside when the reading is in its riskiest 30%.
            calm = (risky < 0.7).shift(1)
            out.append(Rule(f"{base}, out when {names[sid]} is in its riskiest 30%", base,
                            calm.fillna(True).astype(bool), "cash"))
            # The opposite (contrarian) direction, also counted.
            contra = (risky > 0.3).shift(1)
            out.append(Rule(f"{base}, out when {names[sid]} is in its CALMEST 30%", base,
                            contra.fillna(True).astype(bool), "cash"))
    if composite_parts:
        comp = pd.concat(composite_parts, axis=1).mean(axis=1, skipna=True)
        comp_pct = comp.rolling(2520, min_periods=756).rank(pct=True)
        for cut in (0.7, 0.8, 0.9):
            for base in ("S&P 500", "put-write"):
                sig = (comp_pct < cut).shift(1).fillna(True).astype(bool)
                out.append(Rule(f"{base}, out when the combined risk score is in its top {int((1 - cut) * 100)}%",
                                base, sig, "cash"))
    return out


@dataclass
class Row:
    name: str
    base: str
    dev: dict
    full: dict
    alpha_dev: float
    t_dev: float
    p_dev: float
    p_full: float
    passed: bool
    time_in: float
    holdout: dict | None = None
    alpha_holdout: float | None = None
    t_holdout: float | None = None
    safer: bool | None = None        # holdout Sharpe above AND worst drop smaller than the S&P 500's

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def judge(streams: dict[str, pd.Series], rule_list: list[Rule], spx_tr: pd.Series, cash: pd.Series,
          split: str = SPLIT, draws: int = 2000, seed: int = 11, start: str = START) -> dict:
    """Run every rule over its base stream and judge them all together."""
    from .optionslab import _alpha, _metrics
    from .research import reality_check

    days = spx_tr.loc[start:].index[1:]
    tr = spx_tr.pct_change().reindex(days)
    c = cash.reindex(days).fillna(0.0)
    cols = {}
    meta = {}
    for rule in rule_list:
        base = streams[rule.base].reindex(days)
        on = rule.signal.reindex(days).fillna(True)
        cols[rule.name] = base.where(on, c).fillna(c)
        meta[rule.name] = (rule.base, float(on.mean()))
    rets = pd.DataFrame(cols)
    dev = days < pd.Timestamp(split)

    def alphas(mask):
        return np.column_stack([_alpha(rets.loc[mask, n], tr[mask], c[mask])[0].to_numpy() for n in rets])

    t_dev, p_dev = reality_check(alphas(dev), draws, seed=seed)
    _, p_full = reality_check(alphas(np.ones(len(days), bool)), draws, seed=seed)
    bench_hold = _metrics(tr[~dev], c[~dev])
    rows = []
    for k, name in enumerate(rets.columns):
        a_dev = _alpha(rets.loc[dev, name], tr[dev], c[dev])[0]
        passed = bool(a_dev.mean() > 0 and p_dev[k] < PASS_P)
        row = Row(name, meta[name][0], _metrics(rets.loc[dev, name], c[dev]), _metrics(rets[name], c),
                  float(a_dev.mean() * 252), float(t_dev[k]), float(p_dev[k]), float(p_full[k]), passed,
                  meta[name][1])
        if passed:
            h = rets.loc[~dev, name]
            a_h = _alpha(h, tr[~dev], c[~dev])[0]
            row.holdout = _metrics(h, c[~dev])
            row.alpha_holdout = float(a_h.mean() * 252)
            row.t_holdout = float(a_h.mean() / a_h.std() * math.sqrt(len(a_h))) if a_h.std() > 0 else 0.0
            row.safer = bool(row.holdout["sharpe"] > bench_hold["sharpe"]
                             and row.holdout["maxdd"] > bench_hold["maxdd"])
        rows.append(row)
    rows.sort(key=lambda r: r.p_dev)
    base_stats = {n: {"full": _metrics(s.reindex(days).fillna(0), c), "dev": _metrics(s.reindex(days)[dev].fillna(0), c[dev]),
                      "holdout": _metrics(s.reindex(days)[~dev].fillna(0), c[~dev])} for n, s in streams.items()}
    return {"rows": rows, "n": len(rets.columns), "start": str(days[0].date()), "end": str(days[-1].date()),
            "split": split, "bases": base_stats}


def holdout_bar(rows) -> float:
    """t a survivor needs in the untouched years. Checking several survivors there
    is itself several tries, so the bar rises with their number (one-sided 2.5%,
    Bonferroni): 1.96 for one, about 2.64 for six."""
    from statistics import NormalDist

    k = max(1, sum(1 for r in rows if r.passed))
    return NormalDist().inv_cdf(1 - 0.025 / k)


def strong(rows: list[Row]) -> list[Row]:
    """Pre-declared bar for a lower-risk rule that works: passes the corrected
    development test, then in untouched years clears `holdout_bar` AND has a
    better Sharpe AND a smaller worst drop than holding the S&P 500."""
    bar = holdout_bar(rows)
    return [r for r in rows if r.passed and r.t_holdout is not None and r.t_holdout > bar and r.safer]


# --- today's context (for the sheet) -------------------------------------------------------


def context_lines(raw: pd.DataFrame, today: pd.Timestamp | None = None, ids: list[str] | None = None) -> list[str]:
    """Where each force stands now against its last ten years. Context, never a call."""
    today = today or pd.Timestamp.today().normalize()
    cal = pd.bdate_range(today - pd.Timedelta(days=365 * 11), today)
    read = readings(raw, cal)
    out = []
    for sid in ids or ["USEPUINDXD", "BAA10Y", "T10Y3M", "NFCI", "ICSA", "UMCSENT", "UNRATE", "CPIAUCSL", "DCOILWTICO"]:
        if sid not in read or read[sid].dropna().empty:
            continue
        meta = BY_ID[sid]
        f = feature(read[sid]).dropna()
        if f.empty:
            continue
        pct = float(f.iloc[-1])
        risky = pct if meta.risk_up else 1 - pct
        mood = "riskier than usual" if risky >= 0.7 else ("calmer than usual" if risky <= 0.3 else "normal")
        val = read[sid].dropna().iloc[-1]
        shown = f"{val:,.2f}" if abs(val) < 1000 else f"{val:,.0f}"
        if meta.kind == "growth":
            shown = f"{val * 100:+.1f}% over 3 months"
        out.append(f"DRIVER  {meta.name}: {shown} ({pct * 100:.0f}th percentile of 10 years, {mood})")
    return out


# --- words -------------------------------------------------------------------------------


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{digits}f}%"


def describe(expl: Explanation | None, res: dict, spx_month: pd.Series | None = None, top: int = 15) -> list[str]:
    out = ["WHY THE MARKET MOVES, AND WHETHER KNOWING WHY HELPS", "=" * 80]
    if expl is not None:
        out += [f"PART 1. EXPLAIN ({expl.months} months): same-month moves in rates, credit, oil, news uncertainty,",
                f"jobless claims, sentiment and volatility line up with {expl.r2 * 100:.0f}% of the S&P 500's monthly moves.",
                "Hindsight: this says why a month went the way it did, not what comes next.",
                "Typical push of a normal-sized move in each force on that month's S&P 500 return:"]
        for name, b in sorted(expl.betas.items(), key=lambda kv: -abs(kv[1])):
            out.append(f"  {name:<44}{_pct(b)}")
        if spx_month is not None:
            out.append("Recent months:")
            for m in expl.attribution.index[-6:]:
                if m in spx_month.index:
                    out.append("  " + expl.month_story(m, float(spx_month[m])))
        out.append("")
    b = res["bases"]
    out += [f"PART 2. PREDICT: {res['n']} rules, each standing aside when a force KNOWN BEFOREHAND looked risky,",
            f"{res['start']} to {res['end']}, ranked on years before {res['split'][:4]}, later years untouched.",
            f"Holding the S&P 500 from {res['split'][:4]}: Sharpe {b['S&P 500']['holdout']['sharpe']:.2f}, "
            f"worst drop {_pct(b['S&P 500']['holdout']['maxdd'], 0)}. "
            f"Real put-write: Sharpe {b['put-write']['holdout']['sharpe']:.2f}, worst drop "
            f"{_pct(b['put-write']['holdout']['maxdd'], 0)}.",
            f"Bar: luck-corrected pass, then later-years alpha t above {holdout_bar(res['rows']):.2f} (rises with the number "
            "of survivors checked) with a better Sharpe AND a smaller worst drop.",
            f"{'  rule (best first, development years)':<74}{'alpha':>8}{'t':>6}{'p':>6}{'in':>6}"]
    for r in res["rows"][:top]:
        out.append(f"  {r.name[:71]:<72}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}{r.time_in * 100:>5.0f}%")
    survivors = [r for r in res["rows"] if r.passed]
    if not survivors:
        out.append("  None passed the development test, so the later years were never touched.")
    for r in survivors:
        h = r.holdout
        out.append(f"  PASSED: {r.name[:60]}: later alpha {_pct(r.alpha_holdout)} (t {r.t_holdout:.1f}), "
                   f"Sharpe {h['sharpe']:.2f}, worst drop {_pct(h['maxdd'], 0)}, "
                   + ("SAFER than the S&P 500" if r.safer else "not safer than the S&P 500"))
    win = strong(res["rows"])
    out.append("")
    out.append("RESULT: " + ("; ".join(w.name for w in win) + " cleared every bar. Paper-trade it first."
                             if win else "nothing cleared every bar. Knowing why the market moved did not, on this "
                             "evidence, tell you early enough to step aside profitably."))
    return out
