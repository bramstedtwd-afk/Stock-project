"""Insider buying: do companies' own officers and directors know something?

When an executive or director buys their company's shares in the open market
with their own money, they must report it to the SEC (Form 4) within two
business days. Research has long found these purchases, especially several
insiders buying together or the CEO/CFO buying, are followed by above-market
returns. This tests that on S&P 500 companies since 2006 with the SEC's own
quarterly "Insider Transactions Data Sets".

Honesty details:
  * Only OPEN-MARKET PURCHASES (transaction code P). Option exercises, grants
    and sales are ignored.
  * The signal date is the FILING date (when it became public), not the trade
    date. The position is bought at the close of the session after the first
    session following the filing.
  * Same universe care as the earnings lab: today's S&P 500, filings counted
    only after a company joined, and an EQUAL-WEIGHT "own every company" control
    so survivorship is not mistaken for insight.

Network lives in `load_purchases`; the rest is pure.
"""

from __future__ import annotations

import io
import math
import time
import zipfile
from pathlib import Path

import pandas as pd

SEC_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
CONTACT_VAR = "STOCKSAGE_SEC_CONTACT"   # the SEC requires a contact email in every request (.env only)
FIRST_QUARTER = "2006q1"
CHIEFS = ("CEO", "CFO", "CHIEF EXECUTIVE", "CHIEF FINANCIAL", "PRESIDENT")


