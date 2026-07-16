"""Direct Google Drive API tests — no real network, a fake service object
stands in for googleapiclient's fluent files().list/create/update chain."""

import re

import pytest

from stocksage import drive_api


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


def _content_of(media_body):
    """Extract the bytes/text a Media*Upload object was built with, for
    assertions, without needing real network I/O."""
    from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload

    if isinstance(media_body, MediaInMemoryUpload):
        return media_body.getbytes(0, media_body.size())
    if isinstance(media_body, MediaFileUpload):
        from pathlib import Path

        return Path(media_body._filename).read_bytes()
    return None


class FakeFiles:
    """Mirrors service.files() being called fresh each time (as real Google
    API client usage does) — so no state may live on this object itself;
    the id counter must be derived from the shared db, not an instance var."""

    def __init__(self, db):
        self.db = db

    def list(self, q, spaces=None, fields=None, pageSize=None):
        name = re.search(r"name = '([^']*)'", q).group(1)
        parent_match = re.search(r"'([^']*)' in parents", q)
        parent = parent_match.group(1) if parent_match else None
        matches = [
            {"id": fid, "name": meta["name"]}
            for fid, meta in self.db.items()
            if meta["name"] == name
            and (parent is None or parent in meta.get("parents", []))
        ]
        return _Exec({"files": matches})

    def create(self, body, media_body=None, fields=None):
        fid = f"file{len(self.db) + 1}"
        while fid in self.db:  # defensive against any future deletion support
            fid = f"file{int(fid[4:]) + 1}"
        self.db[fid] = {
            "name": body["name"],
            "parents": body.get("parents", []),
            "mimeType": body.get("mimeType"),
            "content": _content_of(media_body) if media_body else None,
        }
        return _Exec({"id": fid})

    def update(self, fileId, media_body=None):
        if media_body is not None:
            self.db[fileId]["content"] = _content_of(media_body)
        return _Exec({"id": fileId})


class FakeDriveService:
    def __init__(self):
        self.db: dict[str, dict] = {}

    def files(self):
        return FakeFiles(self.db)


# --- find_file / ensure_folder ---

def test_find_file_none_when_absent():
    svc = FakeDriveService()
    assert drive_api.find_file(svc, "missing.json") is None


def test_ensure_folder_creates_once_then_reuses():
    svc = FakeDriveService()
    fid1 = drive_api.ensure_folder(svc, "StockSage")
    fid2 = drive_api.ensure_folder(svc, "StockSage")
    assert fid1 == fid2
    assert svc.db[fid1]["mimeType"] == drive_api.FOLDER_MIME


# --- upload_or_update: create then true in-place update ---

def test_upload_or_update_creates_then_updates_in_place():
    svc = FakeDriveService()
    folder_id = drive_api.ensure_folder(svc)
    fid1 = drive_api.upload_or_update(
        svc, "StockSage Brief.json", '{"v":1}', "application/json", folder_id
    )
    assert svc.db[fid1]["content"] == b'{"v":1}'

    fid2 = drive_api.upload_or_update(
        svc, "StockSage Brief.json", '{"v":2}', "application/json", folder_id
    )
    assert fid1 == fid2  # same file, not a duplicate
    assert svc.db[fid1]["content"] == b'{"v":2}'  # true update in place
    # Exactly one file with this name exists, ever.
    assert sum(1 for m in svc.db.values() if m["name"] == "StockSage Brief.json") == 1


def test_upload_or_update_scopes_by_folder():
    """Same filename in two different folders must not collide."""
    svc = FakeDriveService()
    f1 = drive_api.ensure_folder(svc, "FolderA")
    f2 = drive_api.ensure_folder(svc, "FolderB")
    id1 = drive_api.upload_or_update(svc, "x.json", "a", "application/json", f1)
    id2 = drive_api.upload_or_update(svc, "x.json", "b", "application/json", f2)
    assert id1 != id2


# --- publish_via_api end to end ---

def test_publish_via_api_writes_all_three_files(tmp_path, monkeypatch):
    svc = FakeDriveService()
    monkeypatch.setattr(drive_api, "get_service", lambda: svc)

    snap = tmp_path / "brain-snapshot.db"
    snap.write_bytes(b"fake-sqlite-bytes")

    result = drive_api.publish_via_api(
        '{"as_of": "now"}', "# Playbook v3", snap
    )
    names = set(result["files"])
    assert names == {
        "StockSage Brief.json", "StockSage Routine Playbook.md", "brain-snapshot.db"
    }
    folder_id = result["folder_id"]
    for fid in result["files"].values():
        assert folder_id in svc.db[fid]["parents"]
    brief_id = result["files"]["StockSage Brief.json"]
    assert svc.db[brief_id]["content"] == b'{"as_of": "now"}'


def test_publish_via_api_second_call_updates_not_duplicates(tmp_path, monkeypatch):
    svc = FakeDriveService()
    monkeypatch.setattr(drive_api, "get_service", lambda: svc)
    snap = tmp_path / "brain-snapshot.db"
    snap.write_bytes(b"v1")

    drive_api.publish_via_api('{"n":1}', "playbook v1", snap)
    snap.write_bytes(b"v2")
    drive_api.publish_via_api('{"n":2}', "playbook v2", snap)

    # Still exactly one of each file — never accumulates dated duplicates.
    assert len([m for m in svc.db.values() if m["name"] == "StockSage Brief.json"]) == 1
    brief = next(m for m in svc.db.values() if m["name"] == "StockSage Brief.json")
    assert brief["content"] == b'{"n":2}'


def test_publish_via_api_skips_missing_snapshot(tmp_path, monkeypatch):
    svc = FakeDriveService()
    monkeypatch.setattr(drive_api, "get_service", lambda: svc)
    result = drive_api.publish_via_api("{}", "playbook", tmp_path / "nope.db")
    assert "brain-snapshot.db" not in result["files"]


# --- configuration / graceful failure ---

def test_get_service_raises_helpful_error_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_api, "CREDENTIALS_PATH", tmp_path / "nope.json")
    with pytest.raises(drive_api.DriveNotConfigured, match="One-time setup"):
        drive_api.get_service()


def test_credentials_available(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_api, "CREDENTIALS_PATH", tmp_path / "creds.json")
    assert drive_api.credentials_available() is False
    (tmp_path / "creds.json").write_text("{}")
    assert drive_api.credentials_available() is True


def test_escape_prevents_query_injection():
    assert drive_api._escape("it's a test") == "it\\'s a test"
