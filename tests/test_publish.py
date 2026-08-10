import json

from stocksage.advisor import catch_up_from_drive, publish_brief, publish_brief_via_api
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


def test_publish_never_dirties_the_repo_tracked_snapshot(tmp_path, monkeypatch):
    """Regression: an automated publish run must not write to the
    repo-tracked brain/brain-snapshot.db — only the deliberate `brain
    snapshot` command may touch that file. Writing there on every
    scheduled run would permanently block `start.sh update` (git sees
    the repo as dirty and refuses to pull)."""
    from stocksage import brain

    fake_snapshot_path = tmp_path / "repo-would-be-here" / "brain-snapshot.db"
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", fake_snapshot_path)

    engine = _engine(tmp_path, monkeypatch)
    folder = tmp_path / "GoogleDrive" / "StockSage"
    publish_brief(engine, str(folder), max_price=30.0)
    assert not fake_snapshot_path.exists()  # never created, never touched


def test_publish_via_api_never_dirties_the_repo_tracked_snapshot(tmp_path, monkeypatch):
    from stocksage import brain, drive_api
    from tests.test_drive_api import FakeDriveService

    fake_snapshot_path = tmp_path / "repo-would-be-here" / "brain-snapshot.db"
    monkeypatch.setattr(brain, "SNAPSHOT_PATH", fake_snapshot_path)
    monkeypatch.setattr(brain, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(drive_api, "get_service", lambda: FakeDriveService())

    engine = _engine(tmp_path, monkeypatch)
    publish_brief_via_api(engine, max_price=30.0)
    assert not fake_snapshot_path.exists()


def test_publish_via_api_end_to_end(tmp_path, monkeypatch):
    """The no-install path produces the same brief content as the
    folder-based path, delivered via the API instead of a synced folder."""
    from stocksage import brain, drive_api
    from tests.test_drive_api import FakeDriveService

    monkeypatch.setattr(brain, "STATE_DIR", tmp_path / "state")
    engine = _engine(tmp_path, monkeypatch)
    svc = FakeDriveService()
    monkeypatch.setattr(drive_api, "get_service", lambda: svc)

    result = publish_brief_via_api(engine, max_price=30.0)
    assert set(result["files"]) == {
        "StockSage Brief.json", "StockSage Routine Playbook.md", "brain-snapshot.db"
    }
    brief_id = result["files"]["StockSage Brief.json"]
    packet = json.loads(svc.db[brief_id]["content"])
    assert packet["focus"][0]["ticker"] == "CHEAP"  # watchlist default, same as folder path
    assert result["candidates"] >= 1


# --- catch_up_from_drive: how a stateless machine (fresh cloud session,
# brand-new device) starts smart instead of from scratch ---

def test_catch_up_from_drive_merges_when_snapshot_present(tmp_path, monkeypatch):
    from googleapiclient.http import MediaFileUpload

    from stocksage import brain, drive_api
    from tests.test_drive_api import FakeDriveService

    monkeypatch.setattr(brain, "STATE_DIR", tmp_path / "state")

    # Simulate another machine's brain that already has a graded call.
    source_db = tmp_path / "source.db"
    src_engine = Engine(db=Database(source_db), market=FakeMarket({}))
    sid = src_engine.db.record_suggestion("NVDA", "BUY", 0.4, 120.0, {"x": 0.4}, 5)
    src_engine.db.mark_evaluated(sid, 0.05, True)
    snap = brain.export_brain(tmp_path / "snap.db", db_path=source_db)

    svc = FakeDriveService()
    folder_id = drive_api.ensure_folder(svc)
    media = MediaFileUpload(str(snap), mimetype="application/x-sqlite3")
    svc.files().create(
        body={"name": "brain-snapshot.db", "parents": [folder_id]},
        media_body=media, fields="id",
    ).execute()
    monkeypatch.setattr(drive_api, "get_service", lambda: svc)

    fresh_engine = Engine(db=Database(tmp_path / "fresh.db"), market=FakeMarket({}))
    stats = catch_up_from_drive(fresh_engine)
    assert stats is not None
    assert stats["suggestions_added"] == 1
    rows = fresh_engine.db.recent_suggestions()
    assert rows[0]["ticker"] == "NVDA"


def test_catch_up_from_drive_none_when_not_configured(tmp_path, monkeypatch):
    from stocksage import brain, drive_api

    monkeypatch.setattr(brain, "STATE_DIR", tmp_path / "state")

    def _raise():
        raise drive_api.DriveNotConfigured("nope")

    monkeypatch.setattr(drive_api, "get_service", lambda: _raise())
    engine = Engine(db=Database(tmp_path / "e.db"), market=FakeMarket({}))
    assert catch_up_from_drive(engine) is None


def test_catch_up_from_drive_none_when_nothing_published(tmp_path, monkeypatch):
    from stocksage import brain, drive_api
    from tests.test_drive_api import FakeDriveService

    monkeypatch.setattr(brain, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(drive_api, "get_service", lambda: FakeDriveService())
    engine = Engine(db=Database(tmp_path / "e.db"), market=FakeMarket({}))
    assert catch_up_from_drive(engine) is None


# --- Windows encoding (cp1252) regression ---


def test_playbook_reads_as_utf8_regardless_of_platform_locale():
    """ROUTINE.md is full of em-dashes and arrows. On Windows, Python's
    default text encoding is the locale codepage (cp1252), so an
    unqualified read_text() raised UnicodeDecodeError and killed
    publish-drive outright. Every text read must name its encoding.
    """
    from pathlib import Path

    import pytest

    routine = Path(__file__).resolve().parent.parent / "ROUTINE.md"
    assert routine.exists()
    raw = routine.read_bytes()
    # The file genuinely contains non-cp1252-safe bytes, so this test would
    # be vacuous if it ever became pure ASCII.
    assert any(b > 0x7F for b in raw), "expected non-ASCII in ROUTINE.md"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")
    assert raw.decode("utf-8")  # the encoding the code must actually use


def test_no_unqualified_text_io_in_the_package():
    """Guardrail: any read_text()/write_text() without an explicit encoding
    is a latent Windows crash, because the default is the locale codepage."""
    import re
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "stocksage"
    offenders = []
    for py in pkg.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        for call in re.finditer(r"\.(read_text|write_text)\(", src):
            # Grab the balanced call text so multi-line calls are handled.
            i = call.end() - 1
            depth, j = 0, i
            while j < len(src):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if "encoding=" not in src[i : j + 1]:
                line = src[: call.start()].count("\n") + 1
                offenders.append(f"{py.name}:{line}")
    assert not offenders, f"text I/O without explicit encoding: {offenders}"
