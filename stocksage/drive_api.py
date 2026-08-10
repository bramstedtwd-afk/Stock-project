"""Direct Google Drive API access — for machines where installing anything
(including Google Drive for Desktop) isn't possible: no admin rights, a
managed/locked-down device, IT policy, etc.

This talks to Drive's API directly from Python. Nothing gets installed —
only two small Python packages inside StockSage's own virtual environment
(no admin rights needed for that, same as every other dependency). Setup
is a one-time, browser-based sign-in, not a program you download and run.

One-time setup (see README for the click-by-click version):
  1. Create a free Google Cloud project and an OAuth "Desktop app" client
     at https://console.cloud.google.com/apis/credentials
  2. Download its JSON and save it as ~/.stocksage/drive_credentials.json
  3. First run opens your browser once to grant access; after that a
     cached token refreshes itself silently — no further sign-ins.

Files are created or updated *in place* by a fixed name inside one
"StockSage" folder in your Drive — no version-numbered files, no
ambiguity about which one is current, unlike the read/create-only
connector tools used elsewhere in this project. Scope is deliberately
narrow (`drive.file`): this app can only see files it created itself,
never your other Drive contents.

Headless machines (no browser at all, e.g. a scheduled cloud session):
once a device has done the one-time browser sign-in above, its
~/.stocksage/drive_token.json contains a refresh token that's good
indefinitely. Copy that file's contents into the STOCKSAGE_DRIVE_TOKEN
environment variable on the headless machine and get_service() seeds
~/.stocksage/drive_token.json from it on first use — no browser, no
drive_credentials.json needed there at all, since a valid token alone is
enough to refresh itself.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("~/.stocksage").expanduser()
CREDENTIALS_PATH = STATE_DIR / "drive_credentials.json"
TOKEN_PATH = STATE_DIR / "drive_token.json"
TOKEN_ENV_VAR = "STOCKSAGE_DRIVE_TOKEN"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_NAME = "StockSage"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveNotConfigured(RuntimeError):
    """Raised when drive_credentials.json hasn't been set up yet."""


class DriveAuthExpired(DriveNotConfigured):
    """A previously-working Drive sign-in can no longer refresh itself.

    Distinct from DriveNotConfigured because the two want opposite handling:
    never-configured is normal on a machine that doesn't use Drive and should
    stay quiet, while an expired grant means sync *was* working and has
    silently stopped — which must be said out loud or the brain quietly stops
    travelling between devices.
    """


def _is_dead_refresh_token(exc: Exception) -> bool:
    """True when a refresh failed because the grant itself is gone.

    Google signals expired/revoked refresh tokens as 'invalid_grant'
    regardless of the underlying reason, so match on that rather than on an
    exception type that varies across google-auth versions.
    """
    return "invalid_grant" in str(exc).lower()


def credentials_available() -> bool:
    return CREDENTIALS_PATH.exists()


