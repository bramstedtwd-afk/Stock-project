"""Congress lab: do senators' stock purchases, once disclosed, beat the market?

Members of Congress must disclose their trades (STOCK Act) within 45 days, and
the claim that they beat the market is widely repeated. The only thing a member
of the public can act on is the DISCLOSURE, so that is what this tests.

Data: the US Senate's electronic disclosures (efdsearch.senate.gov) as collected
by the public Senate Stock Watcher data set, one file per filing day,
2014-2020 (the collection stopped in early 2021). House filings are PDFs with
no free machine-readable history, so they are not here.

Honesty details, the same as the insider lab:
  * The signal date is the day the filing was RECEIVED (public), never the trade
    date. The position is bought at the close of the session after the first
    session following the filing.
  * Today's S&P 500 companies only, each counted only after it joined, against an
    EQUAL-WEIGHT "own every company" control, so survivorship is not mistaken for
    insight (senators also bought small and since-delisted companies, which no
    free price source covers without flattering the result).
  * Every rule is fixed before looking, judged with White's Reality Check across
    all of them, development years first; the later years are spent only on
    survivors.

Network lives in `load_trades`; the rest is pure.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_ZIP = "https://codeload.github.com/timothycarambat/senate-stock-watcher-data/zip/refs/heads/master"
START = "2014-03-01"
SPLIT = "2018-01-01"
END = "2021-09-30"            # the collection stops in early 2021; the longest hold runs out by here
SELF_OWNERS = ("self", "joint")


def _amount(text: str | None) -> float:
    """'$15,001 - $50,000' -> 32500.5 (midpoint of the disclosed range); 0 when unreadable."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text or "")]
    return sum(nums) / len(nums) if nums else 0.0


def parse_reports(reports: list[dict]) -> list[dict]:
    """One filing day's reports -> one row per disclosed stock PURCHASE with a ticker."""
    rows = []
    for rep in reports or []:
        try:
            filed = datetime.strptime(rep.get("date_recieved") or rep.get("date_received") or "", "%m/%d/%Y")
        except ValueError:
            continue
        senator = " ".join(x for x in (rep.get("first_name"), rep.get("last_name")) if x).strip().title()
        who = rep.get("bioguide") or senator
        for t in rep.get("transactions") or []:
            if "purchase" not in (t.get("type") or "").lower():
                continue
            if "stock" not in (t.get("asset_type") or "stock").lower():
                continue
            raw = t.get("ticker") or ""
            raw = re.sub(r"<[^>]*>", "", raw)          # tickers are often stored as links: <a href=...>MSFT</a>
            ticker = re.sub(r"[^A-Z.\-]", "", raw.upper()).replace(".", "-")
            if not ticker or ticker in ("-", "--"):
                continue
            try:
                traded = datetime.strptime(t.get("transaction_date") or "", "%m/%d/%Y")
            except ValueError:
                traded = None
            rows.append({"filed": pd.Timestamp(filed), "traded": pd.Timestamp(traded) if traded else pd.NaT,
                         "ticker": ticker, "senator": who, "name": senator,
                         "owner": (t.get("owner") or "").strip().lower(), "value": _amount(t.get("amount"))})
    return rows


def load_trades(cache_path: Path | None = None, source: str | Path | None = None, get=None) -> pd.DataFrame:
    """Every disclosed senator stock purchase, 2014-2020 (cached after the first download).
    `source` may be a folder holding the data set's `data/` files instead of the download."""
    if cache_path is not None and Path(cache_path).exists() and source is None:
        return pd.read_parquet(cache_path)
    rows: list[dict] = []
    if source is not None:
        for f in sorted(Path(source).glob("data/transaction_report_for_*.json")):
            rows += parse_reports(json.loads(f.read_text(encoding="utf-8")))
    else:
        if get is None:
            import requests

            def get(url):
                r = requests.get(url, timeout=120, headers={"User-Agent": "StockSage research"})
                r.raise_for_status()
                return r.content
        with zipfile.ZipFile(io.BytesIO(get(DATA_ZIP))) as z:
            for name in sorted(z.namelist()):
                if re.search(r"/data/transaction_report_for_[\d_]+\.json$", name):
                    rows += parse_reports(json.loads(z.read(name)))
    out = pd.DataFrame(rows, columns=["filed", "traded", "ticker", "senator", "name", "owner", "value"])
    out = out.drop_duplicates().sort_values("filed").reset_index(drop=True)
    if cache_path is not None:
        out.to_parquet(cache_path)
    return out


