"""Leaving this machine: take StockSage off it completely.

Signing out of Windows stops the scheduled jobs while you are signed out,
but it does not remove them and it does not protect anything on the disk.
Whoever uses this computer next can read the Robinhood password in .env,
reuse the cached broker session token, reach the Google Drive folder with
the stored OAuth token, and read the whole trading history in the brain.
Signing out is not leaving.

This module does the part that can be automated:

    unschedule   delete every StockSage scheduled job on this machine
    erase        delete credentials, tokens, logs, the brain, the shortcut
    report       tell the owner the revocations only they can perform

The revocations matter most. Deleting a file removes the reference to it,
not necessarily the bytes — on an SSD the old blocks can survive a delete
for a long time. A password you have changed and a token you have revoked
are worthless to anyone who recovers them, so the printed steps are the
real protection and the deletion is defence in depth.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Target:
    """One thing to remove, described in the owner's terms."""

    path: Path
    what: str
    why: str

    @property
    def exists(self) -> bool:
        return self.path.exists()


@dataclass
class Result:
    unscheduled: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    brain_saved_to: str | None = None
    brain_left_in_sync_folder: str | None = None


def sensitive_targets() -> list[Target]:
    """Everything StockSage leaves on a machine, worst first."""
    from .db import default_db_path
    from .envfile import ENV_PATH
    from .security import access_log_path, token_pickle_path

    state = Path("~/.stocksage").expanduser()
    # NOT a hardcoded path: after `brain sync` the brain lives in a cloud
    # folder and STOCKSAGE_DB points there. Deleting the wrong file would
    # report success while the whole trading history stayed on the machine.
    brain_path = default_db_path()
    return [
        Target(
            ENV_PATH, "your Robinhood username and password",
            "plain text — anyone who opens this file can sign in as you",
        ),
        Target(
            token_pickle_path(), "the cached broker session",
            "a bearer token: holding it is as good as being logged in",
        ),
        Target(
            state / "drive_token.json", "your Google Drive token",
            "reaches the Drive folder your research and brain are published to",
        ),
        Target(
            state / "drive_credentials.json", "your Google API client",
            "identifies your Google Cloud project",
        ),
        Target(
            brain_path, "the brain",
            "every graded call and your whole mirrored trading history",
        ),
        Target(
            access_log_path(), "the broker access log",
            "when this app opened your account",
        ),
        Target(state / "daily.log", "the autopilot log", "run history"),
        Target(state / "publish.log", "the publish log", "run history"),
        Target(state / "server.log", "the dashboard log", "run history"),
    ]


def shortcut_targets() -> list[Target]:
    import os

    if os.name == "nt":
        return [Target(
            Path("~/Desktop/StockSage.lnk").expanduser(),
            "the Desktop shortcut", "launches StockSage",
        )]
    return [
        Target(Path("~/Applications/StockSage.app").expanduser(),
               "the StockSage app", "launches StockSage"),
        Target(Path("~/Desktop/StockSage.desktop").expanduser(),
               "the Desktop icon", "launches StockSage"),
        Target(Path("~/.local/share/applications/StockSage.desktop").expanduser(),
               "the applications-menu entry", "launches StockSage"),
    ]


def unschedule(result: Result) -> None:
    """Remove every scheduled job, on whichever scheduler this OS uses."""
    from . import autopilot

    try:
        autopilot.turn_off()
        result.unscheduled.append("all StockSage scheduled jobs")
    except Exception as exc:  # a scheduler that refuses must not stop the wipe
        result.failed.append(f"scheduled jobs ({type(exc).__name__}: {exc})")


def _delete(target: Target, result: Result) -> None:
    try:
        if target.path.is_dir():
            shutil.rmtree(target.path)
        else:
            target.path.unlink()
        result.removed.append(target.what)
    except Exception as exc:
        result.failed.append(f"{target.what} ({type(exc).__name__}: {exc})")


def brain_is_shared() -> bool:
    """True only when the brain sits inside a cloud-synced folder.

    Such a brain is not 'left behind on this machine' — it is the copy that
    follows the owner to their next computer, and deleting it here would
    delete it out of every device that syncs the folder.

    The test is deliberately narrow. "The brain is not in the default
    location" is not enough: STOCKSAGE_DB may point at a second local disk,
    which syncs nowhere, and skipping that would leave the owner's entire
    trading history on a machine they have walked away from. Only a path
    under a folder we can actually see a sync client for counts.
    """
    from .brain import detect_cloud_folders
    from .db import default_db_path

    brain = default_db_path().resolve()
    for _, folder in detect_cloud_folders():
        try:
            brain.relative_to(folder.resolve())
            return True
        except ValueError:
            continue
    return False


def offboard(keep_brain_at: str | Path | None = None) -> Result:
    """Unschedule everything, then delete what StockSage stored here.

    keep_brain_at exports the brain there first — 900 graded calls are not
    reproducible, and someone leaving a machine usually wants the knowledge
    even though they want the credentials gone.
    """
    from .db import default_db_path

    result = Result()
    unschedule(result)

    if keep_brain_at:
        from .brain import export_brain

        try:
            saved = export_brain(keep_brain_at, db_path=default_db_path())
            result.brain_saved_to = str(saved)
        except Exception as exc:
            result.failed.append(f"saving the brain ({type(exc).__name__}: {exc})")
            # Refusing to delete an unsaved brain is the safe failure: the
            # credentials can be revoked, but the learning cannot be rebuilt.
            return result

    shared = brain_is_shared()
    for target in sensitive_targets() + shortcut_targets():
        if not target.exists:
            continue
        if shared and target.what == "the brain":
            # It lives in the owner's cloud folder and travels with them.
            result.brain_left_in_sync_folder = str(target.path)
            continue
        _delete(target, result)
    return result


REVOCATION_STEPS = """WHAT ONLY YOU CAN DO — do these from your phone or another computer.
Deleting files here removes the reference, not reliably the bytes. Revoking
is what actually makes anything left behind worthless.

1. ROBINHOOD — change your password.
   App -> Account -> menu -> Settings -> Security and privacy -> change
   password. This ends every existing session, including any left here.

2. ROBINHOOD — sign out this device and check the history.
   Same screen -> Devices: remove anything you do not recognise. Login
   history shows every sign-in with time and location. Any activity from
   this machine after today is not you.

3. GOOGLE — revoke the Drive access.
   myaccount.google.com/permissions -> find the StockSage app -> Remove
   access. The stored token stops working immediately.

4. If your two-factor seed was ever stored on this machine, re-enrol
   two-factor in Robinhood. That invalidates the old seed.

StockSage could only ever READ your Robinhood account — it cannot place,
change or cancel an order — so nothing left here can move your money."""
