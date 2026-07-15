import json

from stocksage.advisor import publish_brief
from stocksage.db import Database
from stocksage.engine import Engine
from tests.conftest import make_ohlcv
from tests.test_engine import FakeMarket


def _engine(tmp_path, monkeypatch):
    from stocksage import universe

    frames = {
        "CHEAP": make_ohlcv(days=300, start_price=8.0, daily_drift=0.004, seed=71),
        "WEAK": make_ohlcv(days=300, start_price=20.0, daily_drift=-0.008, seed=72),
    }
    monkeypatch.setattr(universe, "all_tickers", lambda: list(frames))
    db = Database(tmp_path / "brain.db")
    db.watchlist_add("CHEAP")
    return Engine(db=db, market=FakeMarket(frames))


def test_publish_writes_brief_playbook_snapshot(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    folder = tmp_path / "GoogleDrive" / "StockSage"
    result = publish_brief(engine, str(folder), max_price=30.0)

    files = {p.name for p in folder.iterdir()}
    assert any(name.startswith("StockSage Brief - ") for name in files)
    assert "brain-snapshot.db" in files

    brief_file = next(folder.glob("StockSage Brief - *.json"))
    packet = json.loads(brief_file.read_text())
    # Focus defaulted to the watchlist; CHEAP is present and scored.
    assert packet["focus"][0]["ticker"] == "CHEAP"
    assert result["candidates"] >= 1


def test_publish_is_idempotent_per_day(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    folder = tmp_path / "sync"
    publish_brief(engine, str(folder))
    publish_brief(engine, str(folder))  # same day: overwrites, no second brief file
    briefs = list(folder.glob("StockSage Brief - *.json"))
    assert len(briefs) == 1


def test_publish_explicit_tickers(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    folder = tmp_path / "sync"
    publish_brief(engine, str(folder), tickers=["WEAK"], include_snapshot=False)
    brief_file = next(folder.glob("StockSage Brief - *.json"))
    packet = json.loads(brief_file.read_text())
    assert packet["focus"][0]["ticker"] == "WEAK"
    assert not (folder / "brain-snapshot.db").exists()