def build_events(buys: pd.DataFrame, universe: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """One row per (company, filing day): how many senators bought, how much, whether any
    bought for their own account, distinct senators over 30 days, and whether the senator
    had bought it before. `day` is the first session AFTER the filing day."""
    days = prices.index
    info = universe.set_index("ticker")
    b = buys[buys["ticker"].isin(info.index) & buys["ticker"].isin(prices.columns)].copy()
    added = pd.to_datetime(info["added"], errors="coerce")
    b = b[b["filed"] >= b["ticker"].map(added).fillna(pd.Timestamp("1900-01-01"))]
    b["own"] = b["owner"].isin(SELF_OWNERS)
    b = b.sort_values("filed")
    b["repeat"] = b.duplicated(["senator", "ticker"], keep="first")
    g = b.groupby(["ticker", "filed"]).agg(senators=("senator", "nunique"), value=("value", "sum"),
                                           own=("own", "any"), repeat=("repeat", "any"),
                                           lag=("traded", lambda s: (s.max() if s.notna().any() else pd.NaT)))
    g = g.reset_index().sort_values(["ticker", "filed"])
    rows = []
    for t, grp in g.groupby("ticker"):
        mine = b[b["ticker"] == t][["filed", "senator"]]
        for r in grp.itertuples(index=False):
            recent = mine[(mine["filed"] <= r.filed) & (mine["filed"] > r.filed - pd.Timedelta(days=30))]
            i = int(days.searchsorted(r.filed, side="right"))
            if i >= len(days):
                continue
            rows.append({"ticker": t, "date": days[i], "day": i, "sector": info.at[t, "sector"],
                         "senators": r.senators, "value": r.value, "own": bool(r.own), "repeat": bool(r.repeat),
                         "senators_30d": recent["senator"].nunique(),
                         "lag_days": (r.filed - r.lag).days if pd.notna(r.lag) else None})
    cols = ["ticker", "date", "day", "sector", "senators", "value", "own", "repeat", "senators_30d", "lag_days"]
    return pd.DataFrame(rows, columns=cols).sort_values("date").reset_index(drop=True)


def signals():
    from .earnings import Signal

    specs = (("any senator purchase", lambda e: pd.Series(True, index=e.index)),
             ("purchases worth $15k+", lambda e: e["value"] >= 15_000),
             ("purchases worth $50k+", lambda e: e["value"] >= 50_000),
             ("2+ senators buying within 30 days", lambda e: e["senators_30d"] >= 2),
             ("bought for the senator's own account", lambda e: e["own"]),
             ("a senator buying a stock again", lambda e: e["repeat"]),
             ("disclosed within 15 days of the trade", lambda e: e["lag_days"].fillna(999) <= 15))
    return [Signal(f"{name}, held {h} days", "all", h, fn) for h in (20, 60, 120) for name, fn in specs]


def run(ev: pd.DataFrame, prices: pd.DataFrame, cash: pd.Series, universe: pd.DataFrame, draws: int = 2000,
        split: str = SPLIT, start: str = START, end: str = END) -> dict:
    """Judge every rule (luck-corrected, later years untouched) against SPY, and report the
    'own every company equally' control over the same days."""
    from . import earnings
    from .insiders import equal_weight_control

    prices = prices.loc[:end]
    ev = ev[ev["day"] < len(prices.index)].reset_index(drop=True)
    res = earnings.run(ev, prices, cash, draws=draws, split=split, start=start, sigs=signals())
    res["control"] = equal_weight_control(prices, universe, cash, start, split)
    res["drift"] = {}
    lags = ev["lag_days"].dropna()
    res["lag"] = {"median": float(lags.median()) if len(lags) else None,
                  "p90": float(lags.quantile(0.9)) if len(lags) else None}
    return res


def describe(res: dict, top: int = 21) -> list[str]:
    from .drivers import holdout_bar
    from .earnings import _pct, strong

    c, lag = res["control"], res.get("lag") or {}
    out = ["CONGRESS: DO SENATORS' DISCLOSED STOCK PURCHASES BEAT THE MARKET?", "=" * 80,
           f"{res['events']:,} filing days of senator purchases at today's S&P 500 companies (after each joined), "
           f"{res['start']} to {res['end']}; bought the session after each filing became public.",
           (f"Filings came a median {lag['median']:.0f} days after the trade (90% within {lag['p90']:.0f}): "
            "by then the move, if any, has had weeks to happen." if lag.get("median") is not None else ""),
           f"Control, owning every company equally: alpha {_pct(c['before'][0])} (t {c['before'][1]:+.1f}) before "
           f"{res['split'][:4]}, {_pct(c['after'][0])} (t {c['after'][1]:+.1f}) after. A rule must do clearly better.",
           f"Later-years bar: alpha t above {holdout_bar(res['rows']):.2f} AND Sharpe at least SPY's "
           f"({res['bench']['holdout']['sharpe']:.2f}).",
           f"{'  rule (best first, development years)':<62}{'alpha':>8}{'t':>6}{'p':>6}{'events':>8}"]
    for r in res["rows"][:top]:
        out.append(f"  {r.name[:59]:<60}{_pct(r.alpha_dev):>8}{r.t_dev:>6.1f}{r.p_dev:>6.2f}{r.events:>8,}")
    surv = [r for r in res["rows"] if r.passed]
    if not surv:
        out.append("  None passed the development test, so the later years were never touched.")
    for r in surv:
        h = r.holdout
        out.append(f"  PASSED: {r.name[:56]}: later alpha {_pct(r.alpha_holdout)} (t {r.t_holdout:.1f}), "
                   f"Sharpe {h['sharpe']:.2f}, CAGR {_pct(h['cagr'])}")
    win = strong(res["rows"])
    out += ["", "RESULT: " + ("; ".join(w.name for w in win) + " cleared every bar. Paper-trade it first (IDEA lines, "
                              "setup word `congress`)." if win else "nothing cleared every bar.")]
    return [x for x in out if x]
