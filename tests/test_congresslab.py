"""Congress lab: Senate disclosures dated by filing, never by trade. Offline."""

from __future__ import annotations

import numpy as np
import pandas as pd

from stocksage import congresslab as cl

REPORTS = [{"first_name": "Jane", "last_name": "Doe", "bioguide": "D000001", "date_recieved": "03/10/2016",
            "transactions": [
                {"transaction_date": "02/20/2016", "owner": "Self", "type": "Purchase",
                 "ticker": '<a href="https://finance.yahoo.com/q?s=AAA" target="_blank">AAA</a>',
                 "asset_type": "Stock", "amount": "$15,001 - $50,000"},
                {"transaction_date": "02/21/2016", "owner": "Spouse", "type": "Sale (Full)", "ticker": "BBB",
                 "asset_type": "Stock", "amount": "$1,001 - $15,000"},
                {"transaction_date": "02/22/2016", "owner": "Spouse", "type": "Purchase", "ticker": "--",
                 "asset_type": "Stock", "amount": "$1,001 - $15,000"}]},
           {"first_name": "John", "last_name": "Roe", "bioguide": "R000002", "date_recieved": "03/15/2016",
            "transactions": [{"transaction_date": "03/01/2016", "owner": "Spouse", "type": "Purchase",
                              "ticker": "AAA", "asset_type": "Stock", "amount": "$1,001 - $15,000"}]}]


def test_only_purchases_with_a_ticker_count_and_links_become_tickers():
    rows = cl.parse_reports(REPORTS)
    assert [(r["ticker"], r["owner"]) for r in rows] == [("AAA", "self"), ("AAA", "spouse")]
    assert rows[0]["value"] == 32500.5 and rows[0]["filed"] == pd.Timestamp("2016-03-10")


def test_events_start_the_session_after_the_filing_and_count_senators_over_30_days():
    days = pd.bdate_range("2016-01-04", periods=120)
    px = pd.DataFrame({"AAA": np.linspace(10, 12, 120), "SPY": np.linspace(200, 210, 120)}, index=days)
    uni = pd.DataFrame({"ticker": ["AAA"], "sector": ["Tech"], "added": ["1990-01-01"]})
    ev = cl.build_events(pd.DataFrame(cl.parse_reports(REPORTS)), uni, px)
    assert list(ev["date"]) == [pd.Timestamp("2016-03-11"), pd.Timestamp("2016-03-16")], "never before it was public"
    assert list(ev["senators_30d"]) == [1, 2] and bool(ev["own"].iloc[0]) and ev["lag_days"].iloc[0] == 19


def test_load_trades_reads_the_data_sets_daily_files(tmp_path):
    import json

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "transaction_report_for_03_10_2016.json").write_text(json.dumps(REPORTS))
    got = cl.load_trades(source=tmp_path)
    assert len(got) == 2 and set(got["ticker"]) == {"AAA"}
