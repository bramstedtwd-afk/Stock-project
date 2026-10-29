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
import re
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("~/.stocksage").expanduser()
CREDENTIALS_PATH = STATE_DIR / "drive_credentials.json"
TOKEN_PATH = STATE_DIR / "drive_token.json"
TOKEN_ENV_VAR = "STOCKSAGE_DRIVE_TOKEN"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_NAME = "StockSage"
FOLDER_MIME = "application/vnd.google-apps.folder"
BRIEF_NAME = "StockSage Brief.json"
PLAYBOOK_NAME = "StockSage Routine Playbook.md"


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


def clean_token(raw: str | None) -> str | None:
    """The token JSON as pasted into an environment-variable box, which may keep
    the quotes around it (KEY='{...}') or add stray spaces and line breaks."""
    text = (raw or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" and not text.startswith('{"'):
        text = text[1:-1].strip()
    return text or None


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
        seed = clean_token(os.environ.get(TOKEN_ENV_VAR))
        if seed:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(seed, encoding="utf-8")
            TOKEN_PATH.chmod(0o600)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        # No Google packages (the trading routine's bare cloud box): the same
        # token works through plain HTTPS, so nothing has to be installed.
        info = _token_info()
        if info and info.get("refresh_token"):
            return RestDrive(info)
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
                    "Either way, re-authorize now. In the StockSage folder run:\n"
                    "    .\\start.bat publish-drive        (Windows)\n"
                    "    ./start.sh publish-drive       (Mac/Linux)\n"
                    "The expired token has been cleared, so this will prompt a "
                    "fresh sign-in."
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
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
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
    media = _media(content.encode("utf-8"), mime_type)
    existing = find_file(service, name, parent_id=folder_id)
    if existing:
        service.files().update(fileId=existing, media_body=media).execute()
        return existing
    meta = {"name": name, "parents": [folder_id]}
    created = (
        service.files().create(body=meta, media_body=media, fields="id").execute()
    )
    return created["id"]


def publish_text(name: str, text: str) -> str:
    """Put one plain-text document in the StockSage Drive folder, in place.

    Used for the human-readable "StockSage Today" sheet, so it can be opened
    from a phone. Returns the Drive file id.
    """
    service = get_service()
    folder_id = ensure_folder(service)
    return upload_or_update(service, name, text, "text/plain", folder_id)


def read_drive_text(name: str) -> str | None:
    """The text of a file in the StockSage Drive folder, or None if absent."""
    service = get_service()
    folder = find_file(service, FOLDER_NAME)
    if folder is None:
        return None
    raw = download_file(service, name, folder)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def read_or_create_text(name: str, default: str) -> str:
    """The text of a file in the StockSage Drive folder, creating it with
    `default` the first time. Created here so this app can read it back later
    (its Drive access reaches only files it created), and the routine appends."""
    service = get_service()
    folder_id = ensure_folder(service)
    raw = download_file(service, name, folder_id)
    if raw is None:
        upload_or_update(service, name, default, "text/plain", folder_id)
        return default
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def modified_time(name: str):
    """When a file in the StockSage Drive folder was last written (UTC), or None
    if the folder or file does not exist. Read-only."""
    from datetime import datetime, timezone

    service = get_service()
    folder = find_file(service, FOLDER_NAME)
    if folder is None:
        return None
    res = (
        service.files()
        .list(q=f"name = '{_escape(name)}' and trashed = false and '{folder}' in parents",
              spaces="drive", fields="files(id, modifiedTime)", pageSize=1)
        .execute()
    )
    files = res.get("files", [])
    if not files or not files[0].get("modifiedTime"):
        return None
    raw = files[0]["modifiedTime"].replace("Z", "+00:00")
    return datetime.fromisoformat(raw).astimezone(timezone.utc)


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


def playbook_version(text: str) -> int | None:
    """The version number from a playbook's own header line, or None.

    Header looks like: `# Agentic Trading Routine — Playbook (v11, 2026-08-10)`
    """
    import re

    if not text:
        return None
    head = text.lstrip().splitlines()[0] if text.strip() else ""
    m = re.search(r"\(v(\d+)\b", head)
    return int(m.group(1)) if m else None


def would_downgrade_playbook(service, folder_id, local_text: str):
    """(True, remote_v, local_v) when uploading would replace the playbook in
    Drive with an OLDER version.

    The playbook calls itself "always current by construction", but nothing
    enforced that: any machine running publish-drive from a stale checkout
    silently overwrote the canonical file with its own older copy. That
    happened — a v7-era playbook landed on top of v10 overnight, and the
    routine woke up operating under superseded risk rules while its log
    still showed the newer ones. A publish must never be able to move the
    playbook backwards.
    """
    local_v = playbook_version(local_text)
    if local_v is None:
        return False, None, None  # unversioned: nothing to compare, allow
    try:
        raw = download_file(service, PLAYBOOK_NAME, folder_id)
        if not raw:
            return False, None, local_v
        remote_text = raw.decode("utf-8", "replace")
    except Exception as exc:  # a check that fails must not block publishing
        log.debug("playbook version check skipped: %s", exc)
        return False, None, local_v
    remote_v = playbook_version(remote_text)
    if remote_v is None:
        return False, None, local_v
    return local_v < remote_v, remote_v, local_v


PLACEHOLDER = "{{AGENTIC_ACCOUNT}}"


def fill_account_from_drive(service, folder_id, local_text: str) -> str | None:
    """The playbook with its account placeholder filled from Drive's copy.

    A machine without the account number (the cloud runner) must still be able
    to publish a new playbook version, but never one that says
    "{{AGENTIC_ACCOUNT}}" where Drive's copy had the real number: the routine
    would stop and ask which account to trade. Returns the text unchanged when
    there is no placeholder, the filled text when Drive's copy carries the
    number, and None (do not publish) when it would replace a filled copy with
    a blank one."""
    if PLACEHOLDER not in local_text:
        return local_text
    try:
        raw = download_file(service, PLAYBOOK_NAME, folder_id)
    except Exception as exc:
        log.debug("could not read Drive's playbook: %s", exc)
        return None
    if not raw:
        return local_text                  # first publish: nothing better to offer
    remote = raw.decode("utf-8", "replace")
    if PLACEHOLDER in remote:
        return local_text
    m = re.search(r"\(#(\d{6,12})\)", remote)
    return local_text.replace(PLACEHOLDER, m.group(1)) if m else None


def publish_via_api(brief_json: str, playbook_text: str, snapshot_path: Path) -> dict:
    """Push the research brief, playbook, and brain snapshot straight to
    Drive via the API — the no-install alternative to a locally-synced
    folder. Each file is updated in place under one fixed name, so the
    routine never has to guess which of several files is newest."""
    service = get_service()
    folder_id = ensure_folder(service)
    written = {
        BRIEF_NAME: upload_or_update(
            service, BRIEF_NAME, brief_json, "application/json", folder_id,
        ),
    }
    # The brief is always safe to refresh — it describes right now. The
    # playbook is not: publishing it from a stale checkout is how a v7 copy
    # landed on top of v10 and the routine ran a superseded ruleset.
    downgrade, remote_v, local_v = would_downgrade_playbook(
        service, folder_id, playbook_text
    )
    skipped = None
    filled = None if downgrade else fill_account_from_drive(service, folder_id, playbook_text)
    if not downgrade and filled is None:
        skipped = ("this machine has no agentic account number and Drive's playbook does; left "
                   "Drive's copy alone rather than blanking the account. Set STOCKSAGE_AGENTIC_ACCOUNT.")
        log.warning("%s", skipped)
    elif downgrade:
        skipped = (
            f"Drive already has playbook v{remote_v}; this machine has only "
            f"v{local_v}. Left Drive's copy alone rather than moving the "
            f"routine's rules backwards. Update this checkout, then publish."
        )
        log.warning("%s", skipped)
    else:
        written[PLAYBOOK_NAME] = upload_or_update(
            service, PLAYBOOK_NAME, filled, "text/markdown", folder_id,
        )
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
    out = {"folder_id": folder_id, "files": written}
    if skipped:
        out["playbook_skipped"] = skipped
    return out


# --- the same Drive calls with nothing installed ------------------------------------
#
# The trading routine runs in a fresh cloud box each hour. Downloading the Google
# client there is one more thing that can fail (it did, once), so the few calls it
# needs (find, read, create, overwrite one small text file) also go over plain
# HTTPS with the standard library. Same token, same drive.file scope.

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"


def _token_info() -> dict | None:
    import json

    raw = None
    if TOKEN_PATH.exists():
        raw = TOKEN_PATH.read_text(encoding="utf-8")
    else:
        raw = clean_token(os.environ.get(TOKEN_ENV_VAR))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _media(data: bytes, mime_type: str):
    try:
        from googleapiclient.http import MediaInMemoryUpload

        return MediaInMemoryUpload(data, mimetype=mime_type)
    except ImportError:
        return _Media(data, mime_type)


class _Media:
    """The three methods of MediaInMemoryUpload that RestDrive reads."""

    def __init__(self, data: bytes, mime_type: str):
        self._data, self._mime = data, mime_type

    def size(self) -> int:
        return len(self._data)

    def getbytes(self, begin: int, length: int) -> bytes:
        return self._data[begin:begin + length]

    def mimetype(self) -> str:
        return self._mime


def _urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class RestDrive:
    """A stand-in for the `build("drive", "v3")` service, covering only
    files().list / get_media / create / update, as this module uses them."""

    def __init__(self, info: dict, http=None):
        self.info, self.http = dict(info), http or _urllib_http
        self._access = None

    def _refresh(self) -> str:
        import json
        from urllib.parse import urlencode

        body = urlencode({"client_id": self.info.get("client_id", ""),
                          "client_secret": self.info.get("client_secret", ""),
                          "refresh_token": self.info["refresh_token"],
                          "grant_type": "refresh_token"}).encode()
        status, raw = self.http("POST", self.info.get("token_uri") or "https://oauth2.googleapis.com/token",
                                {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200:
            if b"invalid_grant" in raw:
                raise DriveAuthExpired("Your Google Drive sign-in has expired and can't renew itself. "
                                       "On the always-on computer run  .\\start.bat publish-drive  to sign in "
                                       "again, then copy the new token where it is used.")
            raise RuntimeError(f"Google sign-in refresh failed ({status}): {raw[:200]!r}")
        self._access = json.loads(raw)["access_token"]
        return self._access

    def request(self, method: str, url: str, body: bytes | None = None, content_type: str | None = None,
                raw: bool = False):
        import json

        for attempt in (1, 2):
            token = self._access or self._refresh()
            headers = {"Authorization": f"Bearer {token}"}
            if content_type:
                headers["Content-Type"] = content_type
            status, data = self.http(method, url, headers, body)
            if status == 401 and attempt == 1:
                self._access = None
                continue
            if status >= 400:
                raise RuntimeError(f"Drive said {status}: {data[:200]!r}")
            return data if raw else (json.loads(data) if data else {})

    def files(self):
        return _RestFiles(self)


class _RestFiles:
    def __init__(self, drive: RestDrive):
        self.d = drive

    def list(self, q, spaces="drive", fields="files(id, name)", pageSize=100):
        from urllib.parse import urlencode

        url = f"{DRIVE_API}/files?" + urlencode({"q": q, "spaces": spaces, "fields": fields, "pageSize": pageSize})
        return _Call(lambda: self.d.request("GET", url))

    def get_media(self, fileId):
        return _Call(lambda: self.d.request("GET", f"{DRIVE_API}/files/{fileId}?alt=media", raw=True))

    def create(self, body, media_body=None, fields="id"):
        import json

        if media_body is None:
            return _Call(lambda: self.d.request("POST", f"{DRIVE_API}/files?fields={fields}",
                                                json.dumps(body).encode(), "application/json"))
        boundary = "stocksage-boundary-7f3a"
        data = media_body.getbytes(0, media_body.size())
        payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(body)}\r\n"
                   f"--{boundary}\r\nContent-Type: {media_body.mimetype()}\r\n\r\n").encode() + data + \
            f"\r\n--{boundary}--".encode()
        return _Call(lambda: self.d.request("POST", f"{DRIVE_UPLOAD}/files?uploadType=multipart&fields={fields}",
                                            payload, f"multipart/related; boundary={boundary}"))

    def update(self, fileId, media_body=None, body=None, fields="id"):
        data = media_body.getbytes(0, media_body.size())
        return _Call(lambda: self.d.request("PATCH", f"{DRIVE_UPLOAD}/files/{fileId}?uploadType=media&fields={fields}",
                                            data, media_body.mimetype()))
