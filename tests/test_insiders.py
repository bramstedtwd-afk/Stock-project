"""Insider buying lab: only real open-market purchases, dated when they became public."""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd

from stocksage import insiders as ins


def fake_zip(rows):
    """rows: (accession, filed 'DD-MON-YYYY', doc, ticker, owner, title, code, shares, price, ad)."""
    sub = "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERTRADINGSYMBOL\n"
    own = "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
    trx = "ACCESSION_NUMBER\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
    for a, filed, doc, tk, owner, title, code, sh, px, ad in rows:
        sub += f"{a}\t{filed}\t{doc}\t{tk}\n"
        own += f"{a}\t{owner}\tOfficer\t{title}\n"
        trx += f"{a}\t{code}\t{sh}\t{px}\t{ad}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", sub)
        z.writestr("REPORTINGOWNER.tsv", own)
        z.writestr("NONDERIV_TRANS.tsv", trx)
    return buf.getvalue()


def test_only_open_market_purchases_on_form_4_count():
    blob = fake_zip([
        ("a1", "05-MAR-2015", "4", "aaa", "o1", "CEO", "P", "100", "10", "A"),   # counts
        ("a2", "05-MAR-2015", "4", "AAA", "o2", "", "S", "100", "10", "D"),      # a sale
        ("a3", "05-MAR-2015", "4", "AAA", "o3", "", "M", "100", "10", "A"),      # option exercise
        ("a4", "05-MAR-2015", "3", "AAA", "o4", "", "P", "100", "10", "A"),      # not a Form 4
    ])
    got = ins.parse_quarter(blob)
    assert list(got["ticker"]) == ["AAA"] and got["value"].iloc[0] == 1000.0
    assert got["filed"].iloc[0] == pd.Timestamp("2015-03-05") and "CEO" in got["title"].iloc[0]


def test_quarters_run_from_2006_to_now():
    q = ins.quarters(last=pd.Timestamp("2007-05-01"))
    assert q[0] == "2006q1" and q[-1] == "2007q2" and len(q) == 6


def test_events_start_after_the_filing_and_count_recent_buyers():
    days = pd.bdate_range("2014-01-01", "2016-12-30")
    px = pd.DataFrame({"AAA": np.linspace(10, 20, len(days)), "SPY": 100.0}, index=days)
    uni = pd.DataFrame({"ticker": ["AAA"], "sector": ["Energy"], "industry": ["x"], "added": ["2014-06-01"]})
    buys = pd.DataFrame({
        "ticker": ["AAA"] * 4,
        "filed": pd.to_datetime(["2014-03-03", "2015-03-02", "2015-03-10", "2015-03-20"]),
        "owner": ["o0", "o1", "o2", "o3"], "relationship": ["Officer"] * 4,
        "title": ["", "CHIEF FINANCIAL OFFICER", "", ""], "value": [5e4, 2e5, 1e4, 1e4]})
    ev = ins.build_events(buys, uni, px)
    assert len(ev) == 3, "the filing before the company joined the index is dropped"
    first = ev.iloc[0]
    assert first["date"] == pd.Timestamp("2015-03-03"), "acted on the session after the filing day"
    assert bool(first["chief"]) and ev["buyers_30d"].tolist() == [1, 2, 3]


def test_signal_family_is_fixed():
    names = [s.name for s in ins.signals()]
    assert len(names) == 15 and len(set(names)) == 15


def test_without_a_contact_email_it_explains_instead_of_calling_the_sec(monkeypatch):
    import pytest

    monkeypatch.delenv(ins.CONTACT_VAR, raising=False)
    with pytest.raises(RuntimeError, match=r"\.\\start\.bat insiders --contact"):
        ins.load_purchases(quarter_list=["2015q1"])


def test_the_contact_command_saves_privately(monkeypatch, tmp_path, capsys):
    import os

    from stocksage import cli, envfile

    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(ins, "load_purchases", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr("stocksage.earnings.load_universe", lambda *a, **k: pd.DataFrame(
        {"ticker": ["AAA"], "sector": ["x"], "industry": ["x"], "added": ["2000-01-01"]}))
    monkeypatch.setattr("stocksage.earnings.load_prices", lambda *a, **k: pd.DataFrame())
    assert cli.main(["insiders", "--contact", "me@example.com"]) == 1
    assert "STOCKSAGE_SEC_CONTACT=me@example.com" in (tmp_path / ".env").read_text()
    os.environ.pop(ins.CONTACT_VAR, None)
