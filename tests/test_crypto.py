"""Crypto context (fake sources) and the crypto lab's measuring tools. Offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from stocksage import crypto, cryptolab

NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)


def candles(start, step, n=100):
    """OKX shape: newest first, close in column 4, confirm flag in column 8 ('0' = still open)."""
    closes = [start * (1 + step) ** i for i in range(n)]
    rows = [[str(i), "0", "0", "0", str(c), "0", "0", "0", "1"] for i, c in enumerate(closes)]
    rows[-1][8] = "0"
    return {"data": rows[::-1]}


def pages(btc_step=0.01, fg="15", meme_d7=5.0):
    return {
        crypto.MARKETS.format(ids=",".join(crypto.MAJORS)): [
            {"id": "bitcoin", "symbol": "btc", "current_price": 70000, "market_cap_rank": 1,
             "price_change_percentage_24h_in_currency": -2.0, "price_change_percentage_7d_in_currency": 3.0,
             "price_change_percentage_30d_in_currency": -9.0}],
        crypto.CANDLES.format(sym="BTC"): candles(50000, btc_step),
        crypto.CANDLES.format(sym="ETH"): candles(3000, -0.005),
        crypto.GLOBAL: {"data": {"total_market_cap": {"usd": 2.5e12}, "market_cap_change_percentage_24h_usd": -3.4,
                                 "market_cap_percentage": {"btc": 59.0}}},
        crypto.MEME_MARKETS: [{"symbol": "doge", "name": "Dogecoin", "market_cap_rank": 9,
                               "price_change_percentage_24h_in_currency": -2.1,
                               "price_change_percentage_7d_in_currency": meme_d7,
                               "price_change_percentage_30d_in_currency": -20.0}],
        crypto.FEAR_GREED: {"data": [{"value": fg, "value_classification": "Extreme Fear"}, {"value": "22"}]},
        crypto.FUNDING.format(sym="BTC"): {"data": [{"fundingRate": "0.0005"}]},
        crypto.FUNDING.format(sym="ETH"): {"data": [{"fundingRate": "-0.0002"}]},
        crypto.STABLES: [{"totalCirculatingUSD": {"peggedUSD": 1e11 * (1 + 0.001 * i)}} for i in range(40)],
    }


def fake(p):
    def get(url):
        if url not in p:
            raise RuntimeError("no page")
        return json.dumps(p[url])
    return get


def test_packet_reads_every_source_and_sets_plain_flags():
    p = crypto.packet(fetch=fake(pages()), now=NOW)
    assert p["majors"][0]["robinhood"] == "BTC-USD" and p["majors"][0]["d30"] == -0.09
    assert p["trend"]["BTC"]["above_50d"] and not p["trend"]["ETH"]["above_50d"]
    assert p["funding"]["BTC"]["crowd"] == "longs crowded" and p["funding"]["ETH"]["crowd"] == "shorts crowded"
    assert p["stablecoins"]["d30"] > 0 and p["whole_market"]["btc_share"] == 0.59
    assert p["meme_sector"]["d7"] == 0.05
    assert p["flags"] == {"btc_uptrend": True, "eth_uptrend": False, "extreme_fear": True, "meme_window": True}
    text = crypto.render(p)
    assert "long trades allowed): YES" in text and "What StockSage measured" in text


def test_no_meme_window_when_bitcoin_is_in_a_downtrend():
    p = crypto.packet(fetch=fake(pages(btc_step=-0.01, fg="50")), now=NOW)
    assert p["flags"]["btc_uptrend"] is False and not p["flags"]["meme_window"] and not p["flags"]["extreme_fear"]


def test_every_source_down_still_renders():
    def down(url):
        raise RuntimeError("offline")

    p = crypto.packet(fetch=down, now=NOW)
    assert p["trend"] == {} and p["majors"] == [] and p["flags"]["btc_uptrend"] is None
    assert "unknown" in crypto.render(p)


def test_trend_uses_completed_days_only():
    t = crypto.trend("BTC", fetch=fake(pages(btc_step=0.0)))
    assert t["vs_50d"] == 0.0 and "vs_100d" in t


def test_lab_trend_filter_sits_out_a_falling_market_and_pays_costs():
    days = pd.date_range("2024-01-01", periods=400)
    close = pd.Series(np.r_[np.linspace(100, 200, 200), np.linspace(200, 100, 200)], index=days)
    r = cryptolab.trend_filter(close, 50)
    hold = close.pct_change().fillna(0)
    assert (1 + r).prod() > (1 + hold).prod(), "out of the market for most of the fall"
    flips = (close > close.rolling(50).mean()).astype(float).diff().abs().sum()
    assert flips >= 1 and r.min() >= hold.min() - cryptolab.COST


def test_lab_after_moves_pools_coins_and_measures_forward_returns():
    days = pd.date_range("2024-01-01", periods=80)
    p = pd.Series(100.0, index=days)
    p.iloc[10:] = 130.0          # one +30% day, flat afterwards
    res = cryptolab.after_moves({"X": p}, lambda r, px: r > 0.2)
    assert res["n"] == 1 and res["d1"]["median"] == 0.0 and res["d30"]["up"] == 0.0


def test_lab_fear_greed_buckets():
    days = pd.date_range("2024-01-01", periods=100)
    fg = pd.Series([10] * 50 + [90] * 50, index=days)
    btc = pd.Series(np.linspace(100, 200, 100), index=days)
    t = cryptolab.fear_greed_buckets(fg, btc, days=30)
    assert set(t.index) == {"extreme fear", "extreme greed"} and t.loc["extreme fear", "up"] == 1.0


def test_tests_never_reach_the_network():
    p = crypto.packet(now=NOW)
    assert p["majors"] == [] and p["trend"] == {}