def quarters(first: str = FIRST_QUARTER, last: pd.Timestamp | None = None) -> list[str]:
    last = last or pd.Timestamp.today()
    y, q = int(first[:4]), int(first[-1])
    out = []
    while (y, q) <= (last.year, (last.month - 1) // 3 + 1):
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def parse_quarter(blob: bytes) -> pd.DataFrame:
    """Open-market purchases in one quarterly zip: one row per transaction."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        def read(name, cols):
            with z.open(name) as f:
                return pd.read_csv(f, sep="\t", usecols=lambda c: c in cols, dtype=str, quoting=3,
                                   on_bad_lines="skip")
        sub = read("SUBMISSION.tsv", {"ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"})
        own = read("REPORTINGOWNER.tsv", {"ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP",
                                          "RPTOWNER_TITLE"})
        trx = read("NONDERIV_TRANS.tsv", {"ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES",
                                          "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"})
    trx = trx[(trx["TRANS_CODE"] == "P") & (trx["TRANS_ACQUIRED_DISP_CD"].fillna("A") == "A")].copy()
    if trx.empty:
        return pd.DataFrame(columns=["ticker", "filed", "owner", "relationship", "title", "value"])
    trx["value"] = pd.to_numeric(trx["TRANS_SHARES"], errors="coerce") * \
        pd.to_numeric(trx["TRANS_PRICEPERSHARE"], errors="coerce")
    own = own.drop_duplicates("ACCESSION_NUMBER")
    m = trx.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    m = m[m["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    out = pd.DataFrame({
        "ticker": m["ISSUERTRADINGSYMBOL"].str.upper().str.strip().str.replace(".", "-", regex=False),
        "filed": pd.to_datetime(m["FILING_DATE"], format="%d-%b-%Y", errors="coerce"),
        "owner": m["RPTOWNERCIK"], "relationship": m["RPTOWNER_RELATIONSHIP"].fillna(""),
        "title": m["RPTOWNER_TITLE"].fillna("").str.upper(), "value": m["value"],
    })
    return out.dropna(subset=["ticker", "filed"])


def load_purchases(cache_path: Path | None = None, get=None, pause: float = 0.2,
                   quarter_list: list[str] | None = None) -> pd.DataFrame:
    """Every open-market insider purchase since 2006 (resumable, cached)."""
    done = pd.read_parquet(cache_path) if cache_path is not None and Path(cache_path).exists() else None
    have = set(done["quarter"]) if done is not None and len(done) else set()
    if get is None:
        import os
        import urllib.request

        contact = (os.environ.get(CONTACT_VAR) or "").strip()
        if "@" not in contact:
            raise RuntimeError("the SEC asks for a contact email with each download. Run  "
                               ".\\start.bat insiders --contact you@example.com  once to save yours.")
        agent = f"StockSage personal research {contact}"

        def get(url):
            req = urllib.request.Request(url, headers={"User-Agent": agent})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
    frames = [done] if done is not None and len(done) else []
    qs = quarter_list or quarters()
    for q in qs:
        if q in have:
            continue
        try:
            part = parse_quarter(get(SEC_URL.format(q=q)))
        except Exception:
            continue                                   # not published yet, or a bad file
        part["quarter"] = q
        frames.append(part)
        if cache_path is not None:
            pd.concat(frames, ignore_index=True).to_parquet(cache_path)
        time.sleep(pause)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --- events ---------------------------------------------------------------------------


def build_events(buys: pd.DataFrame, universe: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """One row per (company, filing day): how many insiders bought, how much, and
    whether a chief officer was among them, plus distinct buyers over 30 days.
    `day` is the first session AFTER the filing date (a filing can land after the close)."""
    days = prices.index
    info = universe.set_index("ticker")
    b = buys[buys["ticker"].isin(info.index) & buys["ticker"].isin(prices.columns)].copy()
    added = pd.to_datetime(info["added"], errors="coerce")
    b = b[b["filed"] >= b["ticker"].map(added).fillna(pd.Timestamp("1900-01-01"))]
    b["chief"] = b["title"].apply(lambda t: any(c in t for c in CHIEFS))
    b["director"] = b["relationship"].str.contains("Director", case=False)
    g = b.groupby(["ticker", "filed"]).agg(buyers=("owner", "nunique"), value=("value", "sum"),
                                           chief=("chief", "any"), director_only=("director", "all"))
    g = g.reset_index().sort_values(["ticker", "filed"])
    rows = []
    for t, grp in g.groupby("ticker"):
        owners = b[b["ticker"] == t][["filed", "owner"]]
        for r in grp.itertuples(index=False):
            recent = owners[(owners["filed"] <= r.filed) & (owners["filed"] > r.filed - pd.Timedelta(days=30))]
            i = int(days.searchsorted(r.filed, side="right"))
            if i >= len(days):
                continue
            rows.append({"ticker": t, "date": days[i], "day": i, "sector": info.at[t, "sector"],
                         "buyers": r.buyers, "value": r.value, "chief": bool(r.chief),
                         "director_only": bool(r.director_only), "buyers_30d": recent["owner"].nunique()})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def signals():
    from .earnings import Signal

    specs = (("any insider purchase", lambda e: pd.Series(True, index=e.index)),
             ("purchases worth $100k+", lambda e: e["value"] >= 100_000),
             ("3+ insiders buying within 30 days", lambda e: e["buyers_30d"] >= 3),
             ("the CEO, CFO or president buying", lambda e: e["chief"]),
             ("only directors buying", lambda e: e["director_only"]))
    return [Signal(f"{name}, held {h} days", "all", h, fn) for h in (20, 60, 120) for name, fn in specs]


def run(ev: pd.DataFrame, prices: pd.DataFrame, cash: pd.Series, universe: pd.DataFrame, draws: int = 2000,
        split: str = "2016-01-01", start: str = "2006-06-01") -> dict:
    """Judge every signal (luck-corrected, later years untouched) and measure each
    against the 'own every company equally' control over the same days."""
    from . import earnings
    from .optionslab import _alpha

    res = earnings.run(ev, prices, cash, draws=draws, split=split, start=start, sigs=signals())
    days = prices.loc[start:].index
    members = [t for t in universe["ticker"] if t in prices.columns]
    added = pd.to_datetime(universe.set_index("ticker")["added"], errors="coerce")
    R = prices[members].pct_change().reindex(days)
    member_mask = pd.DataFrame({t: days >= added.get(t, pd.Timestamp("1900-01-01")) for t in members}, index=days)
    control = R.where(member_mask).mean(axis=1)
    spy = prices["SPY"].pct_change().reindex(days)
    dev = days < pd.Timestamp(split)
    out = {}
    for label, mask in (("before", dev), ("after", ~dev)):
        a, _ = _alpha(control[mask], spy[mask], cash.reindex(days).fillna(0)[mask])
        out[label] = (float(a.mean() * 252), float(a.mean() / a.std() * math.sqrt(len(a))))
    res["control"] = out
    res["drift"] = {}
    return res


def describe(res: dict, top: int = 15) -> list[str]:
    from .drivers import holdout_bar
    from .earnings import _pct, strong

    c = res["control"]
    out = ["INSIDER BUYING: DO OFFICERS AND DIRECTORS BUYING THEIR OWN STOCK KNOW SOMETHING?", "=" * 80,
           f"{res['events']:,} filing days of open-market purchases at today's S&P 500 companies "
           f"(after each joined), {res['start']} to {res['end']}.",
           f"Control, owning every company equally: alpha {_pct(c['before'][0])} (t {c['before'][1]:+.1f}) before "
           f"{res['split'][:4]}, {_pct(c['after'][0])} (t {c['after'][1]:+.1f}) after. A signal must do clearly better.",
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
    out += ["", "RESULT: " + ("; ".join(w.name for w in win) + " cleared every bar. Paper-trade it first."
                              if win else "nothing cleared every bar.")]
    return out
