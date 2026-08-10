from stocksage import brain
from stocksage.db import Database


def seeded(path, ticker="AAPL"):
    db = Database(path)
    db.record_suggestion(ticker, "BUY", 0.4, 100.0, {"trend_long": 0.4}, 5)
    db.record_move_event(ticker, "2026-06-01", -0.04, 2.5, ["earnings"], [])
    db.close()
    return path


def test_snapshot_write_and_absorb(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", tmp_path / "repo" / "brain-snapshot.db")
    source = seeded(tmp_path / "source.db", "NVDA")
    out = brain.write_snapshot(db_path=source)
    assert out == brain.SNAPSHOT_PATH and out.exists()

    fresh = tmp_path / "fresh.db"
    Database(fresh).close()
    stats = brain.absorb_snapshot(db_path=fresh)
    assert stats["suggestions_added"] == 1
    merged = Database(fresh)
    assert {r["ticker"] for r in merged.recent_suggestions()} == {"NVDA"}
    merged.close()
    # Absorbing again adds nothing (idempotent — safe on every routine run).
    again = brain.absorb_snapshot(db_path=fresh)
    assert again["suggestions_added"] == 0


def test_absorb_without_snapshot_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", tmp_path / "nope.db")
    assert brain.absorb_snapshot(db_path=tmp_path / "b.db") is None


def test_brief_survives_bad_snapshot(tmp_path, monkeypatch):
    """A corrupted committed snapshot must never block a trading run."""
    from stocksage.engine import Engine
    from stocksage.advisor import build_brief
    from stocksage import universe
    from tests.conftest import make_ohlcv
    from tests.test_engine import FakeMarket

    bad = tmp_path / "brain-snapshot.db"
    bad.write_bytes(b"garbage, not sqlite")
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", bad)
    monkeypatch.setattr(universe, "all_tickers", lambda: ["OK"])
    engine = Engine(
        db=Database(tmp_path / "local.db"),
        market=FakeMarket({"OK": make_ohlcv(seed=91)}),
    )
    packet = build_brief(engine)
    assert packet["snapshot_absorbed"] == {
        "error": "snapshot unreadable — continuing on local brain"
    }
    assert packet["market_mood"] is not None  # the run itself proceeded