def get_service():
    """Authenticated Drive API client, refreshing or requesting consent
    as needed. Raises DriveNotConfigured with setup instructions if
    there's no way to get a working token.

    On a headless machine with no token on disk yet, seeds one from the
    STOCKSAGE_DRIVE_TOKEN environment variable if set — see module
    docstring. drive_credentials.json is only required for the one-time
    interactive browser consent; once a valid token exists (on disk or
    seeded), it's never touched again.
    """
    if not TOKEN_PATH.exists():
        seed = os.environ.get(TOKEN_ENV_VAR)
        if seed:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(seed)
            TOKEN_PATH.chmod(0o600)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise DriveNotConfigured(
            "Drive API packages aren't installed. Run: "
            "pip install google-auth-oauthlib google-api-python-client"
        ) from exc

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                if not _is_dead_refresh_token(exc):
                    raise
                # Google returns a bare "invalid_grant" for every cause, so
                # spell out the one that actually bites: while the OAuth
                # consent screen sits in "Testing", Google expires refresh
                # tokens after 7 days, which silently kills Drive sync about
                # once a week forever.
                TOKEN_PATH.unlink(missing_ok=True)
                raise DriveAuthExpired(
                    "Your Google Drive sign-in has expired and can't renew "
                    "itself.\n\n"
                    "The usual cause: the OAuth consent screen for your Google "
                    "Cloud project is still in 'Testing' mode, and Google "
                    "expires those refresh tokens after 7 days. To stop this "
                    "recurring, open Google Cloud Console -> APIs & Services "
                    "-> OAuth consent screen and press 'Publish app'. For a "
                    "personal single-user app no verification review is "
                    "needed; you just click through the 'unverified app' "
                    "warning once.\n\n"
                    "Either way, re-authorize now by running:  "
                    "stocksage publish-drive\n"
                    "(the expired token has been cleared, so this will prompt "
                    "a fresh sign-in)."
                ) from exc
        else:
            if not CREDENTIALS_PATH.exists():
                raise DriveNotConfigured(
                    "No Google Drive API credentials found. One-time setup "
                    f"(no install required): see README, then save your OAuth "
                    f"client JSON as {CREDENTIALS_PATH}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
        TOKEN_PATH.chmod(0o600)
    return build("drive", "v3", credentials=creds)


def find_file(service, name: str, parent_id: str | None = None) -> str | None:
    """The file id of an existing, non-trashed file with this exact name."""
    q = f"name = '{_escape(name)}' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = (
        service.files()
        .list(q=q, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute()
    )
    files = results.get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(service, name: str = FOLDER_NAME) -> str:
    """The id of the StockSage folder in Drive, creating it if absent."""
    existing = find_file(service, name)
    if existing:
        return existing
    meta = {"name": name, "mimeType": FOLDER_MIME}
    created = service.files().create(body=meta, fields="id").execute()
    return created["id"]


def upload_or_update(
    service, name: str, content: str, mime_type: str, folder_id: str
) -> str:
    """Create the file if it doesn't exist, or overwrite its content in
    place if it does — a real update, no version-numbered files needed.
    Returns the file id."""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
    existing = find_file(service, name, parent_id=folder_id)
    if existing:
        service.files().update(fileId=existing, media_body=media).execute()
        return existing
    meta = {"name": name, "parents": [folder_id]}
    created = (
        service.files().create(body=meta, media_body=media, fields="id").execute()
    )
    return created["id"]


def download_file(service, name: str, folder_id: str) -> bytes | None:
    """Raw bytes of a file in the StockSage Drive folder, or None if no
    file by that name has been published there yet."""
    fid = find_file(service, name, parent_id=folder_id)
    if fid is None:
        return None
    return service.files().get_media(fileId=fid).execute()


def pull_brain_snapshot(dest_path: str | Path) -> Path | None:
    """Download the shared brain-snapshot.db from Drive to dest_path.

    Returns the local path it was saved to, or None if no snapshot has
    been published to Drive yet (nothing to catch up on). Raises
    DriveNotConfigured the same way every other Drive call does if
    there's no way to authenticate — callers that want this to be
    optional should catch that.
    """
    service = get_service()
    folder_id = ensure_folder(service)
    content = download_file(service, "brain-snapshot.db", folder_id)
    if content is None:
        return None
    dest_path = Path(dest_path).expanduser()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)
    return dest_path


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def publish_via_api(brief_json: str, playbook_text: str, snapshot_path: Path) -> dict:
    """Push the research brief, playbook, and brain snapshot straight to
    Drive via the API — the no-install alternative to a locally-synced
    folder. Each file is updated in place under one fixed name, so the
    routine never has to guess which of several files is newest."""
    service = get_service()
    folder_id = ensure_folder(service)
    written = {
        "StockSage Brief.json": upload_or_update(
            service, "StockSage Brief.json", brief_json,
            "application/json", folder_id,
        ),
        "StockSage Routine Playbook.md": upload_or_update(
            service, "StockSage Routine Playbook.md", playbook_text,
            "text/markdown", folder_id,
        ),
    }
    if snapshot_path.exists():
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(snapshot_path), mimetype="application/x-sqlite3")
        existing = find_file(service, "brain-snapshot.db", parent_id=folder_id)
        if existing:
            service.files().update(fileId=existing, media_body=media).execute()
            written["brain-snapshot.db"] = existing
        else:
            meta = {"name": "brain-snapshot.db", "parents": [folder_id]}
            created = (
                service.files()
                .create(body=meta, media_body=media, fields="id")
                .execute()
            )
            written["brain-snapshot.db"] = created["id"]
    return {"folder_id": folder_id, "files": written}
